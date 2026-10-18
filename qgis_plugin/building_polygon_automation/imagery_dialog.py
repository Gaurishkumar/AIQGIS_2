"""'Client imagery': check imagery files a client supplied and register them for AI tracing.

Every file is inspected before use: is it placed on the map (georeferenced), how sharp is it,
which survey points does it cover, can the worker read it. Raw drone photos, screenshots and
elevation models are recognised and explained instead of silently producing wrong outlines.
Automated use needs a recorded permission (licence gate), exactly as for any other source.
"""

import datetime as dt
import math
import os
import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout)

RASTER_EXT = (".tif", ".tiff", ".vrt", ".jp2", ".img", ".ecw", ".sid", ".jpg", ".jpeg", ".png")
WORKER_READS = (".tif", ".tiff", ".vrt", ".jp2", ".img", ".jpg", ".jpeg", ".png")   # rasterio's GDAL build
GOOD_GSD, MAX_GSD = 0.30, 0.60


def _date(md):
    for k in ("TIFFTAG_DATETIME", "EXIF_DateTimeOriginal", "EXIF_DateTime", "ACQUISITIONDATETIME"):
        v = (md or {}).get(k)
        if v:
            t = str(v).strip().replace(":", "-", 2)[:10]
            try:
                return dt.date.fromisoformat(t).isoformat()
            except ValueError:
                continue
    return ""


def inspect_raster(path, points=(), max_gsd=MAX_GSD):
    """What this file is and whether the AI may trace on it. ``points``: [(lon, lat), ...] of the batch."""
    from osgeo import gdal, osr
    info = {"path": path, "name": os.path.basename(path), "map_ready": False, "for_ai": False, "viewable": False,
            "gsd_m": None, "covers": 0, "date": "", "crs": "", "bbox": None, "verdict": ""}
    try:                       # works whether or not another plugin turned GDAL exceptions on
        ds = gdal.Open(path)
    except RuntimeError as exc:
        ds, err = None, str(exc)
    else:
        err = gdal.GetLastErrorMsg()
    if ds is None:
        info["verdict"] = "Cannot be opened: %s" % ((err or "unknown format").splitlines()[0])
        return info
    md = dict(ds.GetMetadata() or {})
    md.update(ds.GetMetadata("EXIF") or {})
    info["date"] = _date(md)
    gt = ds.GetGeoTransform(can_return_null=True)
    wkt = ds.GetProjection() or ""
    if not wkt or gt is None or tuple(gt) == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
        if ds.GetGCPCount():
            info["verdict"] = ("Has control points but is not map-ready: warp it with Raster > Georeferencer first, "
                               "then add the result")
        elif any(k.startswith("EXIF_GPS") for k in md):
            info["verdict"] = ("Raw drone/camera photo: it has a GPS position but is not placed on the map (tilted, no "
                               "scale). Ask for the stitched orthomosaic (GeoTIFF) - or stitch the photos with drone "
                               "software / OpenDroneMap")
        else:
            info["verdict"] = "No map position (screenshot or plain picture): it must be georeferenced first"
        return info
    info["map_ready"] = info["viewable"] = True
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    info["crs"] = srs.GetAuthorityCode(None) and "EPSG:%s" % srs.GetAuthorityCode(None) or srs.GetName() or "custom"
    tr = osr.CoordinateTransformation(srs, wgs)
    w, h = ds.RasterXSize, ds.RasterYSize
    corners = [(gt[0] + c * gt[1] + r * gt[2], gt[3] + c * gt[4] + r * gt[5]) for c, r in ((0, 0), (w, 0), (0, h), (w, h))]
    ll = [tr.TransformPoint(x, y)[:2] for x, y in corners]
    lons, lats = [p[0] for p in ll], [p[1] for p in ll]
    info["bbox"] = (min(lons), min(lats), max(lons), max(lats))
    px = math.hypot(gt[1], gt[4])
    if srs.IsGeographic():
        gsd = px * 111320.0 * math.cos(math.radians((min(lats) + max(lats)) / 2.0))
    else:
        gsd = px * (srs.GetLinearUnits() or 1.0)
    info["gsd_m"] = gsd
    b = info["bbox"]
    info["covers"] = sum(1 for lon, lat in points if b[0] <= lon <= b[2] and b[1] <= lat <= b[3])
    band = ds.GetRasterBand(1)
    if ds.RasterCount == 1 and gdal.GetDataTypeName(band.DataType).startswith(("Float", "Int32", "UInt32")):
        info["verdict"] = "Looks like an elevation model or analysis layer, not a photo - added for viewing only"
        return info
    ext = os.path.splitext(path)[1].lower()
    if ext not in WORKER_READS:
        info["verdict"] = ("%s files cannot be read by the AI worker: convert to GeoTIFF (Raster > Conversion > "
                           "Translate) and add the GeoTIFF. Shown for viewing." % ext.upper().lstrip("."))
        return info
    if gsd > max_gsd:
        info["verdict"] = ("Too coarse for AI outlines (%s per pixel; needs %s or sharper) - shown for viewing only"
                           % (_cm(gsd), _cm(max_gsd)))
        return info
    info["for_ai"] = True
    if gsd <= GOOD_GSD:
        info["verdict"] = "Good for AI tracing (%s per pixel)" % _cm(gsd)
    else:
        info["verdict"] = ("Usable for normal-size buildings (%s per pixel); small houses keep the map outline"
                           % _cm(gsd))
    if info["covers"] == 0 and points:
        info["verdict"] += " - but it covers none of the open batch's survey points"
    return info


