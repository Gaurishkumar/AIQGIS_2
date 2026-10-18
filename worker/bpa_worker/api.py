"""FastAPI service used by the QGIS plugin (localhost only).

Endpoints (Final MVP Specification, Section 3):
  POST /batches                              submit imported records + config version
  GET  /batches/{id}                         counts per state and route
  GET  /records/{survey_id}                  candidates, proposal, confidence breakdown, flags
  POST /records/{survey_id}/resegment        click-to-fix prompts -> new polygon
  POST /records/{survey_id}/select-candidate operator picks another candidate
  GET  /health                               model, data versions, imagery sources
Extras: GET /batches, GET /batches/{id}/records (bulk), POST /batches/{id}/retry,
POST /batches/from-excel (import fallback), POST /shutdown.

When the BPA_TOKEN environment variable is set, every endpoint except
/health requires the header ``X-BPA-Token`` with that value.
"""

import datetime as dt
import logging
import math
import os
import threading
from typing import Dict, List, Optional

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bpa_common import constants as C
from bpa_common.config import get

from . import __version__
from .batch import BatchManager
from .imagery import ImageryManager
from .pipeline import StateError
from .reference import ReferenceData
from .segmentation import SamSegmenter

log = logging.getLogger(__name__)


def jsonable(o):
    """Recursively convert numpy / tuples / non-finite floats for JSON."""
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, np.ndarray):
        return jsonable(o.tolist())
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    return o


def ok(data, status=200):
    return JSONResponse(jsonable(data), status_code=status)


def _detail(batch, rec):
    """Record detail plus batch id and the versions every automated result must carry."""
    d = rec.detail()
    d["batch_id"] = batch.batch_id
    d["versions"] = batch.pipeline.versions()
    return d


class RecordIn(BaseModel):
    survey_id: str
    row: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    fields: Dict[str, str] = Field(default_factory=dict)
    source: Dict[str, str] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    state: Optional[str] = None


class BatchIn(BaseModel):
    batch_id: Optional[str] = None
    batch_dir: str
    config_path: Optional[str] = None
    config_version: Optional[str] = None
    records: List[RecordIn]


class ExcelBatchIn(BaseModel):
    workbook_path: str
    batch_dir: str
    batch_id: Optional[str] = None
    config_path: Optional[str] = None


class ResegmentIn(BaseModel):
    positive_points: List[List[float]] = Field(default_factory=list)   # [[lon, lat], ...]
    negative_points: List[List[float]] = Field(default_factory=list)
    box: Optional[List[float]] = None                                  # [W, S, E, N]


class SelectIn(BaseModel):
    candidate_id: str


class LearnIn(BaseModel):
    review_gpkg: str


class PointsIn(BaseModel):
    points: List[List[float]]          # [[lon, lat], ...]
    refresh: bool = False


