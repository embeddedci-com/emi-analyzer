"""Turn a parsed board plus a region of interest into an openEMS model.

This reads the **original** BoardModel, not the simplified triangle buffer the viewer uses.
The viewer's geometry is decimated for rendering; meshing it would bake that decimation
into the physics.

Scope, deliberately: a region of interest, one or more lumped ports, a Gaussian excitation,
and frequency-domain surface-current dumps on every copper layer. Absolute field strengths
need a real driver spectrum, which is P4's job; what this produces is comparative — good
for "this layout radiates less than that one at 480 MHz, and here is the trace responsible".
"""

from __future__ import annotations

import logging
import math

import numpy as np
from dataclasses import dataclass, field

from ..kicad.board import BoardModel
from . import csx
from .mesh import Mesh, MeshSpec, build_mesh, max_cell_for_frequency

log = logging.getLogger(__name__)

EPSILON_0 = 8.8541878128e-12

#: Air box around the region of interest, in mm. Radiated field needs somewhere to go
#: before it reaches the absorbing boundary; too little and the PML sits in the near field,
#: where it absorbs poorly and reflects instead.
DEFAULT_AIR_MM = 5.0

#: How far outside the region of interest copper is still included. A trace that leaves the
#: region still carries current back into it, and truncating it exactly at the boundary
#: creates an open circuit that radiates like an antenna stub that is not on the board.
COPPER_MARGIN_MM = 2.0


class ModelError(ValueError):
    """The model cannot be built as requested. The message is shown to the user."""


@dataclass
class Port:
    """A lumped port: a resistor plus an excitation across a gap in the z direction."""

    name: str
    x: float
    y: float
    #: Copper layer the port's top sits on. The bottom is the nearest reference plane.
    layer: str
    #: Half-width of the port footprint in mm, in both in-plane axes.
    half_width_mm: float = 0.2
    resistance: float = 50.0
    #: Only one port is excited per simulation pass; the rest are passive loads.
    excited: bool = True


@dataclass
class SolveParams:
    roi: tuple[float, float, float, float]  # min_x, min_y, max_x, max_y in board mm
    frequencies_hz: list[float]
    ports: list[Port]
    dx_um: float = 50.0
    dy_um: float = 50.0
    dz_um: float = 25.0
    air_mm: float = DEFAULT_AIR_MM
    #: Highest frequency to resolve. Defaults to the top requested dump frequency.
    f_max: float = 0.0
    #: Energy decay at which the run may stop early (-40 dB).
    end_criteria: float = 1e-4
    #: Hard cap on timesteps. 0 means derive it from the physics, which is almost always
    #: what you want — see _required_timesteps.
    max_timesteps: int = 0
    #: Model matched components (§12). Off by default so that every existing caller, and
    #: every result already reported, keeps solving bare copper exactly as before.
    model_components: bool = False
    #: Components offered ahead of the built-in library, already in precedence order. The
    #: server knows who owns what; this does not need to.
    component_candidates: list = field(default_factory=list)
    #: Connector references to fit a Tier B gap port to (§7), with the cable assigned to each:
    #: ``{"USB1": {"type": "usb2-shielded", "length_m": 1.0}}``. Empty means no gap ports,
    #: which is what every solve did before this existed.
    cable_ports: dict = field(default_factory=dict)
    #: Record the NF2FF box the board's far field needs (§16.2). Off by default: it adds twelve
    #: dumps and their artifact, and a solve that is only being read as a hotspot map does not
    #: want either.
    far_field: bool = False
    #: Frequencies the far field is evaluated at. Empty means derive a log grid across the
    #: radiated band, which is what §16.2 asks for -- the dump frequencies alone are the user's
    #: clock harmonics and say nothing about the band between them.
    far_field_frequencies_hz: list[float] = field(default_factory=list)

    def resolved_f_max(self) -> float:
        if self.f_max > 0:
            return self.f_max
        if self.frequencies_hz:
            return max(self.frequencies_hz)
        raise ModelError("the solve names no frequencies and no f_max")


@dataclass
class BuiltModel:
    doc: csx.CSXDocument
    mesh: Mesh
    #: Copper layer name -> the name of its J dump, so post-processing knows what is what.
    dump_names: dict[str, str] = field(default_factory=dict)
    port_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: One entry per component actually placed (§12): reference, model, provenance, source.
    modelled_parts: list[dict] = field(default_factory=list)
    #: One entry per gap port fitted (§7): reference, anchor, exit normal, probe names.
    cable_ports: list[dict] = field(default_factory=list)
    #: Where the NF2FF box went and at which frequencies (§16.2). ``None`` when not recorded.
    far_field: dict | None = None


