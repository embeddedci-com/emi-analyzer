"""Thin necks in power copper.

A supply that is poured, or routed wide, and then squeezed through a thin trace on its way to
the load drops its voltage across that trace, heats it, and adds its inductance to every
current step the load draws, which is where a rail's noise comes from. A board file says
nothing about how much current a net carries, so without being told the check looks for the
shape: a thin run of track that is the only copper joining two pieces of wide copper (pours,
or tracks several times wider) on the same supply. A thin branch out to one IC pin is not a
neck, and neither is a thin trace alongside a wide one.

When a net group gives a net its current, every trace on it is also held to the IPC-2221
width for that current at the allowed temperature rise.
"""

from __future__ import annotations

import math
from typing import Iterator

from ..kicad.geometry import point_in_ring, point_segment_distance
from .model import Finding, RuleContext, classify_net
from .planes import severity

RULE = "power-neck"

#: Copper resistivity at 20 °C, ohm metres.
RHO_CU = 1.72e-8
#: IPC-2221 constants: I = k * dT^0.44 * A^0.725, A in square mils.
K_OUTER = 0.048
K_INNER = 0.024
MIL_PER_MM = 1000 / 25.4

JOIN_MM = 0.05


class _Union:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def join(self, a: int, b: int) -> None:
        self.p[self.find(a)] = self.find(b)


def _near_ring(ring, x: float, y: float, reach: float) -> bool:
    if point_in_ring(x, y, ring):
        return True
    n = len(ring)
    return any(point_segment_distance(x, y, *ring[i], *ring[(i + 1) % n]) <= reach for i in range(n))


