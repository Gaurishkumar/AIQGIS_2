"""Review dock widget: batch controls, queue, evidence panel and review actions."""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (QAbstractItemView, QComboBox, QDockWidget, QHBoxLayout, QHeaderView, QLabel,
                                 QLineEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser,
                                 QToolButton, QVBoxLayout, QWidget)

from .bpa_common import constants as C
from .bpa_common import explain as X

# Light lane colours always get dark text: QGIS follows the Windows dark mode (white text),
# and white text on a pastel row is unreadable.
LANE_COLOURS = {
    X.LANE_SURE: "#d9f2df", X.LANE_CHECK: "#fff0c7", X.LANE_DRAW: "#ffd9d9", X.LANE_DONE: "#e3ebf7",
    X.LANE_REJECTED: "#e6e6e6", X.LANE_INVALID: "#ffd6dc", X.LANE_FAILED: "#f1dcff",
}
TEXT_ON_COLOUR = "#1b1b1b"
STATE_WORDS = {
    C.IMPORTED: "imported", C.QUEUED: "queued", C.MATCHING: "processing", C.FOOTPRINT_READY: "processing",
    C.NEEDS_IMAGERY: "processing", C.NO_IMAGERY_AVAILABLE: "processing", C.SEGMENTING: "processing",
    C.POLYGONIZING: "processing", C.VALIDATING: "processing", C.REVIEW: "to review", C.MANUAL: "to draw",
    C.AUTO_ACCEPTED: "auto-accepted", C.APPROVED: "approved", C.APPROVED_MANUAL: "drawn + approved",
    C.REJECTED: "rejected", C.EXPORTED: "exported", C.INVALID: "invalid row", C.FAILED: "failed",
}
COLUMNS = ["Survey ID", "Building", "Lane", "What the system found", "Conf", "Status"]
# "All" first: every imported row is visible straight away, including rows still being processed.
FILTERS = (("All records", "all"), ("Open work", "open"), ("Sure - approve quickly", "sure"),
           ("Check - look first", "check"), ("Draw yourself", "draw"), ("Approved", "approved"),
           ("Rejected", "rejected"), ("Invalid / failed", "problems"))
# The evidence panel is a white "page" in both light and dark QGIS themes.
PAPER = "QTextBrowser { background-color: #ffffff; color: #1b1b1b; }"


