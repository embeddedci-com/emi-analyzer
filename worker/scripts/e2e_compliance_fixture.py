#!/usr/bin/env python3
"""End to end: a real solve of the fixture board with the far field, then a compliance run on it.

Runs only in the worker image (openEMS, nf2ff and nec2c are there):

    docker run --rm --user root --entrypoint sh -v "$PWD:/repo:ro" \\
        ghcr.io/embeddedci-com/emi-worker:dev \\
        -c 'cp -r /repo /tmp/repo && cd /tmp/repo/worker && python scripts/e2e_compliance_fixture.py'

What it checks, beyond "it finished":

1. **The frequency-domain convention.** ``farfield.json`` divides an openEMS field dump by the
   port's source voltage. openEMS writes FD dumps as 2·Σx·e^(-jωt)·Δt; ``post._dft`` has no
   factor of 2, so ``post.OPENEMS_FD_SCALE`` puts it back. That is checked here against openEMS
   itself: an E-field FD dump is laid along the port's own voltage probe, integrated across the
   gap, and compared with the probe's DFT. The ratio must be 2, not 1 -- a 6 dB question.
2. **The chain.** The solve's artifacts go through the compliance stage with a 25 MHz clock
   attached, exactly as a worker would read them, and the result is printed.

Nothing here is an independent check of the far field's *level* -- that needs a second solver or
a measurement, and is listed as unverified in docs/known-issues.md.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emi_worker.client import RunToken  # noqa: E402
from emi_worker.openems import csx, post  # noqa: E402
from emi_worker.openems import model as emmodel  # noqa: E402
from emi_worker.stages import StageContext  # noqa: E402
from emi_worker.stages import solve as solve_stage  # noqa: E402
from emi_worker.stages.compliance import run_compliance  # noqa: E402

BOARD = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "tiny.kicad_pcb").read_bytes()
WORKDIR = os.environ.get("E2E_WORKDIR", "/tmp/e2e-compliance")
CHECK_FREQS = [100e6, 300e6, 700e6]

CLOCK = {
    "format": "emi-driver", "version": 1, "name": "U1 clock", "net": "CLK",
    "kind": "trapezoid", "role": "signal",
    "trapezoid": {
        "amplitude_v": {"value": 3.3, "source": "datasheet"},
        "period_s": {"value": 4e-8, "source": "datasheet"},
        "pulse_width_s": {"value": 2e-8, "source": "datasheet"},
        "rise_s": {"value": 1.5e-9, "source": "datasheet"},
        "fall_s": {"value": 1.5e-9, "source": "datasheet"},
        "source_impedance_ohm": {"value": 33, "source": "assumed"},
    },
}


class Client:
    def __init__(self, blobs: dict, info: dict):
        self.blobs, self.info = dict(blobs), info
        self.uploads: dict[str, bytes] = {}
        self.last = 0.0

    def run_input(self, token):
        return self.info

    def download(self, url):
        return self.blobs[url]

    def progress(self, token, stage="", pct=0, message="", **fields):
        now = time.monotonic()
        if now - self.last > 20 or stage in ("done", "post", "mesh"):
            print(f"  [{stage} {pct:.0f}%] {message}", flush=True)
            self.last = now

    def upload_artifact(self, token, name, blob, content_type):
        self.uploads[name] = blob
        return {"name": name, "size_bytes": len(blob)}


def ctx_for(client, params) -> StageContext:
    return StageContext(client=client, token=RunToken("t", "e2e", "j", 3600),
                        run={"params": params}, workdir=WORKDIR, cores=os.cpu_count() or 2,
                        max_cells=0, should_stop=lambda: False)


def add_port_dump(build):
    """Wrap build_model so the model also dumps E in the frequency domain along p1's probe."""
    def wrapped(board, transform, params):
        built = build(board, transform, params)
        probe = next(p for p in built.doc.properties
                     if isinstance(p, csx.ProbeBox) and p.name == "p1_ut")
        built.doc.add(csx.DumpBox(name="check_port_E", dump_type=csx.DUMP_E_FREQ, dump_mode=0,
                                  frequencies=CHECK_FREQS, primitives=list(probe.primitives)))
        return built
    return wrapped


def check_fd_convention(workdir: str) -> list[float]:
    import h5py

    with h5py.File(os.path.join(workdir, "check_port_E.h5"), "r") as f:
        z = np.asarray(f["Mesh/z"][:], dtype=float)   # metres
        fd = f["FieldData/FD"]
        integrals = []
        for k in range(len(CHECK_FREQS)):
            ez = (np.asarray(fd[f"f{k}_real"][:]) + 1j * np.asarray(fd[f"f{k}_imag"][:]))
            # (component, z, y, x) or (x, y, z, component) depending on the writer; take the
            # z component along whichever axis has the z lines.
            ez = np.squeeze(ez)
            if ez.shape[0] == 3:
                ez = ez[2]
            elif ez.shape[-1] == 3:
                ez = ez[..., 2]
            ez = np.ravel(ez)
            n = min(len(ez), len(z))
            if n < 2:
                # A single sampled edge: raw mode stores E on the edge from z[0] to z[1].
                integrals.append(abs(ez[0]) * abs(z[-1] - z[0]))
                continue
            dz = np.diff(z[:n])
            integrals.append(abs(np.sum(ez[: n - 1] * dz)))
    u = post.read_probe(os.path.join(workdir, "p1_ut"))
    ours = np.abs(post._dft(u, np.asarray(CHECK_FREQS)))
    return [a / b for a, b in zip(integrals, ours)]


