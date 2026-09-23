"""M0 · cable 7 — is the Thevenin voltage at the connector independent of the cable?

Tier B (docs/implementation.md §5.2) computes an open-circuit voltage at a one-cell gap
between the board edge and a short cable root, hands the antenna to a method-of-moments
solver for ``Z_ant``, and composes

    I_cm(f) = V_oc(f) / Z_ant(f)

That composition is only legitimate if the board and the cable interact *through the gap
port alone*. Real boards and real cables also see each other through the air, and if that
coupling matters then ``V_oc`` moves when the cable is attached and the whole tier is built
on sand. This measures it.

Seven runs on one synthetic board, all on a byte-identical grid so that mesh error — which
M0 already showed is what shifts a radiating resonance — is common to every run and cancels
out of the comparison:

    voc_stub    driver on,  10 mm root,  gap 1 MOhm   -> V_oc with nothing attached
    voc_c150    driver on, 150 mm cable, gap 1 MOhm   -> V_oc with a cable attached
    voc_c300    driver on, 300 mm cable, gap 1 MOhm
    zant_c150   driver off, excitation AT the gap     -> Z_ant seen from the gap
    zant_c300
    truth_c150  driver on, gap filled with metal      -> the real I_cm, coupled
    truth_c300

The driver's lumped resistance stays in place in every run; only the excitation moves. That
is source-zeroing, which is what a Thevenin impedance is defined against — deleting the
driver instead would change the board arm.

    docker run --rm -v "$PWD/worker:/spike" -v <out>:/spike/spike_out -w /spike \\
        -e PYTHONPATH=/spike --entrypoint python3 ghcr.io/embeddedci-com/emi-worker:dev \\
        research/spike_m0_coupling.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from emi_worker.openems import csx, post, run
from emi_worker.openems.mesh import build_axis

OUT = Path("/spike/spike_out")

CELL = 2.5                 # the fine cell, and the wire is one cell across
COARSE = float(os.environ.get("COARSE_MM", "10.0"))

PLANE_X0, PLANE_X1 = -60.0, -CELL      # ground plane, board edge at -2.5
PLANE_Y = 20.0
TRACE_Z = 2 * CELL                     # 5 mm above the plane
TRACE_HW = CELL                        # 5 mm wide
IC_X0, IC_X1 = -55.0, -55.0 + CELL     # the driver's vertical feed
LD_X0, LD_X1 = -2 * CELL, -CELL        # its 50 ohm load, one cell in from the edge
WIRE_X0 = CELL                         # the cable root starts here; the gap is 2 cells
WIRE_HW = CELL

CABLES_MM = [10.0, 150.0, 300.0]
AIR_MM = 250.0

F0, FC = 400e6, 380e6      # 20-780 MHz: every evaluated frequency is well inside the band
FREQS = np.geomspace(50e6, 600e6, 45)

END_CRITERIA = float(os.environ.get("END_CRITERIA", "1e-6"))
MAX_STEPS = int(os.environ.get("MAX_STEPS", "120000"))


def axes() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One grid for every run: the union of every structure's required lines."""
    far = WIRE_X0 + max(CABLES_MM)
    x = build_axis(
        [-AIR_MM + PLANE_X0, PLANE_X0, IC_X0, IC_X1, LD_X0, LD_X1, PLANE_X1, 0.0, WIRE_X0]
        + [WIRE_X0 + c for c in CABLES_MM]
        + [far + AIR_MM],
        CELL, COARSE,
    )
    y = build_axis(
        [-AIR_MM - PLANE_Y, -PLANE_Y, -TRACE_HW, 0.0, TRACE_HW, PLANE_Y, PLANE_Y + AIR_MM],
        CELL, COARSE,
    )
    z = build_axis(
        [-AIR_MM, -WIRE_HW, 0.0, WIRE_HW, TRACE_Z, TRACE_Z + AIR_MM], CELL, COARSE,
    )
    return x, y, z


GAP_R = {"open": 1e6, "port": 50.0, "short": 0.1}