class ReviewDock(QDockWidget):
    action_requested = pyqtSignal(str)
    pick_requested = pyqtSignal(str)

    def __init__(self, controller, settings, parent=None):
        super().__init__("Building Polygon Automation", parent)
        self.setObjectName("BuildingPolygonAutomationDock")
        self.c = controller
        self.settings = settings
        self._rows = {}
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)

        top = QHBoxLayout()
        self.btn_import = QPushButton("Import Excel…")
        self.btn_import.setToolTip("Import a survey workbook (many buildings)")
        self.btn_single = QPushButton("Single building…")
        self.btn_single.setToolTip("Enter one building by its location (paste coordinates, a map link, or pick on the map)")
        self.btn_open = QPushButton("Open batch…")
        self.btn_imagery = QPushButton("Client imagery…")
        self.btn_imagery.setToolTip("Add imagery the client supplied (GeoTIFF / orthomosaic) so the AI can trace outlines")
        self.btn_settings = QToolButton()
        self.btn_settings.setText("⚙")
        self.btn_settings.setToolTip("Settings")
        for b in (self.btn_import, self.btn_single, self.btn_open, self.btn_imagery):
            top.addWidget(b)
        top.addStretch(1)
        top.addWidget(self.btn_settings)
        lay.addLayout(top)

        run = QHBoxLayout()
        self.btn_process = QPushButton("Process")
        self.btn_export = QPushButton("Export…")
        self.btn_report = QPushButton("Report")
        self.btn_report.setToolTip("Seconds per record, records per day and first-time-right rate")
        for b in (self.btn_process, self.btn_export, self.btn_report):
            run.addWidget(b)
        self.worker_label = QLabel("● worker stopped")
        self.worker_label.setStyleSheet("color:#888")
        run.addSpacing(8)
        run.addWidget(self.worker_label)
        run.addStretch(1)
        lay.addLayout(run)

        self.counts_label = QLabel("No batch")
        self.counts_label.setWordWrap(True)
        lay.addWidget(self.counts_label)

        filt = QHBoxLayout()
        filt.addWidget(QLabel("Show"))
        self.filter = QComboBox()
        for label, key in FILTERS:
            self.filter.addItem(label, key)
        filt.addWidget(self.filter)
        self.shown_label = QLabel("")
        filt.addWidget(self.shown_label)
        filt.addStretch(1)
        filt.addWidget(QLabel("Operator"))
        self.operator = QLineEdit(settings.get("operator_id") or "")
        self.operator.setPlaceholderText("your ID")
        self.operator.setMaximumWidth(120)
        filt.addWidget(self.operator)
        lay.addLayout(filt)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)              # one line per record; full text in tooltip + evidence
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setMinimumSectionSize(36)
        for col, width in ((0, 92), (1, 118), (2, 52), (4, 40), (5, 72)):
            self.table.setColumnWidth(col, width)
        split.addWidget(self.table)
        self.evidence = QTextBrowser()
        self.evidence.setStyleSheet(PAPER)
        self.evidence.setOpenExternalLinks(False)
        self.evidence.setOpenLinks(False)
        self.evidence.anchorClicked.connect(self._link)
        split.addWidget(self.evidence)
        split.setSizes([260, 320])
        lay.addWidget(split, 1)

        self.buttons = {}
        rows = (
            (("approve", "Approve (A)", "The outline is right: approve it and compute the corrected coordinate"),
             ("quick", "Quick check (Q)", "Check up to 12 proposals at once over your background map"),
             ("pick", "Pick (C)", "Wrong building: click the right dashed outline"),
             ("reject", "Reject (R)", "Reject with a reason code"),
             ("next", "Next (N)", "Go to the next open record")),
            (("shift", "Shift (S)", "Outline right but offset: drag it onto the roof, Ctrl+S, then A"),
             ("split", "Split (X)", "Outline covers several buildings: draw a cut line, Ctrl+S; the part with the point is kept"),
             ("edit", "Edit (E)", "Move individual corners with snapping; Ctrl+S, then A"),
             ("box", "Box (B)", "Missing building: 3 clicks - two corners along a wall, then the width"),
             ("draw", "Draw (M)", "Any shape: click every corner, right-click, Ctrl+S"),
             ("fix", "Fix (F)", "AI re-trace on licensed imagery: click inside the roof")),
        )
        for row in rows:
            line = QHBoxLayout()
            for code, label, tip in row:
                b = QPushButton(label)
                b.setToolTip(tip)
                b.clicked.connect(lambda _=False, c=code: self.action_requested.emit(c))
                self.buttons[code] = b
                line.addWidget(b)
            lay.addLayout(line)
        self.setWidget(w)

        self.filter.currentIndexChanged.connect(lambda _: self.refresh_table())
        self.table.itemSelectionChanged.connect(self._selected)
        self.operator.editingFinished.connect(lambda: settings.set("operator_id", self.operator.text().strip()))
        controller.records_changed.connect(self.refresh_table)
        controller.record_updated.connect(self.update_row)
        controller.current_changed.connect(self.show_record)
        controller.counts_changed.connect(self.show_counts)
        controller.worker_state.connect(self.show_worker)

    # ------------------------------------------------------------------
    def refresh_table(self):
        keys = self.c.queue(self.filter.currentData())
        self._update_filter_counts(len(keys))
        self.table.blockSignals(True)
        self.table.setRowCount(len(keys))
        self._rows = {}
        for i, k in enumerate(keys):
            self._rows[k] = i
            self._fill(i, k)
        self.table.blockSignals(False)
        if self.c.current in self._rows:
            self.table.selectRow(self._rows[self.c.current])

    def _update_filter_counts(self, shown):
        """Put the record count of every filter in the drop-down and say how many rows are listed."""
        total = len(self.c.records)
        counts = self.c.filter_counts() if total else {}
        self.filter.blockSignals(True)
        for i, (label, key) in enumerate(FILTERS):
            self.filter.setItemText(i, "%s (%d)" % (label, counts.get(key, 0)))
        self.filter.blockSignals(False)
        self.shown_label.setText("showing %d of %d" % (shown, total) if total else "")

    def _fill(self, i, key):
        r = self.c.records[key]
        e = self.c.explanation(key)
        vals = [r["survey_id"], r.get("fields", {}).get("building_name", ""), e["label"], e["short"],
                "" if r.get("confidence") is None else "%.2f" % r["confidence"],
                STATE_WORDS.get(r.get("state"), (r.get("state") or "").lower())]
        colour = LANE_COLOURS.get(e["lane"])
        bg = QBrush(QColor(colour)) if colour else None
        fg = QBrush(QColor(TEXT_ON_COLOUR)) if colour else None
        for col, v in enumerate(vals):
            item = QTableWidgetItem(str(v))
            item.setData(Qt.ItemDataRole.UserRole, key)
            if col == 3:
                item.setToolTip("%s\n\nWhat to do: %s" % ("\n".join(e["why"] + e["doubts"]) or e["headline"], e["todo"]))
            if bg is not None:
                item.setBackground(bg)
                item.setForeground(fg)
            self.table.setItem(i, col, item)

    def update_row(self, key):
        if key in self._rows:
            self._fill(self._rows[key], key)
            self._update_filter_counts(self.table.rowCount())
        else:
            self.refresh_table()

    def _link(self, url):
        """'use' links in the candidate table: switch to that building with one click."""
        text = url.toString()
        if text.startswith("pick:"):
            self.pick_requested.emit(text[len("pick:"):])

    def _selected(self):
        items = self.table.selectedItems()
        if items:
            key = items[0].data(Qt.ItemDataRole.UserRole)
            if key != self.c.current:
                self.c.select(key)

    def show_record(self, key):
        self.evidence.setHtml(self.c.evidence_html(key))
        if key in self._rows:
            self.table.blockSignals(True)
            self.table.selectRow(self._rows[key])
            self.table.blockSignals(False)
            self.table.scrollToItem(self.table.item(self._rows[key], 0))

    def show_counts(self, counts):
        parts = ["%s %d" % (k.replace("_", " ").lower(), v) for k, v in sorted(counts.items())
                 if isinstance(v, int) and k not in ("processed", "total")]
        extra = ""
        if counts.get("total"):
            extra = "  —  worker %s/%s (%s)" % (counts.get("processed"), counts.get("total"), counts.get("status"))
        self.counts_label.setText(" · ".join(parts) + extra)

    def show_worker(self, state):
        colours = {"ready": "#1a8f3c", "starting": "#b06000", "error": "#d33", "stopped": "#888"}
        self.worker_label.setText("● worker %s" % state)
        self.worker_label.setStyleSheet("color:%s" % colours.get(state, "#888"))
