#!/usr/bin/env python3
"""Regenerate the shared cable-emission fixtures.

Asserted against by BOTH worker/tests/test_cable_emission.py and
webapp/src/lib/cableEmission.test.ts. The composition decides the dBµV/m a user reads next to
a limit line, in the compliance estimate and in the Cables tab alike, so the two
implementations have to agree rather than merely resemble each other.

The fixtures go through ``compliance.assemble.compose_cable``, the function the estimate
uses. They used to go through a second composition that ran on the solve's own grid and
ignored the driver's source impedance; the chart drew that one, so the chart and the estimate
could disagree by the source-impedance factor and by every harmonic between grid points.

The cases cover what that second composition got wrong as well as the arithmetic: harmonics
between grid points, a driver whose source impedance differs from the port's, a continuous
spectrum read on the grid, and each way a point can fail to exist.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emi_worker.compliance.assemble import compose_cable, port_impedance  # noqa: E402
from emi_worker.drivers.document import parse as parse_driver  # noqa: E402

OUT = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
       / "cable_emission_fixtures.json")

STD = "fcc-15b-radiated-3m"
GRID = [30e6, 60e6, 120e6, 250e6, 480e6, 960e6]


def clock(z_ohm: float, *, source: str = "datasheet") -> dict:
    return {
        "format": "emi-driver", "version": 1, "name": "25 MHz clock", "kind": "trapezoid",
        "role": "signal",
        "trapezoid": {
            "amplitude_v": {"value": 3.3, "source": "scope"},
            "period_s": {"value": 4e-8, "source": "scope"},
            "pulse_width_s": {"value": 2e-8, "source": "scope"},
            "rise_s": {"value": 1.2e-9, "source": source},
            "fall_s": {"value": 1.2e-9, "source": source},
            "source_impedance_ohm": {"value": z_ohm, "source": "assumed"},
        },
    }


SPECTRUM_DRIVER = {
    "format": "emi-driver", "version": 1, "name": "analyser trace", "kind": "spectrum",
    "role": "signal",
    "spectrum": {
        "rbw_hz": 120000,
        "points": [{"frequency_hz": 50e6, "level_dbuv": 100.0},
                   {"frequency_hz": 500e6, "level_dbuv": 80.0}],
        "source_impedance_ohm": {"value": 50, "source": "assumed"},
    },
}

#: A capture with no rise time and a 200 MHz bandwidth: every harmonic above it is undriven.
NO_RISE_WAVEFORM = {
    "format": "emi-driver", "version": 1, "name": "slow capture", "kind": "waveform",
    "role": "signal",
    "waveform": {
        "sample_interval_s": 2e-9,
        "samples_v": [0.0, 3.3, 3.3, 3.3, 3.3, 3.3, 3.3, 3.3, 3.3, 3.3, 0.0,
                      0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "bandwidth_hz": 2e8,
        "period_s": {"value": 4e-8, "source": "benchpod"},
        "source_impedance_ohm": {"value": 50, "source": "assumed"},
    },
}


def port(ref: str, h: list, usable: list | None = None) -> dict:
    return {
        "ref": ref, "driven_by": "p1",
        "transfer": {
            "frequencies_hz": GRID,
            "h_real": [v[0] for v in h], "h_imag": [v[1] for v in h],
            "usable": usable or [True] * len(GRID),
        },
    }


def antenna(ref: str, z: list, e: list, freqs: list | None = None) -> dict:
    return {
        "ref": ref, "cable_id": "usb2-shielded", "length_m": 1.0, "distance_m": 3.0,
        "frequencies_hz": freqs or GRID,
        "z_real": [v[0] for v in z], "z_imag": [v[1] for v in z], "e_per_amp": e,
    }


def spectrum(z_in: list) -> dict:
    """A port spectrum whose V/I is ``z_in`` at every grid point (I = 10 mA)."""
    return {
        "frequencies_hz": GRID,
        "v_real": [0.01 * z[0] for z in z_in], "v_imag": [0.01 * z[1] for z in z_in],
        "i_real": [0.01] * len(GRID), "i_imag": [0.0] * len(GRID),
    }


H = [[0.02, 0.0], [0.015, 0.005], [0.01, 0.004], [0.006, -0.002], [0.003, 0.001],
     [0.001, 0.0]]
Z_ANT = [[40.0, -900.0], [120.0, -150.0], [300.0, 60.0], [180.0, 120.0], [90.0, -40.0],
         [200.0, 10.0]]
E_AMP = [4.0, 12.0, 25.0, 40.0, 55.0, 62.0]
Z_IN = [[48.0, -30.0], [50.0, -12.0], [52.0, 5.0], [55.0, 20.0], [60.0, 45.0], [70.0, 90.0]]

CASES = [
    {
        # A 25 MHz clock has 37 harmonics in 30-960 MHz and only one of them, 250 MHz, is a
        # grid point. The chart composed on the grid used to drive that one alone.
        "name": "harmonics-between-grid-points",
        "port": port("USB1", H), "antenna": antenna("USB1", Z_ANT, E_AMP),
        "spectrum": spectrum(Z_IN), "z_s": 50.0, "driver": clock(50.0),
    },
    {
        # The same, with a 10 ohm driver. Every point moves by |50 + Z_in| / |10 + Z_in|,
        # which the chart used to leave out.
        "name": "driver-source-impedance-replaces-the-ports",
        "port": port("USB1", H), "antenna": antenna("USB1", Z_ANT, E_AMP),
        "spectrum": spectrum(Z_IN), "z_s": 50.0, "driver": clock(10.0),
    },
    {
        # A continuous spectrum is read at the transfer function's own grid points, inside
        # the range it covers (50-500 MHz).
        "name": "uploaded-spectrum-on-the-grid",
        "port": port("J2", H), "antenna": antenna("J2", Z_ANT, E_AMP),
        "spectrum": spectrum(Z_IN), "z_s": 50.0, "driver": SPECTRUM_DRIVER,
    },
    {
        # A hole in the solve's energy at 120 MHz, an antenna solved only to 480 MHz, and a
        # capture that knows nothing above 200 MHz.
        "name": "every-way-a-point-goes-missing",
        "port": port("J3", H, usable=[True, True, False, True, True, True]),
        "antenna": antenna("J3", Z_ANT[:5], E_AMP[:5], GRID[:5]),
        "spectrum": spectrum(Z_IN), "z_s": 50.0, "driver": NO_RISE_WAVEFORM,
    },
]


def build(case: dict) -> dict:
    comp = compose_cable(case["port"]["transfer"], case["antenna"],
                         port_impedance(case["spectrum"]), case["z_s"],
                         parse_driver(case["driver"]), STD, ref=case["port"]["ref"])
    return {
        "name": case["name"],
        "input": {
            "port": case["port"], "antenna": case["antenna"], "spectrum": case["spectrum"],
            "z_s": case["z_s"], "driver": case["driver"], "standard_id": STD,
        },
        "expected": {
            "covered_hz": list(comp.covered) if comp.covered else None,
            "band_hz": list(comp.band) if comp.band else None,
            "line": comp.line,
            "points": [{"frequency_hz": p.frequency_hz, "current_a": p.current_a,
                        "field_v_per_m": p.field_v_per_m} for p in comp.points],
            "undriven_hz": sorted(comp.undriven),
        },
    }


def main() -> None:
    doc = {"cases": [build(c) for c in CASES]}
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {OUT} ({len(doc['cases'])} cases)")


if __name__ == "__main__":
    main()
