"""Reconstructing nets from copper and a netlist.

Gerbers say where copper is; the IPC-D-356 netlist says where connection points are and
what they are called. Neither alone gives you a net-aware board. Together they do:

1. Rasterise each copper layer and label its connected islands.
2. Drop each netlist point onto its layer. The island it lands in takes that net's name.
3. Tie islands together through plated holes. This is what names an inner plane, which
   typically has no netlist point of its own — it is reached through the vias that stitch
   it to a layer that does.
4. Attach every piece of geometry to the island containing it.

Step 3 is the one that is easy to leave out and hard to notice missing: the board still
renders, the nets still list, and the ground plane is simply anonymous.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict

from ..kicad import geometry as g
from ..kicad.board import Pad, Track, Via, ZonePolygon
from ..raster import LayerRaster, rasterize_rings
from .ipcd356 import Netlist

log = logging.getLogger(__name__)

#: Raster resolution, mm. Below any manufacturable clearance so distinct nets cannot merge.
RESOLUTION_MM = 0.05

#: How far a netlist point may sit from copper and still attach to it, in mm.
#:
#: This must stay well below an antipad clearance, and that is the whole story. A
#: through-hole netlist point nominally touches every layer, but on the layers it does not
#: connect to it passes through a clearance void — so the point lands on nothing, which is
#: the correct answer. A generous tolerance reaches across that void, grabs the surrounding
#: plane, and merges the signal into whatever the plane is. On this repo's own solar board
#: a 0.35 mm tolerance collapsed 39 nets into 12, with every through-hole signal absorbed
#: into +5V or GND.
#:
#: The raster draws via pads as solid discs, so a via centre is already copper and needs no
#: tolerance at all. What remains is rounding: one or two pixels.
POINT_TOLERANCE_MM = 0.08

#: Above this many pixels per layer, the raster is coarsened. A very large board at 50 um
#: would otherwise allocate more than the solve it is preparing for.
MAX_PIXELS = 40_000_000


class Connectivity:
    """Copper islands per layer, and the net each one carries."""

    def __init__(self, layers: list[str], bounds: tuple[float, float, float, float]):
        self.layers = layers
        self.min_x, self.min_y, self.max_x, self.max_y = bounds
        self.resolution = RESOLUTION_MM

        width_mm = max(1e-3, self.max_x - self.min_x)
        height_mm = max(1e-3, self.max_y - self.min_y)
        pixels = (width_mm / self.resolution) * (height_mm / self.resolution)
        if pixels > MAX_PIXELS:
            self.resolution *= (pixels / MAX_PIXELS) ** 0.5
            log.warning(
                "board is large; coarsening the net raster to %.3f mm", self.resolution,
            )

        self.shape = (
            int(height_mm / self.resolution) + 2,
            int(width_mm / self.resolution) + 2,
        )
        self.rasters: dict[str, LayerRaster] = {}
        #: (layer, label) -> net name
        self.net_of: dict[tuple[str, int], str] = {}
        self.warnings: list[str] = []
        #: Netlist coordinates -> model coordinates, once assign() has found it. Kept so that
        #: netlist points can be matched to pads afterwards, not just to islands.
        self.transform = None

    def build_rasters(self, rings_by_layer: dict[str, list[list[tuple[float, float]]]]) -> None:
        for layer in self.layers:
            rings = rings_by_layer.get(layer, [])
            mask = rasterize_rings(rings, (self.min_x, self.min_y), self.shape, self.resolution)
            self.rasters[layer] = LayerRaster(mask, (self.min_x, self.min_y), self.resolution)
            log.info("%s: %d copper islands", layer, self.rasters[layer].count)

    def _score(self, netlist: Netlist, transform, tolerance_px: int) -> int:
        """How many netlist points land on copper under a candidate transform."""
        hits = 0
        for point in netlist.points:
            x, y = transform(point.x_mm, point.y_mm)
            for layer in self.layers:
                raster = self.rasters.get(layer)
                if raster is not None and raster.label_at(x, y, tolerance_px):
                    hits += 1
                    break
        return hits

    def align(self, netlist: Netlist, tolerance_px: int, vias=None):
        """Find the transform that puts the netlist onto the copper.

        CAD tools disagree about the netlist's origin: some use the page, some the board
        corner, some a user-set drill origin — and the netlist's Y axis points up while the
        model's points down.

        The exact answer, when it is available, comes from the drill file. Its holes and the
        netlist's through-hole records describe the same physical vias, so the difference of
        the two centroids is the offset, to full precision. Aligning by bounding boxes
        instead is off by however much the two sets differ at their extremes — a fraction of
        a millimetre, which is enough to drop a pad's netlist point onto the ground pour
        beside it and hand that pad's net to the pour.

        Without a drill file, fall back to bounding boxes and then refine by local search.
        Either way the result is scored, and a poor score is reported rather than used.
        """
        xs = [p.x_mm for p in netlist.points]
        ys = [p.y_mm for p in netlist.points]
        nx0, ny0, ny1 = min(xs), min(ys), max(ys)
        mx0, my0 = self.min_x, self.min_y

        candidates: dict[str, object] = {"as-is": lambda x, y: (x, y)}

        through = [p for p in netlist.points if p.access == 0]
        if vias and through and abs(len(vias) - len(through)) <= max(4, len(through) // 10):
            # Same holes, two coordinate systems. Y is flipped between them because the
            # netlist is Y-up and the model is Y-down.
            vx = sum(v.x for v in vias) / len(vias)
            vy = sum(v.y for v in vias) / len(vias)
            tx = sum(p.x_mm for p in through) / len(through)
            ty = sum(p.y_mm for p in through) / len(through)
            candidates["drill centroids"] = lambda x, y, dx=vx - tx, dy=vy + ty: (x + dx, dy - y)

        candidates["bounding boxes"] = lambda x, y: (x - nx0 + mx0, y - ny0 + my0)
        candidates["bounding boxes, Y flipped"] = lambda x, y: (x - nx0 + mx0, (ny1 - y) + my0)

        best_name, best_fn, best_hits = "as-is", candidates["as-is"], -1
        for name, fn in candidates.items():
            hits = self._score(netlist, fn, tolerance_px)
            log.info("netlist alignment %-28s %d/%d points on copper",
                     name, hits, len(netlist.points))
            if hits > best_hits:
                best_name, best_fn, best_hits = name, fn, hits

        # Refine by a small local search: a centroid match is exact only if both sets are
        # the same points, and a bounding-box match never is.
        step = self.resolution
        base = best_fn
        for _ in range(2):
            improved = False
            for dx in (-2, -1, 0, 1, 2):
                for dy in (-2, -1, 0, 1, 2):
                    if dx == 0 and dy == 0:
                        continue
                    shifted = (lambda x, y, f=base, ox=dx * step, oy=dy * step:
                               (lambda p: (p[0] + ox, p[1] + oy))(f(x, y)))
                    hits = self._score(netlist, shifted, tolerance_px)
                    if hits > best_hits:
                        best_hits, best_fn, improved = hits, shifted, True
            if not improved:
                break
            base = best_fn
            step /= 2

        fraction = best_hits / max(1, len(netlist.points))
        if fraction < 0.5:
            raise ValueError(
                f"only {best_hits} of {len(netlist.points)} netlist points could be matched "
                f"to copper, under any alignment. The netlist and the Gerbers are probably "
                f"from different revisions of the board, or from different boards."
            )
        log.info("netlist aligned by %s: %d/%d points on copper",
                 best_name, best_hits, len(netlist.points))
        if fraction < 0.9:
            self.warnings.append(
                f"Only {best_hits} of {len(netlist.points)} netlist points landed on "
                f"copper, so some nets may be unnamed or wrong."
            )
        return best_fn

    def assign(self, netlist: Netlist, vias=None) -> None:
        """Name the islands, then propagate names through plated holes."""
        tolerance_px = max(1, int(POINT_TOLERANCE_MM / self.resolution))
        transform = self.align(netlist, tolerance_px, vias)
        self.transform = transform

        # A through-hole point touches every layer; a surface point touches only its own.
        # Access codes are 1-based into the stack, and 0 means all layers.
        placed = 0
        unplaced: list[str] = []
        groups: list[list[tuple[str, int]]] = []

        for point in netlist.points:
            targets = (
                list(self.layers) if point.access == 0
                else [self.layers[point.access - 1]]
                if 1 <= point.access <= len(self.layers)
                else []
            )
            if not targets:
                continue

            px, py = transform(point.x_mm, point.y_mm)
            touched: list[tuple[str, int]] = []
            for layer in targets:
                raster = self.rasters.get(layer)
                if raster is None:
                    continue
                label = raster.label_at(px, py, tolerance_px)
                if label:
                    touched.append((layer, label))

            if not touched:
                unplaced.append(point.net)
                continue
            placed += 1

            for key in touched:
                existing = self.net_of.get(key)
                if existing is None:
                    self.net_of[key] = point.net
                elif existing != point.net:
                    # Two nets landing in one island means the raster bridged copper that
                    # is really separate. Worth saying: it is the failure mode that turns
                    # a clearance violation into a wrong answer rather than an error.
                    self.warnings.append(
                        f"{existing} and {point.net} resolve to the same piece of copper on "
                        f"{key[0]}. They may be shorted, or a very fine gap was merged."
                    )

            # A through-hole ties the islands it passes through into one net.
            if point.access == 0 and len(touched) > 1:
                groups.append(touched)

        # Propagate: anything tied to a named island takes that name.
        for group in groups:
            name = next((self.net_of[k] for k in group if k in self.net_of), None)
            if not name:
                continue
            for key in group:
                self.net_of.setdefault(key, name)

        if unplaced:
            self.warnings.append(
                f"{len(unplaced)} netlist points landed on no copper and were ignored. "
                f"If that is many, export the netlist and Gerbers from the same revision."
            )

        named = len({v for v in self.net_of.values()})
        log.info("assigned %d nets from %d placed netlist points", named, placed)

    def net_at(self, layer: str, x_mm: float, y_mm: float) -> str:
        raster = self.rasters.get(layer)
        if raster is None:
            return ""
        label = raster.label_at(x_mm, y_mm, 1)
        return self.net_of.get((layer, label), "") if label else ""


def rings_for_geometry(
    tracks: list[Track], pads: list[Pad], zones: list[ZonePolygon], layer: str,
) -> list[list[tuple[float, float]]]:
    """Every closed outline on one layer, for rasterising."""
    rings: list[list[tuple[float, float]]] = []
    for t in tracks:
        if t.layer == layer and t.width_mm > 0 and len(t.pts) >= 2:
            rings.extend(g.thick_polyline(t.pts, t.width_mm))
    for p in pads:
        if layer in p.layers and len(p.ring) >= 3:
            rings.append(p.ring)
    for z in zones:
        if z.layer == layer and len(z.ring) >= 3:
            rings.append(z.ring)
    return rings


def apply_nets(
    conn: Connectivity,
    tracks: list[Track], pads: list[Pad], zones: list[ZonePolygon], vias: list[Via],
) -> None:
    """Attach each piece of geometry to the net of the island containing it."""
    for t in tracks:
        # A track's midpoint is always inside it; an endpoint may sit exactly on the edge.
        mid = t.pts[len(t.pts) // 2]
        t.net = conn.net_at(t.layer, mid[0], mid[1])
    for p in pads:
        for layer in p.layers:
            net = conn.net_at(layer, p.x, p.y)
            if net:
                p.net = net
                break
    for z in zones:
        # A zone's vertices are on its boundary, so sample the centroid instead.
        cx = sum(px for px, _ in z.ring) / len(z.ring)
        cy = sum(py for _, py in z.ring) / len(z.ring)
        net = conn.net_at(z.layer, cx, cy)
        if not net:
            # A concave pour's centroid can fall in a void; fall back to a vertex nudged
            # inward, which is cheap and usually lands on copper.
            vx, vy = z.ring[0]
            net = conn.net_at(z.layer, (vx + cx) / 2, (vy + cy) / 2)
        z.net = net
    for v in vias:
        for layer in v.layers or conn.layers:
            net = conn.net_at(layer, v.x, v.y)
            if net:
                v.net = net
                break


def summarise(conn: Connectivity) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for (_, _), net in conn.net_of.items():
        counts[net] += 1
    return dict(counts)


#: How far a component's netlist point may sit from a pad's centre and still name it, in mm.
#: IPC-D-356 records a surface pad's centre and a through-hole's drill centre, so the two
#: agree to rounding; the point-in-pad test covers exporters that record an offset access
#: point instead.
REF_TOLERANCE_MM = 0.1

#: Grid cell for finding pads near a point, in mm. A point inside a pad whose centre is more
#: than a cell away is not found, which only a very large pad with an offset access point
#: could do.
_PAD_CELL_MM = 1.0


def attach_parts(
    conn: Connectivity, netlist: Netlist, pads: list[Pad], vias: list[Via],
    layers: list[str],
) -> tuple[list[Pad], list[Via], list[str]]:
    """Name each flashed pad after the component pin the netlist puts on it.

    Gerbers say where pads are and the netlist says whose they are. Without joining the two
    every pad is anonymous, and every check that looks for parts (ESD protection, decoupling,
    crystals, length matching by pin) found nothing to look at on a Gerber upload.

    A through-hole pin is one pad flashed on every layer plus a hole in the drill file. It
    becomes one through-hole pad, as a KiCad board would describe it, and its hole stops
    being counted as a via.

    Returns ``(pads, vias, warnings)``.
    """
    warnings: list[str] = []
    if conn.transform is None:
        return pads, vias, warnings

    grid: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    for i, p in enumerate(pads):
        for layer in p.layers:
            grid[(layer, int(p.x // _PAD_CELL_MM), int(p.y // _PAD_CELL_MM))].append(i)

    drop: set[int] = set()
    holes: list[tuple[float, float]] = []
    wanted = matched = 0

    for point in netlist.points:
        if point.is_via:
            continue
        wanted += 1
        px, py = conn.transform(point.x_mm, point.y_mm)
        targets = (
            list(layers) if point.access == 0
            else [layers[point.access - 1]] if 1 <= point.access <= len(layers)
            else []
        )
        cx, cy = int(px // _PAD_CELL_MM), int(py // _PAD_CELL_MM)
        hits: list[int] = []
        for layer in targets:
            best: tuple[float, int] | None = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for i in grid.get((layer, cx + dx, cy + dy), ()):
                        pad = pads[i]
                        if pad.ref or i in drop:
                            continue  # already claimed by another pin
                        d = math.hypot(pad.x - px, pad.y - py)
                        if d <= REF_TOLERANCE_MM or (len(pad.ring) >= 3 and _inside(px, py, pad.ring)):
                            if best is None or d < best[0]:
                                best = (d, i)
            if best is not None:
                hits.append(best[1])
        if not hits:
            continue
        matched += 1

        keep = pads[hits[0]]
        keep.ref, keep.number = point.ref, point.pin
        keep.net = keep.net or point.net
        if point.access == 0:
            keep.layers = list(layers)
            keep.pad_type = "thru_hole" if point.plated else "np_thru_hole"
            keep.drill_mm = point.drill_mm
            drop.update(hits[1:])
            holes.append((keep.x, keep.y))
        else:
            for i in hits[1:]:
                pads[i].ref, pads[i].number = point.ref, point.pin

    if holes:
        hole_grid: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
        for x, y in holes:
            hole_grid[(int(x // _PAD_CELL_MM), int(y // _PAD_CELL_MM))].append((x, y))

        def is_pin(v: Via) -> bool:
            cx, cy = int(v.x // _PAD_CELL_MM), int(v.y // _PAD_CELL_MM)
            return any(
                math.hypot(v.x - x, v.y - y) <= REF_TOLERANCE_MM
                for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for x, y in hole_grid.get((cx + dx, cy + dy), ())
            )

        vias = [v for v in vias if not is_pin(v)]

    # What is left unnamed on top of a via is the via's own annular ring, flashed once per
    # layer. It is not a part's pad, and as a pad with no reference it showed up in every
    # net's pin list as "". It becomes the via's size instead, which the drill file alone
    # cannot give.
    via_grid: dict[tuple[int, int], list[Via]] = defaultdict(list)
    for v in vias:
        via_grid[(int(v.x // _PAD_CELL_MM), int(v.y // _PAD_CELL_MM))].append(v)
    for i, pad in enumerate(pads):
        if pad.ref or i in drop:
            continue
        cx, cy = int(pad.x // _PAD_CELL_MM), int(pad.y // _PAD_CELL_MM)
        via = next((
            v for dx in (-1, 0, 1) for dy in (-1, 0, 1)
            for v in via_grid.get((cx + dx, cy + dy), ())
            if math.hypot(v.x - pad.x, v.y - pad.y) <= REF_TOLERANCE_MM
        ), None)
        if via is None or len(pad.ring) < 3:
            continue
        xs = [p[0] for p in pad.ring]
        ys = [p[1] for p in pad.ring]
        via.size_mm = max(via.size_mm, min(max(xs) - min(xs), max(ys) - min(ys)))
        drop.add(i)

    if wanted and matched < wanted:
        warnings.append(
            f"{wanted - matched} of {wanted} component pins in the netlist matched no pad, "
            f"so part checks may miss them."
        )
    log.info("netlist named %d of %d component pins", matched, wanted)
    return [p for i, p in enumerate(pads) if i not in drop], vias, warnings


def _inside(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside
