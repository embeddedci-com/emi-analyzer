"""Graded rectilinear mesh generation.

FDTD on a rectilinear grid means every line you add anywhere crosses the whole domain, so
mesh design is the whole cost problem. Three rules drive everything here:

1. **Some lines are non-negotiable.** Copper edges and layer heights have to fall on grid
   lines, or the geometry the solver sees is not the geometry the user drew.
2. **The largest cell is bounded by the shortest wavelength.** Above about a twentieth of
   a wavelength the grid stops resolving the wave and the answer is numerical noise.
3. **Adjacent cells must not differ by much.** A sudden jump in cell size reflects energy
   off the discontinuity — a numerical artefact indistinguishable from a real reflection.

The timestep is set by the *smallest* cell anywhere in the grid, so grading buys fewer
cells but never a larger timestep. That asymmetry is why a "small" refinement of the
vertical mesh costs roughly four times as much as it looks like it should.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

SPEED_OF_LIGHT = 299_792_458.0

#: Cells per wavelength at the top frequency. 20 is the usual working figure; below about
#: 10 the grid dispersion error becomes visible in the result.
CELLS_PER_WAVELENGTH = 20

#: Largest ratio between adjacent cell sizes. openEMS's own guidance is 1.5; 1.4 leaves a
#: little margin and costs very few extra lines.
MAX_CELL_RATIO = 1.4

#: Grid lines closer together than this fraction of the minimum resolution are merged.
#: Two copper edges 3 nm apart are the same edge as far as any mesh is concerned, and
#: keeping both would pin the timestep to 3 nm and make the run impossible.
MERGE_FRACTION = 0.25

#: Lines of padding outside the region of interest on a PML face. openEMS needs 8 for
#: PML_8 and silently falls back to a reflecting PEC wall with fewer.
PML_LINES = 8


class MeshError(ValueError):
    """The mesh cannot be built as requested."""


def merge_close(lines: np.ndarray, min_spacing: float) -> np.ndarray:
    """Sort, deduplicate, and merge lines closer together than ``min_spacing``."""
    if len(lines) == 0:
        return lines
    lines = np.sort(np.asarray(lines, dtype=np.float64))
    kept = [float(lines[0])]
    for v in lines[1:]:
        if v - kept[-1] >= min_spacing:
            kept.append(float(v))
    return np.asarray(kept, dtype=np.float64)


def _subdivide(lines: np.ndarray, max_res: float) -> np.ndarray:
    """Split any gap larger than ``max_res`` into equal parts."""
    out: list[float] = []
    for a, b in zip(lines, lines[1:]):
        out.append(float(a))
        gap = b - a
        if gap > max_res:
            n = int(math.ceil(gap / max_res))
            out.extend(float(a + gap * i / n) for i in range(1, n))
    out.append(float(lines[-1]))
    return np.asarray(out, dtype=np.float64)


def _fill_gap(length: float, s_left: float, s_right: float,
              max_res: float, ratio: float) -> list[float]:
    """Interior offsets within one gap, graded in from both ends.

    Cells grow geometrically from each end at ``ratio``, capped at ``max_res``, meeting
    somewhere in the middle; the series is then scaled to fit the gap exactly.

    Both halves matter. Growing from one end only leaves the far end abruptly meeting its
    neighbour, and stopping the series early to let the remainder be one final cell leaves
    a tail cell up to twice its neighbour — a worse discontinuity than the one being fixed.

    **The direction of that final scaling is what decides whether the grading bound holds.**
    A series that stops just short of the gap has to be stretched to fit, and stretching
    multiplies the first cell too: measured, that put the first cell 1.5-2.7x its neighbour
    where the bound is 1.4, which is most of why real boards came out with 2:1 and 4:1 jumps.
    Taking one cell more than fits and shrinking instead keeps every cell at or below its
    target. Shrinking is only allowed while the smallest cell stays at or above the neighbour
    the series grew from, so this can never shorten the timestep; where it would, the old
    stretched series is used and the gap is simply one the bound cannot be met in.
    """
    if length <= 0:
        return []
    s_left = max(min(s_left, max_res), 1e-12)
    s_right = max(min(s_right, max_res), 1e-12)
    if length <= max_res and length <= max(s_left, s_right) * ratio:
        return []  # a single cell already satisfies both the grading and the wavelength bound

    left: list[float] = []
    right: list[float] = []
    ls = min(s_left * ratio, max_res)
    rs = min(s_right * ratio, max_res)
    total = 0.0
    under: list[float] | None = None
    under_total = 0.0

    # Always extend whichever side currently has the smaller next cell, so the two series
    # meet where their sizes match rather than at an arbitrary midpoint.
    for _ in range(4000):
        take_left = ls <= rs
        nxt = ls if take_left else rs
        if total + nxt > length:
            # Keep the series that fits, then take one cell more than fits as well, so the
            # caller can choose between stretching the first and shrinking the second.
            under, under_total = left + right[::-1], total
            if take_left:
                left.append(ls)
            else:
                right.append(rs)
            total += nxt
            break
        total += nxt
        if take_left:
            left.append(ls)
            ls = min(ls * ratio, max_res)
        else:
            right.append(rs)
            rs = min(rs * ratio, max_res)

    over, over_total = left + right[::-1], total
    if under is None:  # the series happened to fit exactly
        under, under_total = over, over_total

    # Shrinking is preferred, and only rejected when it would take a cell below the neighbour
    # it grew from -- which is the one thing that would cost timesteps for the whole run.
    smallest_neighbour = min(s_left, s_right)
    sizes, total = under, under_total
    if len(over) >= 2 and over_total > 0:
        scale = length / over_total
        if min(over) * scale >= smallest_neighbour - 1e-12:
            sizes, total = over, over_total

    if len(sizes) < 2 or total <= 0:
        return []

    scale = length / total
    pos = 0.0
    out: list[float] = []
    for v in sizes[:-1]:
        pos += v * scale
        out.append(pos)
    return out


def _smooth_ratio(lines: np.ndarray, ratio: float, min_res: float,
                  max_passes: int = 12) -> np.ndarray:
    """Grade every cell-size jump left over, wherever it is.

    The gap filling above grades the span between two required lines; this fixes what is left
    where those spans meet each other, and where the PML padding meets the mesh.

    **Both bounds here are local, not min_res, and that is the whole point.** Copper puts
    required lines closer together than min_res -- a pad edge 40 um from a trace edge when
    min_res is 600 um -- and the two rules that used to be written in terms of min_res both
    misfired exactly there: a jump next to a cell smaller than 2 * min_res was left alone, and
    the merge that follows an insert deleted the new lines again because they were closer
    together than a quarter of min_res. Measured on the fixture board, that left ratios of
    2.0-4.5 at every preset against a stated bound of 1.4.

    Nothing here can shorten the timestep: _fill_gap grows from the smaller neighbour, so the
    cells it inserts are never smaller than a cell the mesh already had.
    """
    lines = np.asarray(lines, dtype=np.float64)
    for _ in range(max_passes):
        sizes = np.diff(lines)
        if len(sizes) < 2:
            return lines

        inserts: list[float] = []
        for i in range(len(sizes) - 1):
            a, b = sizes[i], sizes[i + 1]
            # Grade from the *small* neighbour on both sides. Passing the large cell as
            # its own reference makes _fill_gap decide the gap is already fine and return
            # nothing, which is how a 200:1 jump survived at the PML seam.
            if b > a * ratio:
                inserts.extend(
                    lines[i + 1] + off for off in _fill_gap(b, a, a, b, ratio)
                )
            elif a > b * ratio:
                inserts.extend(
                    lines[i] + off for off in _fill_gap(a, b, b, a, ratio)
                )

        if not inserts:
            return lines
        merged = merge_close(
            np.concatenate([lines, np.asarray(inserts, dtype=np.float64)]),
            _merge_tolerance(lines, min_res),
        )
        if len(merged) <= len(lines):
            return merged
        lines = merged
    return lines


def _merge_tolerance(lines: np.ndarray, min_res: float) -> float:
    """How close two lines may be before they are treated as one.

    A fraction of min_res is right for the lines a caller asks for, and wrong for the ones
    grading produces: next to copper the cells are already far below min_res, so that
    tolerance swallows the very lines that were inserted to grade the jump. Taking the
    smallest existing cell into account keeps the tolerance below anything the mesh is
    already resolving.
    """
    sizes = np.diff(np.asarray(lines, dtype=np.float64))
    smallest = float(sizes.min()) if len(sizes) else min_res
    return min(min_res, smallest) * MERGE_FRACTION


def _pad_pml(lines: np.ndarray, count: int = PML_LINES) -> np.ndarray:
    """Extend an axis outward by ``count`` lines at each end.

    The padding continues the outermost cell size and grows it gently, so the absorbing
    layer sits in a region whose mesh is not itself a discontinuity.
    """
    if len(lines) < 2:
        raise MeshError("cannot pad an axis with fewer than two lines")

    lo_step = lines[1] - lines[0]
    hi_step = lines[-1] - lines[-2]

    lo: list[float] = []
    v, step = float(lines[0]), float(lo_step)
    for _ in range(count):
        v -= step
        step *= 1.2
        lo.append(v)

    hi: list[float] = []
    v, step = float(lines[-1]), float(hi_step)
    for _ in range(count):
        v += step
        step *= 1.2
        hi.append(v)

    return np.concatenate([np.asarray(sorted(lo)), lines, np.asarray(hi)])


def build_axis(
    required: list[float],
    min_res: float,
    max_res: float,
    ratio: float = MAX_CELL_RATIO,
    pml: bool = True,
) -> np.ndarray:
    """Build one axis of the grid.

    ``required`` lines survive; everything else is filler chosen to satisfy the resolution
    and grading bounds.
    """
    if min_res <= 0 or max_res <= 0:
        raise MeshError("mesh resolutions must be positive")
    if max_res < min_res:
        max_res = min_res

    lines = merge_close(np.asarray(required, dtype=np.float64), min_res * MERGE_FRACTION)
    if len(lines) < 2:
        raise MeshError("an axis needs at least two distinct required lines")

    # Fill each gap between required lines, grading in from both neighbours so a dense
    # region hands over smoothly to a sparse one. The gap fill also enforces the wavelength
    # bound, since no generated cell exceeds max_res.
    filled: list[float] = [float(lines[0])]
    gaps = np.diff(lines)
    for i, gap in enumerate(gaps):
        s_left = float(gaps[i - 1]) if i > 0 else float(gap)
        s_right = float(gaps[i + 1]) if i + 1 < len(gaps) else float(gap)
        for off in _fill_gap(float(gap), s_left, s_right, max_res, ratio):
            filled.append(float(lines[i]) + off)
        filled.append(float(lines[i + 1]))
    lines = merge_close(np.asarray(filled), min_res * MERGE_FRACTION)

    # A gap whose own neighbours were coarse can still exceed max_res after one pass, so
    # subdivide anything left over before smoothing the seams.
    lines = _subdivide(lines, max_res)
    lines = _smooth_ratio(lines, ratio, min_res)
    if pml:
        # Padding continues the outermost cell size, but the join between the padding and
        # the mesh is itself a place where two cell sizes meet — so smooth again afterwards
        # rather than trusting the seam. Skipping this left exactly one 2:1 jump per axis,
        # sitting right where the absorbing boundary begins, which is the worst place for a
        # numerical reflection.
        lines = _smooth_ratio(_pad_pml(lines), ratio, min_res)
    # Same tolerance rule as the smoothing pass: a flat fraction of min_res would undo the
    # grading it just did wherever copper forced cells below min_res.
    return merge_close(lines, _merge_tolerance(lines, min_res))


@dataclass
class MeshSpec:
    """What the caller wants from the mesh."""

    #: Region of interest in board space, mm: (min_x, min_y, max_x, max_y).
    roi: tuple[float, float, float, float]
    #: Highest frequency to resolve, Hz. Sets the largest usable cell.
    f_max: float
    #: Smallest cell per axis, um. dz is usually the binding one.
    dx_um: float
    dy_um: float
    dz_um: float
    #: Air above the top copper and below the bottom copper, mm.
    air_above_mm: float = 5.0
    air_below_mm: float = 5.0
    #: Largest relative permittivity in the stack; the wavelength is shortest there, so it
    #: sets the coarsest cell the whole grid may use.
    max_epsilon_r: float = 4.4
    ratio: float = MAX_CELL_RATIO


@dataclass
class Mesh:
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray

    @property
    def cells(self) -> int:
        return len(self.x) * len(self.y) * len(self.z)

    @property
    def min_cell_mm(self) -> float:
        return float(min(np.diff(self.x).min(), np.diff(self.y).min(), np.diff(self.z).min()))

    @property
    def max_cell_mm(self) -> float:
        return float(max(np.diff(self.x).max(), np.diff(self.y).max(), np.diff(self.z).max()))

    @property
    def max_ratio(self) -> float:
        """Largest step in cell size between neighbouring cells, over all three axes.

        Reported because a grid that violates its own grading bound is the leading suspect for
        a diverging run, and until this was measured nobody could tell that it did.
        """
        worst = 1.0
        for axis in (self.x, self.y, self.z):
            d = np.diff(axis)
            if len(d) < 2:
                continue
            worst = max(worst, float(np.maximum(d[1:] / d[:-1], d[:-1] / d[1:]).max()))
        return worst

    def timestep_seconds(self) -> float:
        """Courant limit for a non-uniform grid, from the smallest cell in any axis."""
        d = self.min_cell_mm * 1e-3
        return d / (SPEED_OF_LIGHT * math.sqrt(3))

    def summary(self) -> dict:
        return {
            "cells": self.cells,
            "lines": [len(self.x), len(self.y), len(self.z)],
            "min_cell_um": round(self.min_cell_mm * 1000, 3),
            "max_cell_um": round(self.max_cell_mm * 1000, 1),
            "max_cell_ratio": round(self.max_ratio, 3),
            "dt_seconds": self.timestep_seconds(),
        }


def max_cell_for_frequency(f_max: float, epsilon_r: float) -> float:
    """Largest cell that still resolves the shortest wavelength, in mm.

    The wavelength is shortest in the highest-permittivity material, so that is what bounds
    the cell size everywhere — a rectilinear grid line crosses the whole domain and cannot
    be coarse in the air and fine in the dielectric independently.
    """
    if f_max <= 0:
        raise MeshError("f_max must be positive")
    lam_mm = (SPEED_OF_LIGHT / (f_max * math.sqrt(max(1.0, epsilon_r)))) * 1000.0
    return lam_mm / CELLS_PER_WAVELENGTH


def build_mesh(
    spec: MeshSpec,
    copper_x: list[float],
    copper_y: list[float],
    layer_z: list[float],
) -> Mesh:
    """Build the full 3-D grid.

    ``copper_x``/``copper_y`` are in-plane features that must land on grid lines — trace
    edges, pad edges, port boundaries. ``layer_z`` are the copper layer heights.
    """
    min_x, min_y, max_x, max_y = spec.roi
    if max_x <= min_x or max_y <= min_y:
        raise MeshError("the region of interest has no area")
    if not layer_z:
        raise MeshError("no copper layers to mesh")

    max_res = max_cell_for_frequency(spec.f_max, spec.max_epsilon_r)

    # In-plane: the ROI bounds plus every copper feature inside them.
    def inside(values: list[float], lo: float, hi: float) -> list[float]:
        return [v for v in values if lo <= v <= hi]

    x_req = [min_x, max_x] + inside(copper_x, min_x, max_x)
    y_req = [min_y, max_y] + inside(copper_y, min_y, max_y)

    x = build_axis(x_req, spec.dx_um / 1000.0, max_res, spec.ratio)
    y = build_axis(y_req, spec.dy_um / 1000.0, max_res, spec.ratio)

    # Vertical: every copper layer, plus air boxes. The dielectric between layers needs
    # several cells through it, which is what dz_um is really specifying.
    z_lo = min(layer_z) - spec.air_below_mm
    z_hi = max(layer_z) + spec.air_above_mm
    z_req = sorted(set([z_lo, z_hi] + list(layer_z)))

    # Resolve the dielectric between copper layers at dz. This is the resolution that
    # matters: the field between a signal layer and its reference plane is what sets the
    # impedance, and a single cell across a thin prepreg gets it wrong.
    #
    # It applies *only between copper layers*. Slicing the air boxes at the same resolution
    # would add hundreds of lines describing nothing, and since the timestep is set by the
    # smallest cell anywhere, those lines cost the whole run.
    dz_mm = spec.dz_um / 1000.0
    cu_lo, cu_hi = min(layer_z), max(layer_z)
    filled: list[float] = []
    for a, b in zip(z_req, z_req[1:]):
        filled.append(a)
        gap = b - a
        in_board = a >= cu_lo - 1e-9 and b <= cu_hi + 1e-9
        if in_board and gap > dz_mm:
            n = max(1, int(math.ceil(gap / dz_mm)))
            n = min(n, 200)  # a pathological stackup must not produce a million lines
            filled.extend(a + gap * i / n for i in range(1, n))
    filled.append(z_req[-1])

    z = build_axis(filled, dz_mm, max_res, spec.ratio)

    return Mesh(x=x, y=y, z=z)
