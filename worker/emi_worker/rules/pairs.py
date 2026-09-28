"""How a differential pair's two halves are routed against each other.

A pair only rejects noise and only stays quiet while its halves see the same surroundings.
Two ways that fails, both invisible to a length check:

  * The halves split and run apart: over that stretch each is a single-ended line with its
    own loop, and the pair's differential signal converts to common mode, which radiates.
  * A pour of another net runs close beside one half and not the other. That half couples to
    the pour more strongly than to its partner, the two see different impedances, and the
    imbalance converts to common mode the same way. Pour close on both sides is a coplanar
    pair and is fine; it is the difference that counts.

Both are measured by sampling each half along its copper. Stretches near the pair's own pads
and vias are skipped: fanning out to a connector or a pin is expected to split the halves.
"""

from __future__ import annotations

import math
from typing import Iterator

import numpy as np

from ..kicad.geometry import _points_to_segments, ring_edges
from .model import Finding, RuleContext
from .planes import severity

RULE = "pair-coupling"

#: Sample spacing along each half, mm.
STEP_MM = 0.25
#: Samples this close to one of the pair's pads or vias are fan-out, not routing.
FANOUT_MM = 1.0


def _samples(tracks) -> tuple[np.ndarray, np.ndarray]:
    """(N, 2) points along the tracks and the width at each."""
    pts: list[tuple[float, float]] = []
    widths: list[float] = []
    for t in tracks:
        for (x0, y0), (x1, y1) in zip(t.pts, t.pts[1:]):
            n = max(1, math.ceil(math.dist((x0, y0), (x1, y1)) / STEP_MM))
            for i in range(n):
                f = (i + 0.5) / n
                pts.append((x0 + f * (x1 - x0), y0 + f * (y1 - y0)))
                widths.append(t.width_mm)
    return np.asarray(pts, dtype=np.float64).reshape(-1, 2), np.asarray(widths)


def _segments(tracks) -> np.ndarray:
    segs = [(x0, y0, x1, y1) for t in tracks for (x0, y0), (x1, y1) in zip(t.pts, t.pts[1:])]
    return np.asarray(segs, dtype=np.float64).reshape(-1, 4)


def _nearest(points: np.ndarray, segs: np.ndarray) -> np.ndarray:
    """Distance from each point to the nearest segment."""
    return _nearest_with_index(points, segs)[0]


