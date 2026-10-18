"""QGIS plugin class: toolbar action, dock, shortcuts (A/E/F/C/M/R/N) and map tools."""

import os

from qgis.core import Qgis
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon, QKeySequence
from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox

from .compat import QAction
from .controller import Controller, NeedsConfirmation, UserError
from .dialogs import RejectDialog, SettingsDialog
from .dock import ReviewDock
from .map_tools import BoxTool, ClickToFixTool, PickCandidateTool
from .settings import Settings

SHORTCUTS = (("approve", "A"), ("edit", "E"), ("fix", "F"), ("pick", "C"), ("draw", "M"), ("reject", "R"),
             ("next", "N"), ("shift", "S"), ("split", "X"), ("box", "B"), ("quick", "Q"))


class BuildingPolygonAutomationPlugin(object):
    def __init__(self, iface):
        self.iface = iface
        self.settings = Settings()
        self.controller = None
        self.dock = None
        self.action = None
        self.shortcut_actions = []
        self.fix_tool = None
        self.pick_tool = None
        self.box_tool = None
        self.single_dialog = None

    # ------------------------------------------------------------------
    def initGui(self):  # noqa: N802
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        self.action = QAction(icon, "Building Polygon Automation", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToVectorMenu("&Building Polygon Automation", self.action)
        for code, key in SHORTCUTS:
            a = QAction("BPA: %s" % code.title(), self.iface.mainWindow())
            a.setShortcut(QKeySequence(key))
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            a.setEnabled(False)
            a.triggered.connect(lambda _=False, c=code: self.run_action(c))
            self.iface.registerMainWindowAction(a, key)
            self.iface.mainWindow().addAction(a)
            self.shortcut_actions.append(a)

    def unload(self):
        if self.controller is not None:
            try:
                self.controller.stop_worker()
            except Exception:
                pass
        for a in self.shortcut_actions:
            self.iface.unregisterMainWindowAction(a)
            self.iface.mainWindow().removeAction(a)
        self.shortcut_actions = []
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginVectorMenu("&Building Polygon Automation", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    # ------------------------------------------------------------------
    def _ensure_ui(self):
        if self.dock is not None:
            return
        self.controller = Controller(self.iface, self.settings)
        self.dock = ReviewDock(self.controller, self.settings, self.iface.mainWindow())
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.visibilityChanged.connect(self._visibility)
        self.dock.action_requested.connect(self.run_action)
        self.dock.pick_requested.connect(lambda cid: self._guard(self.controller.pick_candidate, cid))
        self.dock.btn_import.clicked.connect(self.import_excel)
        self.dock.btn_single.clicked.connect(self.single_building)
        self.dock.btn_imagery.clicked.connect(self.client_imagery)
        self.dock.btn_open.clicked.connect(self.open_batch)
        self.dock.btn_process.clicked.connect(lambda: self._guard(self.controller.process))
        self.dock.btn_export.clicked.connect(self.export)
        self.dock.btn_report.clicked.connect(self.show_report)
        self.dock.btn_settings.clicked.connect(self.edit_settings)
        self.controller.message.connect(self._message)
        self.controller.reference_needed.connect(self._reference_needed)
        canvas = self.iface.mapCanvas()
        self.fix_tool = ClickToFixTool(canvas, lambda p, n, b: self._guard(self.controller.click_to_fix, p, n, b))
        self.pick_tool = PickCandidateTool(canvas, self._picked)
        self.box_tool = BoxTool(canvas, self._boxed)

    def toggle(self, checked):
        self._ensure_ui()
        self.dock.setVisible(checked)

    def _visibility(self, visible):
        self.action.setChecked(visible)
        for a in self.shortcut_actions:
            a.setEnabled(visible)

    def _message(self, text, level):
        self.iface.messageBar().pushMessage("Building polygons", text, Qgis.MessageLevel(level),
                                            8 if level != int(Qgis.MessageLevel.Critical) else 0)

    def _guard(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except UserError as exc:
            self._message(str(exc), int(Qgis.MessageLevel.Warning))
        except NeedsConfirmation:
            raise
        except Exception as exc:  # unexpected: show, never crash QGIS
            import traceback
            self._message("Unexpected error: %s" % exc, int(Qgis.MessageLevel.Critical))
            traceback.print_exc()

    # ------------------------------------------------------------------
    def _worker_settings(self):
        s = self.settings
        return (s.project_dir(), s.config_path(), s.worker_python(), s.get("port"), s.get("load_model"))

    def edit_settings(self):
        self._ensure_ui()
        before = self._worker_settings()
        dlg = SettingsDialog(self.settings, self.iface.mainWindow())
        if dlg.exec():
            self.dock.operator.setText(self.settings.get("operator_id") or "")
            if self._worker_settings() != before and self.controller.worker_ready:
                # A running worker keeps the old project's map data and imagery; restart it.
                self.controller.stop_worker()
                self._message("Project settings changed: the worker will restart with them on the next Process.",
                              int(Qgis.MessageLevel.Info))

    def import_excel(self):
        if self.settings.problems():
            self._message("Complete the settings first: %s" % "; ".join(self.settings.problems()),
                          int(Qgis.MessageLevel.Warning))
            self.edit_settings()
        path, _ = QFileDialog.getOpenFileName(self.iface.mainWindow(), "Survey workbook", "",
                                              "Excel workbook (*.xlsx *.xlsm)")
        if path:
            self._guard(self.controller.import_excel, path)
            if self.controller.batch and self.settings.get("auto_start_worker"):
                self._guard(self.controller.ensure_worker)

    def single_building(self):
        """Enter one building (or a few) by location instead of importing an Excel workbook."""
        self._ensure_ui()
        if self.settings.problems():
            self._message("Complete the settings first: %s" % "; ".join(self.settings.problems()),
                          int(Qgis.MessageLevel.Warning))
            self.edit_settings()
            return
        if self.single_dialog is None:
            from .bpa_common.config import get, load_config
            from .entry_dialog import SingleEntryDialog
            try:
                bounds = get(load_config(self.settings.config_path()), "project.country_bounds", {})
            except Exception:
                bounds = {}
            self.single_dialog = SingleEntryDialog(self.iface, bounds, self._single_entries, self.iface.mainWindow())
        self.single_dialog.show()
        self.single_dialog.raise_()

    def _single_entries(self, entries):
        rep = self._guard(self.controller.import_entries, entries)
        if rep and self.controller.batch:
            self._guard(self.controller.process)

    def client_imagery(self):
        """Check imagery the client supplied and use it for AI tracing (with their recorded permission)."""
        self._ensure_ui()
        c = self.controller
        if self._guard(c.operator_id) is None:
            return
        from .bpa_common.config import get, load_config
        from .imagery_dialog import ImageryDialog, build_mosaic
        try:
            max_gsd = float(get(load_config(self.settings.config_path()), "imagery.max_gsd_m", 0.6))
        except Exception:
            max_gsd = 0.6
        dlg = ImageryDialog(c.survey_points(), self.settings.get("operator_id"), max_gsd, self.iface.mainWindow())
        if not dlg.exec():
            return
        spec = dlg.spec()
        usable = dlg.usable()
        if len(usable) > 1:   # tiles of one orthomosaic -> one seamless virtual mosaic
            vrt = build_mosaic(usable, os.path.join(self.settings.project_dir(), "data", "imagery", spec["id"] + ".vrt"))
            if vrt:
                spec["paths"] = [vrt]
        res = self._guard(c.add_imagery, spec, [i["path"] for i in dlg.viewing_only()])
        if not res or not res.get("registered"):
            return
        if not self.settings.get("load_model"):
            self.settings.set("load_model", True)
            c.stop_worker()
            self._message("The AI model is now switched on; the worker restarts with it on Process.",
                          int(Qgis.MessageLevel.Info))
        if c.batch and QMessageBox.question(
                self.iface.mainWindow(), "Client imagery",
                "Re-process the open records of this batch now, so the AI traces them on the new imagery?\n\n"
                "Approved and edited records are kept as they are.") == QMessageBox.StandardButton.Yes:
            self._guard(c.process)

    def open_batch(self):
        d = QFileDialog.getExistingDirectory(self.iface.mainWindow(), "Batch folder", self.settings.batches_dir())
        if d:
            self._guard(self.controller.open_batch, d)

    def export(self):
        if not self.controller or not self.controller.batch:
            self._message("Open a batch first", int(Qgis.MessageLevel.Warning))
            return
        default = os.path.join(self.controller.batch["batch_dir"], "output")
        path, _ = QFileDialog.getSaveFileName(self.iface.mainWindow(), "Export workbook copy", default,
                                              "Excel workbook (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        rep = self._guard(self.controller.export, path)
        if rep:
            QMessageBox.information(self.iface.mainWindow(), "Export finished",
                                    "Exported %d approved record(s).\nSkipped (not approved): %d\n\n%s\n%s" % (
                                        rep["exported"], len(rep["skipped"]), rep["output_path"],
                                        "\n".join(rep.get("delivery", []))))

    def _reference_needed(self, uncovered, total):
        ans = QMessageBox.question(
            self.iface.mainWindow(), "Reference data",
            "%d of %d survey points are outside the downloaded reference data.\n\n"
            "Download Overture buildings, roads and places for those areas now? (one-time, needs internet)\n\n"
            "Choose No to process anyway - uncovered records will fail with REFERENCE_NOT_COVERED." % (uncovered, total))
        if ans == QMessageBox.StandardButton.Yes:
            self._guard(self.controller.prepare_reference)
        else:
            self._guard(self.controller._submit)

    def _boxed(self, geom):
        self.iface.mapCanvas().unsetMapTool(self.box_tool)
        self._guard(self.controller.add_box, geom)

    def show_report(self):
        if not self.controller or not self.controller.batch:
            self._message("Open a batch first", int(Qgis.MessageLevel.Warning))
            return
        rep = self._guard(self.controller.productivity_report)
        if not rep:
            return
        o = rep["outcomes"]
        lines = [
            "Finished records: %d of %d" % (rep["records_finished"], rep["records_total"]),
            "Seconds per record (average): %s" % (rep["seconds_per_record_mean"] if rep["seconds_per_record_mean"] else "-"),
            "Projected records per %d-hour day: %s" % (rep["day_hours"], rep["projected_records_per_day"] or "-"),
            "",
            "Approved as proposed: %d" % o.get("approved as proposed", 0),
            "Outline fixed (shift / split / edit): %d" % o.get("outline fixed", 0),
            "Other building picked: %d" % o.get("other building picked", 0),
            "Drawn by operator: %d" % o.get("drawn by operator", 0),
            "Rejected: %d" % o.get("rejected", 0),
            "",
            "First time right: %s" % ("%.0f%%" % (rep["first_time_right_rate"] * 100) if rep["first_time_right_rate"] is not None else "-"),
            "Right building chosen: %s" % ("%.0f%%" % (rep["right_building_rate"] * 100) if rep["right_building_rate"] is not None else "-"),
            "",
            "Saved to: %s" % ", ".join(rep.get("files", [])),
        ]
        QMessageBox.information(self.iface.mainWindow(), "Productivity report", "\n".join(lines))

    def _picked(self, lon, lat):
        cid = self.controller.candidate_at(lon, lat)
        if cid is None:
            self._message("Click inside one of the dashed candidate outlines", int(Qgis.MessageLevel.Info))
            return
        self.iface.mapCanvas().unsetMapTool(self.pick_tool)
        self._guard(self.controller.pick_candidate, cid)

    def run_action(self, code):
        self._ensure_ui()
        c = self.controller
        if code == "approve":
            try:
                self._guard(c.approve)
            except NeedsConfirmation as exc:
                if QMessageBox.question(self.iface.mainWindow(), "Overlap", str(exc)) == QMessageBox.StandardButton.Yes:
                    self._guard(c.approve, None, True)
        elif code == "edit":
            self._guard(c.start_edit)
        elif code == "fix":
            if self._guard(c._require_current) is not None:
                d = c.details.get(c.current) or {}
                if not d.get("crop_path"):
                    self._message("No licensed imagery crop for this record - use Edit (E) or Draw (M)",
                                  int(Qgis.MessageLevel.Info))
                    return
                self.fix_tool.reset()
                self.iface.mapCanvas().setMapTool(self.fix_tool)
                self._message("Click-to-fix: left click inside the building, right click outside, drag a box; Esc clears",
                              int(Qgis.MessageLevel.Info))
        elif code == "pick":
            if self._guard(c._require_current) is not None:
                self.iface.mapCanvas().setMapTool(self.pick_tool)
                self._message("Click inside the candidate that is the right building", int(Qgis.MessageLevel.Info))
        elif code == "draw":
            self._guard(c.start_draw)
        elif code == "shift":
            self._guard(c.start_shift)
        elif code == "split":
            self._guard(c.start_split)
        elif code == "box":
            if self._guard(c._require_current) is not None and self._guard(c.operator_id) is not None:
                c._snapping()
                self.box_tool.reset()
                self.iface.mapCanvas().setMapTool(self.box_tool)
                self._message("Box: click two corners along one wall, then click to set the width (right-click cancels)",
                              int(Qgis.MessageLevel.Info))
        elif code == "quick":
            if not c.batch:
                self._message("Import or open a batch first", int(Qgis.MessageLevel.Warning))
                return
            if self._guard(c.operator_id) is None:
                return
            from .quick_review import QuickReviewDialog
            dlg = QuickReviewDialog(c, self.iface.mapCanvas().mapSettings().destinationCrs(), self.iface.mainWindow())
            dlg.exec()
            self.dock.refresh_table()
        elif code == "reject":
            key = self._guard(c._require_current)
            if key is not None:
                dlg = RejectDialog(c.records[key]["survey_id"], self.iface.mainWindow())
                if dlg.exec():
                    reason, note = dlg.values()
                    self._guard(c.reject, reason, note)
        elif code == "next":
            if c.next_open() is None:
                self._message("No open records left", int(Qgis.MessageLevel.Success))
