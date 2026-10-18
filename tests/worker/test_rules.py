"""Unit tests: text matching, state machine, geometry, validation gates, coordinates, scoring, confidence."""

import pytest
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon, box

from bpa_common import constants as C
from bpa_worker import confidence as conf
from bpa_worker.candidates import Candidate
from bpa_worker.coords import corrected_point_utm
from bpa_worker.geo import aligned_iou, iou, repair_single_polygon, utm_epsg_for
from bpa_worker.pipeline import RecordState, StateError
from bpa_worker.scoring import Scorer
from bpa_worker.textmatch import TextMatcher
from bpa_worker.validation import longest_thin_part, sliver_loss, validate


# -- text ---------------------------------------------------------------------
def test_normalisation(cfg):
    t = TextMatcher(cfg)
    assert t.normalise("M G Road") == t.normalise("Mahatma Gandhi Road") == "mahatma gandhi road"
    assert t.normalise("Sai Dham Apt.") == "sai dham apartment"
    assert t.normalise("Om Saraswati CHS") == "om saraswati cooperative housing society"
    assert t.normalise("Co-Op Hsg Soc") == "cooperative housing society"
    assert t.core_name("Kalika Tower") == "kalika"


def test_name_similarity_ignores_generic_words(cfg):
    t = TextMatcher(cfg)
    s, m = t.name_similarity("Sai Dham Apartment", ["Shiv Apartment", "Axis Bank ATM"])
    assert s == 0.0 and m is None
    s, m = t.name_similarity("Nanak Appartment", ["Nanak Apartment"])
    assert s >= 0.9 and m == "Nanak Apartment"
    s, _ = t.name_similarity("Saidham Complex", ["Sai Dham"])
    assert s >= 0.9
    # a place named after the locality must not count as the building's name
    assert t.name_similarity("Dahisar Madhuram Hall", ["Dahisar"])[0] == 0.0
    assert t.name_similarity("Deep Darshan Residency", ["Darshan"])[0] == 0.0
    assert t.name_similarity("Simran Elegance", ["Elegance"])[0] == 0.0
    # spelling variants still match
    assert t.name_similarity("Ladiwala Bhavan", ["Kadiwala Bhawan CHS"])[0] > 0.5
    assert t.name_similarity("Mangalmurthi Apartments", ["Mangal Murti"])[0] > 0.8
    assert t.name_similarity("Bhoomi Velocity", ["Bhoomi Velocity Infotech Park"])[0] == 1.0


def test_address_requires_street(cfg):
    t = TextMatcher(cfg)
    assert t.address_similarity("Kolbad Road", ["Thane", "400601"], ["Shop 3, Kolbad Road, Thane 400601"]) > 0.8
    assert t.address_similarity("Kolbad Road", ["Thane", "400601"], ["Gokhale Road, Thane 400601"]) == 0.0
    assert t.address_similarity("Kolbad Rd", ["Thane"], ["Kolbaad Road, Thane"]) > 0.5        # abbreviation + typo


def test_street_similarity_ignores_generic_words(cfg):
    t = TextMatcher(cfg)
    assert t.street_similarity("Kolbad Road", "Gokhale Road") < 0.8
    assert t.street_similarity("M G Road", "Mahatma Gandhi Road") == 1.0
    assert t.street_similarity("Station Rd", "Station Road") == 1.0
    assert t.street_similarity("Pune Link Road", "Pune-Link Marg") >= 0.8
    assert t.street_similarity("Road", "Station Road") == 0.0


