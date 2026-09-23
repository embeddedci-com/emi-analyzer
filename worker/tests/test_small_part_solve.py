"""Small-part solves through openEMS, against closed forms. Slow: set EMI_SLOW_TESTS=1.

The fast checks of the same research scripts (worker/research/sp_*.py), on the coarse preset
only: a 50 ohm microstrip coupon's impedance, delay and S21, a via between planes against the
two-post closed form, and the public synthetic clock board converging with nothing dropped.
A minute or two each on three threads. docs/verification/small-part-solve.md has the full set.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from emi_worker.openems import run as emrun

pytestmark = pytest.mark.skipif(
    not os.environ.get("EMI_SLOW_TESTS") or not shutil.which(emrun.OPENEMS_BIN),
    reason="set EMI_SLOW_TESTS=1 in the worker image (openEMS) to run",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import sp_harness

    monkeypatch.setattr(sp_harness, "OUT", tmp_path)
    return sp_harness


def test_a_microstrip_coupon_reads_its_impedance_delay_and_s21(harness):
    import sp_verify_lines as v

    w = harness.width_for(50.0, lambda x: harness.hammerstad_jensen(x, v.MS_H, v.ER)[0])
    z_hj, e_hj = harness.hammerstad_jensen(w, v.MS_H, v.ER)
    text = v.microstrip_board(w)
    _, c = harness.coupon_params(text, ["SIG"])
    r = v.check_line("microstrip", text, c.ports, c.roi, ["SIG"], z_hj, e_hj, "coarse")
    assert r["converged"]
    assert r["pass_z0"], r["z0_error"]
    assert r["pass_delay"], r["delay_error"]
    net = r["network"]
    f = np.asarray(net["frequencies_hz"])
    want = 20 * np.log10(np.abs(v.lossy_s21(z_hj, e_hj, v.ER, v.TAN_D,
                                            (v.LINE[1] - v.LINE[0]) / 1000.0, f)))
    got = np.array(net["transmission"][0]["s_db"], dtype=float)
    upto = f <= v.S21_TO_HZ * 1.0001
    assert np.all(np.abs(got[upto] - want[upto]) <= v.S21_TOL_DB)
    assert net["truncated_hz"] == []


def test_a_via_between_planes_matches_two_posts(harness, monkeypatch):
    import sp_verify_via as v

    monkeypatch.setattr(v, "CASES", [(0.3, 2.0)])
    monkeypatch.setattr(v, "h", harness)
    monkeypatch.setenv("PRESETS", "coarse")
    v.main()
    import json

    report = json.loads((harness.OUT / "smallpart" / "via.json").read_text())
    row = report["presets"]["coarse"]["rows"][0]
    assert row["pass"], row["error"]


def test_the_synthetic_clock_coupon_settles_with_nothing_dropped(harness):
    import sp_convergence as v

    params, c = harness.coupon_params(v.clock_board(), ["CLK"], preset="coarse",
                                      freqs_hz=v.MAP_HZ)
    got = harness.solve(v.clock_board(), params, "clock")
    assert got.summary["converged"] and "unusable_reason" not in got.summary
    net = got.json("network.json")
    assert net["truncated_hz"] == [] and all(net["usable"])
    assert got.manifest["mode"] == "small_part"
