"""Settings and reject dialogs."""

import os

from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
                                 QVBoxLayout, QWidget)

from .bpa_common import constants as C


class _PathEdit(QWidget):
    def __init__(self, value, directory=False, filt="", parent=None):
        super().__init__(parent)
        self.directory = directory
        self.filt = filt
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(value or "")
        btn = QPushButton("...")
        btn.setFixedWidth(28)
        btn.clicked.connect(self._browse)
        lay.addWidget(self.edit)
        lay.addWidget(btn)

    def _browse(self):
        start = self.edit.text() or os.path.expanduser("~")
        if self.directory:
            p = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Choose file", start, self.filt)
        if p:
            self.edit.setText(os.path.normpath(p))

    def text(self):
        return self.edit.text().strip()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Building Polygon Automation - Settings")
        self.setMinimumWidth(560)
        form = QFormLayout()
        self.project = _PathEdit(settings.get("project_dir"), directory=True)
        self.python = _PathEdit(settings.get("worker_python"), filt="Python (python.exe python)")
        self.config = _PathEdit(settings.get("config_path"), filt="YAML (*.yaml *.yml)")
        self.batches = _PathEdit(settings.get("batches_dir"), directory=True)
        self.operator = QLineEdit(settings.get("operator_id") or "")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(settings.get("port")))
        self.auto_start = QCheckBox("Start the worker automatically")
        self.auto_start.setChecked(bool(settings.get("auto_start_worker")))
        self.auto_next = QCheckBox("Jump to the next record after approve / reject")
        self.auto_next.setChecked(bool(settings.get("auto_next")))
        self.avoid = QCheckBox("Avoid overlaps with other buildings while digitising")
        self.avoid.setChecked(bool(settings.get("avoid_overlap")))
        self.load_model = QCheckBox("Load SAM 2.1 in the worker (needs licensed imagery)")
        self.load_model.setChecked(bool(settings.get("load_model")))
        self.learn = QCheckBox("On export, teach the system from approved buildings (real work only, not demos)")
        self.learn.setChecked(bool(settings.get("learn_from_approved")))
        form.addRow("Project folder", self.project)
        form.addRow("Worker Python", self.python)
        form.addRow("Config file", self.config)
        form.addRow("Batches folder", self.batches)
        form.addRow("Operator ID", self.operator)
        form.addRow("Worker port", self.port)
        form.addRow("", self.auto_start)
        form.addRow("", self.auto_next)
        form.addRow("", self.avoid)
        form.addRow("", self.load_model)
        form.addRow("", self.learn)
        hint = QLabel("Empty paths are derived from the project folder: worker\\.venv, config\\config.yaml, data\\batches.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(hint)
        lay.addWidget(buttons)

    def accept(self):
        s = self.settings
        s.set("project_dir", self.project.text())
        s.set("worker_python", self.python.text())
        s.set("config_path", self.config.text())
        s.set("batches_dir", self.batches.text())
        s.set("operator_id", self.operator.text().strip())
        s.set("port", self.port.value())
        s.set("auto_start_worker", self.auto_start.isChecked())
        s.set("auto_next", self.auto_next.isChecked())
        s.set("avoid_overlap", self.avoid.isChecked())
        s.set("load_model", self.load_model.isChecked())
        s.set("learn_from_approved", self.learn.isChecked())
        super().accept()


class RejectDialog(QDialog):
    def __init__(self, survey_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reject %s" % survey_id)
        self.reason = QComboBox()
        for r in C.REJECT_REASONS:
            self.reason.addItem(r.replace("_", " ").title(), r)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("Optional note for the audit trail")
        self.note.setFixedHeight(70)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QFormLayout(self)
        lay.addRow("Reason", self.reason)
        lay.addRow("Note", self.note)
        lay.addRow(buttons)

    def values(self):
        return self.reason.currentData(), self.note.toPlainText().strip()
