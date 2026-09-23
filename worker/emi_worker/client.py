"""HTTP client for the EMI control plane.

The shape mirrors the build-agent contract exactly:

    dial in (ws) or poll  ->  mint run token  ->  claim  ->  progress*  ->  artifacts*  ->  complete

Two properties are worth stating because they are easy to break later:

* The long-lived worker key is used only for register, poll and mint. Everything
  run-scoped uses the short-lived run token, so a leaked token grants access to one run.
* Bulk bytes never go through this client's ``self._api`` calls. Board inputs and result
  artifacts move directly between the worker and object storage over presigned URLs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Control-plane calls are small; if one hangs, something is wrong and we want to know
#: rather than block the run loop.
CONTROL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Blob transfers get a much longer read timeout -- a result bundle is hundreds of MB.
BLOB_TIMEOUT = httpx.Timeout(900.0, connect=30.0)


class ServerError(RuntimeError):
    """A control-plane call failed."""

    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"{status} from {url}: {body[:300]}")
        self.status = status
        self.body = body


class RunReassigned(ServerError):
    """This worker no longer owns the run.

    Raised on 409. It means a retry handed the run to somebody else, and the correct
    response is to abandon the work immediately rather than keep burning cores on results
    that will be rejected.
    """


#: Refresh a run token this long before it expires. The server issues six-hour tokens, and a
#: solve can run for a day: a token minted once used to expire mid-solve, and every post after
#: that was refused.
REFRESH_MARGIN_S = 3600


@dataclass
class RunToken:
    token: str
    run_id: str
    jti: str
    expires_in: int
    #: time.monotonic() after which the next run-scoped call refreshes the token first.
    refresh_after: float = float("inf")


class Client:
    def __init__(self, api_base: str, api_key: str, *, verify_tls: bool = True):
        self._api = api_base.rstrip("/")
        self._key = api_key
        self._http = httpx.Client(timeout=CONTROL_TIMEOUT, verify=verify_tls, follow_redirects=False)
        # A separate pool for blob traffic so a slow 400 MB upload cannot starve the
        # connection the progress posts need.
        self._blob = httpx.Client(timeout=BLOB_TIMEOUT, verify=verify_tls, follow_redirects=True)

    def close(self) -> None:
        self._http.close()
        self._blob.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- plumbing ----

    def _request(self, method: str, path: str, token: str, **kw) -> Any:
        url = f"{self._api}{path}"
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        resp = self._http.request(method, url, headers=headers, **kw)
        if resp.status_code == 409:
            raise RunReassigned(resp.status_code, resp.text, url)
        if resp.status_code >= 400:
            raise ServerError(resp.status_code, resp.text, url)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ---- worker-key scoped ----

    def register(self, name: str, capabilities: dict) -> dict:
        return self._request(
            "POST", "/emi-agent/register", self._key,
            json={"name": name, "capabilities": capabilities},
        )

    def deregister(self) -> None:
        self._request("POST", "/emi-agent/deregister", self._key, json={})

    def list_runs(self, limit: int = 25) -> list[dict]:
        out = self._request("GET", f"/emi-agent/runs?limit={limit}", self._key)
        return (out or {}).get("runs", [])

    def mint_run_token(self, run_id: str) -> RunToken:
        out = self._request("POST", f"/emi-agent/runs/{run_id}/token", self._key, json={})
        tok = RunToken(
            token=out["token"], run_id=out["run_id"],
            jti=out["jti"], expires_in=int(out.get("expires_in", 0)),
        )
        _schedule_refresh(tok)
        return tok

    def refresh_run_token(self, tok: RunToken) -> None:
        """Extend a run token this worker holds, in place. The jti stays the same."""
        out = self._request("POST", f"/emi-agent/runs/{tok.run_id}/token/refresh", self._key, json={})
        tok.token = out["token"]
        tok.expires_in = int(out.get("expires_in", 0))
        _schedule_refresh(tok)

    def _bearer(self, tok: RunToken) -> str:
        """The run token to send, refreshed first when it is close to expiring.

        A failed refresh is not fatal: the old token is still good until it expires, and the
        next call tries again. Losing the run is, and propagates.
        """
        if time.monotonic() >= tok.refresh_after:
            try:
                self.refresh_run_token(tok)
            except RunReassigned:
                raise
            except Exception as exc:  # noqa: BLE001 -- see docstring
                log.warning("run token refresh failed (retrying on the next call): %s", exc)
        return tok.token

    # ---- run-token scoped ----

    def claim(self, tok: RunToken) -> dict:
        return self._request("POST", f"/emi-agent/runs/{tok.run_id}/claim", self._bearer(tok), json={})

    def progress(self, tok: RunToken, **fields) -> None:
        """Post a progress update.

        Deliberately never raises for a transient failure: losing a progress post must not
        kill a solve that is six hours in. A reassignment is different and does propagate,
        because continuing after that is pure waste.
        """
        try:
            self._request("POST", f"/emi-agent/runs/{tok.run_id}/progress", self._bearer(tok), json=fields)
        except RunReassigned:
            raise
        except Exception as exc:  # noqa: BLE001 -- see docstring
            log.warning("progress post failed (continuing): %s", exc)

    def run_input(self, tok: RunToken) -> dict:
        return self._request("GET", f"/emi-agent/runs/{tok.run_id}/input", self._bearer(tok))

    def upload_artifact(self, tok: RunToken, name: str, data: bytes, content_type: str) -> dict:
        """Upload one artifact straight to object storage.

        The server only mints the URL. The bytes go worker -> Spaces, never through the
        control plane, which is what keeps a 1 vCPU droplet viable as the control plane for
        half-gigabyte result bundles.
        """
        init = self._request(
            "POST", f"/emi-agent/runs/{tok.run_id}/artifacts", self._bearer(tok),
            # The exact length, so storage refuses anything else and the server can refuse a
            # result over its upload limit before a byte is sent.
            json={"name": name, "content_type": content_type, "size_bytes": len(data)},
        )
        resp = self._blob.put(
            init["upload_url"], content=data,
            headers={"Content-Type": init.get("content_type", content_type)},
        )
        if resp.status_code >= 400:
            raise ServerError(resp.status_code, resp.text, "presigned PUT")
        return {
            "name": name, "key": init["key"],
            "content_type": init.get("content_type", content_type),
            "size_bytes": len(data),
        }

    def download(self, url: str) -> bytes:
        resp = self._blob.get(url)
        if resp.status_code >= 400:
            raise ServerError(resp.status_code, resp.text, "presigned GET")
        return resp.content

    def complete(
        self,
        tok: RunToken,
        status: str,
        *,
        summary: dict | None = None,
        error: str = "",
        artifacts: list[dict] | None = None,
        estimate: dict | None = None,
        board: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {"status": status}
        if summary is not None:
            body["summary"] = summary
        if error:
            body["error"] = error[:4000]
        if artifacts:
            body["artifacts"] = artifacts
        if estimate is not None:
            body["estimate"] = estimate
        if board is not None:
            body["board"] = board
        return self._request("POST", f"/emi-agent/runs/{tok.run_id}/complete", self._bearer(tok), json=body)


def _schedule_refresh(tok: RunToken) -> None:
    if tok.expires_in <= 0:
        tok.refresh_after = float("inf")
        return
    # Never later than halfway, so a short token from a test server still refreshes in time.
    lead = min(REFRESH_MARGIN_S, tok.expires_in / 2)
    tok.refresh_after = time.monotonic() + tok.expires_in - lead
