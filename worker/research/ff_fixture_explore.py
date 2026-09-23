"""The fixture board's production solve, far field on, for ff_fixture_decompose.py.

``CASE`` names the output folder, ``end`` sets openEMS's end criterion and ``clear`` the minimum
box clearance in mm. Prints the published far field per volt and its slope.
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

from emi_worker.openems import model as emmodel  # noqa: E402
from emi_worker.openems import post  # noqa: E402
from emi_worker.stages import solve as solve_stage  # noqa: E402
import e2e_compliance_fixture as e2e  # noqa: E402

OUT = Path(os.environ.get("OUT", "/out"))
CASE = os.environ.get("CASE", "fixture")


def main():
    wd = OUT / CASE
    wd.mkdir(parents=True, exist_ok=True)
    e2e.WORKDIR = str(wd)
    if "clear" in os.environ:
        emmodel.FAR_FIELD_MIN_CLEARANCE_MM = float(os.environ["clear"])
    params = {
        "roi": {"min_x_mm": 4.0, "min_y_mm": 24.0, "max_x_mm": 30.0, "max_y_mm": 38.0},
        "frequencies_hz": [30e6, 100e6, 300e6, 1e9],
        "ports": [{"name": "p1", "x_mm": 10.0, "y_mm": 30.0, "layer": "F.Cu",
                   "half_width_mm": 0.15, "excited": True}],
        "mesh": {"dx_um": 150, "dy_um": 150, "dz_um": 100},
        "far_field": True,
        "end_criteria": float(os.environ.get("end", "1e-4")),
    }
    client = e2e.Client({"board": e2e.BOARD}, {"input_url": "board"})
    ctx = e2e.ctx_for(client, params)
    ctx.cores = int(os.environ.get("THREADS", "3"))
    t0 = time.monotonic()
    result = solve_stage.run_solve(ctx)
    print(f"{CASE}: {time.monotonic() - t0:.0f} s, {result.summary['cells']:,} cells, "
          f"{result.summary['timesteps']:,} steps, energy {result.summary['final_energy_db']}")
    for w in result.summary["warnings"]:
        print("  note:", w)
    ff = json.loads(client.uploads["farfield.json"])
    (wd / "farfield.json").write_text(json.dumps(ff))
    (wd / "manifest.json").write_bytes(client.uploads["manifest.json"])
    f = np.asarray(ff["frequencies_hz"])
    ev = np.asarray(ff["e_per_volt"])
    z = np.asarray(ff["z_in_real"]) + 1j * np.asarray(ff["z_in_imag"])
    ei = ev * np.abs(ff["source_impedance_ohm"] + z)
    sl = lambda y: np.polyfit(np.log10(f), 20 * np.log10(y), 1)[0]  # noqa: E731
    lo = f < 200e6
    print(f"  slope E/V {sl(ev):.1f} dB/dec overall, {np.polyfit(np.log10(f[lo]), 20*np.log10(ev[lo]), 1)[0]:.1f} below 200 MHz; "
          f"E/I {sl(ei):.1f}")
    for k in range(0, len(f), 6):
        print(f"  {f[k]/1e6:7.1f} MHz  E/V {ev[k]:.3e}  E/I {ei[k]:.3e}  Zin {z[k].real:9.2f}{z[k].imag:+11.1f}j")


if __name__ == "__main__":
    main()