def _nearest_with_index(points: np.ndarray, segs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance from each point to the nearest segment, and which segment that is."""
    if len(points) == 0:
        return np.zeros(0), np.zeros(0, dtype=int)
    if len(segs) == 0:
        return np.full(len(points), np.inf), np.full(len(points), -1)
    out = np.empty(len(points))
    idx = np.empty(len(points), dtype=int)
    # Chunked, so a big pour does not make one enormous broadcast.
    chunk = max(1, 2_000_000 // max(1, len(segs)))
    for i in range(0, len(points), chunk):
        p = points[i:i + chunk]
        d, _, _ = _points_to_segments(p[:, 0:1], p[:, 1:2], segs[None, :, :])
        idx[i:i + chunk] = d.argmin(axis=1)
        out[i:i + chunk] = d.min(axis=1)
    return out, idx


def _away_from(points: np.ndarray, anchors: list[tuple[float, float]]) -> np.ndarray:
    if len(points) == 0 or not anchors:
        return np.ones(len(points), dtype=bool)
    a = np.asarray(anchors)
    d = np.hypot(points[:, 0:1] - a[None, :, 0], points[:, 1:2] - a[None, :, 1])
    return d.min(axis=1) > FANOUT_MM


def check_pair_coupling(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled(RULE) or not ctx.pairs:
        return
    order = ctx.model.copper_layer_names
    tracks_by: dict[tuple[str, str], list] = {}
    for t in ctx.model.tracks:
        if t.net and len(t.pts) >= 2:
            tracks_by.setdefault((t.net, t.layer), []).append(t)
    pours_by_layer: dict[str, list] = {}
    for z in ctx.model.zones:
        if z.net and z.ring:
            pours_by_layer.setdefault(z.layer, []).append(z)
    # Every pour edge on a layer, with the index of the pour it belongs to, built once.
    edges_by_layer: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for layer, zs in pours_by_layer.items():
        parts = [ring_edges([z.ring]) for z in zs]
        edges_by_layer[layer] = (np.concatenate(parts),
                                 np.concatenate([np.full(len(e), i) for i, e in enumerate(parts)]))

    for pair in ctx.pairs:
        halves = (pair.positive, pair.negative)
        max_uncoupled = float(ctx.setting(RULE, "max_uncoupled_mm", net=pair.positive) or 0.0)
        max_asym = float(ctx.setting(RULE, "max_asymmetry_mm", net=pair.positive) or 0.0)
        factor = float(ctx.setting(RULE, "pour_gap_factor", net=pair.positive) or 2.0)
        anchors = [(p.x, p.y) for p in ctx.model.pads if p.net in halves] + \
                  [(v.x, v.y) for v in ctx.model.vias if v.net in halves]

        uncoupled = {h: 0.0 for h in halves}
        where_uncoupled: tuple[float, float] | None = None
        near: dict[tuple[str, str], float] = {}  # (layer, half) -> mm beside another net's pour
        near_nets: dict[str, set[str]] = {}
        where_near: dict[str, tuple[float, float]] = {}

        # First pass: sample both halves on every layer and measure how far each sample is
        # from the partner.
        per_layer = []
        for layer in order:
            a_tracks = tracks_by.get((pair.positive, layer), [])
            b_tracks = tracks_by.get((pair.negative, layer), [])
            if not a_tracks and not b_tracks:
                continue
            samples = {h: _samples(tr) for h, tr in ((pair.positive, a_tracks), (pair.negative, b_tracks))}
            segs = {pair.positive: _segments(a_tracks), pair.negative: _segments(b_tracks)}
            d_partner = {h: _nearest(samples[h][0], segs[other]) for h, other in (halves, halves[::-1])}
            per_layer.append((layer, samples, d_partner))
        if not per_layer:
            continue

        # The pair's pitch (centre to centre): the netclass's when it has one, else the closest
        # the halves routinely run anywhere on the board, which is what they were routed at.
        pitch = (pair.gap_mm + pair.width_mm) if pair.gap_mm and pair.width_mm else 0.0
        if not pitch:
            both = np.concatenate([d for _, _, dp in per_layer for d in dp.values() if len(d)])
            finite = both[np.isfinite(both)]
            if len(finite) == 0:
                continue  # never on the same layer: nothing to compare
            pitch = float(np.percentile(finite, 10))
        widths = np.concatenate([sm[h][1] for _, sm, _ in per_layer for h in halves if len(sm[h][1])])
        width = float(np.median(widths)) if len(widths) else 0.0
        gap = max(pitch - width, 0.05)
        # Loose enough that a length-tuning bump on one half is not a split: those rise a few
        # tenths of a millimetre, and a real DDR4 strobe pair was flagged for its bumps at 2x.
        apart = max(3.0 * pitch, pitch + 0.5)

        for layer, samples, d_partner in per_layer:
            for h in halves:
                pts, _ = samples[h]
                if len(pts) == 0:
                    continue
                routed = _away_from(pts, anchors)
                split = routed & (d_partner[h] > apart)
                uncoupled[h] += float(split.sum()) * STEP_MM
                if split.any() and where_uncoupled is None:
                    where_uncoupled = tuple(pts[int(np.argmax(split))])

                if layer not in edges_by_layer:
                    continue
                pours = pours_by_layer[layer]
                x0, y0 = pts.min(axis=0) - 3 * pitch
                x1, y1 = pts.max(axis=0) + 3 * pitch
                edges, owner = edges_by_layer[layer]
                keep = ~((np.maximum(edges[:, 0], edges[:, 2]) < x0) | (np.minimum(edges[:, 0], edges[:, 2]) > x1)
                         | (np.maximum(edges[:, 1], edges[:, 3]) < y0) | (np.minimum(edges[:, 1], edges[:, 3]) > y1))
                # The pair's own nets never pour, but a board that does must not count them.
                keep &= ~np.array([z.net in halves for z in pours], dtype=bool)[owner]
                edges, owner = edges[keep], owner[keep]
                if len(edges) == 0:
                    continue
                # Edge of the track to the edge of the pour.
                dist, nearest = _nearest_with_index(pts, edges)
                clearance = dist - samples[h][1] / 2
                close = routed & (clearance <= factor * gap)
                if close.any():
                    near[(layer, h)] = near.get((layer, h), 0.0) + float(close.sum()) * STEP_MM
                    where_near.setdefault(layer, tuple(pts[int(np.argmax(close))]))
                    # Name the pours it actually runs beside.
                    for i in set(owner[nearest[close]].tolist()):
                        near_nets.setdefault(layer, set()).add(pours[i].net)

        worst = max(uncoupled.values())
        if max_uncoupled > 0 and worst > max_uncoupled and where_uncoupled is not None:
            x, y = ctx.pt(*where_uncoupled)
            yield Finding(
                rule=RULE,
                severity=severity(ctx, RULE, "warning"),
                title=f"{pair.positive} and {pair.negative} run apart for {worst:.1f} mm",
                detail=(
                    f"Away from their pads and vias, the halves of {pair.base} are more than three times "
                    f"their pitch apart for {worst:.1f} mm (budget {max_uncoupled:g} mm). Over that "
                    f"stretch each is a single-ended line with its own loop, and the difference "
                    f"between them converts to common-mode current, which radiates. Route the two "
                    f"halves side by side all the way."
                ),
                action="Route the two halves side by side all the way.",
                net=pair.positive, x=x, y=y,
            )

        if max_asym <= 0:
            continue
        for layer in order:
            a, b = near.get((layer, pair.positive), 0.0), near.get((layer, pair.negative), 0.0)
            if abs(a - b) <= max_asym:
                continue
            more, less = (pair.positive, pair.negative) if a > b else (pair.negative, pair.positive)
            nets = ", ".join(sorted(near_nets.get(layer, ()))) or "another net's"
            x, y = ctx.pt(*where_near[layer])
            yield Finding(
                rule=RULE,
                severity=severity(ctx, RULE, "warning"),
                title=f"{more} runs beside the {nets} pour for {abs(a - b):.1f} mm more than {less}",
                detail=(
                    f"On {layer}, {more} runs within {factor:g}x the pair's gap of a {nets} pour for "
                    f"{max(a, b):.1f} mm and {less} for {min(a, b):.1f} mm. The closer half couples "
                    f"to the pour more than to its partner, so the halves see different impedances "
                    f"and part of the signal becomes common mode, which radiates. Pull the pour "
                    f"back from the pair, or give both halves the same clearance to it."
                ),
                action="Pull the pour back, or give both halves the same clearance to it.",
                net=more, layer=layer, x=x, y=y,
            )
