"""Operator flows added for the client pilot, in QGIS with the real worker, under a DARK theme.

  "C:\\Program Files\\QGIS 4.2.2\\bin\\python-qgis.bat" tests\\qgis\\test_plugin_client_flow.py

1. Import + process without imagery: lanes and plain explanations, readable rows in dark mode
2. Client imagery: files are checked (good / too coarse / screenshot / raw drone photo / elevation
   model), registered with a permission record, and re-processing traces the open records with
   SAM while an approved record stays approved
3. Single building entered by coordinates becomes its own batch and is processed the same way

Set BPA_SHOT_DIR to save screenshots of the panel.
"""

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")   # readable screenshots offscreen

from harness import ROOT, Check, FakeIface, app, pump, wait_until  # noqa: E402

app()

import numpy as np  # noqa: E402
import yaml  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QPalette  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QDialogButtonBox, QStyleFactory  # noqa: E402

from building_polygon_automation.bpa_common import constants as C  # noqa: E402
from building_polygon_automation.compat import val  # noqa: E402
from building_polygon_automation.dock import FILTERS  # noqa: E402
from building_polygon_automation.entry_dialog import SingleEntryDialog  # noqa: E402
from building_polygon_automation.imagery_dialog import ImageryDialog, inspect_raster  # noqa: E402
from building_polygon_automation.plugin import BuildingPolygonAutomationPlugin  # noqa: E402

DEMO = os.path.join(ROOT, "data", "demo_project")


def dark_theme():
    QApplication.setStyle(QStyleFactory.create("Fusion"))
    pal = QPalette()
    for role, col in ((QPalette.ColorRole.Window, "#2b2b2b"), (QPalette.ColorRole.WindowText, "#f0f0f0"),
                      (QPalette.ColorRole.Base, "#1e1e1e"), (QPalette.ColorRole.AlternateBase, "#2b2b2b"),
                      (QPalette.ColorRole.Text, "#f0f0f0"), (QPalette.ColorRole.Button, "#353535"),
                      (QPalette.ColorRole.ButtonText, "#f0f0f0"), (QPalette.ColorRole.Highlight, "#2a82da"),
                      (QPalette.ColorRole.HighlightedText, "#ffffff")):
        pal.setColor(role, QColor(col))
    QApplication.setPalette(pal)


def luminance(c):
    return 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()


def rows_readable(table):
    """Every cell must have strong text/background contrast (the dark-theme 'all white' bug)."""
    base = QApplication.palette().color(QPalette.ColorRole.Base)
    text = QApplication.palette().color(QPalette.ColorRole.Text)
    for r in range(table.rowCount()):
        for c in range(table.columnCount()):
            it = table.item(r, c)
            has_bg = it.data(Qt.ItemDataRole.BackgroundRole) is not None
            has_fg = it.data(Qt.ItemDataRole.ForegroundRole) is not None
            bg = it.background().color() if has_bg else base
            fg = it.foreground().color() if has_fg else text
            if abs(luminance(bg) - luminance(fg)) < 0.45:
                return False, (r, c, bg.name(), fg.name())
    return True, None


def make_test_rasters(folder, ortho):
    """A coarse copy, a plain picture without position, a raw 'drone photo' with EXIF GPS, a DEM."""
    src = gdal.Open(ortho)
    coarse = os.path.join(folder, "coarse_1m.tif")
    gdal.Warp(coarse, src, xRes=1.0, yRes=1.0)
    arr = np.full((60, 80, 3), 120, np.uint8)
    png = os.path.join(folder, "screenshot.png")
    mem = gdal.GetDriverByName("MEM").Create("", 80, 60, 3, gdal.GDT_Byte)
    for b in range(3):
        mem.GetRasterBand(b + 1).WriteArray(arr[:, :, b])
    gdal.GetDriverByName("PNG").CreateCopy(png, mem)
    jpg = os.path.join(folder, "DJI_0042.JPG")
    mem.SetMetadata({"EXIF_GPSLatitude": "(19) (15) (5.2)", "EXIF_GPSLatitudeRef": "N",
                     "EXIF_GPSLongitude": "(73) (0) (11.3)", "EXIF_GPSLongitudeRef": "E",
                     "EXIF_DateTimeOriginal": "2025:11:02 10:15:00"})
    gdal.GetDriverByName("JPEG").CreateCopy(jpg, mem, options=["WRITE_EXIF_METADATA=YES"])
    dem = os.path.join(folder, "dem.tif")
    d = gdal.GetDriverByName("GTiff").Create(dem, 50, 50, 1, gdal.GDT_Float32)
    d.SetGeoTransform(src.GetGeoTransform())
    d.SetProjection(src.GetProjection())
    d.GetRasterBand(1).WriteArray(np.random.rand(50, 50).astype("float32") * 30)
    d = None
    return {"coarse": coarse, "png": png, "jpg": jpg, "dem": dem}


