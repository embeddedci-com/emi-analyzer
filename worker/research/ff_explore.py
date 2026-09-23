"""Exploration: which knob moves a short dipole's far field, read the old way (nf2ff sphere).

Kept because the face-sampling numbers in docs/verification/far-field.md came from it:
``res=5`` against ``res=0.01`` (every grid line) is the 0.03 dB. ``CASE`` names the output
folder; ``clear``, ``res`` and ``end`` override the box clearance (mm), the face resolution (mm)
and openEMS's end criterion; ``SHAPE=loop`` swaps the dipole for a loop.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ff_harness as H  # noqa: E402

OUT = Path(os.environ.get("OUT", "/out"))

CASE = os.environ.get("CASE", "base")
L = float(os.environ.get("L_MM", "40"))
W = float(os.environ.get("W_MM", "1"))
FREQS = [30e6, 50e6, 100e6, 200e6, 300e6, 500e6, 700e6, 1e9]


def main():
    kw = {}
    if "clear" in os.environ:
        kw["clearance_mm"] = float(os.environ["clear"])
    if "res" in os.environ:
        kw["resolution_mm"] = float(os.environ["res"])
    if "end" in os.environ:
        kw["end_criteria"] = float(os.environ["end"])
    shape = os.environ.get("SHAPE", "dipole")
    wire = H.dipole(L, W, axis=int(os.environ.get("AXIS", "2"))) if shape == "dipole" \
        else H.loop(L, W)
    setup = H.Setup(wire=wire, frequencies=FREQS, fine_mm=W / 2, **kw)
    wd = OUT / CASE
    meta = H.build(setup, wd)
    print(CASE, json.dumps({k: meta[k] for k in ("cells", "lines", "clearance_mm", "faces_mm",
                                                   "max_timesteps", "f0", "fc")}), flush=True)
    info = H.solve(wd)
    print(" run", info, flush=True)
    src = H.source(wd, meta)
    f = np.asarray(meta["frequencies_hz"])
    for mirror in (None, -0.8):
        res = H.far_field(wd, meta, mirror_z_m=mirror, tag=f"ff_{mirror}")
        e = np.asarray(res["e_max_v_per_m"])
        ev = e / np.abs(src["v_src"])
        ei = e / np.abs(src["i_port"])
        print(f" mirror={mirror}: slope E/V {H.slope_db_per_decade(f, ev):.1f} dB/dec, "
              f"E/I {H.slope_db_per_decade(f, ei):.1f} dB/dec")
        for k in range(len(f)):
            z = src["z_in"][k]
            print(f"  {f[k]/1e6:7.1f} MHz  E/V {ev[k]:.3e}  E/I {ei[k]:.3e}  "
                  f"Zin {z.real:9.2f}{z.imag:+11.1f}j  |Vsrc| {abs(src['v_src'][k]):.3e}")
    # Full sphere, free space: directivity.
    th = np.arange(0.0, 180.1, 5.0)
    ph = np.arange(0.0, 360.0, 10.0)
    res = H.far_field(wd, meta, mirror_z_m=None, theta_deg=th, phi_deg=ph, tag="ff_sphere")
    d = H.directivity(res, th, ph)
    print(" directivity dBi:", " ".join(f"{v:.2f}" for v in d))


if __name__ == "__main__":
    main()
