"""Which layers are reference planes, decided from what is poured on them.

Several checks need this and they must agree, so it lives in one place. A layer is a plane
when one net's pours cover enough of the board -- the same threshold board.json uses for its
plane labels, so a layer the UI calls "GND 95%" is the layer these checks treat as ground.
"""

from __future__ import annotations

from collections import defaultdict

from ..kicad.board import BoardModel
from ..kicad.normalize import _ring_area
from .model import classify_net

PLANE_COVERAGE_MIN = 0.30


def board_bbox(model: BoardModel) -> tuple[float, float, float, float] | None:
    pts = [p for ring in model.outline for p in ring] or [p for z in model.zones for p in z.ring]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def plane_layers(model: BoardModel, min_coverage: float = PLANE_COVERAGE_MIN) -> dict[str, str]:
    """layer -> the net that pours over it, for layers that are planes."""
    bbox = board_bbox(model)
    if bbox is None:
        return {}
    board = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    if board <= 0:
        return {}
    area: dict[tuple[str, str], float] = defaultdict(float)
    for z in model.zones:
        if z.net:
            area[(z.layer, z.net)] += _ring_area(z.ring)
    best: dict[str, tuple[str, float]] = {}
    for (layer, net), a in area.items():
        if a / board >= min_coverage and a > best.get(layer, ("", 0.0))[1]:
            best[layer] = (net, a)
    return {layer: net for layer, (net, _) in best.items()}


def ground_planes(model: BoardModel) -> dict[str, str]:
    return {l: n for l, n in plane_layers(model).items() if classify_net(n) == "ground"}


def severity(ctx, rule: str, natural: str) -> str:
    return ctx.settings.severity(rule, natural) if ctx.settings is not None else natural


def point_segment_distance(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return ((px - x0) ** 2 + (py - y0) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / L2))
    qx, qy = x0 + t * dx, y0 + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5
