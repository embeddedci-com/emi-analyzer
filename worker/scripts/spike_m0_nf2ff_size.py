"""M0 — how big is an NF2FF recording at 60 frequencies?

M4 needs E and H on the six faces of a box around the region, in the frequency domain, at
enough frequencies to interpolate a spectrum. openEMS writes those as HDF5 dumps, and the
worry in §16.2 is that a fine board mesh makes them enormous. This sizes them from the real
mesher rather than guessing.

Per face, per frequency: cells * 3 components * 2 (real, imaginary) * 4 bytes, for E and
again for H.

    docker run --rm -v "$PWD/worker:/spike" -w /spike -e PYTHONPATH=/spike \\
        --entrypoint python3 embeddedci/emi-worker:dev scripts/spike_m0_nf2ff_size.py
"""

from __future__ import annotations

from emi_worker.openems.mesh import MeshSpec, build_mesh

BYTES_PER_CELL_PER_FREQ = 3 * 2 * 4 * 2  # xyz, re+im, float32, E and H

PRESETS = {"coarse": (150, 150, 100), "normal": (75, 75, 50), "fine": (50, 50, 25)}


def faces(nx: int, ny: int, nz: int) -> int:
    """Cells on the six faces of the box."""
    return 2 * (nx * ny) + 2 * (nx * nz) + 2 * (ny * nz)


def main() -> None:
    roi = (0.0, 0.0, 20.0, 20.0)
    print("NF2FF box around a 20 x 20 mm region, 60 frequencies\n")
    print("  preset    grid (x,y,z)        face cells   sub   per freq     60 freqs")
    print("  " + "-" * 68)
    for name, (dx, dy, dz) in PRESETS.items():
        spec = MeshSpec(
            roi=roi, f_max=1e9, dx_um=dx, dy_um=dy, dz_um=dz,
            air_above_mm=5.0, air_below_mm=5.0,
        )
        # Copper features are what force extra lines; a bare region is the floor, so add a
        # feature every 0.5 mm to stand in for real routing.
        feats = [i * 0.5 for i in range(1, 40)]
        m = build_mesh(spec, feats, feats, [0.0175, 0.5, 1.1, 1.5825])
        nx, ny, nz = len(m.x), len(m.y), len(m.z)
        for sub in (1, 2, 4):
            cells = faces(nx // sub, ny // sub, nz // sub)
            per = cells * BYTES_PER_CELL_PER_FREQ
            print(f"  {name:<8}  {nx:4d} x {ny:4d} x {nz:3d}   {cells:10,}   1/{sub}"
                  f"   {per / 1e6:7.1f} MB   {per * 60 / 1e9:6.2f} GB")
        print()

    print("A synthetic feature every 0.5 mm is optimistic: this mesher puts lines where the")
    print("copper is, so a real region has many more. Repeating on a real board:\n")
    real()


def real() -> None:
    """The same sizing, with the grid a real board actually forces."""
    import os
    from pathlib import Path
    from emi_worker.kicad import parse, parse_board
    from emi_worker.kicad.normalize import _board_extent
    from emi_worker.openems.model import Port, SolveParams, build_model

    path = os.environ.get("BOARD", "/boards/solar-ppm/solar-ppm.kicad_pcb")
    if not os.path.exists(path):
        print(f"  (no board at {path})")
        return
    board = parse_board(parse(Path(path).read_text()))
    transform = _board_extent(board)
    xs = [transform.pt(*pt)[0] for t_ in board.tracks for pt in t_.pts]
    ys = [transform.pt(*pt)[1] for t_ in board.tracks for pt in t_.pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    roi = (cx - 10, cy - 10, cx + 10, cy + 10)
    print(f"  {os.path.basename(path)}: 20 x 20 mm region at ({cx:.1f}, {cy:.1f})")

    for name, (dx, dy, dz) in PRESETS.items():
        params = SolveParams(
            roi=roi, frequencies_hz=[1e9],
            ports=[Port(name="p1", x=cx, y=cy, layer=board.copper_layers[0].name,
                        half_width_mm=0.2)],
            dx_um=dx, dy_um=dy, dz_um=dz, air_mm=5.0,
        )
        try:
            built = build_model(board, transform, params)
        except Exception as exc:  # a region with no copper, a port off-layer, ...
            print(f"  {name:<8}  could not build: {exc}")
            continue
        m = built.mesh
        nx, ny, nz = len(m.x), len(m.y), len(m.z)
        for sub in (1, 2, 4):
            cells = faces(nx // sub, ny // sub, nz // sub)
            per = cells * BYTES_PER_CELL_PER_FREQ
            print(f"  {name:<8}  {nx:4d} x {ny:4d} x {nz:3d}   {cells:10,}   1/{sub}"
                  f"   {per / 1e6:7.1f} MB   {per * 60 / 1e9:6.2f} GB"
                  + (f"   [{m.cells / 1e6:.1f} M cells]" if sub == 1 else ""))
        print()


if __name__ == "__main__":
    main()
