"""Turn a BoardModel into the normalised pair everything downstream reads.

``board.json``   metadata: stackup with z heights, layers, nets, vias, outline, warnings,
                 and a byte index into the triangle buffer
``geometry.bin`` one flat float32 array of x,y pairs, three vertices per triangle

Two decisions worth stating.

*Triangles, grouped by (layer, net).* The browser uploads the whole buffer once as a static
vertex buffer and then highlights a net or hides a layer by changing a uniform and a draw
range -- no re-fetch, no re-upload, no geometry library in the frontend.

*One coordinate system, converted once.* KiCad works in millimetres with Y pointing down
and an arbitrary page origin. Everything downstream of this module works in millimetres
with Y up and the origin at the board's bottom-left corner. Doing the flip here, exactly
once, is what stops a sign error from appearing in the viewer, the rules and the mesher
independently.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

import numpy as np

from . import geometry as g
from .board import BoardModel, Pad, Track, Via

FORMAT_VERSION = 1

#: Zone fills are by far the largest source of triangles: a ground pour on a dense board
#: can carry tens of thousands of vertices per polygon, almost all of them describing
#: clearance cut-outs a viewer cannot see. Rings longer than this are simplified before
#: triangulation; the mesher works from the source polygons, not from this buffer.
ZONE_SIMPLIFY_ABOVE = 400

#: Douglas-Peucker tolerance used by that simplification, in mm. Well below the finest
#: mesh, and below a pixel at any realistic zoom.
ZONE_SIMPLIFY_TOLERANCE_MM = 0.02


def _simplify(ring: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker, iterative so a 40,000-point pour cannot blow the stack."""
    if len(ring) < 3:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        x0, y0 = ring[start]
        x1, y1 = ring[end]
        dx, dy = x1 - x0, y1 - y0
        norm = math.hypot(dx, dy)

        worst, worst_i = -1.0, -1
        for i in range(start + 1, end):
            px, py = ring[i]
            if norm < 1e-12:
                d = math.hypot(px - x0, py - y0)
            else:
                d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / norm
            if d > worst:
                worst, worst_i = d, i

        if worst > tol and worst_i > 0:
            keep[worst_i] = True
            stack.append((start, worst_i))
            stack.append((worst_i, end))

    out = [p for p, k in zip(ring, keep) if k]
    return out if len(out) >= 3 else ring


