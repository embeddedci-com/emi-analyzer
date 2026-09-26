"""Cut a small part of a board out as its own model: one net (or a pair), over its planes.

A full-wave solve of a region costs what the region's *mesh* costs, and on a routed board the
mesh is set by every copper edge in it: the in-plane axes come out several times denser than
the preset because each neighbouring trace forces its own grid lines. A coupon keeps only what
the chosen net's current actually flows through and returns through:

* the net's own copper: tracks, arcs, vias, pads and pours;
* every pour (plane) under it, clipped to the coupon, whatever its net -- a power plane is as
  much a return path at RF as a ground plane is;
* ground copper inside the coupon: stitching vias, ground tracks and ground pads;
* the board outline and stackup, unchanged.

Everything else -- neighbouring signals, other parts' pins -- is left out, and the result says
how many nets that was. The coupon therefore answers "what does this net do over its return
path", not "what does it couple into its neighbours".

**Ports go at the net's ends**, the two pads furthest apart, both 50 ohm: the one on the part
with the most pins is excited, on the reasoning that a many-pin part is the driver and a
two-pin part is a series element or a load. With both ends terminated the structure empties
through its ports in a few round trips, which is what keeps the record short (see
``stages/small_part.py``). A net with one pad gets one port and an open far end.

The worker is handed the coupon's region and ports explicitly, like any solve; ``plan`` is how
the browser's choice is reproduced by scripts and tests, and ``extract`` is what the worker
does with a region it was given.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from ..kicad import geometry as g
from ..kicad.board import BoardModel, Pad, ZonePolygon
from ..rules.model import classify_net
from .model import Port

#: Copper kept around the net, in mm, never less than this.
MIN_MARGIN_MM = 3.0

#: ...and never less than this many heights of the dielectric between the net and its plane.
#: A microstrip's field is within a few percent of its infinite-plane value once the plane runs
#: three to five heights past the strip's edge; five is the conservative end. On a 1.5 mm
#: two-layer board that is 7.5 mm, on a 0.2 mm prepreg it is the 3 mm floor.
MARGIN_HEIGHTS = 5.0

#: The largest coupon side, in mm. Past this the cut is not a small part of the board, and the
#: record length a structure that size rings for is not what the budget was set for.
MAX_SIDE_MM = 60.0

#: A port's half-width, mm: the app's (``portPlacement.ts``), so a coupon planned here and one
#: planned in the browser are the same model. The browser has no pad sizes to fit it to; 0.4 mm
#: sits on any pad from an 0402 up, and the box has to span grid lines (``build_model`` checks).
PORT_HALF_WIDTH_MM = 0.2


#: Ground copper within this distance of the net is kept (stitching vias, ground pins and
#: coplanar ground), mm; further away it is left out. Two millimetres is several dielectric
#: heights on a multilayer board, which is where a return current leaves the plane for a via.
GROUND_NEAR_MM = 2.0


class CouponError(ValueError):
    """The coupon cannot be cut as asked. The message is shown to the user."""


@dataclass
class Coupon:
    nets: list[str]
    #: Board space, mm: (min_x, min_y, max_x, max_y).
    roi: tuple[float, float, float, float]
    ports: list[Port]
    #: ``ref.number`` of the pad under each port, in port order.
    port_pads: list[str] = field(default_factory=list)
    margin_mm: float = MIN_MARGIN_MM
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        x0, y0, x1, y1 = self.roi
        return {
            "nets": list(self.nets),
            "roi_mm": [round(v, 4) for v in self.roi],
            "size_mm": [round(x1 - x0, 3), round(y1 - y0, 3)],
            "margin_mm": round(self.margin_mm, 3),
            "ports": [
                {"name": p.name, "x_mm": round(p.x, 4), "y_mm": round(p.y, 4), "layer": p.layer,
                 "reference_layer": p.reference_layer, "excited": p.excited, "pad": pad}
                for p, pad in zip(self.ports, self.port_pads)
            ],
        }


def _net_points(board: BoardModel, transform, nets: set[str]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for t in board.tracks:
        if t.net in nets:
            half = t.width_mm / 2.0
            for x, y in t.pts:
                bx, by = transform.pt(x, y)
                pts += [(bx - half, by - half), (bx + half, by + half)]
    for v in board.vias:
        if v.net in nets:
            r = (v.size_mm or v.drill_mm) / 2.0
            bx, by = transform.pt(v.x, v.y)
            pts += [(bx - r, by - r), (bx + r, by + r)]
    for p in board.pads:
        if p.net in nets:
            pts += [transform.pt(x, y) for x, y in p.ring]
    for z in board.zones:
        if z.net in nets:
            pts += [transform.pt(x, y) for x, y in z.ring]
    return pts


def margin_for(board: BoardModel) -> float:
    """Copper kept around the net, mm. See ``MARGIN_HEIGHTS``.

    The height used is the thicker of the two dielectrics under the outer layers, because that
    is the widest field a net on the outside of the board spreads over. Inner layers sit
    between two planes and spread less.
    """
    stack = [s for s in board.stackup if s.thickness_mm > 0 or s.is_copper]
    copper = [k for k, s in enumerate(stack) if s.is_copper]
    outer: list[float] = []
    if copper:
        for k, step in ((copper[0], 1), (copper[-1], -1)):
            j = k + step
            if 0 <= j < len(stack) and stack[j].is_dielectric:
                outer.append(stack[j].thickness_mm)
    h = max(outer) if outer else board.thickness_mm
    return max(MIN_MARGIN_MM, MARGIN_HEIGHTS * h)


def plan(
    board: BoardModel,
    transform,
    nets: list[str],
    *,
    margin_mm: float | None = None,
    resistance: float = 50.0,
) -> Coupon:
    """The coupon a net (or a pair) gets: its region and its ports. See the module docstring."""
    wanted = [n for n in dict.fromkeys(nets) if n]
    if not wanted:
        raise CouponError("name at least one net to cut out")
    known = set(board.nets) | {t.net for t in board.tracks} | {p.net for p in board.pads}
    missing = [n for n in wanted if n not in known]
    if missing:
        raise CouponError(f"{', '.join(missing)} is not a net on this board")
    grounds = [n for n in wanted if classify_net(n) == "ground"]
    if grounds:
        raise CouponError(
            f"{grounds[0]} is a ground net. A coupon drives a net against its return, so cut "
            f"out the signal or supply that returns through it instead"
        )

    pts = _net_points(board, transform, set(wanted))
    if not pts:
        raise CouponError(f"{', '.join(wanted)} has no copper on this board")
    margin = margin_mm if margin_mm is not None else margin_for(board)
    x0 = min(p[0] for p in pts) - margin
    y0 = min(p[1] for p in pts) - margin
    x1 = max(p[0] for p in pts) + margin
    y1 = max(p[1] for p in pts) + margin
    side = max(x1 - x0, y1 - y0)
    if side > MAX_SIDE_MM:
        raise CouponError(
            f"{', '.join(wanted)} spans {side - 2 * margin:.0f} mm, which with its planes is a "
            f"{side:.0f} mm coupon. Small-part solves stop at {MAX_SIDE_MM:.0f} mm: pick a "
            f"shorter net, or draw a region around the part of it you care about"
        )
    roi = (x0, y0, x1, y1)

    ports: list[Port] = []
    port_pads: list[str] = []
    notes: list[str] = []
    for net in wanted:
        ends = _ends(board, net)
        if not ends:
            notes.append(f"{net} has no pads, so it has no port and is only a passive neighbor")
            continue
        for pad in ends:
            ports.append(_port_on(board, transform, pad, len(ports), roi, resistance))
            port_pads.append(f"{pad.ref}.{pad.number}" if pad.ref else "")
        if len(ends) == 1:
            notes.append(
                f"{net} has one pad, so its far end is left open rather than terminated. An "
                f"open end rings, and the run lasts longer than a terminated one"
            )
    if not ports:
        raise CouponError(f"{', '.join(wanted)} has no pads to put a port on")
    ports = [replace(p, excited=(i == 0)) for i, p in enumerate(ports)]
    for p in ports:
        if not p.reference_layer:
            notes.append(
                f"there is no plane under port {p.name}, so it returns to the nearest copper "
                f"layer whether or not that layer has copper there. If the board has pours, "
                f"refill its zones in KiCad (B) before uploading: an unfilled zone is not copper"
            )
    return Coupon(nets=wanted, roi=roi, ports=ports, port_pads=port_pads,
                  margin_mm=margin, notes=notes)


def _pin_counts(board: BoardModel) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in board.pads:
        if p.ref:
            out[p.ref] = out.get(p.ref, 0) + 1
    return out


def _ends(board: BoardModel, net: str) -> list[Pad]:
    """The net's two ends: the pair of pads furthest apart, the driver first.

    The driver is the pad on the part with the most pins, then the first by reference, so the
    choice is deterministic and a user reading the result can see which end was driven.
    """
    pads = [p for p in board.pads if p.net == net and p.pad_type != "np_thru_hole"]
    if not pads:
        return []
    if len(pads) == 1:
        return pads
    best = (-1.0, pads[0], pads[1])
    for i, a in enumerate(pads):
        for b in pads[i + 1:]:
            d = math.hypot(a.x - b.x, a.y - b.y)
            if d > best[0]:
                best = (d, a, b)
    _, a, b = best
    pins = _pin_counts(board)
    key = lambda p: (-pins.get(p.ref, 0), p.ref, p.number)  # noqa: E731
    return sorted([a, b], key=key)


def _port_on(board: BoardModel, transform, pad: Pad, index: int, roi, resistance: float) -> Port:
    from .model import _layer_z

    layer_z = _layer_z(board)
    copper = [n for n in layer_z]
    if pad.is_through or "*.Cu" in pad.layers:
        layer = copper[0]
    else:
        layer = next((l for l in pad.layers if l in layer_z), copper[0])
    bx, by = transform.pt(pad.x, pad.y)
    return Port(
        name=f"p{index + 1}", x=bx, y=by, layer=layer, half_width_mm=PORT_HALF_WIDTH_MM,
        resistance=resistance, excited=index == 0,
        reference_layer=reference_under(board, transform, bx, by, layer, layer_z) or "",
    )


def reference_under(board: BoardModel, transform, x: float, y: float, layer: str,
                    layer_z: dict[str, float] | None = None) -> str | None:
    """The nearest copper layer that has a pour under (x, y), if any has one."""
    from .model import _layer_z

    layer_z = layer_z or _layer_z(board)
    if layer not in layer_z:
        return None
    z0 = layer_z[layer]
    under = []
    for zone in board.zones:
        if zone.layer == layer or zone.layer not in layer_z:
            continue
        ring = [transform.pt(px, py) for px, py in zone.ring]
        if g.point_in_ring(x, y, ring):
            under.append(zone.layer)
    if not under:
        return None
    return min(set(under), key=lambda n: abs(layer_z[n] - z0))


def _clip(ring: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float
          ) -> list[tuple[float, float]]:
    """Sutherland-Hodgman against an axis-aligned rectangle.

    A plane drawn as one polygon over the whole board has no vertex near the coupon, and a model
    that keeps shapes by their vertices drops it: the net then solves over nothing. Clipping
    puts vertices on the coupon's edge. The subject may be concave (a pour around parts); the
    result can then carry zero-width slivers along the clip edge, which rasterise to nothing.
    """
    def cut(pts, inside, meet):
        out = []
        for i, cur in enumerate(pts):
            prev = pts[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(meet(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(meet(prev, cur))
        return out

    def at_x(xc):
        return lambda a, b: (xc, a[1] + (b[1] - a[1]) * (xc - a[0]) / (b[0] - a[0]))

    def at_y(yc):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (yc - a[1]) / (b[1] - a[1]), yc)

    pts = list(ring)
    for inside, meet in (
        (lambda p: p[0] >= x0, at_x(x0)), (lambda p: p[0] <= x1, at_x(x1)),
        (lambda p: p[1] >= y0, at_y(y0)), (lambda p: p[1] <= y1, at_y(y1)),
    ):
        if not pts:
            break
        pts = cut(pts, inside, meet)
    return pts if len(pts) >= 3 and g.ring_area(pts) > 1e-9 else []


def _near_net(board: BoardModel, nets: set[str], within_mm: float):
    """A test: is a KiCad-space point within ``within_mm`` of the nets' copper?"""
    import numpy as np

    segs, pts = [], []
    for t in board.tracks:
        if t.net in nets:
            reach = within_mm + t.width_mm / 2.0
            for (ax, ay), (bx, by) in zip(t.pts, t.pts[1:]):
                segs.append((ax, ay, bx, by, reach))
            if len(t.pts) == 1:
                pts.append((t.pts[0][0], t.pts[0][1], reach))
    for v in board.vias:
        if v.net in nets:
            pts.append((v.x, v.y, within_mm + (v.size_mm or v.drill_mm) / 2.0))
    for p in board.pads:
        if p.net in nets:
            half = max((math.hypot(x - p.x, y - p.y) for x, y in p.ring), default=0.0)
            pts.append((p.x, p.y, within_mm + half))
    S = np.asarray(segs, dtype=float).reshape(-1, 5)
    P = np.asarray(pts, dtype=float).reshape(-1, 3)

    def close(x: float, y: float) -> bool:
        if len(P) and bool((np.hypot(P[:, 0] - x, P[:, 1] - y) <= P[:, 2]).any()):
            return True
        if not len(S):
            return False
        dx, dy = S[:, 2] - S[:, 0], S[:, 3] - S[:, 1]
        L2 = np.maximum(dx * dx + dy * dy, 1e-18)
        t = np.clip(((x - S[:, 0]) * dx + (y - S[:, 1]) * dy) / L2, 0.0, 1.0)
        d = np.hypot(S[:, 0] + t * dx - x, S[:, 1] + t * dy - y)
        return bool((d <= S[:, 4]).any())

    return close


