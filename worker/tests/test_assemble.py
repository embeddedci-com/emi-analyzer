"""Paths assembled from a solve's artifacts and a driver (§7, §16.2).

The interpolation and the common grid are what let two paths of one driver meet at all: the far
field and the cable transfer functions sit on different grids, and a clock's harmonics sit on
neither. These tests pin that, and the source-impedance factor a driver brings with it.
"""

from __future__ import annotations

import math

import pytest

from emi_worker.compliance.assemble import (
    AttachedDriver,
    SolveArtifacts,
    assemble,
    interp_complex,
    interp_db,
    mesh_preset,
)
from emi_worker.compliance.predict import Path, PathPoint, common_grid, outlook
from emi_worker.drivers.document import parse

STD = "fcc-15b-radiated-3m"


def _clock(period=4e-8, z=50.0):
    return AttachedDriver("d1", parse({
        "format": "emi-driver", "version": 1, "name": "clk", "net": "CLK",
        "kind": "trapezoid", "role": "signal",
        "trapezoid": {
            "amplitude_v": {"value": 1.0, "source": "scope"},
            "period_s": {"value": period, "source": "scope"},
            "pulse_width_s": {"value": period / 4, "source": "scope"},
            "rise_s": {"value": 1e-9, "source": "scope"},
            "fall_s": {"value": 1e-9, "source": "scope"},
            "source_impedance_ohm": {"value": z, "source": "scope"},
        }}))


def _art(fs, e, z_in=50.0, usable=None) -> SolveArtifacts:
    return SolveArtifacts(
        manifest={"run": {"ports": [{"name": "p1", "resistance_ohm": 50.0, "excited": True}]}},
        farfield={"format_version": 3, "frequencies_hz": fs, "e_per_volt": e,
                  "usable": usable or [True] * len(fs),
                  "z_in_real": [z_in] * len(fs), "z_in_imag": [0.0] * len(fs),
                  "driven_by": "p1", "source_impedance_ohm": 50.0, "excited_ports": ["p1"],
                  "distance_m": 3.0})


# ---- interpolation --------------------------------------------------------------------

def test_magnitudes_interpolate_in_db_against_log_frequency():
    # 20 dB per decade: at the geometric mean, halfway in dB.
    assert interp_db([1e8, 1e9], [1.0, 10.0], math.sqrt(1e17)) == pytest.approx(math.sqrt(10))


def test_nothing_is_extrapolated():
    assert interp_db([1e8, 1e9], [1.0, 10.0], 5e7) is None
    assert interp_db([1e8, 1e9], [1.0, 10.0], 2e9) is None
    assert interp_complex([1e8, 1e9], [1, 2], [0, 0], 2e9) is None


def test_a_line_path_is_zero_between_lines_and_silent_outside_its_band():
    p = Path(kind="board", label="b", driver_id="d", line=True, covered_hz=(30e6, 1e9),
             points=[PathPoint(50e6, 1e-4), PathPoint(75e6, 2e-4)])
    assert p.at(50e6) == 1e-4
    assert p.at(60e6) == 0.0, "a 25 MHz clock puts nothing at 60 MHz"
    assert p.at(20e6) is None, "outside the band nothing was asked"


def test_a_continuous_path_interpolates_inside_and_says_nothing_outside():
    p = Path(kind="cable", label="c", driver_id="d",
             points=[PathPoint(1e8, 1e-5), PathPoint(1e9, 1e-4)])
    assert p.at(math.sqrt(1e17)) == pytest.approx(math.sqrt(1e-9))
    assert p.at(2e9) is None


def test_two_grids_meet_on_the_common_grid_and_add():
    """The bug: Path.at matched by exact float, the grids shared no point, and a driver's
    board and cable paths were never added. On the common grid they are, everywhere."""
    a = Path(kind="board", label="a", driver_id="d",
             points=[PathPoint(1e8, 1e-4), PathPoint(1e9, 1e-4)])
    b = Path(kind="cable", label="b", driver_id="d",
             points=[PathPoint(1.5e8, 1e-4), PathPoint(9e8, 1e-4)])
    grid = common_grid([a, b], STD)
    assert grid == [1e8, 1.5e8, 9e8, 1e9]
    o = outlook([a, b], None, STD)
    both = [pt for pt in o.points if not pt.uncovered]
    assert [pt.frequency_hz for pt in both] == [1.5e8, 9e8]
    for pt in both:
        assert pt.field_v_per_m == pytest.approx(2e-4)
    # Where b says nothing, the point says so rather than pretending b is zero.
    assert o.points[0].uncovered == ["b"]


