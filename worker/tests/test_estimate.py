"""The Python half of the shared cost-model contract.

The same fixtures are checked by ``server/emi/estimate_test.go``. If this file and that one
disagree, a user's live estimate in the browser will disagree with what the worker actually
does, and the run will be rejected at the mesh stage after they already committed to it.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from emi_worker.estimate import (
    BYTES_PER_CELL,
    MAX_FILL_FACTOR,
    EstimateError,
    EstimateInput,
    estimate,
)

FIXTURES = Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata" / "estimate_fixtures.json"


def _cases():
    doc = json.loads(FIXTURES.read_text())
    return [(c["name"], c["input"], c["expected"]) for c in doc["cases"]]


@pytest.mark.parametrize("name,inp,expected", _cases(), ids=[c[0] for c in _cases()])
def test_matches_shared_fixtures(name, inp, expected):
    got = estimate(EstimateInput(**inp))
    assert got.cells == expected["cells"]
    assert got.ram_bytes == expected["ram_bytes"]
    assert got.timesteps == expected["timesteps"]
    assert got.dt_seconds == pytest.approx(expected["dt_seconds"], rel=1e-12)
    assert got.sim_time_seconds == pytest.approx(expected["sim_time_seconds"], rel=1e-12)
    assert got.eta_seconds == pytest.approx(expected["eta_seconds"], rel=1e-12)


def test_vertical_mesh_sets_the_timestep():
    """The trap users fall into: dt keys off the smallest cell in *any* axis.

    Halving only the vertical resolution doubles the timestep count even though the
    in-plane mesh is untouched -- and since cells also double, wall clock goes up ~4x.
    """
    base = EstimateInput(
        roi_x_mm=20, roi_y_mm=20, roi_z_mm=5,
        dx_um=200, dy_um=200, dz_um=40, f_min_hz=300e6,
    )
    finer = EstimateInput(**{**base.__dict__, "dz_um": 20})

    a, b = estimate(base), estimate(finer)

    assert b.timesteps >= 2 * a.timesteps - 1
    assert b.eta_seconds >= 3.5 * a.eta_seconds


def test_grading_reduces_cells_but_never_the_timestep():
    uniform = EstimateInput(
        roi_x_mm=24, roi_y_mm=24, roi_z_mm=10,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=100e6,
    )
    graded = EstimateInput(**{**uniform.__dict__, "fill_factor": 0.13})

    u, g = estimate(uniform), estimate(graded)

    assert g.cells < u.cells
    assert g.timesteps == u.timesteps
    assert g.dt_seconds == u.dt_seconds


def test_ram_is_72_bytes_per_cell():
    est = estimate(EstimateInput(
        roi_x_mm=10, roi_y_mm=10, roi_z_mm=2,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=1e9,
    ))
    assert est.ram_bytes == est.cells * BYTES_PER_CELL
    assert BYTES_PER_CELL == 6 * 3 * 4


def test_whole_board_headline_numbers():
    """The figures quoted in the design doc, pinned so prose and code cannot diverge."""
    est = estimate(EstimateInput(
        roi_x_mm=120, roi_y_mm=100, roi_z_mm=10,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=100e6,
    ))
    assert est.cells == 1_920_000_000
    assert est.ram_bytes / 1e9 == pytest.approx(138.24, rel=1e-3)
    assert est.eta_seconds / 86400 == pytest.approx(69.2, rel=0.02)


def test_roi_headline_numbers():
    est = estimate(EstimateInput(
        roi_x_mm=24, roi_y_mm=24, roi_z_mm=10,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=100e6, fill_factor=0.13,
    ))
    assert est.cells == pytest.approx(12e6, rel=0.01)
    assert est.ram_bytes / 1e9 == pytest.approx(0.86, rel=0.02)
    assert est.eta_seconds / 3600 == pytest.approx(10.4, rel=0.02)


def test_courant_limit_is_exact():
    est = estimate(EstimateInput(
        roi_x_mm=1, roi_y_mm=1, roi_z_mm=1,
        dx_um=100, dy_um=100, dz_um=25, f_min_hz=1e9,
    ))
    expected_dt = 25e-6 / (299_792_458.0 * math.sqrt(3))
    assert est.dt_seconds == pytest.approx(expected_dt, rel=1e-15)


@pytest.mark.parametrize("field,value", [
    ("roi_x_mm", 0), ("roi_z_mm", -1), ("dz_um", 0),
    ("f_min_hz", 0), ("ports", 0),
    # 1.5 used to be rejected. It is an ordinary mesh multiplier: measured values on real
    # boards run 0.70 to 8.75, because dx is a floor on cell size and copper edges force
    # lines much closer than that. Only a value that cannot be a ratio at all is refused.
    ("fill_factor", MAX_FILL_FACTOR + 1), ("fill_factor", -0.5), ("fill_factor", 0),
])
def test_rejects_nonsense(field, value):
    good = dict(
        roi_x_mm=10, roi_y_mm=10, roi_z_mm=2,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=1e8, ports=1,
    )
    good[field] = value
    with pytest.raises(EstimateError):
        estimate(EstimateInput(**good))
