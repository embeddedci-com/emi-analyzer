"""Layout checks that do not fit the return-path family: copper islands, crystals, stackup.

Each is cheap and each catches something a schematic review cannot:

  * Floating copper -- a pour island connected to nothing -- is an antenna with no ground to
    drain into.
  * A crystal is the board's loudest, most sensitive clock source. Signals routed beneath it
    couple straight into its oscillator, and one placed near an edge or a connector radiates
    or picks up through the cable.
  * A signal layer with no plane beside it has no defined return path at all, and two signal
    layers stacked with nothing between them couple broadside along every parallel run.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Iterator

import numpy as np

from ..kicad.geometry import point_in_ring, point_segment_distance, ring_area
from .model import Finding, RuleContext, classify_net
from .planes import ground_planes, plane_layers, severity

XTAL_REF = re.compile(r"^(Y|X|XTAL|XO|OSC)\d", re.I)
XTAL_HINT = re.compile(r"crystal|xtal|oscillat|resonator|\d\s*[mk]hz", re.I)
CONN_REF = re.compile(r"^(J|P|CN|CON|USB)\d", re.I)


# ---------------------------------------------------------------------------------------
# Copper islands
# ---------------------------------------------------------------------------------------

def _near_ring(ring: list[tuple[float, float]], x: float, y: float, reach: float) -> bool:
    """Inside the pour, or close enough to its boundary to be joined to it.

    Close enough matters: a pad inside a pour sits in a thermal-relief void, joined only by
    spokes, so its centre is *outside* the poured copper. Testing the centre alone would call
    every thermally relieved pour an island.
    """
    if point_in_ring(x, y, ring):
        return True
    r = np.asarray(ring)
    if len(r) < 2:
        return False
    a, b = r, np.roll(r, -1, axis=0)
    ab = b - a
    L2 = (ab ** 2).sum(axis=1)
    L2[L2 == 0] = 1e-12
    t = np.clip(((x - a[:, 0]) * ab[:, 0] + (y - a[:, 1]) * ab[:, 1]) / L2, 0, 1)
    q = a + ab * t[:, None]
    return bool((np.hypot(q[:, 0] - x, q[:, 1] - y) <= reach).any())


def check_copper_islands(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled("copper-island"):
        return
    min_area = float(ctx.setting("copper-island", "min_area_mm2") or 2.0)
    copper = set(ctx.model.copper_layer_names)

    # Where each (layer, net) connects to something: vias, pads, track ends.
    joins: dict[tuple[str, str], list[tuple[float, float, float]]] = defaultdict(list)
    for v in ctx.model.vias:
        if not v.net:
            continue
        for layer in (copper if v.kind == "through" else set(v.layers) & copper):
            joins[(layer, v.net)].append((v.x, v.y, v.size_mm / 2 + 0.6))
    for p in ctx.model.pads:
        if not p.net:
            continue
        layers = copper if (p.is_through or "*.Cu" in p.layers) else set(p.layers) & copper
        if p.ring:
            xs = [q[0] for q in p.ring]
            ys = [q[1] for q in p.ring]
            radius = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 2
        else:
            radius = 0.5
        for layer in layers:
            joins[(layer, p.net)].append((p.x, p.y, radius + 0.6))
    for t in ctx.model.tracks:
        if t.net and t.pts:
            for x, y in (t.pts[0], t.pts[-1]):
                joins[(t.layer, t.net)].append((x, y, t.width_mm / 2 + 0.3))

    for z in ctx.model.zones:
        area = ring_area(z.ring)
        if area < min_area or len(z.ring) < 3:
            continue
        if z.net:
            xs = [q[0] for q in z.ring]
            ys = [q[1] for q in z.ring]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            connected = any(
                x0 - r <= jx <= x1 + r and y0 - r <= jy <= y1 + r and _near_ring(z.ring, jx, jy, r)
                for jx, jy, r in joins.get((z.layer, z.net), ())
            )
            if connected:
                continue
        cx = sum(q[0] for q in z.ring) / len(z.ring)
        cy = sum(q[1] for q in z.ring) / len(z.ring)
        x, y = ctx.pt(cx, cy)
        what = f"a {z.net} pour" if z.net else "a pour on no net"
        yield Finding(
            rule="copper-island",
            severity=severity(ctx, "copper-island", "warning"),
            title=f"{area:.0f} mm² copper island on {z.layer} is connected to nothing",
            detail=(
                f"This is {what}, but no via, pad or track joins it to the rest of the net. "
                f"Floating copper picks up and re-radiates whatever couples onto it, with no "
                f"path to drain it to ground. Remove it — KiCad's zone setting 'Remove islands' "
                f"does this on refill — or stitch it to ground with a via."
            ),
            net=z.net, layer=z.layer, x=x, y=y,
        )


# ---------------------------------------------------------------------------------------
# Crystals
# ---------------------------------------------------------------------------------------

def _segment_hits_box(x0, y0, x1, y1, bx0, by0, bx1, by1) -> bool:
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


def check_crystals(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled("crystal"):
        return
    min_edge = float(ctx.setting("crystal", "min_edge_mm") or 5.0)
    min_conn = float(ctx.setting("crystal", "min_connector_mm") or 10.0)
    margin = float(ctx.setting("crystal", "keepout_margin_mm") or 0.5)

    by_ref: dict[str, list] = defaultdict(list)
    for p in ctx.model.pads:
        if p.ref:
            by_ref[p.ref].append(p)

    order = ctx.model.copper_layer_names
    grounds = set(ground_planes(ctx.model))
    edges = [
        (*ring[i], *ring[i + 1]) for ring in ctx.model.outline for i in range(len(ring) - 1)
    ]
    connectors = [p for ref, pads in by_ref.items() if CONN_REF.match(ref) for p in pads]

    for ref, pads in sorted(by_ref.items()):
        hint = f"{getattr(pads[0], 'value', '')} {getattr(pads[0], 'footprint', '')}"
        if not (XTAL_REF.match(ref) or XTAL_HINT.search(hint)):
            continue
        pts = [q for p in pads for q in (p.ring or [(p.x, p.y)])]
        bx0 = min(q[0] for q in pts) - margin
        bx1 = max(q[0] for q in pts) + margin
        by0 = min(q[1] for q in pts) - margin
        by1 = max(q[1] for q in pts) + margin
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        x, y = ctx.pt(cx, cy)
        own = {p.net for p in pads}
        side = {l for p in pads for l in p.layers if l in order}

        # Signals under the crystal -- unless a ground plane lies between them and it.
        under: dict[str, str] = {}
        for t in ctx.model.tracks:
            if not t.net or t.net in own or classify_net(t.net) != "signal" or t.net in under:
                continue
            if t.layer in order and side:
                ti = order.index(t.layer)
                shielded = all(
                    any(g in grounds and min(ti, order.index(s)) < order.index(g) < max(ti, order.index(s))
                        for g in order)
                    for s in side if s in order
                )
                if shielded:
                    continue
            for (x0, y0), (x1, y1) in zip(t.pts, t.pts[1:]):
                if _segment_hits_box(x0, y0, x1, y1, bx0, by0, bx1, by1):
                    under[t.net] = t.layer
                    break
        for net, layer in sorted(under.items()):
            yield Finding(
                rule="crystal",
                severity=severity(ctx, "crystal", "warning"),
                title=f"{net} is routed under crystal {ref} on {layer}",
                detail=(
                    f"A signal passing beneath a crystal couples into its oscillator and picks "
                    f"up its harmonics. Keep the area under {ref} clear on every layer down to "
                    f"the first ground plane, and ideally fill it with ground."
                ),
                net=net, layer=layer, x=x, y=y,
            )

        if edges:
            d_edge = min(point_segment_distance(cx, cy, *e) for e in edges)
            if d_edge < min_edge:
                yield Finding(
                    rule="crystal",
                    severity=severity(ctx, "crystal", "warning"),
                    title=f"Crystal {ref} is {d_edge:.1f} mm from the board edge",
                    detail=(
                        f"The oscillator's field is not contained at the edge, so a crystal "
                        f"within {min_edge:g} mm of it radiates more and is more sensitive to "
                        f"what is outside the enclosure. Move it inward."
                    ),
                    x=x, y=y,
                )

        if connectors:
            nearest = min(connectors, key=lambda p: math.dist((cx, cy), (p.x, p.y)))
            d_conn = math.dist((cx, cy), (nearest.x, nearest.y))
            if d_conn < min_conn:
                yield Finding(
                    rule="crystal",
                    severity=severity(ctx, "crystal", "warning"),
                    title=f"Crystal {ref} is {d_conn:.1f} mm from connector {nearest.ref}",
                    detail=(
                        f"A cable plugged into {nearest.ref} is an antenna, and a clock source "
                        f"within {min_conn:g} mm of it drives that antenna directly. Keep "
                        f"crystals away from I/O."
                    ),
                    x=x, y=y,
                )


# ---------------------------------------------------------------------------------------
# Stackup
# ---------------------------------------------------------------------------------------

def check_stackup(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled("stackup"):
        return
    order = ctx.model.copper_layer_names
    planes = plane_layers(ctx.model)
    signal = defaultdict(int)
    for t in ctx.model.tracks:
        if t.net and classify_net(t.net) == "signal":
            signal[t.layer] += 1

    for layer in order:
        if layer in planes or not signal[layer]:
            continue
        elec = ctx.electrics.layer(layer) if ctx.electrics else None
        if elec is not None and not elec.reference_plane:
            two_layer = len(order) <= 2
            yield Finding(
                rule="stackup",
                severity=severity(ctx, "stackup", "warning" if two_layer else "critical"),
                title=f"Signals on {layer} have no reference plane",
                detail=(
                    f"{signal[layer]} signal track segments run on {layer}, but no plane is "
                    f"adjacent to it, so their return current has no defined path and spreads "
                    f"wherever it can. "
                    + ("On a two-layer board, pour ground on the other side under the signals."
                       if two_layer else
                       "Reorder the stackup so every signal layer sits next to a plane.")
                ),
                layer=layer,
            )

    if len(order) > 2:
        for a, b in zip(order, order[1:]):
            if a in planes or b in planes or not (signal[a] and signal[b]):
                continue
            yield Finding(
                rule="stackup",
                severity=severity(ctx, "stackup", "warning"),
                title=f"{a} and {b} are adjacent signal layers with no plane between",
                detail=(
                    f"Traces on {a} and {b} couple broadside wherever they run parallel. Route "
                    f"them orthogonally, or put a plane between the two layers."
                ),
                layer=a,
            )