#: openEMS's Gaussian pulse has support of roughly this many time constants, measured
#: against the solver's own report: it asked for 26811 timesteps at fc = 1 GHz with
#: dt = 1.069e-13 s, which is 2.86 ns, or 2.86/fc.
GAUSSIAN_SUPPORT_OVER_FC = 2.86

#: The resistance across a cable gap port (§7). An open circuit in all but name: against a
#: cable's antenna impedance of a few hundred ohms this loads the gap by about 0.016 dB, which
#: M0 measured. openEMS still needs something in the cell for the probes to measure across.
CABLE_GAP_OHM = 1e6


#: How much of openEMS's Gaussian band the requested frequencies are allowed to occupy.
#: The pulse is about 20 dB down at f0 +- fc, so a frequency sitting on that edge is
#: evaluated where the source barely excites the structure. M0 measured the cost: a transfer
#: function at 1 GHz moved by 3-4 dB between two sources that agreed to 0.01 dB everywhere
#: inside the band, and widening the band alone removed the discrepancy. 0.8 leaves about
#: 7 dB more source amplitude at each requested end than the edge would.
BAND_FILL = 0.8

#: Where a requested frequency is close enough to the edge to be worth saying so, as a
#: fraction of fc. Above this the band could not be widened far enough — see
#: ``excitation_band`` — and the caller is told rather than left to wonder.
NOTE_ABOVE = 0.9


def excitation_band(f_min: float, f_max: float) -> tuple[float, float, str | None]:
    """Centre and half-width of the Gaussian excitation, in Hz, and a note if it is tight.

    The obvious choice — centre on the requested band and set the half-width to reach both
    ends — puts f_min and f_max exactly on the 20 dB points. That was the original code, and
    it is wrong at both edges of every solve: see BAND_FILL.

    Widening costs no timesteps. dt is fixed by the mesh, not by the source, and a wider
    pulse is a *shorter* one in time, so ``required_timesteps`` asks for no more than before.
    What it spends is a little energy above f_max, where the mesh is coarser than
    lambda/20 — energy that is 20 dB down at the edge and is never evaluated.

    **One Gaussian cannot cover an arbitrarily wide band.** Pushing both requested ends
    inside the pulse eventually drives the lower edge below zero, which is DC the PML does
    not absorb and the structure cannot radiate away, so past a span of about 9:1 the width
    is clamped at ``fc = f0``.

    The clamp alone is not worth mentioning: at 11:1 it still leaves the requested ends at
    0.83 fc, which is barely worse than the 0.8 it was aiming for, and a note on every such
    solve would be noise. What is worth mentioning is the *achieved* fill, so the caller
    hears about it only once an end is past NOTE_ABOVE — a span wider than about 19:1. Five
    odd harmonics of a 100 MHz clock stay silent; M3's 30 MHz cable band does not.
    """
    if f_min <= 0 or f_max <= 0 or f_max < f_min:
        raise ModelError(f"an excitation needs 0 < f_min <= f_max, got {f_min} and {f_max}")

    f0 = (f_max + f_min) / 2.0
    # A single-frequency solve has no width of its own; give it a band rather than a pulse
    # so long that the excitation alone dominates the run length.
    fc = min(max((f_max - f_min) / 2.0 / BAND_FILL, f0 / 2.0), f0)

    fill = (f_max - f0) / fc if fc > 0 else 0.0
    if fill <= NOTE_ABOVE:
        return f0, fc, None
    return f0, fc, (
        f"the requested frequencies span {f_max / f_min:.0f}:1, which is wider than one "
        f"excitation pulse covers well; {f_min / 1e6:.0f} MHz and {f_max / 1e6:.0f} MHz sit "
        f"near the edge of the source band and carry a few dB more uncertainty than the "
        f"middle of it. Solving a narrower range would remove that"
    )


def required_timesteps(dt_seconds: float, fc: float, f_min: float) -> tuple[int, str]:
    """How many timesteps a run actually needs, and why.

    Two independent requirements, and the run needs the larger:

    * **The source must finish.** openEMS's Gaussian pulse takes about 2.86/fc seconds. A
      run shorter than that stops before the excitation completes, and every field in the
      result is zero — with no error, because openEMS simply does what it was told.
    * **The lowest frequency must be resolved.** A transform can only see frequencies whose
      period fits in the record, so the run has to cover several periods of f_min.

    This is the same T_sim the cost model uses, which is why the estimate a user is shown
    before starting matches what the solver then does.
    """
    if dt_seconds <= 0:
        raise ModelError("the mesh produced a non-positive timestep")

    excitation_s = GAUSSIAN_SUPPORT_OVER_FC / max(fc, 1.0)
    # Three times the pulse: one to emit it, the rest for the structure to ring down.
    need_excitation = 3.0 * excitation_s
    need_bandwidth = 3.0 / max(f_min, 1.0)

    if need_excitation >= need_bandwidth:
        return int(math.ceil(need_excitation / dt_seconds)), "three excitation lengths"
    return int(math.ceil(need_bandwidth / dt_seconds)), "three periods of the lowest frequency"


