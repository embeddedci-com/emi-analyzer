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

``--caps`` asks the capacitor question instead: 100 pF, 0.45 nH, 0.3 ohm (the library's 0402)
as one series element, as a plain C, and split into a plain C cell plus a series R-L element,
each with the port voltage's tail fitted as in verify_0402.py. ``FINE_UM`` adds one cell that
small on each axis, far from the loop, which shortens the timestep and nothing else.
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


#: ``--caps``: the library's 100 pF 0402. (name, R, L, C, how). "series" is one LEtype=1
#: element across all four cells; "plain" is openEMS's core lumped C in the bottom cell and
#: metal above; "split" is the same C cell and a series R-L element (no C) in the three above.
CAP_CASES = [
    ("C_100pF_plain", None, None, 100e-12, "plain"),
    ("RLC_100pF_series", 0.3, 0.45e-9, 100e-12, "series"),
    ("RLC_200pF_series", 0.3, 0.45e-9, 200e-12, "series"),
    ("RLC_100pF_split", 0.3, 0.45e-9, 100e-12, "split"),
    ("RLC_200pF_split", 0.3, 0.45e-9, 200e-12, "split"),
]
CAPS = "--caps" in sys.argv
if CAPS:
    FREQS = [float(f) for f in np.geomspace(750.3e6 / 3.2, 750.3e6 * 3.5, 25)]
    OUT = OUT.with_name(f"lumped_rlc_caps_fine{os.environ.get('FINE_UM', '0')}")
#: A comma-separated subset of case names to run; the short always runs.
ONLY = [n for n in os.environ.get("ONLY", "").split(",") if n]
#: One cell of this size on each axis, in micrometres, away from the loop: a shorter timestep.
FINE_UM = float(os.environ.get("FINE_UM", "0"))


def grid() -> tuple[list[float], list[float], list[float]]:
    step = 0.25
    if CAPS:
        # Smaller, so a tail of tens of nanoseconds at a short timestep stays minutes.
        x = list(np.round(np.arange(-7.0, 7.0 + step / 2, step), 6))
        y = list(np.round(np.arange(-6.0, 6.0 + step / 2, step), 6))
        z = list(np.round(np.arange(-4.0, 5.0 + step / 2, step), 6))
        if FINE_UM:
            for axis, at in ((x, 4.0), (y, 3.5), (z, 3.0)):
                axis.append(round(at + FINE_UM / 1000, 6))
                axis.sort()
        return x, y, z
    x = list(np.round(np.arange(-12.0, 12.0 + step / 2, step), 6))
    y = list(np.round(np.arange(-10.0, 10.0 + step / 2, step), 6))
    z = list(np.round(np.arange(-8.0, 9.0 + step / 2, step), 6))
    return x, y, z


#: ``--caps``: how long each run records, in seconds. A plain 100 pF drains through the port's
#: 50 ohm with 5 ns; eight time constants leave the tail well clear of the pulse.
TEND_S = float(os.environ.get("TEND_NS", "40")) * 1e-9


def record_steps(x, y, z) -> int:
    """Timesteps for ``TEND_S``, from the Courant limit of the smallest cells.

    openEMS's own timestep is at most this, so the record is at most ``TEND_S`` long, and a
    run whose energy never falls far enough still ends.
    """
    inv = sum(1 / (min(np.diff(a)) * 1e-3) ** 2 for a in (x, y, z))
    return int(TEND_S * 299_792_458 * math.sqrt(inv))


def element(name: str, r, l, c, le_type) -> list[csx.Property]:
    def box(z0: float, z1: float) -> csx.Box:
        return csx.Box(p1=(ELEM_X - HALF_W, -HALF_W, z0), p2=(ELEM_X + HALF_W, HALF_W, z1),
                       priority=csx.PRIORITY_PORT)

    if r is None and l is None and c is None:
        return [csx.Metal(name="elem_short", primitives=[box(0.0, H)])]
    cut = H / 4
    if le_type == "plain":
        # The same bottom cell as "split", metal above: a C spread over all four cells leaves
        # floating nodes between them, and that rings at 1.7 GHz on its own.
        return [csx.LumpedElement(name=f"elem_{name}", direction=2, resistance=None,
                                  capacitance=c, primitives=[box(0.0, cut)]),
                csx.Metal(name=f"elem_{name}_metal", primitives=[box(cut, H)])]
    if le_type == "split":
        return [
            csx.LumpedElement(name=f"elem_{name}_c", direction=2, resistance=None,
                              capacitance=c, primitives=[box(0.0, cut)]),
            csx.LumpedElement(name=f"elem_{name}_rl", direction=2, resistance=r,
                              inductance=l, le_type=csx.LE_SERIES, primitives=[box(cut, H)]),
        ]
    if le_type == "series":
        le_type = csx.LE_SERIES
    return [csx.LumpedElement(name=f"elem_{name}", direction=2, resistance=r, inductance=l,
                              capacitance=c, primitives=[box(0.0, H)], le_type=le_type)]


def build(name: str, r, l, c, le_type) -> csx.CSXDocument:
    x, y, z = grid()
    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=550e6, fc=550e6) if not CAPS
        else csx.Excitation(type=0, f0=1.3e9, fc=1.3e9),
        x_lines=x, y_lines=y, z_lines=z, f_max=1.1e9 if not CAPS else 2.6e9,
        end_criteria=1e-5 if not CAPS else 1e-9,
        max_timesteps=400_000 if not CAPS else record_steps(x, y, z),
    )
    plate = (-6.0, -4.0, 6.0, 4.0) if not CAPS else (-4.0, -3.0, 4.0, 3.0)
    doc.add(csx.Metal(name="plate", primitives=[
        csx.Box(p1=(plate[0], plate[1], 0.0), p2=(plate[2], plate[3], 0.0),
                priority=csx.PRIORITY_METAL)]))
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
    for prop in element(name, r, l, c, le_type):
        doc.add(prop)
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
    tails[name] = tail_time_constant(u, i)
    warnings = sorted({ln.strip() for ln in result.log_text.splitlines()
                       if re.search(r"warning|skipping", ln, re.I)})
    return z, warnings, result


