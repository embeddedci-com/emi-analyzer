"""The loudest spots of a small-part map, and what copper is under each.

A map's single loudest point is a weak answer when a second spot is nearly as loud. On a real
differential pair (docs/verification/small-part-solve.md, board C) the coarse and normal
meshes put the peak at opposite ends of the net, 2.7 dB apart, and both ends were the jogs
beside the ports. Either is a spot to fix, and naming only one sends the user to half of the
problem. So a result lists every separate spot within ``WITHIN_DB`` of the loudest.

A spot is a connected area of the map within ``WITHIN_DB`` of the peak: a matched line is
nearly flat along its length and would otherwise be a row of "spots" a cell apart. Points
beside a port are left out, as the convergence study leaves them out: the port is where current
is injected and is always loud, so a hotspot there says nothing about the layout.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from ..kicad.board import BoardModel

#: Spots within this of the loudest are listed, dB. The level the user decided counts as "the
#: same size of problem": we are looking for big issues, not ranking two near-equal ones.
WITHIN_DB = 3.0

#: Around each port, mm: points this close are the injection, not a hotspot.
PORT_EXCLUSION_MM = 1.5

#: Two spots closer than this are one, mm. The two edges of one trace can read as two ridges.
MERGE_MM = 1.5

#: At most this many spots per map.
MAX_SPOTS = 5

#: A map whose loudest point is within this of the floor carries no field worth a spot, dB.
NOISE_MARGIN_DB = 6.0

#: Nor does one whose loudest point is this far below the run's loudest, dB. A plane layer under
#: a supply net on a real board listed "spots" at -54 dB: residue, not an issue to act on.
QUIET_DB = -40.0

#: A part is named when one of its pads is this close to the spot, mm.
PART_WITHIN_MM = 2.0


def spots(x_mm: np.ndarray, y_mm: np.ndarray, db: np.ndarray, ports: list[tuple[float, float]],
          floor_db: float) -> list[dict]:
    """Separate spots within ``WITHIN_DB`` of the map's loudest point away from the ports.

    ``db`` is (ny, nx) on the grid lines ``x_mm`` and ``y_mm``, in the manifest's dB (0 is the
    run's loudest point). Loudest
    first; each is ``{x_mm, y_mm, db, below_peak_db}``.
    """
    X, Y = np.meshgrid(x_mm, y_mm)
    allowed = np.ones(db.shape, dtype=bool)
    for px, py in ports:
        allowed &= np.hypot(X - px, Y - py) > PORT_EXCLUSION_MM
    if not allowed.any():
        return []
    peak = float(db[allowed].max())
    if peak <= max(floor_db + NOISE_MARGIN_DB, QUIET_DB):
        return []
    hot = allowed & (db >= peak - WITHIN_DB)
    ny, nx = db.shape
    seen = np.zeros_like(hot)
    found: list[tuple[float, int, int]] = []
    for iy, ix in zip(*np.nonzero(hot)):
        if seen[iy, ix]:
            continue
        # One connected area (eight neighbours), and its loudest point.
        best = (float(db[iy, ix]), int(iy), int(ix))
        seen[iy, ix] = True
        queue = deque([(iy, ix)])
        while queue:
            cy, cx = queue.popleft()
            if db[cy, cx] > best[0]:
                best = (float(db[cy, cx]), int(cy), int(cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    qy, qx = cy + dy, cx + dx
                    if 0 <= qy < ny and 0 <= qx < nx and hot[qy, qx] and not seen[qy, qx]:
                        seen[qy, qx] = True
                        queue.append((qy, qx))
        found.append(best)
    out: list[dict] = []
    for level, iy, ix in sorted(found, reverse=True):
        x, y = float(x_mm[ix]), float(y_mm[iy])
        if any(math.hypot(x - s["x_mm"], y - s["y_mm"]) < MERGE_MM for s in out):
            continue
        out.append({"x_mm": round(x, 3), "y_mm": round(y, 3), "db": round(level, 2),
                    "below_peak_db": round(peak - level, 2)})
        if len(out) >= MAX_SPOTS:
            break
    return out


class Nearby:
    """What copper is nearest a point: the net (of those solved) and the part.

    Tracks, pads and vias in board space, built once for a result's few dozen spots.
    """

    def __init__(self, board: BoardModel, transform, nets: list[str] | None,
                 parts_board: BoardModel | None = None):
        want = set(nets or [])
        keep = (lambda n: n in want) if want else (lambda n: bool(n))
        seg: list[tuple[str, str, float, float, float, float]] = []
        for t in board.tracks:
            if not keep(t.net):
                continue
            pts = [transform.pt(x, y) for x, y in t.pts]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                seg.append((t.layer, t.net, x0, y0, x1, y1))
        for p in board.pads:
            if keep(p.net):
                x, y = transform.pt(p.x, p.y)
                for layer in p.layers or [""]:
                    seg.append((layer, p.net, x, y, x, y))
        for v in board.vias:
            if keep(v.net):
                x, y = transform.pt(v.x, v.y)
                for layer in v.layers or [""]:
                    seg.append((layer, v.net, x, y, x, y))
        self._layers = np.array([s[0] for s in seg], dtype=object)
        self._nets = [s[1] for s in seg]
        self._seg = np.array([s[2:] for s in seg], dtype=np.float64).reshape(-1, 4)
        pads = [p for p in (parts_board or board).pads if p.ref]
        self._pad_refs = [p.ref for p in pads]
        self._pads = np.array([transform.pt(p.x, p.y) for p in pads],
                              dtype=np.float64).reshape(-1, 2)

    def net(self, x: float, y: float, layer: str) -> str | None:
        if not len(self._seg):
            return None
        a, b = self._seg[:, :2], self._seg[:, 2:]
        ab = b - a
        L2 = (ab ** 2).sum(axis=1)
        t = np.clip(((np.array([x, y]) - a) * ab).sum(axis=1) / np.where(L2 > 0, L2, 1), 0, 1)
        d = np.hypot(*(a + ab * t[:, None] - np.array([x, y])).T)
        # Copper on the map's own layer first; a spot over a plane layer is the return current
        # under a trace on another, so it falls back to any layer.
        on = self._layers == layer
        if on.any():
            d = np.where(on, d, np.inf)
        return self._nets[int(np.argmin(d))]

    def part(self, x: float, y: float) -> str | None:
        if not len(self._pads):
            return None
        d = np.hypot(self._pads[:, 0] - x, self._pads[:, 1] - y)
        k = int(np.argmin(d))
        return self._pad_refs[k] if d[k] <= PART_WITHIN_MM else None
