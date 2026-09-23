"""A run token is refreshed before it expires, so a day-long solve keeps its run.

The server issues six-hour tokens. The worker once minted one per run and never renewed it,
so every post after six hours was refused and a long solve could never finish.
"""

from __future__ import annotations

import json

import httpx
import pytest

from emi_worker import client as client_mod
from emi_worker.client import Client, RunReassigned


def _client(handler) -> Client:
    c = Client("http://server/api", "eci_key")
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def _token(n: int, expires_in: int = 6 * 3600) -> dict:
    return {"token": f"tok{n}", "run_id": "r1", "jti": "j1", "expires_in": expires_in}


def test_a_fresh_token_is_used_as_is():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers["authorization"]))
        return httpx.Response(200, json=_token(1) if req.url.path.endswith("/token") else {})

    c = _client(handler)
    tok = c.mint_run_token("r1")
    c.claim(tok)
    assert seen[-1] == ("/api/emi-agent/runs/r1/claim", "Bearer tok1")


def test_a_token_near_expiry_is_refreshed_with_the_worker_key(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: now[0])
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers["authorization"]))
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=_token(1))
        if req.url.path.endswith("/token/refresh"):
            return httpx.Response(200, json=_token(2))
        return httpx.Response(200, json={})

    c = _client(handler)
    tok = c.mint_run_token("r1")
    now[0] += 5.5 * 3600  # within the last hour
    c.progress(tok, pct=50)
    assert ("/api/emi-agent/runs/r1/token/refresh", "Bearer eci_key") in seen
    assert seen[-1] == ("/api/emi-agent/runs/r1/progress", "Bearer tok2")
    assert tok.token == "tok2" and tok.jti == "j1"


def test_a_failed_refresh_keeps_the_old_token(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: now[0])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=_token(1))
        if req.url.path.endswith("/token/refresh"):
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    tok = c.mint_run_token("r1")
    now[0] += 5.5 * 3600
    assert c.run_input(tok) == {"ok": True}
    assert tok.token == "tok1"


def test_losing_the_run_during_a_refresh_stops_the_work(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: now[0])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=_token(1))
        return httpx.Response(409, text=json.dumps({"error": "reassigned"}))

    c = _client(handler)
    tok = c.mint_run_token("r1")
    now[0] += 5.5 * 3600
    with pytest.raises(RunReassigned):
        c.progress(tok, pct=10)
