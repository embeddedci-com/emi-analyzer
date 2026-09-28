"""Copper under an antenna.

An antenna is tuned for the copper around it. A trace underneath couples straight into it
(noise in, and the trace's own signal radiated out), and a plane underneath detunes it and
reflects its field back, so the radiated power swings with frequency and direction. Module
makers all say the same thing: nothing under the antenna, on any layer, and a plane under
it counts. Ground included, which is the difference from the crystal check.

Three kinds of antenna are recognised:

  * A rule area in the part's own footprint. When the library drew one, it is the answer.
  * An RF module with a printed antenna (ESP32-WROOM, -MINI and the like). Its pads cover
    one end of the module and the antenna the other, so the antenna is the pad-free strip
    at one end of the body. Variants with a U.FL connector instead (-32U, -1U) have none.
  * A separate antenna part (AE1, ANT1, a chip antenna): its body, grown by a margin.
"""

from __future__ import annotations

import re
from typing import Iterator

from ..kicad import geometry as g
from ..kicad.geometry import point_in_ring
from .model import Finding, RuleContext
from .planes import severity
from .under import box_ring, pad_box, rings_overlap, segment_hits_ring

RULE = "antenna"

ANT_REF = re.compile(r"^(AE|ANT)\d", re.I)
ANT_HINT = re.compile(r"antenna|chip_?ant\b|pcb_?ant\b", re.I)
#: Modules that carry their own printed antenna.
MODULE_HINT = re.compile(
    r"wroom|wrover|esp32-?[a-z]\d*-?mini|esp-?(?:01|07|12)|esp8266|nina-[bw]|bgm\d|mgm\d|"
    r"bmd-?\d|mdbt\d|xbee|rn487|e73-|holyiot|rak\d{4}|stm32wb.*mod|cyw\d+.*mod|wl18",
    re.I,
)
#: A connector for an external antenna instead: ESP32-WROOM-32U, ESP32-C3-MINI-1U.
EXTERNAL_HINT = re.compile(r"-\d+U\b|-\d+U-|mini-1u|u\.?fl|ipex|mhf", re.I)


class _Antenna:
    def __init__(self, ref: str, area: list[tuple[float, float]], own: set[str], how: str):
        self.ref = ref
        self.area = area
        #: Nets whose tracks may enter: a chip antenna's feed. A module's antenna strip has
        #: no pads in it, so nothing is exempt there.
        self.own = own
        self.how = how


def _strip(fp, pads, min_strip: float, margin: float):
    """The pad-free end of a module's body, in the board frame, or None."""
    if fp.local_bbox is None:
        return None
    x0, y0, x1, y1 = fp.local_bbox
    local = []
    for p in pads:
        for q in (p.ring or [(p.x, p.y)]):
            local.append(g.rotate(q[0] - fp.x, q[1] - fp.y, -fp.rotation))
    if not local:
        return None
    px0, py0 = min(q[0] for q in local), min(q[1] for q in local)
    px1, py1 = max(q[0] for q in local), max(q[1] for q in local)
    # Each side's strip: the body beyond the pads, grown outward (not inward, into the pads)
    # by the margin.
    sides = [
        (py0 - y0, (x0 - margin, y0 - margin, x1 + margin, py0)),
        (y1 - py1, (x0 - margin, py1, x1 + margin, y1 + margin)),
        (px0 - x0, (x0 - margin, y0 - margin, px0, y1 + margin)),
        (x1 - px1, (px1, y0 - margin, x1 + margin, y1 + margin)),
    ]
    depth, box = max(sides, key=lambda s: s[0])
    if depth < min_strip:
        return None
    return fp.ring(box)


def find_antennas(ctx: RuleContext) -> list[_Antenna]:
    margin = float(ctx.setting(RULE, "margin_mm") or 0.0)
    min_strip = float(ctx.setting(RULE, "min_strip_mm") or 3.0)
    pads_by_ref: dict[str, list] = {}
    for p in ctx.model.pads:
        if p.ref:
            pads_by_ref.setdefault(p.ref, []).append(p)
    keepouts: dict[str, list] = {}
    for k in ctx.model.keepouts:
        if k.ref and (k.no_tracks or k.no_pour):
            keepouts.setdefault(k.ref, []).append(k)

    out: list[_Antenna] = []
    for fp in ctx.model.footprints:
        if not fp.ref:
            continue
        hint = f"{fp.footprint} {fp.value}"
        pads = pads_by_ref.get(fp.ref, [])
        is_part = bool(ANT_REF.match(fp.ref) or ANT_HINT.search(hint))
        is_module = bool(MODULE_HINT.search(hint)) and not is_part
        if not (is_part or is_module) or EXTERNAL_HINT.search(hint):
            continue
        if fp.ref in keepouts:
            for k in keepouts[fp.ref]:
                out.append(_Antenna(fp.ref, k.ring, set(), "its footprint's keep-out area"))
            continue
        if is_module:
            area = _strip(fp, pads, min_strip, margin)
            if area:
                out.append(_Antenna(fp.ref, area, set(), "the end of the module with no pads"))
            continue
        if fp.local_bbox is not None:
            x0, y0, x1, y1 = fp.local_bbox
            area = fp.ring((x0 - margin, y0 - margin, x1 + margin, y1 + margin))
        elif pads:
            area = box_ring(pad_box(pads, margin))
        else:
            continue
        out.append(_Antenna(fp.ref, area, {p.net for p in pads if p.net}, "its body"))
    return out


