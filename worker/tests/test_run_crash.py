"""A run the worker claimed always ends, even when the worker itself breaks.

Seen on 2026-10-11: a full Docker disk made the scratch directory fail with ENOSPC right
after the claim. The run stayed in_progress until the server's sweeper timed it out, and
every client (webapp, KiCad plugin, MCP) waited on a dead run.
"""

from __future__ import annotations

import errno

import pytest

from emi_worker import runner, scratch
from emi_worker.client import RunToken, ServerError
from emi_worker.config import Capabilities, Config
from emi_worker.runner import Worker, _Job

RUN_ID = "0123456789abcdef0123456789abcdef"


class FakeClient:
    def __init__(self, fail_complete: bool = False):
        self.completed: list[tuple[str, dict]] = []
        self.fail_complete = fail_complete

    def mint_run_token(self, run_id):
        return RunToken(token="t", run_id=run_id, jti="j", expires_in=3600)

    def claim(self, tok):
        pass

    def complete(self, tok, status, **fields):
        if self.fail_complete:
            raise ServerError(503, "unavailable", "http://server.invalid")
        self.completed.append((status, fields))


@pytest.fixture
def worker(tmp_path):
    cfg = Config(server_url="http://server.invalid", api_key="eci_test",
                 name="test-worker", workdir=str(tmp_path / "scratch"))
    w = Worker(cfg, Capabilities(cores=2, ram_gb=4.0, max_cells=1000, kinds=["ingest"]))
    w.client = FakeClient()
    return w


def _job() -> _Job:
    return _Job(run_id=RUN_ID, kind="ingest", payload={"id": RUN_ID, "kind": "ingest"})


def _disk_full(*_a, **_kw):
    raise OSError(errno.ENOSPC, "No space left on device")


def test_a_full_disk_after_the_claim_fails_the_run(worker, monkeypatch):
    monkeypatch.setattr(scratch, "run_dir", _disk_full)

    worker._execute(_job())

    assert worker.client.completed == [("failed", {"error": "the worker ran out of disk space"})]


def test_any_crash_after_the_claim_fails_the_run_with_its_message(worker, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("scratch is broken")
    monkeypatch.setattr(scratch, "run_dir", boom)

    worker._execute(_job())

    assert worker.client.completed == [("failed", {"error": "internal worker error: scratch is broken"})]


def test_a_full_disk_inside_a_stage_is_readable_too(worker, monkeypatch):
    monkeypatch.setitem(runner.STAGES, "ingest", _disk_full)

    worker._execute(_job())

    assert worker.client.completed == [("failed", {"error": "the worker ran out of disk space"})]


def test_failing_to_record_the_failure_does_not_raise(worker, monkeypatch):
    worker.client = FakeClient(fail_complete=True)
    monkeypatch.setattr(scratch, "run_dir", _disk_full)

    worker._execute(_job())  # logged; the sweeper is the backstop


def test_a_failed_token_mint_still_leaves_the_run_alone(worker, monkeypatch):
    def refuse(run_id):
        raise ServerError(500, "nope", "http://server.invalid")
    monkeypatch.setattr(worker.client, "mint_run_token", refuse)

    worker._execute(_job())

    assert worker.client.completed == []
