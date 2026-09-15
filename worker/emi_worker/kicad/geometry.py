"""Turn KiCad shapes into polygons, and polygons into triangles.

Two rules shape everything here.

*Triangulate on the worker.* The browser gets a flat float32 triangle buffer and needs no
geometry library at all. More importantly it guarantees the picture the user sees is the
same polygon set that gets meshed -- a viewer that draws slightly different geometry from
what the solver consumes is a bug factory.

*Err toward more copper, never less.* Curves are approximated by inscribed-or-circumscribed
polygons; where there is a choice, take the one that covers at least the real shape. A mesh
that includes a sliver of extra copper is wrong by a rounding error. One that misses a
connection is wrong by a net.
"""

from __future__ import annotations

import math

import numpy as np

try:
    import mapbox_earcut as earcut
except ImportError:  # pragma: no cover - the pcb extra is not installed
    earcut = None

Point = tuple[float, float]
Ring = list[Point]

#: Maximum distance between a curve and its polygon approximation, in mm.
#: 5 um is far below any PCB feature and well below the finest mesh we would ever build.
CURVE_TOLERANCE_MM = 0.005

#: Bounds on how finely a curve is subdivided. The floor keeps small vias from becoming
#: triangles; the ceiling stops a large arc from generating thousands of points.
MIN_ARC_SEGMENTS = 8
MAX_ARC_SEGMENTS = 180


def _segments_for_radius(radius: float, sweep_rad: float = 2 * math.pi) -> int:
    """How many chords approximate an arc within CURVE_TOLERANCE_MM.

    Sagitta of a chord subtending angle a on radius r is r*(1 - cos(a/2)); solving for the
    tolerance gives the step angle.
    """
    if radius <= CURVE_TOLERANCE_MM:
        return MIN_ARC_SEGMENTS
    ratio = 1.0 - CURVE_TOLERANCE_MM / radius
    ratio = max(-1.0, min(1.0, ratio))
    step = 2.0 * math.acos(ratio)
    if step <= 0:
        return MAX_ARC_SEGMENTS
    n = int(math.ceil(abs(sweep_rad) / step))
    return max(MIN_ARC_SEGMENTS, min(MAX_ARC_SEGMENTS, n))


def rotate(x: float, y: float, angle_deg: float) -> Point:
    """Rotate a point the way KiCad does.

    KiCad stores angles counterclockwise as seen on screen, but its Y axis points down, so
    the transform is a rotation by *minus* the stored angle in ordinary maths terms. Getting
    this backwards mirrors every rotated footprint about its own centre, which is subtle
    enough on a symmetric part and glaring on an SOT-23.
    """
    if angle_deg == 0.0:
        return (x, y)
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    return (x * ca + y * sa, -x * sa + y * ca)


def circle(cx: float, cy: float, radius: float) -> Ring:
    """A closed circle, circumscribing the true circle so no copper is lost."""
    if radius <= 0:
        return []
    n = _segments_for_radius(radius)
    # Scale up so the polygon's inscribed radius equals the true radius.
    r = radius / math.cos(math.pi / n)
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def capsule(p0: Point, p1: Point, width: float) -> Ring:
    """A track segment: a rectangle of the given width with round caps.

    Round caps are not cosmetic. KiCad tracks really are drawn that way, and at a corner
    two segments overlap only because of them; square caps would leave a notch exactly
    where the current turns.
    """
    if width <= 0:
        return []
    r = width / 2.0
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return circle(x0, y0, r)

    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux  # left normal

    n = max(MIN_ARC_SEGMENTS, _segments_for_radius(r, math.pi))
    ring: Ring = []

    # Cap around p1, sweeping from the left side to the right side.
    base = math.atan2(ny, nx)
    for i in range(n + 1):
        a = base - math.pi * i / n
        ring.append((x1 + r * math.cos(a), y1 + r * math.sin(a)))
    # Cap around p0, the other half turn.
    base = math.atan2(-ny, -nx)
    for i in range(n + 1):
        a = base - math.pi * i / n
        ring.append((x0 + r * math.cos(a), y0 + r * math.sin(a)))
    return ring


def rect(cx: float, cy: float, w: float, h: float, angle_deg: float = 0.0) -> Ring:
    hw, hh = w / 2.0, h / 2.0
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    return [
        (cx + rx, cy + ry)
        for rx, ry in (rotate(x, y, angle_deg) for x, y in corners)
    ]


def roundrect(cx: float, cy: float, w: float, h: float, radius: float,
              angle_deg: float = 0.0) -> Ring:
    radius = max(0.0, min(radius, min(w, h) / 2.0))
    if radius <= 1e-9:
        return rect(cx, cy, w, h, angle_deg)

    hw, hh = w / 2.0 - radius, h / 2.0 - radius
    n = _segments_for_radius(radius, math.pi / 2)
    ring: Ring = []
    # Corner centres, counterclockwise, each with the angle its quarter-arc starts at.
    for (ox, oy), start in (
        ((hw, hh), 0.0),
        ((-hw, hh), math.pi / 2),
        ((-hw, -hh), math.pi),
        ((hw, -hh), 3 * math.pi / 2),
    ):
        for i in range(n + 1):
            a = start + (math.pi / 2) * i / n
            ring.append((ox + radius * math.cos(a), oy + radius * math.sin(a)))

    return [(cx + rx, cy + ry) for rx, ry in (rotate(x, y, angle_deg) for x, y in ring)]


