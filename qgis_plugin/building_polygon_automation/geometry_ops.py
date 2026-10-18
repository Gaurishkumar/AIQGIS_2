"""Geometry checks and corrected-coordinate strategies inside QGIS (QgsGeometry / GEOS).

Mirrors the worker's rules so an approved polygon gets exactly the coordinate
the worker would compute: metric work in the record's UTM zone, 6 decimals.
"""

import math

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY,
                       QgsProject)

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def utm_epsg(lon, lat):
    zone = int(math.floor((lon + 180.0) / 6.0)) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


def _tr(src, dst):
    return QgsCoordinateTransform(src, dst, QgsProject.instance())


def to_utm(geom, epsg):
    g = QgsGeometry(geom)
    g.transform(_tr(WGS84, QgsCoordinateReferenceSystem("EPSG:%d" % epsg)))
    return g


def to_wgs84(geom, epsg):
    g = QgsGeometry(geom)
    g.transform(_tr(QgsCoordinateReferenceSystem("EPSG:%d" % epsg), WGS84))
    return g


def single_polygon(geom):
    """Repair and reduce to one polygon part. Returns (geometry, n_parts)."""
    if geom is None or geom.isEmpty():
        return None, 0
    g = QgsGeometry(geom)
    if not g.isGeosValid():
        g = g.makeValid()
    parts = []
    if g.isMultipart():
        for p in g.asGeometryCollection():
            if p.type() == geom.type() and p.area() > 0:
                parts.append(p)
    elif g.area() > 0:
        parts = [g]
    if not parts:
        return None, 0
    best = max(parts, key=lambda p: p.area())
    total = sum(p.area() for p in parts)
    if best.area() / total >= 0.95:
        return best, 1
    return best, len(parts)


def validate_for_approval(geom_wgs84, epsg, cfg_validation):
    """Hard gates that must pass before a polygon can be approved/exported."""
    errors = []
    if geom_wgs84 is None or geom_wgs84.isEmpty():
        return None, ["No polygon"], None
    g, n_parts = single_polygon(geom_wgs84)
    if g is None:
        return None, ["Polygon is empty or invalid after repair"], None
    if n_parts > 1:
        errors.append("Polygon has %d separate parts; keep one building per record" % n_parts)
    if not g.isGeosValid():
        errors.append("Polygon is invalid (self-intersection)")
    area = to_utm(g, epsg).area()
    lo, hi = (cfg_validation or {}).get("area_m2", [15.0, 20000.0])
    if not float(lo) <= area <= float(hi):
        errors.append("Area %.1f m2 is outside %s-%s m2" % (area, lo, hi))
    return g, errors, area


def corrected_coordinate(geom_wgs84, lon, lat, epsg, cfg_cc):
    """Apply the configured strategy (centroid_if_inside | representative_point | snap_to_boundary)."""
    strategy = (cfg_cc or {}).get("strategy", "centroid_if_inside")
    decimals = int((cfg_cc or {}).get("decimals", 6))
    keep_inside = bool((cfg_cc or {}).get("keep_original_if_inside", False))
    g = to_utm(geom_wgs84, epsg)
    pt = to_utm(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)), epsg)
    used = strategy
    if keep_inside and g.intersects(pt):
        out = QgsGeometry(pt)
        used = "original_inside"
    elif strategy == "centroid_if_inside":
        out = g.centroid()
        if not g.contains(out):
            out = g.pointOnSurface()
            used = "centroid_if_inside:representative_point"
    elif strategy == "representative_point":
        out = g.pointOnSurface()
    elif strategy == "snap_to_boundary":
        boundary = QgsGeometry(g.constGet().boundary())
        out = boundary.nearestPoint(pt)
    else:
        raise ValueError("Unknown corrected-coordinate strategy %r" % strategy)
    shift = out.distance(pt)
    dist_poly = 0.0 if g.intersects(pt) else g.distance(pt)
    w = to_wgs84(out, epsg).asPoint()
    return {"corrected_lat": round(w.y(), decimals), "corrected_lon": round(w.x(), decimals),
            "strategy": used, "shift_m": round(shift, 2), "dist_to_polygon_m": round(dist_poly, 2)}


def overlap_share(geom_wgs84, others_wgs84, epsg):
    """Largest share of ``geom`` covered by any other polygon (metric)."""
    g = to_utm(geom_wgs84, epsg)
    a = g.area()
    best = 0.0
    for o in others_wgs84:
        if o is None or o.isEmpty():
            continue
        inter = g.intersection(to_utm(o, epsg))
        if inter and not inter.isEmpty() and a > 0:
            best = max(best, inter.area() / a)
    return best


def geojson_to_geometry(obj):
    """GeoJSON geometry dict -> QgsGeometry (via OGR, identical on QGIS 3 and 4)."""
    import json
    from osgeo import ogr
    if not obj:
        return None
    og = ogr.CreateGeometryFromJson(json.dumps(obj))
    if og is None:
        return None
    return QgsGeometry.fromWkt(og.ExportToWkt())
