"""Plugin settings stored in QgsSettings (per QGIS profile)."""

import os
import secrets

from qgis.core import QgsSettings

PREFIX = "building_polygon_automation/"

DEFAULTS = {
    "project_dir": "",
    "worker_python": "",
    "config_path": "",
    "batches_dir": "",
    "operator_id": "",
    "port": 8765,
    "auto_start_worker": True,
    "auto_next": True,
    "avoid_overlap": True,
    "load_model": True,
    "learn_from_approved": False,
}


class Settings(object):
    def __init__(self):
        self.s = QgsSettings()

    def get(self, key):
        default = DEFAULTS.get(key)
        v = self.s.value(PREFIX + key, default)
        if isinstance(default, bool):
            return v in (True, "true", "True", "1", 1)
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default
        return v if v is not None else default

    def set(self, key, value):
        self.s.setValue(PREFIX + key, value)

    # -- derived paths ------------------------------------------------------
    def project_dir(self):
        d = self.get("project_dir") or os.environ.get("BPA_HOME", "")
        return os.path.normpath(d) if d else ""

    def worker_python(self):
        p = self.get("worker_python")
        if p:
            return p
        d = self.project_dir()
        for cand in (os.path.join(d, "worker", ".venv", "Scripts", "python.exe"),
                     os.path.join(d, "worker", ".venv", "bin", "python")):
            if d and os.path.isfile(cand):
                return cand
        return ""

    def worker_dir(self):
        """Folder containing the bpa_worker package."""
        d = self.project_dir()
        if d and os.path.isdir(os.path.join(d, "worker", "bpa_worker")):
            return os.path.join(d, "worker")
        py = self.worker_python()
        if py:  # <worker>/.venv/Scripts/python.exe
            cand = os.path.dirname(os.path.dirname(os.path.dirname(py)))
            if os.path.isdir(os.path.join(cand, "bpa_worker")):
                return cand
        return os.path.join(d, "worker") if d else ""

    def config_path(self):
        p = self.get("config_path")
        if p:
            return p
        d = self.project_dir()
        return os.path.join(d, "config", "config.yaml") if d else ""

    def batches_dir(self):
        p = self.get("batches_dir")
        if p:
            return p
        d = self.project_dir()
        return os.path.join(d, "data", "batches") if d else ""

    def problems(self):
        """Human-readable list of missing settings (empty when ready)."""
        out = []
        if not self.project_dir() or not os.path.isdir(self.project_dir()):
            out.append("Project folder is not set (Settings)")
        if not os.path.isfile(self.config_path() or ""):
            out.append("Config file not found: %s" % (self.config_path() or "-"))
        if not os.path.isfile(self.worker_python() or ""):
            out.append("Worker Python not found: %s" % (self.worker_python() or "-"))
        return out


def new_token():
    return secrets.token_urlsafe(24)
