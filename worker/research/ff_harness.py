"""Shared harness for the far-field verification: small antennas through the production path.

Everything that decides the far field's level is the production code: the lumped port as
``model.build_model`` draws it, ``model.excitation_band`` and ``required_timesteps`` for the
source and the record, ``nf2ff.plan_faces``/``add_dumps`` for the box, ``scan`` (in the
scripts that use this) for the field, ``nf2ff.write_job``/``run_job`` for the directivity, and
``post.source_spectrum`` for the division by the source. Only the copper is different: a wire
instead of a board, so the answer is known.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from emi_worker.openems import csx, nf2ff, post, run
from emi_worker.openems import model as emmodel
from emi_worker.openems.mesh import build_axis, max_cell_for_frequency

C0 = 299_792_458.0
ETA0 = 376.730313668

THREADS = int(os.environ.get("THREADS", "3"))


@dataclass
class Wire:
    """A wire antenna in model mm: a list of PEC boxes and one gap port."""

    boxes: list[tuple[tuple[float, float, float], tuple[float, float, float]]]
    #: Port box corners, and the axis the gap runs along.
    port: tuple[tuple[float, float, float], tuple[float, float, float]]
    axis: int
    #: Lines each axis must carry (copper and port edges).
    lines: tuple[list[float], list[float], list[float]]
    #: Copper extent (x0, y0, x1, y1, z0, z1), what the box clearance is measured from.
    extent: tuple[float, float, float, float, float, float]


def dipole(length_mm: float, width_mm: float, axis: int = 2) -> Wire:
    """A centre-fed dipole along ``axis``, square section ``width_mm``, one-width gap."""
    h, half, g = width_mm / 2, length_mm / 2, width_mm / 2
    lo = [-h, -h, -h]
    hi = [h, h, h]

    def span(a0, a1):
        p1, p2 = lo[:], hi[:]
        p1[axis], p2[axis] = a0, a1
        return tuple(p1), tuple(p2)

    boxes = [span(-half, -g), span(g, half)]
    port = span(-g, g)
    lines = [[-h, 0.0, h], [-h, 0.0, h], [-h, 0.0, h]]
    lines[axis] = [-half, -g, 0.0, g, half]
    ext = [-h, -h, -h, h, h, h]
    ext_lo, ext_hi = [-h, -h, -h], [h, h, h]
    ext_lo[axis], ext_hi[axis] = -half, half
    ext = (ext_lo[0], ext_lo[1], ext_hi[0], ext_hi[1], ext_lo[2], ext_hi[2])
    return Wire(boxes=boxes, port=port, axis=axis, lines=tuple(lines), extent=ext)


def loop(side_mm: float, width_mm: float) -> Wire:
    """A square loop in the xy plane, side ``side_mm`` centre to centre, fed on the x = +a side."""
    a, h = side_mm / 2, width_mm / 2
    g = width_mm / 2
    boxes = [
        ((-a - h, -a - h, -h), (a + h, -a + h, h)),     # y = -a
        ((-a - h, a - h, -h), (a + h, a + h, h)),       # y = +a
        ((-a - h, -a - h, -h), (-a + h, a + h, h)),     # x = -a
        ((a - h, -a - h, -h), (a + h, -g, h)),          # x = +a, below the gap
        ((a - h, g, -h), (a + h, a + h, h)),            # x = +a, above the gap
    ]
    port = ((a - h, -g, -h), (a + h, g, h))
    lines = ([-a - h, -a, -a + h, a - h, a, a + h],
             [-a - h, -a, -a + h, -g, 0.0, g, a - h, a, a + h],
             [-h, 0.0, h])
    return Wire(boxes=boxes, port=port, axis=1, lines=lines,
                extent=(-a - h, -a - h, a + h, a + h, -h, h))


@dataclass
class Setup:
    wire: Wire
    f_lo: float = 30e6
    f_hi: float = 1e9
    fine_mm: float = 0.5
    #: Largest cell; None = production lambda/20 at f_hi.
    max_cell_mm: float | None = None
    clearance_mm: float | None = None      # None = production far_field_clearance_mm(f_hi)
    #: Face sample spacing; None = production ``nf2ff.face_resolution_mm(f_hi)``.
    resolution_mm: float | None = None
    end_criteria: float = 1e-4             # production default
    frequencies: list[float] = field(default_factory=list)
    resistance: float = 50.0


def build(setup: Setup, wd: Path) -> dict:
    """Write model.xml for ``setup`` into ``wd`` and return the metadata the rest needs."""
    w = setup.wire
    f_max = setup.f_hi
    coarse = setup.max_cell_mm or max_cell_for_frequency(f_max, 1.0)
    clearance = setup.clearance_mm or emmodel.far_field_clearance_mm(f_max)
    pad = emmodel.far_field_pad_mm(clearance, coarse)
    x0, y0, x1, y1, z0, z1 = w.extent
    ax = []
    for i, (lo, hi) in enumerate(((x0, x1), (y0, y1), (z0, z1))):
        req = sorted(set(w.lines[i] + [lo - pad, hi + pad]))
        ax.append(build_axis(req, setup.fine_mm, coarse))
    mesh = SimpleNamespace(x=ax[0], y=ax[1], z=ax[2])

    freqs = setup.frequencies or emmodel.far_field_grid(setup.f_lo, setup.f_hi)
    f0, fc, _ = emmodel.excitation_band(min(freqs), f_max)
    dmin = min(float(np.diff(a).min()) for a in ax) * 1e-3
    dt = dmin / (C0 * np.sqrt(3))
    needed, _ = emmodel.required_timesteps(dt, fc, min(freqs))

    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=f0, fc=fc),
        x_lines=ax[0].tolist(), y_lines=ax[1].tolist(), z_lines=ax[2].tolist(),
        f_max=f_max, end_criteria=setup.end_criteria, max_timesteps=needed,
    )
    doc.add(csx.Metal(name="wire", primitives=[
        csx.Box(p1=p1, p2=p2, priority=csx.PRIORITY_METAL) for p1, p2 in w.boxes]))

    # The port exactly as build_model draws one: R and a soft source in the gap, a voltage
    # probe along the gap's centre line (weight -1, source toward "bottom"), and a current
    # probe drawn 1.5x the port around it.
    p1, p2 = w.port
    doc.add(csx.LumpedElement(name="p1_res", direction=w.axis, resistance=setup.resistance,
                              caps=True, primitives=[csx.Box(p1=p1, p2=p2,
                                                             priority=csx.PRIORITY_PORT)]))
    exc = [0.0, 0.0, 0.0]
    exc[w.axis] = -1.0
    doc.add(csx.ExcitationProperty(name="p1_exc", number=0, excite=tuple(exc),
                                   primitives=[csx.Box(p1=p1, p2=p2,
                                                       priority=csx.PRIORITY_PORT)]))
    c = [(a + b) / 2 for a, b in zip(p1, p2)]
    u1, u2 = c[:], c[:]
    u1[w.axis], u2[w.axis] = p1[w.axis], p2[w.axis]
    doc.add(csx.ProbeBox(name="p1_ut", type=0, norm_dir=w.axis, weight=-1.0,
                         primitives=[csx.Box(p1=tuple(u1), p2=tuple(u2))]))
    i1 = [c[k] - 1.5 * (p2[k] - p1[k]) / 2 for k in range(3)]
    i2 = [c[k] + 1.5 * (p2[k] - p1[k]) / 2 for k in range(3)]
    i1[w.axis] = i2[w.axis] = c[w.axis]
    doc.add(csx.ProbeBox(name="p1_it", type=1, norm_dir=w.axis, weight=1.0,
                         primitives=[csx.Box(p1=tuple(i1), p2=tuple(i2))]))

    resolution = setup.resolution_mm or nf2ff.face_resolution_mm(f_max)
    faces = nf2ff.plan_faces(mesh, w.extent, clearance)
    nf2ff.add_dumps(doc, faces, freqs, resolution_mm=resolution)
    problems = doc.validate()
    if problems:
        raise SystemExit("; ".join(problems))
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "model.xml").write_text(doc.to_string())
    meta = {
        "frequencies_hz": freqs, "f0": f0, "fc": fc, "clearance_mm": clearance,
        "faces_mm": [faces.x0, faces.y0, faces.z0, faces.x1, faces.y1, faces.z1],
        "centre_mm": list(faces.centre()), "cells": doc.cell_count(),
        "lines": [len(a) for a in ax], "dt_s": dt, "max_timesteps": needed,
        "face_resolution_mm": resolution,
    }
    (wd / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta


def solve(wd: Path, *, force: bool = False) -> dict:
    """Run openEMS in ``wd`` unless its dumps are already there."""
    done = wd / "run.json"
    if done.exists() and not force:
        return json.loads(done.read_text())
    t = time.monotonic()
    r = run.run_openems(str(wd / "model.xml"), str(wd), threads=THREADS)
    info = {"timesteps": r.final_timestep, "energy_db": r.final_energy_db,
            "converged": r.converged, "elapsed_s": round(time.monotonic() - t, 1),
            "warnings": r.warnings}
    done.write_text(json.dumps(info, indent=1))
    return info


def far_field(wd: Path, meta: dict, *, mirror_z_m: float | None,
              theta_deg=nf2ff.THETA_DEG, phi_deg=nf2ff.PHI_DEG, tag: str = "ff") -> dict:
    """The production transform and read-back, plus the complex fields for anything else."""
    import h5py

    freqs = meta["frequencies_hz"]
    out = wd / f"{tag}.h5"
    out.unlink(missing_ok=True)
    job = nf2ff.write_job(str(wd), freqs, str(out), centre_mm=tuple(meta["centre_mm"]),
                          radius_m=3.0, mirror_z_m=mirror_z_m,
                          lower_face_z_m=meta["faces_mm"][2] / 1000.0,
                          theta_deg=theta_deg, phi_deg=phi_deg)
    os.replace(job, wd / f"{tag}_job.xml")
    nf2ff.run_job(str(wd / f"{tag}_job.xml"), str(out))
    res = nf2ff.read_field(str(out), freqs, theta_deg=theta_deg, phi_deg=phi_deg)
    with h5py.File(out, "r") as f:
        et = np.stack([f[f"/nf2ff/E_theta/FD/f{k}_real"][:] + 1j * f[f"/nf2ff/E_theta/FD/f{k}_imag"][:]
                       for k in range(len(freqs))])
        ep = np.stack([f[f"/nf2ff/E_phi/FD/f{k}_real"][:] + 1j * f[f"/nf2ff/E_phi/FD/f{k}_imag"][:]
                       for k in range(len(freqs))])
    res["e_theta"], res["e_phi"] = et, ep   # (freq, phi, theta), V/m at 3 m
    return res


def source(wd: Path, meta: dict, resistance: float = 50.0) -> dict:
    s = post.source_spectrum(str(wd), "p1", resistance, meta["frequencies_hz"])
    u = post.read_probe(str(wd / "p1_ut"))
    i = post.read_probe(str(wd / "p1_it"))
    f = np.asarray(meta["frequencies_hz"])
    s["i_port"] = post._dft(i, f) * post.OPENEMS_FD_SCALE
    s["v_port"] = post._dft(u, f) * post.OPENEMS_FD_SCALE
    return s


def directivity(res: dict, theta_deg, phi_deg) -> np.ndarray:
    """Peak directivity in dBi per frequency, from a full-sphere transform."""
    th = np.deg2rad(np.asarray(theta_deg))
    ph = np.deg2rad(np.asarray(phi_deg))
    u = np.abs(res["e_theta"]) ** 2 + np.abs(res["e_phi"]) ** 2     # (f, phi, theta)
    w_th = np.sin(th)
    d_th = np.gradient(th)
    d_ph = 2 * np.pi / len(ph)
    total = (u * (w_th * d_th)[None, None, :]).sum(axis=(1, 2)) * d_ph
    return 10 * np.log10(4 * np.pi * u.reshape(len(u), -1).max(axis=1) / total)


def slope_db_per_decade(f, y) -> float:
    f, y = np.asarray(f, float), np.asarray(y, float)
    ok = y > 0
    return float(np.polyfit(np.log10(f[ok]), 20 * np.log10(y[ok]), 1)[0])