def test_customer_place_matching(cfg):
    t = TextMatcher(cfg)
    same = [("Icici Bank Limited", "ICICI Bank ATM"), ("State Bank Of India", "SBI ATM"),
            ("Saraswat Co Operative Bank", "The Saraswat Co-operative Bank Ltd"), ("Avenue Supermarts Ltd", "D-Mart Kalyan"),
            ("Union Bank Of India", "Union Bank"), ("Kotak Mahindra Bank Ltd.", "Kotak Mahindra"),
            ("Hdfc Bank Ltd.", "HDFC ATM"), ("Karur Vysya Bank Ltd", "Karur Vysya Bank Dombivali Branch")]
    for customer, place in same:
        assert t.customer_similarity(customer, [place])[0] == 1.0, (customer, place)
    different = [("Bank Of India", "State Bank of India ATM"), ("Bank Of Maharashtra", "Bank of India"),
                 ("Axis Bank Limited", "Axis Mall"), ("State Bank Of India", "State Bank"), ("Icici Bank Limited", "HDFC Bank"),
                 ("", "HDFC Bank")]
    for customer, place in different:
        assert t.customer_similarity(customer, [place])[0] == 0.0, (customer, place)
    assert t.customer_similarity("Icici Bank Limited", ["Sai Dham", "ICICI Bank ATM"])[1] == "ICICI Bank ATM"


# -- states -------------------------------------------------------------------
def test_state_machine():
    r = RecordState({"survey_id": "X", "latitude": 19.0, "longitude": 73.0})
    for s in (C.QUEUED, C.MATCHING, C.FOOTPRINT_READY, C.VALIDATING, C.REVIEW, C.APPROVED, C.EXPORTED):
        r.transition(s)
    with pytest.raises(StateError):
        r.transition(C.QUEUED)
    r2 = RecordState({"survey_id": "Y", "reasons": ["bad"]})
    assert r2.state == C.INVALID
    with pytest.raises(StateError):
        r2.transition(C.QUEUED)
    r3 = RecordState({"survey_id": "Z", "latitude": 19.0, "longitude": 73.0})
    for s in (C.QUEUED, C.MATCHING, C.NEEDS_IMAGERY, C.NO_IMAGERY_AVAILABLE, C.REVIEW, C.MATCHING):
        r3.transition(s)        # operator re-processing from REVIEW is allowed


# -- geometry ---------------------------------------------------------------------
def test_utm_zone_for_india():
    assert utm_epsg_for(73.14, 19.23) == 32643      # Mumbai
    assert utm_epsg_for(77.2, 28.6) == 32643        # Delhi
    assert utm_epsg_for(88.36, 22.57) == 32645      # Kolkata
    assert utm_epsg_for(80.27, 13.08) == 32644      # Chennai


def test_iou_and_aligned_iou():
    a = box(0, 0, 10, 10)
    b = affinity.translate(a, 2, 0)
    assert iou(a, b) == pytest.approx(80 / 120)
    assert aligned_iou(a, b) == pytest.approx(1.0)


def test_repair_single_polygon():
    bow = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])           # self-intersecting
    g, n = repair_single_polygon(bow)
    assert g.is_valid and n == 2
    mp = MultiPolygon([box(0, 0, 10, 10), box(20, 20, 20.3, 20.3)])
    g, n = repair_single_polygon(mp)
    assert n == 1 and g.area == pytest.approx(100)


# -- validation gates ------------------------------------------------------------------
def _vctx(**kw):
    ctx = {"point_utm": Point(5, 5), "radius_m": 50.0, "fields": {"building_category": "Residential Building",
                                                                   "floors": "4"}}
    ctx.update(kw)
    return ctx


def test_validation_hard_gates(cfg):
    _, hard, _, _ = validate(box(0, 0, 3, 3), _vctx(), cfg)
    assert C.GATE_AREA in hard
    _, hard, _, _ = validate(MultiPolygon([box(0, 0, 10, 10), box(30, 0, 40, 10)]), _vctx(), cfg)
    assert C.GATE_MULTIPART in hard
    _, hard, _, _ = validate(box(0, 0, 10, 10), _vctx(is_segmented=True, touches_edge=True), cfg)
    assert C.GATE_CROP_EDGE in hard
    _, hard, _, _ = validate(box(500, 500, 520, 520), _vctx(), cfg)
    assert C.GATE_CRS in hard


