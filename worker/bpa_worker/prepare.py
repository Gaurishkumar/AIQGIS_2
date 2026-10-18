"""S0 - Reference data preparation (once per region).

1. Compute the area of interest from all survey points plus a margin.
2. Download Overture buildings, road segments, places and addresses as GeoParquet.
3. Write reference/manifest.json with source, release, bbox, date and SHA-256.

No network call is ever made per record; the worker only reads these files.
"""

import datetime as dt
import hashlib
import json
import logging
import math
import os
import time

log = logging.getLogger(__name__)

EARTH_R = 6371008.8

# Columns kept per Overture type (only those present in the release are read).
COLUMNS = {
    "building": ["id", "geometry", "names", "sources", "height", "num_floors", "class",
                 "subtype", "is_underground", "level"],
    "segment": ["id", "geometry", "subtype", "class", "names"],
    "place": ["id", "geometry", "names", "categories", "basic_category", "taxonomy",
              "addresses", "confidence"],
    "address": ["id", "geometry", "country", "postcode", "street", "number", "unit",
                "address_levels", "postal_city"],
}
FILE_KEYS = {"building": "buildings", "segment": "segments", "place": "places", "address": "addresses"}
LICENCES = {
    "building": "ODbL-1.0 (Overture buildings; includes OSM, Esri Community Maps, Google and Microsoft footprints)",
    "segment": "ODbL-1.0 (Overture transportation, from OpenStreetMap)",
    "place": "CDLA-Permissive-2.0 (Overture places) - check release notes",
    "address": "Varies by source - check Overture release notes",
}


def haversine_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def expand_bbox(bbox, margin_m):
    w, s, e, n = bbox
    lat_c = (s + n) / 2.0
    dlat = margin_m / 111320.0
    dlon = margin_m / (111320.0 * max(0.1, math.cos(math.radians(lat_c))))
    return (w - dlon, s - dlat, e + dlon, n + dlat)


def _bbox_of(points):
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (min(lons), min(lats), max(lons), max(lats))


