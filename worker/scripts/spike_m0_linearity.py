"""M0 · D2 — is a solve's transfer function independent of the excitation?

docs/implementation.md §4 claims a finished solve can be re-weighted by any driver
spectrum instead of re-solved (§8). That holds if, and only if,

    T(f) = |H_dump(f)| / |I_port(f)|

is the same whatever source produced it. FDTD is linear by construction, so what is
actually being tested here is *our pipeline*: the Gaussian excitation, the direct DFT at
named frequencies, and the port probes. Spectral leakage or a poor signal-to-noise ratio
at a band edge would break re-weighting even though the physics is linear.

This runs one model three times with deliberately different Gaussian excitations and
compares T(f) at identical dump frequencies.

    docker run --rm -v "$PWD/worker:/spike" -w /spike -e PYTHONPATH=/spike \
        --entrypoint python3 embeddedci/emi-worker:dev scripts/spike_m0_linearity.py
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np

from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.normalize import _board_extent
from emi_worker.openems import post, run
from emi_worker.openems.model import Port, SolveParams, build_model

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "tiny.kicad_pcb"
OUT = Path("/spike/spike_out")

#: The frequencies every variant records, and where T(f) is compared.
FREQS = [300e6, 500e6, 700e6, 1e9]

#: (name, f0, fc). A is what the solver would choose itself; B pushes the source band well
#: above the dump frequencies and C well below, so the energy each one delivers at a given
#: dump frequency differs by a lot. If T(f) survives that, re-weighting is sound.
VARIANTS = [
    ("A_default", None, None),
    ("B_high", 1.5e9, 0.9e9),
    ("C_low", 200e6, 200e6),
    # Same centre as A, but wide enough that 1 GHz is inside the band rather than on its
    # edge. If D agrees with B at 1 GHz while A does not, the residual is A's band edge.
    ("D_wide", 650e6, 600e6),
]


def build(params: SolveParams):
    board = parse_board(parse(FIXTURE.read_text()))
    return build_model(board, _board_extent(board), params)


def measure(workdir: Path, built, port: str) -> dict:
    """Mean |H| per dump frequency, and the port current spectrum."""
    fields: dict[str, list[float]] = {}
    for layer, dump in built.dump_names.items():
        path = workdir / f"{dump}.h5"
        if not path.exists():
            continue
        grids = post.read_fd_dump(str(path))
        fields[layer] = [float(np.mean(g.magnitude)) for g in grids]

    i_probe = post.read_probe(str(workdir / f"{port}_it"))
    u_probe = post.read_probe(str(workdir / f"{port}_ut"))
    i_spec = np.abs(post._dft(i_probe, np.asarray(FREQS)))
    u_spec = np.abs(post._dft(u_probe, np.asarray(FREQS)))
    return {"fields": fields, "i": i_spec.tolist(), "u": u_spec.tolist()}


def main() -> None:
    params = SolveParams(
        roi=(4.0, 24.0, 30.0, 38.0),
        frequencies_hz=FREQS,
        ports=[Port(name="p1", x=10.0, y=30.0, layer="F.Cu", half_width_mm=0.15)],
        dx_um=250, dy_um=250, dz_um=250, air_mm=3.0,
    )

    # One build decides the mesh and the timestep count; every variant reuses them, so the
    # runs differ only in their source. A different record length would change the DFT.
    reference = build(params)
    steps = reference.doc.max_timesteps
    print(f"mesh: {reference.mesh.cells:,} cells, {steps:,} timesteps, "
          f"dt {reference.mesh.timestep_seconds():.3e} s", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    only = [v for v in os.environ.get("VARIANTS", "").split(",") if v]
    results = {}
    for name, f0, fc in VARIANTS:
        if only and name not in only:
            continue
        built = build(params)
        if f0 is not None:
            built.doc.excitation.f0 = f0
            built.doc.excitation.fc = fc
        built.doc.max_timesteps = steps
        # Run every variant to the same length: an early energy cutoff would truncate one
        # record and not another, which is a DFT difference, not a physics one.
        built.doc.end_criteria = float(os.environ.get("END_CRITERIA", "0.0"))

        wd = OUT / name
        if wd.exists():
            shutil.rmtree(wd)
        wd.mkdir(parents=True)
        xml = wd / "model.xml"
        xml.write_text(built.doc.to_string())

        print(f"--- {name}: f0={built.doc.excitation.f0:.3e} fc={built.doc.excitation.fc:.3e}",
              flush=True)
        r = run.run_openems(str(xml), str(wd), threads=int(os.environ.get("THREADS", "4")))
        print(f"    {r.final_timestep:,} steps, {r.elapsed_s:.0f}s, "
              f"energy {r.final_energy_db:.1f} dB", flush=True)
        results[name] = measure(wd, built, "p1")

    # ---- compare ----
    layers = sorted(results["A_default"]["fields"])
    report = {"frequencies_hz": FREQS, "variants": results, "comparison": {}}
    print("\nT(f) = mean|H| / |I_port|, in dB relative to variant A\n")
    header = "  layer        f (MHz)   |I| ratio dB    T ratio dB"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for layer in layers:
        for k, f in enumerate(FREQS):
            a = results["A_default"]
            ta = a["fields"][layer][k] / a["i"][k] if a["i"][k] else float("nan")
            for name in [n for n in results if n != "A_default"]:
                b = results[name]
                tb = b["fields"][layer][k] / b["i"][k] if b["i"][k] else float("nan")
                i_db = 20 * np.log10(b["i"][k] / a["i"][k]) if a["i"][k] and b["i"][k] else float("nan")
                t_db = 20 * np.log10(tb / ta) if ta and tb else float("nan")
                report["comparison"].setdefault(layer, {}).setdefault(name, []).append(
                    {"frequency_hz": f, "source_ratio_db": i_db, "transfer_ratio_db": t_db}
                )
                print(f"  {layer:<12} {f/1e6:6.0f}   {name:>7} {i_db:+8.1f}   {t_db:+8.2f}")

    (OUT / "m0_linearity.json").write_text(json.dumps(report, indent=2))
    print(f"\nwrote {OUT / 'm0_linearity.json'}")


if __name__ == "__main__":
    main()