def model(cable_mm: float, gap: str, drive: str) -> csx.CSXDocument:
    """``gap`` is 'open', 'port', 'short' or 'metal'; ``drive`` is 'ic' or 'gap'.

    'metal' fills the gap with PEC, which is what "the cable is attached" means physically
    but is *not* the same geometry the other two runs solve. 'short' keeps the lumped
    element and sets it to 0.1 ohm — negligible against a Z_ant of 80 ohm and up — so the
    three runs differ only in the value of one element. The pair separates a coupling error
    from a port-modelling artefact.
    """
    x, y, z = axes()
    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=F0, fc=FC),
        x_lines=x.tolist(), y_lines=y.tolist(), z_lines=z.tolist(),
        f_max=F0 + FC,
        end_criteria=END_CRITERIA,
        max_timesteps=MAX_STEPS,
    )

    prims = [
        # Ground plane: a zero-thickness sheet, the way ingest builds copper layers.
        csx.Box(p1=(PLANE_X0, -PLANE_Y, 0.0), p2=(PLANE_X1, PLANE_Y, 0.0),
                priority=csx.PRIORITY_METAL),
        # The trace above it, from the driver to the load near the edge.
        csx.Box(p1=(IC_X1, -TRACE_HW, TRACE_Z), p2=(LD_X0, TRACE_HW, TRACE_Z),
                priority=csx.PRIORITY_METAL),
        # The cable, a square column one cell across along the exit normal.
        csx.Box(p1=(WIRE_X0, -WIRE_HW, -WIRE_HW),
                p2=(WIRE_X0 + cable_mm, WIRE_HW, WIRE_HW),
                priority=csx.PRIORITY_METAL),
    ]
    if gap == "metal":
        prims.append(csx.Box(p1=(PLANE_X1, -WIRE_HW, -WIRE_HW), p2=(WIRE_X0, WIRE_HW, WIRE_HW),
                             priority=csx.PRIORITY_METAL))
    doc.add(csx.Metal(name="copper", primitives=prims))

    # The driver and its load, vertical columns between trace and plane.
    ic = csx.Box(p1=(IC_X0, -TRACE_HW, 0.0), p2=(IC_X1, TRACE_HW, TRACE_Z),
                 priority=csx.PRIORITY_PORT)
    doc.add(csx.LumpedElement(name="ic_r", direction=2, resistance=50.0, caps=True,
                              primitives=[ic]))
    ld = csx.Box(p1=(LD_X0, -TRACE_HW, 0.0), p2=(LD_X1, TRACE_HW, TRACE_Z),
                 priority=csx.PRIORITY_PORT)
    doc.add(csx.LumpedElement(name="load_r", direction=2, resistance=50.0, caps=True,
                              primitives=[ld]))

    gap_box = csx.Box(p1=(PLANE_X1, -WIRE_HW, -WIRE_HW), p2=(WIRE_X0, WIRE_HW, WIRE_HW),
                      priority=csx.PRIORITY_PORT)
    if gap != "metal":
        doc.add(csx.LumpedElement(
            name="gap_r", direction=0, caps=True,
            resistance=GAP_R[gap], primitives=[gap_box]))

    if drive == "ic":
        doc.add(csx.ExcitationProperty(name="exc", number=0, excite=(0.0, 0.0, 1.0),
                                       primitives=[ic]))
    else:
        doc.add(csx.ExcitationProperty(name="exc", number=0, excite=(1.0, 0.0, 0.0),
                                       primitives=[gap_box]))

    # Gap probes. The voltage integrates E across the gap; the current takes an H loop at
    # the gap centre, which is a grid line, the same placement the dipole spike validated.
    doc.add(csx.ProbeBox(name="gap_ut", type=0, norm_dir=0, weight=-1.0, primitives=[
        csx.Box(p1=(PLANE_X1, 0.0, 0.0), p2=(WIRE_X0, 0.0, 0.0)),
    ]))
    doc.add(csx.ProbeBox(name="gap_it", type=1, norm_dir=0, weight=1.0, primitives=[
        csx.Box(p1=(0.0, -1.5 * WIRE_HW, -1.5 * WIRE_HW),
                p2=(0.0, 1.5 * WIRE_HW, 1.5 * WIRE_HW)),
    ]))
    # The driver's own port, so every run can be checked against the same source.
    doc.add(csx.ProbeBox(name="ic_ut", type=0, norm_dir=2, weight=-1.0, primitives=[
        csx.Box(p1=(IC_X0, 0.0, 0.0), p2=(IC_X0, 0.0, TRACE_Z)),
    ]))
    return doc


def solve(tag: str, cable_mm: float, gap: str, drive: str) -> dict:
    doc = model(cable_mm, gap, drive)
    problems = doc.validate()
    if problems:
        raise SystemExit(f"{tag}: " + "; ".join(problems))
    wd = OUT / f"coupling_{tag}"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "model.xml").write_text(doc.to_string())
    r = run.run_openems(str(wd / "model.xml"), str(wd),
                        threads=int(os.environ.get("THREADS", "4")))
    u = post._dft(post.read_probe(str(wd / "gap_ut")), FREQS)
    i = post._dft(post.read_probe(str(wd / "gap_it")), FREQS)
    print(f"  {tag}: {r.final_timestep:,} steps, {r.elapsed_s:.0f}s, "
          f"energy {r.final_energy_db:.1f} dB", flush=True)
    for w in r.warnings:
        print(f"    warning: {w}")
    return {"tag": tag, "u": u, "i": i, "steps": r.final_timestep,
            "energy_db": r.final_energy_db, "elapsed_s": r.elapsed_s}