def oval(cx: float, cy: float, w: float, h: float, angle_deg: float = 0.0) -> Ring:
    """A stadium: KiCad's "oval" pad is a rectangle with semicircular ends."""
    return roundrect(cx, cy, w, h, min(w, h) / 2.0, angle_deg)


def trapezoid(cx: float, cy: float, w: float, h: float, dx: float, dy: float,
              angle_deg: float = 0.0) -> Ring:
    """KiCad's trapezoid pad: rect_delta widens one axis and narrows the other."""
    hw, hh = w / 2.0, h / 2.0
    ddx, ddy = dy / 2.0, dx / 2.0
    corners = [
        (-hw - ddx, -hh - ddy),
        (hw + ddx, -hh + ddy),
        (hw - ddx, hh - ddy),
        (-hw + ddx, hh + ddy),
    ]
    return [(cx + rx, cy + ry) for rx, ry in (rotate(x, y, angle_deg) for x, y in corners)]


def arc_polyline(start: Point, mid: Point, end: Point) -> list[Point]:
    """Points along the circular arc through three points.

    KiCad stores arcs by start/mid/end rather than centre and angles, so the centre has to
    be recovered as the circumcentre. Three collinear points have none, which is not an
    error -- it is a degenerate arc, and a straight line is the right answer.
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end

    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]

    s1 = x1 * x1 + y1 * y1
    s2 = x2 * x2 + y2 * y2
    s3 = x3 * x3 + y3 * y3
    ux = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
    uy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    if r < 1e-9:
        return [start, end]

    a1 = math.atan2(y1 - uy, x1 - ux)
    am = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    # Choose the sweep direction that actually passes through the middle point.
    def norm(a: float) -> float:
        while a <= -math.pi:
            a += 2 * math.pi
        while a > math.pi:
            a -= 2 * math.pi
        return a

    ccw = norm(am - a1) > 0 and norm(a3 - am) > 0
    sweep = norm(a3 - a1)
    if ccw and sweep < 0:
        sweep += 2 * math.pi
    elif not ccw and sweep > 0:
        sweep -= 2 * math.pi

    n = _segments_for_radius(r, abs(sweep))
    return [
        (ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n))
        for i in range(n + 1)
    ]


def thick_polyline(pts: list[Point], width: float) -> list[Ring]:
    """A polyline with width, as one capsule per span.

    Overlapping capsules rather than a single mitred outline: the union is identical for
    rendering and meshing, and it sidesteps the self-intersection cases a mitre join
    produces at sharp corners.
    """
    return [capsule(pts[i], pts[i + 1], width) for i in range(len(pts) - 1)]


def signed_area(ring: Ring) -> float:
    a = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a / 2.0


def triangulate(outer: Ring, holes: list[Ring] | None = None) -> np.ndarray:
    """Triangulate one polygon into a flat float32 array of x,y pairs (3 per triangle).

    Uses earcut, which wants a single vertex array plus the index at which each hole ring
    starts. Winding does not matter to earcut, which is convenient because KiCad zone fills
    do not guarantee one.
    """
    if len(outer) < 3:
        return np.zeros(0, dtype=np.float32)
    if earcut is None:
        raise RuntimeError(
            "mapbox_earcut is not installed; install the worker's 'pcb' extra"
        )

    verts: list[Point] = list(outer)
    ring_ends = [len(verts)]
    for hole in holes or []:
        if len(hole) < 3:
            continue
        verts.extend(hole)
        ring_ends.append(len(verts))

    arr = np.asarray(verts, dtype=np.float64)
    try:
        idx = earcut.triangulate_float64(arr, np.asarray(ring_ends, dtype=np.uint32))
    except Exception:
        # A degenerate ring should cost us one shape, not the whole board.
        return np.zeros(0, dtype=np.float32)
    if len(idx) == 0:
        return np.zeros(0, dtype=np.float32)
    return arr[np.asarray(idx, dtype=np.int64)].astype(np.float32).reshape(-1)


def triangulate_rings(rings: list[Ring]) -> np.ndarray:
    """Triangulate many independent, non-overlapping-in-intent polygons."""
    parts = [triangulate(r) for r in rings if len(r) >= 3]
    parts = [p for p in parts if p.size]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)


def bounds(rings: list[Ring]) -> tuple[float, float, float, float] | None:
    """Axis-aligned bounds of a set of rings, as (min_x, min_y, max_x, max_y)."""
    xs: list[float] = []
    ys: list[float] = []
    for ring in rings:
        for x, y in ring:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))
