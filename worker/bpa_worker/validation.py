"""S9 - Validation gates.

A failed hard gate sends the record to MANUAL. Soft flags lower confidence and
are shown to the reviewer.
"""

from bpa_common import constants as C
from bpa_common.config import get

from .geo import iou, repair_single_polygon


def area_band(cfg, fields):
    bands = get(cfg, "plausibility.category_area_m2", {}) or {}
    cat = (fields.get("building_category") or "").strip()
    lo, hi = bands.get(cat) or bands.get("default") or [15, 20000]
    lo, hi = float(lo), float(hi)
    try:
        floors = float(fields.get("floors") or 0)
    except ValueError:
        floors = 0
    per_floor = float(get(cfg, "plausibility.min_area_per_floor_m2", 0) or 0)
    if floors > 0 and per_floor:
        lo = max(lo, floors * per_floor)
    return lo, hi


def sliver_loss(poly, width_m):
    """Share of area lost by an opening of ``width_m``: high = thin parts/spikes."""
    if poly is None or poly.area <= 0:
        return 0.0
    opened = poly.buffer(-width_m / 2.0, join_style=2).buffer(width_m / 2.0, join_style=2)
    return max(0.0, 1.0 - opened.area / poly.area)


def longest_thin_part(poly, width_m):
    """Length (m) of the longest part thinner than ``width_m`` (a spike can be long but tiny in area)."""
    if poly is None or poly.area <= 0:
        return 0.0
    opened = poly.buffer(-width_m / 2.0, join_style=2).buffer(width_m / 2.0, join_style=2)
    removed = poly.difference(opened)
    best = 0.0
    for part in getattr(removed, "geoms", [removed]):
        if part.is_empty or part.area < 0.05:
            continue
        rect = part.minimum_rotated_rectangle
        xs = list(rect.exterior.coords)
        sides = [((xs[i][0] - xs[i + 1][0]) ** 2 + (xs[i][1] - xs[i + 1][1]) ** 2) ** 0.5 for i in range(len(xs) - 1)]
        best = max(best, max(sides) if sides else 0.0)
    return best


def validate(poly_utm, ctx, cfg):
    """Validate one proposal polygon.

    ctx keys: point_utm, radius_m, fields, footprint_utm (chosen footprint or
    None), is_segmented (bool), touches_edge (bool), approved_utm (list of
    polygons that are already approved), candidate_evidence (dict).

    Returns (polygon_after_repair, hard_failures, soft_flags, details).
    """
    v = get(cfg, "validation", {}) or {}
    hard, soft, details = [], [], {}
    poly, n_parts = repair_single_polygon(poly_utm)
    if poly is None or poly.is_empty or not poly.is_valid:
        return None, [C.GATE_INVALID_GEOMETRY], [], {"reason": "invalid or empty after repair"}
    if n_parts > 1:
        hard.append(C.GATE_MULTIPART)
    area = float(poly.area)
    details["area_m2"] = round(area, 1)
    amin, amax = v.get("area_m2", [15.0, 20000.0])
    if not float(amin) <= area <= float(amax):
        hard.append(C.GATE_AREA)
    if ctx.get("is_segmented") and ctx.get("touches_edge"):
        hard.append(C.GATE_CROP_EDGE)

    pt = ctx["point_utm"]
    radius = float(ctx["radius_m"])
    dist = float(poly.distance(pt))
    details["distance_m"] = round(dist, 2)
    if dist > float(ctx.get("crs_limit_m") or 2.0 * radius):
        hard.append(C.GATE_CRS)            # far outside the search area: CRS/offset error
    elif dist > radius:
        soft.append(C.FLAG_FAR_FROM_SURVEY)

    fp = ctx.get("footprint_utm")
    if ctx.get("is_segmented") and fp is not None:
        agreement = iou(poly, fp)
        details["footprint_agreement"] = round(agreement, 3)
        if agreement < float(v.get("footprint_agreement_iou", 0.6)):
            soft.append(C.FLAG_LOW_FOOTPRINT_AGREEMENT)

    max_overlap = 0.0
    for other in ctx.get("approved_utm") or []:
        if other is None or other.is_empty:
            continue
        try:
            ov = poly.intersection(other).area / area if area > 0 else 0.0
        except Exception:
            continue
        max_overlap = max(max_overlap, ov)
    if max_overlap > float(v.get("approved_overlap_ratio", 0.10)):
        soft.append(C.FLAG_OVERLAPS_APPROVED)
        details["approved_overlap"] = round(max_overlap, 3)

    lo, hi = area_band(cfg, ctx.get("fields", {}))
    if not lo <= area <= hi:
        soft.append(C.FLAG_IMPLAUSIBLE_AREA)
        details["plausible_area_m2"] = [lo, hi]
    ev = ctx.get("candidate_evidence") or {}
    if ev.get("height_consistent") is False:
        soft.append(C.FLAG_HEIGHT_MISMATCH)

    width = float(v.get("sliver_width_m", 1.0))
    loss = sliver_loss(poly, width)
    spike = longest_thin_part(poly, width)
    details["sliver_loss"] = round(loss, 3)
    details["longest_thin_part_m"] = round(spike, 2)
    if loss > float(v.get("sliver_loss_ratio", 0.15)) or spike > float(v.get("sliver_min_length_m", 4.0)):
        soft.append(C.FLAG_SLIVER)
    return poly, hard, soft, details
