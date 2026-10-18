"""Learning from operator-approved polygons (the data flywheel).

Approved buildings from a batch's review.gpkg are added to the trusted
reference (reference/historic.gpkg). Later surveys of the same building then
match it as a "historic" candidate: highest source trust, and IDENTITY_CLEAR
when the survey point lies in it or within 5 m. Every learned polygon keeps
its provenance (batch, reviewer, approval time) and is replaced, not
duplicated, when the same polygon ID is learned again.

The review.gpkg is only read here; the plugin remains its single writer.
"""

import datetime as dt
import logging
import os

import geopandas as gpd
import pandas as pd
import pyogrio

from bpa_common import constants as C
from bpa_common.config import get, resolve_path

from .prepare import register_file

log = logging.getLogger(__name__)

FIELDS = ["polygon_id", "survey_id", "building_name", "address", "reviewer", "reviewed_at", "source_batch",
          "learned_at"]


def approved_from_review(review_gpkg):
    """Approved building polygons of one review.gpkg with their names and addresses."""
    if not os.path.isfile(review_gpkg):
        raise FileNotFoundError("review.gpkg not found: %s" % review_gpkg)
    b = pyogrio.read_dataframe(review_gpkg, layer="buildings")
    b = b[b["status"].isin(list(C.EXPORTABLE_STATUSES)) & b.geometry.notna()]
    if not len(b):
        return gpd.GeoDataFrame(columns=FIELDS, geometry=[], crs=4326)
    s = pyogrio.read_dataframe(review_gpkg, layer="survey_points", read_geometry=False)
    names = dict(zip(s["survey_id"], s["building_name"]))
    addrs = dict(zip(s["survey_id"], s["address"]))
    batch = os.path.basename(os.path.dirname(os.path.abspath(review_gpkg)))
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    out = gpd.GeoDataFrame({
        "polygon_id": b["polygon_id"].fillna("").astype(str).values,
        "survey_id": b["survey_id"].astype(str).values,
        "building_name": [names.get(x, "") or "" for x in b["survey_id"]],
        "address": [addrs.get(x, "") or "" for x in b["survey_id"]],
        "reviewer": b["reviewer"].fillna("").astype(str).values,
        "reviewed_at": b["reviewed_at"].fillna("").astype(str).values,
        "source_batch": batch,
        "learned_at": now,
    }, geometry=b.geometry.values, crs=b.crs or 4326)
    missing = out["polygon_id"] == ""
    out.loc[missing, "polygon_id"] = [batch + "-" + sid for sid in out.loc[missing, "survey_id"]]
    return out.to_crs(4326)


def learn_from_review(cfg, review_gpkg, ref_dir=None):
    """Add the approved polygons of ``review_gpkg`` to reference/historic.gpkg."""
    ref_dir = ref_dir or resolve_path(cfg, get(cfg, "reference.dir", "data/reference"))
    fname = (get(cfg, "reference.files", {}) or {}).get("historic") or "historic.gpkg"
    path = os.path.join(ref_dir, fname)
    new = approved_from_review(review_gpkg)
    if not len(new):
        return {"added": 0, "updated": 0, "total": _count(path), "path": path}
    if os.path.isfile(path):
        old = pyogrio.read_dataframe(path)
        old = old.to_crs(4326) if old.crs is not None and old.crs.to_epsg() != 4326 else old
        for col in FIELDS:
            if col not in old.columns:
                old[col] = ""
        replaced = old["polygon_id"].isin(set(new["polygon_id"]))
        updated = int(replaced.sum())
        merged = gpd.GeoDataFrame(pd.concat([old[~replaced][FIELDS + ["geometry"]], new[FIELDS + ["geometry"]]],
                                            ignore_index=True), geometry="geometry", crs=4326)
    else:
        updated = 0
        merged = new[FIELDS + ["geometry"]]
    tmp = path + ".tmp.gpkg"
    if os.path.exists(tmp):
        os.remove(tmp)
    pyogrio.write_dataframe(merged, tmp, layer="historic", driver="GPKG")
    os.replace(tmp, path)
    register_file(ref_dir, "historic", path, "Client approved polygons (operator review)", "Client-owned")
    log.info("Learned %d approved polygon(s) from %s (%d replaced); historic now %d",
             len(new) - updated, review_gpkg, updated, len(merged))
    return {"added": len(new) - updated, "updated": updated, "total": len(merged), "path": path}


def _count(path):
    try:
        return int(pyogrio.read_info(path)["features"]) if os.path.isfile(path) else 0
    except Exception:
        return 0