def test_the_common_grid_stays_inside_the_scan():
    a = Path(kind="board", label="a", driver_id="d",
             points=[PathPoint(10e6, 1e-4), PathPoint(100e6, 1e-4)])
    assert common_grid([a], STD) == [100e6]


# ---- the driver on the solve --------------------------------------------------------------

def test_a_board_path_has_a_point_at_every_harmonic():
    fs = [30e6 * (1e9 / 30e6) ** (k / 59) for k in range(60)]
    asm = assemble(_art(fs, [1e-3] * 60), _clock(), STD)
    board = asm.paths[0]
    freqs = [p.frequency_hz for p in board.points]
    assert freqs[0] == pytest.approx(50e6) and freqs[-1] == pytest.approx(1e9)
    assert len(freqs) == 39
    assert board.line


def test_a_driver_with_another_source_impedance_is_reweighted():
    """Everything scales with the port current: Z_s = 50, Z_in = 50, Z_d = 150 is half."""
    fs = [30e6, 1e9]
    same = assemble(_art(fs, [1e-3, 1e-3]), _clock(z=50.0), STD).paths[0].points[0]
    high = assemble(_art(fs, [1e-3, 1e-3]), _clock(z=150.0), STD).paths[0].points[0]
    assert high.field_v_per_m / same.field_v_per_m == pytest.approx(100 / 200)


def test_a_hole_in_the_solved_energy_is_undriven():
    fs = [30e6, 100e6, 300e6, 1e9]
    asm = assemble(_art(fs, [1e-3] * 4, usable=[True, False, True, True]), _clock(), STD)
    assert 100e6 in asm.undriven
    assert "no source energy" in asm.undriven[100e6]


def test_a_hole_left_by_a_short_record_says_so():
    """The fix for a truncated record is a longer run, not a different source."""
    fs = [30e6, 100e6, 300e6, 1e9]
    art = _art(fs, [1e-3] * 4, usable=[True, False, True, True])
    art.farfield["truncated_hz"] = [100e6]
    asm = assemble(art, _clock(), STD)
    assert "stopped before the board stopped ringing" in asm.undriven[100e6]


def test_an_old_solve_asks_for_a_rerun():
    asm = assemble(SolveArtifacts(manifest={"run": {}}), _clock(), STD)
    assert asm.problems and asm.problems[0][0] == "solve-format"


def test_an_unconverged_solve_contributes_nothing():
    """A run that hit its timestep cap is refused whatever its artifacts say.

    Its far field here is marked usable, as every result was before the solve stage marked
    unconverged runs, and it still contributes no path.
    """
    art = _art([30e6, 100e6, 300e6, 1e9], [1e-3] * 4)
    art.manifest["run"].update({"converged": False, "final_energy_db": -12.4})
    asm = assemble(art, _clock(), STD)
    assert asm.paths == []
    assert asm.far_field_ports == []
    assert [k for k, _ in asm.problems] == ["solve-unconverged"]
    assert "12 dB" in asm.problems[0][1]


def test_a_converged_solve_is_used():
    art = _art([30e6, 100e6, 300e6, 1e9], [1e-3] * 4)
    art.manifest["run"].update({"converged": True, "final_energy_db": -40.2})
    asm = assemble(art, _clock(), STD)
    assert asm.paths and not asm.problems


def test_the_mesh_term_follows_the_requested_cell_and_is_coarse_when_unknown():
    assert mesh_preset({"run": {"mesh_request": {"dx_um": 50}}}) == "fine"
    assert mesh_preset({"run": {"mesh_request": {"dx_um": 75}}}) == "normal"
    assert mesh_preset({"run": {}}) == "coarse"
