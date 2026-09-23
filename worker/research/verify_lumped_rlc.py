"""Phase 2 · Does the installed openEMS model a lumped inductor?

CSXCAD reads ``L`` on a LumpedElement, and the writer here emits it, but reading an attribute
is not the same as modelling it. openEMS 0.0.35's ``Operator::Calc_LumpedElements`` knows R and
C; an element with neither is skipped with a warning (``R or C not specified! skipping``) and
the cell is left as whatever material is there. If that is what happens, the middle cell of
``csx.series_rlc`` is an open gap and every capacitor model is an open circuit.

This is a circuit test, not a board: a 50 ohm port drives a short PEC strip over a ground plate
and a z-directed element closes the loop back to the plate. The same loop is solved with the
element replaced by PEC, and that reference is subtracted, so what is left is the element's own
impedance with the loop's inductance removed:

    Z_element(f) = Z_in(f) - Z_in,short(f)

and it is compared with the value the element was given. Each case also reports every
openEMS warning, so a skipped element is visible in the log as well as in the numbers.

Runs in the worker image with this folder's parent mounted. Pass ``--le-type`` to also try
the series-RLC element (``LEtype="1"``), which only the openEMS build path understands:

    docker run --rm -v "$PWD/worker:/spike" -v "$PWD/spike_out:/spike/spike_out" -w /spike \\
        -e PYTHONPATH=/spike --entrypoint python3 emi-worker:phase1 \\
        research/verify_lumped_rlc.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

import numpy as np

from emi_worker.openems import csx, post, run

OUT = Path(os.environ.get("OUT", "/spike/spike_out")) / "lumped_rlc"
THREADS = int(os.environ.get("THREADS", "3"))
FREQS = [100e6, 200e6, 300e6, 500e6, 700e6, 1e9]

#: The loop: port at x = -2 mm, strip at z = 1 mm, element at x = +2 mm. Millimetres.
H = 1.0
PORT_X = -2.0
ELEM_X = 2.0
HALF_W = 0.5

#: (name, R, L, C, LEtype). None means the attribute is not written.
CASES = [
    ("short", None, None, None, None),
    ("R_20", 20.0, None, None, None),
    ("L_10nH", None, 10e-9, None, None),
    ("C_10pF", None, None, 10e-12, None),
]
SERIES_CASES = [
    # A series R-L-C in one element. SRF = 1/(2 pi sqrt(LC)) = 503 MHz.
    ("RLC_series", 1.0, 10e-9, 10e-12, 1),
    ("L_10nH_series", None, 10e-9, None, 1),
]


def grid() -> tuple[list[float], list[float], list[float]]:
    step = 0.25
    x = list(np.round(np.arange(-12.0, 12.0 + step / 2, step), 6))
    y = list(np.round(np.arange(-10.0, 10.0 + step / 2, step), 6))
    z = list(np.round(np.arange(-8.0, 9.0 + step / 2, step), 6))
    return x, y, z


def element(name: str, r, l, c, le_type) -> csx.Property:
    box = csx.Box(p1=(ELEM_X - HALF_W, -HALF_W, 0.0), p2=(ELEM_X + HALF_W, HALF_W, H),
                  priority=csx.PRIORITY_PORT)
    if r is None and l is None and c is None:
        return csx.Metal(name="elem_short", primitives=[box])
    el = csx.LumpedElement(name=f"elem_{name}", direction=2, resistance=r, inductance=l,
                           capacitance=c, primitives=[box], le_type=le_type)
    return el


def build(name: str, r, l, c, le_type) -> csx.CSXDocument:
    x, y, z = grid()
    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=550e6, fc=550e6),
        x_lines=x, y_lines=y, z_lines=z, f_max=1.1e9,
        end_criteria=1e-5, max_timesteps=400_000,
    )
    doc.add(csx.Metal(name="plate", primitives=[
        csx.Box(p1=(-6.0, -4.0, 0.0), p2=(6.0, 4.0, 0.0), priority=csx.PRIORITY_METAL)]))
    doc.add(csx.Metal(name="strip", primitives=[
        csx.Box(p1=(PORT_X - HALF_W, -HALF_W, H), p2=(ELEM_X + HALF_W, HALF_W, H),
                priority=csx.PRIORITY_METAL)]))
    port_box = csx.Box(p1=(PORT_X - HALF_W, -HALF_W, 0.0), p2=(PORT_X + HALF_W, HALF_W, H),
                       priority=csx.PRIORITY_PORT)
    doc.add(csx.LumpedElement(name="port_res", direction=2, resistance=50.0,
                              primitives=[port_box]))
    doc.add(csx.ExcitationProperty(name="port_exc", excite=(0.0, 0.0, -1.0),
                                   primitives=[port_box]))
    doc.add(csx.ProbeBox(name="port_ut", type=0, norm_dir=2, weight=-1.0, primitives=[
        csx.Box(p1=(PORT_X, 0.0, 0.0), p2=(PORT_X, 0.0, H))]))
    doc.add(csx.ProbeBox(name="port_it", type=1, norm_dir=2, weight=1.0, primitives=[
        csx.Box(p1=(PORT_X - 0.75, -0.75, H / 2), p2=(PORT_X + 0.75, 0.75, H / 2))]))
    doc.add(element(name, r, l, c, le_type))
    return doc


def solve(name: str, doc: csx.CSXDocument) -> tuple[np.ndarray, list[str], run.RunResult]:
    work = OUT / name
    work.mkdir(parents=True, exist_ok=True)
    xml = work / "model.xml"
    xml.write_text(doc.to_string())
    result = run.run_openems(str(xml), str(work), threads=THREADS)
    u = post.read_probe(str(work / "port_ut"))
    i = post.read_probe(str(work / "port_it"))
    f = np.asarray(FREQS)
    z = post._dft(u, f) / post._dft(i, f)
    warnings = sorted({ln.strip() for ln in result.log_text.splitlines()
                       if re.search(r"warning|skipping", ln, re.I)})
    return z, warnings, result


def expected(r, l, c, le_type, f: np.ndarray) -> np.ndarray:
    w = 2 * np.pi * f
    parts = []
    if r is not None:
        parts.append(np.full_like(w, r, dtype=complex))
    if l is not None:
        parts.append(1j * w * l)
    if c is not None:
        parts.append(1 / (1j * w * c))
    if le_type == 1:
        return sum(parts)
    # Parallel: the only way 0.0.35 wires more than one value.
    return 1 / sum(1 / p for p in parts)


def main() -> int:
    cases = list(CASES)
    if "--le-type" in sys.argv:
        cases += SERIES_CASES
    f = np.asarray(FREQS)
    z_short, warn_short, _ = solve("short", build("short", None, None, None, None))
    report = {"frequencies_hz": FREQS, "cases": {}}
    print("loop inductance from the shorted reference: "
          + ", ".join(f"{z.imag / (2 * math.pi * fr) * 1e9:.2f} nH" for z, fr in zip(z_short, f)))
    for name, r, l, c, le_type in cases:
        if name == "short":
            continue
        z, warns, res = solve(name, build(name, r, l, c, le_type))
        z_el = z - z_short
        want = expected(r, l, c, le_type, f)
        err_db = 20 * np.log10(np.abs(z_el) / np.abs(want))
        report["cases"][name] = {
            "R": r, "L": l, "C": c, "LEtype": le_type,
            "z_element_real": z_el.real.tolist(), "z_element_imag": z_el.imag.tolist(),
            "expected_real": want.real.tolist(), "expected_imag": want.imag.tolist(),
            "error_db": err_db.tolist(), "warnings": warns,
            "timesteps": res.final_timestep, "final_energy_db": res.final_energy_db,
        }
        print(f"\n== {name}  (R={r} L={l} C={c} LEtype={le_type})")
        for w in warns:
            print(f"   openEMS: {w}")
        print(f"   {'f MHz':>7} {'Z_el':>22} {'expected':>22} {'err dB':>7}")
        for fr, ze, zw, e in zip(f, z_el, want, err_db):
            print(f"   {fr / 1e6:7.0f} {ze.real:10.2f}{ze.imag:+10.2f}j "
                  f"{zw.real:10.2f}{zw.imag:+10.2f}j {e:7.2f}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
