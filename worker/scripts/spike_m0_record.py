"""M0 · D2b — was each recorded run long enough to transform?

Re-weighting a stored solve assumes its record is complete: the structure has rung down, so
the DFT of what was captured is the DFT of the whole response. An early energy cutoff can
end a run while one narrow resonance is still ringing, and that truncation shows up as an
error at exactly that frequency.

This needs no new solve. It transforms each port-current probe over the full record and over
the first 75 %, and reports the difference per frequency. A complete record barely moves.

    python3 worker/scripts/spike_m0_record.py <spike_out dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emi_worker.openems import post  # noqa: E402

FREQS = np.array([300e6, 500e6, 700e6, 1e9])


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    print("record completeness — |I(f)| over the full record vs the first 75 %\n")
    print("  variant        f (MHz)    |I| full      delta dB")
    print("  " + "-" * 48)
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        probe = run_dir / "p1_it"
        if not probe.exists():
            continue
        tr = post.read_probe(str(probe))
        cut = int(len(tr.time_s) * 0.75)
        short = post.ProbeTrace(time_s=tr.time_s[:cut], values=tr.values[:cut])
        full_spec = np.abs(post._dft(tr, FREQS))
        short_spec = np.abs(post._dft(short, FREQS))
        for k, f in enumerate(FREQS):
            d = 20 * np.log10(short_spec[k] / full_spec[k]) if full_spec[k] else float("nan")
            flag = "" if abs(d) < 0.5 else "   <-- still ringing at cutoff"
            print(f"  {run_dir.name:<12} {f/1e6:6.0f}   {full_spec[k]:.3e}   {d:+8.2f}{flag}")
        print()


if __name__ == "__main__":
    main()
