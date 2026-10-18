"""HTTP client for the local worker. Pure Python (urllib), safe to call from QgsTask.run()."""

import json
import urllib.error
import urllib.parse
import urllib.request


class WorkerError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class WorkerClient(object):
    def __init__(self, base_url, token=None, timeout=60):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Never route localhost calls through a system proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, method, path, body=None, params=None, timeout=None):
        url = self.base + path
        if params:
            q = {k: v for k, v in params.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-BPA-Token", self.token)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except Exception:
                detail = exc.reason
            raise WorkerError("%s %s -> %s: %s" % (method, path, exc.code, detail), exc.code)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            raise WorkerError("Worker not reachable at %s (%s)" % (self.base, getattr(exc, "reason", exc)))

    # -- API ------------------------------------------------------------------
    def health(self, timeout=5):
        return self._req("GET", "/health", timeout=timeout)

    def create_batch(self, payload):
        return self._req("POST", "/batches", payload, timeout=120)

    def create_batch_from_excel(self, workbook_path, batch_dir, batch_id=None, config_path=None):
        return self._req("POST", "/batches/from-excel", {"workbook_path": workbook_path, "batch_dir": batch_dir,
                                                         "batch_id": batch_id, "config_path": config_path},
                         timeout=300)

    def batch(self, batch_id):
        return self._req("GET", "/batches/%s" % urllib.parse.quote(batch_id))

    def batch_records(self, batch_id, only_final=False):
        return self._req("GET", "/batches/%s/records" % urllib.parse.quote(batch_id),
                         params={"only_final": "true" if only_final else None}, timeout=180)

    def retry_failed(self, batch_id):
        return self._req("POST", "/batches/%s/retry" % urllib.parse.quote(batch_id), {})

    def record(self, survey_id, batch_id=None):
        return self._req("GET", "/records/%s" % urllib.parse.quote(survey_id), params={"batch_id": batch_id})

    def resegment(self, survey_id, batch_id, positive, negative, box=None):
        return self._req("POST", "/records/%s/resegment" % urllib.parse.quote(survey_id),
                         {"positive_points": positive, "negative_points": negative, "box": box},
                         params={"batch_id": batch_id}, timeout=120)

    def select_candidate(self, survey_id, batch_id, candidate_id):
        return self._req("POST", "/records/%s/select-candidate" % urllib.parse.quote(survey_id),
                         {"candidate_id": candidate_id}, params={"batch_id": batch_id}, timeout=300)

    def coverage(self, points):
        return self._req("POST", "/reference/coverage", {"points": points})

    def prepare_reference(self, points, refresh=False):
        return self._req("POST", "/reference/prepare", {"points": points, "refresh": refresh})

    def prepare_status(self):
        return self._req("GET", "/reference/prepare")

    def learn(self, review_gpkg):
        return self._req("POST", "/reference/learn", {"review_gpkg": review_gpkg}, timeout=300)

    def shutdown(self):
        return self._req("POST", "/shutdown", {}, timeout=5)
