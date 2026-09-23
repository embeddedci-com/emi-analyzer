#!/usr/bin/env python3
"""Regenerate the shared compliance fixtures (docs/implementation.md §9).

Asserted by BOTH worker/tests/test_predict.py and webapp/src/lib/compliance.test.ts. The margin
and the confidence beside it are the two numbers a user is most likely to quote at someone else,
so the browser and the worker have to produce them identically rather than nearly.

The cases cover the three things that are easy to get subtly wrong and impossible to spot
afterwards: coherent versus incoherent summation, the power-share weighting of sigma, and the
near-miss window.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emi_worker.compliance.predict import outlook  # noqa: E402
from emi_worker.compliance.predict import Path as EPath  # noqa: E402
from emi_worker.compliance.predict import PathPoint, from_dbuv  # noqa: E402

OUT = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
       / "compliance_fixtures.json")

STD = "fcc-15b-radiated-3m"

CASES = [
    {
        "name": "design-doc-worked-example",
        # The radiated row of the uncertainty budget: cable 4.5, driver 3, mesh normal 2,
        # interpolation 1 -> sigma 5.85, margin +5.1, 81 %, -2.4 .. +12.6.
        "standard_id": STD,
        "frequencies_hz": [100e6],
        "paths": [
            {"kind": "cable", "label": "cable J1 (USB 2.0, 1 m)", "driver_id": "clk",
             # 43.5 dBuV/m is the Class B limit at 100 MHz; 5.1 dB under it is 38.4.
             "field_dbuv_per_m": [38.4],
             "sigma_terms": {"cable idealisation": 4.5}},
        ],
        "shared_sigma_terms": {"driver provenance": 3.0, "mesh preset": 2.0,
                               "far-field interpolation": 1.0},
    },
    {
        "name": "coherent-and-incoherent-summation",
        # Two paths of one driver add in amplitude (+6 dB for equal pair); a third from a
        # different driver adds in power.
        "standard_id": STD,
        "frequencies_hz": [200e6],
        "paths": [
            {"kind": "board", "label": "board region around U3", "driver_id": "clk",
             "field_dbuv_per_m": [40.0], "sigma_terms": {"mesh preset": 2.0}},
            {"kind": "cable", "label": "cable J1", "driver_id": "clk",
             "field_dbuv_per_m": [40.0], "sigma_terms": {"cable idealisation": 4.5}},
            {"kind": "board", "label": "board region around U7", "driver_id": "buck",
             "field_dbuv_per_m": [40.0], "sigma_terms": {"mesh preset": 2.0}},
        ],
        "shared_sigma_terms": {},
    },
    {
        "name": "near-misses-and-a-quiet-point",
        "standard_id": STD,
        "frequencies_hz": [100e6, 200e6, 500e6],
        "paths": [
            {"kind": "board", "label": "board region around U3", "driver_id": "clk",
             "field_dbuv_per_m": [41.0, 44.0, 10.0],
             "sigma_terms": {"mesh preset": 4.0}},
        ],
        "shared_sigma_terms": {},
    },
]


def build(case: dict) -> dict:
    freqs = case["frequencies_hz"]
    paths = [
        EPath(kind=p["kind"], label=p["label"], driver_id=p["driver_id"],
              points=[PathPoint(frequency_hz=f, field_v_per_m=from_dbuv(v))
                      for f, v in zip(freqs, p["field_dbuv_per_m"])],
              sigma_terms=p["sigma_terms"])
        for p in case["paths"]
    ]
    o = outlook(paths, freqs, case["standard_id"],
                shared_terms=case["shared_sigma_terms"])
    return {
        "name": case["name"],
        "input": {
            "standard_id": case["standard_id"],
            "frequencies_hz": freqs,
            "paths": case["paths"],
            "shared_sigma_terms": case["shared_sigma_terms"],
        },
        "expected": {
            "spectrum": [
                {"frequency_hz": pt.frequency_hz,
                 "field_dbuv_per_m": pt.field_dbuv_per_m,
                 "limit_dbuv_per_m": pt.limit_dbuv_per_m,
                 "margin_db": pt.margin_db,
                 "shares_indicative": pt.shares_indicative,
                 "contributions": [
                     {"label": c.label, "share": c.share} for c in pt.contributions
                 ]}
                for pt in o.points
            ],
            "worst_frequency_hz": o.worst.frequency_hz if o.worst else None,
            "margin_db": o.margin_db,
            "sigma_db": o.sigma_db,
            "sigma_terms": o.sigma_terms,
            "confidence_uncalibrated": o.confidence_uncalibrated,
            "range_80_db": list(o.range_80_db) if o.range_80_db else None,
            "near_misses_hz": [q.frequency_hz for q in o.near_misses],
        },
    }


def main() -> None:
    OUT.write_text(json.dumps({"cases": [build(c) for c in CASES]}, indent=2) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
