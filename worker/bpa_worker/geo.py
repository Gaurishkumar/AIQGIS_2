"""CRS helpers (S2) and geometry utilities. All metric work runs in local UTM."""

import functools
import math

import numpy as np
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.validation import make_valid

WGS84 = 4326


@functools.lru_cache(maxsize=256)
def _utm_epsg_cached(lon_key, lat_key):
    import geopandas as gpd
    s = gpd.GeoSeries([Point(lon_key / 1000.0, lat_key / 1000.0)], crs=WGS84)
    return int(s.estimate_utm_crs().to_epsg())


def utm_epsg_for(lon, lat):
    """Local UTM EPSG for a point (GeoSeries.estimate_utm_crs, as the spec requires).

    Cached on a ~100 m grid; UTM zone borders are never closer than that to change.
    """
    return _utm_epsg_cached(int(round(lon * 1000)), int(round(lat * 1000)))


@functools.lru_cache(maxsize=256)
def transformer(src_epsg, dst_epsg):
    return Transformer.from_crs(CRS.from_epsg(src_epsg), CRS.from_epsg(dst_epsg), always_xy=True)


def reproject(geom, src_epsg, dst_epsg):
    if geom is None or src_epsg == dst_epsg:
        return geom
    tr = transformer(src_epsg, dst_epsg)

    def _xy(coords):
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, _xy)


def to_utm(geom, epsg):
    return reproject(geom, WGS84, epsg)


def to_wgs84(geom, epsg):
    return reproject(geom, epsg, WGS84)


def point_utm(lon, lat, epsg):
    x, y = transformer(WGS84, epsg).transform(lon, lat)
    return Point(x, y)


def lonlat_from_utm(x, y, epsg):
    lon, lat = transformer(epsg, WGS84).transform(x, y)
    return lon, lat


def degrees_bbox(lon, lat, radius_m):
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def iou(a, b):
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    try:
        inter = a.intersection(b).area
        union = a.union(b).area
    except shapely.errors.GEOSException:
        a, b = make_valid(a), make_valid(b)
        inter = a.intersection(b).area
        union = a.union(b).area
    return float(inter / union) if union > 0 else 0.0


def aligned_iou(a, b, max_shift_m=1.5, step_m=0.5):
    """Best IoU of ``a`` and ``b`` after translating ``b`` (offset-tolerant shape agreement).

    ``b`` is first moved onto ``a``'s centroid, then a small grid of shifts is
    searched. ML footprints are often displaced by 1-3 m, so plain IoU
    punishes a correctly traced building for the footprint's own offset.
    """
    from shapely import affinity
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    ca, cb = a.centroid, b.centroid
    base = affinity.translate(b, ca.x - cb.x, ca.y - cb.y)
    best = max(iou(a, b), iou(a, base))
    n = int(round(max_shift_m / step_m))
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            if i == 0 and j == 0:
                continue
            best = max(best, iou(a, affinity.translate(base, i * step_m, j * step_m)))
    return best


def largest_polygon(geom):
    """Return the largest Polygon part of any geometry (None if there is none)."""
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if isinstance(g, (Polygon, MultiPolygon))]
    polys = []
    for g in parts:
        polys.extend(g.geoms if isinstance(g, MultiPolygon) else [g])
    if not polys:
        return None
    return max(polys, key=lambda p: p.area)


def repair_single_polygon(geom, min_part_share=0.95):
    """make_valid and reduce to one Polygon.

    Returns (polygon_or_None, n_parts). A multi-part result keeps its largest
    part only when that part holds at least ``min_part_share`` of the area;
    otherwise the geometry is reported as multi-part so the hard gate fails.
    """
    if geom is None or geom.is_empty:
        return None, 0
    g = geom if geom.is_valid else make_valid(geom)
    if isinstance(g, Polygon):
        return g, 1
    polys = []
    for part in getattr(g, "geoms", []):
        if isinstance(part, Polygon):
            polys.append(part)
        elif isinstance(part, MultiPolygon):
            polys.extend(part.geoms)
    polys = [p for p in polys if p.area > 0]
    if not polys:
        return None, 0
    total = sum(p.area for p in polys)
    best = max(polys, key=lambda p: p.area)
    if best.area / total >= min_part_share:
        return best, 1
    return best, len(polys)


def fill_small_holes(poly, min_area):
    if not isinstance(poly, Polygon) or not poly.interiors:
        return poly
    keep = [r for r in poly.interiors if Polygon(r).area >= min_area]
    return Polygon(poly.exterior, keep)


def compactness(poly):
    """Polsby-Popper compactness 4*pi*A/P^2 (1 = circle, square = 0.785)."""
    if poly is None or poly.is_empty or poly.length == 0:
        return 0.0
    return float(4 * math.pi * poly.area / (poly.length ** 2))


def to_geojson_geometry(geom):
    return shapely.geometry.mapping(geom) if geom is not None else None


def from_geojson_geometry(obj):
    return shapely.geometry.shape(obj) if obj else None
