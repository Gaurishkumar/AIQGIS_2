"""Licence gate, georeferenced crops, GeoPackage writer, reference coverage."""

import os

import numpy as np
import pyogrio
import pytest
import rasterio
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import box

from bpa_worker import synthetic as S
from bpa_worker.gpkg import GeoPackage, decode_geometry, encode_geometry
from bpa_worker.imagery import ImageryManager, LicenceGateClosed, aoi_bounds
from bpa_worker.prepare import compute_boxes, point_covered

from conftest import synthetic_cfg


def test_licence_gate_refuses(base_cfg, synthetic):
    for allow, licence in ((False, "LIC-1"), (True, "")):
        m = ImageryManager(synthetic_cfg(base_cfg, synthetic, allow=allow, licence=licence))
        assert not m.any_enabled()
        b = (290040.0, 2129910.0, 290100.0, 2129970.0)
        assert m.select(b, S.EPSG) is None                     # never selected
        with pytest.raises(LicenceGateClosed):
            m.providers[0].crop(b, S.EPSG, os.path.join(synthetic["dir"], "refused.tif"))
        assert m.status()[0]["enabled"] is False and m.status()[0]["reason"]


def test_crop_is_georeferenced(base_cfg, synthetic, tmp_path):
    m = ImageryManager(synthetic_cfg(base_cfg, synthetic))
    poly = S.scene_buildings()[3][2]                           # B04
    b = aoi_bounds(poly, 15.0, 60.0)
    assert b[2] - b[0] >= 60 and b[3] - b[1] >= 60
    crop = m.select(b, S.EPSG).crop(b, S.EPSG, str(tmp_path / "c.tif"))
    assert crop.gsd_m == pytest.approx(0.3) and crop.licence_ref == "SYNTHETIC-TEST-001"
    c = poly.centroid
    col, row = crop.to_pixel(c.x, c.y)
    x, y = crop.to_map(col, row)
    assert (x, y) == pytest.approx((c.x, c.y))
    assert tuple(crop.image[int(row), int(col) + 20]) != (0, 0, 0)  # roof pixels, not nodata
    with rasterio.open(crop.path) as src:
        assert src.crs.to_epsg() == S.EPSG and abs(src.transform.a - 0.3) < 1e-9


