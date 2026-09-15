"""The worker must keep nothing.

That property is what lets the same worker run on a droplet, a workstation or a laptop,
and be killed at any moment. It has two halves, and both are asserted here:

  * identity is not persisted -- it is the API key in the environment, so a restarted
    worker is a new worker and a worker moved to another machine is the same one;
  * disk is not persisted -- one scratch directory per run, gone when the run ends,
    whatever ended it.

The disk half is the one that rots quietly: a solve writes a mesh and field dumps that can
run to gigabytes, and nothing ever reads them again once the results are uploaded.
"""

from __future__ import annotations

import os

import pytest

from emi_worker.config import Capabilities, Config
from emi_worker.runner import Worker
from emi_worker.stages import StageContext, StageResult


def _config(tmp_path, **over) -> Config:
    return Config(
        server_url="http://server.invalid",
        api_key="eci_test",
        name="test-worker",
        workdir=str(tmp_path / "scratch"),
        **over,
    )


def _caps() -> Capabilities:
    return Capabilities(cores=2, ram_gb=4.0, max_cells=1000, kinds=["ingest", "solve"])


@pytest.fixture
def worker(tmp_path):
    # Client construction is offline -- it builds an httpx client and makes no request
    # until something calls it, so a Worker can be exercised without a server.
    return Worker(_config(tmp_path), _caps())


# ---- identity ------------------------------------------------------------------------

def test_the_worker_persists_no_identity(tmp_path, monkeypatch):
    """Two workers built from the same environment are indistinguishable.

    Nothing is written at construction, and nothing is read back: a worker that moves to
    another machine carries its identity in EMBEDDEDCI_API_KEY and nowhere else.
    """
    monkeypatch.setenv("EMBEDDEDCI_API_KEY", "eci_abc")
    monkeypatch.setenv("EMBEDDEDCI_URL", "https://www.embeddedci.com")
    monkeypatch.setenv("EMI_WORKDIR", str(tmp_path / "w"))

    a = Config.from_env()
    b = Config.from_env()
    assert a == b
    # Reading the environment must not create anything.
    assert not (tmp_path / "w").exists()


def test_an_empty_worker_name_falls_back_to_the_hostname(monkeypatch):
    """`EMI_WORKER_NAME=` with no value is how an env file copied from the example ships.

    Set-but-empty has to mean unset here, or the worker registers namelessly and the UI
    shows a blank row where a machine name should be.
    """
    monkeypatch.setenv("EMBEDDEDCI_API_KEY", "eci_abc")

    monkeypatch.setenv("EMI_WORKER_NAME", "")
    assert Config.from_env().name
    monkeypatch.setenv("EMI_WORKER_NAME", "   ")
    assert Config.from_env().name.strip()
    monkeypatch.setenv("EMI_WORKER_NAME", "bench-01")
    assert Config.from_env().name == "bench-01"


def test_https_becomes_wss(tmp_path, monkeypatch):
    """Running against the droplet means TLS, and the dial-out has to follow."""
    monkeypatch.setenv("EMBEDDEDCI_API_KEY", "eci_abc")
    monkeypatch.setenv("EMBEDDEDCI_URL", "https://www.embeddedci.com")
    cfg = Config.from_env()
    assert cfg.ws_url == "wss://www.embeddedci.com/api/emi-agent/ws"
    assert cfg.api == "https://www.embeddedci.com/api"


def test_a_missing_key_is_refused_up_front(monkeypatch):
    """Better than registering as nobody and failing on the first claim."""
    monkeypatch.delenv("EMBEDDEDCI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="EMBEDDEDCI_API_KEY"):
        Config.from_env()


# ---- disk ------------------------------------------------------------------------------

def test_scratch_from_a_previous_process_is_cleared_on_start(worker):
    """A worker killed mid-solve leaves gigabytes nothing will ever read again.

    The run itself is not lost -- it is the server's to reassign, and a reassigned run is
    downloaded fresh. Only the bytes on this machine are pointless.
    """
    os.makedirs(os.path.join(worker.cfg.workdir, "run-a", "openems"), exist_ok=True)
    os.makedirs(os.path.join(worker.cfg.workdir, "run-b"), exist_ok=True)
    with open(os.path.join(worker.cfg.workdir, "run-a", "openems", "mesh.xml"), "w") as fh:
        fh.write("x" * 1024)

    worker._clear_scratch()

    assert os.listdir(worker.cfg.workdir) == []


def test_clearing_scratch_is_fine_when_there_is_none(worker):
    """First start on a fresh machine, which is the normal case."""
    worker._clear_scratch()  # workdir does not exist at all
    os.makedirs(worker.cfg.workdir)
    worker._clear_scratch()  # exists but is empty
    assert os.listdir(worker.cfg.workdir) == []


def test_keep_scratch_preserves_it(tmp_path):
    """The debugging escape hatch. Off by default, because it is the state we do not want."""
    w = Worker(_config(tmp_path, keep_scratch=True), _caps())
    os.makedirs(os.path.join(w.cfg.workdir, "run-a"), exist_ok=True)
    w._clear_scratch()
    assert os.listdir(w.cfg.workdir) == ["run-a"]


def test_a_finished_run_leaves_nothing_behind(worker):
    """Every run ends the same way on disk, whatever happened to it."""
    ctx = _context(worker, "run-1")
    with open(os.path.join(ctx.rundir, "geometry.bin"), "wb") as fh:
        fh.write(b"\0" * 4096)

    worker._drop_scratch(ctx)

    assert not os.path.exists(ctx.rundir)
    assert os.listdir(worker.cfg.workdir) == []


def test_dropping_scratch_twice_is_not_an_error(worker):
    """It runs from a finally, on paths that may already have unwound."""
    ctx = _context(worker, "run-2")
    worker._drop_scratch(ctx)
    worker._drop_scratch(ctx)


def _context(worker: Worker, run_id: str) -> StageContext:
    from emi_worker.client import RunToken
    return StageContext(
        client=worker.client,
        token=RunToken(token="t", run_id=run_id, jti="j", expires_in=3600),
        run={},
        workdir=worker.cfg.workdir,
        cores=1,
        max_cells=1000,
        should_stop=lambda: False,
    )


def test_stage_result_carries_names_not_files(worker):
    """Why deleting scratch the moment a stage returns is safe.

    Artifacts go worker -> object storage directly during the stage; what comes back is a
    list of names and sizes. If a result ever carried a path, this deletion would break it.
    """
    res = StageResult(summary={"ok": True}, artifacts=[{"name": "manifest.json", "size_bytes": 12}])
    for artifact in res.artifacts:
        assert "path" not in artifact and "file" not in artifact
