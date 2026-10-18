"""See SAM 2.1 at work: what the model is given, and what it returns.

Synthetic mode (no imagery needed) - runs the real worker pipeline on a rendered
0.3 m scene whose true building outlines are known, and draws every step:

    worker\\.venv\\Scripts\\python.exe tools\\demo_sam.py

Real-image mode - segment the building at a point in any GeoTIFF you are
allowed to process (e.g. your own drone ortho, or CC-BY imagery):

    worker\\.venv\\Scripts\\python.exe tools\\demo_sam.py --image ortho.tif --lon 73.1398 --lat 19.2264

Outputs go to data\\demo_sam\\ (PNG pictures + a GeoJSON polygon you can open in QGIS).
"""

import argparse
import copy
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "worker"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import bpa_worker  # noqa: E402,F401
from bpa_common.config import load_config  # noqa: E402

GREEN, RED, YELLOW, MAGENTA, CYAN = (0, 200, 0), (0, 0, 230), (0, 230, 255), (255, 0, 255), (255, 200, 0)


def _poly_px(crop, poly, scale):
    """Map polygon (metres) -> pixel coordinates of the crop, using the crop's affine transform."""
    pts = [crop.to_pixel(x, y) for x, y in poly.exterior.coords]
    return np.array([[int(round(c * scale)), int(round(r * scale))] for c, r in pts], np.int32)


def _label(img, text, y=18):
    (w, h), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    cv2.rectangle(img, (2, y - h - 5), (10 + w, y + base + 1), (30, 30, 30), -1)
    cv2.putText(img, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)


