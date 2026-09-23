"""Which connectors get which cable (§5).

The suggestion is a starting point, never a decision: the result names what it guessed and
from what, and an unassigned connector is *not modelled at all* rather than modelled with a
default. §5's rule is that an unassigned connector is named and compliance treats it as
incomplete — a cable the user did not ask for is exactly the kind of invisible assumption this
tool exists not to make.

**Recognition is reused, not reinvented.** `rules/emc.py` already decides what a connector is,
and it already knows that `SMA_L4.3-W2.6-LS5.2-RD` on a real board is a DO-214AC diode package
rather than an SMA jack. Writing a second answer to that question is how the two drift and how
a diode ends up with a USB cable attached to it (§19, cable test 5).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from emi_worker.cables.library import Cable, built_in


@dataclass(frozen=True)
class Suggestion:
    ref: str
    cable_id: str | None
    #: What the guess was made from, quoted so a wrong one is visible rather than mysterious.
    reason: str
    footprint: str
    matched_hint: str | None = None

    @property
    def assigned(self) -> bool:
        return self.cable_id is not None


def _hint_matches(hint: str, text: str) -> bool:
    """A hint matches on a word boundary, not anywhere in the string.

    Substring matching is what puts a USB cable on a `USBLC6` ESD diode and a coax pigtail on
    anything containing "sma". The boundary is doing real work here.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])", text, re.I) is not None


def suggest_for(ref: str, footprint: str, value: str = "") -> Suggestion:
    """The cable this connector most likely carries, or none when nothing fits."""
    text = f"{footprint} {value}".lower()
    best: tuple[int, Cable, str] | None = None
    for cable in built_in().values():
        for hint in cable.connector_hints:
            if not _hint_matches(hint, text):
                continue
            # The longest matching hint wins: "type-c" is more specific than "usb", and a
            # board carrying both should get the more specific answer.
            if best is None or len(hint) > best[0]:
                best = (len(hint), cable, hint)
    if best is None:
        return Suggestion(ref=ref, cable_id=None, footprint=footprint,
                          reason=f"nothing in the library matches {footprint!r}")
    _, cable, hint = best
    return Suggestion(
        ref=ref, cable_id=cable.id, footprint=footprint, matched_hint=hint,
        reason=f"{footprint!r} contains {hint!r}",
    )


def suggest_all(model) -> list[Suggestion]:
    """One suggestion per connector on the board, in reference order.

    Parts that are not connectors are not considered at all — including the ones whose
    footprint *names* a connector, which is the case cable test 5 is about.
    """
    from emi_worker.rules.emc import _is_connector

    by_ref: dict[str, list] = {}
    for pad in model.pads:
        if pad.ref:
            by_ref.setdefault(pad.ref, []).append(pad)

    out = []
    for ref, pads in sorted(by_ref.items()):
        if not _is_connector(ref, pads):
            continue
        out.append(suggest_for(ref, pads[0].footprint or "", pads[0].value or ""))
    return out


# ---- where a cable actually leaves the board (§5, §7) -----------------------------------

@dataclass(frozen=True)
class Anchor:
    """Where a connector's cable leaves, and which way it goes.

    ``exit_normal`` points **out of the board**, away from the nearest outline edge. It is the
    direction the cable root runs in, and getting it backwards would put the stub inside the
    board — where it would couple to everything and radiate nothing.
    """

    ref: str
    #: The point on the board edge the cable leaves from, in board millimetres.
    x_mm: float
    y_mm: float
    #: Unit vector, outward.
    nx: float
    ny: float
    #: How close this connector's **copper** gets to the outline — the nearest pad, not the
    #: centroid. A big through-hole jack has a centroid well back from the edge while its
    #: nearest pin nearly touches it: measured on a real board, an RJ45 is 14 mm from the edge by
    #: centroid and 2 mm by pad, and only the second answers "is this an edge connector".
    distance_to_edge_mm: float
    #: The width of the connector across the exit direction, which sets the stub's width.
    width_mm: float

    @property
    def on_edge(self) -> bool:
        return self.distance_to_edge_mm <= EDGE_TOLERANCE_MM


