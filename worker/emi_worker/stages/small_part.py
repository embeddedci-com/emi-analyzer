"""The bounded solve: one small part of a board, sized to finish in minutes.

A full-wave solve is expensive for reasons that have nothing to do with the part being asked
about. The timestep is set by the smallest cell anywhere, the record by the lowest frequency
(30 MHz needs 100 ns simulated), the far field adds a box a tenth of a wavelength out on every
side, and a board region full of other nets forces grid lines for all of them. A 15 mm region
of a real board with the far field took 2 h 38 min and left 57 of its 60 frequencies unusable.

This mode removes each of those on purpose:

* **A coupon, not a region** (``openems/coupon.py``): the net's copper over its planes, so the
  mesh is set by one net's edges, not by every trace near it. A region can still be drawn.
* **No far-field box.** The outputs are the near-field map and the port impedance and
  S-parameters, which need no air around the copper beyond the default.
* **A band that keeps the record short**: 100 MHz to 2 GHz by default. The run lasts until its
  energy has decayed ``END_CRITERIA`` below its peak, and with both ends of the net terminated
  in 50 ohm that is a few round trips of the coupon, not the 100 ns 30 MHz would ask for. The
  lower edge is what the excitation has to reach and what the timestep cap is sized from
  (three periods, 30 ns), so it sets the worst case; below 100 MHz a part a few centimetres
  across is quasi-static, and the answer is a lumped L or C a circuit tool gives cheaper. The
  upper edge keeps the coarsest cell under 3.4 mm in FR-4, which the default air and a coupon
  margin of a few millimetres hold without special meshing.
* **A lower end criterion**: -50 dB, where a solve otherwise stops at -40. A port read from a
  record cut at -40 dB is where the far field's negative resistances came from.
* **A budget checked against the real mesh**: cells times the timestep cap. A run that would
  exceed it is refused before it starts, with the numbers, rather than started and abandoned.

What is kept from every solve: a run that reaches its timestep cap publishes no number, one
that diverges is refused, and a frequency at which the port reads as a negative resistance is
dropped and listed in ``truncated_hz``.
"""

from __future__ import annotations

import json
import math

from ..openems import coupon as coupon_mod
from ..openems import hotspots as hotspots_mod
from ..openems import network as network_mod
from ..openems.model import SolveParams
from . import StageError

MODE = "small_part"

#: The default band, Hz. See the module docstring for why these two numbers.
BAND_HZ = (100e6, 2e9)

#: The widest band a small-part solve accepts, Hz. Below the floor the timestep cap (three
#: periods) outgrows any budget; above the ceiling a coupon's cells shrink with the wavelength
#: and the budget is spent on the mesh instead.
MIN_HZ = 50e6
MAX_HZ = 6e9

#: The energy decay a run stops at: -50 dB of its peak.
END_CRITERIA = 1e-5

#: Copper lines closer than this fraction of dx are one grid line (mesh.MERGE_FRACTION is a
#: quarter). The timestep is set by the smallest cell anywhere, and on a routed board a quarter of
#: dx is always reached: a pad corner beside a trace edge, an arc's vertices. A half moves no edge
#: by more than a quarter of dx, a detail the preset does not claim to resolve anyway, and on a
#: real 34 x 13 mm coupon it halved the cells and made the timestep 35 % longer (1.58 M cells
#: at 37.5 um to 0.80 M at 50.7 um, coarse).
MERGE_FRACTION = 0.5

#: The height every map is read at above its copper, mm, whatever the preset. The coarse
#: preset's first grid line above a trace is about this far up; a finer one reads closer, where
#: the field at a trace's edge is louder, and the presets disagreed by 1.7 dB on the level.
MAP_HEIGHT_MM = 0.1

#: A cell this coarse or coarser, um, is where a via's inductance was measured reading high: a
#: 0.3 mm via 1 mm from its return read +10.1 % on the coarse preset against the two-post
#: closed form, and +7.7 % on normal (docs/verification/small-part-solve.md, check 4). The
#: limit is 10 %, so a coarse part with vias says so rather than failing quietly.
COARSE_VIA_UM = 150.0

COARSE_VIA_NOTE = "coarse mesh: via inductance can read up to about 10% high"

#: The largest mesh a small-part solve will build, in cells (216 MB at 72 bytes a cell).
MAX_CELLS = 3_000_000

#: The most work a small-part solve may be asked for: cells times the timestep cap. At the
#: 150 MC/s the worker assumes, 1.5e11 is 17 minutes if the run went all the way to its cap;
#: a terminated coupon stops on its end criterion well before that.
MAX_CELL_STEPS = 1.5e11

#: The shortest record a small-part solve starts with, s. Below this the budget is too small
#: for the part to ring down, and the run is refused before it starts.
MIN_RECORD_S = 10e-9


def is_small_part(p: dict) -> bool:
    return str((p or {}).get("mode") or "") == MODE


