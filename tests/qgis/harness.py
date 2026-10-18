"""Offscreen QGIS harness: QgsApplication + a minimal iface for running the plugin in tests."""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "qgis_plugin"))

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

_APP = None


def app():
    global _APP
    if _APP is None:
        profile = tempfile.mkdtemp(prefix="bpa_qgis_profile_")
        _APP = QgsApplication([], True, profile)
        _APP.initQgis()
        # Standalone QgsSettings live in one shared test file, not in the temp profile: start every run
        # from the plugin defaults so one test's settings (sandbox config, learning) never leak into the next.
        from qgis.core import QgsSettings
        QgsSettings().remove("building_polygon_automation")
        _no_modal_dialogs()
    return _APP


def _no_modal_dialogs():
    """Offscreen nobody can click a message box: answer No / OK and log it instead of hanging."""
    from qgis.PyQt.QtWidgets import QMessageBox

    def answer(kind, default):
        def box(parent, title, text, *args, **kwargs):
            print("   [dialog %s] %s: %s -> %s" % (kind, title, str(text).splitlines()[0], default.name))
            return default
        return staticmethod(box)

    no, ok = QMessageBox.StandardButton.No, QMessageBox.StandardButton.Ok
    QMessageBox.question = answer("question", no)
    QMessageBox.information = answer("information", ok)
    QMessageBox.warning = answer("warning", ok)
    QMessageBox.critical = answer("critical", ok)


class FakeBar(object):
    def __init__(self):
        self.messages = []

    def pushMessage(self, title, text, level=0, duration=0):  # noqa: N802
        self.messages.append((title, text, int(level)))
        print("   [bar %s] %s" % (int(level), text.splitlines()[0] if text else ""))


class FakeIface(object):
    def __init__(self):
        from qgis.PyQt.QtGui import QAction
        self.main = QMainWindow()
        self.canvas = QgsMapCanvas(self.main)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.canvas.resize(800, 600)
        self.bar = FakeBar()
        self._vertex = QAction("vertex", self.main)
        self._add = QAction("add", self.main)
        self._move = QAction("move", self.main)
        self._split = QAction("split", self.main)
        self.active = None
        self.actions = []

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def mainWindow(self):  # noqa: N802
        return self.main

    def messageBar(self):  # noqa: N802
        return self.bar

    def addToolBarIcon(self, a):  # noqa: N802
        self.actions.append(a)

    def removeToolBarIcon(self, a):  # noqa: N802
        pass

    def addPluginToVectorMenu(self, m, a):  # noqa: N802
        pass

    def removePluginVectorMenu(self, m, a):  # noqa: N802
        pass

    def registerMainWindowAction(self, a, k):  # noqa: N802
        return True

    def unregisterMainWindowAction(self, a):  # noqa: N802
        return True

    def addDockWidget(self, area, dock):  # noqa: N802
        self.main.addDockWidget(area, dock)

    def removeDockWidget(self, dock):  # noqa: N802
        self.main.removeDockWidget(dock)

    def setActiveLayer(self, layer):  # noqa: N802
        self.active = layer
        return True

    def actionVertexTool(self):  # noqa: N802
        return self._vertex

    def actionAddFeature(self):  # noqa: N802
        return self._add

    def actionMoveFeature(self):  # noqa: N802
        return self._move

    def actionSplitFeatures(self):  # noqa: N802
        return self._split


def pump(seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def wait_until(cond, timeout=180, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for %s" % what)


class Check(object):
    def __init__(self):
        self.passed = 0
        self.failed = []

    def __call__(self, cond, label):
        if cond:
            self.passed += 1
            print("  PASS", label)
        else:
            self.failed.append(label)
            print("  FAIL", label)

    def done(self):
        print("\n%d passed, %d failed" % (self.passed, len(self.failed)))
        for f in self.failed:
            print("   -", f)
        return 0 if not self.failed else 1
