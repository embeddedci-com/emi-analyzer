"""Net connectivity: what is joined to what, and how far apart.

The analyzer has always known how much copper is on a net. That is not the same question as
how long a signal takes to get from a driver to a receiver, and for anything but a simple
point-to-point net the two answers differ:

  * A fly-by address net reaching four DRAMs is a trunk plus four stubs. Its total copper is
    the sum of all five and describes no signal's journey.
  * A net routed on two layers has vias, which contribute delay but almost no copper.

So this module builds a graph of each net's copper and walks it. The pieces were all already
parsed -- tracks with endpoints, vias with layer spans, pads with positions -- they had just
never been joined up.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass, field

from .kicad.board import BoardModel
from .stackup import BoardElectrics

#: Two endpoints closer than this are the same point.
#:
#: KiCad writes coordinates that meet exactly, but a board that arrived as Gerbers has been
#: through a rasteriser, and a track that ends "on" a pad may miss by a rounding error. This
#: is the tolerance the Gerber net reconstruction settled on for the same reason.
JOIN_TOLERANCE_MM = 0.08


@dataclass(frozen=True)
class PathRun:
    """One layer's worth of a path."""

    layer: str
    length_mm: float


@dataclass
class Path:
    """A route from one pad to another."""

    from_pad: str
    to_pad: str
    runs: list[PathRun] = field(default_factory=list)
    vias: int = 0

    @property
    def length_mm(self) -> float:
        return sum(r.length_mm for r in self.runs)

    def delay_ps(self, elec: BoardElectrics) -> float:
        """Delay, which is the number that actually matters.

        Summed per layer rather than from a board average: an inner-layer millimetre and an
        outer-layer millimetre are about 25% apart, and a length-matching check that missed
        that would pass a board whose lanes are skewed by ten times their budget.
        """
        return sum(r.length_mm * elec.ps_per_mm(r.layer) for r in self.runs) + self.vias * elec.via_ps


@dataclass
class NetTopology:
    net: str
    #: Pad keys ("U1.A3") that this net's copper actually reaches.
    pads: list[str] = field(default_factory=list)
    #: Pads named in the netlist whose copper could not be reached from the rest. Usually a
    #: genuinely unrouted pin; occasionally a join tolerance that was too tight.
    unreachable: list[str] = field(default_factory=list)
    total_copper_mm: float = 0.0
    vias: int = 0
    _paths: dict[tuple[str, str], Path] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """"point-to-point", "branched" or "stub" -- how the net is wired, not how long."""
        n = len(self.pads)
        if n <= 1:
            return "stub"
        return "point-to-point" if n == 2 else "branched"

    def path(self, a: str, b: str) -> Path | None:
        return self._paths.get((a, b)) or self._paths.get((b, a))

    def paths_from(self, driver: str) -> list[Path]:
        return [p for (a, b), p in self._paths.items() if a == driver or b == driver]

    def longest_path(self) -> Path | None:
        if not self._paths:
            return None
        return max(self._paths.values(), key=lambda p: p.length_mm)


class _Graph:
    """Nodes merged on a grid, so joining is O(n) rather than O(n^2).

    A board has tens of thousands of endpoints; comparing every pair would be minutes. The
    grid cell is the join tolerance, so two points that should merge are always in the same
    cell or an adjacent one.
    """

    def __init__(self, tol: float = JOIN_TOLERANCE_MM):
        self.tol = tol
        self._cells: dict[tuple[int, int, str], list[int]] = defaultdict(list)
        self.points: list[tuple[float, float, str]] = []
        self.edges: dict[int, list[tuple[int, float, str, bool]]] = defaultdict(list)

    def node(self, x: float, y: float, layer: str) -> int:
        cx, cy = int(x / self.tol), int(y / self.tol)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self._cells.get((cx + dx, cy + dy, layer), ()):
                    px, py, _ = self.points[i]
                    if math.dist((px, py), (x, y)) <= self.tol:
                        return i
        i = len(self.points)
        self.points.append((x, y, layer))
        self._cells[(cx, cy, layer)].append(i)
        return i

    def points_on(self, p0: tuple[float, float], p1: tuple[float, float], layer: str) -> list[int]:
        """Existing nodes that lie on the segment p0-p1, ordered along it.

        A track that ends part-way along another track is a T-junction, and it is how a
        fly-by net is routed. Without splitting the trunk at that point the stub is a
        separate island and every receiver past it looks unrouted.
        """
        length = math.dist(p0, p1)
        if length <= 0:
            return []
        found: list[tuple[float, int]] = []
        steps = max(1, int(length / self.tol) + 1)
        seen: set[int] = set()
        for k in range(steps + 1):
            t = min(1.0, k * self.tol / length)
            x = p0[0] + (p1[0] - p0[0]) * t
            y = p0[1] + (p1[1] - p0[1]) * t
            cx, cy = int(x / self.tol), int(y / self.tol)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for i in self._cells.get((cx + dx, cy + dy, layer), ()):
                        if i in seen:
                            continue
                        px, py, _ = self.points[i]
                        # Distance from the point to the segment.
                        u = ((px - p0[0]) * (p1[0] - p0[0]) + (py - p0[1]) * (p1[1] - p0[1])) / (length * length)
                        if not (0.0 <= u <= 1.0):
                            continue
                        qx = p0[0] + (p1[0] - p0[0]) * u
                        qy = p0[1] + (p1[1] - p0[1]) * u
                        if math.dist((px, py), (qx, qy)) <= self.tol:
                            seen.add(i)
                            found.append((u, i))
        found.sort()
        return [i for _, i in found]

    def connect(self, a: int, b: int, weight: float, layer: str, is_via: bool = False) -> None:
        if a == b:
            return
        self.edges[a].append((b, weight, layer, is_via))
        self.edges[b].append((a, weight, layer, is_via))


