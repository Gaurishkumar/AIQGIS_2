"""S3 - Candidate generation and scoring features (Final MVP Specification, Section 6).

Features per candidate (all 0..1):
  s_dist   1 inside, else exp(-d / 15 m)
  s_name   fuzzy building name vs historic names, Overture building names and
           Overture places inside / within 10 m
  s_addr   street + locality + pincode vs candidate address data
  s_road   1 near the named street, 0 across a road, 0.5 otherwise
  s_plaus  area plausible for category/floors; height consistency can only lower it
  s_source historic 1.0, OSM 0.8, ML 0.6
"""

import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import LineString
from shapely.ops import nearest_points

from bpa_common.config import get

from .geo import degrees_bbox, iou, point_utm

FEATURES = ("s_dist", "s_name", "s_addr", "s_road", "s_plaus", "s_source")


@dataclass
class Candidate:
    candidate_id: str
    source: str                 # "historic" | "overture"
    source_id: str
    dataset: str                # OpenStreetMap / Microsoft ML Buildings / ... / historic
    geom: object                # shapely, EPSG:4326
    geom_utm: object            # shapely, record UTM
    names: list = field(default_factory=list)
    addresses: list = field(default_factory=list)
    height: float = None
    num_floors: float = None
    features: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    score: float = 0.0
    probability: float = None
    rank: int = 0

    def to_dict(self):
        return {
            "candidate_id": self.candidate_id,
            "source": self.source,
            "source_id": self.source_id,
            "dataset": self.dataset,
            "features": {k: round(float(v), 4) for k, v in self.features.items()},
            "score": round(float(self.score), 4),
            "probability": None if self.probability is None else round(float(self.probability), 4),
            "rank": self.rank,
            "names": self.names[:10],
            "evidence": self.evidence,
        }


def search_radius(cfg, calibration=None):
    lo, hi = get(cfg, "candidates.radius_clamp_m", [25, 100])
    r = float(get(cfg, "candidates.search_radius_m", 50))
    if calibration and calibration.get("radius_m"):
        r = float(calibration["radius_m"])
    return float(min(max(r, lo), hi))


def _to_utm_series(gdf, epsg):
    return gdf.geometry.to_crs(epsg)