def finished(iface):
    return sum(1 for _t, text, _l in iface.bar.messages if text.startswith("Processing finished"))


def main(shot_dir=None):
    shot_dir = shot_dir or os.environ.get("BPA_SHOT_DIR")
    check = Check()
    dark_theme()
    proj = tempfile.mkdtemp(prefix="bpa_client_proj_")
    for sub in ("config", "data"):
        shutil.copytree(os.path.join(DEMO, sub), os.path.join(proj, sub))
    shutil.copy(os.path.join(DEMO, "synthetic_survey.xlsx"), proj)
    cfg_path = os.path.join(proj, "config", "config.yaml")
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    ortho = os.path.join(proj, "data", "imagery", "ortho.tif")
    cfg["imagery"]["providers"] = []            # a client without imagery ... until they send some
    yaml.safe_dump(cfg, open(cfg_path, "w", encoding="utf-8"), sort_keys=False)

    iface = FakeIface()
    plugin = BuildingPolygonAutomationPlugin(iface)
    plugin.initGui()
    s = plugin.settings
    s.set("project_dir", proj)
    s.set("worker_python", os.path.join(ROOT, "worker", ".venv", "Scripts", "python.exe"))
    s.set("batches_dir", os.path.join(proj, "batches"))
    s.set("operator_id", "op_client")
    s.set("port", 8796)
    s.set("auto_next", False)
    s.set("load_model", False)
    plugin.toggle(True)
    c, dock = plugin.controller, plugin.dock
    dock.resize(780, 860)
    dock.show()

    print("== 1. import + process, dark theme")
    c.import_excel(os.path.join(proj, "synthetic_survey.xlsx"))
    check(dock.table.rowCount() == 6 and dock.table.item(0, 2).text() == "Waiting", "imported rows wait for Process")
    ok, bad = rows_readable(dock.table)
    check(ok, "imported rows readable in the dark theme %s" % (bad or "",))
    n = finished(iface)
    c.process()
    wait_until(lambda: c.worker_ready, 240, "worker health")
    wait_until(lambda: finished(iface) > n and len(c.details) >= 6, 300, "footprint batch")
    pump(0.5)
    lanes = [dock.table.item(r, 2).text() for r in range(dock.table.rowCount())]
    check(all(l in ("Sure", "Check", "Draw") for l in lanes), "every processed record has a lane: %s" % lanes)
    ok, bad = rows_readable(dock.table)
    check(ok, "lane-coloured rows readable in the dark theme %s" % (bad or "",))
    check(dock.evidence.styleSheet().startswith("QTextBrowser") and "#ffffff" in dock.evidence.styleSheet(),
          "evidence panel is a white page in the dark theme")
    k1 = next(iter(c.records))
    c.select(k1, zoom=False)
    pump(0.3)
    text = dock.evidence.toPlainText()
    check("What to do:" in text and ("✔" in text or "⚠" in text), "evidence starts with what was found and what to do")
    e = c.explanation(k1)
    check(dock.table.item(dock._rows[k1], 3).text() == e["short"], "table says what the system found")
    labels = [dock.filter.itemText(i) for i in range(dock.filter.count())]
    sure = c.filter_counts()["sure"]
    check(any(t.startswith("Sure - approve quickly (%d)" % sure) for t in labels), "lane filters show counts: %s" % labels)
    dock.filter.setCurrentIndex([k for _l, k in FILTERS].index("sure"))
    check(dock.table.rowCount() == sure, "Sure filter lists only sure records")
    dock.filter.setCurrentIndex(0)
    if shot_dir:
        os.makedirs(shot_dir, exist_ok=True)
        dock.grab().save(os.path.join(shot_dir, "panel_dark_after.png"))
    c.select(k1, zoom=False)
    c.approve(k1)
    approved_sid = c.records[k1]["survey_id"]
    check(c.records[k1]["state"] == C.APPROVED and c.explanation(k1)["lane"] == "done", "approved record is Done")

    print("\n== 2. client imagery")
    tmpd = tempfile.mkdtemp(prefix="bpa_client_img_")
    files = make_test_rasters(tmpd, ortho)
    pts = c.survey_points()
    good = inspect_raster(ortho, pts)
    check(good["for_ai"] and abs(good["gsd_m"] - 0.3) < 0.01 and good["covers"] == 6,
          "orthophoto: map-ready, 30 cm, covers all 6 points (%s)" % good["verdict"])
    coarse = inspect_raster(files["coarse"], pts)
    check(coarse["viewable"] and not coarse["for_ai"] and "Too coarse" in coarse["verdict"], "1 m image: viewing only")
    png = inspect_raster(files["png"], pts)
    check(not png["map_ready"] and "No map position" in png["verdict"], "screenshot recognised (no map position)")
    jpg = inspect_raster(files["jpg"], pts)
    check(not jpg["map_ready"] and "Raw drone" in jpg["verdict"] and jpg["date"] == "2025-11-02",
          "raw drone photo recognised from its EXIF GPS: %s" % jpg["verdict"][:60])
    dem = inspect_raster(files["dem"], pts)
    check(dem["viewable"] and not dem["for_ai"] and "elevation" in dem["verdict"], "elevation model not used as a photo")

    dlg = ImageryDialog(pts, "op_client", 0.6, iface.mainWindow())
    dlg.add_paths([ortho, files["coarse"], files["png"], files["jpg"], files["dem"]])
    okb = dlg.buttons.button(QDialogButtonBox.StandardButton.Ok)
    check(len(dlg.usable()) == 1 and not okb.isEnabled(), "AI use stays locked until the permission is recorded")
    dlg.permission.setChecked(True)
    dlg.licence_ref.setText("Email from client ops head, 14-10-2026")
    check(okb.isEnabled() and okb.text() == "Use for AI tracing", "permission + record unlock AI tracing")
    spec = dlg.spec()
    check(spec["paths"] == [ortho] and spec["allow_automated_analysis"] and spec["licence_ref"],
          "only the usable file goes to the worker, with its permission record")
    res = c.add_imagery(spec, [i["path"] for i in dlg.viewing_only()])
    src_file = os.path.join(proj, "config", "imagery_sources.yaml")
    check(res["registered"] and os.path.isfile(src_file), "imagery registered for the project")
    bcfg = yaml.safe_load(open(c.batch["config_path"], encoding="utf-8"))
    check([p["id"] for p in bcfg["imagery"]["providers"]] == [spec["id"]], "open batch now uses the client imagery")
    check(any(a["action"] == C.ACTION_ADD_IMAGERY and "Email from client" in a["note"] for a in c.store.actions()),
          "audit trail records who added the imagery and the permission")
    check(len(res["layers"]) >= 2, "imagery shown on the map (AI file + viewing-only files)")

    s.set("load_model", True)                     # what the Client imagery button does
    c.stop_worker()
    pump(1.0)
    n = finished(iface)
    c.process()
    wait_until(lambda: c.worker_ready, 300, "worker with model")
    wait_until(lambda: finished(iface) > n, 600, "re-processing")
    pump(0.5)
    methods = {k: (c.details[k].get("proposal") or {}).get("method") for k in c.records if k != k1}
    check(all(m and "SAM" in m for m in methods.values()), "open records re-traced by the AI on client imagery: %s"
          % sorted(set(methods.values())))
    b = c.store.building(approved_sid)
    check(val(b["status"]) == C.STATUS_APPROVED and c.records[k1]["state"] == C.APPROVED,
          "the approved record stayed approved")
    k2 = next(k for k in c.records if k != k1)
    why = " ".join(c.explanation(k2)["why"])
    check("traced by AI" in why and "30 cm" in why and "detected by computer" not in why,
          "explanation names the imagery and its detail: %s" % why[-70:])

    print("\n== 3. single building")
    r0 = c.records[k2]
    entry = SingleEntryDialog(iface, {"min_lat": 6, "max_lat": 37.6, "min_lon": 68, "max_lon": 97.5},
                              plugin._single_entries, iface.mainWindow())
    entry.show()
    entry.location.setText("https://www.google.com/maps?q=%.7f,%.7f" % (r0["latitude"], r0["longitude"]))
    check("%.6f" % r0["latitude"] in entry.parsed.text() and entry.marker is not None,
          "pasted map link understood and marked on the map")
    entry.building_name.setText(r0["fields"].get("building_name") or "Test building")
    n = finished(iface)
    entry.submit()
    pump(0.5)
    check(c.batch["batch_id"].startswith("single_") and len(c.records) == 1, "single building became its own batch")
    check(not entry.isVisible() and entry.marker is None, "dialog closes and removes its map marker")
    try:
        wait_until(lambda: finished(iface) > n and len(c.details) == 1, 300, "single building processing")
    except TimeoutError:
        print("DIAG batch", c.worker_batch_id, "polling", c.poll_timer.isActive(), "fetching", c._fetching,
              "details", list(c.details), "reported", c._finish_reported)
        print("DIAG bar", iface.bar.messages[-6:])
        print("DIAG worker log:\n" + "\n".join(c.worker.log_tail(60) if c.worker else []))
        raise
    ks = next(iter(c.records))
    es = c.explanation(ks)
    check(es["lane"] in ("sure", "check", "draw") and es["why"], "single building has a lane and an explanation (%s)"
          % es["label"])
    check("SAM" in ((c.details[ks].get("proposal") or {}).get("method") or ""),
          "new batches use the registered client imagery too")
    if shot_dir:
        c.select(ks, zoom=False)
        pump(0.3)
        dock.grab().save(os.path.join(shot_dir, "panel_single_building.png"))

    c.stop_worker()
    pump(1.0)
    plugin.unload()
    return check.done()


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
