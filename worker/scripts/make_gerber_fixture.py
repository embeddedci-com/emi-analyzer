#!/usr/bin/env python3
"""Generate the Gerber test fixture from tiny.kicad_pcb.

Requires kicad-cli on PATH. The fixture is committed, so this only needs re-running when
the source board changes — but keeping the generator alongside it means the two can never
silently describe different boards.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "tests" / "fixtures"
SRC = FIXTURES / "tiny.kicad_pcb"
OUT = FIXTURES / "gerber"


def main() -> int:
    if not shutil.which("kicad-cli"):
        print("kicad-cli is not on PATH; install KiCad to regenerate the fixture")
        return 1
    if not SRC.exists():
        print(f"missing {SRC}")
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    run = lambda *a: subprocess.run(a, check=True, capture_output=True, text=True)
    run("kicad-cli", "pcb", "export", "gerbers", "--output", f"{OUT}/",
        "--layers", "F.Cu,B.Cu,Edge.Cuts", "--no-protel-ext", str(SRC))
    run("kicad-cli", "pcb", "export", "drill", "--output", f"{OUT}/",
        "--format", "excellon", "--excellon-units", "mm", str(SRC))
    run("kicad-cli", "pcb", "export", "ipcd356",
        "--output", str(OUT / "tiny.d356"), str(SRC))

    for f in sorted(OUT.iterdir()):
        print(f"  {f.name:34} {f.stat().st_size:>8,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
