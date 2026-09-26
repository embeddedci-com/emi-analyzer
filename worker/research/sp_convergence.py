"""Small-part check 3: does cutting a part out change the answer?

The same net cut out three times, with more of the board around it each time (the coupon's
margin), on two mesh presets. If the cut changed the physics, the largest coupon would differ
from the smaller ones: the hotspot would move, its level would shift, the port would read a
different impedance. Each coupon is compared with the largest one on the same preset, so only
the cut differs; the presets are compared with each other at the largest margin, separately.

Pass criteria, against that reference:

* hotspot location within two cells, or the reference's hotspot still within 1 dB of the peak
  (along a matched line the field is flat and its maximum can sit anywhere on it);
* hotspot level within 1 dB (per volt of source, away from the ports);
* |Z_in| within 5 % (median over the band; the worst point is reported too, since it sits on
  a resonance where a few percent of frequency shift is many percent of impedance);
* S21 within 0.5 dB where the net has a second port.

The part is a public synthetic board (``clock_board``): a four-layer stackup like the real
boards', a clock net from an 8-pin driver to a 2-pin load on the top layer with a bend,
neighbouring traces the coupon leaves out, and ground and supply planes on the inner layers.
Real boards are added with ``SETUPS`` (``folder:NET[|NET2]:label``, comma separated), read
from ``/boards`` (a folder with one subfolder per board, mounted read-only). Their net names
never leave the output folder.

    docker run --rm --cpus 3 --memory 6g -v "$PWD/worker:/spike" \\
        -v "$EMI_TEST_BOARDS:/boards:ro" -v "$PWD/spike_out:/spike/spike_out" -w /spike \\
        -e PYTHONPATH=/spike --entrypoint python3 emi-worker:smallpart \\
        research/sp_convergence.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sp_harness as h  # noqa: E402
import sp_metrics as metrics  # noqa: E402
from emi_worker.stages import StageError  # noqa: E402

MARGINS_MM = [float(v) for v in os.environ.get("MARGINS", "2,4.5,7").split(",")]
PRESETS = os.environ.get("PRESETS", "coarse,normal").split(",")
MAP_HZ = [300e6, 1e9]
LEVEL_TOL_DB = 1.0
MOVE_TOL_CELLS = 2.0
Z_TOL_PERCENT = 5.0
S21_TOL_DB = 0.5


def clock_board() -> str:
    """A public stand-in for a real board's clock net. Everything here is made up."""
    plane = "(pts (xy 0 0) (xy 40 0) (xy 40 30) (xy 0 30))"
    soic = "\n".join(
        f'    (pad "{n}" smd rect (at {-2.7 if n <= 4 else 2.7} {(-1.905 + 1.27 * ((n - 1) % 4)) * (1 if n <= 4 else -1):.3f}) '
        f'(size 1.5 0.6) (layers "F.Cu") (net {net}))'
        for n, net in [(1, '3 "CLK"'), (2, '0 ""'), (3, '0 ""'), (4, '1 "GND"'),
                       (5, '0 ""'), (6, '0 ""'), (7, '4 "DATA"'), (8, '2 "+3V3"')])
    return f"""(kicad_pcb
  (version 20241229)
  (generator "emi-analyzer-small-part-check")
  (general (thickness 1.6062))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
          (25 "Edge.Cuts" user))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "prepreg") (thickness 0.2104) (material "FR4")
           (epsilon_r 4.4) (loss_tangent 0.02))
    (layer "In1.Cu" (type "copper") (thickness 0.0152))
    (layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4")
           (epsilon_r 4.6) (loss_tangent 0.02))
    (layer "In2.Cu" (type "copper") (thickness 0.0152))
    (layer "dielectric 3" (type "prepreg") (thickness 0.2104) (material "FR4")
           (epsilon_r 4.4) (loss_tangent 0.02))
    (layer "B.Cu" (type "copper") (thickness 0.035))))
  (net 0 "") (net 1 "GND") (net 2 "+3V3") (net 3 "CLK") (net 4 "DATA") (net 5 "CLK_OUT")
  (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts") (width 0.1))
  (footprint "SOIC-8" (layer "F.Cu") (at 10 15)
    (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
{soic})
  (footprint "R_0402" (layer "F.Cu") (at 29 19)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.5) (layers "F.Cu") (net 3 "CLK"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.5) (layers "F.Cu") (net 5 "CLK_OUT")))
  (segment (start 7.3 13.095) (end 6 13.095) (width 0.3) (layer "F.Cu") (net 3))
  (segment (start 6 13.095) (end 6 11) (width 0.3) (layer "F.Cu") (net 3))
  (segment (start 6 11) (end 18 11) (width 0.3) (layer "F.Cu") (net 3))
  (segment (start 18 11) (end 22 15) (width 0.3) (layer "F.Cu") (net 3))
  (segment (start 22 15) (end 22 19) (width 0.3) (layer "F.Cu") (net 3))
  (segment (start 22 19) (end 28.5 19) (width 0.3) (layer "F.Cu") (net 3))
  (segment (start 12.7 14.365) (end 16 14.365) (width 0.2) (layer "F.Cu") (net 4))
  (segment (start 16 14.365) (end 16 17) (width 0.2) (layer "F.Cu") (net 4))
  (segment (start 16 17) (end 20 21) (width 0.2) (layer "F.Cu") (net 4))
  (segment (start 20 21) (end 34 21) (width 0.2) (layer "F.Cu") (net 4))
  (segment (start 29.5 19) (end 34 19) (width 0.3) (layer "F.Cu") (net 5))
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (hatch edge 0.5)
    (polygon {plane}) (filled_polygon (layer "In1.Cu") {plane}))
  (zone (net 2) (net_name "+3V3") (layer "In2.Cu") (hatch edge 0.5)
    (polygon {plane}) (filled_polygon (layer "In2.Cu") {plane}))
)
"""


