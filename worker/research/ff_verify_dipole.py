"""Far-field verification: dipoles through the production path, against theory and nec2c.

Each case is a wire dipole built with ``ff_harness`` (the production port, source, record,
box and transform), solved by openEMS in free space, and read at the scan's antenna positions
with ``scan.field`` and the ground plane 0.8 m below as an image -- exactly what
``stages.solve`` does for a board. The same wire, 0.8 m over a perfect ground, goes through
nec2c with a near-field card at every one of those positions.

Compared per amp of feed current. The FDTD dipole is solved without the plane, the NEC one
with it, so their input impedances differ by the plane's coupling; per amp takes that out and
leaves the field transform, which is what is being checked. The coupling itself is printed
(NEC's Z_in over ground against free space) because the product makes the same approximation.

Also: directivity from a full-sphere nf2ff transform (1.76 dBi short, 2.15 dBi half-wave),
the scan level from the old far-field sphere for comparison, and slopes per volt.

    docker run --rm -v "$PWD/worker:/w:ro" -v "$PWD/out:/out" -w /w -e PYTHONPATH=/w \\
        -e OUT=/out --entrypoint python3 <worker image> research/ff_verify_dipole.py

``CASES`` picks cases (comma separated, default all); ``THREADS`` defaults to 3; ``END`` is
openEMS's end criterion (default 1e-4, the production one); ``REFINE`` divides both the finest
and the coarsest cell, to see which differences are the FDTD mesh's; ``F_HI`` moves the top of
the excitation band and the mesh above 1 GHz, to see what the band edge costs. Writes
``$OUT/ff_verify_dipole.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ff_harness as H  # noqa: E402

from emi_worker.cables.nec import NEC2C, parse_output  # noqa: E402
from emi_worker.openems import scan  # noqa: E402
from emi_worker.openems.mesh import max_cell_for_frequency  # noqa: E402
from emi_worker.openems.nf2ff import TABLE_HEIGHT_M  # noqa: E402

OUT = Path(os.environ.get("OUT", "/out"))

#: name: (length mm, square side mm, axis, band Hz, frequencies)
CASES = {
    # Electrically short everywhere below 1 GHz (0.13 lambda at the top).
    "short-vertical": (40.0, 1.0, 2, None),
    "short-horizontal": (40.0, 1.0, 0, None),
    # Half-wave near 280 MHz. Above about 500 MHz it approaches its full-wave antiresonance,
    # where the feed current collapses and a per-amp figure is a ratio of two small numbers
    # that depends on exactly where each solver puts the antiresonance; the band stops there.
    "halfwave-horizontal": (500.0, 5.0, 0, (30e6, 450e6)),
    "halfwave-vertical": (500.0, 5.0, 2, (30e6, 450e6)),
}

FREQS = [30e6, 40e6, 60e6, 80e6, 100e6, 150e6, 200e6, 250e6, 280e6, 300e6, 350e6, 400e6,
         450e6, 500e6, 600e6, 700e6, 850e6, 1000e6]

#: The equivalent radius of a square conductor of side s is 0.59 s (Balanis, Antenna Theory,
#: §9.7). It only moves the current distribution slightly; per amp is barely sensitive to it.
SQUARE_TO_RADIUS = 0.59


def nec_deck(length_m, radius_m, axis, f, points, ground: bool) -> str:
    """A centre-fed dipole 0.8 m over a perfect plane (or in free space), E at ``points``."""
    lam = 299_792_458.0 / f
    seg = max(9, int(np.ceil(20 * length_m / lam)))
    seg += 1 - seg % 2
    a, b = [0.0, 0.0, TABLE_HEIGHT_M], [0.0, 0.0, TABLE_HEIGHT_M]
    a[axis] -= length_m / 2
    b[axis] += length_m / 2
    lines = ["CM far-field verification dipole", "CE",
             f"GW 1 {seg} {a[0]:.6f} {a[1]:.6f} {a[2]:.6f} {b[0]:.6f} {b[1]:.6f} {b[2]:.6f} "
             f"{radius_m:.6f}",
             "GE 1" if ground else "GE 0"]
    if ground:
        lines.append("GN 1")
    lines.append(f"EX 0 1 {seg // 2 + 1} 0 1.0 0.0")
    lines.append(f"FR 0 1 0 0 {f / 1e6:.6f} 0")
    for x, y, z in points:
        lines.append(f"NE 0 1 1 1 {x:.6f} {y:.6f} {z:.6f} 0 0 0")
    lines += ["XQ 0", "EN"]
    return "\n".join(lines) + "\n"


def run_nec(deck: str, f: float):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "d.nec").write_text(deck)
        subprocess.run([NEC2C, f"-i{tmp}/d.nec", f"-o{tmp}/d.out"], check=True,
                       capture_output=True, timeout=600)
        return parse_output((Path(tmp) / "d.out").read_text(), f)


def old_sphere(res: dict) -> np.ndarray:
    return np.asarray(res["e_max_v_per_m"])


def case(name: str) -> dict:
    length, side, axis, band = CASES[name]
    freqs = [f for f in FREQS if band is None or band[0] <= f <= band[1]]
    wire = H.dipole(length, side, axis=axis)
    end = float(os.environ.get("END", "1e-4"))
    refine = float(os.environ.get("REFINE", "1"))
    f_hi = float(os.environ.get("F_HI", "1e9"))
    setup = H.Setup(wire=wire, frequencies=FREQS, fine_mm=side / 2 / refine, end_criteria=end,
                    f_hi=f_hi,
                    max_cell_mm=(max_cell_for_frequency(1e9, 1.0) / refine) if refine != 1
                    else None)
    wd = OUT / ("dipole" + (f"_end{end:g}" if end != 1e-4 else "")
                + (f"_refine{refine:g}" if refine != 1 else "")
                + (f"_fhi{f_hi:g}" if f_hi != 1e9 else "")) / name
    meta = H.build(setup, wd)
    info = H.solve(wd)
    print(f"\n== {name}: {meta['cells']:,} cells, box clearance {meta['clearance_mm']:.0f} mm, "
          f"{info['timesteps']:,} steps, {info['elapsed_s']:.0f} s, energy "
          f"{info['energy_db']} dB", flush=True)
    src = H.source(wd, meta)
    f_all = np.asarray(meta["frequencies_hz"])
    idx = [int(np.argmin(np.abs(f_all - f))) for f in freqs]

    surf = scan.read_surface(str(wd), list(f_all))
    # The product's scan around this "product": its boundary in plan is the wire's half-length
    # when horizontal, its thickness when vertical.
    half_plan = length / 2000.0 if axis != 2 else side / 2000.0
    ground = -TABLE_HEIGHT_M
    pts = scan.ring((0.0, 0.0), scan.SCAN_DISTANCE_M + half_plan, ground)
    e = scan.field(surf, list(f_all), pts, ground_z_m=ground)
    rd = scan.reading(e)                                     # (F, P)
    nh = len(scan.SCAN_HEIGHTS_M)
    by_h = rd.reshape(len(f_all), nh, -1).max(axis=2)       # (F, heights)

    # The old product path, for the record: the far-field sphere with the nf2ff mirror.
    old = old_sphere(H.far_field(wd, meta, mirror_z_m=ground, tag="ff_old"))

    # Directivity, free space, full sphere.
    th = np.arange(0.0, 180.1, 3.0)
    ph = np.arange(0.0, 360.0, 6.0)
    sph = H.far_field(wd, meta, mirror_z_m=None, theta_deg=th, phi_deg=ph, tag="ff_sphere")
    d = H.directivity(sph, th, ph)

    i_abs = np.abs(src["i_port"])
    nec_pts = [(x, y, z - ground) for x, y, z in pts]
    rows = []
    for k in idx:
        f = f_all[k]
        rn = run_nec(nec_deck(length / 1000, SQUARE_TO_RADIUS * side / 1000, axis, f,
                              nec_pts, True), f)
        rfs = run_nec(nec_deck(length / 1000, SQUARE_TO_RADIUS * side / 1000, axis, f,
                               nec_pts[:1], False), f)
        nec_i = abs(1.0 / rn.z_in)
        nec_h = np.asarray(rn.e_v_per_m).reshape(nh, -1).max(axis=1) / nec_i
        ours_h = by_h[k] / i_abs[k]
        diff_h = 20 * np.log10(ours_h / nec_h)
        row = {
            "f_hz": f,
            "ours_scan_v_per_m_per_a": float(ours_h.max()),
            "nec_scan_v_per_m_per_a": float(nec_h.max()),
            "scan_diff_db": float(20 * np.log10(ours_h.max() / nec_h.max())),
            "height_diff_db_max_abs": float(np.abs(diff_h).max()),
            "old_sphere_vs_nec_db": float(20 * np.log10(old[k] / i_abs[k] / nec_h.max())),
            "directivity_dbi": float(d[k]),
            "z_in_fdtd": [float(src["z_in"][k].real), float(src["z_in"][k].imag)],
            "z_in_nec_free": [rfs.z_in.real, rfs.z_in.imag],
            "z_in_nec_ground": [rn.z_in.real, rn.z_in.imag],
            "e_per_volt_scan": float(by_h[k].max() / abs(src["v_src"][k])),
        }
        rows.append(row)
        print(f"  {f / 1e6:6.0f} MHz  scan/NEC {row['scan_diff_db']:+6.2f} dB  worst height "
              f"{row['height_diff_db_max_abs']:5.2f} dB  old sphere/NEC "
              f"{row['old_sphere_vs_nec_db']:+6.2f} dB  D {d[k]:5.2f} dBi  Zin FDTD "
              f"{src['z_in'][k].real:8.1f}{src['z_in'][k].imag:+9.1f}j  NEC free "
              f"{rfs.z_in.real:8.1f}{rfs.z_in.imag:+9.1f}j  ground "
              f"{rn.z_in.real:8.1f}{rn.z_in.imag:+9.1f}j", flush=True)
    ev = np.array([r["e_per_volt_scan"] for r in rows])
    fs = np.array([r["f_hz"] for r in rows])
    out = {"case": name, "length_mm": length, "side_mm": side, "axis": "xyz"[axis],
           "cells": meta["cells"], "clearance_mm": meta["clearance_mm"], "run": info,
           "rows": rows,
           "slope_e_per_volt_db_per_decade": H.slope_db_per_decade(fs, ev)}
    print(f"  per-volt slope {out['slope_e_per_volt_db_per_decade']:.1f} dB/decade")
    return out


def main() -> None:
    names = [n for n in os.environ.get("CASES", ",".join(CASES)).split(",") if n]
    results = {}
    end = float(os.environ.get("END", "1e-4"))
    refine = float(os.environ.get("REFINE", "1"))
    path = OUT / ("ff_verify_dipole" + (f"_end{end:g}" if end != 1e-4 else "")
                  + (f"_refine{refine:g}" if refine != 1 else "")
                  + (f"_fhi{float(os.environ.get('F_HI', '1e9')):g}"
                     if float(os.environ.get("F_HI", "1e9")) != 1e9 else "") + ".json")
    if path.exists():
        results = json.loads(path.read_text())
    for n in names:
        results[n] = case(n)
        path.write_text(json.dumps(results, indent=1))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