def kappa_from_loss_tangent(epsilon_r: float, loss_tangent: float, frequency: float) -> float:
    """Conductivity equivalent to a dielectric loss tangent, in S/m.

    openEMS models loss as a conductivity, which is frequency-independent, while a real
    loss tangent is not. Evaluating at one frequency is therefore an approximation that is
    exact at that frequency and drifts either side of it — so it is evaluated at the centre
    of the band of interest rather than at the top.
    """
    return 2 * math.pi * frequency * EPSILON_0 * epsilon_r * loss_tangent


def _plan_components(model: BoardModel, transform, params: "SolveParams", notes: list[str]):
    """Which matched components go into this solve, and where (§12).

    Returns an empty plan when component modelling is off, which is the default — a board
    with no matched parts must solve bit-for-bit as it did before this existed, and the
    cheapest way to guarantee that is for the whole path to produce nothing.
    """
    from emi_worker.components.place import PlacementPlan, plan_all

    if not params.model_components:
        return PlacementPlan()

    from emi_worker.components import match_part, resolve_part
    from emi_worker.rules.decoupling import CAP_RE

    pads_by_ref: dict[str, list] = {}
    for pad in model.pads:
        if pad.ref:
            pads_by_ref.setdefault(pad.ref, []).append(pad)

    resolved = {}
    unmatched = 0
    for ref, pads in pads_by_ref.items():
        if not CAP_RE.match(ref) or len(pads) != 2:
            continue
        got = resolve_part(
            match_part(ref, pads[0].value or "", pads[0].footprint or ""),
            params.component_candidates or None,
        )
        if got is None:
            unmatched += 1
        else:
            resolved[ref] = got

    plan = plan_all(resolved, pads_by_ref, transform, params.roi)
    if plan.placements:
        notes.append(
            f"{len(plan.placements)} capacitors modelled as series R-L-C"
            + (f"; {len(plan.skipped)} matched but not placed" if plan.skipped else "")
            + (f"; {unmatched} left as bare copper" if unmatched else "")
        )
    return plan


def _plan_cable_ports(model: BoardModel, transform, params: "SolveParams",
                      layer_z: dict, notes: list[str]):
    """Gap ports for the connectors this run asked for (§7).

    Returns (ports, reach_mm). An empty list and zero reach when none were asked for, which
    is what every solve did before Tier B existed.
    """
    from emi_worker.cables.attach import anchors_for_board
    from emi_worker.openems.gapport import fit

    if not params.cable_ports:
        return [], 0.0

    anchors = anchors_for_board(model, transform)
    # The gap is one cell across, so it is the in-plane mesh that sets it.
    gap_mm = min(params.dx_um, params.dy_um) / 1000.0
    # The reference copper: the outermost layer, which is where a connector's shell and
    # ground pads are. A gap against an inner plane would measure a voltage the cable never
    # sees.
    top_layer = next(iter(layer_z))
    z = layer_z[top_layer]

    ports = []
    reach = 0.0
    for ref in sorted(params.cable_ports):
        anchor = anchors.get(ref)
        if anchor is None:
            notes.append(
                f"{ref} was asked for a cable port but is not a connector on this board, so "
                f"its cable is not driven"
            )
            continue
        port, why = fit(anchor, z_mm=z, gap_mm=gap_mm)
        if port is None:
            notes.append(f"{ref} has no cable port: {why}")
            continue
        min_x, min_y, max_x, max_y = params.roi
        if not (min_x <= port.x_mm <= max_x and min_y <= port.y_mm <= max_y):
            notes.append(
                f"{ref} is outside the solve region, so its cable is not driven. Extend the "
                f"region to include it"
            )
            continue
        ports.append(port)
        reach = max(reach, port.reach_mm())
    return ports, reach