def create_app(cfg, load_model=True, reference=None):
    app = FastAPI(title="BPA worker", version=__version__)
    token = os.environ.get("BPA_TOKEN") or None
    state = {"ready": False, "error": None, "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    ref = reference if reference is not None else ReferenceData(cfg)
    segmenter = SamSegmenter(cfg) if load_model else None
    manager = BatchManager(cfg, ref, segmenter)
    imagery_status = ImageryManager(cfg)
    app.state.manager = manager
    app.state.reference = ref
    app.state.segmenter = segmenter

    def _load_model():
        try:
            if segmenter is not None and imagery_status.any_enabled():
                segmenter.load()
        except Exception as exc:  # reported through /health
            state["error"] = str(exc)
        state["ready"] = True

    threading.Thread(target=_load_model, name="bpa-model-load", daemon=True).start()

    def auth(x_bpa_token: Optional[str] = Header(default=None)):
        if token and x_bpa_token != token:
            raise HTTPException(status_code=401, detail="Missing or invalid X-BPA-Token")

    def record_or_404(survey_id, batch_id):
        batch, rec = manager.find_record(survey_id, batch_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="Record %s not found" % survey_id)
        return batch, rec

    @app.get("/health")
    def health():
        seg = segmenter.status() if segmenter else {"loaded": False, "backend": "disabled"}
        if segmenter is not None and not segmenter.loaded and not imagery_status.any_enabled():
            seg["note"] = "Not loaded: no licence-approved imagery source is enabled"
        return ok({
            "status": "ok", "ready": state["ready"], "worker_version": __version__,
            "config_version": get(cfg, "config_version"), "model": seg,
            "reference": manager.reference.status(), "imagery_sources": imagery_status.status(),
            "shadow_mode": bool(get(cfg, "routing.shadow_mode", True)),
            "auth_required": bool(token), "started_at": state["started_at"], "error": state["error"],
        })

    @app.post("/batches", dependencies=[Depends(auth)])
    def create_batch(body: BatchIn):
        if not body.records:
            raise HTTPException(status_code=400, detail="No records submitted")
        records = [r.model_dump() for r in body.records]
        for r in records:
            if r.get("state") == C.INVALID or r.get("reasons"):
                r["state"] = C.INVALID
            elif r.get("latitude") is None or r.get("longitude") is None:
                r["state"] = C.INVALID
                r["reasons"] = list(r.get("reasons") or []) + ["Missing coordinates"]
            else:
                r["state"] = C.IMPORTED
        try:
            batch = manager.create(body.batch_dir, records, batch_id=body.batch_id, config_path=body.config_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Could not create batch: %s" % exc)
        if body.config_version and body.config_version != str(get(batch.cfg, "config_version")):
            log.warning("Plugin config_version %s differs from batch config %s", body.config_version,
                        get(batch.cfg, "config_version"))
        return ok({"batch_id": batch.batch_id, "accepted": sum(1 for r in batch.records.values() if r.state != C.INVALID),
                   "invalid": sum(1 for r in batch.records.values() if r.state == C.INVALID),
                   "status": batch.status, "versions": batch.pipeline.versions()}, 201)

    @app.post("/batches/from-excel", dependencies=[Depends(auth)])
    def create_from_excel(body: ExcelBatchIn):
        from bpa_common.excel_io import ExcelSchemaError, copy_input, read_records
        try:
            records, report = read_records(body.workbook_path, cfg)
        except ExcelSchemaError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        copy_input(body.workbook_path, body.batch_dir)
        batch = manager.create(body.batch_dir, records, batch_id=body.batch_id, config_path=body.config_path)
        return ok({"batch_id": batch.batch_id, "import_report": report, "records": records}, 201)

    @app.get("/batches", dependencies=[Depends(auth)])
    def list_batches():
        return ok([b.summary(with_records=False) for b in manager.batches.values()])

    @app.get("/batches/{batch_id}", dependencies=[Depends(auth)])
    def get_batch(batch_id: str):
        b = manager.get(batch_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Batch %s not found" % batch_id)
        return ok(b.summary())

    @app.get("/batches/{batch_id}/records", dependencies=[Depends(auth)])
    def get_batch_records(batch_id: str, only_final: bool = False):
        b = manager.get(batch_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Batch %s not found" % batch_id)
        recs = [r for r in b.records.values() if not only_final or r.state in C.TERMINAL_WORKER_STATES]
        versions = b.pipeline.versions()
        out = []
        for r in recs:
            d = r.detail()
            d["versions"] = versions
            out.append(d)
        return ok({"batch_id": batch_id, "status": b.status, "records": out})

    @app.post("/batches/{batch_id}/retry", dependencies=[Depends(auth)])
    def retry(batch_id: str):
        b = manager.get(batch_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Batch %s not found" % batch_id)
        return ok({"requeued": manager.retry_failed(b)})

    @app.get("/records/{survey_id}", dependencies=[Depends(auth)])
    def get_record(survey_id: str, batch_id: Optional[str] = Query(default=None)):
        batch, rec = record_or_404(survey_id, batch_id)
        return ok(_detail(batch, rec))

    @app.post("/records/{survey_id}/resegment", dependencies=[Depends(auth)])
    def resegment(survey_id: str, body: ResegmentIn, batch_id: Optional[str] = Query(default=None)):
        batch, rec = record_or_404(survey_id, batch_id)
        try:
            with manager.op_lock:
                batch.pipeline.resegment(rec, batch.ctx(), body.positive_points, body.negative_points, body.box)
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (ValueError, StateError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return ok(_detail(batch, rec))

    @app.post("/records/{survey_id}/select-candidate", dependencies=[Depends(auth)])
    def select_candidate(survey_id: str, body: SelectIn, batch_id: Optional[str] = Query(default=None)):
        batch, rec = record_or_404(survey_id, batch_id)
        try:
            with manager.op_lock:
                batch.pipeline.select_candidate(rec, batch.ctx(), body.candidate_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except StateError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return ok(_detail(batch, rec))

    prep_job = {"status": "idle", "started_at": None, "finished_at": None, "error": None, "result": None}

    @app.post("/reference/coverage", dependencies=[Depends(auth)])
    def reference_coverage(body: PointsIn):
        return ok(manager.reference.coverage([(p[0], p[1]) for p in body.points]))

    @app.post("/reference/prepare", dependencies=[Depends(auth)])
    def reference_prepare(body: PointsIn):
        """S0 for the given survey points (incremental; needs internet once). Runs in the background."""
        if prep_job["status"] == "running":
            raise HTTPException(status_code=409, detail="Reference preparation is already running")

        def _run():
            from .prepare import prepare_reference
            prep_job.update({"status": "running", "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                             "finished_at": None, "error": None, "result": None})
            try:
                m = prepare_reference(cfg, [(p[0], p[1]) for p in body.points], manager.reference.ref_dir,
                                      refresh=body.refresh)
                with manager.op_lock:
                    manager.set_reference(ReferenceData(cfg, ref_dir=manager.reference.ref_dir))
                app.state.reference = manager.reference
                prep_job["result"] = {"overture_release": m.get("overture_release"),
                                      "areas": len(m.get("boxes") or []),
                                      "counts": manager.reference.counts()}
                prep_job["status"] = "done"
            except Exception as exc:
                log.exception("Reference preparation failed")
                prep_job.update({"status": "error", "error": str(exc)})
            prep_job["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

        threading.Thread(target=_run, name="bpa-reference-prepare", daemon=True).start()
        return ok({"status": "started"}, 202)

    @app.get("/reference/prepare", dependencies=[Depends(auth)])
    def reference_prepare_status():
        return ok(prep_job)

    @app.post("/reference/learn", dependencies=[Depends(auth)])
    def reference_learn(body: LearnIn):
        """Add a batch's approved polygons to the trusted reference and reload it."""
        from .learn import learn_from_review
        try:
            with manager.op_lock:
                res = learn_from_review(cfg, body.review_gpkg, manager.reference.ref_dir)
                manager.set_reference(ReferenceData(cfg, ref_dir=manager.reference.ref_dir))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        app.state.reference = manager.reference
        res["reference"] = manager.reference.counts()
        return ok(res)

    @app.post("/reference/reload", dependencies=[Depends(auth)])
    def reference_reload():
        with manager.op_lock:
            manager.set_reference(ReferenceData(cfg, ref_dir=manager.reference.ref_dir))
        app.state.reference = manager.reference
        return ok(manager.reference.status())

    @app.post("/shutdown", dependencies=[Depends(auth)])
    def shutdown():
        def _exit():
            import time
            time.sleep(0.3)
            manager.shutdown()
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
        return ok({"status": "shutting down"})

    return app
