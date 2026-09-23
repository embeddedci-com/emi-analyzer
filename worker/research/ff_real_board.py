"""Far-field verification: the production solve, far field on, on one of your own boards.

What it measures is the cost and the behaviour, not the level (nothing independent says what a
real board's level should be): the box's size and clearance, cells, runtime, peak memory,
convergence, the size of the face dumps on disk and of the artifacts uploaded, whether the
nf2ff guard agreed, and the per-volt slope at the low end.

Point it at a board the way the test suite is (``EMI_TEST_BOARDS``), mounted read-only:

    docker run --rm -v "$PWD/worker:/w:ro" -v "$EMI_TEST_BOARDS:/boards:ro" -v "$PWD/out:/out" \\
        -w /w -e PYTHONPATH=/w -e OUT=/out -e BOARD=<folder> -e NET=<net substring> \\
        --entrypoint python3 <worker image> research/ff_real_board.py

``BOARD`` is a folder under /boards holding one .kicad_pcb. The port goes on the first top-layer
pad of the first net whose name contains ``NET``, and the region is a ``ROI_MM`` square around it
(``full`` for the whole board). ``DX_UM``/``DZ_UM`` pick the mesh preset. The printed summary
names the board only as its layer count and size, so it can be pasted into docs.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from emi_worker.kicad import parse, parse_board  # noqa: E402
from emi_worker.kicad.normalize import _board_extent  # noqa: E402
from emi_worker.stages import solve as solve_stage  # noqa: E402
import e2e_compliance_fixture as e2e  # noqa: E402

OUT = Path(os.environ.get("OUT", "/out"))
BOARDS = Path(os.environ.get("BOARDS", "/boards"))


def cgroup_peak_bytes() -> int | None:
    for p in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            return int(Path(p).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def main() -> None:
    folder = os.environ["BOARD"]
    path = next(p for p in sorted((BOARDS / folder).glob("*.kicad_pcb"))
                if "backup" not in p.name.lower())
    data = path.read_bytes()
    board = parse_board(parse(data.decode()))
    t = _board_extent(board)
    top = board.copper_layers[0].name
    want = os.environ.get("NET", "CLK").upper()
    net = next(n for n in board.nets if want in n.upper())
    pad = next(p for p in board.pads if p.net == net and top in p.layers)
    px, py = t.pt(pad.x, pad.y)
    dx = float(os.environ.get("DX_UM", "150"))
    dz = float(os.environ.get("DZ_UM", "100"))
    roi_mm = os.environ.get("ROI_MM", "40")
    if roi_mm == "full":
        roi = (0.0, 0.0, t.width, t.height)
    else:
        h = float(roi_mm) / 2
        roi = (max(0.0, px - h), max(0.0, py - h), min(t.width, px + h), min(t.height, py + h))
    params = {
        "roi": {"min_x_mm": roi[0], "min_y_mm": roi[1], "max_x_mm": roi[2], "max_y_mm": roi[3]},
        "frequencies_hz": [30e6, 100e6, 300e6, 1e9],
        "ports": [{"name": "p1", "x_mm": px, "y_mm": py, "layer": top,
                   "half_width_mm": max(0.2, 1.5 * dx / 1000), "excited": True}],
        "mesh": {"dx_um": dx, "dy_um": dx, "dz_um": dz},
        "far_field": True,
    }
    tag = os.environ.get("TAG", "board")
    wd = OUT / "real" / tag
    wd.mkdir(parents=True, exist_ok=True)
    e2e.WORKDIR = str(wd)
    client = e2e.Client({"board": data}, {"input_url": "board"})
    ctx = e2e.ctx_for(client, params)
    ctx.cores = int(os.environ.get("THREADS", "3"))
    t0 = time.monotonic()
    result = solve_stage.run_solve(ctx)
    elapsed = time.monotonic() - t0

    run_dir = wd / "e2e" / "openems"
    dumps = sum(p.stat().st_size for p in run_dir.glob("nf2ff_*.h5"))
    uploads = {n: len(b) for n, b in client.uploads.items()}
    manifest = json.loads(client.uploads["manifest.json"])
    summary = {
        "board": f"{len(board.copper_layers)} layers, {t.width:.0f} x {t.height:.0f} mm",
        "region_mm": [round(roi[2] - roi[0], 1), round(roi[3] - roi[1], 1)],
        "mesh_um": [dx, dx, dz],
        "cells": result.summary["cells"],
        "timesteps": result.summary["timesteps"],
        "max_timesteps": result.summary["max_timesteps"],
        "final_energy_db": result.summary["final_energy_db"],
        "converged": result.summary["converged"],
        "solve_s": round(elapsed),
        "peak_memory_bytes": cgroup_peak_bytes(),
        "face_dumps_bytes": dumps,
        "uploaded_bytes": sum(uploads.values()),
        "farfield_json_bytes": uploads.get("farfield.json"),
        "far_field_note": manifest.get("far_field_note"),
        "notes": result.summary["warnings"],
    }
    if "farfield.json" in client.uploads:
        ff = json.loads(client.uploads["farfield.json"])
        f = np.asarray(ff["frequencies_hz"])
        ev = np.asarray(ff["e_per_volt"])
        lo = (f < 200e6) & (ev > 0)
        summary.update({
            "box_mm": [round(ff["faces_mm"][3] - ff["faces_mm"][0], 1),
                       round(ff["faces_mm"][4] - ff["faces_mm"][1], 1),
                       round(ff["faces_mm"][5] - ff["faces_mm"][2], 1)],
            "clearance_mm": ff["clearance_mm"],
            "face_resolution_mm": ff["face_resolution_mm"],
            "scan_radius_m": ff["scan_radius_m"],
            "slope_below_200mhz_db_per_decade": float(np.polyfit(
                np.log10(f[lo]), 20 * np.log10(ev[lo]), 1)[0]),
            "e_per_volt": {f"{f[k] / 1e6:.0f} MHz": float(ev[k])
                           for k in range(0, len(f), 6)},
        })
        (wd / "farfield.json").write_text(json.dumps(ff))
    print(json.dumps(summary, indent=1))
    (wd / "summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