#: How close a connector's nearest pad must come to the outline to count as an edge connector.
#:
#: Set from the four real boards rather than picked: every genuine edge connector on them is
#: within 9 mm (a real board's through-hole USB-A is the furthest at 8.8, its pins set back behind
#: the jack body), and every mid-board header is beyond 18 (one board's H1-H3, at 19 to 29).
#: 12 mm sits in the gap with room on both sides.
EDGE_TOLERANCE_MM = 12.0


def _segments(outline: list[list[tuple[float, float]]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    out = []
    for ring in outline:
        for a, b in zip(ring, list(ring[1:]) + [ring[0]]):
            if a != b:
                out.append((a, b))
    return out


def _closest_point_on_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 0:
        return ax, ay, math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    cx, cy = ax + t * dx, ay + t * dy
    return cx, cy, math.hypot(px - cx, py - cy)


def anchor_for(ref: str, pads: list, transform, outline: list[list[tuple[float, float]]]) -> Anchor | None:
    """Where this connector's cable leaves the board, or None when there is no outline.

    The exit point is the nearest point on the board outline and the normal points away from
    the connector towards it — outward by construction, without needing to know which way the
    outline was wound. An outline whose winding is unknown is the usual case in a board file,
    and guessing it is how a stub ends up pointing inwards.
    """
    if not outline or not pads:
        return None
    pts = [transform.pt(px, py) for pad in pads for px, py in (pad.ring or [])]
    if not pts:
        pts = [transform.pt(pad.x, pad.y) for pad in pads]
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)

    board_segments = _segments([[transform.pt(x, y) for x, y in ring] for ring in outline])
    if not board_segments:
        return None

    # Direction from the centroid: a single point gives one unambiguous outward direction,
    # where the nearest pad would give whichever pin happens to sit closest to a corner.
    best = min(
        (_closest_point_on_segment(cx, cy, a[0], a[1], b[0], b[1]) for a, b in board_segments),
        key=lambda r: r[2],
    )
    ex, ey, _centroid_distance = best

    # Distance from the nearest pad: that is what decides whether a cable leaves here.
    distance = min(
        _closest_point_on_segment(px, py, a[0], a[1], b[0], b[1])[2]
        for px, py in pts
        for a, b in board_segments
    )
    dx, dy = ex - cx, ey - cy
    norm = math.hypot(dx, dy)
    if norm < 1e-9:
        # The connector centre is exactly on the edge, so there is no direction to take from
        # it. Fall back to the segment's own normal rather than inventing one.
        (ax, ay), (bx, by) = min(
            board_segments,
            key=lambda s: _closest_point_on_segment(cx, cy, s[0][0], s[0][1], s[1][0], s[1][1])[2],
        )
        sx, sy = bx - ax, by - ay
        slen = math.hypot(sx, sy) or 1.0
        nx, ny = -sy / slen, sx / slen
    else:
        nx, ny = dx / norm, dy / norm

    # Width across the exit direction: the span of the pads perpendicular to the normal.
    across = [(-ny) * p[0] + nx * p[1] for p in pts]
    width = max(across) - min(across)

    return Anchor(ref=ref, x_mm=ex, y_mm=ey, nx=nx, ny=ny,
                  distance_to_edge_mm=distance, width_mm=max(width, 0.5))


def anchors_for_board(model, transform) -> dict[str, Anchor]:
    """Anchors for every connector the board carries, keyed by reference."""
    from emi_worker.rules.emc import _is_connector

    by_ref: dict[str, list] = {}
    for pad in model.pads:
        if pad.ref:
            by_ref.setdefault(pad.ref, []).append(pad)

    out: dict[str, Anchor] = {}
    for ref, pads in sorted(by_ref.items()):
        if not _is_connector(ref, pads):
            continue
        anchor = anchor_for(ref, pads, transform, model.outline)
        if anchor is not None:
            out[ref] = anchor
    return out