def band(p: dict) -> tuple[float, float]:
    raw = p.get("band_hz")
    if not raw:
        return BAND_HZ
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        raise StageError("band_hz must be [low, high] in Hz") from exc
    if not (MIN_HZ <= lo < hi <= MAX_HZ) or hi < 2 * lo:
        raise StageError(
            f"a small-part band must lie within {MIN_HZ / 1e6:g} MHz to {MAX_HZ / 1e9:g} GHz "
            f"and span at least 2:1; got {lo / 1e6:g} to {hi / 1e6:g} MHz"
        )
    return lo, hi


def apply(p: dict, params: SolveParams) -> SolveParams:
    """Bound a solve's parameters to the small-part mode, or refuse what it cannot do."""
    for key, what in (("far_field", "a far field"), ("cable_ports", "cable ports"),
                      ("model_components", "component models")):
        if p.get(key):
            raise StageError(
                f"a small-part solve has no {what}. That needs a full-wave solve, which is a "
                f"separate experimental feature"
            )
    lo, hi = band(p)
    # Maps at the band's edges as well as wherever the user asked: the excitation is sized from
    # the lowest and highest dump frequency, and the port network is read across the whole band.
    asked = [f for f in params.frequencies_hz if lo <= f <= hi]
    freqs = sorted({lo, hi, *asked})
    params.frequencies_hz = freqs
    params.f_max = hi
    params.far_field = False
    params.cable_ports = {}
    params.model_components = False
    params.end_criteria = END_CRITERIA
    params.merge_fraction = MERGE_FRACTION
    # The band's coarsest cell holds everywhere, the absorbing layer included; the upper edge
    # of the band is chosen for it (module docstring), and the padding used to grow past it.
    params.pml_within_max_cell = True
    params.map_height_mm = MAP_HEIGHT_MM
    # The default band spans 20:1, just past where ``model.excitation_band`` warns that its ends
    # carry "a few dB more uncertainty", so every run said so. That is true of a level read
    # against the source, and nothing here is one: the port numbers are ratios of two transforms
    # of the same record, and the maps are per volt of the source. At 2 GHz the microstrip
    # check read Z0 within 0.5 % and S21 within 0.1 dB on every preset.
    params.band_edge_note = False
    # The cap is derived from the band (three periods of its lowest frequency); a hand-set cap
    # would make the budget below meaningless.
    params.max_timesteps = 0
    return params


def coupon_nets(p: dict) -> list[str]:
    c = p.get("coupon") or {}
    nets = c.get("nets") if isinstance(c, dict) else None
    if not nets:
        return []
    if not isinstance(nets, list) or not all(isinstance(n, str) and n for n in nets):
        raise StageError("coupon.nets must be a list of net names")
    if len(nets) > 4:
        raise StageError("a coupon takes at most four nets: a signal, its pair, and their return")
    return nets


def cut(board, transform, p: dict, params: SolveParams):
    """The board reduced to the coupon's copper, and what was said about it. Unchanged if the
    run named no nets (a drawn region)."""
    # A port the browser placed does not know what is under it: the browser has no pour
    # outlines. The worker does, so a port with no reference names the nearest layer with a
    # pour under it here, for a region as well as a coupon. Without one it falls back to the
    # nearest copper layer, as every solve always has, and the result says so.
    notes: list[str] = []
    for port in params.ports:
        if port.reference_layer:
            continue
        found = coupon_mod.reference_under(board, transform, port.x, port.y, port.layer)
        if found:
            port.reference_layer = found
        else:
            notes.append(
                f"there is no pour under port {port.name}, so it returns to the nearest copper "
                f"layer whether or not that layer has copper there. If the board has pours, "
                f"refill its zones in KiCad (B) before uploading: an unfilled zone is not copper"
            )
    nets = coupon_nets(p)
    if not nets:
        if _coarse(params) and _vias_in(board, transform, params.roi):
            notes.append(COARSE_VIA_NOTE)
        return board, notes
    missing = [n for n in nets if n not in set(board.nets) | {t.net for t in board.tracks}
               | {q.net for q in board.pads}]
    if missing:
        raise StageError(f"{', '.join(missing)} is not a net on this board")
    reduced, cut_notes = coupon_mod.extract(board, transform, nets, params.roi)
    cell = min(params.dx_um, params.dy_um) / 1000.0
    tight = coupon_mod.tight_gaps(reduced, nets, cell)
    if tight:
        many = tight != 1
        cut_notes.append(
            f"{tight} piece{'s' if many else ''} of other copper {'are' if many else 'is'} "
            f"closer to the net than one cell ({cell * 1000:.0f} um), so the mesh may join "
            f"{'them' if many else 'it'} to the net. A finer mesh preset resolves the gap"
        )
    if _coarse(params) and any(v.net in nets for v in reduced.vias):
        cut_notes.append(COARSE_VIA_NOTE)
    return reduced, notes + cut_notes


def _coarse(params: SolveParams) -> bool:
    return min(params.dx_um, params.dy_um) >= COARSE_VIA_UM


