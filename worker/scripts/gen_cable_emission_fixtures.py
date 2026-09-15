#!/usr/bin/env python3
"""Regenerate the shared cable-emission fixtures.

Asserted against by BOTH worker/tests/test_cable_emission.py and
webapp/src/lib/cableEmission.test.ts. The composition decides the dBµV/m a user reads next to
a limit line, in a worker result and in the browser alike, so the two implementations have to
agree rather than merely resemble each other.

The cases are chosen to cover each way a point can fail to exist as well as the arithmetic:
an unusable transfer point, a null in the driver, a frequency the antenna solver never saw,
and a zero impedance.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emi_worker.cables.emission import compose  # noqa: E402

OUT = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
       / "cable_emission_fixtures.json")

# Round numbers, so a disagreement is a bug rather than a rounding difference: at 100 MHz
# H = 0.01, V = 1 V and Z = 300 ohm give exactly 33.33 uA, and 25 V/m/A of radiation gives
# 833.3 uV/m, which is 58.4 dBuV/m.
CASES = [
    {
        "name": "plain-arithmetic",
        "ref": "USB1",
        "cable_id": "usb2-shielded",
        "standard_id": "fcc-15b-radiated-3m",
        "frequencies_hz": [50e6, 100e6, 300e6, 700e6],
        "h": [[0.02, 0.0], [0.01, 0.0], [0.004, 0.003], [0.001, -0.001]],
        "usable": [True, True, True, True],
        "source_v": [[1.0, 0.0], [1.0, 0.0], [0.5, 0.0], [0.25, 0.0]],
        "z": [[80.0, -900.0], [300.0, 0.0], [180.0, 120.0], [70.0, -40.0]],
        "e_per_amp": [6.0, 25.0, 48.0, 62.0],
    },
    {
        "name": "every-way-a-point-goes-missing",
        "ref": "J3",
        "cable_id": "dc-pigtail",
        "standard_id": "fcc-15b-radiated-3m",
        "frequencies_hz": [40e6, 80e6, 160e6, 320e6],
        "h": [[0.03, 0.0], [0.02, 0.0], [0.01, 0.0], [0.005, 0.0]],
        #                unusable       driver null    no antenna     zero impedance
        "usable": [False, True, True, True],
        "source_v": [[1.0, 0.0], None, [1.0, 0.0], [1.0, 0.0]],
        "z": [[50.0, 0.0], [50.0, 0.0], [50.0, 0.0], [0.0, 0.0]],
        "e_per_amp": [10.0, 10.0, 10.0, 10.0],
        # 160 MHz is deliberately absent from the antenna solver's grid.
        "antenna_frequencies_hz": [40e6, 80e6, 320e6],
    },
]


def build(case: dict) -> dict:
    freqs = case["frequencies_hz"]
    transfer = {
        "ref": case["ref"],
        "frequencies_hz": freqs,
        "h_real": [h[0] for h in case["h"]],
        "h_imag": [h[1] for h in case["h"]],
        "usable": case["usable"],
    }
    source = {f: complex(v[0], v[1])
              for f, v in zip(freqs, case["source_v"]) if v is not None}
    a_freqs = case.get("antenna_frequencies_hz", freqs)
    antenna = {}
    for f in a_freqs:
        k = freqs.index(f)
        antenna[f] = (complex(case["z"][k][0], case["z"][k][1]), case["e_per_amp"][k])

    em = compose(case["ref"], case["cable_id"], transfer, source, antenna,
                 standard_id=case["standard_id"])
    return {
        "name": case["name"],
        "input": {
            "transfer": transfer,
            "antenna": {
                "ref": case["ref"], "cable_id": case["cable_id"], "length_m": 1.0,
                "distance_m": 3.0,
                "frequencies_hz": a_freqs,
                "z_real": [case["z"][freqs.index(f)][0] for f in a_freqs],
                "z_imag": [case["z"][freqs.index(f)][1] for f in a_freqs],
                "e_per_amp": [case["e_per_amp"][freqs.index(f)] for f in a_freqs],
            },
            "source_volts": case["source_v"],
            "standard_id": case["standard_id"],
        },
        "expected": {
            "points": [
                {
                    "frequency_hz": p.frequency_hz,
                    "current_dbua": p.current_dbua,
                    "field_dbuv_per_m": p.field_dbuv_per_m,
                    "limit_dbuv_per_m": p.limit_dbuv_per_m,
                    "margin_db": p.margin_db,
                }
                for p in em.points
            ],
            "undriven_hz": sorted(em.undriven),
        },
    }


def main() -> None:
    doc = {"cases": [build(c) for c in CASES]}
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {OUT} ({len(doc['cases'])} cases)")


if __name__ == "__main__":
    main()
