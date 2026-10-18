"""Proxy benchmark for building identification on REAL reference data.

Without the client's historic polygons there is no ground truth, so this
benchmark builds one from OpenStreetMap buildings whose identity is known
(they carry a name). For each sampled target a survey record is simulated the
way surveyors actually record points:

  gps      building centroid + Gaussian noise (sigma 10 m, capped at 40 m)
  street   standing on the nearest road in front of the building (+3 m noise)
  edge     just outside the building wall (0-5 m)

Two evidence settings are reported:
  names    the target keeps its footprint name (as for named buildings)
  no_names every footprint name is hidden - the common case for ML footprints;
           only places (POIs), roads and geometry remain as evidence

Metrics: top-1 accuracy, share decided IDENTITY_CLEAR and precision of the
clear decisions (the spec's safety property: never be "clear" and wrong).
This is a proxy - OSM-named buildings are larger and better mapped than the
average survey target - and it does not replace calibration on client data.

  python -m bpa_worker.benchmark_identity --config ../config/config.yaml --n 300
"""

import argparse
import json
import logging
import math

import numpy as np
from shapely.geometry import Point
from shapely.ops import nearest_points

from bpa_common.config import load_config

from .candidates import CandidateGenerator, search_radius
from .geo import to_utm, to_wgs84, utm_epsg_for
from .reference import ReferenceData
from .scoring import Scorer
from .textmatch import TextMatcher

log = logging.getLogger(__name__)


def _sample_targets(ref, n, seed):
    b = ref.buildings
    named = b[(b["dataset"] == "OpenStreetMap") & (b["names"].map(len) > 0)]
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(named))
    out = []
    for i in idx:
        row = named.iloc[i]
        g = row.geometry
        if g.geom_type != "Polygon":
            continue
        epsg = utm_epsg_for(g.centroid.x, g.centroid.y)
        gu = to_utm(g, epsg)
        if not 60 <= gu.area <= 5000:
            continue
        if ref.covers(g.centroid.x, g.centroid.y) is False:
            continue
        out.append((row, gu, epsg))
        if len(out) >= n:
            break
    return out


def _nearest_named_road(ref, g_wgs, gu, epsg, max_m=60.0):
    from .geo import degrees_bbox
    c = g_wgs.centroid
    seg = ref.query_bbox(ref.segments, degrees_bbox(c.x, c.y, max_m + 40))
    best = None
    if seg is None or not len(seg):
        return None
    su = seg.geometry.to_crs(epsg)
    for (_, row), line in zip(seg.iterrows(), su):
        if not row["names"]:
            continue
        d = line.distance(gu)
        if d <= max_m and (best is None or d < best[0]):
            best = (d, row["names"][0], line)
    return best


def _point(model, gu, road, rng):
    c = gu.centroid
    if model == "gps":
        r = min(40.0, abs(rng.normal(0, 10.0)))
        a = rng.uniform(0, 2 * math.pi)
        return Point(c.x + r * math.cos(a), c.y + r * math.sin(a))
    if model == "street":
        if road is None:
            return None
        p = nearest_points(road[2], gu)[0]
        return Point(p.x + rng.normal(0, 3.0), p.y + rng.normal(0, 3.0))
    if model == "edge":
        ring = gu.exterior
        p = ring.interpolate(rng.uniform(0, ring.length))
        v = np.array([p.x - c.x, p.y - c.y])
        v = v / (np.linalg.norm(v) or 1.0)
        d = rng.uniform(0.0, 5.0)
        return Point(p.x + v[0] * d, p.y + v[1] * d)
    raise ValueError(model)


def run(cfg, n=300, seed=0, models=("gps", "street", "edge")):
    ref = ReferenceData(cfg)
    text = TextMatcher(cfg)
    gen = CandidateGenerator(cfg, ref, text)
    scorer = Scorer(cfg)
    radius = search_radius(cfg)
    targets = _sample_targets(ref, n, seed)
    rng = np.random.default_rng(seed + 1)
    saved_names = ref.buildings["names"].copy()
    results = {}
    for evidence in ("names", "no_names"):
        if evidence == "no_names":
            ref.buildings["names"] = [[] for _ in range(len(ref.buildings))]
        for model in models:
            # own seed per scenario: results stay comparable between code versions
            rng = np.random.default_rng([seed, models.index(model)])
            stats = {"n": 0, "top1": 0, "clear": 0, "clear_correct": 0, "no_candidate": 0, "target_missing": 0}
            for row, gu, epsg in targets:
                road = _nearest_named_road(ref, row.geometry, gu, epsg)
                p = _point(model, gu, road, rng)
                if p is None:
                    continue
                ll = to_wgs84(p, epsg)
                rec = {"survey_id": "BM", "latitude": ll.y, "longitude": ll.x,
                       "fields": {"building_name": row["names"][0], "street": road[1] if road else "",
                                  "area": "", "city": "", "pincode": "",
                                  "building_category": "Residential Building",
                                  "floors": str(int(row["num_floors"])) if row["num_floors"] == row["num_floors"] and row["num_floors"] else "",
                                  "building_height_m": ""}}
                cands, gctx = gen.generate(rec, epsg, radius)
                scorer.score(cands)
                decision, chosen, _, _ = scorer.decide(cands, gctx.get("on_road"))
                stats["n"] += 1
                if not cands:
                    stats["no_candidate"] += 1
                    continue
                if not any(c.source_id == row["source_id"] for c in cands):
                    stats["target_missing"] += 1
                ok = chosen is not None and chosen.source_id == row["source_id"]
                stats["top1"] += ok
                if decision == "IDENTITY_CLEAR":
                    stats["clear"] += 1
                    stats["clear_correct"] += ok
            n_ = max(1, stats["n"])
            results["%s/%s" % (evidence, model)] = {
                "records": stats["n"],
                "top1_accuracy": round(stats["top1"] / n_, 3),
                "clear_rate": round(stats["clear"] / n_, 3),
                "clear_precision": round(stats["clear_correct"] / stats["clear"], 3) if stats["clear"] else None,
                "target_outside_radius": round(stats["target_missing"] / n_, 3),
            }
            log.info("%s/%s: %s", evidence, model, results["%s/%s" % (evidence, model)])
    ref.buildings["names"] = saved_names
    return {"targets": len(targets), "radius_m": radius, "results": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(run(load_config(args.config), args.n, args.seed), indent=2))


if __name__ == "__main__":
    main()
