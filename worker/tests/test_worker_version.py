"""The worker's version, which every run summary carries for a shared report."""

from emi_worker import __version__
from emi_worker.runner import worker_version


def test_image_version_wins(monkeypatch):
    monkeypatch.setenv("EMI_WORKER_VERSION", " 0.3.0 ")
    assert worker_version() == "0.3.0"


def test_package_version_outside_an_image(monkeypatch):
    monkeypatch.delenv("EMI_WORKER_VERSION", raising=False)
    assert worker_version() == __version__
    monkeypatch.setenv("EMI_WORKER_VERSION", "")
    assert worker_version() == __version__
