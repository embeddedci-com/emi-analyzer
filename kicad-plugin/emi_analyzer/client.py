"""The bit of the EMI Analyzer API this plugin uses, over the standard library.

The app serves one fixed local identity with no credentials, and refuses anything that did
not arrive on the loopback interface (server/cmd/emi-local/guard.go). There is nothing to
sign in to and nothing to keep; a plain urllib request is the whole client.

Only the upload path is here. Everything else the user does -- the findings, the viewer, the
ESD simulation -- happens in the app's own pages, in the window, against the same server.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

#: A board file is read into memory and sent in one request; 200 MB is the app's own limit.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

DEFAULT_TIMEOUT = 30.0
#: Sending the board, and the app writing it to disk.
UPLOAD_TIMEOUT = 300.0


class ApiError(RuntimeError):
    """The app answered with an error status."""

    def __init__(self, status: int, message: str, path: str = ""):
        super().__init__(message)
        self.status = status
        self.path = path


class Client:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # ---- plumbing ----

    def _request(
        self,
        method: str,
        url: str,
        body: Optional[bytes] = None,
        content_type: str = "",
        timeout: Optional[float] = None,
    ) -> bytes:
        req = urllib.request.Request(url, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                payload = json.loads(e.read().decode("utf-8"))
                detail = payload.get("error") or payload.get("message") or ""
            except (ValueError, OSError, UnicodeDecodeError):
                pass
            raise ApiError(e.code, detail or f"the app answered {e.code}", url) from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ApiError(0, f"the app could not be reached ({e})", url) from e

    def _api(self, path: str) -> str:
        return f"{self.base}/api{path}"

    def get(self, path: str, timeout: Optional[float] = None) -> Any:
        return json.loads(self._request("GET", self._api(path), timeout=timeout) or b"null")

    def post(self, path: str, payload: Any, timeout: Optional[float] = None) -> Any:
        body = json.dumps(payload).encode("utf-8")
        raw = self._request("POST", self._api(path), body, "application/json", timeout)
        return json.loads(raw or b"null")

    # ---- what the plugin asks for ----

    def status(self) -> Dict[str, Any]:
        """Version, data folder and how the worker container is doing."""
        return self.get("/local/status")

    def lookup_board(self, sha256: str) -> Dict[str, Any]:
        """Has this app seen exactly these bytes before?"""
        return self.get("/emi/boards/lookup?sha256=" + urllib.parse.quote(sha256))

    def get_project(self, project_id: str) -> Dict[str, Any]:
        return self.get(f"/emi/projects/{urllib.parse.quote(project_id)}")

    def create_project(self, name: str) -> Dict[str, Any]:
        return self.post("/emi/projects", {"name": name, "source_kind": "kicad"})

    def list_runs(self, project_id: str) -> list:
        got = self.get(f"/emi/projects/{urllib.parse.quote(project_id)}/runs")
        return (got or {}).get("runs") or []

    def upload_board(self, project_id: str, filename: str, data: bytes, sha256: str) -> Dict[str, Any]:
        """Send the board and queue its analysis. Returns ``{"board": ..., "run": ...}``.

        Two steps, as in the browser: the app says where to put the bytes, the bytes go
        there, and only then does it hear about them. The first step can answer "I already
        have those" -- which it does every time a board is opened again unchanged -- and then
        nothing is sent at all.
        """
        if len(data) > MAX_UPLOAD_BYTES:
            raise ApiError(0, f"the board is {len(data) / 1e6:.0f} MB, larger than the app accepts")
        project = urllib.parse.quote(project_id)
        init = self.post(
            f"/emi/projects/{project}/uploads",
            {"filename": filename, "content_type": "application/zip", "sha256": sha256,
             "size_bytes": len(data)},
        )
        if not init.get("already_uploaded"):
            url = init.get("upload_url")
            if not url:
                raise ApiError(0, "the app offered neither an upload URL nor an existing copy")
            self._request("PUT", url, data, init.get("content_type") or "application/zip", UPLOAD_TIMEOUT)
        return self.post(
            f"/emi/projects/{project}/boards",
            {"input_key": init["key"], "sha256": sha256},
            timeout=UPLOAD_TIMEOUT,
        )

    def get_run(self, run_id: str) -> Dict[str, Any]:
        return self.get(f"/emi/runs/{urllib.parse.quote(run_id)}")

    def artifact(self, run_id: str, name: str) -> Any:
        """A run's JSON artifact, fetched through the URL the app mints for it."""
        ref = self.get(f"/emi/runs/{urllib.parse.quote(run_id)}/artifacts/{urllib.parse.quote(name)}")
        return json.loads(self._request("GET", ref["url"], timeout=UPLOAD_TIMEOUT))
