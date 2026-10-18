"""Pipeline paths, SAM refinement, click-to-fix, select-candidate, historic polygons, API and calibration."""

import copy
import json
import os
import time

import geopandas as gpd
import pyogrio
import pytest
from shapely import affinity

from bpa_common import constants as C
from bpa_worker import synthetic as S
from bpa_worker.batch import BatchManager
from bpa_worker.geo import iou, to_wgs84
from bpa_worker.reference import ReferenceData
from bpa_worker.segmentation import SamSegmenter

from conftest import needs_model, synthetic_cfg


def run_batch(cfg, records, tmp_path, name="b", model=False):
    mgr = BatchManager(cfg, ReferenceData(cfg), SamSegmenter(cfg) if model else None)
    b = mgr.create(str(tmp_path / name), copy.deepcopy(records), batch_id=name, start=False)
    mgr.run_sync(b)
    return mgr, b


def test_footprint_path_without_imagery(base_cfg, synthetic, tmp_path):
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    _, b = run_batch(cfg, synthetic["records"], tmp_path)
    for rec in b.records.values():
        assert rec.state in (C.REVIEW, C.MANUAL)
        assert rec.proposal["method"] == C.METHOD_FOOTPRINT
        assert rec.imagery["status"] == "NO_PROVIDERS_CONFIGURED"
        assert rec.utm_epsg == S.EPSG
    g = b.records["SYN_B03"]
    assert g.candidates[0].evidence.get("matched_name") == "Ganesh Heights"   # place POI inside
    work = os.path.join(str(tmp_path / "b"), "work.gpkg")
    assert len(pyogrio.read_dataframe(work, layer="proposal")) == 6
    assert len(pyogrio.read_dataframe(work, layer="candidate")) >= 6


def test_gate_closed_means_no_imagery(base_cfg, synthetic, tmp_path):
    cfg = synthetic_cfg(base_cfg, synthetic, allow=False)
    _, b = run_batch(cfg, synthetic["records"], tmp_path, "closed")
    for rec in b.records.values():
        assert "SAM" not in rec.proposal["method"]
        assert rec.imagery["status"] == "REFUSED_OR_UNAVAILABLE"
        assert rec.imagery["refused"][0]["reason"] == "allow_automated_analysis is false"


def test_no_candidate_goes_manual(base_cfg, synthetic, tmp_path):
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    far = copy.deepcopy(synthetic["records"][0])
    far["survey_id"] = "FAR"
    far["latitude"] += 0.0009           # ~100 m north: open ground, no footprints
    _, b = run_batch(cfg, [far], tmp_path, "far")
    rec = b.records["FAR"]
    assert rec.decision == C.NO_CANDIDATE and rec.state == C.MANUAL and rec.proposal is None


def test_reference_not_covered(base_cfg, synthetic, tmp_path):
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    ref = ReferenceData(cfg)
    ref.boxes = [(0.0, 0.0, 1.0, 1.0)]  # pretend the manifest covers another area only
    mgr = BatchManager(cfg, ref, None)
    b = mgr.create(str(tmp_path / "nc"), copy.deepcopy(synthetic["records"][:1]), batch_id="nc", start=False)
    mgr.run_sync(b)
    rec = next(iter(b.records.values()))
    assert rec.state == C.FAILED and rec.error_code == "REFERENCE_NOT_COVERED"


def test_historic_polygon_wins(base_cfg, synthetic, tmp_path):
    ref_dir = tmp_path / "ref_hist"
    import shutil
    shutil.copytree(synthetic["ref"], ref_dir)
    poly = S.scene_buildings()[0][2]
    gpd.GeoDataFrame({"polygon_id": ["H1"], "survey_id": ["OLD_1"], "building_name": ["Sai Krupa Apartment"]},
                     geometry=[to_wgs84(poly, S.EPSG)], crs=4326).to_file(str(ref_dir / "historic.gpkg"))
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    cfg["reference"]["dir"] = str(ref_dir)
    _, b = run_batch(cfg, synthetic["records"][:1], tmp_path, "hist")
    rec = b.records["SYN_B01"]
    assert rec.decision == C.IDENTITY_CLEAR and rec.proposal["method"] == C.METHOD_HISTORIC
    assert not any(c.source == "overture" and iou(c.geom_utm, poly) > 0.5 for c in rec.candidates)   # deduplicated
    assert rec.proposal["confidence"] > 0.85


