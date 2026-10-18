"""Operator explanations (lane, why, doubts, what to do) - bpa_common.explain."""

from bpa_common import constants as C
from bpa_common.explain import explain, lane


def _cand(cid, rank, score, chosen, dist, inside=False, dataset="OpenStreetMap", **ev):
    e = {"distance_m": dist, "inside": inside}
    e.update(ev)
    return {"candidate_id": cid, "rank": rank, "score": score, "chosen": chosen, "dataset": dataset,
            "source": "historic" if dataset == "historic" else "overture",
            "features": {"s_name": 1.0 if ev.get("matched_name") else 0.0}, "evidence": e}


def _detail(decision, cands, flags=(), notes=(), route=C.ROUTE_REVIEW, reason="", **prop):
    p = {"method": "footprint", "route": route, "flags": list(flags), "notes": list(notes), "hard_failures": []}
    p.update(prop)
    return {"decision": decision, "decision_reason": reason, "candidates": cands, "proposal": p}


REC = {"state": C.REVIEW, "fields": {"building_name": "Sai Dham Apartment", "customer_name": "Icici Bank Limited",
                                     "building_category": "Residential Building", "floors": "7"}}


def test_sure_record_says_why_and_to_approve():
    d = _detail(C.IDENTITY_CLEAR, [_cand("a", 1, 0.86, True, 0.0, inside=True, customer_place="ICICI Bank ATM"),
                                   _cand("b", 2, 0.41, False, 14.0)])
    e = explain(REC, d)
    assert e["lane"] == "sure" and e["label"] == "Sure"
    assert "inside this building" in " ".join(e["why"])
    assert "ICICI Bank ATM" in " ".join(e["why"])
    assert "people" in " ".join(e["why"])            # OpenStreetMap outline
    assert e["doubts"] == [] and "A" in e["todo"]


def test_close_call_names_the_other_building():
    d = _detail(C.IDENTITY_AMBIGUOUS, [_cand("a", 1, 0.62, True, 2.0), _cand("b", 2, 0.58, False, 9.0)],
                notes=[C.FLAG_IDENTITY_AMBIGUOUS], reason="Top two candidates are close (0.62 vs 0.58)")
    e = explain(REC, d)
    assert e["lane"] == "check"
    assert "9 m" in e["doubts"][0] and "almost as likely" in e["doubts"][0]
    assert "does not know this building's name" in " ".join(e["doubts"])
    assert "C" in e["todo"] and "A" in e["todo"]
    assert e["short"] == e["doubts"][0]


def test_weak_evidence_is_not_called_a_close_call():
    d = _detail(C.IDENTITY_AMBIGUOUS, [_cand("a", 1, 0.61, True, 0.0, inside=True), _cand("b", 2, 0.30, False, 47.0)],
                reason="Best score 0.61 is below 0.70")
    e = explain(REC, d)
    assert "almost as likely" not in " ".join(e["doubts"])
    assert e["doubts"][0].startswith("Only its position supports")


def test_size_doubts_say_too_small_or_too_large():
    small = _detail(C.IDENTITY_CLEAR, [_cand("a", 1, 0.8, True, 0.0, inside=True, area_band_m2=[56, 6000])],
                    flags=[C.FLAG_IMPLAUSIBLE_AREA], area_m2=30.0)
    e = explain(REC, small)
    assert e["lane"] == "check" and "small for a Residential Building with 7 floors" in e["doubts"][0]
    big = _detail(C.IDENTITY_CLEAR, [_cand("a", 1, 0.8, True, 0.0, inside=True, area_band_m2=[56, 6000])],
                  flags=[C.FLAG_IMPLAUSIBLE_AREA], area_m2=9000.0)
    e = explain(REC, big)
    assert "several buildings" in e["doubts"][0] and "X" in e["todo"]


def test_on_road_point_explains_both_sides():
    d = _detail(C.IDENTITY_AMBIGUOUS, [_cand("a", 1, 0.75, True, 6.0), _cand("b", 2, 0.4, False, 7.0)],
                reason="Survey point is on Station Road: the building side cannot be told from position alone")
    assert "either side" in explain(REC, d)["doubts"][0]


def test_draw_lane_without_a_proposal():
    rec = dict(REC, state=C.MANUAL)
    e = explain(rec, {"decision": C.NO_CANDIDATE, "candidates": [], "proposal": None})
    assert e["lane"] == "draw" and "B" in e["todo"] and "M" in e["todo"]


def test_finished_and_problem_records():
    assert lane(dict(REC, state=C.APPROVED)) == "done"
    assert explain(REC, {}, building_status=C.STATUS_APPROVED, reviewer="op7")["headline"] == "Done - approved by op7."
    assert lane(dict(REC, state=C.REJECTED)) == "rejected"
    assert lane(dict(REC, state=C.IMPORTED)) == "waiting"
    bad = dict(REC, state=C.INVALID, reasons=["Latitude is missing or not numeric ('')"])
    e = explain(bad)
    assert e["lane"] == "invalid" and "Latitude" in e["doubts"][0] and e["label"] == "Fix Excel"
    failed = explain(dict(REC, state=C.FAILED), {"error_code": "REFERENCE_NOT_COVERED"})
    assert failed["lane"] == "failed" and "download" in failed["todo"]


def test_found_by_name_is_never_sure():
    d = _detail(C.IDENTITY_CLEAR, [_cand("a", 1, 0.8, True, 240.0, found_by_name="Sai Dham")],
                notes=[C.FLAG_FOUND_BY_NAME])
    e = explain(REC, d)
    assert e["lane"] == "check" and "240 m" in e["why"][0] and "GPS" in " ".join(e["doubts"])
