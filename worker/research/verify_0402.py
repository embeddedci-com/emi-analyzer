"""Phase 2 · Does a modelled 0402 capacitor behave like a capacitor?

The only check that the component construction is a series R-L-C and not something else: one
0402 capacitor on a two-layer board, its first pad driven by a production port against the
plane 0.2 mm below, its second pad taken to the plane by a via in the pad. The capacitor's value
is ``VALUE`` (default "100p"); it is matched to the built-in generic library and placed by the
production path (``model_components``), exactly as a solve would, and its C, ESL and ESR are read
back from what was placed.

**The value matters for a reason that is itself a finding.** The port is 50 ohm, so the part
sits in a series R-C whose time constant is 50 ohm x C: 50 ns for 1 nF, 5 us for 100 nF. The
pulse's low-frequency content charges the capacitor and the run then waits for that charge to
drain through the port. With 1 nF the run reached its 50 ns cap with the port voltage flat at
4.6e-6 V, unconverged. 100 pF drains in 5 ns and puts the self-resonance, 750 MHz, in a band the
solve can resolve quickly.

The library's ESL excludes the mounting loop, because the solve models pads, vias and planes
itself. So the same model is solved a second time with the capacitor's gap bridged by metal:
that run is the port, the pads, the via and the plane and nothing else, and subtracting it
leaves the part:

    Z_part(f) = Z_in(f) - Z_in,short(f)       vs      ESR + j w ESL + 1 / (j w C)

The mounting inductance is read from the short, L_mount = Im(Z_short) / w, and the mounted
self-resonance 1 / (2 pi sqrt((ESL + L_mount) C)) is compared with where Im(Z_in) crosses zero.

Pass criteria (docs/known-issues.md): SRF within 5 %, and |Z_part| within 1 dB of the analytic
series R-L-C from a third of the SRF to three times it.

The solve refuses to place a component on a solver without a series lumped element, which is
what openEMS 0.0.35 is, so this needs the image built with OPENEMS_SOURCE=build:

    docker run --rm --cpus 3 --memory 6g -v "$PWD/worker:/spike" \\
        -v "$PWD/spike_out:/spike/spike_out" -w /spike -e PYTHONPATH=/spike \\
        --entrypoint python3 emi-worker:ci-build research/verify_0402.py
"""

from __future__ import annotations

import copy
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emi_worker.kicad import parse, parse_board  # noqa: E402
from emi_worker.kicad.normalize import _board_extent  # noqa: E402
from emi_worker.openems import csx, post, run  # noqa: E402
from emi_worker.openems.model import (  # noqa: E402
    Port, SolveParams, build_model, excitation_seconds,
)

OUT = Path(os.environ.get("OUT", "/spike/spike_out")) / f"cap_0402_{os.environ.get('VALUE', '100p')}"
THREADS = int(os.environ.get("THREADS", "3"))
PRESET = {"coarse": (150, 150, 100), "normal": (75, 75, 50),
          "fine": (50, 50, 25)}[os.environ.get("PRESET", "coarse")]

VALUE = os.environ.get("VALUE", "100p")
X0, Y0 = 15.0, 10.0
PAD_DX = 0.48


def _library_values() -> tuple[float, float, float]:
    """C, ESL and ESR the generic library gives an 0402 of ``VALUE``, as the solve will place it."""
    from emi_worker.components import match_part, resolve_part

    got = resolve_part(match_part("C1", VALUE, "Capacitor_SMD:C_0402_1005Metric"), None)
    return got.rlc.c_f, got.rlc.esl_h, got.rlc.esr_ohm


C_F, ESL_H, ESR_OHM = _library_values()
SRF_PART = 1 / (2 * math.pi * math.sqrt(ESL_H * C_F))

#: From a third of the part's own SRF to three and a half times it; the mounted SRF is lower.
FREQS = [float(f) for f in np.geomspace(SRF_PART / 3.2, SRF_PART * 3.5, 25)]

BOARD = f"""(kicad_pcb
  (version 20241229)
  (generator "emi-analyzer-0402-check")
  (general (thickness 0.27))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 0.2) (material "FR4")
             (epsilon_r 4.4) (loss_tangent 0))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (footprint "Capacitor_SMD:C_0402_1005Metric" (layer "F.Cu") (at {X0} {Y0} 0)
    (property "Reference" "C1" (at 0 -1.2 0) (layer "F.SilkS"))
    (property "Value" "{VALUE}" (at 0 1.2 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at {-PAD_DX} 0) (size 0.56 0.62) (layers "F.Cu" "F.Mask")
         (roundrect_rratio 0.25) (net 2 "VCC"))
    (pad "2" smd roundrect (at {PAD_DX} 0) (size 0.56 0.62) (layers "F.Cu" "F.Mask")
         (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (via (at {X0 + PAD_DX} {Y0}) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1))
  (zone (net 1) (net_name "GND") (layer "B.Cu")
    (hatch edge 0.5) (min_thickness 0.25)
    (polygon (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)))
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)))
  )
)
"""


def params() -> SolveParams:
    dx, dy, dz = PRESET
    return SolveParams(
        roi=(X0 - 3.0, Y0 - 3.0, X0 + 3.0, Y0 + 3.0), frequencies_hz=FREQS,
        ports=[Port("p1", X0 - PAD_DX, Y0, "F.Cu", half_width_mm=0.2)],
        dx_um=dx, dy_um=dy, dz_um=dz, air_mm=3.0,
        model_components=True, solver_series_rlc=True,
        # A 20:1 band is one cycle of the pulse's carrier, and openEMS checks its end criterion
        # while the source is on; -70 dB is below any dip between its lobes.
        end_criteria=1e-7,
    )


