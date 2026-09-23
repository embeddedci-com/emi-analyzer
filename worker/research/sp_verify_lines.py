"""Small-part check 1: does a coupon of a line give the line's impedance, delay and S21?

A 50 ohm microstrip and a 50 ohm stripline, each drawn as a KiCad board with a ground pour and
put through the product's small-part solve end to end (``sp_harness``): the coupon cut, the
ports the coupon puts at the line's pads, the default band and end criterion, and the
``network.json`` the app reads. Nothing here is a hand-made grid.

Z0 and eps_eff come from the two ports' voltages and currents, as a symmetric reciprocal
two-port (``sp_harness.line_from_z``); the delay is l * sqrt(eps_eff) / c. S21 is read from
``network.json`` and compared with a lossy line of the closed-form Z0 and eps_eff between the
same two 50 ohm ports, dielectric loss included (the copper is perfect in the model).

References and pass criteria:

* microstrip: Hammerstad and Jensen (1980), zero-thickness strip, which is what the model draws;
  Z0 and delay within 5 %. IPC-2141 printed as a sanity bound, not a criterion.
* stripline: Cohn (1954), exact for a zero-thickness strip centred between two planes; Z0 and
  delay within 5 %.
* S21 of the matched microstrip within 0.5 dB of the closed form, 100 MHz to 1 GHz.

``PRESETS`` (comma separated, default "coarse,normal") picks the mesh presets.

    docker run --rm --cpus 3 --memory 6g -v "$PWD/worker:/spike" \\
        -v "$PWD/spike_out:/spike/spike_out" -w /spike -e PYTHONPATH=/spike \\
        --entrypoint python3 emi-worker:smallpart research/sp_verify_lines.py
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
from emi_worker.openems import coupon  # noqa: E402
from emi_worker.openems.model import Port, _layer_z  # noqa: E402

ER = 4.4
TAN_D = 0.02
MS_H = 0.2
LINE = (5.0, 30.0)
Y = 10.0
CHECK_HZ = np.array([0.5e9, 0.75e9, 1e9, 1.5e9, 2e9])
Z_TOL = 0.05
S21_TOL_DB = 0.5
S21_TO_HZ = 1e9


def microstrip_board(w: float) -> str:
    return f"""(kicad_pcb
  (version 20241229)
  (generator "emi-analyzer-small-part-check")
  (general (thickness {MS_H + 0.07}))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "core") (thickness {MS_H}) (material "FR4")
           (epsilon_r {ER}) (loss_tangent {TAN_D}))
    (layer "B.Cu" (type "copper") (thickness 0.035))))
  (net 0 "") (net 1 "GND") (net 2 "SIG")
  (gr_rect (start 0 0) (end 35 20) (layer "Edge.Cuts") (width 0.1))
  (footprint "Pad" (layer "F.Cu") (at {LINE[0]} {Y})
    (property "Reference" "J1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size {w:.4f} {w:.4f}) (layers "F.Cu") (net 2 "SIG")))
  (footprint "Pad" (layer "F.Cu") (at {LINE[1]} {Y})
    (property "Reference" "J2" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size {w:.4f} {w:.4f}) (layers "F.Cu") (net 2 "SIG")))
  (segment (start {LINE[0]} {Y}) (end {LINE[1]} {Y}) (width {w:.4f}) (layer "F.Cu") (net 2))
  (zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5)
    (polygon (pts (xy 0 0) (xy 35 0) (xy 35 20) (xy 0 20)))
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 35 0) (xy 35 20) (xy 0 20))))
)
"""


SL_D = 0.2          # each dielectric either side of the strip
SL_T = 0.0152       # inner copper


def stripline_board(w: float) -> str:
    vias = []
    for x in np.arange(LINE[0] - 1.0, LINE[1] + 1.01, 2.5):
        for dy in (-1.5, 1.5):
            vias.append(f'  (via (at {x:.3f} {Y + dy:.3f}) (size 0.5) (drill 0.25) '
                        f'(layers "F.Cu" "B.Cu") (net 1))')
    plane = "(pts (xy 0 0) (xy 35 0) (xy 35 20) (xy 0 20))"
    return f"""(kicad_pcb
  (version 20241229)
  (generator "emi-analyzer-small-part-check")
  (general (thickness 1.0))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
          (25 "Edge.Cuts" user))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "prepreg") (thickness {SL_D}) (material "FR4")
           (epsilon_r {ER}) (loss_tangent {TAN_D}))
    (layer "In1.Cu" (type "copper") (thickness {SL_T}))
    (layer "dielectric 2" (type "core") (thickness {SL_D}) (material "FR4")
           (epsilon_r {ER}) (loss_tangent {TAN_D}))
    (layer "In2.Cu" (type "copper") (thickness {SL_T}))
    (layer "dielectric 3" (type "prepreg") (thickness 0.5) (material "FR4")
           (epsilon_r {ER}) (loss_tangent {TAN_D}))
    (layer "B.Cu" (type "copper") (thickness 0.035))))
  (net 0 "") (net 1 "GND") (net 2 "SIG")
  (gr_rect (start 0 0) (end 35 20) (layer "Edge.Cuts") (width 0.1))
  (segment (start {LINE[0]} {Y}) (end {LINE[1]} {Y}) (width {w:.4f}) (layer "In1.Cu") (net 2))
{chr(10).join(vias)}
  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
    (polygon {plane}) (filled_polygon (layer "F.Cu") {plane}))
  (zone (net 1) (net_name "GND") (layer "In2.Cu") (hatch edge 0.5)
    (polygon {plane}) (filled_polygon (layer "In2.Cu") {plane}))
)
"""


def lossy_s21(z0: float, eeff: float, er: float, tan_d: float, length_m: float,
              freqs: np.ndarray, zr: float = 50.0) -> np.ndarray:
    """S21 of a line with dielectric loss only, between two ``zr`` ports."""
    k0 = 2 * np.pi * freqs / h.C0
    beta = k0 * math.sqrt(eeff)
    # Pozar (3.198): the part of the field in the dielectric carries its loss.
    alpha = k0 * er * (eeff - 1) * tan_d / (2 * math.sqrt(eeff) * (er - 1))
    gl = (alpha + 1j * beta) * length_m
    return 2.0 / (2 * np.cosh(gl) + (z0 / zr + zr / z0) * np.sinh(gl))


def check_line(kind: str, text: str, ports, roi, nets, z_ref: float, e_ref: float,
               preset: str) -> dict:
    params = h.small_part_params(roi, ports, preset=preset, nets=nets,
                                 freqs_hz=[5e8, 1e9, 2e9])
    got = h.solve(text, params, f"{kind}-{preset}")
    length = (LINE[1] - LINE[0]) / 1000.0
    z11, z12 = h.z_params(got.workdir, CHECK_HZ)
    line = h.line_from_z(z11, z12, length, CHECK_HZ)
    z_err = np.abs(line["z0"]) / z_ref - 1
    delay = length * np.sqrt(line["eps_eff"]) / h.C0
    d_ref = length * math.sqrt(e_ref) / h.C0
    d_err = delay / d_ref - 1
    s = got.summary
    print(f"\n== {kind}, {preset}: {s['cells']:,} cells, {s['timesteps']:,} steps, "
          f"{got.elapsed_s:.0f} s, {s['final_energy_db']:.1f} dB, peak {got.peak_mb:.0f} MB")
    print(f"   {'f GHz':>6} {'Z0':>16} {'err %':>7} {'delay ps':>9} {'err %':>7}")
    for f, z, e, d, de in zip(CHECK_HZ, line["z0"], z_err, delay, d_err):
        print(f"   {f / 1e9:6.2f} {z.real:8.2f}{z.imag:+7.2f}j {e * 100:7.2f} "
              f"{d * 1e12:9.2f} {de * 100:7.2f}")
    return {
        "preset": preset, "cells": s["cells"], "timesteps": s["timesteps"],
        "elapsed_s": got.elapsed_s, "final_energy_db": s["final_energy_db"],
        "peak_mb": got.peak_mb, "converged": s["converged"], "warnings": s["warnings"],
        "frequencies_hz": CHECK_HZ.tolist(),
        "z0_real": line["z0"].real.tolist(), "z0_imag": line["z0"].imag.tolist(),
        "z0_error": z_err.tolist(), "eps_eff": line["eps_eff"].tolist(),
        "delay_s": delay.tolist(), "delay_ref_s": d_ref, "delay_error": d_err.tolist(),
        "pass_z0": bool(np.all(np.abs(z_err) <= Z_TOL)),
        "pass_delay": bool(np.all(np.abs(d_err) <= Z_TOL)),
        "network": got.json("network.json") if "network.json" in got.files else None,
    }


def main() -> int:
    presets = os.environ.get("PRESETS", "coarse,normal").split(",")
    which = os.environ.get("LINES", "microstrip,stripline").split(",")
    report: dict = {}

    if "microstrip" in which:
        w = h.width_for(50.0, lambda x: h.hammerstad_jensen(x, MS_H, ER)[0])
        z_hj, e_hj = h.hammerstad_jensen(w, MS_H, ER)
        text = microstrip_board(w)
        params, c = h.coupon_params(text, ["SIG"])
        print(f"microstrip {w * 1000:.1f} um on {MS_H * 1000:.0f} um: Hammerstad-Jensen "
              f"{z_hj:.2f} ohm, eps_eff {e_hj:.3f}; IPC-2141 "
              f"{h.ipc2141_microstrip(w, MS_H, ER):.1f} ohm; coupon "
              f"{c.roi[2] - c.roi[0]:.1f} x {c.roi[3] - c.roi[1]:.1f} mm, ports "
              f"{[(p.name, p.layer, p.reference_layer, p.excited) for p in c.ports]}")
        runs = {}
        for preset in presets:
            r = check_line("microstrip", text, c.ports, c.roi, ["SIG"], z_hj, e_hj, preset)
            net = r["network"]
            f = np.asarray(net["frequencies_hz"])
            want = lossy_s21(z_hj, e_hj, ER, TAN_D, (LINE[1] - LINE[0]) / 1000.0, f)
            got = np.array([np.nan if v is None else v for v in net["transmission"][0]["s_db"]])
            diff = got - 20 * np.log10(np.abs(want))
            upto = f <= S21_TO_HZ * 1.0001
            r["s21"] = {"frequencies_hz": f.tolist(), "db": got.tolist(),
                        "closed_form_db": (20 * np.log10(np.abs(want))).tolist(),
                        "worst_to_1ghz_db": float(np.nanmax(np.abs(diff[upto]))),
                        "worst_to_band_top_db": float(np.nanmax(np.abs(diff))),
                        "truncated_hz": net["truncated_hz"]}
            r["pass_s21"] = bool(np.all(np.abs(diff[upto]) <= S21_TOL_DB))
            print(f"   S21: worst {r['s21']['worst_to_1ghz_db']:.2f} dB off the closed form to "
                  f"1 GHz, {r['s21']['worst_to_band_top_db']:.2f} dB to 2 GHz; "
                  f"{len(net['truncated_hz'])} frequencies truncated")
            runs[preset] = r
        report["microstrip"] = {"width_mm": w, "h_mm": MS_H, "z0_ref": z_hj, "eps_eff_ref": e_hj,
                                "z0_ipc2141": h.ipc2141_microstrip(w, MS_H, ER),
                                "coupon": c.summary(), "runs": runs}

    if "stripline" in which:
        board = parse_board(parse(stripline_board(0.1)))
        lz = _layer_z(board)
        b = lz["F.Cu"] - lz["In2.Cu"]
        off = (lz["In1.Cu"] - lz["In2.Cu"]) / b
        w = h.width_for(50.0, h.stripline_cohn, b, ER)
        z_c = h.stripline_cohn(w, b, ER)
        text = stripline_board(w)
        board = parse_board(parse(text))
        m = coupon.margin_for(board)
        roi = (LINE[0] - m, Y - w / 2 - m, LINE[1] + m, Y + w / 2 + m)
        ports = [Port("p1", LINE[0], Y, "In1.Cu", w / 2, 50.0, True, "In2.Cu"),
                 Port("p2", LINE[1], Y, "In1.Cu", w / 2, 50.0, False, "In2.Cu")]
        print(f"\nstripline {w * 1000:.1f} um between planes {b * 1000:.1f} um apart (strip at "
              f"{off:.3f} of the gap): Cohn {z_c:.2f} ohm, IPC-2141 "
              f"{h.ipc2141_stripline(w, b, ER):.1f} ohm")
        runs = {}
        for preset in presets:
            runs[preset] = check_line("stripline", text, ports, roi, ["SIG"], z_c, ER, preset)
        report["stripline"] = {"width_mm": w, "plane_gap_mm": b, "strip_position": off,
                               "z0_ref": z_c, "eps_eff_ref": ER,
                               "z0_ipc2141": h.ipc2141_stripline(w, b, ER), "runs": runs}

    print("\n", h.write_report("lines", report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