def _vias_in(board, transform, roi) -> bool:
    x0, y0, x1, y1 = roi
    return any(x0 <= x <= x1 and y0 <= y <= y1
               for x, y in (transform.pt(v.x, v.y) for v in board.vias))


def admit(cells: int, needed_steps: int, dt_seconds: float) -> int:
    """The timestep cap this run gets, or a refusal the user reads.

    The cap is the physics' own (three periods of the band's lowest frequency) or what the
    budget affords at this mesh, whichever is shorter. Three periods is a bound, not what a
    terminated coupon needs: the run ends on its energy criterion long before it, and a
    structure that has emptied has no spectrum left to truncate. So the budget shortens the
    cap rather than refusing outright, down to ``MIN_RECORD_S``; a run that then reaches the
    shortened cap is refused afterwards, like any run that reaches its cap, and says why.
    """
    if cells > MAX_CELLS:
        raise StageError(
            f"this part is too large for a small-part solve: {cells:,} cells, over the "
            f"{MAX_CELLS:,} it allows. Pick a shorter net, draw a smaller region or use the "
            f"coarse mesh"
        )
    affordable = int(MAX_CELL_STEPS // max(cells, 1))
    cap = min(needed_steps, affordable)
    if cap * dt_seconds < MIN_RECORD_S:
        raise StageError(
            f"this part is too large for a small-part solve: at {cells:,} cells its budget "
            f"buys {cap * dt_seconds * 1e9:.1f} ns of simulated time, and a part needs at least "
            f"{MIN_RECORD_S * 1e9:.0f} ns to ring down. Pick a shorter net, draw a smaller "
            f"region or use the coarse mesh"
        )
    return cap


def decay_fraction(energy_db: float) -> float:
    """How far a run's energy has fallen towards the end criterion, 0 to 1, for progress."""
    target = 10.0 * math.log10(END_CRITERIA)
    return min(1.0, max(0.0, energy_db / target))


def unusable_reason(result) -> str | None:
    """Why no number from this run may be shown, or None. The solver's own wording when it
    has one."""
    own = getattr(result, "unconverged_reason", None)
    if callable(own):
        return own()
    if result.converged:
        return None
    return (
        f"the run reached its limit of {result.max_timesteps:,} timesteps with its energy only "
        f"{abs(result.final_energy_db):.1f} dB down, before the fields settled, so no impedance "
        f"or S-parameter from it is used"
    )


def add_hotspots(artifacts, workdir: str, dump_names: dict[str, str],
                 dump_heights: dict[str, float], params: SolveParams, nearby) -> None:
    """List each map's separate spots within a few dB of its loudest (``openems.hotspots``),
    with the net and part nearest each, in the manifest. A failure leaves the list out."""
    import os

    from ..openems import post

    ref = artifacts.manifest.get("reference_magnitude") or 0.0
    if ref <= 0:
        return
    ports = [(q.x, q.y) for q in params.ports]
    floor = -float(artifacts.manifest.get("dynamic_range_db", post.DYNAMIC_RANGE_DB))
    out = []
    for layer, dump in dump_names.items():
        path = os.path.join(workdir, f"{dump}.h5")
        if not os.path.exists(path):
            continue
        try:
            grids = post.read_fd_dump(path, dump_heights.get(layer))
        except (OSError, ValueError):
            continue
        for g in grids:
            found = hotspots_mod.spots(g.x_mm, g.y_mm, g.to_db(reference=ref), ports, floor)
            for s in found:
                s["net"] = nearby.net(s["x_mm"], s["y_mm"], layer)
                s["part"] = nearby.part(s["x_mm"], s["y_mm"])
            if found:
                out.append({"layer": layer, "frequency_hz": g.frequency_hz, "spots": found})
    artifacts.manifest["hotspots"] = {"within_db": hotspots_mod.WITHIN_DB, "maps": out}
    artifacts.files["manifest.json"] = json.dumps(artifacts.manifest, indent=2).encode()


def add_network(artifacts, workdir: str, raw_ports: list[dict], params: SolveParams,
                unusable_reason: str | None) -> None:
    """Write ``network.json`` beside the maps. A failure leaves a note, not a failed run."""
    lo, hi = min(params.frequencies_hz), max(params.frequencies_hz)
    ports = []
    for q, raw in zip(params.ports, raw_ports):
        ports.append({"name": q.name, "resistance": q.resistance, "excited": q.excited,
                      "net": raw.get("net"), "pad": raw.get("pad")})
    try:
        doc = network_mod.network(workdir, ports, network_mod.log_grid(lo, hi),
                                  unusable_reason=unusable_reason)
    except (OSError, ValueError) as exc:
        artifacts.manifest["network_note"] = f"the port network could not be read ({exc})"
    else:
        artifacts.files["network.json"] = json.dumps(doc, separators=(",", ":")).encode()
        artifacts.manifest["network"] = {
            "format_version": network_mod.FORMAT_VERSION,
            "ports": [q["name"] for q in ports],
            "truncated_hz": doc["truncated_hz"],
        }
    artifacts.files["manifest.json"] = json.dumps(artifacts.manifest, indent=2).encode()
