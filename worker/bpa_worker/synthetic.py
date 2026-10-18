"""Synthetic scene generator for testing the imagery + SAM path end to end.

Renders a georeferenced 0.3 m RGB GeoTIFF (in UTM) with textured roofs,
shadows, roads and vegetation, plus:
  * the true building polygons (ground truth for IoU),
  * "footprints" that mimic ML datasets (shifted, slightly wrong shape),
  * survey records placed near the buildings (some on the road).
Used by the test-suite and ``python -m bpa_worker.synthetic`` only.
"""

import json
import os

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely import affinity
from shapely.geometry import LineString, Point, box

from .geo import to_wgs84

EPSG = 32643          # UTM 43N (Mumbai region)
GSD = 0.3
ORIGIN = (290000.0, 2130000.0)   # upper-left corner (x, y)
SIZE_M = 240.0


def _rect(cx, cy, w, h, angle=0.0):
    return affinity.rotate(box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), angle, origin=(cx, cy))


def _l_shape(cx, cy, w, h, notch, angle=0.0):
    outer = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    cut = box(cx + w / 2 - notch, cy + h / 2 - notch, cx + w / 2, cy + h / 2)
    return affinity.rotate(outer.difference(cut), angle, origin=(cx, cy))


def scene_buildings():
    x0, y0 = ORIGIN
    cx = lambda dx: x0 + dx   # noqa: E731
    cy = lambda dy: y0 - dy   # noqa: E731
    b = [
        ("B01", "Sai Krupa Apartment", _rect(cx(60), cy(60), 24, 16, 8)),
        ("B02", "Shanti Niwas", _rect(cx(90), cy(58), 14, 18, 8)),       # close neighbour of B01
        ("B03", "Ganesh Heights", _l_shape(cx(160), cy(70), 30, 26, 11, -12)),
        ("B04", "Om Plaza", _rect(cx(60), cy(150), 36, 22, 0)),
        ("B05", "Laxmi Bhavan", _rect(cx(150), cy(160), 12, 12, 30)),
        ("B06", "Vrindavan Society", _rect(cx(190), cy(165), 20, 30, 30)),
    ]
    return b


def render_scene(path, seed=7):
    """Write the synthetic GeoTIFF and return scene metadata."""
    rng = np.random.default_rng(seed)
    n = int(SIZE_M / GSD)
    transform = from_origin(ORIGIN[0], ORIGIN[1], GSD, GSD)
    img = np.zeros((n, n, 3), np.float32)
    # ground: dusty soil with low-frequency variation
    yy, xx = np.mgrid[0:n, 0:n]
    img[:] = (118, 108, 92)
    img += (np.sin(xx / 37.0) * np.cos(yy / 53.0) * 8)[..., None]
    # vegetation blobs
    for _ in range(25):
        c = rng.uniform(0, n, 2)
        r = rng.uniform(10, 30)
        m = (xx - c[0]) ** 2 + (yy - c[1]) ** 2 < r * r
        img[m] = (58, 88, 50)
    x0, y0 = ORIGIN
    roads = [LineString([(x0, y0 - 105), (x0 + SIZE_M, y0 - 105)]),
             LineString([(x0 + 120, y0), (x0 + 120, y0 - SIZE_M)])]
    road_mask = rasterize([(r.buffer(6), 1) for r in roads], out_shape=(n, n), transform=transform).astype(bool)
    img[road_mask] = (70, 70, 72)
    buildings = scene_buildings()
    roof_colours = [(205, 72, 60), (190, 190, 182), (60, 110, 190), (215, 215, 205), (170, 90, 60), (120, 150, 200)]
    for i, (_, _, poly) in enumerate(buildings):
        shadow = affinity.translate(poly, 2.4, -2.4)
        sm = rasterize([(shadow, 1)], out_shape=(n, n), transform=transform).astype(bool)
        img[sm] *= 0.45
    for i, (_, _, poly) in enumerate(buildings):
        m = rasterize([(poly, 1)], out_shape=(n, n), transform=transform).astype(bool)
        col = np.array(roof_colours[i % len(roof_colours)], np.float32)
        img[m] = col
        # roof texture: ridge line + water tanks
        img[m] += rng.normal(0, 6, (m.sum(), 3))
        c = poly.representative_point()
        tank = rasterize([(c.buffer(1.2), 1)], out_shape=(n, n), transform=transform).astype(bool)
        img[tank] = (30, 30, 30)
    img += rng.normal(0, 4, img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=n, height=n, count=3, dtype="uint8",
                       crs=CRS.from_epsg(EPSG), transform=transform, compress="deflate",
                       tiled=True, blockxsize=256, blockysize=256) as dst:
        dst.write(np.transpose(img, (2, 0, 1)))
        dst.update_tags(TIFFTAG_DATETIME="2025:01:15 10:30:00")
    return {"buildings": buildings, "roads": roads}


