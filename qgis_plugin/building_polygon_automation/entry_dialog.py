"""Single-building entry: one (or a few) buildings by location instead of an Excel import."""

import datetime as dt

from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout)

from .bpa_common.entry import LocationError, parse_location
from .map_tools import PickCandidateTool

CATEGORIES = ["", "Residential Building", "Commercial Complex", "IT Park", "Community Centre", "Industrial",
              "Hospital", "School / College", "Hotel", "Shop / Showroom", "Bank Branch"]


class SingleEntryDialog(QDialog):
    """Non-modal, so the operator can click the building on the map while it is open.

    ``on_submit(entries)`` receives a list of dicts (survey_id, latitude, longitude,
    building_name, street, area, city, pincode, customer_name, building_category, floors).
    """

    def __init__(self, iface, bounds, on_submit, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.bounds = bounds or {}
        self.on_submit = on_submit
        self.entries = []
        self.marker = None
        self._prev_tool = None
        self.setWindowTitle("Single building")
        self.setMinimumWidth(480)

        self.location = QLineEdit()
        self.location.setPlaceholderText("19.07604, 72.87773   or paste a Google Maps link / 19°04'33.7\"N 72°52'39.8\"E")
        self.btn_pick = QPushButton("Pick on map")
        self.btn_pick.setToolTip("Click the building on the map (use your background map to find it)")
        loc = QHBoxLayout()
        loc.addWidget(self.location, 1)
        loc.addWidget(self.btn_pick)
        self.parsed = QLabel(" ")
        self.parsed.setWordWrap(True)
        self.survey_id = QLineEdit()
        self.building_name = QLineEdit()
        self.street = QLineEdit()
        self.area = QLineEdit()
        self.city = QLineEdit()
        self.pincode = QLineEdit()
        self.pincode.setMaximumWidth(90)
        self.customer = QLineEdit()
        self.customer.setPlaceholderText("optional - e.g. the bank / store being surveyed (helps find the building)")
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItems(CATEGORIES)
        self.floors = QSpinBox()
        self.floors.setRange(0, 200)
        self.floors.setSpecialValueText("unknown")

        form = QFormLayout()
        form.addRow("Location *", loc)
        form.addRow("", self.parsed)
        form.addRow("Survey ID", self.survey_id)
        form.addRow("Building name", self.building_name)
        addr = QHBoxLayout()
        for w, tip in ((self.street, "Street"), (self.area, "Area"), (self.city, "City")):
            w.setPlaceholderText(tip)
            addr.addWidget(w)
        self.pincode.setPlaceholderText("PIN")
        addr.addWidget(self.pincode)
        form.addRow("Address", addr)
        form.addRow("Customer", self.customer)
        cat = QHBoxLayout()
        cat.addWidget(self.category, 1)
        cat.addWidget(QLabel("Floors"))
        cat.addWidget(self.floors)
        form.addRow("Category", cat)

        self.btn_add = QPushButton("Add to list")
        self.btn_add.setToolTip("Keep this building and enter another; all of them are processed together")
        self.btn_remove = QPushButton("Remove")
        self.listing = QListWidget()
        self.listing.setMaximumHeight(110)
        lst = QHBoxLayout()
        lst.addWidget(self.btn_add)
        lst.addWidget(self.btn_remove)
        lst.addStretch(1)
        hint = QLabel("Only the location is required. A building name, address or customer makes the right "
                      "building easier to find. Tip: pick the point on the building itself, not on the road.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888")
        self.buttons = QDialogButtonBox()
        self.btn_process = self.buttons.addButton("Process", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.close)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(lst)
        lay.addWidget(self.listing)
        lay.addWidget(hint)
        lay.addWidget(self.buttons)

        self.location.textChanged.connect(self._check)
        self.btn_pick.clicked.connect(self._start_pick)
        self.btn_add.clicked.connect(self.add_current)
        self.btn_remove.clicked.connect(self._remove)
        self.pick_tool = PickCandidateTool(iface.mapCanvas(), self._picked)
        self.pick_tool.setCursor(Qt.CursorShape.CrossCursor)
        self._new_id()
        self._check()

    # ------------------------------------------------------------------
    def _new_id(self):
        self.survey_id.setText("SB-%s-%02d" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"), len(self.entries) + 1))

    def _check(self):
        try:
            r = parse_location(self.location.text(), self.bounds)
        except LocationError as exc:
            self.parsed.setText("<span style='color:#c33'>%s</span>" % exc if self.location.text().strip() else
                                "<span style='color:#888'>Paste a location or click Pick on map</span>")
            return None
        note = (" &nbsp;(%s)" % r["note"]) if r["note"] else ""
        self.parsed.setText("<span style='color:#2e8b4a'>&#10004; %.6f, %.6f</span>%s" % (r["lat"], r["lon"], note))
        self._show_marker(r["lat"], r["lon"])
        return r

    def _show_marker(self, lat, lon):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
        canvas = self.iface.mapCanvas()
        tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        p = tr.transform(QgsPointXY(lon, lat))
        if self.marker is None:
            self.marker = QgsVertexMarker(canvas)
            self.marker.setColor(QColor(230, 30, 30))
            self.marker.setIconType(QgsVertexMarker.IconType.ICON_CROSS)
            self.marker.setIconSize(18)
            self.marker.setPenWidth(3)
        self.marker.setCenter(p)

    def _start_pick(self):
        canvas = self.iface.mapCanvas()
        self._prev_tool = canvas.mapTool()
        canvas.setMapTool(self.pick_tool)
        self.parsed.setText("<span style='color:#888'>Click the building on the map…</span>")

    def _picked(self, lon, lat):
        canvas = self.iface.mapCanvas()
        canvas.unsetMapTool(self.pick_tool)
        if self._prev_tool is not None:
            canvas.setMapTool(self._prev_tool)
        self.location.setText("%.7f, %.7f" % (lat, lon))
        self.raise_()
        self.activateWindow()

    def current_entry(self):
        r = self._check()
        if r is None:
            return None
        e = {"survey_id": self.survey_id.text().strip() or None, "latitude": r["lat"], "longitude": r["lon"],
             "building_name": self.building_name.text().strip(), "street": self.street.text().strip(),
             "area": self.area.text().strip(), "city": self.city.text().strip(),
             "pincode": self.pincode.text().strip(), "customer_name": self.customer.text().strip(),
             "building_category": self.category.currentText().strip(),
             "floors": str(self.floors.value()) if self.floors.value() else ""}
        if not e["survey_id"]:
            e["survey_id"] = "SB-%s-%02d" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"), len(self.entries) + 1)
        return e

    def add_current(self):
        e = self.current_entry()
        if e is None:
            return False
        if any(x["survey_id"] == e["survey_id"] for x in self.entries):
            self.parsed.setText("<span style='color:#c33'>Survey ID %s is already in the list</span>" % e["survey_id"])
            return False
        self.entries.append(e)
        item = QListWidgetItem("%s  %s  (%.6f, %.6f)" % (e["survey_id"], e["building_name"] or "-", e["latitude"],
                                                          e["longitude"]))
        self.listing.addItem(item)
        for w in (self.location, self.building_name):
            w.clear()
        self._new_id()
        return True

    def _remove(self):
        row = self.listing.currentRow()
        if row >= 0:
            self.listing.takeItem(row)
            del self.entries[row]

    def submit(self):
        if self.location.text().strip() and not self.add_current():
            return
        if not self.entries:
            self.parsed.setText("<span style='color:#c33'>Enter a location first</span>")
            return
        entries, self.entries = list(self.entries), []
        self.listing.clear()
        self.close()
        self.on_submit(entries)

    def hideEvent(self, e):  # noqa: N802  (Close button, Esc and the window's X all end here)
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is self.pick_tool:
            canvas.unsetMapTool(self.pick_tool)
        if self.marker is not None:
            canvas.scene().removeItem(self.marker)
            self.marker = None
        super().hideEvent(e)