class Transform:
    """KiCad space (mm, Y down, page origin) -> board space (mm, Y up, corner origin)."""

    def __init__(self, min_x: float, min_y: float, max_x: float, max_y: float):
        self.min_x, self.min_y = min_x, min_y
        self.max_x, self.max_y = max_x, max_y
        self.width = max_x - min_x
        self.height = max_y - min_y

    def pt(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.min_x, self.max_y - y)

    def ring(self, ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [self.pt(x, y) for x, y in ring]

    def array(self, arr: np.ndarray) -> np.ndarray:
        """Apply the transform to a packed [x0,y0,x1,y1,...] float32 array in place."""
        if arr.size == 0:
            return arr
        view = arr.reshape(-1, 2)
        view[:, 0] -= np.float32(self.min_x)
        view[:, 1] = np.float32(self.max_y) - view[:, 1]
        return arr


def board_extent(model: BoardModel) -> Transform:
    """Board bounds, from the Edge.Cuts outline when there is one, else from the copper."""
    rings: list[list[tuple[float, float]]] = list(model.outline)
    if not rings:
        rings = [t.pts for t in model.tracks]
        rings += [p.ring for p in model.pads]
        rings += [z.ring for z in model.zones]
        rings += [[(v.x, v.y)] for v in model.vias]
    b = g.bounds([r for r in rings if r])
    if b is None:
        raise ValueError("board has no geometry to measure")
    return Transform(*b)


# The old private names, for callers that have not moved to the public ones yet.
_Transform = Transform
_board_extent = board_extent


def _stackup_with_z(model: BoardModel) -> tuple[list[dict], dict[str, float]]:
    """Assign a z coordinate to every stackup entry, measured up from the bottom.

    Copper z heights are what the mesher needs and what the viewer uses for its exploded
    stackup view. They are derived by walking the physical stack rather than by assuming a
    uniform spacing, because a 4-layer board with a thick core and thin prepregs has very
    non-uniform layer spacing -- and that spacing is exactly what sets the vertical mesh
    resolution, which sets the timestep, which sets the run time.
    """
    entries = [s for s in model.stackup if s.thickness_mm > 0 or s.is_copper]
    total = sum(s.thickness_mm for s in entries)
    if total <= 0:
        total = model.thickness_mm

    # KiCad writes the stackup top-first; z is measured from the bottom.
    out: list[dict] = []
    z = total
    copper_z: dict[str, float] = {}
    for s in entries:
        z_top = z
        z_bottom = z - s.thickness_mm
        out.append({
            "name": s.name,
            "role": s.role,
            "type": s.type,
            "thickness_mm": round(s.thickness_mm, 6),
            "material": s.material,
            "epsilon_r": s.epsilon_r or None,
            "loss_tangent": s.loss_tangent or None,
            "from_file": s.from_file,
            "z_bottom_mm": round(z_bottom, 6),
            "z_top_mm": round(z_top, 6),
        })
        if s.is_copper:
            copper_z[s.name] = round((z_top + z_bottom) / 2.0, 6)
        z = z_bottom
    return out, copper_z


def _track_rings(track: Track) -> list[list[tuple[float, float]]]:
    if len(track.pts) < 2 or track.width_mm <= 0:
        return []
    return g.thick_polyline(track.pts, track.width_mm)


def _via_ring(via: Via) -> list[tuple[float, float]]:
    r = (via.size_mm or via.drill_mm) / 2.0
    return g.circle(via.x, via.y, r) if r > 0 else []


#: A layer is called a plane when one net's pours cover at least this much of the board.
#: Below it the copper is pours around routing rather than a reference plane, and naming it
#: would be misleading -- a signal layer with a bit of ground fill is not a ground plane.
PLANE_COVERAGE_MIN = 0.30


def _plane_of(layer: str, plane_area: dict, board_area: float) -> dict:
    """What this layer is, as opposed to what the file's layer-type field claims."""
    if board_area <= 0:
        return {}
    best_net, best_area = "", 0.0
    for (lyr, net), area in plane_area.items():
        if lyr == layer and net and area > best_area:
            best_net, best_area = net, area
    coverage = best_area / board_area
    if not best_net or coverage < PLANE_COVERAGE_MIN:
        return {}
    return {"plane_net": best_net, "plane_coverage": round(min(coverage, 1.0), 3)}


def normalize(model: BoardModel, source: dict | None = None) -> tuple[dict, bytes]:
    """Produce ``(board_doc, geometry_bytes)``."""
    tf = board_extent(model)
    stackup, copper_z = _stackup_with_z(model)
    copper_names = set(model.copper_layer_names)

    # Collect rings per (layer, net). Anything on a non-copper layer is ignored: silkscreen
    # and courtyards are not conductors and have no business in a field solve.
    rings: dict[tuple[str, str], list[list[tuple[float, float]]]] = defaultdict(list)
    net_stats: dict[str, dict] = defaultdict(
        lambda: {"tracks": 0, "vias": 0, "pads": 0, "length_mm": 0.0, "layers": set()}
    )

    for track in model.tracks:
        if track.layer not in copper_names:
            continue
        parts = _track_rings(track)
        if not parts:
            continue
        rings[(track.layer, track.net)].extend(parts)
        st = net_stats[track.net]
        st["tracks"] += 1
        st["length_mm"] += track.length_mm
        st["layers"].add(track.layer)

    for pad in model.pads:
        st = net_stats[pad.net]
        st["pads"] += 1
        for layer in pad.layers:
            if layer in copper_names:
                rings[(layer, pad.net)].append(pad.ring)
                st["layers"].add(layer)
            elif layer == "*.Cu":
                # A through-hole pad's copper exists on every layer.
                for name in model.copper_layer_names:
                    rings[(name, pad.net)].append(pad.ring)
                    st["layers"].add(name)

    for via in model.vias:
        ring = _via_ring(via)
        if not ring:
            continue
        st = net_stats[via.net]
        st["vias"] += 1
        spanned = [n for n in model.copper_layer_names if n in via.layers]
        if len(spanned) >= 2:
            # A via spans from its first to its last named layer, inclusive of everything
            # between -- listing only the endpoints would leave an inner-layer pad missing.
            names = model.copper_layer_names
            lo, hi = names.index(spanned[0]), names.index(spanned[-1])
            spanned = names[min(lo, hi):max(lo, hi) + 1]
        for name in spanned or model.copper_layer_names:
            rings[(name, via.net)].append(ring)
            st["layers"].add(name)

    # Which net owns each layer, by poured area. A layer's type in the board file says what
    # the designer set a dropdown to; this says what is actually on it, which is what the
    # user needs to see -- "GND plane" is a fact about the board, "power" is a label.
    plane_area: dict[tuple[str, str], float] = defaultdict(float)

    for zone in model.zones:
        if zone.layer not in copper_names:
            continue
        plane_area[(zone.layer, zone.net)] += g.ring_area(zone.ring)
        ring = zone.ring
        if len(ring) > ZONE_SIMPLIFY_ABOVE:
            ring = _simplify(ring, ZONE_SIMPLIFY_TOLERANCE_MM)
        rings[(zone.layer, zone.net)].append(ring)
        net_stats[zone.net]["layers"].add(zone.layer)

    # Triangulate, transform, and pack into one buffer with an index.
    buffers: list[np.ndarray] = []
    groups: list[dict] = []
    offset = 0
    for (layer, net), ring_list in sorted(rings.items()):
        tris = g.triangulate_rings(ring_list)
        if tris.size == 0:
            continue
        tf.array(tris)
        buffers.append(tris)
        vertex_count = tris.size // 2
        groups.append({
            "layer": layer,
            "net": net,
            "offset": offset,          # in vertices, not bytes
            "count": vertex_count,
        })
        offset += vertex_count

    geometry = (
        np.concatenate(buffers) if buffers else np.zeros(0, dtype=np.float32)
    ).astype("<f4", copy=False)

    nets = []
    for i, name in enumerate(model.nets):
        st = net_stats.get(name)
        if st is None:
            continue
        nets.append({
            "index": i,
            "name": name,
            "tracks": st["tracks"],
            "vias": st["vias"],
            "pads": st["pads"],
            "length_mm": round(st["length_mm"], 4),
            "layers": sorted(st["layers"]),
        })

    outline = [tf.ring(r) for r in model.outline]

    board_area = tf.width * tf.height

    doc = {
        "format_version": FORMAT_VERSION,
        "source": source or {},
        "units": "mm",
        "coordinate_system": {
            "x": "right", "y": "up", "z": "up from the bottom copper",
            "origin": "bottom-left corner of the board extent",
            "note": "KiCad's Y-down page coordinates are converted here, once.",
        },
        "board": {
            "width_mm": round(tf.width, 4),
            "height_mm": round(tf.height, 4),
            "thickness_mm": round(model.thickness_mm, 4),
            "outline": [[[round(x, 4), round(y, 4)] for x, y in r] for r in outline],
        },
        "layers": [
            {
                "name": layer.name,
                "kind": layer.kind,
                "index": i,
                "z_mm": copper_z.get(layer.name, 0.0),
                **_plane_of(layer.name, plane_area, board_area),
            }
            for i, layer in enumerate(model.copper_layers)
        ],
        "stackup": stackup,
        "nets": nets,
        "vias": [
            {
                "x": round(tf.pt(v.x, v.y)[0], 4),
                "y": round(tf.pt(v.x, v.y)[1], 4),
                "size_mm": v.size_mm,
                "drill_mm": v.drill_mm,
                "net": v.net,
                "layers": [n for n in model.copper_layer_names if n in v.layers],
                "kind": v.kind,
            }
            for v in model.vias
        ],
        "pads": [
            {
                "ref": p.ref, "number": p.number, "net": p.net,
                "x": round(tf.pt(p.x, p.y)[0], 4),
                "y": round(tf.pt(p.x, p.y)[1], 4),
                "type": p.pad_type,
                "drill_mm": p.drill_mm,
                "layers": [n for n in p.layers if n in copper_names or n == "*.Cu"],
            }
            for p in model.pads
        ],
        "geometry": {
            "file": "geometry.bin",
            "dtype": "float32",
            "components": 2,
            "primitive": "triangles",
            "vertex_count": int(geometry.size // 2),
            "byte_length": int(geometry.nbytes),
            "groups": groups,
        },
        "kicad": {"version": model.version, "generator": model.generator},
        "warnings": model.warnings,
    }
    return doc, geometry.tobytes()


def to_json(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":")).encode()
