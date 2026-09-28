"""What is routed under a part: shared by the crystal, ESD clamp and antenna checks.

Three parts want the area beneath them left alone, for different reasons. A crystal's
oscillator picks up and sprays whatever passes under it. An ESD clamp carries amps of
discharge current for a nanosecond, and a line routed under it gets a share of that. An
antenna radiates into, and is detuned by, any copper near it at all.

The first two only care about signals, and a ground plane between the trace and the part
screens it. An antenna cares about all copper, ground included, on every layer.
"""

from __future__ import annotations

from ..kicad.geometry import point_in_ring
from .model import RuleContext, classify_net
from .planes import ground_planes

Box = tuple[float, float, float, float]


def pad_box(pads, margin: float = 0.0) -> Box:
    """The box around a part's pads (their copper, not their centres), grown by margin."""
    pts = [q for p in pads for q in (p.ring or [(p.x, p.y)])]
    return (min(q[0] for q in pts) - margin, min(q[1] for q in pts) - margin,
            max(q[0] for q in pts) + margin, max(q[1] for q in pts) + margin)


def segment_hits_box(x0, y0, x1, y1, bx0, by0, bx1, by1) -> bool:
    """Liang-Barsky: does the segment enter the box?"""
    t0, t1 = 0.0, 1.0
    dx, dy = x1 - x0, y1 - y0
    for p, q in ((-dx, x0 - bx0), (dx, bx1 - x0), (-dy, y0 - by0), (dy, by1 - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            r = q / p
            if p < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                return False
    return True


def _cross(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segment_hits_ring(x0, y0, x1, y1, ring: list[tuple[float, float]]) -> bool:
    """Does the segment enter the polygon: an end inside, or a crossing of its edge."""
    if point_in_ring(x0, y0, ring) or point_in_ring(x1, y1, ring):
        return True
    n = len(ring)
    for i in range(n):
        (ax, ay), (bx, by) = ring[i], ring[(i + 1) % n]
        d1, d2 = _cross(x0, y0, x1, y1, ax, ay), _cross(x0, y0, x1, y1, bx, by)
        d3, d4 = _cross(ax, ay, bx, by, x0, y0), _cross(ax, ay, bx, by, x1, y1)
        if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
            return True
    return False


def box_ring(box: Box) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = box
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def rings_overlap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    """Do two polygons share any area: a vertex of one inside the other, or edges crossing."""
    if any(point_in_ring(x, y, b) for x, y in a) or any(point_in_ring(x, y, a) for x, y in b):
        return True
    n = len(a)
    return any(segment_hits_ring(*a[i], *a[(i + 1) % n], b) for i in range(n))


def shielded(ctx: RuleContext, layer: str, side: set[str]) -> bool:
    """A ground plane lies between this layer and every layer the part sits on."""
    order = ctx.model.copper_layer_names
    if layer not in order or not side:
        return False
    grounds = set(ground_planes(ctx.model))
    ti = order.index(layer)
    return all(
        any(g in grounds and min(ti, order.index(s)) < order.index(g) < max(ti, order.index(s))
            for g in order)
        for s in side if s in order
    )


def signals_under(ctx: RuleContext, ring: list[tuple[float, float]], own: set[str],
                  side: set[str]) -> dict[str, str]:
    """net -> layer for signal tracks entering the area, unless a ground plane screens them.

    Only signals: a supply or ground under a crystal is how it is meant to be decoupled.
    """
    order = ctx.model.copper_layer_names
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
    is_box = len(ring) == 4 and len(set(xs)) == 2 and len(set(ys)) == 2
    screened: dict[str, bool] = {}
    under: dict[str, str] = {}
    for t in ctx.model.tracks:
        if not t.net or t.net in own or t.net in under or classify_net(t.net) != "signal":
            continue
        if t.layer not in order:
            continue
        if t.layer not in screened:
            screened[t.layer] = shielded(ctx, t.layer, side)
        if screened[t.layer]:
            continue
        for (x0, y0), (x1, y1) in zip(t.pts, t.pts[1:]):
            if not segment_hits_box(x0, y0, x1, y1, bx0, by0, bx1, by1):
                continue
            if is_box or segment_hits_ring(x0, y0, x1, y1, ring):
                under[t.net] = t.layer
                break
    return under
