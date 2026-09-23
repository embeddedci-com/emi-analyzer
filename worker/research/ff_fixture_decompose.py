"""Far-field verification: which fault flattened the fixture board's slope, and by how much.

Reads one finished fixture solve (``ff_fixture_explore.py``) and re-reads its box four ways, so
each fault's share is measured rather than argued:

  nf2ff sphere + nf2ff mirror    what production published before (version 2)
  nf2ff sphere, no mirror        the transform alone, free space
  scan + image currents          what production publishes now (version 3)
  scan, no image                 the same, free space

Run it on a solve made with the old stride-sampled (open) box and on one made with the closed
box to separate the box from the mirror. Pass the solve's run folder as ``RUN`` (the folder
holding the nf2ff_*.h5 dumps and p1_ut/p1_it).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emi_worker.openems import nf2ff, post, scan  # noqa: E402


def slope(f, y, below=200e6):
    lo = f < below
    return float(np.polyfit(np.log10(f[lo]), 20 * np.log10(y[lo]), 1)[0])


def main() -> None:
    wd = Path(os.environ["RUN"])
    ff = json.loads((wd.parents[1] / "farfield.json").read_text())
    freqs = ff["frequencies_hz"]
    f = np.asarray(freqs)
    v = np.abs(post.source_spectrum(str(wd), "p1", 50.0, freqs)["v_src"])
    box = ff["faces_mm"]
    centre = ((box[0] + box[3]) / 2, (box[1] + box[4]) / 2, (box[2] + box[5]) / 2)
    out = {}
    for mirror in (-nf2ff.TABLE_HEIGHT_M, None):
        h5 = wd / f"decompose_{mirror}.h5"
        job = nf2ff.write_job(str(wd), freqs, str(h5), centre_mm=centre, radius_m=3.0,
                              mirror_z_m=mirror, lower_face_z_m=box[2] / 1000)
        nf2ff.run_job(job, str(h5))
        e = np.asarray(nf2ff.read_field(str(h5), freqs)["e_max_v_per_m"]) / v
        out[f"nf2ff sphere, mirror {mirror}"] = e
    try:
        s = scan.read_surface(str(wd), freqs)
        closed = True
    except scan.ScanError as exc:
        print("box:", exc)
        closed = False
        src = open(scan.__file__).read().replace(
            'raise ScanError(\n                    f"the far-field box is open',
            'print(\n                    f"(open)')
        ns: dict = {}
        exec(compile(src, "scan_unchecked", "exec"), ns)
        s = ns["read_surface"](str(wd), freqs)
    copper = [box[0] + ff["clearance_mm"], box[1] + ff["clearance_mm"],
              box[3] - ff["clearance_mm"], box[4] - ff["clearance_mm"]]
    x0, y0, x1, y1 = (c / 1000 for c in copper)
    ground = box[2] / 1000 + ff["clearance_mm"] / 1000 - nf2ff.TABLE_HEIGHT_M
    pts = scan.ring(((x0 + x1) / 2, (y0 + y1) / 2), 3.0 + 0.5 * np.hypot(x1 - x0, y1 - y0),
                    ground)
    for g in (ground, None):
        e = scan.reading(scan.field(s, freqs, pts, ground_z_m=g)).max(axis=1) / v
        out[f"scan, {'image' if g is not None else 'free space'}"] = e
    print(f"box closed: {closed}")
    for name, e in out.items():
        print(f"  {name:32s} slope below 200 MHz {slope(f, e):5.1f} dB/dec, overall "
              f"{slope(f, e, 2e9):5.1f};  30 MHz {e[0]:.3e}  500 MHz "
              f"{e[int(np.argmin(np.abs(f - 5e8)))]:.3e} V/m/V")
    (wd / "decompose.json").write_text(json.dumps(
        {"closed": closed, "frequencies_hz": freqs,
         **{k: [float(x) for x in e] for k, e in out.items()}}, indent=1))


if __name__ == "__main__":
    main()