def shorted(built):
    """The same document with the part's gap bridged by metal: the mounting alone."""
    doc = copy.deepcopy(built.doc)
    part = [p for p in doc.properties
            if isinstance(p, csx.LumpedElement) and p.name.startswith("cap_C1")]
    assert part, "the capacitor was not placed"
    boxes = [prim for p in part for prim in p.primitives]
    for p in part:
        doc.properties.remove(p)
    x0 = min(b.p1[0] for b in boxes)
    x1 = max(b.p2[0] for b in boxes)
    b = boxes[0]
    doc.add(csx.Metal(name="gap_short", primitives=[
        csx.Box(p1=(x0, b.p1[1], b.p1[2]), p2=(x1, b.p2[1], b.p2[2]),
                priority=csx.PRIORITY_PORT)]))
    return doc


def solve(name: str, doc, built) -> tuple[np.ndarray, run.RunResult]:
    work = OUT / name
    work.mkdir(parents=True, exist_ok=True)
    (work / "model.xml").write_text(doc.to_string())
    res = run.run_openems(str(work / "model.xml"), str(work), threads=THREADS,
                          excitation_s=excitation_seconds(built.doc.excitation.fc))
    u = post.read_probe(str(work / "p1_ut"))
    i = post.read_probe(str(work / "p1_it"))
    f = np.asarray(FREQS)
    return post._dft(u, f) / post._dft(i, f), res


def crossing(f: np.ndarray, x: np.ndarray) -> float | None:
    """Where x crosses zero from below, interpolated in log frequency."""
    for k in range(len(f) - 1):
        if x[k] < 0 <= x[k + 1]:
            t = -x[k] / (x[k + 1] - x[k])
            return float(math.exp(math.log(f[k]) + t * (math.log(f[k + 1]) - math.log(f[k]))))
    return None


def main() -> int:
    board = parse_board(parse(BOARD))
    built = build_model(board, _board_extent(board), params())
    print("notes:", *built.notes, sep="\n  ")
    if not built.modelled_parts:
        raise SystemExit("C1 was not placed; see the notes above")
    f = np.asarray(FREQS)
    w = 2 * np.pi * f
    z_short, res_short = solve("short", shorted(built), built)
    z_cap, res_cap = solve("cap", built.doc, built)
    l_mount = z_short.imag / w
    z_part = z_cap - z_short
    want = ESR_OHM + 1j * w * ESL_H + 1 / (1j * w * C_F)
    err_db = 20 * np.log10(np.abs(z_part) / np.abs(want))

    lm = float(np.median(l_mount))
    srf_part = SRF_PART
    srf_mounted_expected = 1 / (2 * math.pi * math.sqrt((ESL_H + lm) * C_F))
    srf_mounted = crossing(f, z_cap.imag)
    srf_part_measured = crossing(f, z_part.imag)
    band = (f >= srf_part / 3) & (f <= 3 * srf_part)

    for name, r in (("short", res_short), ("part", res_cap)):
        if not r.converged:
            print(f"WARNING: the {name} run is not usable: {r.unconverged_reason()}")
    print(f"\nmesh {built.mesh.cells:,} cells; short {res_short.final_timestep:,} steps "
          f"({res_short.final_energy_db:.1f} dB); part {res_cap.final_timestep:,} steps "
          f"({res_cap.final_energy_db:.1f} dB)")
    for wmsg in res_cap.warnings:
        print("  solver:", wmsg)
    print(f"mounting inductance from the short: {lm * 1e9:.3f} nH "
          f"({l_mount.min() * 1e9:.3f}-{l_mount.max() * 1e9:.3f} across the band)")
    print(f"{'f MHz':>8} {'Z_part':>22} {'analytic':>22} {'err dB':>7}")
    for fr, zp, zw, e in zip(f, z_part, want, err_db):
        print(f"{fr / 1e6:8.1f} {zp.real:10.3f}{zp.imag:+10.3f}j {zw.real:10.3f}{zw.imag:+10.3f}j "
              f"{e:7.2f}")
    fmt = lambda v: f"{v / 1e6:.1f} MHz" if v else "none in band"  # noqa: E731
    print(f"\npart SRF: analytic {fmt(srf_part)}, measured {fmt(srf_part_measured)}")
    print(f"mounted SRF: analytic {fmt(srf_mounted_expected)}, measured {fmt(srf_mounted)}")
    worst = float(np.max(np.abs(err_db[band]))) if band.any() else float("nan")
    print(f"worst |Z| error from SRF/3 to 3 x SRF: {worst:.2f} dB")

    report = {
        "preset": PRESET, "cells": built.mesh.cells,
        "c_f": C_F, "esl_h": ESL_H, "esr_ohm": ESR_OHM, "frequencies_hz": FREQS,
        "l_mount_h": lm, "srf_part_hz": srf_part, "srf_part_measured_hz": srf_part_measured,
        "srf_mounted_expected_hz": srf_mounted_expected, "srf_mounted_measured_hz": srf_mounted,
        "worst_error_db_srf_over_3_to_3x": worst,
        "z_part_real": z_part.real.tolist(), "z_part_imag": z_part.imag.tolist(),
        "z_in_real": z_cap.real.tolist(), "z_in_imag": z_cap.imag.tolist(),
        "z_short_real": z_short.real.tolist(), "z_short_imag": z_short.imag.tolist(),
        "error_db": err_db.tolist(), "warnings": res_cap.warnings,
        "timesteps": [res_short.final_timestep, res_cap.final_timestep],
        "converged": [res_short.converged, res_cap.converged],
        "elapsed_s": [res_short.elapsed_s, res_cap.elapsed_s],
    }
    (OUT / "report.json").write_text(
        json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
