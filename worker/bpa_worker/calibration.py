"""Calibration and pilot metrics (Final MVP Specification, Sections 6 and 10).

* Search radius R = 95th percentile of survey-point-to-true-building distance,
  clamped to 25-100 m.
* Identity model: logistic regression on the candidate features plus the
  margin to the next candidate; scores normalised within each record;
  thresholds chosen on a held-out, spatially separated set (split by locality)
  to meet a target precision.
* Confidence model: logistic regression predicting "acceptable result"
  (correct building and IoU >= 0.8) from identity probability, SAM IoU,
  stability, footprint agreement and soft flags.
* evaluate_batch(): correct-building rate, IoU, boundary error and corrected
  coordinate error per path.
"""

import datetime as dt
import json
import logging
import os

import numpy as np

from bpa_common.config import get

from .candidates import FEATURES
from .confidence import SIGNALS
from .geo import iou, point_utm, to_utm, utm_epsg_for

log = logging.getLogger(__name__)

ID_FEATURES = list(FEATURES) + ["margin"]


def _read(path, layer=None):
    import pyogrio
    return pyogrio.read_dataframe(path, layer=layer) if layer else pyogrio.read_dataframe(path)


def load_ground_truth(path):
    """GeoPackage/any OGR file with survey_id + true polygon (+ optional corrected_lat/lon).

    A batch's review.gpkg works directly: its approved buildings and corrected
    points are the operators' own answers.
    """
    import pyogrio
    layers = [l[0] for l in pyogrio.list_layers(path)]
    if "buildings" in layers and "corrected_points" in layers:
        b = _read(path, "buildings")
        b = b[b["status"].isin(["APPROVED", "APPROVED_MANUAL"]) & b.geometry.notna()]
        cp = _read(path, "corrected_points")
        cpm = {str(r["survey_id"]): r for _, r in cp.iterrows()}
        out = {}
        for _, r in b.iterrows():
            c = cpm.get(str(r["survey_id"]))
            out[str(r["survey_id"])] = {"geometry": r.geometry,
                                        "corrected_lat": c["corrected_lat"] if c is not None else None,
                                        "corrected_lon": c["corrected_lon"] if c is not None else None}
        return out
    gt = _read(path)
    if gt.crs is not None and gt.crs.to_epsg() != 4326:
        gt = gt.to_crs(4326)
    cols = {c.lower(): c for c in gt.columns}
    if "survey_id" not in cols:
        raise ValueError("Ground truth needs a survey_id column")
    out = {}
    for _, r in gt.iterrows():
        sid = str(r[cols["survey_id"]])
        lat = r[cols["corrected_lat"]] if "corrected_lat" in cols else None
        lon = r[cols["corrected_lon"]] if "corrected_lon" in cols else None
        out[sid] = {"geometry": r.geometry, "corrected_lat": lat, "corrected_lon": lon}
    return out


def derive_radius(distances, clamp=(25.0, 100.0)):
    d = np.asarray([x for x in distances if x is not None and np.isfinite(x)], float)
    if d.size == 0:
        return None
    return float(np.clip(np.percentile(d, 95), clamp[0], clamp[1]))


def spatial_split(groups, train_frac=0.7, seed=0):
    """Split unique group labels (localities) into train/test sets."""
    uniq = sorted(set(groups))
    rng = np.random.default_rng(seed)
    rng.shuffle(uniq)
    n_train = max(1, int(round(len(uniq) * train_frac))) if len(uniq) > 1 else len(uniq)
    train = set(uniq[:n_train])
    return [g in train for g in groups]


def _fit(X, y):
    from sklearn.linear_model import LogisticRegression
    m = LogisticRegression(max_iter=2000, class_weight="balanced")
    m.fit(X, y)
    return m


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


def _record_probs(coef, intercept, rows):
    raw = _sigmoid(intercept + rows @ coef)
    return raw / raw.sum() if raw.sum() > 0 else raw


def _identity_rows(cands_df):
    """Per record: sorted candidates with features and margin."""
    by_rec = {}
    for _, c in cands_df.iterrows():
        by_rec.setdefault(str(c["survey_id"]), []).append(c)
    out = {}
    for sid, cs in by_rec.items():
        cs = sorted(cs, key=lambda c: -float(c["score"]))
        rows = []
        for i, c in enumerate(cs):
            nxt = float(cs[i + 1]["score"]) if i + 1 < len(cs) else 0.0
            margin = float(c["score"]) - nxt if i == 0 else float(c["score"]) - float(cs[0]["score"])
            rows.append([float(c[f]) for f in FEATURES] + [margin])
        out[sid] = (cs, np.asarray(rows, float))
    return out