def db(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.abs(a) / np.abs(b))


def main() -> None:
    x, y, z = axes()
    cells = (len(x) - 1) * (len(y) - 1) * (len(z) - 1)
    print(f"grid [{len(x)}, {len(y)}, {len(z)}], {cells:,} cells, fine {CELL} mm, "
          f"coarse {COARSE:g} mm", flush=True)

    plan = [
        ("voc_stub", 10.0, "open", "ic"),
        ("voc_c150", 150.0, "open", "ic"),
        ("voc_c300", 300.0, "open", "ic"),
        ("zant_c150", 150.0, "port", "gap"),
        ("zant_c300", 300.0, "port", "gap"),
        ("truth_c150", 150.0, "metal", "ic"),
        ("truth_c300", 300.0, "metal", "ic"),
        ("short_c150", 150.0, "short", "ic"),
        ("short_c300", 300.0, "short", "ic"),
    ]
    only = os.environ.get("ONLY")
    if only:
        keep = set(only.split(","))
        plan = [p for p in plan if p[0] in keep]
    res = {p[0]: solve(*p) for p in plan}

    out: dict = {"frequencies_hz": FREQS.tolist(), "cells": cells,
                 "runs": {k: {"steps": v["steps"], "energy_db": v["energy_db"],
                              "elapsed_s": v["elapsed_s"]} for k, v in res.items()}}

    if {"voc_stub", "voc_c150", "voc_c300"} <= res.keys():
        print("\nDoes V_oc move when a cable is attached?  (dB re the 10 mm root)")
        print("\n  f (MHz)   |V_oc| stub      150 mm     300 mm")
        d150 = db(res["voc_c150"]["u"], res["voc_stub"]["u"])
        d300 = db(res["voc_c300"]["u"], res["voc_stub"]["u"])
        for k in range(0, len(FREQS), 4):
            print(f"  {FREQS[k]/1e6:7.0f}  {20*np.log10(abs(res['voc_stub']['u'][k])):10.1f} dB"
                  f"  {d150[k]:+9.2f}  {d300[k]:+9.2f}")
        print(f"\n  worst |delta|: 150 mm {np.abs(d150).max():.2f} dB, "
              f"300 mm {np.abs(d300).max():.2f} dB")
        out["voc_shift_db"] = {"c150": d150.tolist(), "c300": d300.tolist()}

    for L, truth in [(L, t) for L in ("150", "300") for t in ("truth", "short")]:
        need = {f"voc_c{L}", f"zant_c{L}", f"{truth}_c{L}"}
        if not need <= res.keys():
            continue
        z_ant = res[f"zant_c{L}"]["u"] / res[f"zant_c{L}"]["i"]
        pred = res[f"voc_c{L}"]["u"] / z_ant
        meas = res[f"{truth}_c{L}"]["i"]
        err = db(pred, meas)
        print(f"\nTier B composition against the coupled run, {L} mm cable, "
              f"gap {'PEC' if truth == 'truth' else '0.1 ohm'}")
        print("\n  f (MHz)   |Z_ant|    ang     I pred dBuA   I meas dBuA    error")
        for k in range(0, len(FREQS), 4):
            print(f"  {FREQS[k]/1e6:7.0f}  {abs(z_ant[k]):8.1f} {np.angle(z_ant[k], deg=True):+7.0f}"
                  f"  {20*np.log10(abs(pred[k])*1e6):12.1f} {20*np.log10(abs(meas[k])*1e6):13.1f}"
                  f"  {err[k]:+8.2f}")
        print(f"\n  median |error| {np.median(np.abs(err)):.2f} dB, "
              f"90th pct {np.percentile(np.abs(err), 90):.2f} dB, "
              f"worst {np.abs(err).max():.2f} dB")
        out[f"compose_{truth}_c{L}"] = {
            "z_ant_real": z_ant.real.tolist(), "z_ant_imag": z_ant.imag.tolist(),
            "i_pred_abs": np.abs(pred).tolist(), "i_meas_abs": np.abs(meas).tolist(),
            "error_db": err.tolist(),
        }

    (OUT / "m0_coupling.json").write_text(json.dumps(out, indent=2))
    print(f"\nwrote {OUT}/m0_coupling.json")


if __name__ == "__main__":
    main()