def main() -> None:
    os.makedirs(WORKDIR, exist_ok=True)
    solve_params = {
        "roi": {"min_x_mm": 4.0, "min_y_mm": 24.0, "max_x_mm": 30.0, "max_y_mm": 38.0},
        # 30 MHz to 1 GHz: the whole scan for a 25 MHz clock (47 CFR 15.33(b)).
        "frequencies_hz": [30e6, 100e6, 300e6, 1e9],
        "ports": [{"name": "p1", "x_mm": 10.0, "y_mm": 30.0, "layer": "F.Cu",
                   "half_width_mm": 0.15, "excited": True}],
        "mesh": {"dx_um": 150, "dy_um": 150, "dz_um": 100},
        "far_field": True,
    }
    client = Client({"board": BOARD}, {"input_url": "board"})
    print("solve: tiny.kicad_pcb, 30 MHz-1 GHz, far field on", flush=True)
    solve_stage.emmodel.build_model = add_port_dump(emmodel.build_model)
    t0 = time.monotonic()
    result = solve_stage.run_solve(ctx_for(client, solve_params))
    print(f"solve finished in {time.monotonic() - t0:.0f} s: {result.summary['cells']:,} cells, "
          f"{result.summary['timesteps']:,} steps, converged={result.summary['converged']}")
    for w in result.summary["warnings"]:
        print("  note:", w)

    workdir = os.path.join(WORKDIR, "e2e", "openems")
    ratios = check_fd_convention(workdir)
    print("openEMS FD dump / post._dft of the same port voltage:",
          ", ".join(f"{f / 1e6:g} MHz {r:.3f}" for f, r in zip(CHECK_FREQS, ratios)))

    if "farfield.json" not in client.uploads:
        note = json.loads(client.uploads["manifest.json"]).get("far_field_note")
        raise SystemExit(f"no far field: {note}")
    ff = json.loads(client.uploads["farfield.json"])
    print(f"farfield.json v{ff['format_version']}: {len(ff['frequencies_hz'])} frequencies "
          f"{ff['frequencies_hz'][0] / 1e6:.1f}-{ff['frequencies_hz'][-1] / 1e6:.1f} MHz, "
          f"box clearance {ff['clearance_mm']:.0f} mm, "
          f"usable {sum(ff['usable'])}/{len(ff['usable'])}")
    for k in range(0, len(ff["frequencies_hz"]), 12):
        e = ff["e_per_volt"][k]
        print(f"  {ff['frequencies_hz'][k] / 1e6:8.1f} MHz  E/V = {e:.3e} V/m/V "
              f"({20 * math.log10(e / 1e-6) if e > 0 else float('-inf'):.1f} dBuV/m per V)  "
              f"Z_in = {ff['z_in_real'][k]:.1f}{ff['z_in_imag'][k]:+.1f}j")

    blobs = {"board": BOARD, **{n: b for n, b in client.uploads.items() if n.endswith(".json")}}
    info = {"input_url": "board",
            "solve": {"run_id": "e2e", "artifacts": {n: n for n in (
                "manifest.json", "ports.json", "farfield.json", "cable_ports.json",
                "cable_antenna.json") if n in blobs}},
            "drivers": [{"id": "clk", "document": CLOCK}]}
    cc = Client(blobs, info)
    res = run_compliance(ctx_for(cc, {
        "solve_run_id": "e2e", "cable_assignments": {"J1": {"type": "none"}},
        "power": "dc", "enclosure": "plastic",
    }))
    doc = json.loads(cc.uploads["compliance.json"])
    print("\ncompliance:", json.dumps(res.summary))
    for g in doc["gaps"]:
        print("  gap:", g["key"], "-", g["message"])
    print("  inputs:", json.dumps({k: doc["inputs"][k] for k in (
        "connectors", "excited_ports", "far_field_ports", "covered_hz", "required_hz")}))
    driven = [p for p in doc["spectrum"] if p["field_dbuv_per_m"] is not None]
    print(f"  {len(doc['spectrum'])} spectrum points, {len(driven)} driven")
    for p in sorted(driven, key=lambda q: q["limit_dbuv_per_m"] - q["field_dbuv_per_m"])[:5]:
        print(f"  {p['frequency_hz'] / 1e6:7.1f} MHz  {p['field_dbuv_per_m']:6.1f} dBuV/m  "
              f"limit {p['limit_dbuv_per_m']:.1f} ({p['detector']})")
    if "margin_db" in doc:
        print(f"  margin {doc['margin_db']:+.1f} dB at {doc['worst']['frequency_hz'] / 1e6:.0f} "
              f"MHz, sigma {doc['sigma_db']:.2f} dB, confidence (uncalibrated) "
              f"{doc['confidence_uncalibrated']:.2f}")
    for n in doc["notes"]:
        print("  note:", n)


if __name__ == "__main__":
    main()