class CandidateGenerator(object):
    def __init__(self, cfg, reference, text):
        self.cfg = cfg
        self.ref = reference
        self.text = text
        sc = get(cfg, "scoring", {}) or {}
        self.decay = float(sc.get("dist_decay_m", 15.0))
        self.places_within = float(sc.get("name_places_within_m", 10.0))
        self.road_within = float(sc.get("road_match_within_m", 20.0))
        self.road_ignore = float(sc.get("road_point_ignore_m", 5.0))
        self.road_thr = float(sc.get("road_name_match_threshold", 0.8))
        self.road_extra = float(sc.get("road_search_extra_m", 60.0))
        self.crossing_classes = set(sc.get("crossing_road_classes", []) or [])
        self.half_widths = sc.get("road_half_width_m", {}) or {}
        self.addr_requires_street = bool(sc.get("addr_requires_street", True))
        self.addr_missing = float(sc.get("addr_missing_value", 0.0))
        self.trust = sc.get("source_trust", {}) or {}
        self.customer_places = bool(sc.get("customer_place_evidence", False))
        self.dedup_iou = float(get(cfg, "candidates.dedup_iou", 0.5))
        self.name_radius = float(get(cfg, "candidates.name_search_radius_m", 300) or 0)
        self.name_radius_low = float(get(cfg, "candidates.name_search_low_precision_m", 3000) or 0)
        self.name_min = float(get(cfg, "candidates.name_search_min_similarity", 0.9))
        self.name_max = int(get(cfg, "candidates.name_search_max_results", 3))
        self.max_candidates = int(get(cfg, "candidates.max_candidates", 100))
        pl = get(cfg, "plausibility", {}) or {}
        self.bands = pl.get("category_area_m2", {}) or {}
        self.min_area_per_floor = float(pl.get("min_area_per_floor_m2", 0) or 0)
        self.floor_h = float(pl.get("floor_height_m", 3.0))
        tol = pl.get("height_tolerance", {}) or {}
        self.h_abs = float(tol.get("abs_m", 6.0))
        self.h_rel = float(tol.get("rel", 0.5))
        self.h_factor = float(pl.get("height_mismatch_factor", 0.5))

    # ------------------------------------------------------------------
    def generate(self, record, epsg, radius):
        """Return (candidates, context) for one survey record."""
        lon, lat = record["longitude"], record["latitude"]
        pt = point_utm(lon, lat, epsg)
        ctx = {"point_utm": pt, "radius_m": radius, "epsg": epsg}
        f = record.get("fields", {})

        # -- footprints intersecting the search circle --------------------
        big_bbox = degrees_bbox(lon, lat, radius + 40.0)
        cands = []
        hist = self.ref.query_bbox(self.ref.historic, big_bbox)
        if hist is not None and len(hist):
            hu = _to_utm_series(hist, epsg)
            for (i, row), gu in zip(hist.iterrows(), hu):
                if gu.distance(pt) <= radius:
                    cands.append(Candidate(
                        candidate_id="historic:%s" % row["source_id"], source="historic",
                        source_id=str(row["source_id"]), dataset="historic",
                        geom=row.geometry, geom_utm=gu, names=list(row["names"]),
                        addresses=[row["address"]] if row["address"] else [],
                        evidence={"historic_survey_id": row.get("survey_id") or None}))
        bld = self.ref.query_bbox(self.ref.buildings, big_bbox)
        neighbours = []   # all nearby buildings (for place assignment and negative prompts)
        if bld is not None and len(bld):
            bu = _to_utm_series(bld, epsg)
            for (i, row), gu in zip(bld.iterrows(), bu):
                neighbours.append((row["source_id"], gu, row.geometry))
                if gu.distance(pt) > radius:
                    continue
                cands.append(Candidate(
                    candidate_id="overture:%s" % row["source_id"], source="overture",
                    source_id=str(row["source_id"]), dataset=row["dataset"] or "",
                    geom=row.geometry, geom_utm=gu, names=list(row["names"]),
                    height=row["height"], num_floors=row["num_floors"]))
        ctx["neighbours"] = neighbours

        # -- dedup: historic wins when IoU > threshold ----------------------
        hist_c = [c for c in cands if c.source == "historic"]
        if hist_c:
            kept = []
            for c in cands:
                if c.source == "overture" and any(iou(c.geom_utm, h.geom_utm) > self.dedup_iou for h in hist_c):
                    continue
                kept.append(c)
            cands = kept
        if len(cands) > self.max_candidates:
            cands.sort(key=lambda c: c.geom_utm.distance(pt))
            cands = cands[: self.max_candidates]

        roads = self._roads(lon, lat, radius, epsg, f.get("street", "")) if cands else []
        if cands:
            self._attach_places(cands, neighbours, lon, lat, radius, epsg)
            ctx["roads"] = roads
            ctx["on_road"] = self._on_road(pt, roads)
            for c in cands:
                self._features(c, record, pt, roads)

        # -- worst case: the point is far from the building ----------------
        # When nothing nearby carries the building's name, look further out for a
        # place or building with that name and offer it as a candidate.
        best_name = max([c.features.get("s_name", 0.0) for c in cands] or [0.0])
        if best_name < 0.75:
            far = self._name_search(record, epsg, pt, {c.source_id for c in cands})
            for c in far:
                self._features(c, record, pt, roads)
            cands.extend(far)
            ctx["name_found_far"] = [{"candidate_id": c.candidate_id, "name": c.evidence["found_by_name"],
                                      "distance_m": c.evidence.get("distance_m")} for c in far]
        return cands, ctx

    def _name_search(self, record, epsg, pt, exclude):
        """Buildings within a wider radius whose own name, or a place inside them, matches strongly.

        The radius grows when the coordinate looks truncated. A match needs a
        distinctive name (5+ letters once words like "apartment" are removed),
        a similarity of at least 0.9, and the same pincode when one is known.
        """
        f = record.get("fields", {})
        core = self.text.core_name(f.get("building_name", ""))
        if len(core.replace(" ", "")) < 5 or self.name_radius <= 0:
            return []
        low_precision = any("precision" in (w or "").lower() for w in record.get("warnings") or [])
        rad = self.name_radius_low if low_precision else self.name_radius
        lon, lat = record["longitude"], record["latitude"]
        pincode = (f.get("pincode") or "").strip()
        found = {}   # building source_id -> (similarity, matched name, address)

        def match(names, address):
            """Best similarity of the record name against ``names`` (None when it is no match)."""
            best, best_name = 0.0, None
            for n in names:
                c2 = self.text.core_name(n)
                if c2:
                    sim = self.text.name_sim(core, c2)
                    if sim > best:
                        best, best_name = sim, n
            if best < self.name_min:
                return None
            if pincode and address:
                codes = [w for w in address.replace(",", " ").split() if w.isdigit() and len(w) == 6]
                if codes and pincode not in codes:
                    return None
            return best, best_name

        def keep(bid, hit, address):
            if bid in exclude:
                return
            if bid not in found or hit[0] > found[bid][0]:
                found[bid] = (hit[0], hit[1], address or "")

        bbox = degrees_bbox(lon, lat, rad)
        pl = self.ref.query_bbox(self.ref.places, bbox)
        if pl is not None and len(pl):
            for _, row in pl.iterrows():
                if not row["names"]:
                    continue
                hit = match(list(row["names"]), row["address"])     # cheap text test first
                if hit is None:
                    continue
                p = row.geometry
                near = self.ref.query_bbox(self.ref.buildings, degrees_bbox(p.x, p.y, 10.0))
                if near is None or not len(near):
                    continue
                inside = [r["source_id"] for _, r in near.iterrows() if r.geometry.contains(p)]
                bid = inside[0] if inside else min(near.iterrows(), key=lambda t: t[1].geometry.distance(p))[1]["source_id"]
                keep(bid, hit, row["address"])
        bl = self.ref.query_bbox(self.ref.buildings, bbox)
        if bl is not None and len(bl):
            named = bl[bl["names"].map(len) > 0]
            for _, row in named.iterrows():
                hit = match(list(row["names"]), "")
                if hit is not None:
                    keep(row["source_id"], hit, "")
        if not found:
            return []
        rows = self.ref.buildings[self.ref.buildings["source_id"].isin(list(found))]
        out = []
        for _, row in rows.iterrows():
            sim, name, addr = found[row["source_id"]]
            from .geo import to_utm
            gu = to_utm(row.geometry, epsg)
            out.append(Candidate(
                candidate_id="overture:%s" % row["source_id"], source="overture", source_id=str(row["source_id"]),
                dataset=row["dataset"] or "", geom=row.geometry, geom_utm=gu,
                names=list(row["names"]) + [name], addresses=[addr] if addr else [],
                height=row["height"], num_floors=row["num_floors"],
                evidence={"found_by_name": name, "name_similarity": round(sim, 3)}))
        out.sort(key=lambda c: (-c.evidence["name_similarity"], c.geom_utm.distance(pt)))
        return out[: self.name_max]

    # ------------------------------------------------------------------
    def _attach_places(self, cands, neighbours, lon, lat, radius, epsg):
        """Give each place to the building it lies in, else the nearest within 10 m."""
        pl = self.ref.query_bbox(self.ref.places, degrees_bbox(lon, lat, radius + 40.0))
        if pl is None or not len(pl) or not neighbours:
            return
        pu = _to_utm_series(pl, epsg)
        ids = [n[0] for n in neighbours]
        geoms = np.array([n[1] for n in neighbours], dtype=object)
        tree = shapely.STRtree(geoms)
        by_id = {}
        for c in cands:
            if c.source == "overture":
                by_id.setdefault(c.source_id, c)
        hist = [c for c in cands if c.source == "historic"]
        for (i, row), p in zip(pl.iterrows(), pu):
            inside = tree.query(p, predicate="within")
            if len(inside):
                target = ids[int(inside[0])]
            else:
                near = tree.query_nearest(p, max_distance=self.places_within)
                if not len(near):
                    target = None
                else:
                    target = ids[int(near[0])]
            names = list(row["names"])
            addr = row["address"]
            if target is not None and target in by_id:
                c = by_id[target]
                c.names.extend(names)
                if addr:
                    c.addresses.append(addr)
                c.evidence.setdefault("places", []).append(names[0] if names else row["category"])
            for h in hist:  # historic polygons: inside or within 10 m
                if h.geom_utm.distance(p) <= self.places_within:
                    h.names.extend(names)
                    if addr:
                        h.addresses.append(addr)

    def _roads(self, lon, lat, radius, epsg, street):
        seg = self.ref.query_bbox(self.ref.segments, degrees_bbox(lon, lat, radius + self.road_extra))
        out = []
        if seg is None or not len(seg):
            return out
        su = _to_utm_series(seg, epsg)
        for (i, row), g in zip(seg.iterrows(), su):
            names = list(row["names"])
            sim = max([self.text.street_similarity(street, n) for n in names] or [0.0]) if street else 0.0
            out.append({"geom": g, "names": names, "class": row["road_class"],
                        "matches_street": sim >= self.road_thr, "street_sim": sim})
        return out

    # ------------------------------------------------------------------
    def _features(self, c, record, pt, roads):
        f = record.get("fields", {})
        g = c.geom_utm
        d = float(g.distance(pt))
        inside = d == 0.0
        area = float(g.area)
        c.evidence.update({"distance_m": round(d, 2), "inside": inside, "area_m2": round(area, 1)})

        s_dist = 1.0 if inside else math.exp(-d / self.decay)

        s_name, matched = self.text.name_similarity(f.get("building_name", ""), c.names)
        if matched:
            c.evidence["matched_name"] = matched
        if self.customer_places and f.get("customer_name"):
            # The customer's own outlet in the building (bank branch, ATM, store) names it as well
            # as a building name does.
            s_cust, place = self.text.customer_similarity(f["customer_name"], c.names)
            if place:
                c.evidence["customer_place"] = place
            s_name = max(s_name, s_cust)

        locality = [f.get("area", ""), f.get("city", ""), f.get("pincode", "")]
        if c.addresses:
            s_addr = self.text.address_similarity(f.get("street", ""), locality, c.addresses,
                                                  requires_street=self.addr_requires_street,
                                                  street_threshold=self.road_thr)
        else:
            # No address data is not evidence against the building (cf. s_road's 0.5).
            s_addr = self.addr_missing

        s_road, road_ev = self._road_feature(g, pt, roads, inside)
        c.evidence.update(road_ev)

        s_plaus, pl_ev = self._plausibility(c, f, area)
        c.evidence.update(pl_ev)

        if c.source == "historic":
            s_source = float(self.trust.get("historic", 1.0))
        else:
            s_source = float(self.trust.get(c.dataset, self.trust.get("default", 0.6)))

        c.features = {"s_dist": s_dist, "s_name": s_name, "s_addr": s_addr,
                      "s_road": s_road, "s_plaus": s_plaus, "s_source": s_source}

    def _on_road(self, pt, roads):
        """Name of the road whose carriageway the survey point stands on (None if none)."""
        for r in roads:
            if self.crossing_classes and r["class"] not in self.crossing_classes:
                continue
            ignore = max(self.road_ignore, float(self.half_widths.get(r["class"], 0.0)) + 1.0)
            if r["geom"].distance(pt) <= ignore:
                return (r["names"][0] if r["names"] else r["class"]) or "road"
        return None

    def _road_feature(self, g, pt, roads, inside):
        ev = {}
        if not roads:
            return 0.5, ev
        crosses = None
        if not inside:
            near = nearest_points(pt, g)[1]
            length = pt.distance(near)
            if length > 0.6:
                # stop 0.5 m short of the building so a road along its wall is not a "crossing"
                t = (length - 0.5) / length
                end = (pt.x + (near.x - pt.x) * t, pt.y + (near.y - pt.y) * t)
                line = LineString([(pt.x, pt.y), end])
                for r in roads:
                    if self.crossing_classes and r["class"] not in self.crossing_classes:
                        continue
                    ignore = max(self.road_ignore, float(self.half_widths.get(r["class"], 0.0)) + 1.0)
                    if r["geom"].distance(pt) <= ignore:
                        continue  # survey point stands on this road's carriageway: side is unknown
                    if line.intersects(r["geom"]):
                        crosses = r
                        break
        if crosses is not None:
            ev["crosses_road"] = (crosses["names"][0] if crosses["names"] else crosses["class"])
            return 0.0, ev
        matched = [r for r in roads if r["matches_street"] and r["geom"].distance(g) <= self.road_within]
        if matched:
            ev["on_named_street"] = matched[0]["names"][0] if matched[0]["names"] else ""
            return 1.0, ev
        return 0.5, ev

    def _declared_height(self, f):
        h = _to_float(f.get("building_height_m"))
        if h and h > 0:
            return h
        fl = _to_float(f.get("floors"))
        return fl * self.floor_h if fl and fl > 0 else None

    def _plausibility(self, c, f, area):
        ev = {}
        cat = (f.get("building_category") or "").strip()
        band = self.bands.get(cat) or self.bands.get("default") or [15, 20000]
        lo, hi = float(band[0]), float(band[1])
        floors = _to_float(f.get("floors"))
        if floors and floors > 0 and self.min_area_per_floor:
            lo = max(lo, floors * self.min_area_per_floor)
        if area < lo:
            s = math.exp(-abs(math.log(area / lo)) / math.log(2)) if area > 0 else 0.0
            ev["area_plausible"] = False
        elif area > hi:
            s = math.exp(-abs(math.log(area / hi)) / math.log(2))
            ev["area_plausible"] = False
        else:
            s = 1.0
            ev["area_plausible"] = True
        if not ev["area_plausible"]:
            ev["area_band_m2"] = [round(lo, 1), round(hi, 1)]
        declared = self._declared_height(f)
        ref_h = None
        if c.height and c.height > 0:
            ref_h = float(c.height)
        elif c.num_floors and c.num_floors > 0:
            ref_h = float(c.num_floors) * self.floor_h
        elif self.ref.heights_path:
            ref_h = self.ref.median_height(c.geom)
        if declared and ref_h:
            consistent = abs(declared - ref_h) <= max(self.h_abs, self.h_rel * declared)
            ev["height_consistent"] = bool(consistent)
            ev["reference_height_m"] = round(ref_h, 1)
            if not consistent:
                s *= self.h_factor
        return s, ev


def _to_float(v):
    try:
        f = float(str(v).strip())
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None
