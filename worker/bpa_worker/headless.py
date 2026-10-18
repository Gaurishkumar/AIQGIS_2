"""Headless batch runs (no QGIS): processing, reports, export and imagery planning."""

import csv
import datetime as dt
import json
import logging
import os

from shapely.geometry import mapping

from bpa_common import constants as C
from bpa_common.config import freeze_config, get, load_frozen_config
from bpa_common.excel_io import copy_input, export_workbook, read_records

log = logging.getLogger(__name__)


def _feature(geom, props):
    return {"type": "Feature", "geometry": mapping(geom) if geom is not None else None, "properties": props}


def write_reports(batch, out_dir):
    """proposals.geojson, candidates.geojson, review_queue.csv and summary.json."""
    from .api import jsonable
    os.makedirs(out_dir, exist_ok=True)
    props_fc, cand_fc, rows = [], [], []
    for key, rec in batch.records.items():
        p = rec.proposal or {}
        base = {"survey_id": rec.survey_id, "row": rec.row, "state": rec.state,
                "building_name": rec.fields.get("building_name", ""), "decision": rec.decision,
                "decision_reason": rec.decision_reason, "identity_prob": rec.identity_prob}
        if p.get("geometry") is not None:
            props_fc.append(_feature(p["geometry"], jsonable({**base, **{k: p.get(k) for k in (
                "method", "confidence", "route", "would_route", "area_m2", "corrected_lat", "corrected_lon",
                "strategy", "shift_m", "dist_to_polygon_m", "chosen_candidate_id", "footprint_source")},
                "flags": ",".join(p.get("flags", [])), "hard_failures": ",".join(p.get("hard_failures", [])),
                "notes": ",".join(p.get("notes", []))})))
        for c in rec.candidates:
            cand_fc.append(_feature(c.geom, jsonable({"survey_id": rec.survey_id, "candidate_id": c.candidate_id,
                                                      "rank": c.rank, "score": round(c.score, 4),
                                                      "dataset": c.dataset, "chosen": c.candidate_id == rec.chosen_candidate_id,
                                                      **{k: round(v, 3) for k, v in c.features.items()}})))
        top = rec.candidates[0] if rec.candidates else None
        second = rec.candidates[1] if len(rec.candidates) > 1 else None
        rows.append({
            "row": rec.row, "survey_id": rec.survey_id, "building_name": rec.fields.get("building_name", ""),
            "state": rec.state, "route": p.get("route", ""), "would_route": p.get("would_route", ""),
            "decision": rec.decision or "", "method": p.get("method", ""),
            "confidence": p.get("confidence", ""), "identity_prob": rec.identity_prob,
            "n_candidates": len(rec.candidates),
            "top_score": round(top.score, 3) if top else "", "second_score": round(second.score, 3) if second else "",
            "matched_name": (top.evidence.get("matched_name") if top else "") or "",
            "distance_m": (top.evidence.get("distance_m") if top else ""),
            "area_m2": p.get("area_m2", ""), "corrected_lat": p.get("corrected_lat", ""),
            "corrected_lon": p.get("corrected_lon", ""), "shift_m": p.get("shift_m", ""),
            "flags": ";".join(p.get("flags", []) + p.get("hard_failures", [])),
            "notes": ";".join(p.get("notes", [])), "warnings": ";".join(rec.warnings),
            "reasons": ";".join(rec.reasons), "error": rec.error_message or "",
        })
    with open(os.path.join(out_dir, "proposals.geojson"), "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": props_fc}, fh)
    with open(os.path.join(out_dir, "candidates.geojson"), "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": cand_fc}, fh)
    # Review queue order: route (manual first), then confidence ascending.
    order = {C.ROUTE_MANUAL: 0, C.ROUTE_REVIEW: 1, C.ROUTE_AUTO_ACCEPT: 2, "": 3}
    rows.sort(key=lambda r: (order.get(r["route"], 3), r["confidence"] if r["confidence"] != "" else -1))
    with open(os.path.join(out_dir, "review_queue.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["survey_id"])
        w.writeheader()
        w.writerows(rows)
    summary = batch.summary(with_records=False)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(jsonable(summary), fh, indent=2)
    return summary


def prepare_batch_dir(cfg, excel, batch_dir):
    os.makedirs(batch_dir, exist_ok=True)
    for sub in ("input", "cache", "output"):
        os.makedirs(os.path.join(batch_dir, sub), exist_ok=True)
    input_copy = copy_input(excel, batch_dir)
    cfg_path = os.path.join(batch_dir, "config.yaml")
    if not os.path.exists(cfg_path):
        freeze_config(cfg, cfg_path)
    return input_copy, cfg_path


def process_workbook(cfg, excel, batch_dir, load_model=True, batch_id=None, auto_reference=False):
    from bpa_common.config import resolve_path
    from .batch import BatchManager
    from .reference import ReferenceData
    from .segmentation import SamSegmenter

    input_copy, cfg_path = prepare_batch_dir(cfg, excel, batch_dir)
    bcfg = load_frozen_config(cfg_path)
    records, report = read_records(input_copy, bcfg)
    log.info("Imported %d records (%d invalid)", report["total"], report["invalid"])
    points = [(r["longitude"], r["latitude"]) for r in records if r["state"] == C.IMPORTED]
    if auto_reference and points:
        from .prepare import prepare_reference
        prepare_reference(bcfg, points, resolve_path(bcfg, get(bcfg, "reference.dir")))
    ref = ReferenceData(bcfg)
    cov = ref.coverage(points)
    if cov["uncovered"]:
        log.warning("%d of %d survey points are outside the reference data; they will be FAILED with "
                    "REFERENCE_NOT_COVERED (run prepare-reference or use --auto-reference)",
                    cov["uncovered"], cov["total"])
    seg = SamSegmenter(bcfg) if load_model else None
    mgr = BatchManager(bcfg, ref, seg)
    batch = mgr.create(batch_dir, records, batch_id=batch_id or os.path.basename(os.path.abspath(batch_dir)),
                       config_path=cfg_path, start=False)
    t0 = dt.datetime.now()
    mgr.run_sync(batch)
    summary = write_reports(batch, os.path.join(batch_dir, "output"))
    summary["import_report"] = {k: v for k, v in report.items() if k != "headers"}
    summary["reference_coverage"] = cov
    summary["elapsed_s"] = round((dt.datetime.now() - t0).total_seconds(), 1)
    batch.store.close()
    return summary


def _read_layer(path, layer):
    import pyogrio
    try:
        return pyogrio.read_dataframe(path, layer=layer)
    except Exception as exc:
        log.warning("Could not read %s from %s: %s", layer, path, exc)
        return None


def export_batch(cfg, batch_dir, output=None, review_gpkg=None):
    """Export APPROVED / APPROVED_MANUAL buildings from review.gpkg to a workbook copy."""
    review_gpkg = review_gpkg or os.path.join(batch_dir, "review.gpkg")
    if not os.path.exists(review_gpkg):
        raise FileNotFoundError("review.gpkg not found: %s" % review_gpkg)
    inputs = [f for f in os.listdir(os.path.join(batch_dir, "input")) if f.lower().endswith((".xlsx", ".xlsm"))]
    if not inputs:
        raise FileNotFoundError("No input workbook in %s" % os.path.join(batch_dir, "input"))
    source = os.path.join(batch_dir, "input", inputs[0])
    bcfg = cfg
    frozen = os.path.join(batch_dir, "config.yaml")
    if os.path.exists(frozen):
        bcfg = load_frozen_config(frozen)
    b = _read_layer(review_gpkg, "buildings")
    cp = _read_layer(review_gpkg, "corrected_points")
    results = results_from_review(b, cp)
    stem = os.path.splitext(os.path.basename(source))[0]
    output = output or os.path.join(batch_dir, "output", "%s_output_%s.xlsx" % (stem, dt.datetime.now().strftime("%Y%m%d_%H%M%S")))
    return export_workbook(source, output, results, bcfg)


def results_from_review(buildings, corrected):
    """Build export results from review.gpkg layers (GeoDataFrames)."""
    results = {}
    if buildings is None:
        return results
    cp = {}
    if corrected is not None:
        for _, r in corrected.iterrows():
            cp[str(r["survey_id"])] = r
    for _, b in buildings.iterrows():
        sid = str(b["survey_id"])
        status = b.get("status")
        c = cp.get(sid)
        if c is None or b.geometry is None:
            continue
        results[sid] = {
            "status": status, "corrected_lat": c.get("corrected_lat"), "corrected_lon": c.get("corrected_lon"),
            "polygon_id": b.get("polygon_id"), "area_m2": b.get("area_m2"), "method": b.get("method"),
            "confidence": b.get("confidence"), "route": b.get("route"), "reviewer": b.get("reviewer"),
            "reviewed_at": b.get("reviewed_at"), "strategy": c.get("strategy"), "shift_m": c.get("shift_m"),
            "dist_to_polygon_m": c.get("dist_to_polygon_m"), "footprint_source": b.get("footprint_source"),
            "imagery_source": b.get("imagery_source"), "config_version": b.get("config_version"),
            "model_version": b.get("model_version"), "manifest_hash": b.get("manifest_hash"),
        }
    return results


def imagery_plan(cfg, batch_dir, output=None):
    """Cluster records that could not be settled by footprints (DBSCAN, haversine)."""
    import numpy as np
    from sklearn.cluster import DBSCAN
    from shapely.geometry import MultiPoint
    from .geo import to_utm, to_wgs84, utm_epsg_for
    work = os.path.join(batch_dir, "work.gpkg")
    recs = _read_layer(work, "survey_record")
    props = _read_layer(work, "proposal")
    if recs is None:
        raise FileNotFoundError("work.gpkg has no survey_record table: %s" % work)
    decided = {}
    if props is not None:
        for _, p in props.iterrows():
            decided[str(p["survey_id"])] = p.get("decision")
    need = recs[(recs["state"] != C.INVALID) & recs["survey_id"].map(lambda s: decided.get(str(s)) != C.IDENTITY_CLEAR)]
    eps_m = float(get(cfg, "imagery.clustering_eps_m", 500.0))
    radius = float(get(cfg, "candidates.search_radius_m", 50))
    fc, clusters = [], []
    if len(need):
        coords = np.radians(np.column_stack([need["latitude"].astype(float), need["longitude"].astype(float)]))
        labels = DBSCAN(eps=eps_m / 6371008.8, min_samples=1, metric="haversine").fit_predict(coords)
        need = need.assign(cluster=labels)
        for lab, grp in need.groupby("cluster"):
            lon, lat = float(grp["longitude"].mean()), float(grp["latitude"].mean())
            epsg = utm_epsg_for(lon, lat)
            mp = to_utm(MultiPoint(list(zip(grp["longitude"].astype(float), grp["latitude"].astype(float)))), epsg)
            aoi = mp.convex_hull.buffer(radius + 60.0)
            env = aoi.envelope
            info = {"cluster": int(lab), "records": int(len(grp)), "survey_ids": ",".join(map(str, grp["survey_id"])),
                    "aoi_km2": round(env.area / 1e6, 4)}
            clusters.append(info)
            fc.append(_feature(to_wgs84(env, epsg), info))
    plan = {"records_needing_imagery": int(len(need)), "clusters": len(clusters),
            "total_aoi_km2": round(sum(c["aoi_km2"] for c in clusters), 3), "eps_m": eps_m,
            "cluster_list": clusters}
    output = output or os.path.join(batch_dir, "output", "imagery_purchase_plan.geojson")
    with open(output, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": fc}, fh)
    plan["output"] = output
    return plan
