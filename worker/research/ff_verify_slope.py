"""Far-field verification: the per-volt slope of a small loop and a short dipole.

The fixture board's far field per volt rose about 20 dB/decade where an electrically small
radiator fed from a voltage should rise at least 40. This drives two structures whose answer is
closed form through the production port, source, record, box and ``scan.field``, and compares
per volt of source against theory:

* **A small square loop** fed through the 50 ohm port. Its field is a magnetic dipole's,
  E = eta k^2 I A / (4 pi r) |1 + 1/(jkr)| broadside, and its current is V / (50 + jwL) with L
  from the closed form for a square loop, so the per-volt level is predicted with nothing taken
  from the solve. It rises 40 dB/decade until wL passes 50 ohm (about 180 MHz here) and 20 above.
* **A short dipole**, capacitive: I = jwC V, so E/V rises 40 dB/decade across the band. Its
  capacitance has no simple closed form, so the level is fitted and the slope is the check.

Both are read in free space, broadside at 3 m, so nothing but the port and the transform is
involved. Writes ``$OUT/ff_verify_slope.json``.

    docker run --rm -v "$PWD/worker:/w:ro" -v "$PWD/out:/out" -w /w -e PYTHONPATH=/w \\
        -e OUT=/out --entrypoint python3 <worker image> research/ff_verify_slope.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ff_harness as H  # noqa: E402

from emi_worker.openems import scan  # noqa: E402

OUT = Path(os.environ.get("OUT", "/out"))
MU0 = 4e-7 * math.pi
FREQS = [30e6, 40e6, 60e6, 80e6, 100e6, 150e6, 200e6, 300e6, 400e6, 500e6, 700e6, 1000e6]

LOOP_SIDE_MM = 20.0
WIRE_MM = 1.0
#: Equivalent radius of a square conductor (Balanis §9.7).
RADIUS_MM = 0.59 * WIRE_MM


def square_loop_inductance(side_m: float, radius_m: float) -> float:
    """Grover's square loop: L = (2 mu0 a / pi) (ln(a / r) - 0.774)."""
    return 2 * MU0 * side_m / math.pi * (math.log(side_m / radius_m) - 0.774)


def run(name: str, wire: H.Wire):
    wd = OUT / "slope" / name
    meta = H.build(H.Setup(wire=wire, frequencies=FREQS, fine_mm=WIRE_MM / 2), wd)
    info = H.solve(wd)
    src = H.source(wd, meta)
    surf = scan.read_surface(str(wd), meta["frequencies_hz"])
    return meta, info, src, surf


def fit_slope(f, y):
    return float(np.polyfit(np.log10(f), 20 * np.log10(y), 1)[0])


def main() -> None:
    out = {}
    f = np.asarray(FREQS)
    k = 2 * np.pi * f / scan.C0
    r = 3.0

    # -- loop, in the xy plane: broadside is anywhere in that plane --------------------------
    meta, info, src, surf = run("loop", H.loop(LOOP_SIDE_MM, WIRE_MM))
    e = np.linalg.norm(scan.field(surf, FREQS, np.array([[3.0, 0.0, 0.0]]))[:, 0], axis=1)
    ev = e / np.abs(src["v_src"])
    ei = e / np.abs(src["i_port"])
    area = (LOOP_SIDE_MM / 1000) ** 2
    ind = square_loop_inductance(LOOP_SIDE_MM / 1000, RADIUS_MM / 1000)
    near = np.abs(1 + 1 / (1j * k * r))
    ei_th = scan.ETA0 * k ** 2 * area / (4 * np.pi * r) * near
    z_th = 50.0 + 1j * 2 * np.pi * f * ind
    ev_th = ei_th / np.abs(z_th)
    print(f"loop {LOOP_SIDE_MM:g} mm: {meta['cells']:,} cells, {info['timesteps']:,} steps; "
          f"L closed form {ind * 1e9:.1f} nH")
    rows = []
    for j in range(len(f)):
        z = src["z_in"][j]
        rows.append({"f_hz": f[j], "e_per_volt": ev[j], "theory_per_volt": ev_th[j],
                     "per_volt_db": 20 * math.log10(ev[j] / ev_th[j]),
                     "per_amp_db": 20 * math.log10(ei[j] / ei_th[j]),
                     "z_in": [z.real, z.imag],
                     "l_from_z_nh": z.imag / (2 * np.pi * f[j]) * 1e9})
        print(f"  {f[j] / 1e6:6.0f} MHz  E/V {ev[j]:.3e} theory {ev_th[j]:.3e} "
              f"({rows[-1]['per_volt_db']:+.2f} dB)  E/I vs theory {rows[-1]['per_amp_db']:+.2f}"
              f" dB  Zin {z.real:7.2f}{z.imag:+8.1f}j  (L {rows[-1]['l_from_z_nh']:.1f} nH)")
    lo = f <= 100e6
    hi = f >= 400e6
    out["loop"] = {"rows": rows, "inductance_closed_form_nh": ind * 1e9,
                   "slope_below_100mhz": fit_slope(f[lo], ev[lo]),
                   "theory_slope_below_100mhz": fit_slope(f[lo], ev_th[lo]),
                   "slope_above_400mhz": fit_slope(f[hi], ev[hi]),
                   "theory_slope_above_400mhz": fit_slope(f[hi], ev_th[hi])}
    print(f"  slope per volt below 100 MHz {out['loop']['slope_below_100mhz']:.1f} "
          f"(theory {out['loop']['theory_slope_below_100mhz']:.1f}), above 400 MHz "
          f"{out['loop']['slope_above_400mhz']:.1f} "
          f"(theory {out['loop']['theory_slope_above_400mhz']:.1f}) dB/decade")

    # -- short dipole along z, read broadside -------------------------------------------------
    meta, info, src, surf = run("dipole", H.dipole(40.0, WIRE_MM, axis=2))
    e = np.linalg.norm(scan.field(surf, FREQS, np.array([[3.0, 0.0, 0.0]]))[:, 0], axis=1)
    ev = e / np.abs(src["v_src"])
    # Capacitive: E/V = K f^2 |near-field factor of an electric dipole| for |Z_in| >> 50 ohm.
    shape = f ** 2 * np.abs(1 - 1j / (k * r) - 1 / (k * r) ** 2)
    kfit = np.exp(np.mean(np.log(ev / shape)))
    resid = 20 * np.log10(ev / (kfit * shape))
    print(f"short dipole 40 mm: {info['timesteps']:,} steps")
    for j in range(len(f)):
        print(f"  {f[j] / 1e6:6.0f} MHz  E/V {ev[j]:.3e}  vs K f^2 {resid[j]:+.2f} dB  Zin "
              f"{src['z_in'][j].real:7.1f}{src['z_in'][j].imag:+9.1f}j")
    out["dipole"] = {"slope": fit_slope(f, ev), "theory_slope": fit_slope(f, shape),
                     "max_residual_db": float(np.abs(resid).max()),
                     "rows": [{"f_hz": f[j], "e_per_volt": ev[j], "residual_db": resid[j]}
                              for j in range(len(f))]}
    print(f"  slope per volt {out['dipole']['slope']:.1f} (theory "
          f"{out['dipole']['theory_slope']:.1f}) dB/decade, worst residual from K f^2 "
          f"{out['dipole']['max_residual_db']:.2f} dB")

    (OUT / "ff_verify_slope.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
