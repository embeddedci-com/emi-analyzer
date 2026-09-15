"""Stitching vias: are the ground planes tied together often enough, especially at the edge?

Two ground planes joined only at a few points are not one ground. Between those points they
form a parallel-plate cavity, and a cavity resonates -- at a frequency set by the distance
between the vias that tie it. Keeping every point of the overlap within about a twentieth of
a wavelength of a stitching via pushes that resonance above anything the board produces.

At the board edge the same gap becomes a slot antenna, which is why edge stitching is checked
separately: a via fence along the outline stops a plane edge radiating.

Spacing defaults to lambda/20 at the board's maximum frequency, from the same stackup-derived
velocity the other checks use.
"""

from __future__ import annotations

import math
from typing import Iterator

import numpy as np

from ..raster import label_components, rasterize_rings
from .model import Finding, RuleContext, classify_net
from .planes import board_bbox, ground_planes, severity

RES_MM = 0.5
MAX_REGIONS = 10


def _spacing(ctx: RuleContext, rule: str) -> float:
    s = float(ctx.setting(rule, "max_spacing_mm") or 0.0)
    return s if s > 0 else ctx.wavelength_mm / 20.0


def _grid(ctx: RuleContext):
    bbox = board_bbox(ctx.model)
    if bbox is None:
        return None
    ox, oy = bbox[0], bbox[1]
    w = int((bbox[2] - bbox[0]) / RES_MM) + 2
    h = int((bbox[3] - bbox[1]) / RES_MM) + 2
    if w * h > 16_000_000:
        return None
    return (ox, oy), (h, w)


def _coverage(ctx: RuleContext, layer: str, net: str, origin, shape) -> np.ndarray:
    rings = [z.ring for z in ctx.model.zones if z.layer == layer and z.net == net]
    return rasterize_rings(rings, origin, shape, RES_MM)


def _stitch_points(ctx: RuleContext) -> np.ndarray:
    """Every place ground layers are joined: ground vias, and plated ground holes.

    A through via spans every layer however its layer list is written, so its span is not
    consulted; KiCad records a through via as F.Cu/B.Cu and that must not read as "misses the
    inner planes".
    """
    pts = [
        (v.x, v.y) for v in ctx.model.vias
        if v.net and classify_net(v.net) == "ground"
        and (v.kind == "through" or len(set(v.layers)) >= 2)
    ]
    pts += [
        (p.x, p.y) for p in ctx.model.pads
        if p.net and classify_net(p.net) == "ground" and p.is_through
    ]
    return np.array(pts, dtype=np.float64) if pts else np.zeros((0, 2))


