"""Rasterising copper, and finding what is connected to what.

Used by two things that look unrelated but need the same primitive: the reference-plane gap
check, which asks "is there copper under this trace", and Gerber net reconstruction, which
asks "which island of copper is this, and what is it called".

Resolution is a real trade-off. Too coarse and two traces separated by a hair merge into one
net; too fine and a dense board's raster costs more memory than the field solve it is
preparing for. 50 um is below any manufacturable clearance and keeps a 100 x 80 mm board to
3.2 million cells per layer.
"""

from __future__ import annotations

import numpy as np

Ring = list[tuple[float, float]]


def rasterize_rings(
    rings: list[Ring],
    origin: tuple[float, float],
    shape: tuple[int, int],
    resolution_mm: float,
) -> np.ndarray:
    """Fill polygons into a boolean mask.

    Even-odd **within** one ring, OR **across** rings. Both halves matter and they are not
    the same rule: a pour with voids is emitted as a single outline that travels into each
    void and back out, so even-odd is what carves the holes; but separate polygons are
    separate copper islands, and XOR-ing those together would make two overlapping pours
    erase each other and invent a gap that is not on the board.
    """
    height, width = shape
    mask = np.zeros((height, width), dtype=bool)
    ox, oy = origin

    for ring in rings:
        if len(ring) < 3:
            continue
        pts = np.asarray(ring, dtype=np.float64)
        x0, y0 = pts[:, 0], pts[:, 1]
        x1, y1 = np.roll(x0, -1), np.roll(y0, -1)

        row_lo = max(0, int((min(y0.min(), y1.min()) - oy) / resolution_mm))
        row_hi = min(height - 1, int((max(y0.max(), y1.max()) - oy) / resolution_mm) + 1)

        for row in range(row_lo, row_hi + 1):
            sy = oy + row * resolution_mm
            hits = ((y0 <= sy) & (y1 > sy)) | ((y1 <= sy) & (y0 > sy))
            if not hits.any():
                continue
            t = (sy - y0[hits]) / (y1[hits] - y0[hits])
            xs = np.sort(x0[hits] + t * (x1[hits] - x0[hits]))
            for i in range(0, len(xs) - 1, 2):
                a = int((xs[i] - ox) / resolution_mm)
                b = int((xs[i + 1] - ox) / resolution_mm)
                if b >= a:
                    mask[row, max(0, a):min(width, b + 1)] = True
    return mask


class _UnionFind:
    def __init__(self) -> None:
        self.parent: list[int] = [0]

    def make(self) -> int:
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> int:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        self.parent[hi] = lo
        return lo


def _runs(row: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive [start, end] spans of True in one row."""
    if not row.any():
        return []
    d = np.diff(row.astype(np.int8))
    starts = list(np.flatnonzero(d == 1) + 1)
    ends = list(np.flatnonzero(d == -1))
    if row[0]:
        starts.insert(0, 0)
    if row[-1]:
        ends.append(len(row) - 1)
    return list(zip(starts, ends))


def label_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """Label connected regions of True, 4-connected.

    Works on row runs rather than pixels, with union-find to merge runs that touch the row
    above. A dense board has millions of pixels but only tens of thousands of runs, which is
    the difference between this taking a second and taking a minute.

    Returns ``(labels, count)``; label 0 is background and real labels are 1..count.
    """
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    uf = _UnionFind()
    prev: list[tuple[int, int, int]] = []

    for row in range(height):
        current: list[tuple[int, int, int]] = []
        for start, end in _runs(mask[row]):
            label = 0
            for pstart, pend, plabel in prev:
                if pstart > end:
                    break
                if pend >= start:  # overlaps this run
                    label = uf.find(plabel) if label == 0 else uf.union(label, plabel)
            if label == 0:
                label = uf.make()
            labels[row, start:end + 1] = label
            current.append((start, end, label))
        prev = current

    # Resolve every label to its root, then renumber so labels are 1..count with no gaps.
    if uf.parent == [0]:
        return labels, 0

    roots = np.array([uf.find(i) if i else 0 for i in range(len(uf.parent))], dtype=np.int32)
    resolved = roots[labels]

    used = np.unique(resolved)
    used = used[used != 0]
    remap = np.zeros(int(resolved.max()) + 1, dtype=np.int32)
    remap[used] = np.arange(1, len(used) + 1, dtype=np.int32)
    return remap[resolved], len(used)


class LayerRaster:
    """One layer's copper mask, plus the coordinate mapping to reach it."""

    def __init__(self, mask: np.ndarray, origin: tuple[float, float], resolution_mm: float):
        self.mask = mask
        self.origin = origin
        self.resolution_mm = resolution_mm
        self.labels, self.count = label_components(mask)

    def label_at(self, x_mm: float, y_mm: float, search_px: int = 0) -> int:
        """Label at a point, optionally searching a small neighbourhood.

        The neighbourhood matters for netlist points: a pad centre is copper, but a via's
        recorded position can land a pixel outside its own annular ring once the drill is
        subtracted, and a point that lands on background carries no net anywhere.
        """
        col = int((x_mm - self.origin[0]) / self.resolution_mm)
        row = int((y_mm - self.origin[1]) / self.resolution_mm)
        h, w = self.labels.shape

        if 0 <= row < h and 0 <= col < w and self.labels[row, col]:
            return int(self.labels[row, col])
        if search_px <= 0:
            return 0

        # Search outward ring by ring and take the *nearest* copper, not the most common in
        # the window. On a dense board a pad sits inside a ground pour, so "most common"
        # returns the pour every time and every net on the board comes out as GND.
        for radius in range(1, search_px + 1):
            r0, r1 = max(0, row - radius), min(h, row + radius + 1)
            c0, c1 = max(0, col - radius), min(w, col + radius + 1)
            if r0 >= r1 or c0 >= c1:
                break
            window = self.labels[r0:r1, c0:c1]
            hits = np.argwhere(window != 0)
            if hits.size == 0:
                continue
            # Nearest by squared distance from the query point.
            dr = hits[:, 0] + r0 - row
            dc = hits[:, 1] + c0 - col
            nearest = np.argmin(dr * dr + dc * dc)
            return int(window[hits[nearest, 0], hits[nearest, 1]])
        return 0
