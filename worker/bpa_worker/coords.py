"""S10 - Corrected coordinate (configurable strategy, 6 decimal places)."""

from shapely.geometry import Point
from shapely.ops import nearest_points

from bpa_common import constants as C
from bpa_common.config import get

from .geo import lonlat_from_utm


def corrected_point_utm(poly_utm, point_utm, strategy, keep_original_if_inside=False):
    """Return (Point, strategy_used) in the polygon's UTM CRS."""
    if keep_original_if_inside and poly_utm.covers(point_utm):
        return Point(point_utm.x, point_utm.y), "original_inside"
    if strategy == C.STRATEGY_CENTROID_IF_INSIDE:
        c = poly_utm.centroid
        if poly_utm.contains(c):
            return c, C.STRATEGY_CENTROID_IF_INSIDE
        return poly_utm.representative_point(), C.STRATEGY_CENTROID_IF_INSIDE + ":representative_point"
    if strategy == C.STRATEGY_REPRESENTATIVE_POINT:
        return poly_utm.representative_point(), C.STRATEGY_REPRESENTATIVE_POINT
    if strategy == C.STRATEGY_SNAP_TO_BOUNDARY:
        return nearest_points(poly_utm.exterior, point_utm)[0], C.STRATEGY_SNAP_TO_BOUNDARY
    raise ValueError("Unknown corrected-coordinate strategy: %r" % strategy)


def corrected_coordinate(poly_utm, point_utm, epsg, cfg):
    """Corrected lat/lon plus the evidence the client remark needs."""
    cc = get(cfg, "corrected_coordinate", {}) or {}
    strategy = cc.get("strategy", C.STRATEGY_CENTROID_IF_INSIDE)
    decimals = int(cc.get("decimals", 6))
    p, used = corrected_point_utm(poly_utm, point_utm, strategy, bool(cc.get("keep_original_if_inside", False)))
    lon, lat = lonlat_from_utm(p.x, p.y, epsg)
    return {
        "corrected_lat": round(lat, decimals),
        "corrected_lon": round(lon, decimals),
        "strategy": used,
        "shift_m": round(float(p.distance(point_utm)), 2),
        "dist_to_polygon_m": round(float(poly_utm.distance(point_utm)), 2),
    }