def test_validation_soft_flags(cfg):
    fp = box(0, 0, 20, 20)
    _, hard, soft, det = validate(box(9, 0, 29, 20), _vctx(footprint_utm=fp, is_segmented=True), cfg)
    assert not hard and C.FLAG_LOW_FOOTPRINT_AGREEMENT in soft
    _, _, soft, _ = validate(box(0, 0, 20, 20), _vctx(approved_utm=[box(0, 0, 20, 5)]), cfg)
    assert C.FLAG_OVERLAPS_APPROVED in soft
    _, _, soft, _ = validate(box(60, 0, 80, 20), _vctx(), cfg)
    assert C.FLAG_FAR_FROM_SURVEY in soft
    spiky = box(0, 0, 20, 20).union(box(20, 9.8, 60, 10.2))
    assert sliver_loss(spiky, 1.0) < 0.15                    # tiny share of the area ...
    assert longest_thin_part(spiky, 1.0) > 30                # ... but a 40 m spike
    _, _, soft, _ = validate(spiky, _vctx(), cfg)
    assert C.FLAG_SLIVER in soft
    _, _, soft, _ = validate(box(0, 0, 20, 20), _vctx(), cfg)
    assert C.FLAG_SLIVER not in soft


# -- corrected coordinate ------------------------------------------------------------------
def test_corrected_point_strategies():
    sq = box(0, 0, 10, 10)
    p, used = corrected_point_utm(sq, Point(15, 5), "centroid_if_inside")
    assert (p.x, p.y) == (5, 5) and used == "centroid_if_inside"
    u = Polygon([(0, 0), (30, 0), (30, 30), (20, 30), (20, 10), (10, 10), (10, 30), (0, 30)])   # U shape
    p, used = corrected_point_utm(u, Point(15, 40), "centroid_if_inside")
    assert u.contains(p) and used.endswith("representative_point")
    p, _ = corrected_point_utm(sq, Point(15, 5), "snap_to_boundary")
    assert (p.x, p.y) == (10, 5)
    p, used = corrected_point_utm(sq, Point(3, 3), "centroid_if_inside", keep_original_if_inside=True)
    assert (p.x, p.y) == (3, 3) and used == "original_inside"


# -- scoring / decision -------------------------------------------------------------------------
def _cand(cid, feats, source="overture", dist=0.0):
    c = Candidate(candidate_id=cid, source=source, source_id=cid, dataset="OpenStreetMap", geom=None,
                  geom_utm=box(0, 0, 1, 1))
    c.features = dict(zip(("s_dist", "s_name", "s_addr", "s_road", "s_plaus", "s_source"), feats))
    c.evidence = {"distance_m": dist, "inside": dist == 0}
    return c


def test_decision_rule(cfg):
    sc = Scorer(cfg)
    clear = sc.score([_cand("a", (1, 1, 1, 1, 1, .8)), _cand("b", (0.5, 0, 0, .5, 1, .6))])
    assert sc.decide(clear)[0] == C.IDENTITY_CLEAR
    close = sc.score([_cand("a", (1, 0, 0, .5, 1, .8)), _cand("b", (0.9, 0, 0, .5, 1, .8))])
    assert sc.decide(close)[0] == C.IDENTITY_AMBIGUOUS
    weak = sc.score([_cand("a", (0.3, 0, 0, .5, 1, .6))])
    assert sc.decide(weak)[0] == C.IDENTITY_AMBIGUOUS
    assert sc.decide([])[0] == C.NO_CANDIDATE
    hist = sc.score([_cand("h", (0.8, 0, 0, .5, 1, 1), source="historic", dist=3.0),
                     _cand("o", (1, 0, 0, .5, 1, .8))])
    d, chosen, p, why = sc.decide(hist)
    assert d == C.IDENTITY_CLEAR and chosen.candidate_id == "h" and "Historic" in why


