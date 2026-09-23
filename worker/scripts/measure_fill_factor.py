#!/usr/bin/env python3
"""Measure the cost model's fill factor against real boards (a correction to the first estimate).

``estimate.py`` scales a uniform bounding-box cell count by ``fill_factor`` to account for
grading. The webapp has been passing 0.15 -- a guess, made before any mesh had been built.
The early spikes found a real 20x20 mm region at the fine preset is 62.6 M cells against the ~13 M the
design doc assumed, so the guess is worth checking rather than inheriting.

This builds the real mesh with the real mesher for each board, region size and preset, and
reports the ratio the estimator should have used:

    fill_factor = cells the mesher produced / uniform cells over the same box

It only meshes, so it needs no solver and runs anywhere the worker imports. Point it at a
folder of ``<board>/<board>.kicad_pcb`` the way the tests are (``EMI_TEST_BOARDS``):

    cd worker && EMI_TEST_BOARDS=/path/to/boards OUT=/tmp/fill python3 scripts/measure_fill_factor.py

Boards are reported as "board A", "board B", ... in path order, with their layer count and
size, never by name: the boards this was calibrated on are private, and the output is meant
to be pasted into the docs. ``SHOW_NAMES=1`` prints the folder names as well, for local use.

It also reports the smallest cell, because the timestep is set by it and the estimator
assumes it is ``min(dx, dy, dz)``.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.normalize import _board_extent
from emi_worker.openems.model import Port, SolveParams, build_model

PRESETS = {"coarse": (150, 150, 100), "normal": (75, 75, 50), "fine": (50, 50, 25)}
REGIONS_MM = (10.0, 20.0, 40.0)
AIR_MM = 5.0


def uniform_cells(roi_mm: tuple[float, float, float, float], z_mm: float,
                  dx: int, dy: int, dz: int) -> int:
    nx = math.ceil((roi_mm[2] - roi_mm[0]) * 1000.0 / dx)
    ny = math.ceil((roi_mm[3] - roi_mm[1]) * 1000.0 / dy)
    nz = math.ceil(z_mm * 1000.0 / dz)
    return nx * ny * nz


def boards() -> list[Path]:
    root = Path(os.environ.get("EMI_TEST_BOARDS") or os.environ.get("BOARDS", "/boards"))
    found = sorted(root.glob("*/*.kicad_pcb"))
    # KiCad leaves _autosave-*.kicad_pcb beside the real file; measuring both would weight
    # that board twice in the statistics.
    return [p for p in found
            if "backup" not in p.name.lower() and not p.name.startswith("_autosave")]


def main() -> int:
    rows = []
    print(f"{'board':<22} {'region':>7} {'preset':<7} {'meshed':>12} {'uniform':>14} "
          f"{'fill':>7} {'min um':>7}")
    print("-" * 82)
    show = os.environ.get("SHOW_NAMES") == "1"
    for n, path in enumerate(boards()):
        try:
            board = parse_board(parse(path.read_text()))
        except Exception as exc:
            print(f"board {chr(65 + n)} could not parse: {exc}")
            continue
        transform = _board_extent(board)
        label = f"board {chr(65 + n)}"
        outline = [transform.pt(*pt) for ring in board.outline for pt in ring]
        size = ""
        if outline:
            size = (f", {max(p[0] for p in outline) - min(p[0] for p in outline):.0f} x "
                    f"{max(p[1] for p in outline) - min(p[1] for p in outline):.0f} mm")
        print(f"{label}: {len(board.copper_layers)} layers{size}"
              + (f"  ({path.parent.name})" if show else ""))
        pts = [transform.pt(*pt) for t in board.tracks for pt in t.pts]
        if not pts:
            print(f"{label:<22} no tracks")
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

        # The same vertical extent the estimator is given: stack plus air both sides.
        thickness = sum(l.thickness_mm for l in board.stackup if l.thickness_mm > 0)
        z_mm = (thickness or 1.6) + 2 * AIR_MM

        for side in REGIONS_MM:
            half = side / 2
            roi = (cx - half, cy - half, cx + half, cy + half)
            for name, (dx, dy, dz) in PRESETS.items():
                params = SolveParams(
                    roi=roi, frequencies_hz=[1e9],
                    ports=[Port(name="p1", x=cx, y=cy,
                                layer=board.copper_layers[0].name, half_width_mm=0.2)],
                    dx_um=dx, dy_um=dy, dz_um=dz, air_mm=AIR_MM,
                )
                try:
                    built = build_model(board, transform, params)
                except Exception as exc:
                    print(f"{label:<22} {side:>5.0f}mm {name:<7} "
                          f"could not build: {str(exc)[:40]}")
                    continue
                meshed = built.mesh.cells
                uniform = uniform_cells(roi, z_mm, dx, dy, dz)
                m = built.mesh
                ux = math.ceil((roi[2] - roi[0]) * 1000.0 / dx)
                uy = math.ceil((roi[3] - roi[1]) * 1000.0 / dy)
                uz = math.ceil(z_mm * 1000.0 / dz)
                per_axis = (f"  [x {len(m.x)}/{ux} = {len(m.x)/ux:.2f}, "
                            f"y {len(m.y)}/{uy} = {len(m.y)/uy:.2f}, "
                            f"z {len(m.z)}/{uz} = {len(m.z)/uz:.2f}]")
                fill = meshed / uniform
                min_um = m.min_cell_mm * 1000.0
                rows.append({"board": label, "region_mm": side,
                             "preset": name, "meshed_cells": meshed,
                             "uniform_cells": uniform, "fill_factor": fill,
                             "min_cell_um": round(min_um, 3),
                             # How many more timesteps the real grid needs than the
                             # estimator's min(dx, dy, dz) predicts.
                             "timestep_ratio": round(min(dx, dy, dz) / min_um, 3)})
                print(f"{label:<22} {side:>5.0f}mm {name:<7} {meshed:>12,} "
                      f"{uniform:>14,} {fill:>7.3f} {min_um:>7.1f}{per_axis}")

    if not rows:
        print("\nno boards measured")
        return 1

    from emi_worker.estimate import MESH_MULTIPLIER_FLOOR

    print(f"\n{len(rows)} measurements")
    for preset in PRESETS:
        fills = sorted(r["fill_factor"] for r in rows if r["preset"] == preset)
        steps = sorted(r["timestep_ratio"] for r in rows if r["preset"] == preset)
        if not fills:
            continue
        n = len(fills)
        print(f"  {preset:<7} fill min {fills[0]:.2f}  median {fills[n // 2]:.2f}  "
              f"max {fills[-1]:.2f}   (floor in use {MESH_MULTIPLIER_FLOOR[preset]})   "
              f"timestep ratio {steps[0]:.2f}-{steps[-1]:.2f}")

    out = Path(os.environ.get("OUT", "/spike/spike_out")) / "fill_factor.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"measurements": rows}, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
