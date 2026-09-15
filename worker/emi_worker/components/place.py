"""Putting a resolved component into the mesh (§12).

A capacitor is a series R-L-C bridging the facing edges of its two pads, in the board plane,
at the copper height. §2.4 and K1 established why it has to be three elements rather than one:
the shipped CSXCAD wires R, C and L in parallel, and a capacitor is ESR and ESL in series with
C.

Three elements need three adjacent cells, which need four grid lines across the pad gap — so
placement cannot be decided after meshing. It runs first, contributes the lines it needs, and
then builds the elements once the mesh exists. That is the whole shape of this module.

**Nothing is placed where anything is uncertain.** A part straddling the region boundary, pads
on different layers, an off-axis rotation, a gap too small to divide — each is skipped and
reported by reference. §12's rule is that a board with no matched parts solves bit-for-bit as
it does today, and the same logic makes a board with *some* matched parts differ only where
the model is sound.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from emi_worker.components.document import Resolved

#: The gap has to be at least this wide, in mm, to hold three cells that the mesher will
#: honour. Below it the cells collapse into each other and openEMS applies one element where
#: three were meant.
MIN_GAP_MM = 0.06

#: How far a pad centre may be from the axis before the part counts as rotated off-axis. A
#: capacitor at 45 degrees has no in-plane axis to bridge along, and guessing one would place
#: the element across the wrong pair of edges.
OFF_AXIS_TOLERANCE_MM = 0.05


@dataclass(frozen=True)
class Placement:
    """Where one component's elements go, in board millimetres."""

    ref: str
    resolved: Resolved
    #: 0 for x, 1 for y: the axis the element bridges along.
    axis: int
    #: The gap's start and end along ``axis``.
    lo: float
    hi: float
    #: The extent across the gap, on the other in-plane axis.
    across_lo: float
    across_hi: float
    layer: str

    def required_lines(self) -> list[float]:
        """The four grid lines a three-cell series element needs."""
        step = (self.hi - self.lo) / 3.0
        return [self.lo, self.lo + step, self.lo + 2 * step, self.hi]

    def cells(self, z_mm: float) -> list[tuple[tuple[float, float, float],
                                               tuple[float, float, float]]]:
        """The three (p1, p2) boxes, in order along the axis, at the copper height."""
        lines = self.required_lines()
        out = []
        for a, b in zip(lines, lines[1:]):
            if self.axis == 0:
                p1 = (a, self.across_lo, z_mm)
                p2 = (b, self.across_hi, z_mm)
            else:
                p1 = (self.across_lo, a, z_mm)
                p2 = (self.across_hi, b, z_mm)
            out.append((p1, p2))
        return out


@dataclass
class PlacementPlan:
    placements: list[Placement] = field(default_factory=list)
    #: (reference, why) for every part that resolved but could not be placed.
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def required_x(self) -> list[float]:
        return sorted({v for p in self.placements if p.axis == 0 for v in p.required_lines()})

    def required_y(self) -> list[float]:
        return sorted({v for p in self.placements if p.axis == 1 for v in p.required_lines()})


def _bounds(ring: list[tuple[float, float]], transform) -> tuple[float, float, float, float]:
    pts = [transform.pt(x, y) for x, y in ring]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def plan_placement(
    ref: str,
    resolved: Resolved,
    pads: list,
    transform,
    roi: tuple[float, float, float, float],
) -> tuple[Placement | None, str | None]:
    """Where this part's element goes, or why it cannot be placed."""
    if len(pads) != 2:
        return None, f"{ref} has {len(pads)} pads, not two"
    a, b = pads

    a_layers = [l for l in (a.layers or []) if l.endswith(".Cu")]
    b_layers = [l for l in (b.layers or []) if l.endswith(".Cu")]
    if not a_layers or not b_layers:
        return None, f"{ref} has a pad on no copper layer"
    if a_layers[0] != b_layers[0]:
        return None, (
            f"{ref} has pads on different layers ({a_layers[0]} and {b_layers[0]}), so there "
            f"is no in-plane gap to bridge")
    layer = a_layers[0]

    ax0, ay0, ax1, ay1 = _bounds(a.ring, transform)
    bx0, by0, bx1, by1 = _bounds(b.ring, transform)
    a_cx, a_cy = (ax0 + ax1) / 2, (ay0 + ay1) / 2
    b_cx, b_cy = (bx0 + bx1) / 2, (by0 + by1) / 2

    dx, dy = abs(b_cx - a_cx), abs(b_cy - a_cy)
    if dx >= dy:
        axis, off_axis = 0, dy
    else:
        axis, off_axis = 1, dx
    if off_axis > OFF_AXIS_TOLERANCE_MM:
        return None, (
            f"{ref} is rotated off-axis (pad centres differ by {off_axis:.2f} mm across the "
            f"part), so there is no single in-plane axis to bridge along")

    # The facing edges: the inner edge of each pad along the axis.
    if axis == 0:
        lo, hi = (ax1, bx0) if a_cx < b_cx else (bx1, ax0)
        # Across the gap, the element spans where the two pads actually overlap: an element
        # wider than the narrower pad would bridge to copper that is not there.
        across_lo, across_hi = max(ay0, by0), min(ay1, by1)
    else:
        lo, hi = (ay1, by0) if a_cy < b_cy else (by1, ay0)
        across_lo, across_hi = max(ax0, bx0), min(ax1, bx1)

    if hi - lo < MIN_GAP_MM:
        return None, (
            f"{ref} has a {max(hi - lo, 0):.3f} mm pad gap, too small to divide into the "
            f"three cells a series R-L-C needs")
    if across_hi <= across_lo:
        return None, f"{ref}'s pads do not overlap across the gap"

    min_x, min_y, max_x, max_y = roi
    corners = [(lo, across_lo), (hi, across_hi)] if axis == 0 else [(across_lo, lo), (across_hi, hi)]
    for cx, cy in corners:
        if not (min_x <= cx <= max_x and min_y <= cy <= max_y):
            return None, (
                f"{ref} straddles the region boundary, so only part of it would be modelled")

    return Placement(ref=ref, resolved=resolved, axis=axis, lo=lo, hi=hi,
                     across_lo=across_lo, across_hi=across_hi, layer=layer), None


def plan_all(
    resolved_by_ref: dict[str, Resolved],
    pads_by_ref: dict[str, list],
    transform,
    roi: tuple[float, float, float, float],
) -> PlacementPlan:
    plan = PlacementPlan()
    for ref, resolved in sorted(resolved_by_ref.items()):
        if not resolved.placeable:
            plan.skipped.append((ref, "; ".join(resolved.gaps)))
            continue
        placement, why = plan_placement(ref, resolved, pads_by_ref.get(ref, []), transform, roi)
        if placement is None:
            plan.skipped.append((ref, why or "could not be placed"))
        else:
            plan.placements.append(placement)
    return plan


def modelled_parts(plan: PlacementPlan) -> list[dict]:
    """The modelled-parts list a result carries (§12): reference, model, owner, source."""
    out = []
    for p in plan.placements:
        r = p.resolved
        out.append({
            "ref": p.ref,
            "component_id": r.component_id,
            "component": r.component_name,
            "generic": r.generic,
            "source": r.source.describe() if r.source else None,
            "c_f": r.rlc.c_f,
            "esl_h": r.rlc.esl_h,
            "esr_ohm": r.rlc.esr_ohm,
            "self_resonance_hz": r.rlc.self_resonance_hz(),
            "layer": p.layer,
        })
    return out