def extract(board: BoardModel, transform, nets: list[str],
            roi: tuple[float, float, float, float], *, keep_mm: float = 2.0
            ) -> tuple[BoardModel, list[str]]:
    """The board with only the coupon's copper left in it. See the module docstring.

    ``keep_mm`` is how far past the region copper is kept, matching what ``build_model`` keeps
    (``COPPER_MARGIN_MM``) so a trace crossing the edge is not cut short inside the grid.
    """
    wanted = set(nets)
    x0, y0, x1, y1 = roi
    x0, y0, x1, y1 = x0 - keep_mm, y0 - keep_mm, x1 + keep_mm, y1 + keep_mm

    def near(bx: float, by: float, r: float = 0.0) -> bool:
        return x0 - r <= bx <= x1 + r and y0 - r <= by <= y1 + r

    def ring_near(ring) -> bool:
        pts = [transform.pt(x, y) for x, y in ring]
        return bool(pts) and (min(p[0] for p in pts) <= x1 and max(p[0] for p in pts) >= x0
                              and min(p[1] for p in pts) <= y1 and max(p[1] for p in pts) >= y0)

    ground = {n for n in set(board.nets) | {t.net for t in board.tracks}
              | {p.net for p in board.pads} if n and classify_net(n) == "ground"}
    keep_net = lambda n: n in wanted or n in ground  # noqa: E731
    close = _near_net(board, wanted, GROUND_NEAR_MM)

    # Ground copper only where it is close to the net. Every via and pad puts grid lines across
    # the whole coupon, and under a BGA the ground balls and vias alone made a 34 x 13 mm
    # coupon 6.2 M cells at the coarsest preset; the ones that carry the net's return are the
    # ones beside it.
    tracks = [t for t in board.tracks if ring_near(t.pts) and (
        t.net in wanted or (t.net in ground and any(close(x, y) for x, y in t.pts)))]
    vias = [v for v in board.vias if near(*transform.pt(v.x, v.y), (v.size_mm or v.drill_mm) / 2.0)
            and (v.net in wanted or (v.net in ground and close(v.x, v.y)))]
    pads = [p for p in board.pads if ring_near(p.ring) and (
        p.net in wanted or (p.net in ground and close(p.x, p.y)))]

    # Pours are clipped in KiCad space. The transform is a flip and a shift, so the coupon is
    # still an axis-aligned rectangle there.
    kx0, ky1 = x0 + transform.min_x, transform.max_y - y0
    kx1, ky0 = x1 + transform.min_x, transform.max_y - y1
    zones: list[ZonePolygon] = []
    for z in board.zones:
        clipped = _clip(z.ring, kx0, ky0, kx1, ky1)
        if clipped:
            zones.append(ZonePolygon(layer=z.layer, net=z.net, ring=clipped))

    left_out = {t.net for t in board.tracks if ring_near(t.pts)} | \
               {p.net for p in board.pads if ring_near(p.ring)}
    left_out = {n for n in left_out if n and not keep_net(n)}
    notes = []
    if left_out:
        notes.append(
            f"{len(left_out)} other net{'s' if len(left_out) != 1 else ''} inside the coupon "
            f"{'were' if len(left_out) != 1 else 'was'} left out, so this is the net over its "
            f"return path, not its coupling into neighbors"
        )
    if not any(z.net not in wanted for z in zones):
        notes.append(
            "no plane lies under this coupon, so its current returns through ground tracks and "
            "vias only. If the board has pours, refill its zones in KiCad (B) before uploading"
        )
    out = replace(board, tracks=tracks, vias=vias, pads=pads, zones=zones)
    return out, notes


