"""Plugin controller: batch lifecycle, worker communication, review actions, export.

All HTTP calls run in QgsTask (TaskRunner); results are applied on the main
thread. Approve / edit / reject / export work on review.gpkg without the worker.
"""

import collections
import datetime as dt
import os
import statistics
import time

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
                       QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsSnappingConfig,
                       QgsVectorFileWriter)
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from . import geometry_ops as G
from . import styling
from .bpa_common import constants as C
from .bpa_common import explain as X
from .bpa_common.config import (add_imagery_to_batch, freeze_config, get, load_config, load_frozen_config,
                                save_imagery_source)
from .bpa_common.excel_io import ExcelSchemaError, copy_input, export_workbook, read_records, write_entry_workbook
from .compat import geom_equal, num, val
from .review_store import ReviewStore, now_iso
from .settings import new_token
from .tasks import TaskRunner
from .worker_client import WorkerClient, WorkerError
from .worker_process import WorkerProcess, port_in_use

REVIEWED = (C.STATUS_APPROVED, C.STATUS_APPROVED_MANUAL, C.STATUS_REJECTED)
LOCKED = (C.STATUS_EDITED, C.STATUS_APPROVED, C.STATUS_APPROVED_MANUAL, C.STATUS_REJECTED)
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


class UserError(Exception):
    """A problem the operator can fix (shown in the message bar)."""


class NeedsConfirmation(Exception):
    """The action needs an explicit operator confirmation (e.g. overlap warning)."""


def _record_key(r, counts):
    sid = r["survey_id"]
    return sid if sid and counts[sid] == 1 else "%s@row%s" % (sid or "EMPTY", r.get("row"))