def test_crop_warps_other_crs(base_cfg, synthetic, tmp_path):
    """An image delivered in EPSG:4326 is read through WarpedVRT into the record's UTM zone."""
    src_path = synthetic["ortho"]
    dst_path = str(tmp_path / "ortho_4326.tif")
    with rasterio.open(src_path) as src:
        tr, w, h = calculate_default_transform(src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
        with rasterio.open(dst_path, "w", driver="GTiff", width=w, height=h, count=3, dtype="uint8",
                           crs="EPSG:4326", transform=tr) as dst:
            for i in range(1, 4):
                reproject(rasterio.band(src, i), rasterio.band(dst, i), resampling=Resampling.bilinear)
    cfg = synthetic_cfg(base_cfg, synthetic)
    cfg["imagery"]["providers"][0]["paths"] = [dst_path]
    m = ImageryManager(cfg)
    poly = S.scene_buildings()[0][2]
    b = aoi_bounds(poly, 15.0, 60.0)
    crop = m.select(b, S.EPSG).crop(b, S.EPSG, str(tmp_path / "w.tif"))
    assert crop.gsd_m < 0.35
    # the warped crop shows the same ground as the native UTM image at the same map points
    c = poly.centroid
    diffs = []
    with rasterio.open(src_path) as src:
        for dx, dy in ((5, 0), (-6, 2), (0, -4), (25, 20), (-20, -18)):
            x, y = c.x + dx, c.y + dy
            r, cc = src.index(x, y)
            native = src.read(window=((r, r + 1), (cc, cc + 1)))[:, 0, 0].astype(float)
            col, row = crop.to_pixel(x, y)
            warped = crop.image[int(row), int(col)].astype(float)
            diffs.append(np.abs(native - warped).mean())
    assert np.median(diffs) < 25, diffs


def test_coarse_imagery_refused(base_cfg, synthetic, tmp_path):
    cfg = synthetic_cfg(base_cfg, synthetic)
    cfg["imagery"]["max_gsd_m"] = 0.2
    m = ImageryManager(cfg)
    b = aoi_bounds(S.scene_buildings()[0][2], 15, 60)
    from bpa_worker.imagery import ImageryUnavailable
    with pytest.raises(ImageryUnavailable, match="coarser"):
        m.providers[0].crop(b, S.EPSG, str(tmp_path / "x.tif"))


def test_xyz_local_tiles(base_cfg, synthetic, tmp_path):
    """XYZ provider mosaics {z}/{x}/{y} tiles, georeferences them and reprojects to UTM."""
    import cv2
    from bpa_worker.geo import to_wgs84
    z = 19
    poly = S.scene_buildings()[0][2]
    ll = to_wgs84(poly.centroid, S.EPSG)
    n = 2 ** z
    tx = int((ll.x + 180) / 360 * n)
    lat = np.radians(ll.y)
    ty = int((1 - np.log(np.tan(lat) + 1 / np.cos(lat)) / np.pi) / 2 * n)
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            d = tmp_path / str(z) / str(tx + dx)
            d.mkdir(parents=True, exist_ok=True)
            img = np.full((256, 256, 3), 40 * (dx + 3), np.uint8)
            cv2.imwrite(str(d / ("%d.png" % (ty + dy))), img)
    cfg = synthetic_cfg(base_cfg, synthetic, imagery=False)
    cfg["imagery"]["providers"] = [{"id": "tiles", "type": "xyz_tiles", "zoom": z,
                                    "url": str(tmp_path) + "/{z}/{x}/{y}.png",
                                    "allow_automated_analysis": True, "licence_ref": "TILE-LIC"}]
    m = ImageryManager(cfg)
    b = aoi_bounds(poly, 5, 40)
    crop = m.select(b, S.EPSG).crop(b, S.EPSG, str(tmp_path / "t.tif"))
    assert crop.gsd_m < 0.3 and crop.image.shape[0] > 100 and crop.image.max() > 0


def test_gpkg_roundtrip(tmp_path):
    p = str(tmp_path / "w.gpkg")
    gp = GeoPackage(p)
    gp.create_table("t", [("key", "TEXT"), ("v", "REAL")], geometry_type="POLYGON", key="key")
    gp.upsert("t", {"key": "a", "v": 1.0}, box(73, 19, 73.001, 19.001))
    gp.upsert("t", {"key": "a", "v": 2.0}, box(73, 19, 73.002, 19.002))
    gp.upsert("t", {"key": "b", "v": 3.0}, None)
    gp.close()
    df = pyogrio.read_dataframe(p, layer="t")
    assert len(df) == 2 and df.set_index("key").loc["a", "v"] == 2.0
    assert df.crs.to_epsg() == 4326 and df.geometry.iloc[0].bounds[2] == pytest.approx(73.002)
    g = box(1, 2, 3, 4)
    assert decode_geometry(encode_geometry(g)).equals(g)


def test_reference_boxes_and_coverage():
    pts = [(73.0, 19.0), (73.001, 19.001), (73.5, 19.5)]
    boxes = compute_boxes(pts, 1000, "clusters", 3000)
    assert len(boxes) == 2
    assert point_covered(boxes, 73.0005, 19.0005, 300)
    assert not point_covered(boxes, 72.0, 19.0, 300)
    assert len(compute_boxes(pts, 1000, "project")) == 1


def _resampled_copy(src_path, dst_path, factor, zero_left_half=False):
    """Copy of the synthetic ortho at ``factor`` x coarser resolution (optionally half no-data)."""
    with rasterio.open(src_path) as src:
        w, h = int(src.width / factor), int(src.height / factor)
        data = src.read(out_shape=(src.count, h, w), resampling=Resampling.average)
        if zero_left_half:
            data[:, :, : w // 2] = 0
        prof = src.profile.copy()
        prof.update(width=w, height=h, transform=src.transform * src.transform.scale(src.width / w, src.height / h))
    with rasterio.open(dst_path, "w", **prof) as dst:
        dst.write(data)
    return dst_path


def test_sharpest_file_wins_and_big_crops_are_capped(base_cfg, synthetic, tmp_path):
    coarse = _resampled_copy(synthetic["ortho"], str(tmp_path / "coarse.tif"), 2)       # 0.6 m
    cfg = synthetic_cfg(base_cfg, synthetic)
    cfg["imagery"]["providers"][0]["paths"] = [coarse, synthetic["ortho"]]               # coarse listed first
    m = ImageryManager(cfg)
    b = aoi_bounds(S.scene_buildings()[3][2], 15.0, 60.0)
    crop = m.select(b, S.EPSG).crop(b, S.EPSG, str(tmp_path / "c.tif"))
    assert crop.gsd_m == pytest.approx(0.3) and crop.meta["source_file"] == "ortho.tif"
    cfg["imagery"]["crop"]["max_pixels"] = 100                                          # e.g. 2 cm drone image
    capped = ImageryManager(cfg).select(b, S.EPSG).crop(b, S.EPSG, str(tmp_path / "small.tif"))
    assert max(capped.shape) <= 101 and capped.gsd_m == pytest.approx((b[2] - b[0]) / 100, rel=0.02)
    c = S.scene_buildings()[3][2].centroid                                              # still georeferenced
    assert capped.to_map(*capped.to_pixel(c.x, c.y)) == pytest.approx((c.x, c.y))


def test_mostly_blank_crop_is_refused(base_cfg, synthetic, tmp_path):
    from bpa_worker.imagery import ImageryUnavailable
    gappy = _resampled_copy(synthetic["ortho"], str(tmp_path / "gappy.tif"), 1, zero_left_half=True)
    cfg = synthetic_cfg(base_cfg, synthetic)
    cfg["imagery"]["providers"][0]["paths"] = [gappy]
    m = ImageryManager(cfg)
    with rasterio.open(gappy) as src:
        x0 = src.bounds.left
    b = (x0 + 5, src.bounds.bottom + 50, x0 + 65, src.bounds.bottom + 110)              # inside the blank half
    with pytest.raises(ImageryUnavailable, match="no picture"):
        m.providers[0].crop(b, S.EPSG, str(tmp_path / "g.tif"))


def test_registered_sources_reach_new_batches_and_open_batch(base_cfg, tmp_path):
    import shutil
    import yaml
    from bpa_common.config import (add_imagery_to_batch, freeze_config, load_config, load_frozen_config,
                                   save_imagery_source)
    proj = tmp_path / "proj"
    (proj / "config").mkdir(parents=True)
    root_cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "config", "config.yaml")
    shutil.copy(root_cfg, proj / "config" / "config.yaml")
    cfg = load_config(str(proj / "config" / "config.yaml"))
    frozen = str(tmp_path / "batch_config.yaml")
    freeze_config(cfg, frozen)                                       # a batch made before the imagery came
    spec = {"id": "client_drone", "type": "local_raster", "paths": [str(tmp_path / "x.tif")],
            "allow_automated_analysis": True, "licence_ref": "Email from client 12-10-2026", "added_by": "op1"}
    path = save_imagery_source(cfg, spec)
    assert os.path.isfile(path) and yaml.safe_load(open(path))["sources"][0]["id"] == "client_drone"
    again = load_config(str(proj / "config" / "config.yaml"))
    assert [p["id"] for p in again["imagery"]["providers"]] == ["client_drone"]
    assert not load_frozen_config(frozen)["imagery"]["providers"]    # old batch unchanged ...
    add_imagery_to_batch(frozen, spec)
    add_imagery_to_batch(frozen, dict(spec, licence_ref="Contract 7"))  # ... until added; same id replaces
    provs = load_frozen_config(frozen)["imagery"]["providers"]
    assert len(provs) == 1 and provs[0]["licence_ref"] == "Contract 7"
    fresh = str(tmp_path / "batch2.yaml")
    freeze_config(again, fresh)                                      # new batches carry it, without doubling
    assert [p["id"] for p in load_frozen_config(fresh)["imagery"]["providers"]] == ["client_drone"]