def _cm(v):
    return "%.0f cm" % (v * 100) if v < 1 else "%.1f m" % v


def build_mosaic(infos, out_path):
    """One virtual mosaic for tiled orthomosaics (same CRS and resolution), so a building on a tile
    edge is still covered. Returns the mosaic path, or None when the files do not belong together."""
    from osgeo import gdal
    if len(infos) < 2:
        return None
    crs = {i["crs"] for i in infos}
    gsds = [i["gsd_m"] for i in infos]
    if len(crs) != 1 or max(gsds) > min(gsds) * 1.15:
        return None
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    vrt = gdal.BuildVRT(out_path, [i["path"] for i in infos])
    if vrt is None:
        return None
    vrt = None   # flush to disk
    return out_path if os.path.isfile(out_path) else None


class ImageryDialog(QDialog):
    COLS = ["File", "Map-ready", "Detail", "Covers", "Date", "Verdict"]

    def __init__(self, points, operator, max_gsd=MAX_GSD, parent=None):
        super().__init__(parent)
        self.points = points
        self.max_gsd = max_gsd
        self.infos = []
        self.setWindowTitle("Client imagery")
        self.setMinimumWidth(820)
        intro = QLabel("Add the imagery the client supplied (GeoTIFF / orthomosaic tiles). Each file is checked: "
                       "it must be placed on the map and sharp enough (%s per pixel or better). Raw drone photos "
                       "must be stitched into an orthomosaic first." % _cm(max_gsd))
        intro.setWordWrap(True)
        self.btn_files = QPushButton("Add files…")
        self.btn_folder = QPushButton("Add folder…")
        self.btn_remove = QPushButton("Remove")
        row = QHBoxLayout()
        for b in (self.btn_files, self.btn_folder, self.btn_remove):
            row.addWidget(b)
        row.addStretch(1)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(160)

        self.permission = QCheckBox("The client has given written permission for this imagery to be processed "
                                    "automatically by this software")
        self.licence_ref = QLineEdit()
        self.licence_ref.setPlaceholderText("e.g. Email from Mr X, ABC Ltd, 12-10-2026 / contract no.")
        self.source_name = QLineEdit()
        self.capture = QLineEdit()
        self.capture.setPlaceholderText("YYYY-MM-DD (when the imagery was taken)")
        form = QFormLayout()
        form.addRow("", self.permission)
        form.addRow("Permission record *", self.licence_ref)
        form.addRow("Name", self.source_name)
        form.addRow("Captured", self.capture)
        self.summary = QLabel(" ")
        self.summary.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(row)
        lay.addWidget(self.table)
        lay.addLayout(form)
        lay.addWidget(self.summary)
        lay.addWidget(self.buttons)
        self.btn_files.clicked.connect(self._add_files)
        self.btn_folder.clicked.connect(self._add_folder)
        self.btn_remove.clicked.connect(self._remove)
        self.permission.toggled.connect(self._update)
        self.licence_ref.textChanged.connect(self._update)
        self._update()

    # ------------------------------------------------------------------
    def add_paths(self, paths):
        known = {os.path.normcase(i["path"]) for i in self.infos}
        for p in paths:
            if os.path.normcase(p) in known or not p.lower().endswith(RASTER_EXT):
                continue
            info = inspect_raster(p, self.points, self.max_gsd)
            self.infos.append(info)
            known.add(os.path.normcase(p))
        if not self.source_name.text() and self.infos:
            base = os.path.basename(os.path.dirname(self.infos[0]["path"])) or "imagery"
            self.source_name.setText("client_%s" % re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_"))
        if not self.capture.text():
            dates = sorted(i["date"] for i in self.infos if i["date"])
            if dates:
                self.capture.setText(dates[-1])
        self._fill()
        self._update()

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Client imagery", "",
                                                "Imagery (%s)" % " ".join("*" + e for e in RASTER_EXT))
        self.add_paths(paths)

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder with client imagery")
        if d:
            found = []
            for root, _dirs, files in os.walk(d):
                found.extend(os.path.join(root, f) for f in files if f.lower().endswith(RASTER_EXT))
            self.add_paths(sorted(found))

    def _remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.infos[r]
        self._fill()
        self._update()

    def _fill(self):
        self.table.setRowCount(len(self.infos))
        for r, i in enumerate(self.infos):
            vals = [i["name"], "yes" if i["map_ready"] else "NO", _cm(i["gsd_m"]) if i["gsd_m"] else "-",
                    str(i["covers"]) if i["map_ready"] else "-", i["date"] or "-", i["verdict"]]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 5:
                    item.setToolTip(i["path"] + "\n" + i["verdict"])
                item.setForeground(Qt.GlobalColor.darkGreen if i["for_ai"] else
                                   (Qt.GlobalColor.darkYellow if i["viewable"] else Qt.GlobalColor.red))
                self.table.setItem(r, c, item)

    def usable(self):
        return [i for i in self.infos if i["for_ai"]]

    def viewing_only(self):
        return [i for i in self.infos if i["viewable"] and not i["for_ai"]]

    def _update(self):
        ai, view = self.usable(), self.viewing_only()
        covered = len({(lon, lat) for lon, lat in self.points
                       for i in ai if i["bbox"][0] <= lon <= i["bbox"][2] and i["bbox"][1] <= lat <= i["bbox"][3]})
        allowed = self.permission.isChecked() and bool(self.licence_ref.text().strip())
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ai and allowed:
            ok.setText("Use for AI tracing")
            ok.setEnabled(True)
        elif ai:
            ok.setText("Use for AI tracing")
            ok.setEnabled(False)
        elif view:
            ok.setText("Show on map only")
            ok.setEnabled(True)
        else:
            ok.setText("OK")
            ok.setEnabled(False)
        parts = []
        if ai:
            parts.append("%d file(s) usable for AI tracing, covering %d survey point(s) of the open batch"
                         % (len(ai), covered))
            if not allowed:
                parts.append("tick the permission box and record who gave it to enable AI tracing")
        if view:
            parts.append("%d file(s) for viewing only" % len(view))
        bad = len(self.infos) - len(ai) - len(view)
        if bad:
            parts.append("%d file(s) cannot be used (see Verdict)" % bad)
        self.summary.setText("; ".join(parts) or "Add the imagery files.")

    def spec(self, source_id=None):
        """Provider entry for the worker (licence gate open only with the recorded permission)."""
        sid = source_id or re.sub(r"[^A-Za-z0-9_]+", "_", self.source_name.text().strip()) or "client_imagery"
        return {"id": sid, "type": "local_raster", "paths": [i["path"] for i in self.usable()],
                "allow_automated_analysis": bool(self.permission.isChecked()),
                "licence_ref": self.licence_ref.text().strip(), "capture_date": self.capture.text().strip(),
                "priority": 10, "gsd_m": round(min(i["gsd_m"] for i in self.usable()), 3) if self.usable() else None}
