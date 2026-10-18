"""work.gpkg - the worker-owned GeoPackage (single writer).

Tables (Final MVP Specification, Section 8): survey_record, candidate,
imagery_request, segmentation_result, proposal, processing_job, plus
batch_info with config/model/manifest versions for reproducibility.
"""

import datetime as dt
import json
import os

from .gpkg import GeoPackage


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _j(v):
    return json.dumps(v, default=str, ensure_ascii=False)


class WorkStore(object):
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.gp = GeoPackage(path)
        g = self.gp
        g.create_table("batch_info", [("key", "TEXT"), ("value", "TEXT")], key="key")
        g.create_table("survey_record", [
            ("survey_id", "TEXT"), ("row", "INTEGER"), ("latitude", "REAL"), ("longitude", "REAL"),
            ("fields_json", "TEXT"), ("source_json", "TEXT"), ("utm_epsg", "INTEGER"), ("state", "TEXT"),
            ("error_code", "TEXT"), ("error_message", "TEXT"), ("warnings", "TEXT"),
            ("config_version", "TEXT"), ("config_hash", "TEXT"), ("updated_at", "TEXT")],
            geometry_type="POINT", key="survey_id", description="Survey records and workflow state")
        g.create_table("candidate", [
            ("cand_key", "TEXT"), ("candidate_id", "TEXT"), ("survey_id", "TEXT"), ("source", "TEXT"),
            ("source_id", "TEXT"), ("dataset", "TEXT"), ("s_dist", "REAL"), ("s_name", "REAL"),
            ("s_addr", "REAL"), ("s_road", "REAL"), ("s_plaus", "REAL"), ("s_source", "REAL"),
            ("score", "REAL"), ("probability", "REAL"), ("rank", "INTEGER"), ("evidence_json", "TEXT")],
            geometry_type="GEOMETRY", key="cand_key", description="All scored candidates")
        g.create_table("imagery_request", [
            ("request_id", "TEXT"), ("survey_id", "TEXT"), ("provider", "TEXT"), ("licence_ref", "TEXT"),
            ("capture_date", "TEXT"), ("gsd_m", "REAL"), ("crop_path", "TEXT"), ("status", "TEXT"),
            ("error", "TEXT"), ("created_at", "TEXT")],
            geometry_type="POLYGON", key="request_id", description="Imagery crops and licences")
        g.create_table("segmentation_result", [
            ("result_id", "TEXT"), ("survey_id", "TEXT"), ("model_version", "TEXT"),
            ("prompt_variant", "TEXT"), ("sam_iou", "REAL"), ("stability", "REAL"), ("mask_path", "TEXT"),
            ("pixels", "INTEGER"), ("chosen", "INTEGER"), ("created_at", "TEXT")],
            key="result_id", description="SAM outputs per prompt variant")
        g.create_table("proposal", [
            ("survey_id", "TEXT"), ("method", "TEXT"), ("confidence", "REAL"), ("route", "TEXT"),
            ("would_route", "TEXT"), ("route_reason", "TEXT"), ("decision", "TEXT"),
            ("identity_prob", "REAL"), ("sam_iou", "REAL"), ("stability", "REAL"),
            ("footprint_agreement", "REAL"), ("flags", "TEXT"), ("hard_failures", "TEXT"), ("notes", "TEXT"),
            ("corrected_lat", "REAL"), ("corrected_lon", "REAL"), ("strategy", "TEXT"), ("shift_m", "REAL"),
            ("dist_to_polygon_m", "REAL"), ("area_m2", "REAL"), ("chosen_candidate_id", "TEXT"),
            ("footprint_source", "TEXT"), ("imagery_source", "TEXT"), ("model_version", "TEXT"),
            ("config_version", "TEXT"), ("manifest_hash", "TEXT"), ("revision", "INTEGER"),
            ("created_at", "TEXT")],
            geometry_type="POLYGON", key="survey_id", description="Automated result before review")
        g.create_table("processing_job", [
            ("job_id", "TEXT"), ("batch_id", "TEXT"), ("survey_id", "TEXT"), ("stage", "TEXT"),
            ("state", "TEXT"), ("started_at", "TEXT"), ("finished_at", "TEXT"), ("error_code", "TEXT")],
            key="job_id", description="Processing stages")
        self._job_seq = 0

    # ------------------------------------------------------------------
    def set_info(self, **kv):
        with self.gp.transaction():
            for k, v in kv.items():
                self.gp.upsert("batch_info", {"key": k, "value": v if isinstance(v, str) else _j(v)})

    def write_record(self, rec, cfg_version, cfg_hash):
        from shapely.geometry import Point
        geom = Point(rec.longitude, rec.latitude) if rec.longitude is not None else None
        self.gp.upsert("survey_record", {
            "survey_id": rec.survey_id, "row": rec.row, "latitude": rec.latitude, "longitude": rec.longitude,
            "fields_json": _j(rec.fields), "source_json": _j(rec.source), "utm_epsg": rec.utm_epsg,
            "state": rec.state, "error_code": rec.error_code, "error_message": rec.error_message,
            "warnings": _j(rec.warnings), "config_version": cfg_version, "config_hash": cfg_hash,
            "updated_at": now_iso()}, geom)

    def write_candidates(self, survey_id, cands):
        with self.gp.transaction():
            self.gp.delete_where("candidate", "survey_id", survey_id)
            for c in cands:
                row = {"cand_key": "%s|%s" % (survey_id, c.candidate_id), "candidate_id": c.candidate_id,
                       "survey_id": survey_id, "source": c.source, "source_id": c.source_id,
                       "dataset": c.dataset, "score": c.score, "probability": c.probability, "rank": c.rank,
                       "evidence_json": _j(c.evidence)}
                row.update({k: float(v) for k, v in c.features.items()})
                self.gp.insert("candidate", row, c.geom)

    def write_imagery_request(self, req, aoi_geom):
        row = dict(req)
        row.pop("aoi", None)
        self.gp.upsert("imagery_request", row, aoi_geom)

    def write_segmentation(self, survey_id, model_version, variants, stab, chosen, mask_paths):
        ts = now_iso()
        with self.gp.transaction():
            for name, v in variants.items():
                self.gp.upsert("segmentation_result", {
                    "result_id": "%s:%s:%s" % (survey_id, name, ts), "survey_id": survey_id,
                    "model_version": model_version, "prompt_variant": name, "sam_iou": v.get("sam_iou"),
                    "stability": stab, "mask_path": mask_paths.get(name, ""), "pixels": v.get("pixels"),
                    "chosen": 1 if name == chosen else 0, "created_at": ts})

    def write_proposal(self, survey_id, p, versions):
        if p is None:
            self.gp.delete_where("proposal", "survey_id", survey_id)
            return
        row = {k: p.get(k) for k in (
            "method", "confidence", "route", "would_route", "route_reason", "decision", "identity_prob",
            "sam_iou", "stability", "footprint_agreement", "corrected_lat", "corrected_lon", "strategy",
            "shift_m", "dist_to_polygon_m", "area_m2", "chosen_candidate_id", "footprint_source",
            "imagery_source", "revision")}
        row.update({"survey_id": survey_id, "flags": _j(p.get("flags", [])),
                    "hard_failures": _j(p.get("hard_failures", [])), "notes": _j(p.get("notes", [])),
                    "model_version": versions.get("model_version"), "config_version": versions.get("config_version"),
                    "manifest_hash": versions.get("manifest_hash"), "created_at": now_iso()})
        self.gp.upsert("proposal", row, p.get("geometry"))

    def job(self, batch_id, survey_id, stage, state, started_at, error_code=None):
        self._job_seq += 1
        self.gp.upsert("processing_job", {
            "job_id": "%s:%s:%s:%d" % (batch_id, survey_id, stage, self._job_seq), "batch_id": batch_id,
            "survey_id": survey_id, "stage": stage, "state": state, "started_at": started_at,
            "finished_at": now_iso(), "error_code": error_code})

    def close(self):
        self.gp.close()
