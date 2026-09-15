"""M0 · cable 4, step 2 — which nec2c wire matches the FDTD one?

`spike_m0_wire_fdtd.py` found the dipole's resonance in openEMS. A wire in a rectilinear grid
is a square column one cell across, so its effective radius comes from the mesh. This sweeps
nec2c over wire radius to find the one that reproduces the FDTD resonance, which is the
conversion a Tier B against Tier C comparison needs.

    python3 worker/scripts/spike_m0_wire_nec.py [path/to/m0_dipole_fdtd.json]
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LENGTH_M = 0.5
SEGMENTS = 41


def deck(radius_m: float, f_start_mhz: float, steps: int, step_mhz: float) -> str:
    half = LENGTH_M / 2
    return "\n".join([
        "CM dipole, radius sweep",
        "CE",
        f"GW 1 {SEGMENTS} 0 0 {-half:.6f} 0 0 {half:.6f} {radius_m:.6f}",
        "GE 0",
        f"EX 0 1 {SEGMENTS // 2 + 1} 0 1.0 0.0",
        f"FR 0 {steps} 0 0 {f_start_mhz:.3f} {step_mhz:.3f}",
        "XQ 0",   # without an execution card NEC echoes the structure and computes nothing
        "EN",
    ]) + "\n"


def run_nec(text: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "deck.nec").write_text(text)
        if shutil.which("nec2c"):
            cmd = ["nec2c", "-i", str(d / "deck.nec"), "-o", str(d / "out.txt")]
        else:
            cmd = ["docker", "run", "--rm", "-v", f"{d}:/w", "emi-spike-nec2c",
                   "nec2c", "-i/w/deck.nec", "-o/w/out.txt"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return (d / "out.txt").read_text()


def sweep(report: str) -> list[tuple[float, float, float]]:
    """(frequency Hz, R, X) for every frequency block in the report."""
    out: list[tuple[float, float, float]] = []
    freq = None
    lines = report.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"FREQUENCY\s*:\s*([\d.eE+-]+)\s*MHz", line, re.I)
        if m:
            freq = float(m.group(1)) * 1e6
        if "ANTENNA INPUT PARAMETERS" in line and freq is not None:
            for row in lines[i:i + 10]:
                nums = re.findall(r"[-+]?\d*\.?\d+E[-+]\d+", row)
                if len(nums) >= 8:
                    out.append((freq, float(nums[4]), float(nums[5])))
                    break
    return out


def resonance(points: list[tuple[float, float, float]]) -> tuple[float, float]:
    for (f0, r0, x0), (f1, r1, x1) in zip(points, points[1:]):
        if x0 < 0 <= x1:
            t = -x0 / (x1 - x0)
            return f0 + t * (f1 - f0), r0 + t * (r1 - r0)
    return float("nan"), float("nan")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    target_f = target_r = None
    if path and path.exists():
        d = json.loads(path.read_text())
        target_f, target_r = d["resonance_hz"], d["r_at_resonance"]
        print(f"FDTD reference: {target_f/1e6:.1f} MHz, R = {target_r:.1f} ohm "
              f"(wire one {d['info']['min_cell_mm'] * 2:.1f} mm cell across)\n")

    print("  radius (mm)   resonance (MHz)   R (ohm)   L/lambda")
    print("  " + "-" * 52)
    best = None
    for radius_mm in (1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0):
        pts = sweep(run_nec(deck(radius_mm / 1000.0, 180.0, 41, 4.0)))
        f_res, r_res = resonance(pts)
        if f_res != f_res:  # nan
            print(f"  {radius_mm:8.1f}      no crossing in 180-340 MHz")
            continue
        frac = LENGTH_M / (299_792_458.0 / f_res)
        mark = ""
        if target_f:
            err = abs(f_res - target_f)
            if best is None or err < best[0]:
                best = (err, radius_mm, f_res, r_res)
        print(f"  {radius_mm:8.1f}      {f_res/1e6:10.1f}   {r_res:9.1f}   {frac:.3f}{mark}")

    if best:
        err, radius_mm, f_res, r_res = best
        print(f"\nclosest to FDTD: radius {radius_mm:.1f} mm -> {f_res/1e6:.1f} MHz "
              f"({err/1e6:+.1f} MHz off), R = {r_res:.1f} vs {target_r:.1f} ohm")
        print("\nThe FDTD column is 5 mm across, so an equivalent radius of a few millimetres")
        print("is the expected order: a square conductor behaves like a round one of a")
        print("comparable size, not like the thin wire a naive comparison would assume.")


if __name__ == "__main__":
    main()
