#!/usr/bin/env python3
"""Verification · why Tier B read low, with nec2c alone (docs/verification/cables-and-drivers.md §4).

Cable test 4 found Tier B 6-15 dB below a fully coupled openEMS solve on real boards, from
45 MHz up to the first resonance, and agreeing at it. Tier B is ``I = V_oc / Z_ant``: V_oc from
the solve's 1 MOhm gap, Z_ant from nec2c. This asks which half was wrong, in nec2c, in seconds.

**Part 1, the antenna.** Z_ant with the board as a thin wire as long as the board (what Tier B
used) against the board as a plate, a wire grid of its outline (what the grid in test 4
holds). Same cable, same feed, free space, test 4's 31 frequencies.

**Part 2, the composition.** The whole of cable test 4 rebuilt in nec2c, so both sides are
exact to the same solver:

    coupled  a plate board, a source loop on it (a 5 mm high wire between two grid nodes,
             1 V in one leg and 50 ohm in the other), a gap wire at the connector, a 10 mm
             stub and the cable. The current at the cable's root is the reference.
    Tier B   the same without the cable, the gap carrying 1 MOhm: V_oc. Composed with
             the thin-wire Z_ant, with the plate Z_ant, and with the coupled structure's own
             impedance at the gap.
    V_oc'    V_oc with the cable attached (gap still open). V_oc' / Z_gap must reproduce the
             coupled current exactly; it does to 0.5 dB, which is nec2c's own floor here.

The source sits near the connector, in the middle, or across the board (``LOOPS``), because
what attaching the cable does to V_oc depends on where the source is.

    docker run --rm --cpus 2 -v "$PWD/worker:/spike" -w /spike -e PYTHONPATH=/spike \\
        --entrypoint python3 emi-worker:main research/verify_cable_tier_b_plate.py

``BOARDS`` is ``span:width,...`` in mm, the board along the exit and across it (the test-4
strips were 60 x 30 and 100 x 30 mm). Runs two nec2c processes at a time.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from emi_worker.cables import nec

C = 299_792_458.0
F = np.geomspace(30e6, 600e6, 31)
CABLE_M = float(os.environ.get("CABLE_M", "0.3"))
#: Test 4's cable: a square column one 2 mm cell across, at its equivalent radius.
R_CABLE = 0.59 * 2.0e-3
BOARDS = [tuple(float(v) / 1000 for v in b.split(":"))
          for b in os.environ.get("BOARDS", "60:30,100:30").split(",")]
LOOPS = {"near": (-0.35, -0.1), "mid": (-0.6, -0.3), "far": (-0.9, -0.6)}
GAP = 0.005          # several radii long, or nec2c's thin-wire kernel misbehaves
STUB = 0.010
OPEN_OHM = 1e6

_NUM = r"[-+]?\d+\.?\d*(?:[Ee][-+]?\d+)?"
_CUR = re.compile(rf"^\s*(\d+)\s+(\d+)\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+({_NUM})\s+({_NUM})"
                  rf"\s+{_NUM}\s+{_NUM}\s*$", re.M)


def _run(text: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        i, o = Path(d) / "d.nec", Path(d) / "d.out"
        i.write_text(text)
        subprocess.run(["nec2c", f"-i{i}", f"-o{o}"], check=True, capture_output=True)
        return o.read_text()


def _z_and_currents(out: str) -> tuple[complex, dict[int, complex]]:
    m = nec._IMPEDANCE.search(out.split("ANTENNA INPUT PARAMETERS", 1)[1])
    z = complex(float(m.group(1)), float(m.group(2)))
    block = out.split("CURRENTS AND LOCATION", 1)[1]
    first: dict[int, complex] = {}
    for m in _CUR.finditer(block):
        first.setdefault(int(m.group(2)), complex(float(m.group(3)), float(m.group(4))))
    return z, first


def coupled(span, width, f, loop, *, gap_ohm=None, cable=True, feed="loop"):
    """The deck for part 2. Returns (z at the feed, first-segment currents by tag)."""
    seg = min(C / f / 20, nec.MAX_SEGMENT_M)
    cards = ["CM tier b in nec2c", "CE"]
    cards += nec._plate_cards(span, width, None, 0.0, first_tag=10)
    # The loop's feet on grid nodes, or nec2c leaves it floating beside the plate.
    pitch = nec.plate_pitch(span, width)
    xs, ys = nec._lattice(-span, 0.0, pitch), nec._lattice(-width / 2, width / 2, pitch)
    xa, xb = (min(xs, key=lambda x: abs(x - v * span)) for v in loop)
    y, h = min(ys, key=lambda v: abs(v + 0.010)), 0.005
    cards += [f"GW 1 1 {xa} {y} 0 {xa} {y} {h} 0.0005",
              f"GW 2 {max(1, round(abs(xb - xa) / 0.005))} {xa} {y} {h} {xb} {y} {h} 0.0005",
              f"GW 3 1 {xb} {y} {h} {xb} {y} 0 0.0005",
              f"GW 4 1 0 0 0 {GAP} 0 0 {R_CABLE}",
              f"GW 5 2 {GAP} 0 0 {GAP + STUB} 0 0 {R_CABLE}"]
    if cable:
        n = math.ceil(CABLE_M / seg)
        cards.append(f"GW 6 {n} {GAP + STUB} 0 0 {GAP + STUB + CABLE_M} 0 0 {R_CABLE}")
    cards += ["GE 0", "LD 4 3 1 1 50 0"]
    if gap_ohm:
        cards.append(f"LD 4 4 1 1 {gap_ohm} 0")
    cards.append("EX 0 1 1 0 1 0" if feed == "loop" else "EX 0 4 1 0 1 0")
    cards += [f"FR 0 1 0 0 {f / 1e6:.6f} 0", "XQ 0", "EN"]
    return _z_and_currents(_run("\n".join(cards) + "\n"))


def z_ant(span, width, f):
    deck = nec.Deck(length_m=CABLE_M, frequency_hz=f, board_span_m=span, radius_m=R_CABLE,
                    board_width_m=width, far_end="open", ground=False)
    return nec.run(deck).z_in


def point(args):
    span, width, f, loop = args
    _, cur = coupled(span, width, f, loop)
    i_ref = cur[6]
    _, cur = coupled(span, width, f, loop, gap_ohm=OPEN_OHM, cable=False)
    v_oc = cur[4] * OPEN_OHM
    _, cur = coupled(span, width, f, loop, gap_ohm=OPEN_OHM)
    v_oc_cable = cur[4] * OPEN_OHM
    z_gap, _ = coupled(span, width, f, loop, feed="gap")
    return dict(i_ref=i_ref, v_oc=v_oc, v_oc_cable=v_oc_cable, z_gap=z_gap,
                z_thin=z_ant(span, None, f), z_plate=z_ant(span, width, f))


def _db(x):
    return 20 * np.log10(np.abs(x))


def main() -> None:
    pool = ThreadPoolExecutor(max_workers=2)
    for span, width in BOARDS:
        print(f"\n== board {span * 1e3:.0f} x {width * 1e3:.0f} mm, {CABLE_M:g} m cable")
        zt = np.array(list(pool.map(lambda f: z_ant(span, None, f), F)))
        zp = np.array(list(pool.map(lambda f: z_ant(span, width, f), F)))
        below = F < min(F[i] for i in range(len(F) - 1) if zp[i].imag < 0 <= zp[i + 1].imag)
        d = _db(zt / zp)
        print(f"part 1: thin-wire |Z_ant| over the plate's, below the plate's resonance: "
              f"{d[below].min():+.1f} to {d[below].max():+.1f} dB, mean {d[below].mean():+.1f}")
        for name in os.environ.get("LOOPS", "near,mid,far").split(","):
            rows = list(pool.map(point, [(span, width, float(f), LOOPS[name]) for f in F]))
            g = {k: np.array([r[k] for r in rows]) for k in rows[0]}
            errs = {
                "thin": _db(g["v_oc"] / g["z_thin"] / g["i_ref"]),
                "plate": _db(g["v_oc"] / g["z_plate"] / g["i_ref"]),
                "V_oc'/Z_gap": _db(g["v_oc_cable"] / g["z_gap"] / g["i_ref"]),
                "V_oc'/V_oc": _db(g["v_oc_cable"] / g["v_oc"]),
            }
            print(f"part 2, source {name}: below resonance, min / mean / max dB")
            for k, e in errs.items():
                print(f"    {k:12s} {e[below].min():+6.1f} {e[below].mean():+6.1f} "
                      f"{e[below].max():+6.1f}")


if __name__ == "__main__":
    main()
