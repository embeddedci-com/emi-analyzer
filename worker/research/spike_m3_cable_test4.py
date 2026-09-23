#!/usr/bin/env python3
"""M3 · cable test 4 — Tier B against Tier C on the real fixtures (docs/implementation.md §10).

M0 answered this on one synthetic board: `V_oc / Z_ant` reproduced a fully coupled solve to
1.3 dB at 150 mm and 2.2 dB at 300 mm. The gate is set on real boards instead — it was run
on three private boards, two with a USB-C and one with an RJ45 connector, at 0.3 m and 1 m,
passing if Tier B keeps the layout ranking at every frequency and agrees within ±6 dB below
the first resonance. SETUPS names your own boards: `folder:connector-ref:cable,...`, each
folder under BOARDS holding one .kicad_pcb, e.g.
`SETUPS=my-board:USB1:usb2-shielded,other-board:RJ1:ethernet-ftp`.

Two solves per configuration, on **one shared grid**:

    tier B   the production model: a 10 mm PEC stub, a one-cell gap with a 1 MOhm element
             across it, a driver port on the board. post.cable_transfer() turns the gap probe
             into H_cm, and nec2c supplies Z_ant. This is exactly what the product does.
    tier C   the same document with the gap filled (0.1 Ohm), a PEC cable running from the
             stub's end out to the full length, and a current probe at the root. The cable is
             in the grid, so nothing is composed and nothing is assumed.

**One grid for both.** Tier C's domain — which has to hold the cable — is used for the Tier B
run too, even though production Tier B would use a much smaller one. Mesh error is then common
to the two sides and cancels, instead of appearing as a coupling residual. M0 established that
this is the only way the comparison means anything.

**The in-plane preset is what costs.** `merge_close` collapses required lines closer than
`dx_um / 4`, so the smallest cell — and therefore the timestep — is `dx_um / 4` exactly, and a
run to 30 MHz needs `3 / f_min` of record whatever the cable is doing. The whole matrix at the
150 µm preset is ~240 h of solver; at 300 µm it is ~41 h, and at 600 µm ~11 h. Since B and C
share the grid, coarsening it moves both sides together — so the study preset is a knob here
in a way it would never be in a product run. `DX_UM` sets it.

**Air around the cable** (``AIR_MM``, September 2026). The grid used to stop 5 mm above and
below the copper and at the edge of the strip, so Tier C's cable ran 5-15 mm from the absorbing
boundary for its whole length, while nec2c's Z_ant is a wire in free space: the two tiers were
then different antennas, and B against C measured the boundary as much as the coupling.
``AIR_MM`` pads every side with air (no copper) the way the far-field box does, with the box
itself left out. ``AIR_MM=0`` is the old domain.

**The record** (``MAX_NS``). Every run is a fixed ``MAX_NS`` long, with openEMS's own end
criterion off (``solve`` says why), and every transform
is repeated on the first 80 % of the record: a result that moves by more than a few tenths of
a dB between the two was cut short, and says so in its ``record_db``. Stability is judged from
the energy instead: a run whose energy has not fallen 30 dB by the end of its record is
refused.

    docker run --rm --cpus 3 -m 6g -v "$PWD/worker:/spike" -v <pcb>:/boards:ro \\
        -v <out>:/spike/spike_out -e SETUPS=<folder:ref:cable,...> -e AIR_MM=80 \\
        -w /spike -e PYTHONPATH=/spike --entrypoint python3 emi-worker:phase1 \\
        research/spike_m3_cable_test4.py

Board names go in ``SETUPS`` and in the output; neither belongs in anything committed.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import numpy as np

from emi_worker.cables import nec
from emi_worker.cables.attach import anchors_for_board
from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.normalize import _board_extent
from emi_worker.openems import csx, post, run
from emi_worker.openems.gapport import STUB_CLEARANCE_MM
from emi_worker.openems.model import Port, SolveParams, build_model, excitation_seconds

C = 299_792_458.0
OUT = Path(os.environ.get("OUT", "/spike/spike_out"))
BOARDS = Path(os.environ.get("BOARDS", "/boards"))

DX_UM = float(os.environ.get("DX_UM", "300"))
DZ_UM = float(os.environ.get("DZ_UM", "150"))
F_MIN = float(os.environ.get("F_MIN", "30e6"))
F_MAX = float(os.environ.get("F_MAX", "600e6"))
#: Width of the strip across the exit direction. Not the region along it -- see region().
ROI_MM = float(os.environ.get("ROI_MM", "30"))
#: How far back from the connector the excitation sits.
DRIVER_SETBACK_MM = float(os.environ.get("SETBACK_MM", "15"))
THREADS = int(os.environ.get("THREADS", "3"))
#: Air on every side of the domain, in mm; see the docstring. 0 is the old 5 mm.
AIR_MM = float(os.environ.get("AIR_MM", "80"))
#: Effectively off: see solve(). The record is MAX_NS long instead.
END_CRITERIA = float(os.environ.get("END_CRITERIA", "1e-12"))
#: Length of every record, in ns. 40 ns is four times the excitation; the 80 % check says
#: whether it was enough.
MAX_NS = float(os.environ.get("MAX_NS", "40"))
#: Height of the cable above the reference ground, for nec2c. The standard test setup.
HEIGHT_M = float(os.environ.get("HEIGHT_M", "1.0"))

FREQS = np.geomspace(F_MIN, F_MAX, int(os.environ.get("N_FREQ", "31")))

#: Board folder under BOARDS, connector reference, the cable in the library.
SETUPS = [tuple(v.split(":")) for v in os.environ.get("SETUPS", "").split(",") if v]
LENGTHS_M = [float(v) for v in os.environ.get("LENGTHS", "0.3,1.0").split(",")]

#: The gap resistance that stands in for "the cable is bonded to the board". M0 measured this
#: against an actual PEC fill: the two differ by 0.02 dB, and keeping the element in place
#: means Tier B and Tier C differ in one *number* rather than in their geometry.
GAP_SHORT_OHM = 0.1


def board_of(name: str):
    hits = [p for p in sorted((BOARDS / name).glob("*.kicad_pcb"))
            if not p.name.startswith("_autosave") and "backup" not in p.name.lower()]
    if not hits:
        raise SystemExit(f"no .kicad_pcb under {BOARDS / name}")
    board = parse_board(parse(hits[0].read_text()))
    return board, _board_extent(board)


def driver_port(board, transform, anchor, roi) -> Port:
    """Where to excite the board.

    The far end of the region from the connector, on the outermost copper layer: a source at
    the connector itself would drive the gap by conduction and measure the port rather than
    the layout. The absolute level this produces is arbitrary — H_cm is a ratio — but it has
    to be the *same* source in both tiers, which sharing one Port guarantees.
    """
    # A quarter of the way across the board from the connector, on the exit axis: far enough
    # that the source is driving the layout rather than the port, and inside the strip.
    min_x, min_y, max_x, max_y = roi
    span = (max_x - min_x) if abs(anchor.nx) >= abs(anchor.ny) else (max_y - min_y)
    back = min(DRIVER_SETBACK_MM, span / 8)
    x = anchor.x_mm - anchor.nx * back
    y = anchor.y_mm - anchor.ny * back
    # Wide enough to span cells at whatever study preset is in force: a port narrower than
    # the mesh excites nothing, and build_model refuses it rather than solving zero fields.
    return Port(name="drv", x=x, y=y, layer=board.copper_layers[0].name,
                half_width_mm=max(0.2, 1.5 * DX_UM / 1000.0))


def board_extent(board, transform) -> tuple[float, float, float, float]:
    pts = [transform.pt(*p) for o in board.outline for p in o] or \
          [transform.pt(*p) for t in board.tracks for p in t.pts]
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def region(board, transform, anchor, length_m: float) -> tuple[float, float, float, float]:
    """The shared domain: the whole board along the exit, a slice across it, then the cable.

    **Along the exit normal the board is kept whole**, because it is the antenna's other arm
    and that is what decides where the structure resonates. Measured with nec2c in free
    space: a 100 mm arm puts a 1 m cable's first resonance at 123 MHz and a 0.3 m cable's at
    347 MHz, while a 12 mm arm — which is what a connector-sized region leaves — has no
    resonance anywhere in the band at all. A gate phrased as "below the first resonance" is
    not worth running on a structure that has none.

    **Across it the region is a strip**, because the transverse extent barely moves the
    antenna and multiplies every copper feature in the board: on one of the boards, going from a
    30 mm strip to the full 100 mm costs 3x the cells for a counterpoise that is already
    the right length.

    Past the board, the cable plus a quarter wave of air before the absorbing boundary.
    """
    bx0, by0, bx1, by1 = board_extent(board, transform)
    reach = length_m * 1e3 + (C / (C / (4 * length_m))) / 4 * 1e3
    half = ROI_MM / 2
    if abs(anchor.nx) >= abs(anchor.ny):
        x0, x1 = bx0 - 1.0, bx1 + 1.0
        y0, y1 = anchor.y_mm - half, anchor.y_mm + half
        if anchor.nx < 0:
            x0 -= reach
        else:
            x1 += reach
    else:
        y0, y1 = by0 - 1.0, by1 + 1.0
        x0, x1 = anchor.x_mm - half, anchor.x_mm + half
        if anchor.ny < 0:
            y0 -= reach
        else:
            y1 += reach
    return (x0, y0, x1, y1)


#: Cell growth along the cable, and the largest cell there: λ/20 at F_MAX.
REGRADE_RATIO = 1.3
REGRADE_BEYOND_MM = 25.0


def regrade_along_cable(built, anchor) -> float:
    """Re-grade the grid along the exit axis past the stub, and return the worst cell ratio.

    The mesher filled the cable's air at about 2.4 mm cells and then stepped to 11.5 mm where
    the region met its padding, a 4.8:1 jump on the axis the cable runs along, against a stated
    bound of 1.4. The first run of this study diverged on exactly that grid (the energy climbed
    back 2,290x after the pulse). Past the stub, where there is no copper to resolve, the lines
    are replaced by a series growing 1.3x per cell to λ/20 of F_MAX, reaching as far as the
    old grid did, then eight PML lines. B and C share the result, so it is still one grid.
    """
    axis = 0 if abs(anchor.nx) >= abs(anchor.ny) else 1
    sign = 1.0 if (anchor.nx if axis == 0 else anchor.ny) > 0 else -1.0
    attr = "x_lines" if axis == 0 else "y_lines"
    lines = np.asarray(getattr(built.doc, attr), dtype=float)
    origin = anchor.x_mm if axis == 0 else anchor.y_mm
    # Keep everything up to 15 mm past the stub (gap + 10 mm stub), where the cable starts.
    cut = origin + sign * (built.cable_ports[0]["gap_mm"] + 10.0 + 15.0)
    keep = lines[lines <= cut] if sign > 0 else lines[lines >= cut][::-1]
    outer = lines[-1 - 8] if sign > 0 else lines[8]           # the last non-PML line
    grown = list(keep)
    step = abs(grown[-1] - grown[-2])
    beyond = C / F_MAX / 20 * 1e3
    while sign * (outer - grown[-1]) > 0:
        step = min(step * REGRADE_RATIO, beyond)
        grown.append(grown[-1] + sign * step)
    for _ in range(8):
        step *= 1.2
        grown.append(grown[-1] + sign * step)
    from emi_worker.openems.mesh import _smooth_ratio

    new = _smooth_ratio(np.asarray(sorted(grown)), 1.4, DX_UM / 1000.0)
    setattr(built.doc, attr, new.tolist())
    d = np.diff(new)
    return float(np.max(np.maximum(d[1:] / d[:-1], d[:-1] / d[1:])))


def add_cable(built, anchor, length_m: float) -> None:
    """Turn the Tier B document into Tier C: bond the gap, and run the cable out.

    The cable starts where the stub ends, so the two tiers share the stub exactly and differ
    only past it. It is a square PEC column one gap-cell across, which is what M0's wire was:
    openEMS has no thin-wire model, and a staircased column is the honest version.
    """
    meta = built.cable_ports[0]
    axis = 0 if abs(meta["exit_normal"][0]) >= abs(meta["exit_normal"][1]) else 1
    sign = math.copysign(1.0, meta["exit_normal"][axis])
    gap = meta["gap_mm"]
    z = meta["z_mm"]
    start = (meta["anchor_mm"][axis]) + sign * (gap + 10.0)   # STUB_LENGTH_MM
    end = start + sign * length_m * 1e3
    across = meta["anchor_mm"][1 - axis]
    hw = gap / 2.0

    lo, hi = (start, end) if sign > 0 else (end, start)
    if axis == 0:
        cable = csx.Box(p1=(lo, across - hw, z - hw), p2=(hi, across + hw, z + hw),
                        priority=csx.PRIORITY_METAL)
        loop = csx.Box(p1=(start + sign * gap, across - 1.5 * hw, z - 1.5 * hw),
                       p2=(start + sign * gap, across + 1.5 * hw, z + 1.5 * hw))
    else:
        cable = csx.Box(p1=(across - hw, lo, z - hw), p2=(across + hw, hi, z + hw),
                        priority=csx.PRIORITY_METAL)
        loop = csx.Box(p1=(across - 1.5 * hw, start + sign * gap, z - 1.5 * hw),
                       p2=(across + 1.5 * hw, start + sign * gap, z + 1.5 * hw))

    built.doc.add(csx.Metal(name="tierc_cable", primitives=[cable]))
    # The current on the cable, an H loop just past the root. Type 1 is a current probe; the
    # normal is the exit axis, which is the direction the current flows.
    built.doc.add(csx.ProbeBox(name="cable_it", type=1, norm_dir=axis, weight=1.0,
                               primitives=[loop]))
    # Bond the gap. Only the resistance changes: same cells, same caps, same everything else.
    for prop in built.doc.properties:
        if isinstance(prop, csx.LumpedElement) and prop.name.endswith("_r") \
                and prop.name.startswith("cable_"):
            prop.resistance = GAP_SHORT_OHM


def _pad_with_air() -> None:
    """Give every side AIR_MM of air, as the far-field box does, without the box."""
    if AIR_MM <= 0:
        return
    from emi_worker.openems import model as model_mod
    from emi_worker.openems import nf2ff as nf2ff_mod

    model_mod.far_field_pad_mm = lambda clearance, cell: AIR_MM
    model_mod.far_field_clearance_mm = lambda f_top: 1.0
    nf2ff_mod.add_dumps = lambda *a, **k: None


def _record_steps(doc) -> int:
    """Timesteps for MAX_NS of record, from the Courant limit of the finished grid."""
    d = [float(np.diff(np.asarray(v)).min()) * 1e-3
         for v in (doc.x_lines, doc.y_lines, doc.z_lines)]
    dt = 0.95 / (C * math.sqrt(sum(1.0 / x ** 2 for x in d)))
    return int(math.ceil(MAX_NS * 1e-9 / dt))


def _probe_decay_db(wd: Path) -> float:
    """How far below its own peak the last tenth of the loudest-ending probe is, in dB.

    run.divergence_ratio is switched off here (see solve), so this is the stability check
    instead: a diverging grid ends loud, a healthy one ends quiet.
    """
    worst = -300.0
    for f in wd.iterdir():
        if f.suffix or f.name in ("et", "ht") or not f.is_file() or f.name.endswith(".xml"):
            continue
        try:
            v = np.abs(post.read_probe(str(f)).values)
        except (OSError, ValueError):
            continue
        if v.size < 20 or v.max() == 0:
            continue
        worst = max(worst, 20 * math.log10(v[-v.size // 10:].max() / v.max() + 1e-300))
    return worst


def solve(tag: str, built, freqs: np.ndarray) -> dict:
    """Run one tier, or reuse a finished run of the same model.

    openEMS's own end criterion is off (a fixed MAX_NS record instead): this study's 30-600 MHz
    pulse is several lobes and about 9 ns long, a board strip with a 10 mm stub empties between
    them, and the first run was stopped at 9 ns, inside the excitation. The worker's divergence
    check was refusing the same run for the same reason; it now waits for the excitation to end
    (``excitation_s``). Stability is judged on the energy at the end of the record as well.
    """
    problems = built.doc.validate()
    if problems:
        raise SystemExit(f"{tag}: " + "; ".join(problems))
    wd = OUT / f"test4_{tag}"
    wd.mkdir(parents=True, exist_ok=True)
    xml = built.doc.to_string()
    done = wd / "run.json"
    if done.exists() and (wd / "model.xml").exists() and (wd / "model.xml").read_text() == xml:
        info = json.loads(done.read_text())
        print(f"    {tag}: reusing {info['steps']:,} steps", flush=True)
        return {"wd": wd, "steps": info["steps"]}
    (wd / "model.xml").write_text(xml)
    t0 = time.time()
    r = run.run_openems(str(wd / "model.xml"), str(wd), threads=THREADS,
                        excitation_s=excitation_seconds(built.doc.excitation.fc))
    tail = _probe_decay_db(wd)
    print(f"    {tag}: {r.final_timestep:,} steps, {time.time()-t0:.0f}s, "
          f"energy {r.final_energy_db:.1f} dB, loudest probe ends {tail:.0f} dB down",
          flush=True)
    for w in r.warnings:
        print(f"      warning: {w}")
    done.write_text(json.dumps({"steps": r.final_timestep, "seconds": time.time() - t0,
                                "energy_db": r.final_energy_db, "warnings": r.warnings,
                                "probe_tail_db": tail}))
    # Judged on the energy. A probe alone is not evidence: the 1 MOhm gap holds the charge the
    # pulse's DC content leaves on it and bleeds it off over ~100 ns, so its voltage can end
    # 10 dB below peak in a run whose energy is 60 dB down. A cable still ringing is a short
    # record, which the 80 % check prices.
    if r.final_energy_db > -30.0:
        raise SystemExit(f"{tag}: the energy is only {-r.final_energy_db:.0f} dB down at the "
                         f"end of the record: this run is not decaying")
    return {"wd": wd, "steps": r.final_timestep, "probe_tail_db": tail}


def _head(trace, fraction: float = 0.8):
    n = int(fraction * len(trace.time_s))
    return post.ProbeTrace(trace.time_s[:n], trace.values[:n])


def antenna(length_m: float, freqs: np.ndarray, gap_mm: float, board_span_m: float
            ) -> tuple[np.ndarray, np.ndarray]:
    """Z_ant and E_per_amp from nec2c, for **the structure the FDTD grid contains**.

    Three deliberate departures from what a product run asks nec2c for, each because the
    other side of this comparison cannot have the thing:

    * **No ground plane.** The FDTD domain is absorbing on all six sides, so there is no
      image under the cable. A NEC cable 1 m over perfect ground is a different antenna, and
      the first smoke run measured the difference: a flat +12 dB, which is the two antennas
      disagreeing rather than the composition failing.
    * **An open far end,** because a wire in the grid ends in air. The library says
      ``equipment`` for USB, which is a drop wire to a plane that here does not exist.
    * **The radius that matches the column.** openEMS has no thin-wire model, so Tier C's
      cable is a square column one gap-cell across; the equivalent radius of a square of
      side *a* is about 0.59 a. Cable 4a measured how much this matters — 4 % of resonance
      across a 10x change in radius — so it is not a detail to leave at a default.
    """
    ring = nec.ObservationRing(distance_m=3.0)
    z, e = [], []
    for f in freqs:
        deck = nec.Deck(length_m=length_m, frequency_hz=float(f), height_m=HEIGHT_M,
                        board_span_m=board_span_m, far_end="open", ring=ring,
                        radius_m=0.59 * gap_mm / 1000.0, ground=False)
        r = nec.run(deck)
        z.append(r.z_in)
        e.append(r.e_per_amp())
    return np.asarray(z), np.asarray(e)


def first_resonance_hz(z: np.ndarray, freqs: np.ndarray) -> float:
    """The lowest frequency where the reactance crosses zero going inductive."""
    x = z.imag
    for i in range(len(x) - 1):
        if x[i] < 0 <= x[i + 1]:
            # Linear in log f, which is how the grid is spaced.
            t = -x[i] / (x[i + 1] - x[i])
            return float(math.exp(math.log(freqs[i]) + t * (math.log(freqs[i + 1])
                                                           - math.log(freqs[i]))))
    return float(freqs[-1])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _pad_with_air()
    print(f"cable test 4 — {DX_UM:.0f}/{DZ_UM:.0f} um preset, "
          f"{F_MIN/1e6:.0f}-{F_MAX/1e6:.0f} MHz, {len(FREQS)} points\n")

    out: dict = {"preset_um": [DX_UM, DZ_UM], "frequencies_hz": FREQS.tolist(),
                 "roi_mm": ROI_MM, "height_m": HEIGHT_M, "air_mm": AIR_MM,
                 "end_criteria": END_CRITERIA, "cases": []}

    only = os.environ.get("ONLY")
    for name, ref, cable_id in SETUPS:
        if only and name not in only.split(","):
            continue
        board, transform = board_of(name)
        anchors = anchors_for_board(board, transform)
        anchor = anchors.get(ref)
        if anchor is None:
            print(f"{name}: no connector {ref} (have {sorted(anchors)})")
            continue

        for length_m in LENGTHS_M:
            roi = region(board, transform, anchor, length_m)
            params = SolveParams(
                roi=roi, frequencies_hz=FREQS.tolist(), f_max=F_MAX,
                ports=[driver_port(board, transform, anchor, roi)],
                dx_um=DX_UM, dy_um=DX_UM, dz_um=DZ_UM,
                cable_ports={ref: {"type": cable_id, "length_m": length_m}},
                # Effectively off. The DFT reads whatever record it is given, and a run that
                # stops at -40 dB of energy leaves the lowest frequency with a fraction of a
                # period in it -- the first smoke run covered 1.7 periods of 200 MHz. M0 hit
                # this too and re-ran its whole study with a fixed step count.
                end_criteria=END_CRITERIA,
                max_timesteps=int(os.environ.get("MAX_STEPS", "0")),
                far_field=AIR_MM > 0,
            )
            tag = f"{name}_{ref}_{length_m:g}m"
            b = build_model(board, transform, params)
            if b.cable_ports:
                worst = regrade_along_cable(b, anchor)
                b.doc.max_timesteps = _record_steps(b.doc)
                print(f"  re-graded along the cable: worst cell ratio {worst:.2f}; "
                      f"{b.doc.max_timesteps:,} steps for {MAX_NS:g} ns", flush=True)
            if not b.cable_ports:
                print(f"  {tag}: no gap port — {'; '.join(b.notes)}")
                continue
            m = b.mesh
            cells = b.doc.cell_count()
            print(f"  {tag}: {cells/1e6:.1f} M cells, domain "
                  f"{roi[2]-roi[0]:.0f} x {roi[3]-roi[1]:.0f} mm", flush=True)

            rb = solve(f"{tag}_B", b, FREQS)
            c = build_model(board, transform, params)
            regrade_along_cable(c, anchor)
            c.doc.max_timesteps = b.doc.max_timesteps
            add_cable(c, anchor, length_m)
            rc = solve(f"{tag}_C", c, FREQS)

            probe = b.cable_ports[0]["probe"]
            gap_tr = post.read_probe(str(rb["wd"] / probe))
            u_tr = post.read_probe(str(rb["wd"] / "drv_ut"))
            i_tr = post.read_probe(str(rb["wd"] / "drv_it"))

            def pred_from(gap, u, i):
                hh = post.cable_transfer(gap, u, i, FREQS.tolist())
                vs = post._dft(u, FREQS) + post._dft(i, FREQS) * 50.0
                return (np.asarray(hh["h_real"]) + 1j * np.asarray(hh["h_imag"])) * vs

            bx0, by0, bx1, by1 = board_extent(board, transform)
            arm_m = ((bx1 - bx0) if abs(anchor.nx) >= abs(anchor.ny)
                     else (by1 - by0)) / 1000.0
            z_ant, e_per_amp = antenna(length_m, FREQS, b.cable_ports[0]["gap_mm"], arm_m)
            pred = pred_from(gap_tr, u_tr, i_tr) / z_ant
            c_tr = post.read_probe(str(rc["wd"] / "cable_it"))
            meas = post._dft(c_tr, FREQS)
            err = 20.0 * np.log10(np.abs(pred) / np.abs(meas))
            # The same on 80 % of each record: how far the result still depends on its tail.
            pred_s = pred_from(_head(gap_tr), _head(u_tr), _head(i_tr)) / z_ant
            meas_s = post._dft(_head(c_tr), FREQS)
            record_db = float(max(np.max(np.abs(20 * np.log10(np.abs(pred_s) / np.abs(pred)))),
                                  np.max(np.abs(20 * np.log10(np.abs(meas_s) / np.abs(meas))))))

            f_res = first_resonance_hz(z_ant, FREQS)
            below = FREQS < f_res
            print(f"\n    first resonance {f_res/1e6:.0f} MHz "
                  f"({int(below.sum())} of {len(FREQS)} points below it)")
            print(f"    {'f (MHz)':>9} {'|Z_ant|':>9} {'ang':>5} "
                  f"{'B dBuA':>9} {'C dBuA':>9} {'error':>8}")
            for k in range(0, len(FREQS), max(1, len(FREQS) // 12)):
                print(f"    {FREQS[k]/1e6:9.0f} {abs(z_ant[k]):9.0f} "
                      f"{np.angle(z_ant[k], deg=True):+5.0f} "
                      f"{20*np.log10(abs(pred[k])*1e6):9.1f} "
                      f"{20*np.log10(abs(meas[k])*1e6):9.1f} {err[k]:+8.2f}")
            lo = np.abs(err[below]) if below.any() else np.abs(err)
            print(f"\n    below resonance: median {np.median(lo):.2f} dB, "
                  f"90th {np.percentile(lo, 90):.2f} dB, worst {lo.max():.2f} dB  "
                  f"{'PASS' if lo.max() <= 6.0 else 'FAIL'} (gate 6 dB)")
            print(f"    whole band:      median {np.median(np.abs(err)):.2f} dB, "
                  f"worst {np.abs(err).max():.2f} dB; record check {record_db:.3f} dB\n",
                  flush=True)

            out["cases"].append({
                "board": name, "ref": ref, "cable_id": cable_id, "length_m": length_m,
                "cells": cells, "roi_mm": list(roi),
                "first_resonance_hz": f_res,
                "z_ant_real": z_ant.real.tolist(), "z_ant_imag": z_ant.imag.tolist(),
                "e_per_amp": e_per_amp.tolist(),
                "i_pred_abs": np.abs(pred).tolist(), "i_meas_abs": np.abs(meas).tolist(),
                "error_db": err.tolist(),
                "steps_b": rb["steps"],
                "steps_c": rc["steps"],
                "record_db": record_db,
                "below_resonance_worst_db": float(lo.max()),
                "below_resonance_median_db": float(np.median(lo)),
            })
            (OUT / "m3_cable_test4.json").write_text(json.dumps(out, indent=2))

    ranking(out)
    (OUT / "m3_cable_test4.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {OUT}/m3_cable_test4.json")


def ranking(out: dict) -> None:
    """The other half of the gate: does Tier B order the configurations the way Tier C does?

    Ranking is what the tool is actually for — a user asks whether this layout is better than
    that one, not what the absolute microamps are. A tier that is 3 dB optimistic everywhere
    still answers that question; one that swaps two layouts does not.
    """
    cases = out["cases"]
    if len(cases) < 2:
        return
    freqs = out["frequencies_hz"]
    pred = np.array([c["i_pred_abs"] for c in cases])
    meas = np.array([c["i_meas_abs"] for c in cases])
    labels = [f"{c['board']}/{c['length_m']:g}m" for c in cases]
    agree = 0
    swaps: list[str] = []
    for k in range(len(freqs)):
        rb = list(np.argsort(-pred[:, k]))
        rc = list(np.argsort(-meas[:, k]))
        if rb == rc:
            agree += 1
        elif len(swaps) < 8:
            swaps.append(f"{freqs[k]/1e6:.0f} MHz: B {[labels[i] for i in rb]} "
                         f"vs C {[labels[i] for i in rc]}")
    print(f"\nlayout ranking: B matches C at {agree} of {len(freqs)} frequencies")
    for s in swaps:
        print(f"  {s}")
    out["ranking"] = {"agree": agree, "total": len(freqs), "examples": swaps,
                      "labels": labels}


if __name__ == "__main__":
    main()