def _nearest(points: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Distance from every (ys x xs) cell centre to the nearest point, in chunks."""
    out = np.full((len(ys), len(xs)), np.inf)
    if len(points) == 0:
        return out
    step = max(1, int(2_000_000 / max(1, len(xs) * len(points))))
    px = points[:, 0][None, None, :]
    py = points[:, 1][None, None, :]
    xx = xs[None, :, None]
    for r0 in range(0, len(ys), step):
        yy = ys[r0:r0 + step][:, None, None]
        out[r0:r0 + step] = np.sqrt((xx - px) ** 2 + (yy - py) ** 2).min(axis=2)
    return out


def check_stitching(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled("stitching"):
        return
    planes = ground_planes(ctx.model)
    if len(planes) < 2:
        return  # one ground plane has nothing to be stitched to
    g = _grid(ctx)
    if g is None:
        return
    origin, shape = g
    overlap = np.ones(shape, dtype=bool)
    for layer, net in planes.items():
        overlap &= _coverage(ctx, layer, net, origin, shape)
    if not overlap.any():
        return

    spacing = _spacing(ctx, "stitching")
    points = _stitch_points(ctx)
    if len(points) == 0:
        # Worth saying once, plainly, rather than as a region whose "worst distance" is
        # infinite: nothing ties these planes together at all.
        bbox = board_bbox(ctx.model)
        x, y = ctx.pt((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        yield Finding(
            rule="stitching",
            severity=severity(ctx, "stitching", "critical"),
            title=f"No vias stitch the ground planes on {' and '.join(sorted(planes))} together",
            detail=(
                "These layers each carry a ground plane, but no ground via joins them anywhere. "
                "They behave as two separate grounds with a resonant cavity between them. Add "
                f"ground vias across the board, at most {spacing:.1f} mm apart."
            ),
            x=x, y=y,
        )
        return
    xs = origin[0] + (np.arange(shape[1]) + 0.5) * RES_MM
    ys = origin[1] + (np.arange(shape[0]) + 0.5) * RES_MM
    dist = _nearest(points, xs, ys)

    far = overlap & (dist > spacing)
    labels, count = label_components(far)
    if count == 0:
        return

    flat = labels.ravel()
    sizes = np.bincount(flat, minlength=count + 1)
    rows, cols = np.indices(shape)
    sum_r = np.bincount(flat, weights=rows.ravel(), minlength=count + 1)
    sum_c = np.bincount(flat, weights=cols.ravel(), minlength=count + 1)
    worst = np.zeros(count + 1)
    np.maximum.at(worst, flat, np.where(np.isfinite(dist), dist, 0).ravel())

    min_px = max(4, int((spacing / 2 / RES_MM) ** 2))
    regions = [
        (int(sizes[i]), i) for i in range(1, count + 1) if sizes[i] >= min_px
    ]
    regions.sort(reverse=True)
    layers = " and ".join(sorted(planes))

    for size, i in regions[:MAX_REGIONS]:
        cx = origin[0] + (sum_c[i] / sizes[i] + 0.5) * RES_MM
        cy = origin[1] + (sum_r[i] / sizes[i] + 0.5) * RES_MM
        area = size * RES_MM * RES_MM
        w = float(worst[i])
        x, y = ctx.pt(cx, cy)
        yield Finding(
            rule="stitching",
            severity=severity(ctx, "stitching", "critical" if w > 2 * spacing else "warning"),
            title=f"{area:.0f} mm² of ground plane with no stitching via within {spacing:.1f} mm",
            detail=(
                f"Where {layers} overlap, this region is up to {w:.1f} mm from the nearest via "
                f"joining them (budget {spacing:.1f} mm, λ/20 at "
                f"{ctx.max_frequency_hz / 1e6:.0f} MHz). Between stitching points the two planes "
                f"form a cavity that resonates; add ground vias through this area."
            ),
            x=x, y=y,
        )
    if len(regions) > MAX_REGIONS:
        ctx.notes.append(
            f"{len(regions) - MAX_REGIONS} smaller under-stitched ground regions not listed"
        )


def check_edge_stitching(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled("edge-stitching") or not ctx.model.outline:
        return
    planes = ground_planes(ctx.model)
    if not planes:
        return
    g = _grid(ctx)
    if g is None:
        return
    origin, shape = g
    ground = np.zeros(shape, dtype=bool)
    for layer, net in planes.items():
        ground |= _coverage(ctx, layer, net, origin, shape)

    spacing = _spacing(ctx, "edge-stitching")
    band = float(ctx.setting("edge-stitching", "edge_band_mm") or 2.0)
    stitches = _stitch_points(ctx)
    reach = max(1, int(band / RES_MM))
    step = 1.0
    if len(stitches) == 0:
        x, y = ctx.pt(*ctx.model.outline[0][0])
        yield Finding(
            rule="edge-stitching",
            severity=severity(ctx, "edge-stitching", "warning"),
            title="No ground vias along the board edge",
            detail=(
                "A ground plane reaches the board edge, but there are no ground vias at all to "
                "fence it. Add a row along the outline, at most "
                f"{spacing:.1f} mm apart."
            ),
            x=x, y=y,
        )
        return

    for ring in ctx.model.outline:
        samples: list[tuple[float, float]] = []
        for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
            n = max(1, int(math.dist((x0, y0), (x1, y1)) / step))
            samples += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n)]
        if not samples:
            continue

        pts = np.array(samples)
        if len(stitches):
            d = np.sqrt(((pts[:, None, :] - stitches[None, :, :]) ** 2).sum(axis=2)).min(axis=1)
        else:
            d = np.full(len(pts), np.inf)

        gap = []
        for (sx, sy), dist in zip(samples, d):
            c = int((sx - origin[0]) / RES_MM)
            r = int((sy - origin[1]) / RES_MM)
            window = ground[max(0, r - reach):r + reach + 1, max(0, c - reach):c + reach + 1]
            gap.append(bool(window.size and window.any()) and dist > spacing)

        # Contiguous runs of the edge where a plane reaches it and no via is close.
        run_start = None
        for i, is_gap in enumerate(gap + [False]):
            if is_gap and run_start is None:
                run_start = i
            elif not is_gap and run_start is not None:
                length = (i - run_start) * step
                if length >= spacing:
                    mid = samples[(run_start + i - 1) // 2]
                    worst = float(np.max(d[run_start:i]))
                    x, y = ctx.pt(*mid)
                    yield Finding(
                        rule="edge-stitching",
                        severity=severity(ctx, "edge-stitching", "warning"),
                        title=f"{length:.0f} mm of board edge with no stitching via within {spacing:.1f} mm",
                        detail=(
                            f"A ground plane runs to the board edge here, but along {length:.0f} mm "
                            f"of it the nearest ground via is up to {worst:.1f} mm away (budget "
                            f"{spacing:.1f} mm). An unstitched plane edge radiates like a slot "
                            f"antenna. Add a row of ground vias along the edge, spaced at most "
                            f"{spacing:.1f} mm apart."
                        ),
                        x=x, y=y,
                    )
                run_start = None