def _panels(crop, prompts, mask, final_poly, truth=None, footprint=None, scale=2):
    """Three panels: what SAM saw | prompts it got | what it returned."""
    base = cv2.resize(crop.image[:, :, ::-1], None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    a = base.copy()
    _label(a, "1. image given to SAM")
    b = base.copy()
    if footprint is not None:
        cv2.polylines(b, [_poly_px(crop, footprint, scale)], True, CYAN, 1)
    for c, r in prompts.get("pos", []):
        cv2.drawMarker(b, (int(c * scale), int(r * scale)), GREEN, cv2.MARKER_CROSS, 14, 3)
    for c, r in prompts.get("neg", []):
        cv2.drawMarker(b, (int(c * scale), int(r * scale)), RED, cv2.MARKER_TILTED_CROSS, 14, 3)
    if prompts.get("box") is not None:
        x0, y0, x1, y1 = [int(v * scale) for v in prompts["box"]]
        cv2.rectangle(b, (x0, y0), (x1, y1), YELLOW, 1)
    _label(b, "2. prompts (+ inside, x outside)")
    c = base.copy()
    m = cv2.resize(mask.astype(np.uint8), (c.shape[1], c.shape[0]), interpolation=cv2.INTER_NEAREST).astype(bool)
    c[m] = (c[m] * 0.45 + np.array(GREEN) * 0.55).astype(np.uint8)
    if truth is not None:
        cv2.polylines(c, [_poly_px(crop, truth, scale)], True, MAGENTA, 1)
    if final_poly is not None:
        cv2.polylines(c, [_poly_px(crop, final_poly, scale)], True, YELLOW, 2)
    _label(c, "3. mask + final polygon")
    gap = np.full((a.shape[0], 6, 3), 255, np.uint8)
    return np.hstack([a, gap, b, gap, c])


def synthetic_demo(out_dir):
    from bpa_worker import synthetic as S
    from bpa_worker.batch import BatchManager
    from bpa_worker.geo import iou
    from bpa_worker.imagery import Crop, read_crop
    from bpa_worker.reference import ReferenceData
    from bpa_worker.segmentation import SamSegmenter

    cfg = load_config(os.path.join(ROOT, "config", "config.yaml"))
    scene_dir = os.path.join(out_dir, "scene")
    print("1) Rendering a synthetic 0.3 m ortho image with 6 buildings (true outlines known)...")
    scene = S.render_scene(os.path.join(scene_dir, "ortho.tif"))
    S.write_reference(os.path.join(scene_dir, "ref"), scene)
    cfg["reference"]["dir"] = os.path.join(scene_dir, "ref")
    cfg["reference"]["files"]["addresses"] = None
    cfg["imagery"]["providers"] = [{"id": "synthetic_ortho", "type": "local_raster",
                                    "paths": [os.path.join(scene_dir, "ortho.tif")],
                                    "allow_automated_analysis": True, "licence_ref": "SYNTHETIC-DEMO"}]
    print("2) Loading SAM 2.1 (%s) from data\\models ..." % cfg["segmentation"]["model"])
    seg = SamSegmenter(cfg)
    t0 = time.time()
    if not seg.load():
        sys.exit("SAM could not be loaded: %s" % seg.error)
    print("   loaded %s on %s in %.1f s" % (seg.model_version, seg.device, time.time() - t0))
    print("3) Running the real worker pipeline on 6 survey records...")
    mgr = BatchManager(cfg, ReferenceData(cfg), seg)
    batch = mgr.create(os.path.join(out_dir, "batch"), copy.deepcopy(S.survey_records(scene)), batch_id="demo", start=False)
    t0 = time.time()
    mgr.run_sync(batch)
    print("   done in %.1f s\n" % (time.time() - t0))
    truth = S.truth_by_survey(scene)
    rows, panels = [], []
    print("   %-8s %-15s %10s %10s %9s %9s" % ("record", "method", "footprint", "SAM", "SAM score", "stability"))
    for sid, rec in batch.records.items():
        p = rec.proposal
        cand = rec.candidate(rec.chosen_candidate_id)
        img, tr, epsg, gsd = read_crop(rec.crop_path)
        crop = Crop(img, tr, epsg, gsd, rec.crop_path, "", "", "", (0, 0, 0, 0))
        pr = json.load(open(os.path.join(os.path.dirname(rec.crop_path), "prompts.json")))
        v = pr["chosen"]
        mask = cv2.imread(os.path.join(os.path.dirname(rec.crop_path), "mask_%s.png" % v), 0) > 0
        fp_iou, sam_iou = iou(cand.geom_utm, truth[sid]), iou(p["geometry_utm"], truth[sid])
        print("   %-8s %-15s %10.3f %10.3f %9.3f %9.3f" % (sid, p["method"], fp_iou, sam_iou, p["sam_iou"] or 0,
                                                          p["stability"] or 0))
        rows.append((fp_iou, sam_iou))
        panel = _panels(crop, pr["variants"][v], mask, p["geometry_utm"], truth[sid], cand.geom_utm)
        _label(panel, "%s  variant %s  IoU vs truth: footprint %.2f -> SAM %.2f" % (sid, v, fp_iou, sam_iou),
               panel.shape[0] - 8)
        cv2.imwrite(os.path.join(out_dir, "%s.png" % sid), panel)
        panels.append(panel)
    pick = [panels[i] for i in (4, 1, 3)]          # small building, close neighbour, large building
    width = max(p.shape[1] for p in pick)
    padded = [np.hstack([p, np.full((p.shape[0], width - p.shape[1], 3), 255, np.uint8)]) for p in pick]
    sheet = np.vstack([np.vstack([p, np.full((6, width, 3), 255, np.uint8)]) for p in padded])
    cv2.imwrite(os.path.join(out_dir, "sam_demo_overview.png"), sheet)
    fp = [r[0] for r in rows]
    sm = [r[1] for r in rows]
    print("\n   mean IoU vs true outline: footprint %.3f  ->  SAM %.3f" % (np.mean(fp), np.mean(sm)))
    print("   legend: cyan = input footprint (offset like real ML data), green + = inside prompt,")
    print("           red x = neighbour prompt, yellow box = box prompt, green fill = SAM mask,")
    print("           yellow line = final polygon, magenta = true outline")
    print("\nPictures: %s" % out_dir)


def real_image_demo(out_dir, image, lon, lat, size_m):
    from shapely.geometry import mapping
    from bpa_worker.geo import point_utm, to_wgs84, utm_epsg_for
    from bpa_worker.imagery import ImageryManager
    from bpa_worker.polygonize import mask_to_polygon, touches_edge
    from bpa_worker.segmentation import SamSegmenter

    cfg = load_config(os.path.join(ROOT, "config", "config.yaml"))
    # Your own test image: you confirm you are allowed to analyse it.
    cfg["imagery"]["providers"] = [{"id": "user_test_image", "type": "local_raster", "paths": [os.path.abspath(image)],
                                    "allow_automated_analysis": True, "licence_ref": "USER-SUPPLIED-TEST"}]
    epsg = utm_epsg_for(lon, lat)
    pt = point_utm(lon, lat, epsg)
    bounds = (pt.x - size_m / 2, pt.y - size_m / 2, pt.x + size_m / 2, pt.y + size_m / 2)
    mgr = ImageryManager(cfg)
    prov = mgr.providers[0] if mgr.providers else None
    if prov is None or not prov.covers(bounds, epsg):
        sys.exit("The image does not cover a %d m square around %.6f, %.6f" % (size_m, lat, lon))
    print("1) Cutting a %d x %d m crop around the point from %s ..." % (size_m, size_m, image))
    crop = prov.crop(bounds, epsg, os.path.join(out_dir, "crop.tif"))
    print("   crop %d x %d px at %.2f m/px (UTM EPSG:%d)" % (crop.shape[1], crop.shape[0], crop.gsd_m, epsg))
    seg = SamSegmenter(cfg)
    print("2) Loading SAM 2.1 ...")
    if not seg.load():
        sys.exit("SAM could not be loaded: %s" % seg.error)
    click = crop.to_pixel(pt.x, pt.y)
    print("3) Prompt: one positive point at pixel (%.0f, %.0f) -> SAM returns 3 candidate masks" % click)
    t0 = time.time()
    masks = seg.predict_all("demo", crop.image, [click], [], None, multimask=True)
    print("   image embedding + decoding took %.1f s" % (time.time() - t0))
    amin, amax = cfg["validation"]["area_m2"]
    for i, (m, s) in enumerate(masks):
        print("   mask %d: %.0f m2, SAM score %.3f" % (i, m.sum() * crop.gsd_m ** 2, s))
    ok = [(m, s) for m, s in masks if amin <= m.sum() * crop.gsd_m ** 2 <= amax and not touches_edge(m)]
    mask, score = max(ok or masks, key=lambda t: t[1])
    poly, info = mask_to_polygon(mask, crop, cfg, positive_px=[click])
    if poly is None:
        sys.exit("No polygon could be made from the mask: %s" % info)
    panel = _panels(crop, {"pos": [click]}, mask, poly, scale=max(1, int(600 / max(crop.shape))))
    cv2.imwrite(os.path.join(out_dir, "real_image_result.png"), panel)
    gj = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {
        "area_m2": round(poly.area, 1), "sam_score": round(score, 3)}, "geometry": mapping(to_wgs84(poly, epsg))}]}
    with open(os.path.join(out_dir, "sam_polygon.geojson"), "w", encoding="utf-8") as fh:
        json.dump(gj, fh)
    print("4) Polygon: %.0f m2, %d vertices (regularised: %s)" % (poly.area, len(poly.exterior.coords) - 1,
                                                                  info.get("regularized")))
    print("\nPicture: %s\nPolygon for QGIS: %s" % (os.path.join(out_dir, "real_image_result.png"),
                                                   os.path.join(out_dir, "sam_polygon.geojson")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", help="GeoTIFF you are allowed to analyse (real-image mode)")
    ap.add_argument("--lon", type=float)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--size", type=float, default=60.0, help="crop size in metres (default 60)")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "demo_sam"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    if args.image:
        if args.lon is None or args.lat is None:
            sys.exit("--image needs --lon and --lat of a building")
        real_image_demo(args.out, args.image, args.lon, args.lat, args.size)
    else:
        synthetic_demo(args.out)


if __name__ == "__main__":
    main()