def build(model: BoardModel, nets: list[str] | None = None) -> dict[str, NetTopology]:
    """Build a topology for each net.

    Restricting to the nets that are actually asked about matters: length matching cares
    about a few dozen of them, and building paths for all four hundred on a dense board is
    work nobody reads.
    """
    wanted = set(nets) if nets is not None else None
    by_net: dict[str, _Graph] = defaultdict(_Graph)
    pads_by_net: dict[str, list[tuple[str, float, float, list[str]]]] = defaultdict(list)
    out: dict[str, NetTopology] = {}

    # Pass one: every endpoint, via and pad becomes a node, so that pass two can split
    # segments wherever another piece of copper meets them.
    for track in model.tracks:
        if not track.net or (wanted is not None and track.net not in wanted):
            continue
        g = by_net[track.net]
        for x, y in track.pts:
            g.node(x, y, track.layer)
    for via in model.vias:
        if not via.net or (wanted is not None and via.net not in wanted):
            continue
        for layer in (via.layers or ()):
            by_net[via.net].node(via.x, via.y, layer)
    for pad in model.pads:
        if not pad.net or (wanted is not None and pad.net not in wanted):
            continue
        for layer in pad.layers:
            if layer in model.copper_layer_names:
                by_net[pad.net].node(pad.x, pad.y, layer)

    # Pass two: edges, split at any node that lands on them.
    for track in model.tracks:
        if not track.net or (wanted is not None and track.net not in wanted):
            continue
        g = by_net[track.net]
        for i in range(len(track.pts) - 1):
            p0, p1 = track.pts[i], track.pts[i + 1]
            # The segment's own ends always anchor the chain, resolved through node() so a
            # merged endpoint is found wherever it was merged to. Relying on points_on alone
            # lost them: a fix-up segment shorter than the join tolerance merges both its ends
            # into one node sitting at the *first* point, and the next segment then starts at
            # a node whose projection falls just before it -- so it was dropped, leaving a
            # one-node chain that laid no edge at all. Real routes went missing that way.
            a = g.node(*p0, track.layer)
            b = g.node(*p1, track.layer)
            interior = [n for n in g.points_on(p0, p1, track.layer) if n not in (a, b)]
            chain = [a, *interior, b]
            for u, v in zip(chain, chain[1:]):
                ux, uy, _ = g.points[u]
                vx, vy, _ = g.points[v]
                g.connect(u, v, math.dist((ux, uy), (vx, vy)), track.layer)

    for via in model.vias:
        if not via.net or (wanted is not None and via.net not in wanted):
            continue
        g = by_net[via.net]
        spanned = [n for n in via.layers if n in model.copper_layer_names] or via.layers
        nodes = [g.node(via.x, via.y, layer) for layer in spanned]
        # A via joins every layer it spans. Its delay is charged once per traversal, not per
        # pair, so the edge weight is zero length and the via flag carries the cost.
        for i in range(len(nodes) - 1):
            g.connect(nodes[i], nodes[i + 1], 0.0, spanned[i], is_via=True)

    # Pads joined by a copper pour rather than by a track. Very common for ground and power,
    # and without it every decoupling capacitor looks unrouted. The pour is treated as one
    # node: the path through a plane is short, wide and not what length matching is about,
    # so its length is taken as zero rather than modelled.
    zone_nodes: dict[str, dict[tuple[str, int], int]] = defaultdict(dict)
    for zi, zone in enumerate(model.zones):
        if not zone.net or (wanted is not None and zone.net not in wanted):
            continue
        g = by_net[zone.net]
        zone_nodes[zone.net][(zone.layer, zi)] = g.node(
            *_ring_centroid(zone.ring), f"zone:{zone.layer}"
        )

    for pad in model.pads:
        if not pad.net or (wanted is not None and pad.net not in wanted):
            continue
        key = f"{pad.ref}.{pad.number}" if pad.ref else pad.number
        layers = [n for n in pad.layers if n in model.copper_layer_names]
        if not layers and pad.is_through:
            layers = list(model.copper_layer_names)
        pads_by_net[pad.net].append((key, pad.x, pad.y, layers or model.copper_layer_names[:1]))

        # Join the pad to any same-net pour it sits inside.
        for zi, zone in enumerate(model.zones):
            if zone.net != pad.net or zone.layer not in (layers or ()):
                continue
            node = zone_nodes.get(pad.net, {}).get((zone.layer, zi))
            if node is not None and _point_in_ring(pad.x, pad.y, zone.ring):
                by_net[pad.net].connect(
                    by_net[pad.net].node(pad.x, pad.y, zone.layer), node, 0.0, zone.layer
                )

    for net, g in by_net.items():
        out[net] = _finish(net, g, pads_by_net.get(net, []))
    # A net with pads but no copper at all is still worth reporting -- it is unrouted.
    for net, pads in pads_by_net.items():
        if net not in out:
            out[net] = NetTopology(net=net, unreachable=[p[0] for p in pads])
    return out


