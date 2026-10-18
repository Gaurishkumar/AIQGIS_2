"""Custom map tools: click-to-fix (positive / negative points, drag = box) and pick-candidate."""

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject,
                       QgsRectangle)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _to_wgs84(canvas, point):
    tr = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), WGS84, QgsProject.instance())
    return tr.transform(point)


class ClickToFixTool(QgsMapTool):
    """Left click = positive point, right click = negative point, left drag = box.

    Every gesture calls ``on_prompts(positive, negative, box)`` with WGS84
    coordinates ([[lon, lat], ...], box = [W, S, E, N] or None). Esc clears.
    """

    DRAG_PX = 6

    def __init__(self, canvas, on_prompts):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_prompts = on_prompts
        self.pos, self.neg, self.box = [], [], None
        self.markers = []
        self._press = None
        self._press_map = None
        self._button = None
        self.band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
        self.band.setColor(QColor(255, 230, 0, 60))
        self.band.setStrokeColor(QColor(255, 200, 0))
        self.band.setWidth(2)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def reset(self):
        self.pos, self.neg, self.box = [], [], None
        for m in self.markers:
            self.canvas.scene().removeItem(m)
        self.markers = []
        self.band.reset(Qgis.GeometryType.Polygon)

    def _marker(self, point, positive):
        m = QgsVertexMarker(self.canvas)
        m.setCenter(point)
        m.setColor(QColor(0, 200, 0) if positive else QColor(230, 0, 0))
        m.setIconType(QgsVertexMarker.IconType.ICON_CROSS if positive else QgsVertexMarker.IconType.ICON_X)
        m.setIconSize(14)
        m.setPenWidth(3)
        self.markers.append(m)

    def canvasPressEvent(self, e):  # noqa: N802
        self._press = e.pixelPoint()
        self._press_map = e.mapPoint()
        self._button = e.button()

    def canvasMoveEvent(self, e):  # noqa: N802
        if self._press is None or self._button != Qt.MouseButton.LeftButton:
            return
        if (e.pixelPoint() - self._press).manhattanLength() >= self.DRAG_PX:
            rect = QgsRectangle(self._press_map, e.mapPoint())
            self.band.setToGeometry(QgsGeometry.fromRect(rect), None)

    def canvasReleaseEvent(self, e):  # noqa: N802
        if self._press is None:
            return
        dragged = (e.pixelPoint() - self._press).manhattanLength() >= self.DRAG_PX
        if self._button == Qt.MouseButton.LeftButton and dragged:
            a = _to_wgs84(self.canvas, self._press_map)
            b = _to_wgs84(self.canvas, e.mapPoint())
            self.box = [min(a.x(), b.x()), min(a.y(), b.y()), max(a.x(), b.x()), max(a.y(), b.y())]
        elif self._button == Qt.MouseButton.LeftButton:
            p = _to_wgs84(self.canvas, e.mapPoint())
            self.pos.append([p.x(), p.y()])
            self._marker(e.mapPoint(), True)
        elif self._button == Qt.MouseButton.RightButton:
            p = _to_wgs84(self.canvas, e.mapPoint())
            self.neg.append([p.x(), p.y()])
            self._marker(e.mapPoint(), False)
        self._press = None
        if self.pos or self.box:
            self.on_prompts(list(self.pos), list(self.neg), self.box)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.reset()

    def deactivate(self):
        self.reset()
        super().deactivate()


class PickCandidateTool(QgsMapTool):
    """Click inside a candidate polygon: calls ``on_pick(lon, lat)``."""

    def __init__(self, canvas, on_pick):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_pick = on_pick
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def canvasReleaseEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        p = _to_wgs84(self.canvas, e.mapPoint())
        self.on_pick(p.x(), p.y())


class BoxTool(QgsMapTool):
    """Draw a building as a rectangle with three clicks.

    Click 1 and 2: two corners along one wall. Move the mouse to set the width,
    click 3 to finish. Right-click or Esc cancels. Clicks snap to nearby
    vertices and edges (project snapping). The rectangle is built in the local
    UTM zone, so its corners are true right angles on the ground whatever the
    map projection. Calls ``on_box(QgsGeometry in EPSG:4326)``.
    """

    def __init__(self, canvas, on_box):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_box = on_box
        self.points = []           # clicked points, canvas CRS
        self.band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
        self.band.setColor(QColor(255, 200, 0, 50))
        self.band.setStrokeColor(QColor(230, 120, 0))
        self.band.setWidth(2)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def reset(self):
        self.points = []
        self.band.reset(Qgis.GeometryType.Polygon)

    def _rectangle(self, a, b, c):
        """Rectangle (EPSG:4326) from two wall corners and a width point, all in canvas CRS."""
        from .geometry_ops import utm_epsg
        from qgis.core import QgsPointXY
        src = self.canvas.mapSettings().destinationCrs()
        ll = _to_wgs84(self.canvas, a)
        utm = QgsCoordinateReferenceSystem("EPSG:%d" % utm_epsg(ll.x(), ll.y()))
        to_m = QgsCoordinateTransform(src, utm, QgsProject.instance())
        A, B, Cc = (to_m.transform(p) for p in (a, b, c))
        dx, dy = B.x() - A.x(), B.y() - A.y()
        length = (dx * dx + dy * dy) ** 0.5
        if length < 0.5:
            return None
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux
        w = (Cc.x() - A.x()) * nx + (Cc.y() - A.y()) * ny
        if abs(w) < 0.5:
            return None
        ring = [QgsPointXY(A.x(), A.y()), QgsPointXY(B.x(), B.y()),
                QgsPointXY(B.x() + nx * w, B.y() + ny * w), QgsPointXY(A.x() + nx * w, A.y() + ny * w),
                QgsPointXY(A.x(), A.y())]
        g = QgsGeometry.fromPolygonXY([ring])
        g.transform(QgsCoordinateTransform(utm, WGS84, QgsProject.instance()))
        return g

    def _preview(self, rect_wgs84):
        g = QgsGeometry(rect_wgs84)
        g.transform(QgsCoordinateTransform(WGS84, self.canvas.mapSettings().destinationCrs(), QgsProject.instance()))
        self.band.setToGeometry(g, None)

    def canvasMoveEvent(self, e):  # noqa: N802
        p = e.snapPoint()
        if len(self.points) == 1:
            self.band.setToGeometry(QgsGeometry.fromPolylineXY([self.points[0], p]), None)
        elif len(self.points) == 2:
            r = self._rectangle(self.points[0], self.points[1], p)
            if r is not None:
                self._preview(r)

    def canvasReleaseEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.RightButton:
            self.reset()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.points.append(e.snapPoint())
        if len(self.points) == 3:
            r = self._rectangle(*self.points)
            self.reset()
            if r is not None:
                self.on_box(r)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.reset()

    def deactivate(self):
        self.reset()
        super().deactivate()
