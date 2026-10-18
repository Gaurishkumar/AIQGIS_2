"""Quick check: review many proposals at once.

Each tile shows one record's proposed outline (yellow) and survey point (red)
over the background layers the operator already has switched on (their
basemap), rendered by QGIS for viewing only. The operator clicks the tiles
that look wrong (or presses their number) and approves the rest in one go.
Wrong ones stay in the normal review queue.
"""

import time

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFillSymbol,
                       QgsGeometry, QgsMapRendererParallelJob, QgsMapSettings, QgsMarkerSymbol, QgsPointXY,
                       QgsProject, QgsRectangle, QgsSingleSymbolRenderer, QgsVectorLayer)
from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPixmap
from qgis.PyQt.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

from .bpa_common import constants as C

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
TILE_W, TILE_H = 240, 180


class _Tile(QFrame):
    clicked = pyqtSignal()

    def __init__(self, number, caption, parent=None):
        super().__init__(parent)
        self.setObjectName("tile")
        self.setFrameShape(QFrame.Shape.Box)
        self.setLineWidth(2)
        self.image = QLabel("rendering…")
        self.image.setFixedSize(TILE_W, TILE_H)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption = QLabel("%d. %s" % (number, caption))
        self.caption.setWordWrap(True)
        self.caption.setFixedWidth(TILE_W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)
        lay.addWidget(self.image)
        lay.addWidget(self.caption)
        self.set_marked(False)

    def set_marked(self, marked):
        self.marked = marked
        self.setStyleSheet("QFrame#tile { border: 3px solid %s; }" % ("#d0342c" if marked else "#2f9e44"))

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


def _memory_layer(kind, geom, symbol):
    lyr = QgsVectorLayer("%s?crs=EPSG:4326" % kind, "highlight", "memory")
    f = QgsFeature()
    f.setGeometry(geom)
    lyr.dataProvider().addFeatures([f])
    lyr.setRenderer(QgsSingleSymbolRenderer(symbol))
    return lyr


def background_layers():
    """Raster layers the operator has switched on (their basemap), top to bottom."""
    root = QgsProject.instance().layerTreeRoot()
    return [l for l in root.checkedLayers() if l.type() == Qgis.LayerType.Raster]