def _copper_features(
    model: BoardModel,
    transform,
    roi: tuple[float, float, float, float],
    margin: float,
) -> tuple[list[float], list[float]]:
    """In-plane coordinates that must land on grid lines.

    Trace edges and pad edges: if a copper edge falls mid-cell, the solver rounds it to the
    nearest line and the geometry it simulates is not the geometry that was drawn.
    """
    min_x, min_y, max_x, max_y = roi
    lo_x, hi_x = min_x - margin, max_x + margin
    lo_y, hi_y = min_y - margin, max_y + margin

    xs: set[float] = set()
    ys: set[float] = set()

    def note(x: float, y: float) -> None:
        if lo_x <= x <= hi_x:
            xs.add(round(x, 6))
        if lo_y <= y <= hi_y:
            ys.add(round(y, 6))

    for track in model.tracks:
        half = track.width_mm / 2.0
        for px, py in track.pts:
            bx, by = transform.pt(px, py)
            # Both edges of the track, not just its centreline.
            note(bx - half, by - half)
            note(bx + half, by + half)
            note(bx, by)

    for pad in model.pads:
        for px, py in pad.ring:
            bx, by = transform.pt(px, py)
            note(bx, by)

    for via in model.vias:
        bx, by = transform.pt(via.x, via.y)
        r = (via.size_mm or via.drill_mm) / 2.0
        note(bx - r, by - r)
        note(bx + r, by + r)

    return sorted(xs), sorted(ys)


def _layer_z(model: BoardModel) -> dict[str, float]:
    """Copper layer heights above the bottom of the stack, in mm."""
    entries = [s for s in model.stackup if s.thickness_mm > 0 or s.is_copper]
    total = sum(s.thickness_mm for s in entries) or model.thickness_mm
    z = total
    out: dict[str, float] = {}
    for s in entries:
        top, bottom = z, z - s.thickness_mm
        if s.is_copper:
            out[s.name] = round((top + bottom) / 2.0, 6)
        z = bottom
    return out


