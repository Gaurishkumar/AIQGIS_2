"""Offline reference data: loading, manifest verification and spatial queries.

Loaded once at worker start-up; per record only in-memory spatial index
queries run (GeoDataFrame.sindex). No network access happens here.
"""

import hashlib
import json
import logging
import os

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

from bpa_common.config import get, resolve_path

from .prepare import sha256_file

log = logging.getLogger(__name__)


def verify_manifest(ref_dir):
    """Check every file listed in manifest.json against its SHA-256."""
    path = os.path.join(ref_dir, "manifest.json")
    if not os.path.exists(path):
        return {"ok": False, "manifest_hash": None, "files": {}, "errors": ["manifest.json not found"]}
    with open(path, "rb") as fh:
        raw = fh.read()
    manifest = json.loads(raw.decode("utf-8"))
    out = {"ok": True, "manifest_hash": hashlib.sha256(raw).hexdigest(),
           "overture_release": manifest.get("overture_release"), "files": {}, "errors": []}
    for key, info in (manifest.get("files") or {}).items():
        fpath = os.path.join(ref_dir, info.get("path", ""))
        if not os.path.exists(fpath):
            out["files"][key] = {"ok": False, "reason": "missing"}
            out["errors"].append("%s missing: %s" % (key, fpath))
            out["ok"] = False
            continue
        digest = sha256_file(fpath)
        ok = digest == info.get("sha256")
        out["files"][key] = {"ok": ok, "rows": info.get("rows"), "release": info.get("release")}
        if not ok:
            out["errors"].append("%s SHA-256 mismatch" % key)
            out["ok"] = False
    return out


# ---------------------------------------------------------------------------
# Overture attribute flattening
# ---------------------------------------------------------------------------

def _as_list(v):
    if v is None:
        return []
    if isinstance(v, float) and np.isnan(v):
        return []
    if isinstance(v, np.ndarray):
        return list(v)
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def _names(v):
    """All name strings of an Overture ``names`` struct."""
    if not isinstance(v, dict):
        return []
    out = []
    if v.get("primary"):
        out.append(str(v["primary"]))
    common = v.get("common")
    if isinstance(common, dict):
        out.extend(str(x) for x in common.values() if x)
    elif common is not None:
        for item in _as_list(common):
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[1]:
                out.append(str(item[1]))
            elif isinstance(item, dict) and item.get("value"):
                out.append(str(item["value"]))
    for rule in _as_list(v.get("rules")):
        if isinstance(rule, dict) and rule.get("value"):
            out.append(str(rule["value"]))
    seen, uniq = set(), []
    for n in out:
        k = n.strip().lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(n.strip())
    return uniq


def _geometry_dataset(v):
    """Dataset that supplied the geometry (source entry with an empty property)."""
    entries = [e for e in _as_list(v) if isinstance(e, dict)]
    for e in entries:
        if not e.get("property"):
            return e.get("dataset") or ""
    return entries[0].get("dataset", "") if entries else ""


def _place_address(v):
    parts = []
    for a in _as_list(v):
        if isinstance(a, dict):
            parts.extend(str(a.get(k)) for k in ("freeform", "locality", "postcode") if a.get(k))
    return " ".join(parts)


def _address_text(row):
    parts = [row.get(k) for k in ("number", "street", "unit", "postcode", "postal_city")]
    for lvl in _as_list(row.get("address_levels")):
        if isinstance(lvl, dict) and lvl.get("value"):
            parts.append(lvl["value"])
    return " ".join(str(p) for p in parts if p and not (isinstance(p, float) and np.isnan(p)))


def _num(v):
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ReferenceData
# ---------------------------------------------------------------------------

