"""Phase 2 · Does the production model get a microstrip's characteristic impedance right?

A 50 ohm microstrip on FR-4, built as a KiCad board and put through the production path end to
end: the KiCad parser, ``build_model`` (mesher, zero-thickness copper, stackup) and its lumped
ports, run by ``run_openems``, transformed by ``post._dft``. Nothing here is a hand-made grid.

Two production ports, one at each end of the line, both 50 ohm, the first excited. One solve
gives V and I at both ports, and a symmetric reciprocal two-port has only two unknowns, so

    V1 = Z11 I1 + Z12 I2
    V2 = Z12 I1 + Z11 I2

is solved for Z11 and Z12 at each frequency. For a uniform line of length l,

    Z11 = Z0 coth(gamma l),  Z12 = Z0 / sinh(gamma l)  =>  Z0 = sqrt(Z11^2 - Z12^2),
    cosh(gamma l) = Z11 / Z12  =>  eps_eff = (beta / k0)^2.

The ports' own vertical current path is a small series inductance at each end that this does
not remove; with a 0.2 mm dielectric it is about a tenth of a nanohenry, and the check is made
where the line is long enough (a quarter wavelength or more) for it to matter least.

The reference is Hammerstad and Jensen (1980) for zero-thickness strip, which is what the model
draws, and IPC-2141 as a sanity bound. Pass criterion: within 5 % of Hammerstad-Jensen.

    docker run --rm --cpus 3 --memory 6g -v "$PWD/worker:/spike" \\
        -v "$PWD/spike_out:/spike/spike_out" -w /spike -e PYTHONPATH=/spike \\
        --entrypoint python3 emi-worker:phase1 research/verify_microstrip.py

``PRESETS`` (comma separated, default "coarse,normal,fine") picks the mesh presets to run.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emi_worker.kicad import parse, parse_board  # noqa: E402
from emi_worker.kicad.normalize import _board_extent  # noqa: E402
from emi_worker.openems import post, run  # noqa: E402
from emi_worker.openems.model import Port, SolveParams, build_model  # noqa: E402

OUT = Path(os.environ.get("OUT", "/spike/spike_out")) / "microstrip"
THREADS = int(os.environ.get("THREADS", "3"))
PRESETS = {"coarse": (150, 150, 100), "normal": (75, 75, 50), "fine": (50, 50, 25)}

ETA0 = 376.730313668
H_MM = 0.2
EPS_R = 4.4
LINE_MM = (5.0, 25.0)
Y_MM = 10.0
FREQS = [0.5e9, 1e9, 1.5e9, 2e9, 2.5e9, 3e9]


def hammerstad_jensen(w: float, h: float, er: float) -> tuple[float, float]:
    """Z0 and eps_eff of a zero-thickness microstrip (Hammerstad and Jensen, 1980)."""
    u = w / h
    f = 6.0 + (2 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    z01 = ETA0 / (2 * math.pi) * math.log(f / u + math.sqrt(1.0 + 4.0 / u ** 2))
    a = (1.0 + math.log((u ** 4 + (u / 52.0) ** 2) / (u ** 4 + 0.432)) / 49.0
         + math.log(1.0 + (u / 18.1) ** 3) / 18.7)
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1.0 + 10.0 / u) ** (-a * b)
    return z01 / math.sqrt(eeff), eeff


def ipc2141(w: float, h: float, er: float, t: float = 0.0) -> float:
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))


def width_for(z_target: float, h: float, er: float) -> float:
    lo, hi = 0.05 * h, 10 * h
    for _ in range(100):
        mid = (lo + hi) / 2
        if hammerstad_jensen(mid, h, er)[0] > z_target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def board_text(w: float) -> str:
    return f"""(kicad_pcb
  (version 20241229)
  (generator "emi-analyzer-microstrip-check")
  (general (thickness {H_MM + 0.07}))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness {H_MM}) (material "FR4")
             (epsilon_r {EPS_R}) (loss_tangent 0))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (segment (start {LINE_MM[0]} {Y_MM}) (end {LINE_MM[1]} {Y_MM}) (width {w:.4f}) (layer "F.Cu") (net 2))
  (zone (net 1) (net_name "GND") (layer "B.Cu")
    (hatch edge 0.5) (min_thickness 0.25)
    (polygon (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)))
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)))
  )
)
"""


def extract(workdir: Path, freqs: np.ndarray) -> dict:
    def spectra(name: str):
        u = post.read_probe(str(workdir / f"{name}_ut"))
        i = post.read_probe(str(workdir / f"{name}_it"))
        return post._dft(u, freqs), post._dft(i, freqs)

    v1, i1 = spectra("p1")
    v2, i2 = spectra("p2")
    det = i1 ** 2 - i2 ** 2
    z11 = (v1 * i1 - v2 * i2) / det
    z12 = (v2 * i1 - v1 * i2) / det
    z0 = np.sqrt(z11 ** 2 - z12 ** 2)
    z0 = np.where(z0.real < 0, -z0, z0)
    length_m = (LINE_MM[1] - LINE_MM[0]) / 1000.0
    gl = np.arccosh(z11 / z12)
    beta = np.abs(gl.imag) / length_m
    # arccosh returns the principal value; the line is under a wavelength long at 3 GHz, so
    # beta*l stays below pi and the principal branch is the right one.
    k0 = 2 * np.pi * freqs / 299_792_458.0
    return {"z0": z0, "eps_eff": (beta / k0) ** 2, "z11": z11, "z12": z12}


def main() -> int:
    w = width_for(50.0, H_MM, EPS_R)
    z_hj, e_hj = hammerstad_jensen(w, H_MM, EPS_R)
    z_ipc = ipc2141(w, H_MM, EPS_R)
    print(f"strip {w * 1000:.1f} um on {H_MM * 1000:.0f} um of eps_r {EPS_R}: "
          f"Hammerstad-Jensen {z_hj:.2f} ohm, eps_eff {e_hj:.3f}; IPC-2141 {z_ipc:.2f} ohm")
    board = parse_board(parse(board_text(w)))
    transform = _board_extent(board)
    freqs = np.asarray(FREQS)
    report = {"width_mm": w, "h_mm": H_MM, "eps_r": EPS_R, "z0_hammerstad_jensen": z_hj,
              "eps_eff_hammerstad_jensen": e_hj, "z0_ipc2141": z_ipc,
              "frequencies_hz": FREQS, "presets": {}}
    wanted = os.environ.get("PRESETS", "coarse,normal,fine").split(",")
    for name in wanted:
        dx, dy, dz = PRESETS[name]
        params = SolveParams(
            roi=(2.0, 4.0, 28.0, 16.0), frequencies_hz=FREQS,
            ports=[Port("p1", LINE_MM[0], Y_MM, "F.Cu", half_width_mm=w / 2, excited=True),
                   Port("p2", LINE_MM[1], Y_MM, "F.Cu", half_width_mm=w / 2, excited=False)],
            dx_um=dx, dy_um=dy, dz_um=dz, air_mm=3.0,
        )
        built = build_model(board, transform, params)
        work = OUT / name
        work.mkdir(parents=True, exist_ok=True)
        (work / "model.xml").write_text(built.doc.to_string())
        m = built.mesh
        across = int(((m.y >= Y_MM - w / 2 - 1e-9) & (m.y <= Y_MM + w / 2 + 1e-9)).sum()) - 1
        through = int(((m.z >= -1e-9) & (m.z <= H_MM + 0.07 + 1e-9)).sum())
        print(f"\n== {name}: {m.cells:,} cells, {across} cells across the strip, "
              f"smallest {m.min_cell_mm * 1000:.1f} um", flush=True)
        res = run.run_openems(str(work / "model.xml"), str(work), threads=THREADS,
                              source_ends_at_step=built.source_ends_at_step)
        got = extract(work, freqs)
        err = (np.abs(got["z0"]) / z_hj - 1) * 100
        e_err = (got["eps_eff"] / e_hj - 1) * 100
        print(f"   {res.final_timestep:,} steps, {res.elapsed_s:.0f} s, "
              f"energy {res.final_energy_db:.1f} dB, converged {res.converged}")
        print(f"   {'f GHz':>6} {'Z0':>16} {'err %':>7} {'eps_eff':>8} {'err %':>7}")
        for f, z, e, ee, eerr in zip(freqs, got["z0"], err, got["eps_eff"], e_err):
            print(f"   {f / 1e9:6.2f} {z.real:8.2f}{z.imag:+7.2f}j {e:7.2f} {ee:8.3f} {eerr:7.2f}")
        report["presets"][name] = {
            "cells": m.cells, "cells_across_strip": across, "z_lines_in_board": through,
            "min_cell_um": m.min_cell_mm * 1000, "timesteps": res.final_timestep,
            "elapsed_s": res.elapsed_s, "final_energy_db": res.final_energy_db,
            "converged": res.converged, "warnings": res.warnings,
            "z0_real": got["z0"].real.tolist(), "z0_imag": got["z0"].imag.tolist(),
            "z0_error_percent": err.tolist(), "eps_eff": got["eps_eff"].tolist(),
            "eps_eff_error_percent": e_err.tolist(),
        }
    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