def ml_footprint(poly, rng):
    """Mimic an ML footprint: 1.5-2.5 m offset, slight scale/rotation error, simplified."""
    dx, dy = rng.uniform(1.5, 2.5) * rng.choice([-1, 1]), rng.uniform(1.0, 2.0) * rng.choice([-1, 1])
    f = affinity.translate(poly, dx, dy)
    f = affinity.scale(f, rng.uniform(0.9, 1.08), rng.uniform(0.9, 1.08))
    f = affinity.rotate(f, rng.uniform(-4, 4))
    return f.minimum_rotated_rectangle if rng.random() < 0.5 else f


def write_reference(ref_dir, scene, seed=3):
    """Overture-like buildings + roads GeoParquet and a manifest for the scene."""
    import geopandas as gpd
    from .prepare import sha256_file
    rng = np.random.default_rng(seed)
    os.makedirs(ref_dir, exist_ok=True)
    rows = []
    for bid, name, poly in scene["buildings"]:
        fp = ml_footprint(poly, rng)
        rows.append({"id": "ov-" + bid, "names": None,
                     "sources": [{"property": "", "dataset": "Google Open Buildings"}],
                     "height": None, "num_floors": None, "class": None, "subtype": None,
                     "is_underground": False, "geometry": to_wgs84(fp, EPSG)})
    b = gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)
    b.to_parquet(os.path.join(ref_dir, "overture_buildings.parquet"), index=False)
    r = gpd.GeoDataFrame({"id": ["r1", "r2"], "subtype": ["road", "road"], "class": ["residential", "tertiary"],
                          "names": [{"primary": "Station Road"}, {"primary": "Tilak Road"}]},
                         geometry=[to_wgs84(g, EPSG) for g in scene["roads"]], crs=4326)
    r.to_parquet(os.path.join(ref_dir, "overture_segments.parquet"), index=False)
    p = gpd.GeoDataFrame({"id": ["p1"], "names": [{"primary": "Ganesh Heights"}],
                          "categories": [{"primary": "residential_building"}],
                          "addresses": [[{"freeform": "Tilak Road, Kalyan", "locality": "Kalyan", "postcode": "421301"}]]},
                         geometry=[to_wgs84(scene["buildings"][2][2].representative_point(), EPSG)], crs=4326)
    p.to_parquet(os.path.join(ref_dir, "overture_places.parquet"), index=False)
    manifest = {"overture_release": "synthetic", "files": {}}
    for key, fname in (("buildings", "overture_buildings.parquet"), ("segments", "overture_segments.parquet"),
                       ("places", "overture_places.parquet")):
        manifest["files"][key] = {"path": fname, "release": "synthetic", "sha256": sha256_file(os.path.join(ref_dir, fname))}
    with open(os.path.join(ref_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    return b


def survey_records(scene):
    """Survey points: inside, near the edge, and on the road in front."""
    x0, y0 = ORIGIN
    recs = []
    offsets = {"B01": (3, 2), "B02": (0, -12), "B03": (-4, 3), "B04": (10, 16), "B05": (0, 0), "B06": (-6, -9)}
    for bid, name, poly in scene["buildings"]:
        c = poly.centroid
        dx, dy = offsets[bid]
        p = Point(c.x + dx, c.y + dy)
        ll = to_wgs84(p, EPSG)
        recs.append({"survey_id": "SYN_" + bid, "row": len(recs) + 2, "latitude": ll.y, "longitude": ll.x,
                     "fields": {"survey_id": "SYN_" + bid, "building_name": name, "street": "Tilak Road",
                                "area": "Kalyan", "city": "Kalyan", "pincode": "421301",
                                "building_category": "Residential Building", "floors": "4",
                                "building_height_m": "12"},
                     "source": {}, "warnings": [], "reasons": [], "state": "IMPORTED"})
    return recs


def truth_by_survey(scene):
    return {"SYN_" + bid: poly for bid, _, poly in scene["buildings"]}