class ReferenceData(object):
    """In-memory, spatially indexed reference layers (all EPSG:4326)."""

    def __init__(self, cfg, ref_dir=None, verify=None):
        self.cfg = cfg
        self.ref_dir = ref_dir or resolve_path(cfg, get(cfg, "reference.dir", "data/reference"))
        verify = get(cfg, "reference.verify_manifest", True) if verify is None else verify
        self.verification = verify_manifest(self.ref_dir) if verify else {
            "ok": None, "manifest_hash": None, "files": {}, "errors": []}
        if verify and not self.verification["ok"]:
            log.warning("Reference manifest problems: %s", self.verification["errors"])
        self.manifest_hash = self.verification.get("manifest_hash")
        self.overture_release = self.verification.get("overture_release")
        self.boxes = []
        mpath = os.path.join(self.ref_dir, "manifest.json")
        if os.path.exists(mpath):
            with open(mpath, "r", encoding="utf-8") as fh:
                self.boxes = [tuple(b) for b in (json.load(fh).get("boxes") or [])]
        self.coverage_margin_m = float(get(cfg, "reference.prepare.coverage_margin_m", 300))
        files = get(cfg, "reference.files", {}) or {}

        self.buildings = self._load_parquet(files.get("buildings"), self._prep_buildings)
        self.segments = self._load_parquet(files.get("segments"), self._prep_segments)
        self.places = self._load_parquet(files.get("places"), self._prep_places)
        self.addresses = self._load_parquet(files.get("addresses"), self._prep_addresses)
        self.historic = self._load_historic(files.get("historic"))
        hp = os.path.join(self.ref_dir, files.get("heights") or "")
        self.heights_path = hp if files.get("heights") and os.path.isfile(hp) else None
        for layer in (self.buildings, self.segments, self.places, self.addresses, self.historic):
            if layer is not None and len(layer):
                _ = layer.sindex  # build the STRtree now, not on the first request
        log.info("Reference loaded: %s", self.counts())

    # -- loading -------------------------------------------------------------
    def _load_parquet(self, fname, prep):
        if not fname:
            return None
        path = os.path.join(self.ref_dir, fname)
        if not os.path.isfile(path):
            log.warning("Reference file not found: %s", path)
            return None
        gdf = gpd.read_parquet(path)
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(4326)
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)
        return prep(gdf)

    @staticmethod
    def _prep_buildings(gdf):
        gdf = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        if "is_underground" in gdf:
            gdf = gdf[gdf["is_underground"].fillna(False).astype(bool) == False]  # noqa: E712
        names = gdf["names"].map(_names) if "names" in gdf else pd.Series([[]] * len(gdf), index=gdf.index)
        out = gpd.GeoDataFrame({
            "source_id": gdf["id"].astype(str),
            "names": names,
            "dataset": gdf["sources"].map(_geometry_dataset) if "sources" in gdf else "",
            "height": gdf["height"].map(_num) if "height" in gdf else None,
            "num_floors": gdf["num_floors"].map(_num) if "num_floors" in gdf else None,
            "b_class": gdf["class"].astype(object).where(gdf["class"].notna(), None) if "class" in gdf else None,
        }, geometry=gdf.geometry.values, crs=4326)
        return out.reset_index(drop=True)

    @staticmethod
    def _prep_segments(gdf):
        if "subtype" in gdf:
            gdf = gdf[gdf["subtype"] == "road"]
        gdf = gdf[gdf.geom_type.isin(["LineString", "MultiLineString"])]
        names = gdf["names"].map(_names) if "names" in gdf else pd.Series([[]] * len(gdf), index=gdf.index)
        out = gpd.GeoDataFrame({
            "source_id": gdf["id"].astype(str),
            "names": names,
            "road_class": gdf["class"].astype(object).where(gdf["class"].notna(), None) if "class" in gdf else None,
        }, geometry=gdf.geometry.values, crs=4326)
        return out.reset_index(drop=True)

    @staticmethod
    def _prep_places(gdf):
        names = gdf["names"].map(_names) if "names" in gdf else pd.Series([[]] * len(gdf), index=gdf.index)

        def cat(row):
            for col in ("basic_category",):
                if col in row and isinstance(row[col], str):
                    return row[col]
            c = row.get("categories")
            if isinstance(c, dict) and c.get("primary"):
                return c["primary"]
            t = row.get("taxonomy")
            if isinstance(t, dict) and t.get("primary"):
                return t["primary"]
            return ""

        cats = gdf.apply(cat, axis=1) if len(gdf) else pd.Series([], dtype=object)
        out = gpd.GeoDataFrame({
            "source_id": gdf["id"].astype(str),
            "names": names,
            "category": cats,
            "address": gdf["addresses"].map(_place_address) if "addresses" in gdf else "",
        }, geometry=gdf.geometry.values, crs=4326)
        return out.reset_index(drop=True)

    @staticmethod
    def _prep_addresses(gdf):
        texts = gdf.apply(lambda r: _address_text(r), axis=1) if len(gdf) else pd.Series([], dtype=object)
        out = gpd.GeoDataFrame({
            "source_id": gdf["id"].astype(str),
            "street": gdf["street"] if "street" in gdf else None,
            "address": texts,
        }, geometry=gdf.geometry.values, crs=4326)
        return out.reset_index(drop=True)

    def _load_historic(self, fname):
        if not fname:
            return None
        path = os.path.join(self.ref_dir, fname)
        if not os.path.isfile(path):
            return None
        gdf = gpd.read_file(path)
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(4326)
        gdf = gdf[gdf.geometry.notna() & gdf.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        cols = {c.lower(): c for c in gdf.columns}

        def col(name, default=""):
            c = cols.get(name)
            return gdf[c].fillna("").astype(str) if c else pd.Series([default] * len(gdf), index=gdf.index)

        pid = col("polygon_id")
        pid = pid.where(pid != "", pd.Series(["H%06d" % i for i in range(len(gdf))], index=gdf.index))
        out = gpd.GeoDataFrame({
            "source_id": pid,
            "survey_id": col("survey_id"),
            "names": col("building_name").map(lambda s: [s] if s else []),
            "address": col("address"),
        }, geometry=gdf.geometry.values, crs=4326)
        log.info("Historic polygons loaded: %d", len(out))
        return out.reset_index(drop=True)

    # -- queries ----------------------------------------------------------------
    @staticmethod
    def query_bbox(gdf, bbox):
        if gdf is None or len(gdf) == 0:
            return gdf.iloc[0:0] if gdf is not None else None
        idx = gdf.sindex.query(box(*bbox), predicate="intersects")
        return gdf.iloc[np.sort(idx)]

    def covers(self, lon, lat):
        """Is the survey point inside the downloaded reference area?

        Returns None when the manifest records no areas (e.g. a hand-built
        reference folder), in which case no coverage check is possible.
        """
        if not self.boxes:
            return None
        from .prepare import point_covered
        return point_covered(self.boxes, lon, lat, self.coverage_margin_m)

    def coverage(self, points):
        """Split [(lon, lat), ...] into covered / uncovered counts."""
        flags = [self.covers(lon, lat) for lon, lat in points]
        return {"total": len(points), "covered": sum(1 for f in flags if f),
                "uncovered": sum(1 for f in flags if f is False),
                "unknown": sum(1 for f in flags if f is None), "areas": len(self.boxes)}

    def counts(self):
        def n(g):
            return 0 if g is None else int(len(g))
        return {"buildings": n(self.buildings), "segments": n(self.segments), "places": n(self.places),
                "addresses": n(self.addresses), "historic": n(self.historic),
                "heights": bool(self.heights_path)}

    def median_height(self, poly_wgs84):
        """Median 2.5D height inside a polygon (None without the optional raster)."""
        if not self.heights_path:
            return None
        import rasterio
        from rasterio.mask import mask as rmask
        from .geo import reproject
        try:
            with rasterio.open(self.heights_path) as src:
                geom = poly_wgs84
                epsg = src.crs.to_epsg() if src.crs else 4326
                if epsg and epsg != 4326:
                    geom = reproject(poly_wgs84, 4326, epsg)
                data, _ = rmask(src, [geom.__geo_interface__], crop=True, filled=False)
                vals = data[0].compressed()
                vals = vals[np.isfinite(vals) & (vals > 0)]
                return float(np.median(vals)) if vals.size else None
        except Exception as exc:  # outside coverage etc.
            log.debug("height lookup failed: %s", exc)
            return None

    def status(self):
        return {"dir": self.ref_dir, "manifest_ok": self.verification.get("ok"),
                "manifest_hash": self.manifest_hash, "overture_release": self.overture_release,
                "errors": self.verification.get("errors", []), "counts": self.counts()}
