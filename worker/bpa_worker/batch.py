"""Batches: record bookkeeping, background processing and work.gpkg lifecycle."""

import collections
import datetime as dt
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from bpa_common import constants as C
from bpa_common.config import get, load_frozen_config

from .imagery import ImageryManager
from .pipeline import Pipeline, RecordState
from .store import WorkStore, now_iso

log = logging.getLogger(__name__)


def load_calibration(cfg):
    """Merge the identity and confidence calibration files named in config."""
    from bpa_common.config import resolve_path
    out = {}
    for key in ("scoring.calibration_file", "confidence.calibration_file"):
        path = get(cfg, key)
        if not path:
            continue
        p = resolve_path(cfg, path)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for k, v in data.items():
                out.setdefault(k, v)
        else:
            log.warning("Calibration file not found: %s", p)
    return out


class Batch(object):
    def __init__(self, batch_id, batch_dir, cfg, records, pipeline):
        self.batch_id = batch_id
        self.batch_dir = os.path.abspath(batch_dir)
        self.cfg = cfg
        self.pipeline = pipeline
        self.cache_dir = os.path.join(self.batch_dir, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(os.path.join(self.batch_dir, "output"), exist_ok=True)
        self.records = collections.OrderedDict()
        counts = collections.Counter(r["survey_id"] for r in records)
        for r in records:
            key = r["survey_id"]
            if not key or counts[key] > 1:
                key = "%s@row%s" % (r["survey_id"] or "EMPTY", r.get("row"))
            self.records[key] = RecordState(r)
        self.store = WorkStore(os.path.join(self.batch_dir, "work.gpkg"))
        self.status = "created"
        self.created_at = now_iso()
        self.started_at = None
        self.finished_at = None
        self.processed = 0
        self.lock = threading.RLock()
        v = pipeline.versions()
        self.store.set_info(batch_id=batch_id, created_at=self.created_at, worker_version=_worker_version(),
                            config_version=v["config_version"], config_hash=v["config_hash"],
                            manifest_hash=v["manifest_hash"] or "", overture_release=v["overture_release"] or "",
                            model_version=v["model_version"] or "")
        for rec in self.records.values():
            if rec.state == C.IMPORTED:
                rec.transition(C.QUEUED)
            self.store.write_record(rec, v["config_version"], v["config_hash"])

    def ctx(self):
        return {"store": self.store, "batch_id": self.batch_id, "cache_dir": self.cache_dir}

    def counts(self):
        states = collections.Counter(r.state for r in self.records.values())
        routes = collections.Counter((r.proposal or {}).get("route") for r in self.records.values()
                                     if r.proposal and r.state not in (C.QUEUED, C.MATCHING))
        would = collections.Counter((r.proposal or {}).get("would_route") for r in self.records.values()
                                    if r.proposal)
        methods = collections.Counter((r.proposal or {}).get("method") for r in self.records.values()
                                      if r.proposal and r.proposal.get("geometry") is not None)
        return {"states": dict(states), "routes": dict(routes), "would_route": dict(would),
                "methods": dict(methods)}

    def summary(self, with_records=True):
        out = {"batch_id": self.batch_id, "batch_dir": self.batch_dir, "status": self.status,
               "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
               "total": len(self.records), "processed": self.processed, "counts": self.counts(),
               "versions": self.pipeline.versions()}
        if with_records:
            out["records"] = [r.summary() for r in self.records.values()]
        return out

    def find(self, survey_id):
        return self.records.get(survey_id)


def _worker_version():
    from . import __version__
    return __version__


class BatchManager(object):
    def __init__(self, cfg, reference, segmenter):
        self.cfg = cfg
        self.reference = reference
        self.segmenter = segmenter
        self.batches = collections.OrderedDict()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bpa-batch")
        self.op_lock = threading.RLock()   # serialises model/pipeline work

    def _pipeline_for(self, cfg):
        imagery = ImageryManager(cfg)
        if imagery.any_enabled() and self.segmenter is not None and not self.segmenter.loaded:
            self.segmenter.load()
        return Pipeline(cfg, self.reference, imagery, self.segmenter, load_calibration(cfg))

    def create(self, batch_dir, records, batch_id=None, config_path=None, start=True):
        cfg = self.cfg
        if config_path:
            cfg = load_frozen_config(config_path)
        batch_id = batch_id or "b%s" % dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
        with self.op_lock:
            old = self.batches.pop(batch_id, None)
            if old is not None:
                old.store.close()
            batch = Batch(batch_id, batch_dir, cfg, records, self._pipeline_for(cfg))
            self.batches[batch_id] = batch
        if start:
            self.executor.submit(self._run, batch)
        return batch

    def set_reference(self, reference):
        """Swap in freshly loaded reference data (after an incremental S0 run)."""
        self.reference = reference
        for b in self.batches.values():
            b.pipeline.ref = reference
            b.pipeline.gen.ref = reference

    def run_sync(self, batch):
        self._run(batch)
        return batch

    def _run(self, batch):
        batch.status = "running"
        batch.started_at = now_iso()
        ctx = batch.ctx()
        try:
            for rec in batch.records.values():
                if rec.state != C.QUEUED:
                    if rec.state == C.INVALID:
                        batch.processed += 1
                    continue
                with self.op_lock:
                    batch.pipeline.process(rec, ctx)
                batch.processed += 1
            batch.status = "done"
        except Exception:
            log.exception("Batch %s crashed", batch.batch_id)
            batch.status = "error"
        batch.finished_at = now_iso()
        log.info("Batch %s %s: %s", batch.batch_id, batch.status, batch.counts())

    def retry_failed(self, batch):
        n = 0
        for rec in batch.records.values():
            if rec.state == C.FAILED:
                rec.transition(C.QUEUED)
                n += 1
        if n:
            batch.status = "queued"
            self.executor.submit(self._run, batch)
        return n

    def get(self, batch_id):
        return self.batches.get(batch_id)

    def latest(self):
        return next(reversed(self.batches.values())) if self.batches else None

    def find_record(self, survey_id, batch_id=None):
        if batch_id:
            b = self.get(batch_id)
            return (b, b.find(survey_id)) if b else (None, None)
        for b in reversed(list(self.batches.values())):
            rec = b.find(survey_id)
            if rec is not None:
                return b, rec
        return None, None

    def shutdown(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        for b in self.batches.values():
            try:
                b.store.close()
            except Exception:
                pass