def tight_gaps(board: BoardModel, nets: list[str], cell_mm: float) -> int:
    """How many pieces of other copper come closer to the nets than one cell.

    A grid line runs the whole domain, so two copper edges closer together than a cell can land
    on the same line, and the net is then joined to its neighbour: a trace becomes a stub to
    ground. That is how a real coupon's through line read S21 of -57 dB, before vias were drawn
    round (``model.py``). This counts the vias, pads and tracks of other nets a coupon keeps
    within a cell of the net, so a result can say its preset is too coarse for the spacing.
    """
    wanted = set(nets)
    if cell_mm <= 0:
        return 0
    tests: dict[float, object] = {}

    def within(extra: float):
        key = round(extra, 4)
        if key not in tests:
            tests[key] = _near_net(board, wanted, cell_mm + extra)
        return tests[key]

    count = 0
    for v in board.vias:
        if v.net not in wanted:
            count += bool(within((v.size_mm or v.drill_mm) / 2.0)(v.x, v.y))
    for p in board.pads:
        if p.net not in wanted:
            count += bool(any(within(0.0)(x, y) for x, y in p.ring))
    for t in board.tracks:
        if t.net not in wanted:
            count += bool(any(within(t.width_mm / 2.0)(x, y) for x, y in t.pts))
    return count
