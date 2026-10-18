"""S8 - Mask to regularised polygon.

1. Binary mask; optional 1-pixel morphological opening (speckle).
2. Keep the connected component containing the positive prompt, else the one
   overlapping the candidate most, else the largest.
3. Fill holes smaller than 4 m2.
4. Vectorise with rasterio.features.shapes(mask, transform=crop_transform).
5. (Crop is already in the record's UTM zone.) Regularise with
   buildingregulariser.regularize_geodataframe.
6. Simplify at ~half the pixel size, shapely.make_valid, keep one Polygon.
"""

import logging
import warnings

import cv2
import numpy as np
import shapely
from rasterio import features
from shapely.geometry import shape
from shapely.ops import unary_union

from bpa_common.config import get

from .geo import fill_small_holes, repair_single_polygon

log = logging.getLogger(__name__)


def touches_edge(mask, px=2):
    if mask is None or not mask.any():
        return False
    return bool(mask[:px, :].any() or mask[-px:, :].any() or mask[:, :px].any() or mask[:, -px:].any())


def select_component(mask, positive_px=None, candidate_mask=None):
    n, labels = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
    if n <= 2:
        return mask.astype(bool)
    h, w = mask.shape
    votes = {}
    for (c, r) in positive_px or []:
        ci, ri = int(round(c)), int(round(r))
        if 0 <= ri < h and 0 <= ci < w and labels[ri, ci] > 0:
            votes[labels[ri, ci]] = votes.get(labels[ri, ci], 0) + 1
    if votes:
        keep = max(votes.items(), key=lambda kv: kv[1])[0]
        return labels == keep
    if candidate_mask is not None and candidate_mask.any():
        overlap = np.bincount(labels[candidate_mask].ravel(), minlength=n)
        overlap[0] = 0
        if overlap.max() > 0:
            return labels == int(np.argmax(overlap))
    sizes = np.bincount(labels.ravel(), minlength=n)
    sizes[0] = 0
    return labels == int(np.argmax(sizes))


def fill_holes_px(mask, max_hole_px):
    """Fill background regions enclosed by the mask whose area is < max_hole_px."""
    inv = (~mask).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    out = mask.copy()
    h, w = mask.shape
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if x == 0 or y == 0 or x + bw >= w or y + bh >= h:
            continue  # touches the border: outside, not a hole
        if area < max_hole_px:
            out[labels == i] = True
    return out


def rasterize(poly_utm, crop):
    h, w = crop.shape
    if poly_utm is None or poly_utm.is_empty:
        return np.zeros((h, w), bool)
    return features.rasterize([(poly_utm, 1)], out_shape=(h, w), transform=crop.transform,
                              fill=0, dtype="uint8").astype(bool)


def regularize(poly_utm, epsg, gsd, cfg):
    """Regularise with buildingregulariser; returns the input on failure."""
    params = dict(get(cfg, "polygon.regularize_params", {}) or {})
    try:
        import geopandas as gpd
        from buildingregulariser import regularize_geodataframe
        gdf = gpd.GeoDataFrame(geometry=[poly_utm], crs=epsg)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out = regularize_geodataframe(
                gdf,
                parallel_threshold=float(params.get("parallel_threshold", 1.0)),
                simplify=True,
                # ~1.7 px removes pixel stair-steps before edges are squared.
                simplify_tolerance=max(0.3, float(params.get("simplify_px", 1.7)) * gsd),
                allow_45_degree=bool(params.get("allow_45_degree", True)),
                diagonal_threshold_reduction=float(params.get("diagonal_threshold_reduction", 15)),
                allow_circles=bool(params.get("allow_circles", False)),
                num_cores=1,
            )
        if len(out) and out.geometry.iloc[0] is not None and not out.geometry.iloc[0].is_empty:
            return out.geometry.iloc[0], True
    except Exception as exc:
        log.warning("Regularisation failed, keeping the vectorised polygon: %s", exc)
    return poly_utm, False


def mask_to_polygon(mask, crop, cfg, positive_px=None, candidate_utm=None):
    """Convert a SAM mask to a clean single Polygon in the crop's UTM CRS.

    Returns (polygon_or_None, info dict).
    """
    pc = get(cfg, "polygon", {}) or {}
    info = {"touches_edge": False, "regularized": False, "pixels": int(mask.sum())}
    if not mask.any():
        info["error"] = "empty mask"
        return None, info
    gsd = float(crop.gsd_m)
    m = mask.astype(bool)
    k = int(pc.get("morph_open_px", 1) or 0)
    if k > 0:
        opened = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_OPEN,
                                  np.ones((3, 3), np.uint8), iterations=k).astype(bool)
        if opened.any():
            m = opened
    cand_mask = rasterize(candidate_utm, crop) if candidate_utm is not None else None
    m = select_component(m, positive_px, cand_mask)
    min_hole_m2 = float(pc.get("min_hole_area_m2", 4.0))
    m = fill_holes_px(m, max(1, int(round(min_hole_m2 / (gsd * gsd)))))
    info["touches_edge"] = touches_edge(m, int(get(cfg, "validation.edge_touch_px", 2)))
    info["pixels"] = int(m.sum())

    polys = [shape(g) for g, v in features.shapes(m.astype(np.uint8), mask=m, transform=crop.transform) if v == 1]
    if not polys:
        info["error"] = "vectorisation produced no polygon"
        return None, info
    poly = unary_union(polys)
    poly, _ = repair_single_polygon(poly, min_part_share=0.0)
    poly = fill_small_holes(poly, min_hole_m2)
    info["raw_area_m2"] = round(float(poly.area), 2)

    if pc.get("regularize", True):
        reg, ok = regularize(poly, crop.epsg, gsd, cfg)
        info["regularized"] = ok
        # A regulariser result that changes the footprint wildly is discarded.
        if ok and reg.area > 0 and 0.7 <= reg.area / poly.area <= 1.3:
            poly = reg
        elif ok:
            info["regularized"] = False
            info["regularize_rejected"] = True
    tol = float(pc.get("simplify_pixel_factor", 0.5)) * gsd
    simp = poly.simplify(tol, preserve_topology=True)
    if not simp.is_empty and simp.area > 0:
        poly = simp
    poly, n_parts = repair_single_polygon(shapely.make_valid(poly))
    info["n_parts"] = n_parts
    if poly is None:
        info["error"] = "repair produced no polygon"
    return poly, info
