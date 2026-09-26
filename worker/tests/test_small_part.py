"""The bounded small-part solve (stages/small_part.py) and what it reports (openems/network.py).

Fast: no openEMS. The solve itself is checked in ``test_small_part_solve.py`` (slow) and in
worker/research/sp_*.py; this pins the rules around it: what the mode refuses, the budget, and
the port network's arithmetic on probes whose answer is known.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from emi_worker.openems import network
from emi_worker.openems.model import Port, SolveParams
from emi_worker.stages import StageError, small_part

FIXTURES = Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata" / "small_part_fixtures.json"


def _params(**kw) -> SolveParams:
    return SolveParams(roi=(0, 0, 10, 10), frequencies_hz=kw.pop("freqs", [300e6]),
                       ports=[Port("p1", 5, 5, "F.Cu")], **kw)


def test_a_small_part_solve_refuses_what_it_does_not_do():
    for key in ("far_field", "cable_ports", "model_components"):
        with pytest.raises(StageError, match="full-wave"):
            small_part.apply({"mode": "small_part", key: {"J1": {}} if key == "cable_ports" else True},
                             _params())


def test_the_band_sets_the_maps_the_end_criterion_and_the_merge():
    p = small_part.apply({"mode": "small_part"}, _params(freqs=[300e6, 5e9, 50e6],
                                                          max_timesteps=123, end_criteria=1e-4))
    assert p.frequencies_hz == [100e6, 300e6, 2e9]  # asked frequencies outside the band dropped
    assert p.f_max == 2e9 and p.end_criteria == small_part.END_CRITERIA
    assert p.max_timesteps == 0  # the budget sets the cap, not the caller
    assert p.merge_fraction == small_part.MERGE_FRACTION
    assert not p.far_field
    assert p.pml_within_max_cell and p.map_height_mm == small_part.MAP_HEIGHT_MM
    assert not p.band_edge_note  # every output is a ratio to the source


@pytest.mark.parametrize("band", [[10e6, 1e9], [1e9, 1.5e9], [100e6, 10e9], "x"])
def test_a_band_outside_the_limits_is_refused(band):
    with pytest.raises(StageError):
        small_part.band({"band_hz": band})


def test_the_budget_shortens_the_cap_and_refuses_below_the_shortest_record():
    dt = 1e-13
    # Plenty of budget: the physics' own cap stands.
    assert small_part.admit(100_000, 300_000, dt) == 300_000
    # A bigger mesh gets what the budget affords.
    cells = 1_000_000
    cap = small_part.admit(cells, 300_000, dt)
    assert cap == int(small_part.MAX_CELL_STEPS // cells) and cap < 300_000
    # Too big to afford the shortest record, or too many cells outright: refused, with numbers.
    with pytest.raises(StageError, match="ns of simulated time"):
        small_part.admit(2_900_000, 10**9, 1e-14)
    with pytest.raises(StageError, match="cells, over"):
        small_part.admit(small_part.MAX_CELLS + 1, 1000, dt)


def test_constants_match_the_browser():
    """The browser plans and prices the same part (webapp/src/lib/smallPart.ts)."""
    from emi_worker.openems import coupon

    c = json.loads(FIXTURES.read_text())["constants"]
    assert c["min_margin_mm"] == coupon.MIN_MARGIN_MM
    assert c["margin_heights"] == coupon.MARGIN_HEIGHTS
    assert c["max_side_mm"] == coupon.MAX_SIDE_MM
    assert c["port_half_width_mm"] == coupon.PORT_HALF_WIDTH_MM
    assert c["max_cells"] == small_part.MAX_CELLS
    assert c["max_cell_steps"] == small_part.MAX_CELL_STEPS
    assert c["min_record_s"] == small_part.MIN_RECORD_S
    assert c["end_criteria_db"] == pytest.approx(10 * np.log10(small_part.END_CRITERIA))
    assert c["band_hz"] == list(small_part.BAND_HZ)


# ---- the port network -----------------------------------------------------------------------

DT = 5e-12
N = 4000
T = np.arange(N) * DT


def _probe(path: Path, values: np.ndarray) -> None:
    path.write_text("% probe\n" + "\n".join(f"{t:.12e}\t{v:.12e}" for t, v in zip(T, values)) + "\n")


def _pulse(delay: float = 0.0) -> np.ndarray:
    tau = 0.2e-9
    return np.exp(-(((T - 1e-9 - delay) / tau) ** 2))


def _write(tmp: Path, name: str, v: np.ndarray, i: np.ndarray) -> None:
    _probe(tmp / f"{name}_ut", v)
    _probe(tmp / f"{name}_it", i)


def test_a_matched_through_reads_s21_of_one_and_s11_of_nothing(tmp_path):
    # A 50 ohm source into a lossless matched line: V1 = Z0 I1 at the input, and the same wave
    # delayed at the output, where the 50 ohm load draws V2 = -Z0 I2 (current into the structure).
    z0 = 50.0
    v1 = _pulse()
    _write(tmp_path, "p1", v1, v1 / z0)
    v2 = _pulse(0.3e-9)
    _write(tmp_path, "p2", v2, -v2 / z0)
    doc = network.network(str(tmp_path), [
        {"name": "p1", "resistance": 50.0, "excited": True, "pad": "U1.1"},
        {"name": "p2", "resistance": 50.0, "excited": False, "pad": "R1.1"},
    ], network.log_grid(100e6, 2e9))
    s21 = np.array(doc["transmission"][0]["s_db"], dtype=float)
    assert np.all(np.abs(s21) < 0.01)
    assert all(v is None or v < -60 for v in doc["s11_db"])
    assert np.allclose(doc["z_in_real"], 50.0, rtol=1e-6)
    phase = np.unwrap(np.radians(np.array(doc["transmission"][0]["s_phase_deg"], dtype=float)))
    slope = np.polyfit(np.array(doc["frequencies_hz"]), phase, 1)[0]
    assert -slope / (2 * np.pi) == pytest.approx(0.3e-9, rel=1e-3)
    assert doc["driven"]["pad"] == "U1.1" and doc["transmission"][0]["pad"] == "R1.1"
    assert doc["truncated_hz"] == [] and all(doc["usable"])


def test_a_negative_resistance_is_dropped_as_truncated(tmp_path):
    v = _pulse()
    # A port that delivers power *out* of a passive structure: I in antiphase to V.
    _write(tmp_path, "p1", v, -v / 50.0)
    doc = network.network(str(tmp_path), [{"name": "p1", "resistance": 50.0, "excited": True}],
                          network.log_grid(100e6, 1e9, 10))
    assert len(doc["truncated_hz"]) == 10
    assert doc["z_in_real"] == [None] * 10 and not any(doc["usable"])


def test_an_unconverged_run_publishes_no_number(tmp_path):
    v = _pulse()
    _write(tmp_path, "p1", v, v / 50.0)
    doc = network.network(str(tmp_path), [{"name": "p1", "resistance": 50.0, "excited": True}],
                          network.log_grid(100e6, 1e9, 10), unusable_reason="it hit its cap")
    assert doc["unusable_reason"] == "it hit its cap"
    assert doc["z_in_real"] == [None] * 10 and doc["s11_db"] == [None] * 10


def test_a_network_needs_exactly_one_driven_port(tmp_path):
    with pytest.raises(ValueError):
        network.network(str(tmp_path), [{"name": "p1", "excited": False}], [1e9])
