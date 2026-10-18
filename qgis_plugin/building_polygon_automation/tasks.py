"""Background HTTP calls via QgsTask.

run() only performs the HTTP call; it never touches QgsProject, layers or
widgets. Results are applied in finished(), which QGIS calls on the main thread.
"""

from qgis.core import QgsApplication, QgsTask


class CallTask(QgsTask):
    def __init__(self, description, fn, on_done=None, on_error=None):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.fn = fn
        self.on_done = on_done
        self.on_error = on_error
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.fn()
            return True
        except Exception as exc:  # reported in finished() on the main thread
            self.error = exc
            return False

    def finished(self, ok):
        if ok:
            if self.on_done:
                self.on_done(self.result)
        elif self.on_error:
            self.on_error(self.error or RuntimeError("Task cancelled"))


class TaskRunner(object):
    """Keeps task references alive until they finish (QgsTask objects must not be GC'd)."""

    def __init__(self, synchronous=False):
        self.synchronous = synchronous
        self._tasks = set()

    def run(self, description, fn, on_done=None, on_error=None):
        if self.synchronous:  # used by the automated tests (no event loop)
            try:
                res = fn()
            except Exception as exc:
                if on_error:
                    on_error(exc)
                return None
            if on_done:
                on_done(res)
            return None
        task = CallTask(description, fn, on_done, on_error)
        self._tasks.add(task)
        task.taskCompleted.connect(lambda t=task: self._tasks.discard(t))
        task.taskTerminated.connect(lambda t=task: self._tasks.discard(t))
        QgsApplication.taskManager().addTask(task)
        return task

    def busy(self):
        return bool(self._tasks)
