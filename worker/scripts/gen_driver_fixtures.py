#!/usr/bin/env python3
"""Regenerate the shared driver-spectrum fixtures.

The output is asserted against by BOTH worker/tests/test_driver_spectrum.py and
webapp/src/lib/driverSpectrum.test.ts. A driver spectrum decides what absolute level a user
is shown, in the browser and in a worker result alike, so the two have to agree exactly.
Run `make driver-fixtures`, which regenerates and then runs both suites.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emi_worker.drivers.spectrum import (  # noqa: E402
    Trapezoid,
    corner_frequencies,
    envelope_v,
    trapezoid_series,
)

OUT = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
       / "driver_fixtures.json")

CASES = [
    # §9.2's worked example: a 25 MHz SPI clock at 50 % duty.
    ("spi-clock-25mhz", 40, dict(
        amplitude_v=3.3, period_s=4.0e-8, pulse_width_s=2.0e-8,
        rise_s=1.2e-9, fall_s=1.2e-9)),
    # A narrow pulse moves the first corner up and fills in the even harmonics.
    ("pwm-10-percent", 40, dict(
        amplitude_v=5.0, period_s=1.0e-5, pulse_width_s=1.0e-6,
        rise_s=2.0e-8, fall_s=2.0e-8)),
    # Asymmetric edges, which the closed form cannot express at all.
    ("asymmetric-edges", 40, dict(
        amplitude_v=3.3, period_s=4.0e-8, pulse_width_s=1.5e-8,
        rise_s=1.0e-9, fall_s=5.0e-9)),
    # Edges short enough that the second corner is out of band: a near-square wave, whose
    # harmonics are 4A/(n*pi) and which pins the amplitude convention across languages.
    ("near-square-1mhz", 15, dict(
        amplitude_v=1.0, period_s=1.0e-6, pulse_width_s=5.0e-7,
        rise_s=1.0e-12, fall_s=1.0e-12)),
    # Slow edges on a slow clock: both corners sit inside the harmonic set.
    ("slow-edges-1mhz", 60, dict(
        amplitude_v=12.0, period_s=1.0e-6, pulse_width_s=4.0e-7,
        rise_s=5.0e-8, fall_s=5.0e-8)),
]

#: Envelope probes, as fractions of each case's corner frequencies, so every case exercises
#: all three regions and both breakpoints exactly.
ENVELOPE_AT = [("0.1*f1", 0.1, 1), ("f1", 1.0, 1), ("3*f1", 3.0, 1),
               ("f2", 1.0, 2), ("10*f2", 10.0, 2)]


def main() -> int:
    cases = []
    for name, harmonics, kw in CASES:
        trap = Trapezoid(**kw)
        f1, f2 = corner_frequencies(trap)
        envelope = []
        for label, factor, which in ENVELOPE_AT:
            f = factor * (f1 if which == 1 else f2)
            envelope.append({"label": label, "frequency_hz": f,
                             "amplitude_v": envelope_v(trap, f)})
        cases.append({
            "name": name,
            "input": kw,
            "harmonics": harmonics,
            "expected": {
                "corner_1_hz": f1,
                "corner_2_hz": f2,
                "series": [{"frequency_hz": f, "amplitude_v": a}
                           for f, a in trapezoid_series(trap, harmonics)],
                "envelope": envelope,
            },
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "_comment": (
            "Shared fixtures for driver spectra. The Python and TypeScript implementations "
            "must reproduce these exactly. Regenerate with `make driver-fixtures`, never by "
            "hand."
        ),
        "cases": cases,
    }, indent=2) + "\n")
    print(f"wrote {OUT} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
