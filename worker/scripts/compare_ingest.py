#!/usr/bin/env python3
"""Compare the Gerber path against the KiCad path for one board.

The committed fixture is small and hand-written, which makes it good for exercising
mechanics and poor for proving a clean reconstruction. This runs the same comparison
against a real board: export its Gerbers with kicad-cli, read them back, and check that
both paths describe the same thing.

Routed length per net is the number that matters. It moves if the geometry is wrong *or*
if the nets are attached to the wrong copper, so agreement means both are right — which
counting nets and tracks cannot tell you.

    python3 scripts/compare_ingest.py path/to/board.kicad_pcb
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emi_worker.gerber import load_gerber_board  # noqa: E402
from emi_worker.kicad import parse, parse_board  # noqa: E402
from emi_worker.kicad.normalize import normalize  # noqa: E402


def export(src: Path, out: Path) -> dict[str, bytes]:
    board = parse_board(parse(src.read_text(encoding="utf-8", errors="replace")))
    layers = ",".join(board.copper_layer_names + ["Edge.Cuts"])
    run = lambda *a: subprocess.run(a, check=True, capture_output=True, text=True)
    run("kicad-cli", "pcb", "export", "gerbers", "--output", f"{out}/",
        "--layers", layers, "--no-protel-ext", str(src))
    run("kicad-cli", "pcb", "export", "drill", "--output", f"{out}/",
        "--format", "excellon", "--excellon-units", "mm", str(src))
    run("kicad-cli", "pcb", "export", "ipcd356",
        "--output", str(out / "netlist.d356"), str(src))
    return {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    if not shutil.which("kicad-cli"):
        print("kicad-cli is not on PATH")
        return 1

    src = Path(sys.argv[1])
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    with tempfile.TemporaryDirectory() as tmp:
        files = export(src, Path(tmp))
        gerber = load_gerber_board(files)

    kicad = parse_board(parse(src.read_text(encoding="utf-8", errors="replace")))
    gdoc, _ = normalize(gerber, {})
    kdoc, _ = normalize(kicad, {})

    print(f"\n{src.name}")
    print(f"  layers   gerber {gerber.copper_layer_names}")
    print(f"           kicad  {kicad.copper_layer_names}")
    print(f"  board    gerber {gdoc['board']['width_mm']} x {gdoc['board']['height_mm']} mm"
          f"   kicad {kdoc['board']['width_mm']} x {kdoc['board']['height_mm']} mm")
    named = sum(1 for t in gerber.tracks if t.net)
    print(f"  tracks   {named}/{len(gerber.tracks)} named on the gerber path")
    conflicts = [w for w in gerber.warnings if "resolve to the same" in w]
    print(f"  shorts   {len(conflicts)} reported")

    def lengths(doc):
        return {n["name"].upper()[-12:]: n["length_mm"]
                for n in doc["nets"] if n["length_mm"] > 1.0}

    g, k = lengths(gdoc), lengths(kdoc)
    shared = sorted(set(g) & set(k), key=lambda n: -k[n])
    print(f"\n  {'net':16} {'gerber':>9} {'kicad':>9}   delta")
    bad = 0
    for name in shared[:20]:
        delta = abs(g[name] - k[name]) / max(k[name], 1e-9)
        flag = "" if delta < 0.05 else "  <-- differs"
        if delta >= 0.05:
            bad += 1
        print(f"  {name:16} {g[name]:9.1f} {k[name]:9.1f} {100*delta:6.1f}%{flag}")

    print(f"\n  {len(shared)} nets in common, {bad} differing by more than 5%")
    return 0 if bad == 0 and named == len(gerber.tracks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