def calibrate_from_ground_truth(cfg, batch_dir, gt_path, out_dir, target_precision=0.98, seed=0):
    work = os.path.join(batch_dir, "work.gpkg")
    recs = _read(work, "survey_record")
    cands = _read(work, "candidate")
    props = _read(work, "proposal")
    gt = load_ground_truth(gt_path)
    os.makedirs(out_dir, exist_ok=True)
    report = {"created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "batch_dir": batch_dir,
              "ground_truth": gt_path, "n_ground_truth": len(gt)}

    fields = {str(r["survey_id"]): json.loads(r["fields_json"] or "{}") for _, r in recs.iterrows()}
    pts = {str(r["survey_id"]): (float(r["longitude"]), float(r["latitude"])) for _, r in recs.iterrows()
           if r["latitude"] == r["latitude"]}

    # -- radius -------------------------------------------------------------
    dists = []
    for sid, g in gt.items():
        if sid in pts:
            epsg = utm_epsg_for(*pts[sid])
            dists.append(to_utm(g["geometry"], epsg).distance(point_utm(pts[sid][0], pts[sid][1], epsg)))
    clamp = get(cfg, "candidates.radius_clamp_m", [25, 100])
    radius = derive_radius(dists, clamp)
    report["radius_m"] = radius
    report["distance_p50_m"] = float(np.percentile(dists, 50)) if dists else None
    report["distance_p95_m"] = float(np.percentile(dists, 95)) if dists else None

    # -- identity model -------------------------------------------------------
    rows = _identity_rows(cands)
    X, y, groups, rec_ids = [], [], [], []
    labelled = {}
    for sid, (cs, feats) in rows.items():
        if sid not in gt:
            continue
        epsg = utm_epsg_for(*pts[sid])
        gtu = to_utm(gt[sid]["geometry"], epsg)
        ious = [iou(to_utm(c.geometry, epsg), gtu) for c in cs]
        best = int(np.argmax(ious)) if ious else -1
        lab = [1 if (i == best and ious[i] >= 0.5) else 0 for i in range(len(cs))]
        labelled[sid] = (feats, lab)
        for f, l in zip(feats, lab):
            X.append(f)
            y.append(l)
            groups.append(fields.get(sid, {}).get("area") or sid)
            rec_ids.append(sid)
    identity = None
    if len(set(y)) == 2 and sum(y) >= 5:
        rec_group = {sid: fields.get(sid, {}).get("area") or sid for sid in labelled}
        rec_list = sorted(labelled)
        is_train = dict(zip(rec_list, spatial_split([rec_group[s] for s in rec_list], 0.7, seed)))
        Xtr = np.asarray([x for x, s in zip(X, rec_ids) if is_train[s]])
        ytr = np.asarray([v for v, s in zip(y, rec_ids) if is_train[s]])
        if len(set(ytr)) < 2:
            Xtr, ytr = np.asarray(X), np.asarray(y)
        m = _fit(Xtr, ytr)
        coef, intercept = m.coef_[0], float(m.intercept_[0])
        # held-out evaluation and threshold choice
        test = [s for s in rec_list if not is_train[s]] or rec_list
        tops = []
        for s in test:
            feats, lab = labelled[s]
            p = _record_probs(coef, intercept, feats)
            order = np.argsort(-p)
            p1 = float(p[order[0]])
            p2 = float(p[order[1]]) if len(order) > 1 else 0.0
            tops.append((p1, p1 - p2, lab[int(order[0])] == 1, any(lab)))
        top1 = float(np.mean([t[2] for t in tops if t[3]])) if any(t[3] for t in tops) else None
        threshold, margin, coverage = 0.99, 0.0, 0.0
        for thr in np.linspace(0.3, 0.99, 70):
            sel = [t for t in tops if t[0] >= thr]
            if len(sel) < 5:
                continue
            prec = np.mean([t[2] for t in sel])
            if prec >= target_precision:
                threshold, coverage = float(thr), len(sel) / len(tops)
                break
        identity = {"features": ID_FEATURES, "coef": [float(c) for c in coef], "intercept": intercept,
                    "normalise": True, "threshold": round(threshold, 3), "margin": margin,
                    "heldout_top1_accuracy": top1, "heldout_clear_coverage": coverage,
                    "target_precision": target_precision, "n_train_records": int(sum(is_train.values())),
                    "n_test_records": len(test)}
    else:
        report["identity_skipped"] = "Need both classes and >= 5 correct candidates (got %d)" % sum(y)
    report["identity"] = identity

    # -- confidence model ------------------------------------------------------
    Xc, yc = [], []
    for _, p in props.iterrows():
        sid = str(p["survey_id"])
        if sid not in gt or p.geometry is None or sid not in pts:
            continue
        epsg = utm_epsg_for(*pts[sid])
        gtu = to_utm(gt[sid]["geometry"], epsg)
        chosen_ok = False
        cs = rows.get(sid, ([], None))[0]
        for c in cs:
            if c["candidate_id"] == p["chosen_candidate_id"]:
                chosen_ok = iou(to_utm(c.geometry, epsg), gtu) >= 0.5
        good = chosen_ok and iou(to_utm(p.geometry, epsg), gtu) >= 0.8
        flags = json.loads(p["flags"] or "[]")
        sig = {"identity_prob": p["identity_prob"], "sam_iou": p["sam_iou"] if p["sam_iou"] == p["sam_iou"] else None,
               "stability": p["stability"] if p["stability"] == p["stability"] else None,
               "footprint_agreement": p["footprint_agreement"] if p["footprint_agreement"] == p["footprint_agreement"] else None,
               "n_flags": len(flags)}
        from .confidence import signal_vector
        v = signal_vector(sig)
        Xc.append([v[k] for k in SIGNALS])
        yc.append(1 if good else 0)
    confidence = None
    if len(set(yc)) == 2 and min(sum(yc), len(yc) - sum(yc)) >= 5:
        mc = _fit(np.asarray(Xc), np.asarray(yc))
        confidence = {"features": list(SIGNALS), "coef": [float(c) for c in mc.coef_[0]],
                      "intercept": float(mc.intercept_[0]), "n_records": len(yc),
                      "acceptable_rate": float(np.mean(yc))}
    else:
        report["confidence_skipped"] = "Need >= 5 acceptable and >= 5 unacceptable results (got %d/%d)" % (
            sum(yc), len(yc) - sum(yc))
    report["confidence"] = confidence

    cal = {"radius_m": radius, "identity": identity, "confidence": confidence,
           "created_at": report["created_at"], "source_batch": batch_dir}
    path = os.path.join(out_dir, "calibration.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cal, fh, indent=2)
    report["calibration_file"] = path
    report["metrics"] = evaluate_batch(cfg, batch_dir, gt_path)
    with open(os.path.join(out_dir, "calibration_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    return report


def boundary_error(a, b, step=0.5):
    """Mean symmetric distance between polygon boundaries (metres)."""
    def sample(ring):
        n = max(8, int(ring.length / step))
        return [ring.interpolate(i / n, normalized=True) for i in range(n)]
    ea, eb = a.exterior, b.exterior
    da = [eb.distance(p) for p in sample(ea)]
    db = [ea.distance(p) for p in sample(eb)]
    return float((np.mean(da) + np.mean(db)) / 2.0)


def evaluate_batch(cfg, batch_dir, gt_path, review_gpkg=None):
    """Pilot metrics per path, against ground truth (proposals, or final buildings when given)."""
    work = os.path.join(batch_dir, "work.gpkg")
    recs = _read(work, "survey_record")
    props = _read(work, "proposal")
    gt = load_ground_truth(gt_path)
    pts = {str(r["survey_id"]): (float(r["longitude"]), float(r["latitude"])) for _, r in recs.iterrows()
           if r["latitude"] == r["latitude"]}
    source = {str(p["survey_id"]): p for _, p in props.iterrows()}
    if review_gpkg and os.path.exists(review_gpkg):
        b = _read(review_gpkg, "buildings")
        source = {str(r["survey_id"]): r for _, r in b.iterrows()}
    per_path = {}
    for sid, g in gt.items():
        p = source.get(sid)
        if p is None or p.geometry is None or sid not in pts:
            continue
        epsg = utm_epsg_for(*pts[sid])
        gtu = to_utm(g["geometry"], epsg)
        pu = to_utm(p.geometry, epsg)
        v = iou(pu, gtu)
        path = p.get("method") or "unknown"
        m = per_path.setdefault(path, {"n": 0, "correct_building": 0, "ious": [], "boundary_m": [], "coord_err_m": []})
        m["n"] += 1
        m["correct_building"] += 1 if v >= 0.5 else 0
        m["ious"].append(v)
        if pu.geom_type == "Polygon" and gtu.geom_type == "Polygon":
            m["boundary_m"].append(boundary_error(pu, gtu))
        if g.get("corrected_lat") is not None and p.get("corrected_lat") is not None:
            try:
                a = point_utm(float(g["corrected_lon"]), float(g["corrected_lat"]), epsg)
                c = point_utm(float(p["corrected_lon"]), float(p["corrected_lat"]), epsg)
                m["coord_err_m"].append(a.distance(c))
            except (TypeError, ValueError):
                pass
    out = {}
    all_ious, n_all, n_ok = [], 0, 0
    for path, m in per_path.items():
        n_all += m["n"]
        n_ok += m["correct_building"]
        all_ious += m["ious"]
        out[path] = {"n": m["n"], "correct_building_rate": m["correct_building"] / m["n"],
                     "iou_median": float(np.median(m["ious"])), "iou_mean": float(np.mean(m["ious"])),
                     "boundary_error_m_mean": float(np.mean(m["boundary_m"])) if m["boundary_m"] else None,
                     "coord_error_m_median": float(np.median(m["coord_err_m"])) if m["coord_err_m"] else None}
    out["_overall"] = {"n": n_all, "correct_building_rate": (n_ok / n_all) if n_all else None,
                       "iou_median": float(np.median(all_ious)) if all_ious else None}
    return out
