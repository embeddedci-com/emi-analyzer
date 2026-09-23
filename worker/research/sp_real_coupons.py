"""Small-part check 4: coupons cut from real boards, through the product path.

Each coupon is planned the way the app plans it (``coupon.plan``: the net's copper plus a margin,
ports on the two pads furthest apart) and solved by the product's solve stage at the default
band and end criterion, on two presets. Reported per coupon: size, cells, timesteps, simulated
time, wall time, openEMS's peak memory, where the energy ended, whether the run was refused or
its numbers dropped, and how far the two presets agree (hotspot and port, ``sp_metrics``).

A coupon passes when both runs converge on their energy criterion, no frequency is dropped as
truncated, and the presets agree within the convergence study's tolerances
(``sp_convergence.passes``).

``SETUPS`` names them: ``folder:NET[|NET2]:label``, comma separated, read from ``/boards``
(mounted read-only; one subfolder per board). Only the label is printed or written outside the
per-run folders, so the report can be quoted without naming the board or the net.

    docker run --rm --cpus 3 --memory 6g -v "$PWD/worker:/spike" \\
        -v "$EMI_TEST_BOARDS:/boards:ro" -v "$PWD/spike_out:/spike/spike_out" -w /spike \\
        -e PYTHONPATH=/spike -e SETUPS=... --entrypoint python3 emi-worker:smallpart \\
        research/sp_real_coupons.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sp_harness as h  # noqa: E402
import sp_convergence as convergence  # noqa: E402
import sp_metrics as metrics  # noqa: E402
from emi_worker.kicad import parse, parse_board  # noqa: E402
from emi_worker.openems import coupon  # noqa: E402
from emi_worker.stages import StageError  # noqa: E402

PRESETS = os.environ.get("PRESETS", "coarse,normal").split(",")
MAP_HZ = [300e6, 1e9]


def main() -> int:
    report: dict = {}
    for entry in filter(None, os.environ.get("SETUPS", "").split(",")):
        folder, nets, label = entry.split(":")
        path = next(p for p in sorted(Path("/boards", folder).glob("*.kicad_pcb")))
        text = path.read_text()
        board = parse_board(parse(text))
        copper = sum(1 for s in board.stackup if s.is_copper)
        print(f"== {label} ({copper} layers)", flush=True)
        runs = {}
        for preset in PRESETS:
            try:
                params, c = h.coupon_params(text, nets.split("|"), preset=preset,
                                            freqs_hz=MAP_HZ)
            except coupon.CouponError as exc:
                runs[preset] = {"refused": "coupon: " + str(exc).split(":")[-1]}
                print(f"   {preset}: no coupon ({exc})")
                continue
            try:
                got = h.solve(text, params, f"real-{label.replace(' ', '_')}-{preset}")
            except StageError as exc:
                runs[preset] = {"refused": str(exc)}
                print(f"   {preset}: refused: {exc}")
                continue
            s = got.summary
            dt = s["mesh"]["dt_seconds"]
            net = got.json("network.json")
            ports = [(p.x, p.y) for p in c.ports]
            r = {
                "size_mm": c.summary()["size_mm"], "ports": len(c.ports),
                "notes": [n for n in c.notes], "cells": s["cells"],
                "timesteps": s["timesteps"], "max_timesteps": s["max_timesteps"],
                "simulated_ns": s["timesteps"] * dt * 1e9, "elapsed_s": got.elapsed_s,
                "peak_mb": got.peak_mb, "final_energy_db": s["final_energy_db"],
                "converged": s["converged"], "unusable_reason": net.get("unusable_reason"),
                "truncated": len(net["truncated_hz"]),
                "hotspot": metrics.hotspot(got, c.ports[0].layer, MAP_HZ, ports),
                "port": metrics.port(got),
            }
            runs[preset] = r
            print(f"   {preset}: {r['size_mm'][0]:.1f} x {r['size_mm'][1]:.1f} mm, "
                  f"{r['ports']} ports, {r['cells']:,} cells, {r['timesteps']:,} of "
                  f"{r['max_timesteps']:,} steps ({r['simulated_ns']:.1f} ns), "
                  f"{r['elapsed_s']:.0f} s, {r['peak_mb']:.0f} MB, "
                  f"{r['final_energy_db']:.1f} dB, truncated {r['truncated']}, "
                  f"unusable: {r['unusable_reason']}", flush=True)
        done = [runs[p] for p in PRESETS if p in runs and "cells" in runs[p]]
        verdict = {"runs": runs}
        if len(done) == 2:
            d = metrics.compare(done[1], done[0])
            verdict["presets_agree"] = d
            d["pass"] = convergence.passes(d)
            verdict["runs_clean"] = all(
                r["converged"] and not r["truncated"] and not r["unusable_reason"] for r in done)
            verdict["pass"] = bool(verdict["runs_clean"] and d["pass"])
            convergence.show("presets", d)
        report[label] = metrics.strip(verdict)
    print("\n", h.write_report(os.environ.get("REPORT", "real_coupons"), report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