class Controller(QObject):
    message = pyqtSignal(str, int)            # text, Qgis.MessageLevel value
    records_changed = pyqtSignal()
    record_updated = pyqtSignal(str)
    current_changed = pyqtSignal(str)
    worker_state = pyqtSignal(str)            # stopped | starting | ready | error
    counts_changed = pyqtSignal(dict)
    reference_needed = pyqtSignal(int, int)   # uncovered, total
    busy_changed = pyqtSignal(bool)

    def __init__(self, iface, settings, runner=None):
        super().__init__()
        self.iface = iface
        self.settings = settings
        self.runner = runner or TaskRunner()
        self.cfg = None
        self.batch = None
        self.store = None
        self.records = collections.OrderedDict()
        self.details = {}
        self.loaded_rev = {}
        self._explained = {}                            # record key -> (signature, explanation)
        self.current = None
        self.worker = None
        self.client = None
        self.token = new_token()
        self.layers = {}
        self.crop_layer = None
        self.group = None
        self.worker_batch_id = None
        self._pending_geom = {}
        self._draw_for = None
        self._draw_before = None
        self._edit_kind = None
        self._split_done = set()
        self._fetching = False
        self._finish_reported = False
        self._t_acc = collections.defaultdict(float)   # record key -> active seconds so far
        self._t_mark = None                             # (record key, time it was opened)
        self._fix_inflight = False
        self._fix_queued = None
        self._health_tries = 0
        self._after_ready = []
        self.health = {}
        self.worker_ready = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self.poll)
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(1000)
        self.health_timer.timeout.connect(self._check_health)
        self.auto_prepare_reference = None   # None = ask via reference_needed signal

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def info(self, text):
        self.message.emit(text, int(Qgis.MessageLevel.Info))

    def warn(self, text):
        self.message.emit(text, int(Qgis.MessageLevel.Warning))

    def error(self, text):
        self.message.emit(text, int(Qgis.MessageLevel.Critical))

    def success(self, text):
        self.message.emit(text, int(Qgis.MessageLevel.Success))

    def operator_id(self):
        op = (self.settings.get("operator_id") or "").strip()
        if not op:
            raise UserError("Enter your operator ID in the panel before reviewing")
        return op

    def _cfg(self, dotted, default=None):
        return get(self.cfg or {}, dotted, default)

    def record(self, key):
        return self.records.get(key)

    def epsg_for(self, key):
        d = self.details.get(key) or {}
        if d.get("utm_epsg"):
            return int(d["utm_epsg"])
        r = self.records[key]
        return G.utm_epsg(r["longitude"], r["latitude"])

    # ------------------------------------------------------------------
    # batch import / open
    # ------------------------------------------------------------------
    def import_entries(self, entries):
        """Single-building entry: the typed / picked buildings become a normal batch.

        They are written as a small workbook in the client template layout and imported like
        any Excel file, so the checks, processing, review, export and audit trail are identical.
        """
        if not entries:
            raise UserError("Enter at least one building")
        problems = [p for p in self.settings.problems() if not p.startswith("Worker Python")]
        if problems:
            raise UserError("; ".join(problems))
        cfg = load_config(self.settings.config_path())
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.settings.batches_dir(), "_single_entries", "single_%s.xlsx" % stamp)
        write_entry_workbook(cfg, entries, path)
        return self.import_excel(path, prefix="single")

    def import_excel(self, path, prefix="batch"):
        """S1: copy the workbook into a new batch folder, validate, create review.gpkg."""
        problems = [p for p in self.settings.problems() if not p.startswith("Worker Python")]
        if problems:
            raise UserError("; ".join(problems))
        cfg = load_config(self.settings.config_path())
        batch_id = "%s_%s" % (prefix, dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
        batch_dir = os.path.join(self.settings.batches_dir(), batch_id)
        os.makedirs(batch_dir, exist_ok=True)
        for sub in ("cache", "output"):
            os.makedirs(os.path.join(batch_dir, sub), exist_ok=True)
        input_copy = copy_input(path, batch_dir)
        cfg_path = os.path.join(batch_dir, "config.yaml")
        freeze_config(cfg, cfg_path)
        bcfg = load_frozen_config(cfg_path)
        try:
            records, report = read_records(input_copy, bcfg)
        except ExcelSchemaError as exc:
            raise UserError("Workbook could not be imported: %s" % exc)
        store = ReviewStore(os.path.join(batch_dir, "review.gpkg"))
        store.load_survey_points(records)
        store.set_meta(batch_id=batch_id, source_workbook=os.path.abspath(path), input_copy=input_copy,
                       created_at=now_iso(), config_version=get(bcfg, "config_version"),
                       operator=self.settings.get("operator_id") or "")
        self._open(batch_id, batch_dir, bcfg, store, records)
        self.success("Imported %d records (%d invalid) into %s" % (report["total"], report["invalid"], batch_id))
        return report

    def open_batch(self, batch_dir):
        """Re-open an existing batch folder (review.gpkg keeps all review work)."""
        cfg_path = os.path.join(batch_dir, "config.yaml")
        gpkg = os.path.join(batch_dir, "review.gpkg")
        if not (os.path.isfile(cfg_path) and os.path.isfile(gpkg)):
            raise UserError("Not a batch folder (config.yaml and review.gpkg expected): %s" % batch_dir)
        bcfg = load_frozen_config(cfg_path)
        store = ReviewStore(gpkg)
        meta = store.meta()
        input_copy = meta.get("input_copy") or ""
        if not os.path.isfile(input_copy):
            inputs = [f for f in os.listdir(os.path.join(batch_dir, "input")) if f.lower().endswith(".xlsx")]
            input_copy = os.path.join(batch_dir, "input", inputs[0]) if inputs else ""
        records, _ = read_records(input_copy, bcfg)
        states = {r["survey_id"]: r for r in store.survey_rows()}
        for r in records:
            s = states.get(r["survey_id"])
            if s and s.get("state"):
                r["state"] = s["state"]
                r["route"] = s.get("route")
                r["confidence"] = s.get("confidence")
        self._open(meta.get("batch_id") or os.path.basename(batch_dir), batch_dir, bcfg, store, records)
        self.worker_batch_id = meta.get("worker_batch_id") or None
        self.info("Opened %s (%d records). Approve, edit, draw, reject and export work now; click Process "
                  "once to reconnect the worker for Pick and Fix (review work is kept)."
                  % (os.path.basename(batch_dir), len(records)))

    def _open(self, batch_id, batch_dir, cfg, store, records):
        self.close_batch(remove_layers=True)
        self.cfg = cfg
        self.store = store
        meta = store.meta()
        self.batch = {"batch_id": batch_id, "batch_dir": batch_dir,
                      "input_copy": meta.get("input_copy"), "config_path": os.path.join(batch_dir, "config.yaml")}
        counts = collections.Counter(r["survey_id"] for r in records)
        self.records = collections.OrderedDict()
        for r in records:
            r = dict(r)
            r.setdefault("route", None)
            r.setdefault("confidence", None)
            self.records[_record_key(r, counts)] = r
        self.details, self.loaded_rev, self._explained = {}, {}, {}
        self._load_layers()
        self.records_changed.emit()
        self.counts_changed.emit(self.counts())
        self.zoom_to_batch()

    def close_batch(self, remove_layers=False):
        self.poll_timer.stop()
        if remove_layers and self.group is not None:
            try:
                ids = [l.layerId() for l in self.group.findLayers()]
                QgsProject.instance().removeMapLayers(ids)
                parent = self.group.parent()
                if parent is not None:
                    parent.removeChildNode(self.group)
            except RuntimeError:
                pass
        self.group = None
        self.layers = {}
        self.crop_layer = None
        self.current = None

    # ------------------------------------------------------------------
    # layers
    # ------------------------------------------------------------------
    def _load_layers(self):
        proj = QgsProject.instance()
        root = proj.layerTreeRoot()
        self.group = root.insertGroup(0, "BPA %s" % self.batch["batch_id"])
        spec = [("corrected_points", "Corrected points", styling.style_corrected),
                ("survey_points", "Survey points", styling.style_survey_points),
                ("buildings", "Buildings", styling.style_buildings),
                ("candidates", "Candidates", styling.style_candidates)]
        from qgis.core import QgsVectorLayer
        for name, title, style in spec:
            lyr = QgsVectorLayer(self.store.uri(name), title, "ogr")
            if not lyr.isValid():
                raise UserError("Layer %s could not be opened from review.gpkg" % name)
            style(lyr)
            proj.addMapLayer(lyr, False)
            self.group.addLayer(lyr)
            self.layers[name] = lyr
            if name != "candidates":
                self.store.adopt(name, lyr)   # writes go through the displayed layer
        b = self.layers["buildings"]
        form = b.editFormConfig()
        form.setSuppress(Qgis.AttributeFormSuppression.On)
        b.setEditFormConfig(form)
        for name in ("survey_points", "corrected_points", "candidates"):
            self.layers[name].setReadOnly(True)
        b.beforeCommitChanges.connect(self._before_commit)
        b.committedFeaturesAdded.connect(self._features_committed)
        b.afterCommitChanges.connect(self._after_commit)
        b.featureAdded.connect(self._feature_added)
        self.layers["candidates"].setSubsetString("\"survey_id\" = ''")

    def zoom_to_batch(self):
        lyr = self.layers.get("survey_points")
        if lyr is None or self.iface is None:
            return
        lyr.updateExtents()
        ext = lyr.extent()
        if ext.isEmpty() or not ext.isFinite():
            return
        canvas = self.iface.mapCanvas()
        tr = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        r = tr.transformBoundingBox(ext)
        r.scale(1.1)
        canvas.setExtent(r)
        canvas.refresh()

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    def ensure_worker(self, then=None):
        """Start the worker if needed and call ``then`` once /health answers."""
        if then:
            self._after_ready.append(then)
        if self.client is not None and self.worker_ready:
            self._flush_ready()
            return
        port = int(self.settings.get("port"))
        if self.worker is None or not self.worker.is_running():
            if port_in_use(port):
                # A worker started outside QGIS (e.g. from a terminal) is reused if it is open.
                self.client = WorkerClient("http://127.0.0.1:%d" % port, None)
            else:
                py = self.settings.worker_python()
                if not py or not os.path.isfile(py):
                    raise UserError("Worker Python not found - set it in Settings")
                cfg_path = self.settings.config_path()
                self.worker = WorkerProcess(py, self.settings.worker_dir(), cfg_path, port, self.token,
                                            load_model=self.settings.get("load_model"), parent=self)
                self.worker.stopped.connect(self._worker_stopped)
                try:
                    self.worker.start()
                except WorkerError as exc:
                    self.worker_state.emit("error")
                    raise UserError(str(exc))
                self.client = self.worker.client
        self.worker_ready = False
        self._health_tries = 0
        self.worker_state.emit("starting")
        self.health_timer.start()

    def _check_health(self):
        self._health_tries += 1
        if self._health_tries > 180:
            self.health_timer.stop()
            self.worker_state.emit("error")
            tail = self.worker.log_tail() if self.worker else ""
            self.error("Worker did not answer within 3 minutes.\n%s" % tail)
            return
        client = self.client

        def done(h):
            if self.worker_ready:
                return
            if h.get("auth_required") and not client.token:
                self.health_timer.stop()
                self.worker_state.emit("error")
                self.error("A protected worker is already running on this port; stop it or change the port")
                return
            self.health_timer.stop()
            self.worker_ready = True
            self.health = h
            self.worker_state.emit("ready")
            ref = h.get("reference", {})
            if ref.get("manifest_ok") is False:
                self.warn("Reference data problems: %s" % "; ".join(ref.get("errors") or []))
            self._flush_ready()

        self.runner.run("BPA worker health", lambda: client.health(timeout=2), done, lambda e: None)

    def _flush_ready(self):
        fns, self._after_ready = self._after_ready, []
        for fn in fns:
            fn()

    def _worker_stopped(self, code):
        self.worker_ready = False
        self.worker_state.emit("stopped")
        if code not in (0, None):
            self.warn("Worker stopped (exit code %s).\n%s" % (code, self.worker.log_tail() if self.worker else ""))

    def stop_worker(self):
        self.poll_timer.stop()
        self.health_timer.stop()
        if self.worker is not None:
            self.worker.stop()
        self.worker_ready = False
        self.worker_state.emit("stopped")

    # ------------------------------------------------------------------
    # processing
    # ------------------------------------------------------------------
    def process(self, skip_reference_check=False):
        if not self.batch:
            raise UserError("Import a workbook first")
        valid = [r for r in self.records.values() if r["state"] != C.INVALID and r.get("latitude") is not None]
        if not valid:
            raise UserError("There are no valid records to process")
        points = [[r["longitude"], r["latitude"]] for r in valid]

        def check():
            if skip_reference_check:
                self._submit()
                return

            def got(cov):
                if cov.get("uncovered"):
                    if self.auto_prepare_reference is True:
                        self.prepare_reference()
                    elif self.auto_prepare_reference is False:
                        self._submit()
                    else:
                        self.reference_needed.emit(int(cov["uncovered"]), int(cov["total"]))
                else:
                    self._submit()

            self.runner.run("BPA reference coverage", lambda: self.client.coverage(points), got,
                            lambda e: self.error("Coverage check failed: %s" % e))

        self.ensure_worker(check)

    def prepare_reference(self):
        """Incremental S0 for this batch's survey points, then submit the batch."""
        points = [[r["longitude"], r["latitude"]] for r in self.records.values()
                  if r["state"] != C.INVALID and r.get("latitude") is not None]
        self.busy_changed.emit(True)
        self.info("Downloading reference data for the uncovered areas (one-time, needs internet)...")

        def started(_):
            self._prep_timer = QTimer(self)
            self._prep_timer.setInterval(3000)

            def tick():
                def status(s):
                    if s.get("status") == "done":
                        self._prep_timer.stop()
                        self.busy_changed.emit(False)
                        self.success("Reference data ready: %s" % s.get("result"))
                        self._submit()
                    elif s.get("status") == "error":
                        self._prep_timer.stop()
                        self.busy_changed.emit(False)
                        self.error("Reference download failed: %s" % s.get("error"))
                self.runner.run("BPA reference status", self.client.prepare_status, status, lambda e: None)

            self._prep_timer.timeout.connect(tick)
            self._prep_timer.start()

        self.runner.run("BPA prepare reference", lambda: self.client.prepare_reference(points), started,
                        lambda e: (self.busy_changed.emit(False), self.error("Could not start download: %s" % e)))

    def _submit(self):
        recs = []
        for key, r in self.records.items():
            state = C.INVALID if r.get("reasons") else C.IMPORTED
            recs.append({"survey_id": r["survey_id"], "row": r.get("row"), "latitude": r.get("latitude"),
                         "longitude": r.get("longitude"), "fields": r.get("fields", {}),
                         "source": r.get("source", {}), "warnings": r.get("warnings", []),
                         "reasons": r.get("reasons", []), "state": state})
        payload = {"batch_id": self.batch["batch_id"], "batch_dir": self.batch["batch_dir"],
                   "config_path": self.batch["config_path"], "config_version": self._cfg("config_version"),
                   "records": recs}

        def done(res):
            self.worker_batch_id = res["batch_id"]
            self.store.set_meta(worker_batch_id=res["batch_id"], submitted_at=now_iso(),
                                manifest_hash=(res.get("versions") or {}).get("manifest_hash") or "")
            self.loaded_rev = {}
            self._finish_reported = False
            self.info("Batch submitted: %d records queued, %d invalid" % (res["accepted"], res["invalid"]))
            self.busy_changed.emit(True)
            self.poll_timer.start()
            self.poll()

        self.runner.run("BPA submit batch", lambda: self.client.create_batch(payload), done,
                        lambda e: self.error("Batch submission failed: %s" % e))

    def poll(self):
        if not self.worker_batch_id or self.client is None:
            return
        bid = self.worker_batch_id

        def got(summary):
            changed = []
            for s in summary.get("records", []):
                sid = s["survey_id"]
                if s["state"] in C.TERMINAL_WORKER_STATES and self.loaded_rev.get(sid) != s.get("revision"):
                    changed.append(sid)
            self.counts_changed.emit(dict(self.counts(), worker=summary.get("counts", {}),
                                          processed=summary.get("processed"), total=summary.get("total"),
                                          status=summary.get("status")))
            finished = summary.get("status") in ("done", "error")
            if self._fetching:
                return                      # a results fetch is still running; next poll catches up

            def fetched(res):
                self._fetching = False
                self._apply_many(res.get("records", []), finished)

            def failed(e):
                self._fetching = False
                self.warn("Could not fetch results: %s" % e)

            if changed:
                self._fetching = True
                self.runner.run("BPA fetch results", lambda: self.client.batch_records(bid, only_final=True),
                                fetched, failed)
            elif finished:
                self._finish_polling(summary)

        self.runner.run("BPA batch status", lambda: self.client.batch(bid), got,
                        lambda e: self.warn("Worker status: %s" % e))

    def _apply_many(self, details, finished):
        for d in details:
            if self.loaded_rev.get(d["survey_id"]) != d.get("revision"):
                self.apply_detail(d)
        self.records_changed.emit()
        self.counts_changed.emit(self.counts())
        if finished:
            self._finish_polling(None)

    def _finish_polling(self, summary):
        if self._finish_reported:
            return
        self._finish_reported = True
        self.poll_timer.stop()
        self.busy_changed.emit(False)
        client = self.client
        self.runner.run("BPA worker health", lambda: client.health(timeout=5),
                        lambda h: setattr(self, "health", h), lambda e: None)
        c = self.counts()
        self.success("Processing finished: %d to review, %d manual, %d failed" % (
            c.get("REVIEW", 0), c.get("MANUAL", 0), c.get("FAILED", 0)))

    def retry_failed(self):
        if not self.worker_batch_id:
            raise UserError("Nothing submitted yet")
        def requeued(r):
            self._finish_reported = False
            self.info("%s record(s) re-queued" % r.get("requeued"))
            self.poll_timer.start()

        self.runner.run("BPA retry", lambda: self.client.retry_failed(self.worker_batch_id), requeued,
                        lambda e: self.error(str(e)))

    # ------------------------------------------------------------------
    # applying worker results
    # ------------------------------------------------------------------
    def _key_for(self, survey_id):
        if survey_id in self.records:
            return survey_id
        for k, r in self.records.items():
            if r["survey_id"] == survey_id:
                return k
        return None

    def apply_detail(self, d, force=False, action=None):
        """Store a worker record detail in review.gpkg (proposal, candidates, state)."""
        key = self._key_for(d["survey_id"])
        if key is None:
            return
        sid = d["survey_id"]
        self.details[key] = d
        self.loaded_rev[sid] = d.get("revision")
        rec = self.records[key]
        prop = d.get("proposal") or {}
        reviewed = rec.get("state") in (C.APPROVED, C.APPROVED_MANUAL, C.REJECTED)
        if not reviewed or force:
            rec["state"] = d["state"]
        rec["route"] = prop.get("route")
        rec["would_route"] = prop.get("would_route")
        rec["confidence"] = prop.get("confidence")
        rec["method"] = prop.get("method")
        rec["utm_epsg"] = d.get("utm_epsg")
        survey_attrs = {"route": prop.get("route"), "would_route": prop.get("would_route"),
                        "confidence": prop.get("confidence"), "utm_epsg": d.get("utm_epsg")}
        if not reviewed or force:
            survey_attrs["state"] = d["state"]
        self.store.set_survey(sid, **survey_attrs)

        cands = []
        for c in d.get("candidates", []):
            cands.append({"candidate_id": c["candidate_id"], "rank": c["rank"], "score": c["score"],
                          "dataset": c.get("dataset"), "chosen": 1 if c.get("chosen") else 0,
                          "label": "#%d %.2f" % (c["rank"], c["score"]),
                          "geometry": G.geojson_to_geometry(c.get("geometry"))})
        self.store.replace_candidates(sid, cands)
        if self.layers.get("candidates") is not None:
            self.layers["candidates"].dataProvider().reloadData()
            self.layers["candidates"].triggerRepaint()

        b = self.store.building(sid)
        locked = b is not None and val(b["status"]) in LOCKED
        geom = G.geojson_to_geometry(prop.get("geometry")) if prop.get("geometry") else None
        usable = geom is not None and not geom.isEmpty() and prop.get("route") != C.ROUTE_MANUAL
        if action == C.ACTION_CLICK_TO_FIX and not usable and b is not None:
            # A refused click-to-fix never throws away the operator's current polygon.
            reasons = ", ".join(C.FLAG_TEXT.get(h, h) for h in prop.get("hard_failures") or []) or "no usable mask"
            self.store.add_action(sid, self.settings.get("operator_id") or "", action,
                                  note="refused: %s; previous polygon kept" % reasons, after=geom)
            self.warn("Click-to-fix result for %s was refused (%s) - the previous polygon is kept. "
                      "Add more points inside the roof or drag a box." % (sid, reasons))
            self.record_updated.emit(key)
            if key == self.current:
                self.current_changed.emit(key)
            return
        if force or not locked:
            before = QgsGeometry(b.geometry()) if b is not None else None
            if usable:
                attrs = self._building_attrs(sid, prop, d)
                attrs.update({"status": C.STATUS_PROPOSED, "reviewer": None, "reviewed_at": None,
                              "reason_code": None})
                self.store.upsert_building(sid, geom, attrs)
                self.store.delete_corrected(sid)
                self.store.add_action(sid, action and self.settings.get("operator_id") or "worker",
                                      action or C.ACTION_LOAD_PROPOSAL, before=before, after=geom,
                                      note="%s rev %s" % (prop.get("method"), d.get("revision")))
            elif b is not None and val(b["status"]) == C.STATUS_PROPOSED:
                self.store.delete_buildings([b.id()])
        self.record_updated.emit(key)
        if key == self.current:
            self.current_changed.emit(key)

    def _building_attrs(self, sid, prop, d):
        versions = d.get("versions") or {}
        seg = d.get("segmentation") or {}
        return {"polygon_id": "%s-%s" % (self.batch["batch_id"], sid), "method": prop.get("method"),
                "confidence": prop.get("confidence"), "route": prop.get("route"),
                "would_route": prop.get("would_route"), "area_m2": prop.get("area_m2"),
                "source_proposal_ref": "work.gpkg:proposal:%s:rev%s" % (sid, d.get("revision")),
                "footprint_source": prop.get("footprint_source"), "imagery_source": prop.get("imagery_source"),
                "flags": ",".join((prop.get("flags") or []) + (prop.get("notes") or [])),
                "config_version": versions.get("config_version") or self._cfg("config_version"),
                # the segmentation model only counts when it actually produced the geometry
                "model_version": seg.get("model_version") if "SAM" in (prop.get("method") or "")
                or prop.get("method") == C.METHOD_CLICK_TO_FIX else None,
                "manifest_hash": versions.get("manifest_hash") or self.store.meta().get("manifest_hash") or None}

    # ------------------------------------------------------------------
    # queue and navigation
    # ------------------------------------------------------------------
    def counts(self):
        c = collections.Counter(r.get("state") for r in self.records.values())
        return dict(c)

    def queue(self, filt="all"):
        """Record keys sorted: open work first by route (manual, review), lowest confidence first."""
        def group(r):
            s = r.get("state")
            if s in (C.REVIEW, C.MANUAL, C.AUTO_ACCEPTED):
                return 0
            if s in (C.APPROVED, C.APPROVED_MANUAL, C.REJECTED):
                return 2
            if s in (C.INVALID, C.FAILED):
                return 3
            return 1

        def key(k):
            r = self.records[k]
            conf = r.get("confidence")
            return (group(r), C.ROUTE_ORDER.get(r.get("route"), 9), conf if conf is not None else -1.0, r.get("row") or 0)

        keys = sorted(self.records, key=key)
        if filt in X.OPEN_LANES:
            return [k for k in keys if self.explanation(k)["lane"] == filt]
        f = self._FILTERS.get(filt)
        return [k for k in keys if f is None or f(self.records[k])]

    _FILTERS = {"open": lambda r: r.get("state") in (C.REVIEW, C.MANUAL, C.AUTO_ACCEPTED),
                "review": lambda r: r.get("state") == C.REVIEW,
                "manual": lambda r: r.get("state") == C.MANUAL,
                "approved": lambda r: r.get("state") in (C.APPROVED, C.APPROVED_MANUAL),
                "rejected": lambda r: r.get("state") == C.REJECTED,
                "problems": lambda r: r.get("state") in (C.INVALID, C.FAILED)}

    def filter_counts(self):
        """Number of records per queue filter (one pass, for the filter drop-down)."""
        out = collections.Counter(all=len(self.records))
        for k, r in self.records.items():
            for name, f in self._FILTERS.items():
                if f(r):
                    out[name] += 1
            out[self.explanation(k)["lane"]] += 1
        return out

    def explanation(self, key):
        """Plain-language lane / why / doubts / what-to-do for one record (bpa_common.explain)."""
        r = self.records.get(key) or {}
        d = self.details.get(key)
        sig = (r.get("state"), id(d), (d or {}).get("revision"), tuple(r.get("warnings") or ()))
        hit = self._explained.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        e = X.explain(r, d, close_margin=float(self._cfg("scoring.decision.min_margin", 0.15)))
        self._explained[key] = (sig, e)
        return e

    # -- operator timing (productivity report) ------------------------------------------------
    IDLE_CAP_S = 600.0   # a record left open longer than this counts as at most 10 minutes

    def _touch(self, key):
        now = time.monotonic()
        if self._t_mark and self._t_mark[0] != key:
            prev, t0 = self._t_mark
            self._t_acc[prev] += min(now - t0, self.IDLE_CAP_S)
            self._t_mark = None
        if self._t_mark is None:
            self._t_mark = (key, now)

    def _finish_time(self, key):
        """Active seconds spent on ``key`` since it was last completed; resets its clock."""
        now = time.monotonic()
        spent = self._t_acc.pop(key, 0.0)
        if self._t_mark and self._t_mark[0] == key:
            spent += min(now - self._t_mark[1], self.IDLE_CAP_S)
            self._t_mark = (key, now)
        return spent

    def select(self, key, zoom=True):
        if key not in self.records:
            return
        self._touch(key)
        self.current = key
        r = self.records[key]
        sid = r["survey_id"]
        cl = self.layers.get("candidates")
        if cl is not None:
            cl.setSubsetString("\"survey_id\" = '%s'" % sid.replace("'", "''"))
        self._show_crop(key)
        if zoom and self.iface is not None:
            self._zoom_to_record(key)
        self.current_changed.emit(key)

    def next_open(self):
        order = self.queue("open")
        if not order:
            return None
        if self.current in order:
            i = order.index(self.current)
            nxt = order[i + 1] if i + 1 < len(order) else None
        else:
            nxt = order[0]
        if nxt:
            self.select(nxt)
        return nxt

    # -- quick check (many proposals at once) ---------------------------------------------------
    def quick_check_keys(self, limit=12, exclude=()):
        """Records ready for a quick visual check: in REVIEW with an untouched proposal.

        Clear identities first, then by confidence (highest first). Records whose
        proposal failed a hard gate never appear here.
        """
        status = {val(f["survey_id"]): val(f["status"]) for f in self.store.all_buildings()}
        rows = []
        for k, r in self.records.items():
            if k in exclude or r.get("state") != C.REVIEW or status.get(r["survey_id"]) != C.STATUS_PROPOSED:
                continue
            d = self.details.get(k) or {}
            if (d.get("proposal") or {}).get("hard_failures"):
                continue
            clear = d.get("decision") == C.IDENTITY_CLEAR
            rows.append((0 if clear else 1, -(r.get("confidence") or 0.0), r.get("row") or 0, k))
        rows.sort()
        return [k for _a, _b, _c, k in rows[:limit]]

    def approve_many(self, keys, seconds_total=0.0, note="quick check"):
        """Approve several records after a quick visual check.

        Records that need a decision (overlap with an approved building, a failed
        check) are held back for normal review instead of being approved.
        """
        self.operator_id()
        per = float(seconds_total) / max(1, len(keys))
        approved, held = [], []
        for k in keys:
            try:
                self.approve(k, duration_s=per, quiet=True, note=note)
                approved.append(k)
            except (NeedsConfirmation, UserError) as exc:
                held.append((k, str(exc)))
        self._refresh_layers()
        self.counts_changed.emit(self.counts())
        msg = "Quick check: %d approved" % len(approved)
        if held:
            msg += ", %d kept for normal review" % len(held)
        self.success(msg)
        return {"approved": approved, "held": held}

    # -- productivity and accuracy evidence ---------------------------------------------------
    def productivity_report(self, write=True, day_hours=8.0):
        """Seconds per record, projected records per day and first-time-right rate, from the audit trail."""
        by_sid = collections.defaultdict(list)
        for a in self.store.actions():
            if a["survey_id"]:
                by_sid[a["survey_id"]].append(a)
        finals = (C.ACTION_APPROVE, C.ACTION_REJECT) + C.DRAW_ACTIONS
        rows = []
        for key, r in self.records.items():
            acts = sorted(by_sid.get(r["survey_id"], []), key=lambda a: a["timestamp"] or "")
            done = [a for a in acts if a["action"] in finals]
            if not done:
                continue
            kinds = {a["action"] for a in acts}
            if done[-1]["action"] == C.ACTION_REJECT:
                outcome = "rejected"
            elif kinds & set(C.DRAW_ACTIONS):
                outcome = "drawn by operator"
            elif C.ACTION_PICK_CANDIDATE in kinds:
                outcome = "other building picked"
            elif kinds & set(C.OUTLINE_FIX_ACTIONS):
                outcome = "outline fixed"
            else:
                outcome = "approved as proposed"
            seconds = sum(float(a["duration_s"]) for a in done if a["duration_s"] is not None)
            rows.append({"survey_id": r["survey_id"], "row": r.get("row"), "outcome": outcome,
                         "seconds": round(seconds, 1),
                         "quick_check": any("quick check" in (a["note"] or "") for a in done),
                         "operator": done[-1]["operator_id"] or "", "finished_at": done[-1]["timestamp"]})
        outcomes = collections.Counter(x["outcome"] for x in rows)
        timed = [x["seconds"] for x in rows if x["seconds"] > 0]
        mean_s = statistics.mean(timed) if timed else None
        reviewed = len(rows) - outcomes.get("rejected", 0)
        summary = {
            "records_finished": len(rows),
            "records_total": len(self.records),
            "seconds_per_record_mean": round(mean_s, 1) if mean_s else None,
            "seconds_per_record_median": round(statistics.median(timed), 1) if timed else None,
            "projected_records_per_day": int(day_hours * 3600 / mean_s) if mean_s else None,
            "day_hours": day_hours,
            "outcomes": dict(outcomes),
            "first_time_right_rate": round(outcomes.get("approved as proposed", 0) / reviewed, 3) if reviewed else None,
            "right_building_rate": round((reviewed - outcomes.get("other building picked", 0)
                                          - outcomes.get("drawn by operator", 0)) / reviewed, 3) if reviewed else None,
            "approved_in_quick_check": sum(1 for x in rows if x["quick_check"]),
            "by_operator": {},
        }
        for op in sorted({x["operator"] for x in rows}):
            mine = [x for x in rows if x["operator"] == op]
            t = [x["seconds"] for x in mine if x["seconds"] > 0]
            summary["by_operator"][op or "-"] = {
                "records": len(mine), "seconds_per_record_mean": round(statistics.mean(t), 1) if t else None,
                "projected_records_per_day": int(day_hours * 3600 / statistics.mean(t)) if t else None}
        if write and self.batch:
            import csv
            import json
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = os.path.join(self.batch["batch_dir"], "output")
            os.makedirs(out_dir, exist_ok=True)
            csv_path = os.path.join(out_dir, "productivity_%s.csv" % stamp)
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["row", "survey_id", "outcome", "seconds", "quick_check",
                                                   "operator", "finished_at"])
                w.writeheader()
                w.writerows(sorted(rows, key=lambda x: x["row"] or 0))
            json_path = os.path.join(out_dir, "productivity_%s.json" % stamp)
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2)
            summary["files"] = [csv_path, json_path]
        return summary

    def _zoom_to_record(self, key):
        r = self.records[key]
        canvas = self.iface.mapCanvas()
        b = self.store.building(r["survey_id"])
        if b is not None and not b.geometry().isEmpty():
            rect = QgsRectangle(b.geometry().boundingBox())
        elif r.get("longitude") is not None:
            rect = QgsRectangle(QgsPointXY(r["longitude"], r["latitude"]), QgsPointXY(r["longitude"], r["latitude"]))
        else:
            return
        if r.get("longitude") is not None:
            rect.combineExtentWith(QgsPointXY(r["longitude"], r["latitude"]))
        tr = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        out = tr.transformBoundingBox(rect)
        pad = max(out.width(), out.height(), 1e-9)
        dest_units_m = canvas.mapSettings().destinationCrs().mapUnits() == Qgis.DistanceUnit.Meters
        minimum = 90.0 if dest_units_m else 0.0008
        grow = max(pad * 0.8, minimum)
        out.grow(grow)
        canvas.setExtent(out)
        canvas.refresh()

    def _show_crop(self, key):
        proj = QgsProject.instance()
        if self.crop_layer is not None:
            try:
                proj.removeMapLayer(self.crop_layer.id())
            except RuntimeError:
                pass
            self.crop_layer = None
        d = self.details.get(key) or {}
        path = d.get("crop_path")
        if not path or not os.path.isfile(path) or self.group is None:
            return
        lyr = QgsRasterLayer(path, "Imagery crop %s" % self.records[key]["survey_id"])
        if lyr.isValid():
            proj.addMapLayer(lyr, False)
            self.group.addLayer(lyr)
            self.crop_layer = lyr

    # ------------------------------------------------------------------
    # review actions
    # ------------------------------------------------------------------
    def _require_current(self, key=None):
        key = key or self.current
        if not key or key not in self.records:
            raise UserError("Select a record first")
        if self.records[key].get("state") == C.INVALID:
            raise UserError("This row is INVALID (%s); fix the source workbook and re-import"
                            % "; ".join(self.records[key].get("reasons") or []))
        return key

    def _commit_buildings(self):
        lyr = self.layers.get("buildings")
        if lyr is not None and lyr.isEditable():
            if lyr.isModified() and not lyr.commitChanges(True):
                raise UserError("Could not save edits: %s" % "; ".join(lyr.commitErrors()))
            if lyr.isEditable():
                lyr.commitChanges(True)
            self.iface and self.iface.mapCanvas().refresh()

    def approve(self, key=None, confirm_overlap=False, duration_s=None, quiet=False, note=None):
        key = self._require_current(key)
        operator = self.operator_id()
        self._commit_buildings()
        r = self.records[key]
        sid = r["survey_id"]
        b = self.store.building(sid)
        if b is None or b.geometry().isEmpty():
            raise UserError("No polygon to approve for %s - draw it (M) or pick a candidate (C)" % sid)
        if val(b["status"]) == C.STATUS_REJECTED:
            raise UserError("%s was rejected; pick a candidate, re-segment or draw to reopen it" % sid)
        epsg = self.epsg_for(key)
        geom, errors, area = G.validate_for_approval(b.geometry(), epsg, self._cfg("validation", {}))
        if errors:
            raise UserError("Cannot approve %s: %s" % (sid, "; ".join(errors)))
        others = [(val(f["survey_id"]), f.geometry()) for f in self.store.all_buildings(C.EXPORTABLE_STATUSES)
                  if val(f["survey_id"]) != sid]
        share = G.overlap_share(geom, [g for _, g in others], epsg) if others else 0.0
        limit = float(self._cfg("validation.approved_overlap_ratio", 0.10))
        if share > limit and not confirm_overlap:
            same = [s for s, g in others if G.overlap_share(g, [geom], epsg) > 0.9 and share > 0.9]
            hint = (" It looks like the same building as %s (duplicate survey?)." % ", ".join(same)) if same else ""
            raise NeedsConfirmation("The polygon overlaps an approved building by %.0f%%.%s Approve anyway?"
                                    % (share * 100, hint))
        method = val(b["method"]) or ""
        status = C.STATUS_APPROVED_MANUAL if method == C.METHOD_MANUAL else C.STATUS_APPROVED
        cc = G.corrected_coordinate(geom, r["longitude"], r["latitude"], epsg, self._cfg("corrected_coordinate", {}))
        before = QgsGeometry(b.geometry())
        self.store.update_building(b.id(), {"status": status, "reviewer": operator, "reviewed_at": now_iso(),
                                            "area_m2": round(area, 1), "reason_code": None,
                                            "polygon_id": val(b["polygon_id"]) or "%s-%s" % (self.batch["batch_id"], sid)},
                                   geom=geom if not geom_equal(geom, before) else None)
        self.store.upsert_corrected(sid, cc)
        self.store.set_survey(sid, state=status)
        notes = [n for n in (note, "overlap %.0f%% confirmed" % (share * 100) if share > limit else None) if n]
        self.store.add_action(sid, operator, C.ACTION_APPROVE, before=before, after=geom, note="; ".join(notes) or None,
                              duration_s=duration_s if duration_s is not None else self._finish_time(key))
        r["state"] = status
        self.record_updated.emit(key)
        if quiet:
            return cc
        self._refresh_layers()
        self.counts_changed.emit(self.counts())
        self.success("%s approved (%s, %.0f m2, corrected %.6f, %.6f)" % (sid, status, area, cc["corrected_lat"], cc["corrected_lon"]))
        if self.settings.get("auto_next"):
            self.next_open()
        return cc

    def reject(self, reason_code, note="", key=None):
        key = self._require_current(key)
        operator = self.operator_id()
        if reason_code not in C.REJECT_REASONS:
            raise UserError("Choose a reject reason")
        self._commit_buildings()
        r = self.records[key]
        sid = r["survey_id"]
        b = self.store.building(sid)
        if b is not None:
            self.store.update_building(b.id(), {"status": C.STATUS_REJECTED, "reviewer": operator,
                                                "reviewed_at": now_iso(), "reason_code": reason_code})
        self.store.delete_corrected(sid)
        self.store.set_survey(sid, state=C.REJECTED)
        self.store.add_action(sid, operator, C.ACTION_REJECT, reason_code=reason_code, note=note,
                              before=b.geometry() if b is not None else None, duration_s=self._finish_time(key))
        r["state"] = C.REJECTED
        self._refresh_layers()
        self.record_updated.emit(key)
        self.counts_changed.emit(self.counts())
        self.info("%s rejected (%s)" % (sid, reason_code))
        if self.settings.get("auto_next"):
            self.next_open()

    def _snapping(self):
        proj = QgsProject.instance()
        sc = QgsSnappingConfig(proj)
        sc.setEnabled(True)
        sc.setMode(Qgis.SnappingMode.AllLayers)
        sc.setTypeFlag(Qgis.SnappingTypes(Qgis.SnappingType.Vertex | Qgis.SnappingType.Segment))
        sc.setTolerance(10)
        sc.setUnits(Qgis.MapToolUnit.Pixels)
        proj.setSnappingConfig(sc)
        if self.settings.get("avoid_overlap"):
            proj.setAvoidIntersectionsMode(Qgis.AvoidIntersectionsMode.AvoidIntersectionsLayers)
            proj.setAvoidIntersectionsLayers([self.layers["buildings"]])

    def start_edit(self, key=None):
        key = self._require_current(key)
        self.operator_id()
        sid = self.records[key]["survey_id"]
        b = self.store.building(sid)
        if b is None:
            raise UserError("No polygon to edit for %s - use Draw (M)" % sid)
        self._start_outline_tool(sid, b, C.ACTION_EDIT)
        if self.iface is not None:
            self.iface.actionVertexTool().trigger()
        self.info("Edit %s with the Vertex tool, then save (Ctrl+S) and Approve (A)" % sid)

    def _start_outline_tool(self, sid, feature, kind):
        lyr = self.layers["buildings"]
        self._snapping()
        if self.iface is not None:
            self.iface.setActiveLayer(lyr)
        lyr.startEditing()
        lyr.selectByIds([feature.id()])
        self._edit_kind = kind

    def _building_or_error(self, key, what):
        sid = self.records[key]["survey_id"]
        b = self.store.building(sid)
        if b is None or b.geometry().isEmpty():
            raise UserError("No polygon to %s for %s - use Box (B) or Draw (M)" % (what, sid))
        if val(b["status"]) == C.STATUS_REJECTED:
            raise UserError("%s was rejected; pick a candidate or draw it to reopen it" % sid)
        return sid, b

    def start_shift(self, key=None):
        """Move the whole outline (ML footprints are often right in shape but 1-3 m off)."""
        key = self._require_current(key)
        self.operator_id()
        sid, b = self._building_or_error(key, "move")
        self._start_outline_tool(sid, b, C.ACTION_SHIFT)
        if self.iface is not None:
            self.iface.actionMoveFeature().trigger()
        self.info("Drag the outline of %s onto the roof, then save (Ctrl+S) and Approve (A)" % sid)

    def start_split(self, key=None):
        """Cut a footprint that covers several buildings; the part with the survey point is kept."""
        key = self._require_current(key)
        self.operator_id()
        sid, b = self._building_or_error(key, "split")
        self._start_outline_tool(sid, b, C.ACTION_SPLIT)
        if self.iface is not None:
            self.iface.actionSplitFeatures().trigger()
        self.info("Draw a line across %s where the buildings meet (right-click to finish), then save (Ctrl+S). "
                  "The part with the survey point is kept." % sid)

    def add_box(self, geom_wgs84, key=None):
        """Store a building drawn with the 3-click box tool; approved as APPROVED_MANUAL when valid."""
        key = self._require_current(key)
        operator = self.operator_id()
        self._commit_buildings()
        r = self.records[key]
        sid = r["survey_id"]
        olds = self.store.features("buildings", "survey_id", sid)
        before = QgsGeometry(olds[0].geometry()) if olds else None
        epsg = self.epsg_for(key)
        geom, errors, area = G.validate_for_approval(geom_wgs84, epsg, self._cfg("validation", {}))
        if errors:
            raise UserError("Box for %s not saved: %s" % (sid, "; ".join(errors)))
        if olds:
            self.store.delete_buildings([f.id() for f in olds])
        cc = G.corrected_coordinate(geom, r["longitude"], r["latitude"], epsg, self._cfg("corrected_coordinate", {}))
        self.store.upsert_building(sid, geom, {
            "polygon_id": "%s-%s" % (self.batch["batch_id"], sid), "method": C.METHOD_MANUAL,
            "status": C.STATUS_APPROVED_MANUAL, "route": r.get("route"), "confidence": None,
            "reviewer": operator, "reviewed_at": now_iso(), "area_m2": round(area, 1),
            "config_version": self._cfg("config_version")})
        self.store.upsert_corrected(sid, cc)
        self.store.set_survey(sid, state=C.APPROVED_MANUAL)
        self.store.add_action(sid, operator, C.ACTION_DRAW_BOX, before=before, after=geom,
                              duration_s=self._finish_time(key))
        r["state"] = C.APPROVED_MANUAL
        self._refresh_layers()
        self.record_updated.emit(key)
        self.counts_changed.emit(self.counts())
        self.success("%s drawn as a box and approved (%.0f m2)" % (sid, area))
        if self.settings.get("auto_next"):
            self.next_open()
        return cc

    def start_draw(self, key=None):
        key = self._require_current(key)
        self.operator_id()
        self._draw_for = self.records[key]["survey_id"]
        self._edit_kind = None
        lyr = self.layers["buildings"]
        self._snapping()
        if self.iface is not None:
            self.iface.setActiveLayer(lyr)
        lyr.startEditing()
        # Remove the record's current polygon inside the edit session: with "avoid
        # overlap" on, the new drawing would otherwise be clipped against the very
        # proposal it replaces. Discarding the edits brings the old polygon back.
        self._draw_before = None
        old = self.store.building(self._draw_for)
        if old is not None:
            self._draw_before = QgsGeometry(old.geometry())
            lyr.deleteFeature(old.id())
        if self.iface is not None:
            self.iface.actionAddFeature().trigger()
        self.info("Draw the building for %s, then save (Ctrl+S) - it is approved as APPROVED_MANUAL" % self._draw_for)

    # -- edit signal handlers ---------------------------------------------------------------
    def _feature_added(self, fid):
        lyr = self.layers.get("buildings")
        if lyr is None or not self._draw_for or not lyr.isEditable():
            return
        f = lyr.getFeature(fid)
        if val(f["survey_id"]):
            return
        idx = lyr.fields().indexFromName
        lyr.changeAttributeValue(fid, idx("survey_id"), self._draw_for)
        lyr.changeAttributeValue(fid, idx("method"), C.METHOD_MANUAL)
        lyr.changeAttributeValue(fid, idx("status"), C.STATUS_EDITED)

    def _before_commit(self):
        lyr = self.layers["buildings"]
        eb = lyr.editBuffer()
        if eb is None:
            return
        changed = eb.changedGeometries()
        self._pending_geom = {}
        if changed:
            req = QgsFeatureRequest().setFilterFids(list(changed.keys()))
            for f in lyr.dataProvider().getFeatures(req):
                self._pending_geom[f.id()] = (val(f["survey_id"]), QgsGeometry(f.geometry()),
                                              QgsGeometry(changed[f.id()]))

    def _after_commit(self):
        pending, self._pending_geom = self._pending_geom, {}
        kind = self._edit_kind or C.ACTION_EDIT
        if pending:
            QTimer.singleShot(0, lambda: self._record_edits(pending, kind))

    def _record_edits(self, pending, kind=C.ACTION_EDIT):
        operator = self.settings.get("operator_id") or ""
        lyr = self.store.layer("buildings")
        done, self._split_done = self._split_done, set()
        for fid, (sid, before, after) in pending.items():
            key = self._key_for(sid) if sid else None
            if key is None or sid in done:
                continue                                   # split records are logged by _resolve_split
            if not lyr.getFeature(fid).isValid():
                continue                                   # feature was removed meanwhile
            epsg = self.epsg_for(key)
            area = G.to_utm(after, epsg).area() if after and not after.isEmpty() else None
            self.store.update_building(fid, {"status": C.STATUS_EDITED, "area_m2": round(area, 1) if area else None,
                                             "reviewer": None, "reviewed_at": None})
            self.store.delete_corrected(sid)
            self.store.add_action(sid, operator, kind, before=before, after=after)
            rec = self.records[key]
            if rec.get("state") in (C.APPROVED, C.APPROVED_MANUAL):
                rec["state"] = C.REVIEW
                self.store.set_survey(sid, state=C.REVIEW)
            self.record_updated.emit(key)
        self._refresh_layers()

    def _features_committed(self, layer_id, features):
        feats = [(f.id(), val(f["survey_id"])) for f in features]
        if self._draw_for:
            QTimer.singleShot(0, lambda: self._record_drawn(feats))
        else:
            # Not a Draw (M) session: the new features come from QGIS's split tool (or a paste).
            self._resolve_split(feats, dict(self._pending_geom))

    def _resolve_split(self, feats, before_map):
        """New features outside a Draw session come from QGIS's split tool (or a paste).

        The decision is deferred until the save has finished; _record_edits then
        skips these records so each is logged once, as a SPLIT.
        """
        operator = self.settings.get("operator_id") or ""
        for sid in sorted({s for _, s in feats if s}):
            key = self._key_for(sid)
            if key is None:
                continue
            before = next((b for _fid, (s, b, _a) in before_map.items() if s == sid), None)
            self._split_done.add(sid)
            QTimer.singleShot(0, lambda k=key, s=sid, b=before: self._finish_split(k, s, b, operator))

    def _finish_split(self, key, sid, before, operator):
        """Keep the part that contains (or is nearest to) the survey point; remove the others."""
        pr = self.store.layer("buildings").dataProvider()
        req = QgsFeatureRequest().setFilterExpression(self.store._where("survey_id", sid))
        parts = [f for f in pr.getFeatures(req) if f.hasGeometry() and not f.geometry().isEmpty()]
        if len(parts) < 2:
            return
        r = self.records[key]
        pt = QgsGeometry.fromPointXY(QgsPointXY(r["longitude"], r["latitude"]))
        inside = [f for f in parts if f.geometry().contains(pt)]
        keeper = inside[0] if inside else min(parts, key=lambda f: f.geometry().distance(pt))
        drop = [f.id() for f in parts if f.id() != keeper.id()]
        self.store.delete_buildings(drop)
        area = G.to_utm(keeper.geometry(), self.epsg_for(key)).area()
        self.store.update_building(keeper.id(), {"status": C.STATUS_EDITED, "area_m2": round(area, 1),
                                                 "reviewer": None, "reviewed_at": None})
        self.store.delete_corrected(sid)
        self.store.add_action(sid, operator, C.ACTION_SPLIT, before=before, after=keeper.geometry(),
                              note="kept the part with the survey point; %d part(s) removed" % len(drop))
        rec = self.records[key]
        if rec.get("state") in (C.APPROVED, C.APPROVED_MANUAL):
            rec["state"] = C.REVIEW
            self.store.set_survey(sid, state=C.REVIEW)
        self.record_updated.emit(key)
        self._refresh_layers()

    def _record_drawn(self, feats):
        operator = self.settings.get("operator_id") or ""
        for fid, sid in feats:
            sid = sid or self._draw_for
            key = self._key_for(sid) if sid else None
            if key is None:
                continue
            new = self.layers["buildings"].getFeature(fid)
            olds = [f.id() for f in self.store.features("buildings", "survey_id", sid) if f.id() != fid]
            if olds:
                self.store.delete_buildings(olds)      # one building feature per survey record
            epsg = self.epsg_for(key)
            geom, errors, area = G.validate_for_approval(new.geometry(), epsg, self._cfg("validation", {}))
            r = self.records[key]
            attrs = {"survey_id": sid, "polygon_id": "%s-%s" % (self.batch["batch_id"], sid),
                     "method": C.METHOD_MANUAL, "route": r.get("route"), "confidence": None,
                     "config_version": self._cfg("config_version")}
            if errors:
                attrs.update({"status": C.STATUS_EDITED})
                self.store.update_building(fid, attrs)
                self.store.add_action(sid, operator, C.ACTION_DRAW_MANUAL, before=self._draw_before, after=new.geometry(),
                                      note="not approved: " + "; ".join(errors))
                self.warn("Drawn polygon for %s is not valid: %s" % (sid, "; ".join(errors)))
            else:
                cc = G.corrected_coordinate(geom, r["longitude"], r["latitude"], epsg, self._cfg("corrected_coordinate", {}))
                attrs.update({"status": C.STATUS_APPROVED_MANUAL, "reviewer": operator, "reviewed_at": now_iso(),
                              "area_m2": round(area, 1)})
                self.store.update_building(fid, attrs, geom=geom)
                self.store.upsert_corrected(sid, cc)
                self.store.set_survey(sid, state=C.APPROVED_MANUAL)
                self.store.add_action(sid, operator, C.ACTION_DRAW_MANUAL, before=self._draw_before, after=geom,
                                      duration_s=self._finish_time(key))
                r["state"] = C.APPROVED_MANUAL
                self.success("%s drawn and approved (APPROVED_MANUAL, %.0f m2)" % (sid, area))
            self.record_updated.emit(key)
        self._draw_for = None
        self._draw_before = None
        self.counts_changed.emit(self.counts())
        self._refresh_layers()

    # -- worker-assisted actions -------------------------------------------------------------
    def click_to_fix(self, positive, negative, box=None, key=None):
        key = self._require_current(key)
        self.operator_id()
        d = self.details.get(key) or {}
        if not d.get("crop_path"):
            raise UserError("No licensed imagery crop exists for this record; use Edit (E) or Draw (M)")
        if not self.worker_batch_id:
            raise UserError("The worker has not processed this batch")
        if self._fix_inflight:
            self._fix_queued = (positive, negative, box, key)
            return
        self._fix_inflight = True
        sid = self.records[key]["survey_id"]
        self._commit_buildings()

        def done(res):
            self._fix_inflight = False
            self.apply_detail(res, force=True, action=C.ACTION_CLICK_TO_FIX)
            self._refresh_layers()
            if self._fix_queued:
                q, self._fix_queued = self._fix_queued, None
                self.click_to_fix(*q)

        def fail(exc):
            self._fix_inflight = False
            self._fix_queued = None
            self.error(self._worker_error_text("Click-to-fix", exc))

        self.runner.run("BPA click-to-fix", lambda: self.client.resegment(sid, self.worker_batch_id, positive,
                                                                          negative, box), done, fail)

    def candidate_at(self, lon, lat, key=None):
        key = key or self.current
        d = self.details.get(key) or {}
        pt = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
        hits = []
        for c in d.get("candidates", []):
            g = G.geojson_to_geometry(c.get("geometry"))
            if g is not None and g.contains(pt):
                hits.append((g.area(), c["candidate_id"]))
        if not d and key in self.records and self.store is not None:
            # Batch reopened: the worker's details are gone, but review.gpkg keeps the candidates.
            for f in self.store.features("candidates", "survey_id", self.records[key]["survey_id"]):
                g = f.geometry()
                if g is not None and not g.isEmpty() and g.contains(pt):
                    hits.append((g.area(), val(f["candidate_id"])))
        return min(hits)[1] if hits else None

    def pick_candidate(self, candidate_id, key=None):
        key = self._require_current(key)
        self.operator_id()
        if not self.worker_batch_id:
            raise UserError("The worker has not processed this batch")
        sid = self.records[key]["survey_id"]
        self._commit_buildings()
        self.runner.run("BPA pick candidate",
                        lambda: self.client.select_candidate(sid, self.worker_batch_id, candidate_id),
                        lambda res: (self.apply_detail(res, force=True, action=C.ACTION_PICK_CANDIDATE),
                                     self._refresh_layers(), self.info("%s re-processed from %s" % (sid, candidate_id))),
                        lambda e: self.error(self._worker_error_text("Pick candidate", e)))

    @staticmethod
    def _worker_error_text(what, exc):
        """Plain-language message for a failed worker call."""
        if isinstance(exc, WorkerError) and exc.status == 404:
            return ("%s needs the worker to know this batch, but the worker was restarted. Click Process once "
                    "to reload the batch - approved, edited and rejected records are kept." % what)
        if isinstance(exc, WorkerError) and exc.status is None:
            return "%s: the worker is not running. Click Process to start it." % what
        return "%s failed: %s" % (what, exc)

    def _refresh_layers(self):
        for name in ("buildings", "survey_points", "corrected_points", "candidates"):
            lyr = self.layers.get(name)
            if lyr is not None:
                lyr.triggerRepaint()
        if self.iface is not None:
            self.iface.mapCanvas().refresh()

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------
    def export(self, output_path=None, delivery_formats=("GPKG",)):
        """Write a copy of the workbook with approved results (+ optional delivery layers)."""
        if not self.batch:
            raise UserError("Open a batch first")
        self._commit_buildings()
        operator = self.settings.get("operator_id") or ""
        results = {}
        for b in self.store.all_buildings(C.EXPORTABLE_STATUSES):
            sid = val(b["survey_id"])
            cp = self.store.corrected(sid)
            if cp is None or b.geometry().isEmpty():
                continue
            results[sid] = {
                "status": val(b["status"]), "corrected_lat": num(cp["corrected_lat"]),
                "corrected_lon": num(cp["corrected_lon"]), "polygon_id": val(b["polygon_id"]),
                "area_m2": num(b["area_m2"]), "method": val(b["method"]), "confidence": num(b["confidence"]),
                "route": val(b["route"]), "reviewer": val(b["reviewer"]), "reviewed_at": val(b["reviewed_at"]),
                "strategy": val(cp["strategy"]), "shift_m": num(cp["shift_m"]),
                "dist_to_polygon_m": num(cp["dist_to_polygon_m"]), "footprint_source": val(b["footprint_source"]),
                "imagery_source": val(b["imagery_source"]), "config_version": val(b["config_version"]),
                "model_version": val(b["model_version"]), "manifest_hash": val(b["manifest_hash"])}
        src = self.batch.get("input_copy") or self.store.meta().get("input_copy")
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(self.batch["batch_dir"], "output")
        stem = os.path.splitext(os.path.basename(src))[0]
        output_path = output_path or os.path.join(out_dir, "%s_output_%s.xlsx" % (stem, stamp))
        rep = export_workbook(src, output_path, results, self.cfg)
        rep["delivery"] = []
        if results and delivery_formats:
            ids = ",".join("'%s'" % s.replace("'", "''") for s in results)
            mem = self.layers["buildings"].materialize(QgsFeatureRequest().setFilterExpression(
                "\"survey_id\" IN (%s) AND \"status\" IN ('APPROVED','APPROVED_MANUAL')" % ids))
            ext = {"GPKG": "gpkg", "ESRI Shapefile": "shp", "KML": "kml"}
            for fmt in delivery_formats:
                path = os.path.join(out_dir, "%s_buildings_%s.%s" % (stem, stamp, ext.get(fmt, "dat")))
                opts = QgsVectorFileWriter.SaveVectorOptions()
                opts.driverName = fmt
                opts.layerName = "approved_buildings"
                res = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsProject.instance().transformContext(), opts)
                if res[0] == QgsVectorFileWriter.WriterError.NoError:
                    rep["delivery"].append(path)
                else:
                    self.warn("Delivery layer %s failed: %s" % (fmt, res[1]))
        self.store.add_action("", operator, C.ACTION_EXPORT, note="%d records -> %s" % (rep["exported"], output_path))
        self.store.set_meta(last_export=output_path, last_export_at=now_iso())
        self.success("Exported %d approved record(s) to %s" % (rep["exported"], output_path))
        if self.settings.get("learn_from_approved") and rep["exported"]:
            self.learn_from_approved()
        return rep

    def learn_from_approved(self):
        """Teach the worker this batch's approved buildings (they become trusted reference)."""
        if self.client is None or not self.worker_ready:
            self.info("Approved buildings will be learned once the worker is running (click Process, then Export).")
            return
        path = self.store.path
        operator = self.settings.get("operator_id") or ""

        def done(res):
            self.store.add_action("", operator, C.ACTION_LEARN,
                                  note="%d added, %d updated, %d trusted buildings in total"
                                  % (res.get("added", 0), res.get("updated", 0), res.get("total", 0)))
            self.success("Learned %d approved building(s); future surveys of them are matched directly"
                         % (res.get("added", 0) + res.get("updated", 0)))

        self.runner.run("BPA learn", lambda: self.client.learn(path), done,
                        lambda e: self.warn(self._worker_error_text("Learning from approvals", e)))

    # ------------------------------------------------------------------
    # client imagery
    # ------------------------------------------------------------------
    def survey_points(self):
        """(lon, lat) of every valid record of the open batch."""
        return [(r["longitude"], r["latitude"]) for r in self.records.values()
                if r.get("latitude") is not None and r.get("longitude") is not None]

    def add_imagery(self, spec, view_paths=()):
        """Register client imagery for this project (new batches) and the open batch; show it on the map.

        Only a spec with ``allow_automated_analysis`` and a permission record (``licence_ref``)
        opens the worker's licence gate; without them the files are only shown for viewing.
        """
        op = self.operator_id()
        layers = self.show_imagery(list(spec.get("paths") or []) + list(view_paths), spec.get("id") or "imagery")
        if not spec.get("paths"):
            return {"registered": False, "layers": layers}
        if not (spec.get("allow_automated_analysis") and str(spec.get("licence_ref") or "").strip()):
            raise UserError("AI tracing needs the client's permission and a record of it (who, when)")
        spec = dict(spec, added_by=op, added_at=now_iso())
        save_imagery_source(load_config(self.settings.config_path()), spec)
        if self.batch:
            add_imagery_to_batch(self.batch["config_path"], spec)
            self.cfg = load_frozen_config(self.batch["config_path"])
            self.store.add_action("", op, C.ACTION_ADD_IMAGERY,
                                  note="%s: %d file(s), %s per pixel, permission: %s" % (
                                      spec["id"], len(spec["paths"]), spec.get("gsd_m"), spec["licence_ref"]))
        self.success("Client imagery '%s' added: open records are traced on it when you click Process" % spec["id"])
        return {"registered": True, "layers": layers, "spec": spec}

    def show_imagery(self, paths, title):
        """Add imagery files to the map (bottom of the layer list, so outlines stay on top)."""
        proj = QgsProject.instance()
        root = proj.layerTreeRoot()
        group = root.findGroup("Client imagery") or root.addGroup("Client imagery")
        have = {os.path.normcase(l.source()) for l in proj.mapLayers().values()}
        added = []
        for p in paths:
            if os.path.normcase(p) in have:
                continue
            lyr = QgsRasterLayer(p, "%s - %s" % (title, os.path.basename(p)))
            if lyr.isValid():
                proj.addMapLayer(lyr, False)
                group.addLayer(lyr)
                added.append(lyr.id())
        if group.parent() is root and root.children()[-1] is not group:
            clone = group.clone()
            root.addChildNode(clone)
            root.removeChildNode(group)
        return added

    # ------------------------------------------------------------------
    # evidence panel
    # ------------------------------------------------------------------
    def evidence_html(self, key):
        r = self.records.get(key)
        if r is None:
            return ""
        d = self.details.get(key) or {}
        p = d.get("proposal") or {}
        f = r.get("fields", {})
        esc = _esc
        out = ["<h3 style='margin:2px 0'>%s &ndash; %s</h3>" % (esc(r["survey_id"]), esc(f.get("building_name", "")))]
        out.append("<div>%s</div>" % esc(", ".join(x for x in [f.get("street"), f.get("area"), f.get("city"),
                                                               f.get("pincode")] if x)))
        facts = [esc(f.get("building_type")), esc(f.get("building_category")),
                 "%s floors" % esc(f["floors"]) if f.get("floors") else "",
                 "%s m" % esc(f["building_height_m"]) if f.get("building_height_m") else "",
                 "customer: %s" % esc(f["customer_name"]) if f.get("customer_name") else ""]
        facts = [x for x in facts if x]
        if facts:
            out.append("<div style='color:#555'>%s</div>" % " &middot; ".join(facts))
        out.append(self._explanation_html(key))
        out.append("<h4 style='margin:10px 0 2px 0;color:#555'>Details</h4><div style='font-size:92%'>")
        out.append("<p><b>State:</b> %s" % esc(r.get("state")))
        if r.get("reasons"):
            out.append("<br><b style='color:#c00'>Invalid:</b> %s" % esc("; ".join(r["reasons"])))
        if r.get("warnings"):
            out.append("<br><b style='color:#b06000'>Warnings:</b> %s" % esc("; ".join(r["warnings"])))
        if d.get("error_message"):
            out.append("<br><b style='color:#c00'>Error %s:</b> %s" % (esc(d.get("error_code")), esc(d["error_message"])))
        out.append("</p>")
        if d:
            out.append("<p><b>Identity:</b> %s &ndash; %s (probability %s)</p>" % (
                esc((d.get("decision") or "").replace("IDENTITY_", "").replace("_", " ").lower()),
                esc(d.get("decision_reason")), _fmt(d.get("identity_prob"))))
        if p:
            out.append("<p><b>Proposal:</b> %s &middot; confidence <b>%s</b> &middot; route %s (would: %s)<br>%s</p>" % (
                esc(p.get("method")), _fmt(p.get("confidence")), esc(p.get("route")), esc(p.get("would_route")),
                esc(p.get("route_reason"))))
            rows = []
            for k, label in (("sam_iou", "SAM predicted IoU"), ("stability", "Prompt stability"),
                             ("footprint_agreement", "Footprint agreement (IoU)"), ("area_m2", "Area m²"),
                             ("shift_m", "Corrected point moves (m)")):
                if p.get(k) is not None:
                    rows.append("<tr><td>%s</td><td><b>%s</b></td></tr>" % (label, _fmt(p.get(k))))
            if rows:
                out.append("<table cellspacing=0 cellpadding=2>%s</table>" % "".join(rows))
            texts = p.get("flag_text") or []
            notes = [C.FLAG_TEXT.get(n, n) for n in p.get("notes", [])]
            if texts or notes:
                out.append("<p><b>Check:</b><ul style='margin:2px 0 2px -20px'>%s</ul></p>" %
                           "".join("<li>%s</li>" % esc(t) for t in texts + notes))
        img = d.get("imagery") or {}
        if img.get("status") == "OK":
            out.append("<p><b>Imagery:</b> %s, licence %s, captured %s, %.2f m/px</p>" % (
                esc(img.get("provider")), esc(img.get("licence_ref")), esc(img.get("capture_date") or "?"),
                float(img.get("gsd_m") or 0)))
        elif img:
            out.append("<p><b>Imagery:</b> %s</p>" % esc(img.get("status", "").replace("_", " ").lower()))
        cands = d.get("candidates") or []
        if cands:
            out.append("<p><b>Candidates</b> (%d within %s m):</p><table cellspacing=0 cellpadding=2 border=0>"
                       "<tr style='background:#eee'><th>#</th><th>score</th><th>dist</th><th>d</th><th>n</th>"
                       "<th>a</th><th>r</th><th>p</th><th>s</th><th>evidence</th><th></th></tr>" % (len(cands), _fmt(d.get("radius_m"))))
            shown = cands[:8] + [c for c in cands[8:] if (c.get("evidence") or {}).get("found_by_name")]
            for c in shown:
                fe, ev = c.get("features", {}), c.get("evidence", {})
                bits = []
                if ev.get("matched_name"):
                    bits.append("name: %s" % ev["matched_name"])
                if ev.get("found_by_name"):
                    bits.append("found by name: %s" % ev["found_by_name"])
                if ev.get("on_named_street"):
                    bits.append("on %s" % ev["on_named_street"])
                if ev.get("crosses_road"):
                    bits.append("across %s" % ev["crosses_road"])
                if ev.get("height_consistent") is False:
                    bits.append("height mismatch")
                bits.append(c.get("dataset") or "")
                style = " style='background:#fff6c2'" if c.get("chosen") else ""
                use = "" if c.get("chosen") else "<a href='pick:%s'>use</a>" % esc(c["candidate_id"])
                out.append("<tr%s><td>%d</td><td><b>%.2f</b></td><td>%s</td><td>%.2f</td><td>%.2f</td><td>%.2f</td>"
                           "<td>%.2f</td><td>%.2f</td><td>%.2f</td><td>%s</td><td>%s</td></tr>" % (
                               style, c["rank"], c["score"], _fmt(ev.get("distance_m")), fe.get("s_dist", 0),
                               fe.get("s_name", 0), fe.get("s_addr", 0), fe.get("s_road", 0), fe.get("s_plaus", 0),
                               fe.get("s_source", 0), esc("; ".join(b for b in bits if b)), use))
            out.append("</table><div style='color:#777;font-size:90%'>d distance &middot; n name or customer outlet "
                       "&middot; a address &middot; r road side &middot; p plausibility (size, height) &middot; "
                       "s source</div>")
        out.append("</div>")
        return "".join(out)

    LANE_INK = {X.LANE_SURE: "#2e8b4a", X.LANE_CHECK: "#c98a00", X.LANE_DRAW: "#c93a3a", X.LANE_DONE: "#3f6fb5",
                X.LANE_REJECTED: "#777777", X.LANE_INVALID: "#c2185b", X.LANE_FAILED: "#8e44ad",
                X.LANE_WAITING: "#999999"}

    def _explanation_html(self, key):
        """The plain-language card at the top of the evidence panel."""
        e = self.explanation(key)
        if e["lane"] in (X.LANE_DONE, X.LANE_REJECTED) and self.store is not None:
            b = self.store.building(self.records[key]["survey_id"])
            reviewer = val(b["reviewer"]) if b is not None else None
            if reviewer:
                e = dict(e, headline=e["headline"].rstrip(".") + " by %s." % reviewer)
        ink = self.LANE_INK.get(e["lane"], "#999999")
        lines = ["<div style='font-size:112%%'><b style='color:%s'>%s</b> &nbsp; %s</div>" % (
            ink, _esc(e["label"].upper()), _esc(e["headline"]))]
        lines.append("<div style='margin:4px 0'><b>What to do:</b> %s</div>" % _esc(e["todo"]))
        for w in e["why"]:
            lines.append("<div style='color:#1f6f35'>&#10004; %s</div>" % _esc(w))
        for w in e["doubts"]:
            lines.append("<div style='color:#8a4b00'>&#9888; %s</div>" % _esc(w))
        return ("<table width='100%%' cellspacing='0' cellpadding='6' style='margin-top:6px'><tr>"
                "<td width='6' bgcolor='%s'></td><td bgcolor='#f6f6f6'>%s</td></tr></table>" % (ink, "".join(lines)))


def _esc(s):
    s = "" if s is None else str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return "%.2f" % v
    return str(v)