def _overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def compute_boxes(points, margin_m=1000.0, mode="clusters", link_m=3000.0):
    """Return a list of (W, S, E, N) boxes covering the survey points.

    ``project`` mode returns one box (spec default). ``clusters`` mode groups
    points by single linkage within ``link_m`` and returns one expanded box per
    group, merging boxes that overlap, which avoids downloading empty country.
    """
    points = [(float(x), float(y)) for x, y in points]
    if not points:
        raise ValueError("No valid survey points to compute the reference area")
    if mode == "project":
        return [expand_bbox(_bbox_of(points), margin_m)]
    parent = list(range(len(points)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if haversine_m(points[i][0], points[i][1], points[j][0], points[j][1]) <= link_m:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(points)):
        groups.setdefault(find(i), []).append(points[i])
    boxes = [expand_bbox(_bbox_of(g), margin_m) for g in groups.values()]
    merged = True
    while merged:
        merged = False
        out = []
        for b in boxes:
            for k, o in enumerate(out):
                if _overlap(b, o):
                    out[k] = _union(b, o)
                    merged = True
                    break
            else:
                out.append(b)
        boxes = out
    return sorted(boxes)


def box_area_km2(b):
    w, s, e, n = b
    h = haversine_m(w, s, w, n)
    wd = haversine_m(w, (s + n) / 2, e, (s + n) / 2)
    return h * wd / 1e6


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _bbox_filter(pc, boxes):
    expr = None
    for w, s, e, n in boxes:
        f = ((pc.field("bbox", "xmin") < e) & (pc.field("bbox", "xmax") > w)
             & (pc.field("bbox", "ymin") < n) & (pc.field("bbox", "ymax") > s))
        expr = f if expr is None else (expr | f)
    return expr


_STAC_CACHE = {}


def _stac_files(release, overture_type, boxes):
    """S3 keys of the release's files for ``overture_type`` that intersect ``boxes``.

    Reads Overture's STAC-geoparquet index. The type is taken from the asset
    path (``theme=<theme>/type=<type>/``) because the index's ``collection``
    column is not populated in current releases. Returns None when the index
    cannot be read (caller then scans the whole release prefix).
    """
    import io
    import urllib.request
    import pyarrow.parquet as pq
    from overturemaps import core

    try:
        if release not in _STAC_CACHE:
            url = "https://stac.overturemaps.org/%s/collections.parquet" % release
            with urllib.request.urlopen(url, timeout=120) as resp:
                _STAC_CACHE[release] = pq.read_table(io.BytesIO(resp.read())).to_pylist()
        rows = _STAC_CACHE[release]
    except Exception as exc:  # network / format problem -> full scan fallback
        log.warning("STAC index unavailable (%s); scanning the full release prefix", exc)
        return None
    marker = "theme=%s/type=%s/" % (core.type_theme_map[overture_type], overture_type)
    out = []
    for r in rows:
        try:
            href = r["assets"]["aws"]["alternate"]["s3"]["href"]
        except (KeyError, TypeError):
            continue
        if marker not in href:
            continue
        bb = r.get("bbox") or {}
        fb = (bb.get("xmin"), bb.get("ymin"), bb.get("xmax"), bb.get("ymax"))
        if None in fb or any(_overlap(fb, b) for b in boxes):
            out.append(href[len("s3://"):])
    return out


def download_type(overture_type, boxes, out_path, release, append=False):
    """Download one Overture type restricted to ``boxes`` and write GeoParquet.

    With ``append`` the new features are merged into the existing file
    (duplicates by Overture id are dropped).
    """
    import geopandas as gpd
    import pyarrow.compute as pc
    import pyarrow.dataset as pds
    import pyarrow.fs as pafs
    import shapely
    from overturemaps import core

    t0 = time.time()
    files = _stac_files(release, overture_type, boxes)
    s3 = pafs.S3FileSystem(anonymous=True, region="us-west-2", connect_timeout=30, request_timeout=120)
    if files is not None and not files:
        log.warning("Overture index lists no %s files for the survey area", overture_type)
        table = None
    else:
        source = files if files is not None else core._dataset_path(overture_type, release)
        log.info("%s: reading %s file(s)", overture_type, len(files) if files is not None else "all")
        dataset = pds.dataset(source, filesystem=s3, format="parquet")
        names = set(dataset.schema.names)
        cols = [c for c in COLUMNS[overture_type] if c in names]
        flt = _bbox_filter(pc, boxes)
        if overture_type == "segment" and "subtype" in names:
            flt = flt & (pc.field("subtype") == "road")
        table = dataset.to_table(filter=flt, columns=cols, use_threads=True)
    if table is None or table.num_rows == 0:
        gdf = gpd.GeoDataFrame({"id": []}, geometry=gpd.GeoSeries([], crs=4326), crs=4326)
    else:
        df = table.drop(["geometry"]).to_pandas() if table.num_columns > 1 else None
        geom = shapely.from_wkb(table.column("geometry").to_numpy(zero_copy_only=False))
        gdf = gpd.GeoDataFrame(df, geometry=geom, crs=4326)
    if append and os.path.exists(out_path):
        import pandas as pd
        old = gpd.read_parquet(out_path)
        if len(old) and len(gdf):
            gdf = gpd.GeoDataFrame(pd.concat([old, gdf], ignore_index=True), geometry="geometry", crs=4326)
            gdf = gdf.drop_duplicates(subset="id", keep="last").reset_index(drop=True)
        elif len(old):
            gdf = old
    tmp = out_path + ".tmp"
    gdf.to_parquet(tmp, index=False)
    os.replace(tmp, out_path)
    log.info("%s: %d features in %.1fs -> %s", overture_type, len(gdf), time.time() - t0, out_path)
    return len(gdf)


def prepare_reference(cfg, points, ref_dir, types=None, release=None, margin_m=None,
                      mode=None, link_m=None, refresh=False):
    """Run S0 for ``points`` [(lon, lat), ...] and write the manifest.

    Incremental by default: only areas not yet covered are downloaded and the
    new features are merged into the existing files (the release stays pinned).
    ``refresh`` re-downloads every covered area.
    """
    from overturemaps import core
    from bpa_common.config import get

    prep = get(cfg, "reference.prepare", {}) or {}
    margin_m = float(margin_m if margin_m is not None else prep.get("margin_m", 1000))
    mode = mode or prep.get("bbox_mode", "clusters")
    link_m = float(link_m if link_m is not None else prep.get("cluster_link_m", 3000))
    types = types or prep.get("types", ["building", "segment", "place", "address"])
    need_m = float(prep.get("coverage_margin_m", 300))
    files_cfg = get(cfg, "reference.files", {}) or {}
    os.makedirs(ref_dir, exist_ok=True)

    manifest_path = os.path.join(ref_dir, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    existing = [tuple(b) for b in manifest.get("boxes") or []]
    pinned = manifest.get("overture_release")

    # Incremental: reference data is shared by every batch in the project, so a
    # new workbook only downloads the areas its survey points are not yet covered by.
    if release is None and pinned and not refresh:
        available, latest = core.get_available_releases()
        if pinned in available:
            release = pinned
        else:
            log.warning("Pinned Overture release %s is no longer published; refreshing all areas with %s",
                        pinned, latest)
            release, refresh = latest, True
    release = release or core.get_latest_release()
    if pinned and release != pinned and not refresh:
        log.warning("Release %s differs from the pinned %s; refreshing all areas", release, pinned)
        refresh = True

    if refresh:
        todo_points = list(points) + [((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) for b in existing]
        new_boxes = compute_boxes(points, margin_m, mode, link_m) + existing
        existing = []
    else:
        todo_points = [p for p in points if not point_covered(existing, p[0], p[1], need_m)]
        new_boxes = compute_boxes(todo_points, margin_m, mode, link_m) if todo_points else []
    if not new_boxes:
        log.info("All %d survey points are already covered by the reference data", len(points))
        manifest["last_checked"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        _write_manifest(manifest_path, manifest)
        return manifest
    log.info("Overture release %s; %d new box(es), %.1f km2 (%d uncovered survey points)", release,
             len(new_boxes), sum(box_area_km2(b) for b in new_boxes), len(todo_points))

    manifest.update({
        "created_at": manifest.get("created_at") or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "overture_release": release,
        "bbox_mode": mode,
        "margin_m": margin_m,
        "boxes": [list(b) for b in existing + new_boxes],
    })
    manifest.setdefault("files", {})
    for t in types:
        key = FILE_KEYS[t]
        fname = files_cfg.get(key, "overture_%s.parquet" % key)
        path = os.path.join(ref_dir, fname)
        append = bool(existing) and os.path.exists(path)
        rows = download_type(t, new_boxes, path, release, append=append)
        manifest["files"][key] = {
            "path": fname,
            "source": "Overture Maps Foundation",
            "overture_type": t,
            "release": release,
            "licence": LICENCES[t],
            "bbox": [list(b) for b in existing + new_boxes],
            "download_date": dt.date.today().isoformat(),
            "rows": rows,
            "sha256": sha256_file(path),
        }
    _write_manifest(manifest_path, manifest)
    return manifest


def point_covered(boxes, lon, lat, need_m=300.0):
    """True when a survey point plus ``need_m`` lies inside one reference box."""
    w, s, e, n = expand_bbox((lon, lat, lon, lat), need_m)
    return any(b[0] <= w and b[1] <= s and b[2] >= e and b[3] >= n for b in boxes)


def register_file(ref_dir, key, path, source, licence, release=""):
    """Add a locally supplied reference file (historic.gpkg, heights.tif) to the manifest."""
    manifest_path = os.path.join(ref_dir, "manifest.json")
    manifest = {"files": {}}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    manifest.setdefault("files", {})
    manifest["files"][key] = {
        "path": os.path.relpath(path, ref_dir),
        "source": source,
        "licence": licence,
        "release": release,
        "download_date": dt.date.today().isoformat(),
        "sha256": sha256_file(path),
    }
    _write_manifest(manifest_path, manifest)
    return manifest


def _write_manifest(path, manifest):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, path)