class QuickReviewDialog(QDialog):
    PAGE = 12
    COLS = 4

    def __init__(self, controller, dest_crs=None, parent=None):
        super().__init__(parent)
        self.c = controller
        self.dest_crs = dest_crs if dest_crs is not None and dest_crs.isValid() else QgsCoordinateReferenceSystem("EPSG:3857")
        self.setWindowTitle("Quick check")
        self.keys = []
        self.tiles = {}
        self.jobs = []
        self.skipped = set()
        self.page_started = time.monotonic()
        self.totals = {"approved": 0, "held": 0}
        self.background = background_layers()

        lay = QVBoxLayout(self)
        hint = QLabel("Check each yellow outline against the background. Click a tile (or press its number) "
                      "if it is wrong; then approve the rest in one go. Wrong ones stay in the normal queue.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        if not self.background:
            warn = QLabel("No background map is switched on, so tiles show outlines only. "
                          "Turn on your satellite layer in the Layers panel for a real check.")
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#a8640f")
            lay.addWidget(warn)
        self.grid = QGridLayout()
        self.grid.setSpacing(8)
        lay.addLayout(self.grid)
        bar = QHBoxLayout()
        self.status = QLabel("")
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.setDefault(True)
        self.skip_btn = QPushButton("Skip this page")
        close_btn = QPushButton("Close")
        bar.addWidget(self.status, 1)
        bar.addWidget(self.skip_btn)
        bar.addWidget(self.approve_btn)
        bar.addWidget(close_btn)
        lay.addLayout(bar)
        self.approve_btn.clicked.connect(self.approve_unmarked)
        self.skip_btn.clicked.connect(self.skip_page)
        close_btn.clicked.connect(self.accept)
        self.load_page()

    # ------------------------------------------------------------------
    def _cancel_jobs(self):
        for job, _o, _p in self.jobs:
            if job.isActive():
                job.cancel()
        self.jobs = []

    def done(self, result):  # dialog closing: stop any render still running
        self._cancel_jobs()
        super().done(result)

    @staticmethod
    def _show(job, tile):
        try:
            tile.image.setPixmap(QPixmap.fromImage(job.renderedImage()))
        except RuntimeError:
            pass  # the tile was replaced before its render finished

    def load_page(self):
        self._cancel_jobs()
        for t in self.tiles.values():
            self.grid.removeWidget(t)
            t.deleteLater()
        self.tiles = {}
        self.keys = self.c.quick_check_keys(self.PAGE, exclude=self.skipped)
        self.page_started = time.monotonic()
        for i, key in enumerate(self.keys):
            r = self.c.records[key]
            d = self.c.details.get(key) or {}
            name = (r.get("fields", {}).get("building_name") or "")[:24]
            conf = r.get("confidence")
            unsure = d.get("decision") != C.IDENTITY_CLEAR
            caption = "%s · %s · %s%s" % (r["survey_id"], name or "-",
                                                    "conf %.2f" % conf if conf is not None else "conf -",
                                                    " · unsure" if unsure else "")
            tile = _Tile(i + 1, caption, self)
            tile.clicked.connect(lambda k=key: self.toggle(k))
            self.grid.addWidget(tile, i // self.COLS, i % self.COLS)
            self.tiles[key] = tile
            self._render(key, tile)
        self._update_status()

    def _render(self, key, tile):
        r = self.c.records[key]
        b = self.c.store.building(r["survey_id"])
        if b is None:
            return
        poly = QgsGeometry(b.geometry())
        pt = QgsGeometry.fromPointXY(QgsPointXY(r["longitude"], r["latitude"]))
        outline = _memory_layer("Polygon", poly, QgsFillSymbol.createSimple(
            {"color": "0,0,0,0", "outline_color": "255,215,0,255", "outline_width": "0.9"}))
        point = _memory_layer("Point", pt, QgsMarkerSymbol.createSimple(
            {"name": "circle", "color": "230,40,40", "outline_color": "255,255,255", "size": "2.6"}))
        tr = QgsCoordinateTransform(WGS84, self.dest_crs, QgsProject.instance())
        ext = tr.transformBoundingBox(poly.boundingBox())
        ext.combineExtentWith(tr.transform(pt.asPoint()))
        side = max(ext.width(), ext.height(), 1e-9)
        metres = self.dest_crs.mapUnits() == Qgis.DistanceUnit.Meters
        side = max(side * 2.2, 55.0 if metres else 0.0005)
        cx, cy = ext.center().x(), ext.center().y()
        aspect = TILE_W / float(TILE_H)
        half_w, half_h = side * aspect / 2.0, side / 2.0
        ms = QgsMapSettings()
        ms.setDestinationCrs(self.dest_crs)
        ms.setLayers([point, outline] + self.background)
        ms.setExtent(QgsRectangle(cx - half_w, cy - half_h, cx + half_w, cy + half_h))
        ms.setOutputSize(QSize(TILE_W, TILE_H))
        ms.setBackgroundColor(QColor(235, 238, 236))
        job = QgsMapRendererParallelJob(ms)
        job.finished.connect(lambda j=job, t=tile: self._show(j, t))
        # keep the memory layers alive for the duration of the render
        self.jobs.append((job, outline, point))
        job.start()

    # ------------------------------------------------------------------
    def toggle(self, key):
        t = self.tiles.get(key)
        if t is not None:
            t.set_marked(not t.marked)
        self._update_status()

    def marked(self):
        return [k for k, t in self.tiles.items() if t.marked]

    def _update_status(self):
        n = len(self.keys)
        m = len(self.marked())
        self.approve_btn.setText("Approve %d unmarked" % (n - m))
        self.approve_btn.setEnabled(n - m > 0)
        if not n:
            self.status.setText("Nothing left to quick-check. Approved %d, kept %d for normal review."
                                % (self.totals["approved"], self.totals["held"]))
        else:
            self.status.setText("%d on this page, %d marked wrong" % (n, m))

    def approve_unmarked(self):
        marked = set(self.marked())
        ok = [k for k in self.keys if k not in marked]
        if not ok:
            return
        res = self.c.approve_many(ok, time.monotonic() - self.page_started)
        self.totals["approved"] += len(res["approved"])
        self.totals["held"] += len(res["held"]) + len(marked)
        self.skipped.update(marked)
        self.skipped.update(k for k, _ in res["held"])
        self.load_page()

    def skip_page(self):
        self.skipped.update(self.keys)
        self.load_page()

    def keyPressEvent(self, e):  # noqa: N802
        k = e.key()
        digits = {getattr(Qt.Key, "Key_%d" % d): (d if d else 10) for d in range(10)}
        if k in digits and digits[k] <= len(self.keys):
            self.toggle(self.keys[digits[k] - 1])
            return
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.approve_unmarked()
            return
        super().keyPressEvent(e)

    def sizeHint(self):  # noqa: N802
        return QSize(self.COLS * (TILE_W + 16) + 30, 3 * (TILE_H + 60) + 120)
