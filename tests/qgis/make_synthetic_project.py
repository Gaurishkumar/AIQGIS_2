"""Create a self-contained synthetic project for the imagery / click-to-fix plugin test.

Run with the WORKER Python (needs rasterio/geopandas):
  worker\\.venv\\Scripts\\python.exe tests\\qgis\\make_synthetic_project.py <out_dir>

Writes <out_dir>/config/config.yaml (licensed synthetic ortho enabled),
<out_dir>/data/reference/*, <out_dir>/data/imagery/ortho.tif,
<out_dir>/synthetic_survey.xlsx (client column layout) and truth.geojson.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "worker"))

import bpa_worker  # noqa: E402,F401
import openpyxl  # noqa: E402
import yaml  # noqa: E402
from shapely.geometry import mapping  # noqa: E402

from bpa_worker import synthetic as S  # noqa: E402
from bpa_worker.geo import to_wgs84  # noqa: E402

HEADERS = ["Survey ID", "latitude", "longitude", "Customer Name", "Building Name", "Building No", "Tower/Block",
           "Street Name", "Sector/Phase", "Area Name", "Landmark", "City", "District Name", "State Name", "pincode",
           "Standard Building Address", "Building Type", "Building Category", "Building Height(m)", "Floors",
           "Location coordinates change remark", "Correct and Correeted Lat &long", "Name", "Status",
           "Delivery Date", None, "Don’t Remove This  Data", "Location coordinates change remark"]


def main(out):
    os.makedirs(os.path.join(out, "config"), exist_ok=True)
    ref = os.path.join(out, "data", "reference")
    ortho = os.path.join(out, "data", "imagery", "ortho.tif")
    scene = S.render_scene(ortho)
    S.write_reference(ref, scene)
    with open(os.path.join(ROOT, "config", "config.yaml"), "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["reference"]["files"]["addresses"] = None
    cfg["imagery"]["providers"] = [{
        "id": "synthetic_ortho", "type": "local_raster", "paths": ["data/imagery/ortho.tif"],
        "allow_automated_analysis": True, "licence_ref": "SYNTHETIC-TEST-001", "capture_date": "2025-01-15",
        "priority": 1}]
    cfg["segmentation"]["checkpoints_dir"] = os.path.join(ROOT, "data", "models")
    with open(os.path.join(out, "config", "config.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(HEADERS)
    opts = ["Select Option", cfg["excel"]["remarks"]["unchanged"], cfg["excel"]["remarks"]["modified"],
            cfg["excel"]["remarks"]["adjusted_near"]]
    for i, r in enumerate(S.survey_records(scene)):
        f = r["fields"]
        row = [r["survey_id"], round(r["latitude"], 6), round(r["longitude"], 6), "Test Bank", f["building_name"],
               None, None, f["street"], None, f["area"], None, f["city"], "Thane", "Maharashtra", int(f["pincode"]),
               "%s,%s,%s" % (f["building_name"], f["street"], f["city"]), "Non-MTU", f["building_category"],
               int(f["building_height_m"]), int(f["floors"]), None, None, None, None, None, None, None,
               opts[i] if i < len(opts) else None]
        ws.append(row)
    ws.merge_cells("AA1:AA5")
    wb.save(os.path.join(out, "synthetic_survey.xlsx"))
    truth = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"survey_id": "SYN_" + bid},
         "geometry": mapping(to_wgs84(poly, S.EPSG))} for bid, _, poly in scene["buildings"]]}
    with open(os.path.join(out, "truth.geojson"), "w", encoding="utf-8") as fh:
        json.dump(truth, fh)
    print(out)


if __name__ == "__main__":
    main(sys.argv[1])
