"""Starts and stops the local worker with QProcess (separate Python environment)."""

import collections
import os
import socket

from qgis.PyQt.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

from .worker_client import WorkerClient, WorkerError

# QGIS exports these for its own Python/GDAL/PROJ. Passing them to the worker's
# separate Python would load the wrong libraries or PROJ database.
_STRIP_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PROJ_LIB", "PROJ_DATA", "GDAL_DATA",
              "GDAL_DRIVER_PATH", "QT_PLUGIN_PATH", "QGIS_PREFIX_PATH", "O4W_QT_PREFIX",
              "O4W_QT_BINARIES", "O4W_QT_PLUGINS", "O4W_QT_LIBRARIES", "O4W_QT_TRANSLATIONS",
              "O4W_QT_HEADERS", "O4W_QT_DOC", "VSI_CACHE", "VSI_CACHE_SIZE")


def port_in_use(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


class WorkerProcess(QObject):
    log_line = pyqtSignal(str)
    stopped = pyqtSignal(int)

    def __init__(self, python_exe, worker_dir, config_path, port, token, load_model=True, parent=None):
        super().__init__(parent)
        self.python_exe = python_exe
        self.worker_dir = worker_dir
        self.config_path = config_path
        self.port = int(port)
        self.token = token
        self.load_model = load_model
        self.proc = None
        self.lines = collections.deque(maxlen=400)
        self.client = WorkerClient("http://127.0.0.1:%d" % self.port, token)

    def is_running(self):
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self):
        if self.is_running():
            return True
        if not os.path.isfile(self.python_exe):
            raise WorkerError("Worker Python not found: %s" % self.python_exe)
        if port_in_use(self.port):
            raise WorkerError("Port %d is already in use; stop the other process or change the port" % self.port)
        env = QProcessEnvironment.systemEnvironment()
        for k in _STRIP_ENV:
            env.remove(k)
        # Every PYTHON* variable belongs to QGIS's interpreter. PYTHONEXECUTABLE in
        # particular makes the worker's python.exe ignore its own venv.
        for k in list(env.keys()):
            if k.upper().startswith("PYTHON"):
                env.remove(k)
        env.insert("PYTHONPATH", self.worker_dir)
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("BPA_TOKEN", self.token)
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(self.worker_dir)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        args = ["-m", "bpa_worker", "--log-level", "INFO", "serve", "--config", self.config_path,
                "--port", str(self.port)]
        if not self.load_model:
            args.append("--no-model")
        self.proc.start(self.python_exe, args)
        if not self.proc.waitForStarted(10000):
            raise WorkerError("Worker process failed to start: %s" % self.proc.errorString())
        return True

    def _read(self):
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in data.splitlines():
            if line.strip():
                self.lines.append(line)
                self.log_line.emit(line)

    def _finished(self, code, status=None):
        self.stopped.emit(int(code))

    def log_tail(self, n=40):
        return "\n".join(list(self.lines)[-n:])

    def stop(self):
        if not self.is_running():
            return
        try:
            self.client.shutdown()
        except WorkerError:
            pass
        if not self.proc.waitForFinished(4000):
            self.proc.kill()
            self.proc.waitForFinished(2000)