def test_on_road_point_needs_name_evidence(cfg):
    sc = Scorer(cfg)
    cs = sc.score([_cand("a", (1, 0, 0.5, 1, 1, .8)), _cand("b", (0.3, 0, 0.5, .5, 1, .6))])
    assert sc.decide(cs)[0] == C.IDENTITY_CLEAR
    d, chosen, _, why = sc.decide(cs, on_road="Station Road")
    assert d == C.IDENTITY_AMBIGUOUS and chosen.candidate_id == "a" and "Station Road" in why
    named = sc.score([_cand("a", (1, 1, 0.5, 1, 1, .8)), _cand("b", (0.3, 0, 0.5, .5, 1, .6))])
    assert sc.decide(named, on_road="Station Road")[0] == C.IDENTITY_CLEAR


def test_weights_match_spec(cfg):
    sc = Scorer(cfg)
    [c] = sc.score([_cand("a", (1, 0.5, 0.2, 0.5, 1, 0.8))])
    assert c.score == pytest.approx(0.40 + 0.20 * 0.5 + 0.10 * 0.2 + 0.15 * 0.5 + 0.10 + 0.05 * 0.8)


def test_calibrated_identity_model(cfg):
    model = {"identity": {"features": ["s_dist", "s_name", "s_addr", "s_road", "s_plaus", "s_source", "margin"],
                          "coef": [6, 3, 1, 1, 1, 0.5, 4], "intercept": -6, "threshold": 0.6, "margin": 0.2}}
    sc = Scorer(cfg, model)
    cs = sc.score([_cand("a", (1, 1, 0, .5, 1, .8)), _cand("b", (0.2, 0, 0, .5, 1, .6))])
    assert cs[0].candidate_id == "a" and abs(sum(c.probability for c in cs) - 1) < 1e-9
    assert sc.decide(cs)[0] == C.IDENTITY_CLEAR


# -- confidence / routing -------------------------------------------------------------------------
def test_routing_shadow_mode(cfg):
    route, would, _ = conf.route("S1", 0.95, [], [], C.IDENTITY_CLEAR, "footprint", True, cfg)
    assert route == C.ROUTE_REVIEW and would == C.ROUTE_AUTO_ACCEPT
    assert conf.route("S1", 0.95, [C.GATE_AREA], [], C.IDENTITY_CLEAR, "footprint", True, cfg)[0] == C.ROUTE_MANUAL
    assert conf.route("S1", 0.95, [], ["x"], C.IDENTITY_CLEAR, "footprint", True, cfg)[1] == C.ROUTE_REVIEW
    assert conf.route("S1", 0.95, [], [], C.IDENTITY_CLEAR, "no_candidate", True, cfg)[1] == C.ROUTE_REVIEW
    assert conf.route("S1", 0.0, [], [], C.NO_CANDIDATE, "no_candidate", False, cfg)[0] == C.ROUTE_MANUAL
    cfg["routing"]["shadow_mode"] = False
    cfg["routing"]["auto_accept_enabled"] = True
    cfg["routing"]["audit_sample_rate"] = 0.0
    assert conf.route("S1", 0.95, [], [], C.IDENTITY_CLEAR, "footprint", True, cfg)[0] == C.ROUTE_AUTO_ACCEPT


def test_confidence_formula(cfg):
    c1 = conf.record_confidence({"identity_prob": 0.9, "dataset": "OpenStreetMap", "n_flags": 0}, cfg)
    assert c1 == pytest.approx(0.9 * 0.85)
    c2 = conf.record_confidence({"identity_prob": 0.9, "sam_iou": 0.96, "stability": 0.9,
                                 "footprint_agreement": 0.84, "n_flags": 1}, cfg)
    assert c2 == pytest.approx(0.9 * 0.9 * 0.85)