def _ring_centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    if not ring:
        return 0.0, 0.0
    return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)


def _point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """Even-odd point-in-polygon. Pours are not convex, so a bounding box will not do."""
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xi = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < xi:
                inside = not inside
    return inside


def _finish(net: str, g: _Graph, pads: list[tuple[str, float, float, list[str]]]) -> NetTopology:
    topo = NetTopology(net=net)
    topo.total_copper_mm = sum(w for edges in g.edges.values() for _, w, _, _ in edges) / 2
    topo.vias = sum(1 for edges in g.edges.values() for _, _, _, v in edges if v) // 2

    # Attach each pad to the copper it lands on. A pad is not a point on one layer: a
    # through-hole pad is reachable from every layer it spans, and attaching it to only one
    # would make a perfectly routed net look unreachable.
    anchors: dict[str, list[int]] = {}
    for key, x, y, layers in pads:
        found = []
        for layer in layers:
            cx, cy = int(x / g.tol), int(y / g.tol)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for i in g._cells.get((cx + dx, cy + dy, layer), ()):
                        px, py, _ = g.points[i]
                        if math.dist((px, py), (x, y)) <= g.tol:
                            found.append(i)
        # A node with no edges is a pad the graph knows about but nothing joins -- the pad
        # was registered so that segments could split on it, which is not the same as being
        # connected. An unrouted pin has to stay unrouted.
        attached = sorted({i for i in found if g.edges.get(i)})
        if attached:
            anchors[key] = attached
            topo.pads.append(key)
        else:
            topo.unreachable.append(key)

    # One Dijkstra per pad. Nets have a handful of pads, so this is cheap; doing it per pair
    # would not be.
    keys = topo.pads
    for i, a in enumerate(keys):
        dist, prev = _dijkstra(g, anchors[a])
        for b in keys[i + 1:]:
            best = min((n for n in anchors[b] if n in dist), key=lambda n: dist[n], default=None)
            if best is None:
                continue
            topo._paths[(a, b)] = _trace(g, prev, best, a, b)
    return topo


def _dijkstra(g: _Graph, sources: list[int]) -> tuple[dict[int, float], dict[int, int]]:
    dist: dict[int, float] = {s: 0.0 for s in sources}
    prev: dict[int, int] = {}
    q: list[tuple[float, int]] = [(0.0, s) for s in sources]
    heapq.heapify(q)
    while q:
        d, n = heapq.heappop(q)
        if d > dist.get(n, math.inf):
            continue
        for m, w, _, _ in g.edges.get(n, ()):
            nd = d + w
            if nd < dist.get(m, math.inf) - 1e-12:
                dist[m] = nd
                prev[m] = n
                heapq.heappush(q, (nd, m))
    return dist, prev


def _trace(g: _Graph, prev: dict[int, int], end: int, a: str, b: str) -> Path:
    """Walk the predecessor chain back, accumulating length per layer and counting vias."""
    path = Path(from_pad=a, to_pad=b)
    per_layer: dict[str, float] = defaultdict(float)
    order: list[str] = []
    node = end
    while node in prev:
        p = prev[node]
        for m, w, layer, is_via in g.edges.get(p, ()):
            if m == node:
                if is_via:
                    path.vias += 1
                else:
                    if layer not in per_layer:
                        order.append(layer)
                    per_layer[layer] += w
                break
        node = p
    path.runs = [PathRun(layer=l, length_mm=per_layer[l]) for l in order]
    return path
