"""M0 — can we write, run and parse a nec2c deck?

Before trusting an antenna solver on a cable, it has to reproduce something whose answer is
known independently. A half-wave dipole in free space is the standard case: at exactly
0.5 lambda its input impedance is about 73 + j42 ohm and its peak gain is 2.15 dBi.

nec2c is invoked as a subprocess, the way openEMS and ngspice already are. On a host without
the binary this falls back to running it in a container.

    python3 worker/scripts/spike_m0_nec.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

C = 299_792_458.0


def deck(length_m: float, freq_hz: float, segments: int = 21, radius_m: float = 1e-3) -> str:
    """A centre-fed wire along z, its feed on the middle segment."""
    half = length_m / 2.0
    feed = segments // 2 + 1
    return "\n".join([
        "CM half-wave dipole, free space",
        "CE",
        f"GW 1 {segments} 0 0 {-half:.6f} 0 0 {half:.6f} {radius_m:.6f}",
        "GE 0",
        f"EX 0 1 {feed} 0 1.0 0.0",
        f"FR 0 1 0 0 {freq_hz / 1e6:.6f} 0",
        "RP 0 1 1 1000 90.0 0.0 0.0 0.0",
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
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = d / "out.txt"
        if not out.exists():
            raise RuntimeError(f"nec2c produced no output\n{p.stdout}\n{p.stderr}")
        return out.read_text()


def impedance(report: str) -> tuple[float, float]:
    """Real and imaginary input impedance, from ANTENNA INPUT PARAMETERS."""
    lines = report.splitlines()
    for i, line in enumerate(lines):
        if "ANTENNA INPUT PARAMETERS" in line:
            for row in lines[i:i + 10]:
                nums = re.findall(r"[-+]?\d*\.?\d+E[-+]\d+", row)
                # tag, seg, then voltage (re, im), current (re, im), impedance (re, im), ...
                if len(nums) >= 8:
                    return float(nums[4]), float(nums[5])
    raise RuntimeError("no input parameters in the report")


def gain_db(report: str) -> float:
    lines = report.splitlines()
    for i, line in enumerate(lines):
        if "RADIATION PATTERNS" in line:
            for row in lines[i:i + 12]:
                nums = re.findall(r"[-+]?\d+\.\d+", row)
                if len(nums) >= 5 and row.strip()[0].isdigit():
                    return float(nums[4])  # total gain, dB
    raise RuntimeError("no radiation pattern in the report")


def main() -> None:
    """Check the deck against what a dipole is actually known to do.

    The famous 73.1 + 42.5j is the *infinitesimally thin* dipole at exactly 0.5 lambda. A real
    wire has a radius, which shortens its resonance and raises the reactance at 0.5 lambda, so
    that value is the wrong thing to assert. What is stable and well documented is the
    resonant length (about 0.47-0.48 lambda for a/lambda = 0.001), the resistance there
    (about 65-75 ohm) and the peak gain (2.15 dBi).
    """
    freq = 300e6
    lam = C / freq

    print(f"half-wave dipole at {freq / 1e6:.0f} MHz (lambda = {lam:.3f} m, a/lambda = 0.001)\n")

    print("segment convergence at 0.5 lambda:")
    for n in (11, 21, 41, 81):
        r, x = impedance(run_nec(deck(0.5 * lam, freq, segments=n)))
        print(f"  {n:3d} segments   Z = {r:6.1f} {x:+7.1f}j")

    print("\nlength sweep at 41 segments, looking for X = 0:")
    res_frac, res_r, prev = None, None, None
    for frac in (0.45, 0.46, 0.47, 0.48, 0.49, 0.50):
        r, x = impedance(run_nec(deck(frac * lam, freq, segments=41)))
        print(f"  {frac:.2f} lambda   Z = {r:6.1f} {x:+7.1f}j")
        if prev and prev[2] < 0 <= x:
            # linear interpolation between the two straddling lengths
            f0, r0, x0 = prev
            res_frac = f0 + (frac - f0) * (-x0) / (x - x0)
            res_r = r0 + (r - r0) * (-x0) / (x - x0)
        prev = (frac, r, x)

    gain = gain_db(run_nec(deck(0.475 * lam, freq, segments=41)))

    print("\nverdict")
    checks = []
    if res_frac is not None:
        ok = 0.46 <= res_frac <= 0.49
        checks.append(ok)
        print(f"  resonant length   {res_frac:.3f} lambda   expected 0.46-0.49     {'ok' if ok else 'OFF'}")
        ok = 60.0 <= res_r <= 80.0
        checks.append(ok)
        print(f"  R at resonance    {res_r:.1f} ohm        expected 60-80         {'ok' if ok else 'OFF'}")
    else:
        checks.append(False)
        print("  resonance         not bracketed by the sweep                     OFF")
    ok = abs(gain - 2.15) < 0.3
    checks.append(ok)
    print(f"  peak gain         {gain:.2f} dBi        expected 2.15 +/- 0.3   {'ok' if ok else 'OFF'}")

    print("\nThese are the tolerances cable tests 1 and 2 should use: a resonance and a gain,")
    print("not the thin-wire impedance, which no real conductor reproduces.")
    sys.exit(0 if all(checks) else 1)


if __name__ == "__main__":
    main()