@needs_model
def test_sam_refinement_and_operator_tools(base_cfg, synthetic, tmp_path):
    cfg = synthetic_cfg(base_cfg, synthetic)
    mgr, b = run_batch(cfg, synthetic["records"], tmp_path, "sam", model=True)
    fp, pr = [], []
    for sid, rec in b.records.items():
        truth = synthetic["truth"][sid]
        chosen = rec.candidate(rec.chosen_candidate_id)
        fp.append(iou(chosen.geom_utm, truth))
        pr.append(iou(rec.proposal["geometry_utm"], truth))
        assert "SAM" in rec.proposal["method"] and rec.segmentation["stability"] > 0.5
    assert sorted(pr)[len(pr) // 2] >= 0.85 and sum(pr) > sum(fp) + 0.5
    work = os.path.join(str(tmp_path / "sam"), "work.gpkg")
    seg = pyogrio.read_dataframe(work, layer="segmentation_result")
    assert set(seg["prompt_variant"]) >= {"A", "B", "C"}
    req = pyogrio.read_dataframe(work, layer="imagery_request")
    assert (req["licence_ref"] == "SYNTHETIC-TEST-001").all() and (req["status"] == "OK").all()

    # click-to-fix on the cached embedding
    rec = b.records["SYN_B06"]
    truth = synthetic["truth"]["SYN_B06"]
    inside = to_wgs84(affinity.translate(truth.centroid, 3, 3), S.EPSG)
    t0 = time.time()
    b.pipeline.resegment(rec, b.ctx(), [[inside.x, inside.y]], [], None)
    assert time.time() - t0 < 5.0
    assert rec.proposal["method"] == C.METHOD_CLICK_TO_FIX and iou(rec.proposal["geometry_utm"], truth) > 0.85
    assert rec.state == C.REVIEW

    # operator picks another candidate
    rec = b.records["SYN_B01"]
    other = next(c for c in rec.candidates if c.candidate_id != rec.chosen_candidate_id)
    b.pipeline.select_candidate(rec, b.ctx(), other.candidate_id)
    assert rec.chosen_candidate_id == other.candidate_id and rec.identity_prob == 1.0
    with pytest.raises(LookupError):
        b.pipeline.select_candidate(rec, b.ctx(), "overture:nope")


def test_api_contract(base_cfg, synthetic, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from bpa_worker.api import create_app
    monkeypatch.setenv("BPA_TOKEN", "secret")
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    client = TestClient(create_app(cfg, load_model=False))
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["auth_required"] and h["reference"]["counts"]["buildings"] == 6
    assert client.get("/batches").status_code == 401
    H = {"X-BPA-Token": "secret"}
    recs = copy.deepcopy(synthetic["records"])
    recs.append({"survey_id": "BAD", "row": 99, "latitude": None, "longitude": None, "fields": {}, "reasons": ["x"]})
    r = client.post("/batches", json={"batch_id": "api1", "batch_dir": str(tmp_path / "api1"), "records": recs},
                    headers=H)
    assert r.status_code == 201 and r.json()["accepted"] == 6 and r.json()["invalid"] == 1
    for _ in range(200):
        s = client.get("/batches/api1", headers=H).json()
        if s["status"] == "done":
            break
        time.sleep(0.1)
    assert s["status"] == "done" and s["counts"]["states"].get(C.INVALID) == 1
    d = client.get("/records/SYN_B03", headers=H).json()
    assert d["proposal"]["geometry"]["type"] == "Polygon" and d["candidates"] and d["versions"]["manifest_hash"]
    assert client.get("/records/NOPE", headers=H).status_code == 404
    cid = d["candidates"][-1]["candidate_id"]
    d2 = client.post("/records/SYN_B03/select-candidate", json={"candidate_id": cid}, headers=H).json()
    assert d2["chosen_candidate_id"] == cid
    r = client.post("/records/SYN_B03/resegment", json={"positive_points": [[73.0, 19.0]]}, headers=H)
    assert r.status_code == 409                                  # no licensed crop cached
    cov = client.post("/reference/coverage", json={"points": [[73.0, 19.25]]}, headers=H).json()
    assert cov["total"] == 1
    bulk = client.get("/batches/api1/records", headers=H).json()
    assert len(bulk["records"]) == 7


def test_calibration_and_metrics(base_cfg, synthetic, tmp_path):
    """With ground truth the identity model, radius and pilot metrics are produced."""
    from bpa_worker.calibration import calibrate_from_ground_truth, evaluate_batch
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    recs = []
    for k in range(4):          # replicate the scene's records with jitter to get enough labels
        for r in synthetic["records"]:
            r2 = copy.deepcopy(r)
            r2["survey_id"] = "%s_%d" % (r["survey_id"], k)
            r2["latitude"] += (k - 1.5) * 0.00002
            r2["fields"]["area"] = "Area%d" % k
            recs.append(r2)
    _, b = run_batch(cfg, recs, tmp_path, "cal")
    gt_rows = [{"survey_id": r["survey_id"], "geometry": to_wgs84(synthetic["truth"][r["survey_id"].rsplit("_", 1)[0]], S.EPSG)}
               for r in recs]
    gt = str(tmp_path / "gt.gpkg")
    gpd.GeoDataFrame(gt_rows, geometry="geometry", crs=4326).to_file(gt)
    rep = calibrate_from_ground_truth(cfg, str(tmp_path / "cal"), gt, str(tmp_path / "calout"), target_precision=0.9)
    assert 25 <= rep["radius_m"] <= 100
    assert rep["identity"] and rep["identity"]["heldout_top1_accuracy"] is not None
    cal = json.load(open(rep["calibration_file"]))
    assert set(cal) >= {"radius_m", "identity", "confidence"}
    m = evaluate_batch(cfg, str(tmp_path / "cal"), gt)
    assert m["_overall"]["n"] == 24 and m["_overall"]["correct_building_rate"] >= 0.9
    assert m["footprint"]["iou_median"] > 0.5


def _fake_review_gpkg(path, records, polys):
    """A review.gpkg as the plugin writes it: approved buildings + survey points."""
    import pyogrio
    from shapely.geometry import Point
    b = gpd.GeoDataFrame({"polygon_id": ["b1-%s" % r["survey_id"] for r in records],
                          "survey_id": [r["survey_id"] for r in records],
                          "status": ["APPROVED"] * (len(records) - 1) + ["REJECTED"],
                          "reviewer": ["op"] * len(records), "reviewed_at": ["2026-10-13T10:00:00"] * len(records)},
                         geometry=polys, crs=4326)
    pyogrio.write_dataframe(b, path, layer="buildings", driver="GPKG")
    s = gpd.GeoDataFrame({"survey_id": [r["survey_id"] for r in records],
                          "building_name": [r["fields"]["building_name"] for r in records],
                          "address": ["Tilak Road, Kalyan"] * len(records)},
                         geometry=[Point(r["longitude"], r["latitude"]) for r in records], crs=4326)
    pyogrio.write_dataframe(s, path, layer="survey_points", driver="GPKG", append=True)
    cp = gpd.GeoDataFrame({"survey_id": [r["survey_id"] for r in records],
                           "corrected_lat": [p.centroid.y for p in polys], "corrected_lon": [p.centroid.x for p in polys]},
                          geometry=[p.centroid for p in polys], crs=4326)
    pyogrio.write_dataframe(cp, path, layer="corrected_points", driver="GPKG", append=True)


def test_learn_from_approved_makes_next_survey_a_historic_match(base_cfg, synthetic, tmp_path):
    import shutil
    from bpa_worker.calibration import load_ground_truth
    from bpa_worker.learn import learn_from_review
    ref_dir = tmp_path / "ref_learn"
    shutil.copytree(synthetic["ref"], ref_dir)
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    cfg["reference"]["dir"] = str(ref_dir)
    recs = synthetic["records"][:3]
    polys = [to_wgs84(synthetic["truth"][r["survey_id"]], S.EPSG) for r in recs]
    review = str(tmp_path / "review.gpkg")
    _fake_review_gpkg(review, recs, polys)

    _, before = run_batch(cfg, recs, tmp_path, "before")
    assert all(r.proposal["method"] == C.METHOD_FOOTPRINT for r in before.records.values())

    res = learn_from_review(cfg, review, str(ref_dir))
    assert res["added"] == 2 and res["total"] == 2           # the rejected one is not learned
    again = learn_from_review(cfg, review, str(ref_dir))
    assert again["added"] == 0 and again["updated"] == 2 and again["total"] == 2   # no duplicates

    _, after = run_batch(cfg, recs, tmp_path, "after")
    learned = [after.records[r["survey_id"]] for r in recs[:2]]
    assert all(r.decision == C.IDENTITY_CLEAR and r.proposal["method"] == C.METHOD_HISTORIC for r in learned)
    assert all(iou(r.proposal["geometry_utm"], synthetic["truth"][r.survey_id]) > 0.99 for r in learned)
    assert after.records[recs[2]["survey_id"]].proposal["method"] == C.METHOD_FOOTPRINT

    gt = load_ground_truth(review)                          # review.gpkg doubles as ground truth
    assert set(gt) == {recs[0]["survey_id"], recs[1]["survey_id"]}
    assert gt[recs[0]["survey_id"]]["corrected_lat"] is not None


def test_far_name_search_offers_but_does_not_override(base_cfg, synthetic, tmp_path):
    """A survey point far from its building: the named building is found and offered."""
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    rec = copy.deepcopy(next(r for r in synthetic["records"] if r["survey_id"] == "SYN_B03"))
    rec["survey_id"] = "FAR_B03"
    rec["latitude"] += 0.0012                                  # ~130 m north of Ganesh Heights
    _, b = run_batch(cfg, [rec], tmp_path, "farname")
    r = b.records["FAR_B03"]
    far = [c for c in r.candidates if c.evidence.get("found_by_name")]
    assert far and far[0].evidence["found_by_name"] == "Ganesh Heights"
    assert iou(far[0].geom_utm, synthetic["truth"]["SYN_B03"]) > 0.5
    assert r.state in (C.REVIEW, C.MANUAL)
    detail = r.detail()
    assert detail["name_found_far"] and detail["name_found_far"][0]["name"] == "Ganesh Heights"


@needs_model
def test_building_too_small_for_the_imagery_keeps_the_footprint(base_cfg, synthetic, tmp_path):
    cfg = synthetic_cfg(base_cfg, synthetic)
    cfg["imagery"]["min_building_px"] = 10000                          # nothing is wide enough
    _, b = run_batch(cfg, synthetic["records"][:2], tmp_path, "coarse", model=True)
    for rec in b.records.values():
        assert "SAM" not in rec.proposal["method"]
        assert C.FLAG_IMAGERY_TOO_COARSE in rec.proposal.get("notes", [])
        assert rec.imagery["status"] == "TOO_COARSE" and "px wide" in rec.imagery["note"]
