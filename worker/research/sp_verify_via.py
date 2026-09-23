"""Small-part check 2: is a via's inductance what the closed form says it is?

A via between two planes, and the coupon's port beside it: a two-layer board poured with ground
on both sides, a 0.4 mm pad on the top for the port, and one ground via s mm away joining the
planes. The port drives between the planes at the pad, the via shorts them, and below the
first resonance Z_in = R + jwL, where L is the inductance of the loop port - top plane - via -
bottom plane.

Between two parallel plates that loop has an exact closed form. The plates image every vertical
current into an infinite column, so the fields do not vary with z and the posts are a two-wire
line of length h, with no end effects to model:

    L = (mu0 / 2 pi) h acosh((s^2 - r1^2 - r2^2) / (2 r1 r2))

r1 is the port's and r2 the via's radius. The model draws both as square boxes, a port 0.4 mm
across and a via as wide as its pad; a square conductor of side a carries surface current like
a round one of radius 0.59 a (conformal mapping), which is the radius used here. h is the
distance between the two copper sheets as the model places them (``model._layer_z``).

The earlier form of this check -- a pad, a strap and a via to a plane, against Goldfarb and
Pucel's via-to-ground formula -- read 57 % high on the difference between two via sizes, on
both presets alike. That configuration has a strap-to-via junction and a loop whose ends are
not images of each other, neither of which the formula models; the parallel-plate one has
neither.

Pass: L within 10 % of the closed form for every via size and spacing, on each preset.

    docker run --rm --cpus 3 --memory 6g -v "$PWD/worker:/spike" \\
        -v "$PWD/spike_out:/spike/spike_out" -w /spike -e PYTHONPATH=/spike \\
        --entrypoint python3 emi-worker:smallpart research/sp_verify_via.py
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sp_harness as h  # noqa: E402
from emi_worker.kicad import parse, parse_board  # noqa: E402
from emi_worker.openems import post  # noqa: E402
from emi_worker.openems.coupon import PORT_HALF_WIDTH_MM  # noqa: E402
from emi_worker.openems.model import _layer_z  # noqa: E402

H_DIEL = 1.5
T_CU = 0.035
#: (via diameter, port-to-via spacing), mm.
CASES = [(0.3, 1.0), (0.3, 2.0), (0.8, 2.0)]
#: Where L is read: well below the first resonance, where Im(Z) is a few ohms.
READ_HZ = np.array([100e6, 150e6, 200e6, 300e6])
TOL = 0.10
SQUARE_TO_ROUND = 0.59


def board(d_via: float, spacing: float) -> str:
    plane = "(pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20))"
    return f"""(kicad_pcb
  (version 20241229)
  (generator "emi-analyzer-small-part-check")
  (general (thickness {H_DIEL + 2 * T_CU}))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness {T_CU}))
    (layer "dielectric 1" (type "core") (thickness {H_DIEL}) (material "FR4")
           (epsilon_r 4.4) (loss_tangent 0))
    (layer "B.Cu" (type "copper") (thickness {T_CU}))))
  (net 0 "") (net 1 "GND") (net 2 "PORT")
  (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts") (width 0.1))
  (footprint "Pad" (layer "F.Cu") (at 9 10)
    (property "Reference" "TP1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu") (net 2 "PORT")))
  (via (at {9 + spacing} 10) (size {d_via}) (drill {d_via / 2}) (layers "F.Cu" "B.Cu") (net 1))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
    (polygon {plane}) (filled_polygon (layer "F.Cu") {plane}))
  (zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5)
    (polygon {plane}) (filled_polygon (layer "B.Cu") {plane}))
)
"""


def two_posts(h_mm: float, s_mm: float, r1_mm: float, r2_mm: float) -> float:
    """Loop inductance of two posts between parallel plates, H. Exact in the quasi-static limit."""
    arg = (s_mm ** 2 - r1_mm ** 2 - r2_mm ** 2) / (2 * r1_mm * r2_mm)
    return 2e-7 * (h_mm / 1000.0) * math.acosh(arg)


def main() -> int:
    presets = os.environ.get("PRESETS", "coarse,normal").split(",")
    report: dict = {"cases": CASES, "presets": {}}
    for preset in presets:
        rows = []
        for d, s in CASES:
            text = board(d, s)
            lz = _layer_z(parse_board(parse(text)))
            h_mm = lz["F.Cu"] - lz["B.Cu"]
            params, c = h.coupon_params(text, ["PORT"], preset=preset)
            got = h.solve(text, params, f"via-{d}-{s}-{preset}")
            src = post.source_spectrum(str(got.workdir), "p1", 50.0, READ_HZ.tolist())
            z = src["z_in"]
            l_meas = z.imag / (2 * np.pi * READ_HZ)
            r1 = SQUARE_TO_ROUND * 2 * PORT_HALF_WIDTH_MM
            r2 = SQUARE_TO_ROUND * d
            ref = two_posts(h_mm, s, r1, r2)
            err = l_meas / ref - 1
            smry = got.summary
            print(f"== {preset}, via {d} mm at {s} mm: {smry['cells']:,} cells, "
                  f"{smry['timesteps']:,} steps, {got.elapsed_s:.0f} s, "
                  f"{smry['final_energy_db']:.1f} dB; L {np.array2string(l_meas * 1e9, precision=3)}"
                  f" nH against {ref * 1e9:.3f} nH: {np.array2string(err * 100, precision=1)} %",
                  flush=True)
            rows.append({"via_mm": d, "spacing_mm": s, "h_mm": h_mm, "l_h": l_meas.tolist(),
                         "r_ohm": z.real.tolist(), "reference_h": ref, "error": err.tolist(),
                         "cells": smry["cells"], "timesteps": smry["timesteps"],
                         "elapsed_s": got.elapsed_s, "final_energy_db": smry["final_energy_db"],
                         "pass": bool(np.all(np.abs(err) <= TOL))})
        report["presets"][preset] = {"frequencies_hz": READ_HZ.tolist(), "rows": rows,
                                     "pass": all(r["pass"] for r in rows)}
    print("\n", h.write_report("via", report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
