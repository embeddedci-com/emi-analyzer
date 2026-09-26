"""The openEMS base image and the worker image that starts FROM it must agree.

They are separate Dockerfiles built by separate workflows, so nothing but these checks stops
them drifting: a probe the base image tests that is not the one a solve asks, a Debian that
differs between OPENEMS_SOURCE=build and apt, or a worker pinned to an image whose tag names
another openEMS commit.
"""

from __future__ import annotations

import re
from pathlib import Path

from emi_worker.config import _version_line
from emi_worker.openems import run

WORKER = Path(__file__).resolve().parent.parent
BASE_DIR = WORKER / "openems-image"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _debian_from(dockerfile: str) -> str:
    m = re.search(r"^FROM (debian:\S+@sha256:[0-9a-f]{64})", dockerfile, re.M)
    assert m, "no digest-pinned Debian FROM line"
    return m.group(1)


def test_base_image_runs_the_worker_probe():
    """The base image fails its build on the same model run.solver_has_series_rlc gives openEMS."""
    assert _read(BASE_DIR / "probe-inductor.xml") == run.series_rlc_probe_xml(), (
        "regenerate worker/openems-image/probe-inductor.xml from run.series_rlc_probe_xml()")


def test_same_debian_for_every_openems_source():
    assert _debian_from(_read(BASE_DIR / "Dockerfile")) == _debian_from(_read(WORKER / "Dockerfile"))


def test_worker_pins_the_base_image_of_the_same_openems_commit():
    ref = re.search(r"^ARG OPENEMS_REF=([0-9a-f]{40})$", _read(BASE_DIR / "Dockerfile"), re.M)
    assert ref, "openems-image/Dockerfile must pin OPENEMS_REF to a full commit"
    image = re.search(r"^ARG OPENEMS_IMAGE=(\S+)$", _read(WORKER / "Dockerfile"), re.M)
    assert image, "worker/Dockerfile has no OPENEMS_IMAGE"
    m = re.fullmatch(r"ghcr\.io/embeddedci-com/emi-openems:([0-9a-f]{12})(@sha256:[0-9a-f]{64})?",
                     image.group(1))
    assert m, f"unexpected OPENEMS_IMAGE {image.group(1)!r}"
    assert m.group(1) == ref.group(1)[:12]


def test_notice_names_the_compiled_commit():
    """NOTICE is where the image says which source its GPL programs came from."""
    ref = re.search(r"^ARG OPENEMS_REF=([0-9a-f]{40})$", _read(BASE_DIR / "Dockerfile"), re.M)
    assert ref and f"commit {ref.group(1)}" in _read(WORKER / "NOTICE")


def test_default_openems_source_is_the_built_one():
    """The released image must be the one that models an inductor."""
    assert re.search(r"^ARG OPENEMS_SOURCE=build$", _read(WORKER / "Dockerfile"), re.M)


def test_version_line_skips_the_banner_frame():
    banner = (
        " ---------------------------------------------------------------------- \n"
        " | openEMS 64bit -- version v0.0.36-1-gabcdef0\n"
        " | (C) 2010-2023 Thorsten Liebig  GPL license\n"
        " ---------------------------------------------------------------------- \n"
    )
    assert _version_line(banner) == "openEMS 64bit -- version v0.0.36-1-gabcdef0"
    assert _version_line("openEMS 0.0.35\n") == "openEMS 0.0.35"
    assert _version_line("") == "unknown"
