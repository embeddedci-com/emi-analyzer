"""M0 · cable 4, step 1 — do openEMS and nec2c agree on the same wire?

Tier B hands the antenna to a method-of-moments solver while Tier C keeps it inside the FDTD
grid. Before comparing them on a board, they have to agree on a structure that is only a
wire. This builds a centre-fed dipole directly with the CSX writer, sweeps its input
impedance, and reports the resonance.

A wire in a rectilinear FDTD grid is a square column one cell across, so its effective radius
is set by the mesh, not by a radius we choose. Finding which nec2c radius reproduces the FDTD
resonance is therefore part of the answer, and it is the number a Tier C comparison needs.

    docker run --rm -v "$PWD/worker:/spike" -v <out>:/spike/spike_out -w /spike \\
        -e PYTHONPATH=/spike --entrypoint python3 embeddedci/emi-worker:dev \\
        scripts/spike_m0_wire_fdtd.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from emi_worker.openems import csx, post, run
from emi_worker.openems.mesh import build_axis

OUT = Path("/spike/spike_out")

LENGTH_MM = 500.0          # a half-wave dipole near 285 MHz
CELL_MM = float(os.environ.get("CELL_MM", "5.0"))  # the wire is one cell across, which sets its effective radius
SPAN_XY_MM = float(os.environ.get("SPAN_XY_MM", "250.0"))
SPAN_Z_MM = float(os.environ.get("SPAN_Z_MM", "400.0"))
MAX_RES_MM = float(os.environ.get("MAX_RES_MM", "50.0"))  # the coarse cells away from the wire
F0, FC = 350e6, 320e6      # band 30-670 MHz, clear of DC, with margin at both ends
FREQS = np.linspace(100e6, 600e6, 51)


def model() -> tuple[csx.CSXDocument, dict]:
    half = LENGTH_MM / 2.0
    h = CELL_MM / 2.0       # the wire spans one cell in x and y
    gap = CELL_MM           # the feed gap is one cell tall

    x = build_axis([-SPAN_XY_MM, -h, 0.0, h, SPAN_XY_MM], CELL_MM, MAX_RES_MM)
    y = build_axis([-SPAN_XY_MM, -h, 0.0, h, SPAN_XY_MM], CELL_MM, MAX_RES_MM)
    z = build_axis(
        [-SPAN_Z_MM, -half, -gap / 2, 0.0, gap / 2, half, SPAN_Z_MM], CELL_MM, MAX_RES_MM
    )

    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=F0, fc=FC),
        x_lines=x.tolist(), y_lines=y.tolist(), z_lines=z.tolist(),
        f_max=F0 + FC,
        end_criteria=1e-6,
        max_timesteps=60000,
    )

    # Two arms, with a one-cell gap between them for the feed.
    doc.add(csx.Metal(name="wire", primitives=[
        csx.Box(p1=(-h, -h, -half), p2=(h, h, -gap / 2), priority=csx.PRIORITY_METAL),
        csx.Box(p1=(-h, -h, gap / 2), p2=(h, h, half), priority=csx.PRIORITY_METAL),
    ]))

    feed = csx.Box(p1=(-h, -h, -gap / 2), p2=(h, h, gap / 2), priority=csx.PRIORITY_PORT)
    doc.add(csx.LumpedElement(name="feed_res", direction=2, resistance=50.0,
                              caps=True, primitives=[feed]))
    doc.add(csx.ExcitationProperty(name="feed_exc", number=0, excite=(0.0, 0.0, 1.0),
                                   primitives=[feed]))
    doc.add(csx.ProbeBox(name="feed_ut", type=0, norm_dir=2, weight=-1.0, primitives=[
        csx.Box(p1=(0.0, 0.0, -gap / 2), p2=(0.0, 0.0, gap / 2)),
    ]))
    doc.add(csx.ProbeBox(name="feed_it", type=1, norm_dir=2, weight=1.0, primitives=[
        csx.Box(p1=(-h * 1.5, -h * 1.5, 0.0), p2=(h * 1.5, h * 1.5, 0.0)),
    ]))

    return doc, {"cells": doc.cell_count(),
                 "lines": [len(x), len(y), len(z)],
                 "min_cell_mm": float(min(np.diff(x).min(), np.diff(y).min(), np.diff(z).min()))}


def impedance(workdir: Path) -> np.ndarray:
    u = post.read_probe(str(workdir / "feed_ut"))
    i = post.read_probe(str(workdir / "feed_it"))
    return post._dft(u, FREQS) / post._dft(i, FREQS)


def resonance(freqs: np.ndarray, z: np.ndarray) -> tuple[float, float]:
    """First frequency where the reactance crosses zero going positive, and R there."""
    x = z.imag
    for k in range(len(freqs) - 1):
        if x[k] < 0 <= x[k + 1]:
            t = -x[k] / (x[k + 1] - x[k])
            return (float(freqs[k] + t * (freqs[k + 1] - freqs[k])),
                    float(z.real[k] + t * (z.real[k + 1] - z.real[k])))
    return float("nan"), float("nan")


def main() -> None:
    doc, info = model()
    problems = doc.validate()
    if problems:
        raise SystemExit("; ".join(problems))
    print(f"grid {info['lines']}, {info['cells']:,} cells, smallest cell "
          f"{info['min_cell_mm']:.2f} mm, coarse {MAX_RES_MM:.0f} mm, "
          f"domain {SPAN_XY_MM:.0f}/{SPAN_Z_MM:.0f} mm", flush=True)

    tag = os.environ.get("TAG", f"{CELL_MM:g}mm")
    wd = OUT / f"dipole_fdtd_{tag}"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "model.xml").write_text(doc.to_string())
    r = run.run_openems(str(wd / "model.xml"), str(wd),
                        threads=int(os.environ.get("THREADS", "4")))
    print(f"{r.final_timestep:,} of {r.max_timesteps:,} steps, {r.elapsed_s:.0f}s, "
          f"energy {r.final_energy_db:.1f} dB", flush=True)
    for w in r.warnings:
        print(f"  warning: {w}")

    z = impedance(wd)
    f_res, r_res = resonance(FREQS, z)

    print("\n  f (MHz)      R        X")
    for k in range(0, len(FREQS), 5):
        print(f"  {FREQS[k]/1e6:7.0f}  {z.real[k]:8.1f} {z.imag[k]:+9.1f}")
    print(f"\nFDTD resonance: {f_res/1e6:.1f} MHz, R = {r_res:.1f} ohm")
    print(f"a half-wave dipole of {LENGTH_MM:.0f} mm resonates near "
          f"{0.475 * 299.792458 / (LENGTH_MM / 1000) :.0f} MHz for a thin wire")

    (OUT / f"m0_dipole_fdtd_{tag}.json").write_text(json.dumps({
        "info": info,
        "frequencies_hz": FREQS.tolist(),
        "z_real": z.real.tolist(),
        "z_imag": z.imag.tolist(),
        "resonance_hz": f_res,
        "r_at_resonance": r_res,
        "steps": r.final_timestep,
        "energy_db": r.final_energy_db,
    }, indent=2))
    print(f"wrote {OUT}/m0_dipole_fdtd_{tag}.json")


if __name__ == "__main__":
    main()