def _coverage(area, rings) -> float:
    """Fraction of the area covered by any of the rings, by sampling a grid over it."""
    xs = [p[0] for p in area]
    ys = [p[1] for p in area]
    ax0, ay0, ax1, ay1 = min(xs), min(ys), max(xs), max(ys)
    near = []
    for r in rings:
        rx = [p[0] for p in r]
        ry = [p[1] for p in r]
        if max(rx) >= ax0 and min(rx) <= ax1 and max(ry) >= ay0 and min(ry) <= ay1:
            near.append(r)
    if not near:
        return 0.0
    n = 16
    inside = hit = 0
    for i in range(n):
        for j in range(n):
            x = ax0 + (i + 0.5) * (ax1 - ax0) / n
            y = ay0 + (j + 0.5) * (ay1 - ay0) / n
            if not point_in_ring(x, y, area):
                continue
            inside += 1
            if any(point_in_ring(x, y, r) for r in near):
                hit += 1
    return hit / inside if inside else 0.0


def check_antennas(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled(RULE):
        return
    min_pour = float(ctx.setting(RULE, "min_pour_pct") or 0.0) / 100.0
    order = ctx.model.copper_layer_names
    for ant in find_antennas(ctx):
        cx = sum(p[0] for p in ant.area) / len(ant.area)
        cy = sum(p[1] for p in ant.area) / len(ant.area)
        x, y = ctx.pt(cx, cy)

        crossing: dict[str, str] = {}
        for t in ctx.model.tracks:
            if not t.net or t.net in ant.own or t.net in crossing:
                continue
            if any(segment_hits_ring(*a, *b, ant.area) for a, b in zip(t.pts, t.pts[1:])):
                crossing[t.net] = t.layer
        vias = [v for v in ctx.model.vias if v.net not in ant.own and point_in_ring(v.x, v.y, ant.area)]
        for v in vias:
            crossing.setdefault(v.net or "unconnected", "via")
        if crossing:
            nets = sorted(crossing)
            shown = ", ".join(nets[:4]) + (f" and {len(nets) - 4} more" if len(nets) > 4 else "")
            layers = sorted({l for l in crossing.values() if l != "via"},
                            key=lambda l: order.index(l) if l in order else 0)
            where = ", ".join(layers) or "vias"
            yield Finding(
                rule=RULE,
                severity=severity(ctx, RULE, "warning"),
                title=f"{shown} {'runs' if len(nets) == 1 else 'run'} under the antenna of {ant.ref}",
                detail=(
                    f"The antenna area of {ant.ref} ({ant.how}) has copper from {shown} in it, on "
                    f"{where}. A trace under an antenna couples into it both ways: its noise lands "
                    f"in the receiver and its own signal is radiated. Route around the antenna, "
                    f"on every layer, or move the antenna so it overhangs the board edge."
                ),
                action="Keep every layer clear under the antenna.",
                net=nets[0], layer=layers[0] if layers else "", x=x, y=y,
            )

        by_layer: dict[str, list] = {}
        nets_by_layer: dict[str, set[str]] = {}
        for z in ctx.model.zones:
            if not z.net or z.net in ant.own or not rings_overlap(z.ring, ant.area):
                continue
            by_layer.setdefault(z.layer, []).append(z.ring)
            nets_by_layer.setdefault(z.layer, set()).add(z.net)
        for layer in sorted(by_layer, key=lambda l: order.index(l) if l in order else 0):
            frac = _coverage(ant.area, by_layer[layer])
            if frac <= 0 or frac < min_pour:
                continue
            nets = ", ".join(sorted(nets_by_layer[layer]))
            yield Finding(
                rule=RULE,
                severity=severity(ctx, RULE, "warning"),
                title=f"{nets} pour covers {frac * 100:.0f}% of the antenna of {ant.ref} on {layer}",
                detail=(
                    f"The antenna area of {ant.ref} ({ant.how}) has {nets} copper poured under "
                    f"{frac * 100:.0f}% of it on {layer}. A plane near an antenna detunes it and "
                    f"reflects its field back onto it, so the radiated power rises and falls with "
                    f"frequency, and it matters as much for ground as for any other net. Cut the "
                    f"pour back from the antenna area on every layer."
                ),
                action="Cut the pour back from the antenna area on every layer.",
                net=sorted(nets_by_layer[layer])[0], layer=layer, x=x, y=y,
            )
