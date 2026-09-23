"""The far field through a real openEMS solve, against nec2c. Slow: set EMI_SLOW_TESTS=1.

A 40 mm dipole, horizontal, through the production port, source, box and ``scan.field``, read at
the scan's antenna positions over the ground plane, against nec2c's near field at the same
points over a perfect plane. About a minute on three threads; research/ff_verify_dipole.py runs
the full set this is taken from. See docs/verification/far-field.md.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from emi_worker.cables.nec import available as nec_available
from emi_worker.openems import run as emrun

pytestmark = pytest.mark.skipif(
    not os.environ.get("EMI_SLOW_TESTS") or not nec_available()
    or not shutil.which(emrun.OPENEMS_BIN),
    reason="set EMI_SLOW_TESTS=1 in the worker image (openEMS and nec2c) to run",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))


def test_a_horizontal_dipole_over_ground_matches_nec2c(tmp_path, monkeypatch):
    import ff_verify_dipole as v

    monkeypatch.setattr(v, "OUT", tmp_path)
    monkeypatch.setattr(v, "FREQS", [30e6, 100e6, 300e6, 700e6])
    out = v.case("short-horizontal")
    for row in out["rows"]:
        assert abs(row["scan_diff_db"]) < 0.5, row["f_hz"]
        assert abs(row["directivity_dbi"] - 1.76) < 0.05, row["f_hz"]
    # Capacitive and horizontal over a plane: at least 40 dB/decade per volt.
    assert out["slope_e_per_volt_db_per_decade"] > 40.0