def ipc2221_width_mm(current_a: float, rise_c: float, thickness_mm: float, outer: bool) -> float:
    """Trace width IPC-2221 asks for to carry a current at a temperature rise."""
    k = K_OUTER if outer else K_INNER
    area_mil2 = (current_a / (k * rise_c ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (thickness_mm * MIL_PER_MM) / MIL_PER_MM


def _thickness(ctx: RuleContext, layer: str) -> float:
    le = ctx.electrics.layer(layer) if ctx.electrics else None
    return (le.copper_thickness_mm if le else 0.0) or 0.035


def _items(ctx: RuleContext, net: str):
    """Every piece of copper on the net: (kind, layers, payload)."""
    order = ctx.model.copper_layer_names
    items = []
    for t in ctx.model.tracks:
        if t.net == net and len(t.pts) >= 2:
            for a, b in zip(t.pts, t.pts[1:]):
                items.append(("track", {t.layer}, (a, b, t.width_mm, t.layer)))
    for z in ctx.model.zones:
        if z.net == net and len(z.ring) >= 3:
            items.append(("zone", {z.layer}, z))
    for v in ctx.model.vias:
        if v.net == net:
            items.append(("via", set(v.layers) if v.kind != "through" else set(order), v))
    for p in ctx.model.pads:
        if p.net == net:
            items.append(("pad", set(order) if p.is_through else set(p.layers), p))
    return items


def _touch(a, b) -> bool:
    ka, la, pa = a
    kb, lb, pb = b
    if not (la & lb):
        return False
    if ka == "zone" and kb == "zone":
        return False  # two pours of one net only meet through something else
    if kb == "track" and ka != "track":
        a, b = b, a
        ka, la, pa, kb, lb, pb = kb, lb, pb, ka, la, pa
    if ka == "track":
        (x0, y0), (x1, y1), w, _ = pa
        ends = ((x0, y0), (x1, y1))
        if kb == "track":
            (u0, v0), (u1, v1), w2, _ = pb
            reach = (w + w2) / 2 + JOIN_MM
            return (any(point_segment_distance(x, y, u0, v0, u1, v1) <= reach for x, y in ends)
                    or any(point_segment_distance(x, y, x0, y0, x1, y1) <= reach for x, y in ((u0, v0), (u1, v1))))
        if kb == "zone":
            return any(_near_ring(pb.ring, x, y, w / 2 + JOIN_MM) for x, y in ends)
        if kb == "via":
            return point_segment_distance(pb.x, pb.y, x0, y0, x1, y1) <= (w + pb.size_mm) / 2 + JOIN_MM
        if kb == "pad":
            return (any(point_in_ring(x, y, pb.ring) for x, y in ends if pb.ring)
                    or point_segment_distance(pb.x, pb.y, x0, y0, x1, y1) <= w / 2 + JOIN_MM)
    if {ka, kb} == {"zone", "via"} or {ka, kb} == {"zone", "pad"}:
        z, o = (pa, pb) if ka == "zone" else (pb, pa)
        reach = getattr(o, "size_mm", 0.0) / 2 + JOIN_MM
        return _near_ring(z.ring, o.x, o.y, max(reach, 0.3))
    if ka in ("via", "pad") and kb in ("via", "pad"):
        return math.dist((pa.x, pa.y), (pb.x, pb.y)) <= 0.1
    return False


def _bbox(item):
    kind, _, p = item
    if kind == "track":
        (x0, y0), (x1, y1), w, _ = p
        return min(x0, x1) - w, min(y0, y1) - w, max(x0, x1) + w, max(y0, y1) + w
    if kind == "zone":
        xs = [q[0] for q in p.ring]
        ys = [q[1] for q in p.ring]
        return min(xs), min(ys), max(xs), max(ys)
    r = max(getattr(p, "size_mm", 0.0), 1.0)
    if kind == "pad" and p.ring:
        xs = [q[0] for q in p.ring]
        ys = [q[1] for q in p.ring]
        return min(xs), min(ys), max(xs), max(ys)
    return p.x - r, p.y - r, p.x + r, p.y + r


def _overlaps(a, b, pad: float = 0.5) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


def _necks(ctx: RuleContext, net: str, ratio: float, min_wide: float):
    items = _items(ctx, net)
    if not any(k == "track" for k, _, _ in items):
        return []
    widest = max((p[2] for k, _, p in items if k == "track"), default=0.0)
    has_zone = any(k == "zone" for k, _, _ in items)
    if not has_zone and widest < min_wide:
        return []

    def width(i):
        return items[i][2][2] if items[i][0] == "track" else math.inf

    # "Thin" is relative: a track is thin when the widest copper on the net is ratio times
    # wider. The comparison that decides a neck is made against its own two sides below.
    cap = math.inf if has_zone else widest
    thin = [i for i, (k, _, p) in enumerate(items)
            if k == "track" and p[2] * ratio <= cap and p[2] < min_wide]
    if not thin:
        return []
    thin_set = set(thin)
    boxes = [_bbox(it) for it in items]
    n = len(items)
    rest = _Union(n)
    chains = _Union(n)
    for i in range(n):
        for j in range(i + 1, n):
            if not _overlaps(boxes[i], boxes[j]) or not _touch(items[i], items[j]):
                continue
            ti, tj = i in thin_set, j in thin_set
            if ti and tj:
                chains.join(i, j)
            elif not ti and not tj:
                rest.join(i, j)
    # Which rest-components each thin chain touches.
    touching: dict[int, set[int]] = {}
    for i in thin:
        for j in range(n):
            if j in thin_set or not _overlaps(boxes[i], boxes[j]) or not _touch(items[i], items[j]):
                continue
            touching.setdefault(chains.find(i), set()).add(rest.find(j))
    # The widest copper in each rest-component: a pour counts as unlimited.
    side_width: dict[int, float] = {}
    for j in range(n):
        if j in thin_set:
            continue
        r = rest.find(j)
        w = math.inf if items[j][0] == "zone" else (width(j) if items[j][0] == "track" else 0.0)
        side_width[r] = max(side_width.get(r, 0.0), w)

    out = []
    for chain, sides in touching.items():
        wide = sorted((side_width.get(s, 0.0) for s in sides), reverse=True)
        members = [i for i in thin if chains.find(i) == chain]
        w = min(items[i][2][2] for i in members)
        # A neck is narrower than "wide" itself: next to a pour every trace is ratio times
        # narrower, and a 1.45 mm trace between two 5 V pours on a real board is not a problem
        # anyone would fix without knowing the current (which is what current_a is for).
        if len(wide) < 2 or wide[1] < min_wide or w * ratio > wide[1] or w >= min_wide:
            continue
        length = sum(math.dist(items[i][2][0], items[i][2][1]) for i in members)
        mid = min(members, key=lambda i: items[i][2][2])
        (x0, y0), (x1, y1), _, layer = items[mid][2]
        out.append((w, length, layer, ((x0 + x1) / 2, (y0 + y1) / 2), wide[:2]))
    return out


def _side(w: float) -> str:
    return "a pour" if math.isinf(w) else f"a {w:.2f} mm track"


def check_power_necks(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled(RULE):
        return
    order = ctx.model.copper_layer_names
    nets = sorted({t.net for t in ctx.model.tracks if t.net and classify_net(t.net) == "power"})
    for net in nets:
        ratio = float(ctx.setting(RULE, "neck_ratio", net=net) or 4.0)
        min_wide = float(ctx.setting(RULE, "min_wide_mm", net=net) or 1.0)
        for w, length, layer, (mx, my), (a, b) in _necks(ctx, net, ratio, min_wide):
            t = _thickness(ctx, layer)
            r_mohm = RHO_CU * (length / 1000) / ((w / 1000) * (t / 1000)) * 1000
            x, y = ctx.pt(mx, my)
            yield Finding(
                rule=RULE,
                severity=severity(ctx, RULE, "warning"),
                title=f"{net} necks down to {w:.2f} mm between {_side(a)} and {_side(b)}",
                detail=(
                    f"On {layer}, {length:.1f} mm of {w:.2f} mm track is the only copper joining two "
                    f"wide parts of {net}, so all the current between them goes through it. That is "
                    f"about {r_mohm:.0f} mΩ, {r_mohm:.0f} mV lost per amp, heat in the trace, and "
                    f"inductance in series with every current step the load draws. Widen the trace "
                    f"to match the copper either side, or pour it. If the net only carries a few "
                    f"milliamps, suppress the finding for it."
                ),
                action="Widen the trace to match the copper on either side, or pour it.",
                net=net, layer=layer, x=x, y=y,
            )

        current = float(ctx.setting(RULE, "current_a", net=net) or 0.0)
        if current <= 0:
            continue
        rise = float(ctx.setting(RULE, "max_rise_c", net=net) or 10.0)
        worst = None
        for t in ctx.model.tracks:
            if t.net != net or len(t.pts) < 2:
                continue
            outer = t.layer in (order[0], order[-1]) if order else True
            need = ipc2221_width_mm(current, rise, _thickness(ctx, t.layer), outer)
            if t.width_mm < need and (worst is None or t.width_mm / need < worst[0].width_mm / worst[1]):
                worst = (t, need)
        if worst is None:
            continue
        t, need = worst
        (x0, y0), (x1, y1) = t.pts[0], t.pts[1]
        x, y = ctx.pt((x0 + x1) / 2, (y0 + y1) / 2)
        yield Finding(
            rule=RULE,
            severity=severity(ctx, RULE, "warning"),
            title=f"{net} carries {current:g} A on a {t.width_mm:.2f} mm trace; it needs {need:.2f} mm",
            detail=(
                f"The narrowest trace on {net} relative to what it has to carry is {t.width_mm:.2f} mm "
                f"on {t.layer}. For {current:g} A with at most {rise:g} °C of rise, IPC-2221 asks "
                f"for {need:.2f} mm of {_thickness(ctx, t.layer) * 1000:.0f} µm copper. IPC-2221 is "
                f"conservative for short runs next to a plane, but a trace this far under it runs hot "
                f"and drops voltage. Widen it."
            ),
            action="Widen the trace for the current it carries.",
            net=net, layer=t.layer, x=x, y=y,
        )
