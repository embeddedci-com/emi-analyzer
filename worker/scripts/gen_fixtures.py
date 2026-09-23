#!/usr/bin/env python3
"""Regenerate the shared cost-model fixtures.

The output is asserted against by BOTH server/emi/estimate_test.go and
worker/tests/test_estimate.py. Changing it changes every estimate a user is shown, so run
`make fixtures` (which regenerates and then runs both suites) rather than this directly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emi_worker.estimate import (  # noqa: E402
    IN_PLANE_MIN_CELL_FRACTION,
    MESH_MULTIPLIER_FLOOR,
    EstimateInput,
    estimate,
)

OUT = Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata" / "estimate_fixtures.json"

CASES = [
    # The design doc's headline comparison: the same board meshed two ways.
    ("whole-board-uniform", dict(
        roi_x_mm=120, roi_y_mm=100, roi_z_mm=10,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=100e6, ports=1)),
    ("roi-20mm-graded", dict(
        roi_x_mm=24, roi_y_mm=24, roi_z_mm=10,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=100e6, ports=1, fill_factor=0.13)),
    # openEMS runs one full pass per excited port, so ports multiply wall clock.
    ("roi-20mm-graded-4ports", dict(
        roi_x_mm=24, roi_y_mm=24, roi_z_mm=10,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=100e6, ports=4, fill_factor=0.13)),
    # A cheap case: coarse mesh and a high frequency floor.
    ("coarse-1ghz-floor", dict(
        roi_x_mm=10, roi_y_mm=10, roi_z_mm=4,
        dx_um=100, dy_um=100, dz_um=100, f_min_hz=1e9, ports=1)),
    # dz is 10x finer than dx/dy, so the vertical mesh alone sets the timestep.
    ("vertical-mesh-dominates", dict(
        roi_x_mm=20, roi_y_mm=20, roi_z_mm=5,
        dx_um=200, dy_um=200, dz_um=20, f_min_hz=300e6, ports=1)),
    # A mesh multiplier above 1, which is what real boards actually produce: dx is a floor
    # on cell size and copper edges force lines much closer. Measured medians are 6.93
    # coarse, 4.11 normal, 2.65 fine. This case exists so all three implementations pin the
    # widened range rather than only the old (0, 1].
    ("roi-20mm-measured-multiplier", dict(
        roi_x_mm=20, roi_y_mm=20, roi_z_mm=11.6,
        dx_um=50, dy_um=50, dz_um=25, f_min_hz=100e6, ports=1, fill_factor=2.65)),
    # The coarse preset at the radiated band's floor, with its floor multiplier: what the
    # browser shows for a first look at 30 MHz. dx/4 = 37.5 um sets the timestep, not dz.
    ("coarse-30mhz-floor-multiplier", dict(
        roi_x_mm=6, roi_y_mm=6, roi_z_mm=11.6,
        dx_um=150, dy_um=150, dz_um=100, f_min_hz=30e6, ports=1,
        fill_factor=MESH_MULTIPLIER_FLOOR["coarse"])),
    # Extents that do not divide evenly, to pin the ceil() behaviour across languages.
    ("non-integer-division", dict(
        roi_x_mm=7.3, roi_y_mm=4.1, roi_z_mm=1.55,
        dx_um=60, dy_um=60, dz_um=35, f_min_hz=250e6, ports=2,
        fill_factor=0.5, periods=5.0, throughput_mcells_per_s=150.0)),
]


def main() -> int:
    cases = []
    for name, kw in CASES:
        est = estimate(EstimateInput(**kw))
        cases.append({"name": name, "input": kw, "expected": est.as_dict()})
        print(f"{name:28} cells={est.cells:>14,}  ram={est.ram_bytes/1e9:>8.2f} GB  "
              f"steps={est.timesteps:>10,}  eta={est.eta_seconds/3600:>9.2f} h")

    OUT.write_text(json.dumps({
        "_comment": (
            "Shared fixtures for the FDTD cost model. The Go, Python and TypeScript "
            "implementations must all reproduce these exactly. Regenerate with "
            "`make fixtures`, never by hand."
        ),
        # Constants the browser and the worker must share, pinned beside the cases.
        "in_plane_min_cell_fraction": IN_PLANE_MIN_CELL_FRACTION,
        "mesh_multiplier_floor": MESH_MULTIPLIER_FLOOR,
        "cases": cases,
    }, indent=2) + "\n")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