def build_model(model: BoardModel, transform, params: SolveParams) -> BuiltModel:
    """Build the openEMS document for one solve."""
    if not params.ports:
        raise ModelError(
            "a solve needs at least one port. Without a source, openEMS has nothing to "
            "excite and every field comes out zero."
        )
    if not params.frequencies_hz:
        raise ModelError("a solve needs at least one frequency to record fields at")

    f_max = params.resolved_f_max()
    f_centre = sum(params.frequencies_hz) / len(params.frequencies_hz)
    notes: list[str] = []

    layer_z = _layer_z(model)
    if not layer_z:
        raise ModelError("the board has no copper layers")

    dielectrics = [s for s in model.stackup if s.is_dielectric and s.thickness_mm > 0]
    max_er = max((s.epsilon_r for s in dielectrics), default=4.4) or 4.4

    # ---- mesh ----
    copper_x, copper_y = _copper_features(model, transform, params.roi, COPPER_MARGIN_MM)

    # The port's own edges must be grid lines too. This is not a refinement — a port box
    # that falls entirely between grid lines contains no cells, so the excitation is
    # applied to nothing. openEMS reports no error for that: it finds the excitation
    # property, runs to completion, and every field in the result is zero.
    for port in params.ports:
        for dx in (-port.half_width_mm, 0.0, port.half_width_mm):
            copper_x.append(port.x + dx)
            copper_y.append(port.y + dx)
    # Cable ports, planned before meshing for the same reason components are: the gap is one
    # cell wide and needs grid lines either side of it, and the domain has to reach past the
    # board edge far enough to hold the stub.
    cable_ports, cable_reach = _plan_cable_ports(model, transform, params, layer_z, notes)
    for port in cable_ports:
        if port.axis == 0:
            copper_x.extend(port.required_lines())
            copper_y.extend(port.across_lines())
        else:
            copper_y.extend(port.required_lines())
            copper_x.extend(port.across_lines())

    # Components, planned before meshing. A series R-L-C is three elements in three adjacent
    # cells, so the gap needs four grid lines — which cannot be arranged after the fact.
    plan = _plan_components(model, transform, params, notes)
    copper_x.extend(plan.required_x())
    copper_y.extend(plan.required_y())

    copper_x = sorted(set(copper_x))
    copper_y = sorted(set(copper_y))
    # §7: grid past the edge. Without this the stub ends inside the absorbing boundary and is
    # terminated by the PML instead of by open air.
    #
    # Extended only on the sides the stubs actually leave from. Growing all four sides is the
    # obvious version and costs far more than it needs to: on solar-ppm with one westward stub
    # it took the mesh from 7.8 M cells to 11.3 M (+46 %) against §4's +5-15 % budget, almost
    # all of it air nothing reaches into.
    roi = list(params.roi)
    if cable_ports:
        grew: list[str] = []
        for port in cable_ports:
            reach = port.reach_mm()
            if port.axis == 0:
                side = 0 if port.sign < 0 else 2
                roi[side] += -reach if port.sign < 0 else reach
                grew.append("west" if port.sign < 0 else "east")
            else:
                side = 1 if port.sign < 0 else 3
                roi[side] += -reach if port.sign < 0 else reach
                grew.append("south" if port.sign < 0 else "north")
        sides = sorted(set(grew))
        notes.append(
            f"the region was extended {max(p.reach_mm() for p in cable_ports):.0f} mm "
            f"{' and '.join(sides)} of the board to hold "
            f"{len(cable_ports)} cable stub{'s' if len(cable_ports) != 1 else ''}"
        )
    roi = (roi[0], roi[1], roi[2], roi[3])

    spec = MeshSpec(
        roi=roi,
        f_max=f_max,
        dx_um=params.dx_um,
        dy_um=params.dy_um,
        dz_um=params.dz_um,
        air_above_mm=params.air_mm,
        air_below_mm=params.air_mm,
        max_epsilon_r=max_er,
    )
    mesh = build_mesh(spec, copper_x, copper_y, list(layer_z.values()))

    coarse = max_cell_for_frequency(f_max, max_er)
    if mesh.max_cell_mm > coarse * 1.05:
        notes.append(
            f"the coarsest cell is {mesh.max_cell_mm * 1000:.0f} um against a "
            f"{coarse * 1000:.0f} um limit at {f_max / 1e9:.2f} GHz; results near the top "
            f"of the band will be under-resolved"
        )

    # ---- document ----
    f_min = min(params.frequencies_hz)
    f0, fc, band_note = excitation_band(f_min, f_max)
    if band_note:
        notes.append(band_note)

    dt = mesh.timestep_seconds()
    needed, why = required_timesteps(dt, fc, f_min)

    max_steps = params.max_timesteps
    if max_steps <= 0:
        max_steps = needed
    elif max_steps < needed:
        # Worth stating plainly rather than silently honouring: a run capped below the
        # excitation length returns zeros, and a user who set this by hand deserves to know
        # what it will cost them.
        notes.append(
            f"the timestep cap of {max_steps:,} is below the {needed:,} this run needs "
            f"({why}); the result will be under-resolved and may be entirely zero"
        )

    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=f0, fc=fc),
        x_lines=mesh.x.tolist(),
        y_lines=mesh.y.tolist(),
        z_lines=mesh.z.tolist(),
        f_max=f_max,
        end_criteria=params.end_criteria,
        max_timesteps=max_steps,
    )

    x0, y0, x1, y1 = params.roi
    gx0, gx1 = float(mesh.x[0]), float(mesh.x[-1])

    gy0, gy1 = float(mesh.y[0]), float(mesh.y[-1])
    # §7: the dielectric is clipped to the board outline.
    #
    # It used to fill the whole mesh, which was harmless while the mesh stopped at the region.
    # Once a cable port extends the domain past the board edge it is not: the stub would sit
    # on FR-4 instead of in air, which changes both where the cable resonates and how much it
    # radiates. The clip is to the outline's bounding box.
    #
    # A bounding box is exact for a rectangular board, which all four of the boards this was
    # built against are. On a board with a cut corner or a notch it leaves FR-4 where there is
    # none — an error in the right direction to notice (too much dielectric, never too little)
    # and one that needs an extruded polygon primitive to fix properly. CSXCAD's Polygon here
    # is flat, so that is a separate piece of work.
    dx0, dy0, dx1, dy1 = gx0, gy0, gx1, gy1
    outline_pts = [transform.pt(x, y) for ring in model.outline for x, y in ring]
    if outline_pts:
        dx0 = max(gx0, min(p[0] for p in outline_pts))
        dx1 = min(gx1, max(p[0] for p in outline_pts))
        dy0 = max(gy0, min(p[1] for p in outline_pts))
        dy1 = min(gy1, max(p[1] for p in outline_pts))
        if dx1 <= dx0 or dy1 <= dy0:
            # The region does not overlap the outline at all. Keep the old behaviour rather
            # than emitting an inside-out box, and say so.
            dx0, dy0, dx1, dy1 = gx0, gy0, gx1, gy1
            notes.append(
                "the board outline does not overlap the solve region, so the dielectric was "
                "not clipped to it"
            )

    # Dielectric slabs, spanning the whole grid in plane so the board does not end in the
    # middle of the air box.
    z = sum(s.thickness_mm for s in model.stackup if s.thickness_mm > 0) or model.thickness_mm
    for s in model.stackup:
        if s.thickness_mm <= 0 and not s.is_copper:
            continue
        top, bottom = z, z - s.thickness_mm
        if s.is_dielectric:
            kappa = kappa_from_loss_tangent(s.epsilon_r or 4.4, s.loss_tangent or 0.0, f_centre)
            doc.add(csx.Material(
                name=f"diel_{s.name.replace(' ', '_')}",
                epsilon=s.epsilon_r or 4.4,
                kappa=kappa,
                primitives=[csx.Box(
                    p1=(dx0, dy0, bottom), p2=(dx1, dy1, top),
                    priority=csx.PRIORITY_DIELECTRIC,
                )],
            ))
            if not s.from_file:
                notes.append(
                    f"{s.name} has an assumed permittivity of {s.epsilon_r or 4.4}; a wrong "
                    f"value shifts every resonance in the result"
                )
        z = bottom

    # Copper. Filtered by bounding box rather than clipped: primitives outside the grid are
    # simply not discretised, and clipping a trace at the region boundary would create an
    # open stub that radiates like something that is not on the board.
    lo_x, hi_x = x0 - COPPER_MARGIN_MM, x1 + COPPER_MARGIN_MM
    lo_y, hi_y = y0 - COPPER_MARGIN_MM, y1 + COPPER_MARGIN_MM

    def in_region(pts) -> bool:
        return any(lo_x <= p[0] <= hi_x and lo_y <= p[1] <= hi_y for p in pts)

    per_layer: dict[str, list[csx.Primitive]] = {name: [] for name in layer_z}
    kept = dropped = 0

    for track in model.tracks:
        if track.layer not in per_layer:
            continue
        pts = [transform.pt(px, py) for px, py in track.pts]
        if not in_region(pts):
            dropped += 1
            continue
        kept += 1
        from ..kicad import geometry as g
        for ring in g.thick_polyline(pts, track.width_mm):
            per_layer[track.layer].append(csx.Polygon(
                vertices=ring, elevation=layer_z[track.layer],
            ))

    for zone in model.zones:
        if zone.layer not in per_layer:
            continue
        ring = [transform.pt(px, py) for px, py in zone.ring]
        if not in_region(ring):
            dropped += 1
            continue
        kept += 1
        per_layer[zone.layer].append(csx.Polygon(
            vertices=ring, elevation=layer_z[zone.layer],
        ))

    for pad in model.pads:
        ring = [transform.pt(px, py) for px, py in pad.ring]
        if not in_region(ring):
            continue
        targets = [l for l in pad.layers if l in per_layer]
        if "*.Cu" in pad.layers:
            targets = list(per_layer)
        for name in targets:
            per_layer[name].append(csx.Polygon(vertices=ring, elevation=layer_z[name]))

    for name, prims in per_layer.items():
        if prims:
            doc.add(csx.Metal(name=f"cu_{name.replace('.', '_')}", primitives=prims))

    log.info("model: %d copper features in region, %d outside", kept, dropped)
    if kept == 0:
        raise ModelError(
            "there is no copper in the selected region. Move or enlarge the region of "
            "interest."
        )

    # Vias, as vertical PEC boxes joining the layers they span.
    via_prims: list[csx.Primitive] = []
    for via in model.vias:
        bx, by = transform.pt(via.x, via.y)
        if not (lo_x <= bx <= hi_x and lo_y <= by <= hi_y):
            continue
        spanned = [n for n in layer_z if n in via.layers] or list(layer_z)
        zs = [layer_z[n] for n in spanned]
        r = (via.size_mm or via.drill_mm) / 2.0
        if r <= 0:
            continue
        via_prims.append(csx.Box(
            p1=(bx - r, by - r, min(zs)), p2=(bx + r, by + r, max(zs)),
            priority=csx.PRIORITY_METAL,
        ))
    if via_prims:
        doc.add(csx.Metal(name="cu_vias", primitives=via_prims))

    # ---- ports ----
    layer_order = list(layer_z)
    port_names: list[str] = []
    for i, port in enumerate(params.ports):
        if port.layer not in layer_z:
            raise ModelError(f"port {port.name!r} names unknown layer {port.layer!r}")
        top_z = layer_z[port.layer]
        # The return is the nearest other copper layer — the plane the signal references.
        others = [n for n in layer_order if n != port.layer]
        if not others:
            raise ModelError("a port needs at least two copper layers to sit between")
        bottom_layer = min(others, key=lambda n: abs(layer_z[n] - top_z))
        bottom_z = layer_z[bottom_layer]

        hw = port.half_width_mm
        box = csx.Box(
            p1=(port.x - hw, port.y - hw, min(top_z, bottom_z)),
            p2=(port.x + hw, port.y + hw, max(top_z, bottom_z)),
            priority=csx.PRIORITY_PORT,
        )

        doc.add(csx.LumpedElement(
            name=f"{port.name}_res", direction=2, resistance=port.resistance,
            caps=True, primitives=[box],
        ))
        if port.excited:
            # Excite from the signal layer toward the reference plane.
            sign = -1.0 if top_z > bottom_z else 1.0
            doc.add(csx.ExcitationProperty(
                name=f"{port.name}_exc", number=i, excite=(0.0, 0.0, sign),
                primitives=[box],
            ))

        # Voltage along the gap, and current around the conductor. The current probe has to
        # enclose the metal, so it is drawn a little larger than the port itself.
        doc.add(csx.ProbeBox(
            name=f"{port.name}_ut", type=0, norm_dir=2, weight=-1.0,
            primitives=[csx.Box(
                p1=(port.x, port.y, min(top_z, bottom_z)),
                p2=(port.x, port.y, max(top_z, bottom_z)),
            )],
        ))
        doc.add(csx.ProbeBox(
            name=f"{port.name}_it", type=1, norm_dir=2, weight=1.0,
            primitives=[csx.Box(
                p1=(port.x - hw * 1.5, port.y - hw * 1.5, (top_z + bottom_z) / 2),
                p2=(port.x + hw * 1.5, port.y + hw * 1.5, (top_z + bottom_z) / 2),
            )],
        ))
        port_names.append(port.name)

    # ---- field dumps ----
    #
    # Surface current per copper layer, as the tangential magnetic field just above it.
    #
    # The obvious choice, DumpType 12 "electric current density", is wrong here: it is
    # J = kappa * E, and E is identically zero inside a perfect conductor, so a dump on a
    # copper layer comes back all zeros. Sampling slightly off the copper instead returns
    # the dielectric's loss current, which is non-zero, looks like a result, and is not one.
    #
    # For a conductor the surface current is |J_s| = |n x H|, so the honest measurement is
    # the H field on the grid line immediately above the copper. That is what shows which
    # trace is carrying the current that radiates.
    z_lines = built_z = mesh.z
    dump_names: dict[str, str] = {}
    for name, zc in layer_z.items():
        idx = int(np.searchsorted(z_lines, zc + 1e-9))
        if idx >= len(z_lines):
            idx = len(z_lines) - 1
        z_dump = float(z_lines[idx])
        if abs(z_dump - zc) < 1e-9 and idx + 1 < len(z_lines):
            z_dump = float(z_lines[idx + 1])
        dump = f"Hf_{name.replace('.', '_')}"
        doc.add(csx.DumpBox(
            name=dump,
            dump_type=csx.DUMP_H_FREQ,
            frequencies=params.frequencies_hz,
            primitives=[csx.Box(p1=(x0, y0, z_dump), p2=(x1, y1, z_dump))],
        ))
        dump_names[name] = dump

    problems = doc.validate()

    # Verify every port encloses at least one cell in each axis. The mesh should guarantee
    # this now that port edges are required lines, but the failure it prevents is silent
    # and total, so it is worth asserting rather than assuming.
    for port in params.ports:
        nx = int(((mesh.x >= port.x - port.half_width_mm) &
                  (mesh.x <= port.x + port.half_width_mm)).sum())
        ny = int(((mesh.y >= port.y - port.half_width_mm) &
                  (mesh.y <= port.y + port.half_width_mm)).sum())
        if nx < 2 or ny < 2:
            problems.append(
                f"port {port.name!r} spans {nx}x{ny} grid lines and would excite nothing; "
                f"widen it beyond the mesh resolution "
                f"({params.dx_um:.0f}x{params.dy_um:.0f} um)"
            )

    if problems:
        raise ModelError("; ".join(problems))

    # Cable ports, now that the mesh exists. §7: a PEC stub along the exit normal, a one-cell
    # gap joining it to the board's copper, and a 1 MOhm element across that gap with probes.
    #
    # 1 MOhm rather than an actual open circuit: openEMS needs something in the cell for the
    # probes to measure across, and against a cable's antenna impedance of a few hundred ohms
    # a megohm is an open to better than a thousandth of a decibel. M0 measured the loading
    # error at 0.016 dB.
    cable_port_meta: list[dict] = []
    for port in cable_ports:
        gap_lo, gap_hi = port.gap_span()
        stub_lo, stub_hi = port.stub_span()
        across_lo, across_hi = port.across_span()
        if port.axis == 0:
            gap_box = csx.Box(p1=(gap_lo, across_lo, port.z_mm),
                              p2=(gap_hi, across_hi, port.z_mm),
                              priority=csx.PRIORITY_PORT)
            stub_box = csx.Box(p1=(stub_lo, across_lo, port.z_mm),
                               p2=(stub_hi, across_hi, port.z_mm),
                               priority=csx.PRIORITY_METAL)
            probe_u = csx.Box(p1=(gap_lo, (across_lo + across_hi) / 2, port.z_mm),
                              p2=(gap_hi, (across_lo + across_hi) / 2, port.z_mm))
        else:
            gap_box = csx.Box(p1=(across_lo, gap_lo, port.z_mm),
                              p2=(across_hi, gap_hi, port.z_mm),
                              priority=csx.PRIORITY_PORT)
            stub_box = csx.Box(p1=(across_lo, stub_lo, port.z_mm),
                               p2=(across_hi, stub_hi, port.z_mm),
                               priority=csx.PRIORITY_METAL)
            probe_u = csx.Box(p1=((across_lo + across_hi) / 2, gap_lo, port.z_mm),
                              p2=((across_lo + across_hi) / 2, gap_hi, port.z_mm))

        name = f"cable_{port.ref}"
        doc.add(csx.Metal(name=f"{name}_stub", primitives=[stub_box]))
        doc.add(csx.LumpedElement(name=f"{name}_r", direction=port.axis,
                                  resistance=CABLE_GAP_OHM, caps=True, primitives=[gap_box]))
        doc.add(csx.ProbeBox(name=f"{name}_ut", type=0, norm_dir=port.axis, weight=-1.0,
                             primitives=[probe_u]))
        cable_port_meta.append({
            "ref": port.ref,
            "anchor_mm": [round(port.x_mm, 4), round(port.y_mm, 4)],
            "exit_normal": [round(port.nx, 4), round(port.ny, 4)],
            "z_mm": round(port.z_mm, 4),
            "width_mm": round(port.width_mm, 4),
            "gap_mm": round(port.gap_mm, 6),
            "probe": f"{name}_ut",
        })

    # Components, now that the mesh exists and the lines they asked for are in it.
    modelled: list[dict] = []
    if plan.placements:
        from emi_worker.components.place import modelled_parts

        for placement in plan.placements:
            z = layer_z.get(placement.layer)
            if z is None:
                notes.append(
                    f"{placement.ref} sits on {placement.layer}, which is not in the stackup, "
                    f"so it was left as bare copper"
                )
                continue
            rlc = placement.resolved.rlc
            for element in csx.series_rlc(
                f"cap_{placement.ref}", direction=placement.axis,
                resistance=rlc.esr_ohm, inductance=rlc.esl_h, capacitance=rlc.c_f,
                cells=placement.cells(z),
            ):
                doc.add(element)
        modelled = modelled_parts(plan)
    for ref, why in plan.skipped:
        notes.append(f"{ref} matched a component but was not modelled: {why}")

    # §16.2's NF2FF box, last: it is placed from the finished mesh, because the grid is what
    # actually exists -- a cable port extends the domain on one side only, and a box centred on
    # the region would sit outside the grid there and inside the structure opposite.
    far_field_meta = None
    if params.far_field:
        from emi_worker.openems import nf2ff as nf2ff_mod

        ff_freqs = params.far_field_frequencies_hz or far_field_grid(
            params.resolved_f_max())
        faces = nf2ff_mod.plan_faces(mesh, params.roi)
        nf2ff_mod.add_dumps(doc, faces, ff_freqs)
        far_field_meta = {
            "frequencies_hz": ff_freqs,
            "faces_mm": [faces.x0, faces.y0, faces.z0, faces.x1, faces.y1, faces.z1],
            "centre_mm": list(faces.centre()),
            "sub_sampling": nf2ff_mod.FACE_SUB_SAMPLING,
        }
        notes.append(
            f"the far field is recorded on a box {faces.x1 - faces.x0:.0f} x "
            f"{faces.y1 - faces.y0:.0f} x {faces.z1 - faces.z0:.0f} mm at "
            f"{len(ff_freqs)} frequencies"
        )

    return BuiltModel(
        doc=doc, mesh=mesh, dump_names=dump_names, port_names=port_names, notes=notes,
        modelled_parts=modelled, cable_ports=cable_port_meta, far_field=far_field_meta,
    )


#: The radiated band starts here (§15). Below it a different standard and a different method
#: apply -- §16.3's conducted scan, which is a circuit problem, not an FDTD one.
RADIATED_MIN_HZ = 30e6

#: How many points across the band. §16.2 asks for "about 60": enough that interpolating in dB
#: between them cannot hide a resonance, few enough that twelve face dumps stay an artifact
#: rather than a download. M0 sized both -- 0.07-0.25 GB sub-sampled at 60 frequencies.
FAR_FIELD_POINTS = 60


def far_field_grid(f_max: float, points: int = FAR_FIELD_POINTS) -> list[float]:
    """A log grid across the radiated band, up to what this solve actually resolves.

    Log rather than linear because the limits, the cable resonances and the board's own modes
    are all roughly log-spaced, and because a linear grid spends half its points above 500 MHz
    where nothing changes quickly.

    Capped at the solve's own f_max: asking the transform for a frequency the excitation never
    contained returns a number, and it is noise.
    """
    top = max(float(f_max), RADIATED_MIN_HZ * 2)
    step = (top / RADIATED_MIN_HZ) ** (1.0 / (points - 1))
    return [RADIATED_MIN_HZ * step ** k for k in range(points)]