#: What ``tail_time_constant`` measured on each run, for the report.
tails: dict[str, dict] = {}


def tail_time_constant(u, i, start_s: float = 5e-9, floor: float = 1e-4) -> dict:
    """The port voltage's decay after the pulse, fitted as one exponential.

    A charged capacitor drains through the port's 50 ohm, so its tail is exp(-t / tau) with
    tau = (50 + ESR) C and V/I = -50 (the port resistor's own law). The voltage is first
    averaged over 1 ns, which removes ringing in the GHz range without touching a decay of
    nanoseconds, then fitted from ``start_s`` until it falls to ``floor`` of its peak or
    changes sign. A tail that never falls fits a tau far longer than the record.
    """
    t, v = np.asarray(u.time_s), np.asarray(u.values)
    cur = np.asarray(i.values)[: len(v)]
    n = max(1, int(round(1e-9 / (t[1] - t[0]))))
    vs = np.convolve(v, np.ones(n) / n, mode="same")
    peak = float(np.max(np.abs(v)))
    k0 = int(np.searchsorted(t, start_s))
    if k0 >= len(t) - 1:
        return {"tau_s": None, "note": "the record ends before the tail starts"}
    k1 = k0
    while (k1 < len(vs) - n // 2 and abs(vs[k1]) > floor * peak
           and np.sign(vs[k1]) == np.sign(vs[k0])):
        k1 += 1
    if t[min(k1, len(t) - 1)] - t[k0] < 2e-9:
        return {"tau_s": None, "note": "under 2 ns of clean decay after the pulse"}
    tt, vv = t[k0:k1], vs[k0:k1]
    slope, _ = np.polyfit(tt, np.log(np.abs(vv)), 1)
    ratio = float(np.median(vv / np.convolve(cur, np.ones(n) / n, mode="same")[k0:k1]))
    return {"tau_s": float(-1 / slope) if slope < 0 else None, "v_over_i": ratio,
            "fit_s": [float(tt[0]), float(tt[-1])], "v_end": float(v[-1]), "v_peak": peak}


def expected(r, l, c, le_type, f: np.ndarray) -> np.ndarray:
    w = 2 * np.pi * f
    parts = []
    if r is not None:
        parts.append(np.full_like(w, r, dtype=complex))
    if l is not None:
        parts.append(1j * w * l)
    if c is not None:
        parts.append(1 / (1j * w * c))
    if le_type in (1, "series", "split"):
        return sum(parts)
    # Parallel: the only way 0.0.35 wires more than one value.
    return 1 / sum(1 / p for p in parts)


def main() -> int:
    cases = list(CASES) if not CAPS else list(CAP_CASES)
    if "--le-type" in sys.argv:
        cases += SERIES_CASES
    f = np.asarray(FREQS)
    z_short, warn_short, _ = solve("short", build("short", None, None, None, None))
    report = {"frequencies_hz": FREQS, "cases": {}}
    print("loop inductance from the shorted reference: "
          + ", ".join(f"{z.imag / (2 * math.pi * fr) * 1e9:.2f} nH" for z, fr in zip(z_short, f)))
    for name, r, l, c, le_type in cases:
        if name == "short" or (ONLY and name not in ONLY):
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
            "dt_s": res.dt_seconds, "tail": tails.get(name),
        }
        print(f"\n== {name}  (R={r} L={l} C={c} LEtype={le_type})  dt {res.dt_seconds:.3e} s, "
              f"{res.final_timestep:,} steps, {res.final_energy_db:.1f} dB")
        tail = tails[name]
        if tail.get("tau_s") and c:
            print(f"   tail: tau {tail['tau_s'] * 1e9:.2f} ns (50 ohm x C: {50 * c * 1e9:.2f}), "
                  f"V/I {tail['v_over_i']:.1f} ohm")
        elif c:
            print(f"   tail: {tail.get('note', 'no decay')}")
        for w in warns:
            print(f"   openEMS: {w}")
        print(f"   {'f MHz':>7} {'Z_el':>22} {'expected':>22} {'err dB':>7}")
        for fr, ze, zw, e in zip(f, z_el, want, err_db):
            print(f"   {fr / 1e6:7.0f} {ze.real:10.2f}{ze.imag:+10.2f}j "
                  f"{zw.real:10.2f}{zw.imag:+10.2f}j {e:7.2f}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ("report_caps.json" if CAPS else "report.json")).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