def boards() -> list[tuple[str, str, list[str]]]:
    """(label, board text, nets): the public board, then any real ones asked for."""
    out = [("synthetic clock", clock_board(), ["CLK"])]
    for entry in filter(None, os.environ.get("SETUPS", "").split(",")):
        folder, nets, label = entry.split(":")
        path = next(p for p in sorted(Path("/boards", folder).glob("*.kicad_pcb")))
        out.append((label, path.read_text(), nets.split("|")))
    return out


def study(label: str, text: str, nets: list[str]) -> dict:
    runs: dict[str, dict] = {}
    for preset in PRESETS:
        for margin in MARGINS_MM:
            params, c = h.coupon_params(text, nets, preset=preset, margin_mm=margin,
                                        freqs_hz=MAP_HZ)
            key = f"{preset}/{margin:g}"
            name = f"conv-{label.replace(' ', '_')}-{preset}-{margin:g}"
            try:
                got = h.solve(text, params, name)
            except StageError as exc:
                runs[key] = {"preset": preset, "margin_mm": margin, "refused": str(exc)}
                print(f"   {key:>12}: refused: {exc}", flush=True)
                continue
            s = got.summary
            layer = c.ports[0].layer
            ports = [(p.x, p.y) for p in c.ports]
            runs[key] = {
                "preset": preset, "margin_mm": margin,
                "size_mm": c.summary()["size_mm"], "cells": s["cells"],
                "timesteps": s["timesteps"], "elapsed_s": got.elapsed_s,
                "peak_mb": got.peak_mb, "final_energy_db": s["final_energy_db"],
                "converged": s["converged"],
                "hotspot": metrics.hotspot(got, layer, MAP_HZ, ports),
                "port": metrics.port(got),
            }
            r = runs[key]
            print(f"   {key:>12}: {r['size_mm'][0]:.1f} x {r['size_mm'][1]:.1f} mm, "
                  f"{r['cells']:,} cells, {r['timesteps']:,} steps, {r['elapsed_s']:.0f} s, "
                  f"{r['final_energy_db']:.1f} dB; hotspot "
                  + ", ".join(f"{x['frequency_hz'] / 1e6:.0f} MHz ({x['x_mm']:.2f}, "
                              f"{x['y_mm']:.2f}) {x['db_per_volt']:.1f} dB"
                              for x in r["hotspot"]), flush=True)
    # The cut: each margin against the largest, on the same preset, so only the cut differs.
    diffs = {}
    for preset in PRESETS:
        ref_key = f"{preset}/{MARGINS_MM[-1]:g}"
        for margin in MARGINS_MM[:-1]:
            key = f"{preset}/{margin:g}"
            if "refused" in runs[ref_key] or "refused" in runs[key]:
                continue
            d = metrics.compare(runs[ref_key], runs[key])
            d["pass"] = passes(d)
            diffs[f"{key} vs {ref_key}"] = d
            show(f"{key} vs {ref_key}", d)
    # The mesh: the presets against each other at every margin, the product's own among them.
    # Reported, and judged by the same tolerances, but a separate question from the cut.
    mesh = {}
    if len(PRESETS) > 1:
        for margin in MARGINS_MM:
            a = f"{PRESETS[-1]}/{margin:g}"
            for preset in PRESETS[:-1]:
                b = f"{preset}/{margin:g}"
                if "refused" in runs[a] or "refused" in runs[b]:
                    continue
                d = metrics.compare(runs[a], runs[b])
                d["pass"] = passes(d)
                mesh[f"{b} vs {a}"] = d
                show(f"{b} vs {a}", d)
    return metrics.strip({"runs": runs, "cut": diffs, "mesh": mesh})


def passes(d: dict) -> bool:
    """The criteria in the module docstring. A hotspot has not moved if the reference's is still
    within 1 dB of this map's peak, however far the maximum itself wandered along a flat line."""
    located = (d["worst_move_cells"] <= MOVE_TOL_CELLS
               or (d["worst_reference_below_peak_db"] is not None
                   and d["worst_reference_below_peak_db"] <= LEVEL_TOL_DB))
    return bool(located and d["worst_level_db"] <= LEVEL_TOL_DB
                and d["median_z_percent"] <= Z_TOL_PERCENT
                and (d["worst_s21_db"] is None or d["worst_s21_db"] <= S21_TOL_DB))


def show(label: str, d: dict) -> None:
    print(f"   {label}: hotspot moved {d['worst_move_cells']:.1f} cells (reference's is "
          f"{d['worst_reference_below_peak_db']:.2f} dB below the peak), level "
          f"{d['worst_level_db']:.2f} dB; |Z| median {d['median_z_percent']:.1f} % worst "
          f"{d['worst_z_percent']:.1f} %; S21 {d['worst_s21_db']} dB; "
          f"{'pass' if d['pass'] else 'FAIL'}", flush=True)


def main() -> int:
    report = {}
    only = os.environ.get("ONLY")
    for label, text, nets in boards():
        if only and label != only:
            continue
        print(f"== {label}", flush=True)
        report[label] = study(label, text, nets)
    print("\n", h.write_report(os.environ.get("REPORT", "convergence"), report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
