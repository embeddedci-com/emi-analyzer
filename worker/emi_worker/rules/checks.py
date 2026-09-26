"""The individual checks.

Each is a function taking a RuleContext and yielding Findings. They are independent by
design: one raising does not stop the others, and adding a check is one function plus one
line in RULES.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Callable, Iterator

import numpy as np

from .. import impedance
from ..kicad.geometry import outer_rings, ring_edges, segment_to_edges
from .model import (
    Finding,
    RuleContext,
    RulesResult,
    classify_net,
    dedupe,
)
from .planes import plane_layers
from .settings import RULE_CATALOGUE

log = logging.getLogger(__name__)

#: A return via this far from a signal via still helps; beyond it, the return current has
#: to detour. 2 mm is the usual layout-review number for digital work.
RETURN_VIA_RADIUS_MM = 2.0

#: A via stub shorter than this is not worth flagging: its quarter-wave resonance sits far
#: above any frequency this tool claims to model.
MIN_STUB_MM = 0.3

#: Copper closer than this to the board edge radiates from the edge rather than coupling
#: back into the plane. The classic guidance is the 20-H rule; 1 mm is a practical floor.
EDGE_KEEPOUT_MM = 1.0

#: Resolution of the reference-plane raster, in mm per pixel. 0.1 mm resolves the gaps that
#: matter (a split plane, a routing channel through a pour) without making the grid huge:
#: a 100 x 90 mm board becomes 1000 x 900, which is nothing for numpy.
PLANE_RASTER_MM = 0.1

#: A trace longer than lambda/20 couples to free space efficiently enough to matter. This
#: is the textbook threshold, and on a real board almost every net clears it -- the BenchPod
#: has 41 -- so it is used for the *count*, not for the findings.
RADIATOR_FRACTION = 1.0 / 20.0

#: What actually gets its own finding. A net at a tenth of a wavelength is worth a look; one
#: at a quarter is an antenna. Reporting forty nets teaches the user to ignore the panel.
RADIATOR_REPORT_FRACTION = 1.0 / 10.0
RADIATOR_CRITICAL_FRACTION = 1.0 / 4.0
RADIATOR_MAX_REPORTED = 12


def copper_z(ctx: RuleContext) -> dict[str, float]:
    """Height of each copper layer's centre above the bottom of the stack, in mm.

    From the stackup's thicknesses. A board file with no stackup falls back to the layer
    order, one unit apart, which still ranks neighbours correctly.
    """
    z: dict[str, float] = {}
    running = 0.0
    for entry in reversed([e for e in ctx.model.stackup if e.thickness_mm > 0 or e.is_copper]):
        if entry.is_copper:
            z[entry.name] = running + entry.thickness_mm / 2
        running += entry.thickness_mm
    names = ctx.model.copper_layer_names
    if not all(n in z for n in names):
        return {n: float(len(names) - 1 - i) for i, n in enumerate(names)}
    return z


def _layer_pairs(ctx: RuleContext) -> dict[str, str]:
    """Map each copper layer to the reference plane beneath it.

    The planes are the ones ingest decided from pour coverage, which the viewer labels and
    the impedance model uses, so the plane checks and the electrical model agree on which
    layer is whose reference. With the stackup analysed, its answer is used directly: it
    already knows which plane is physically nearest and that a signal layer in between
    blocks it. Without one, the nearest plane layer by stackup height stands in.
    """
    if ctx.electrics is not None and ctx.electrics.layers:
        return {
            name: le.reference_plane
            for name, le in ctx.electrics.layers.items()
            if le.reference_plane and le.reference_plane != name
        }

    planes = set(plane_layers(ctx.model))
    if not planes:
        return {}
    z = copper_z(ctx)
    out: dict[str, str] = {}
    for name in ctx.model.copper_layer_names:
        candidates = [p for p in planes if p != name and p in z]
        if name in z and candidates:
            out[name] = min(candidates, key=lambda p: abs(z[p] - z[name]))
    return out


# --------------------------------------------------------------------------------------
# R1: layer transitions without a nearby return via
# --------------------------------------------------------------------------------------

def check_return_vias(ctx: RuleContext) -> Iterator[Finding]:
    """A signal via with no ground via nearby.

    When a signal changes layer, its return current has to change reference plane too. The
    only path for that is a via tying the planes together. Without one within a couple of
    millimetres, the return detours around the nearest stitching point, and the loop that
    detour encloses is what radiates.
    """
    ground = [v for v in ctx.model.vias if classify_net(v.net) == "ground"]
    signals = [v for v in ctx.model.vias if classify_net(v.net) == "signal" and v.net]
    if not signals:
        return
    if not ground:
        yield Finding(
            rule="return-via",
            severity="critical",
            title="No ground vias anywhere on the board",
            detail=(
                f"{len(signals)} signal vias change layer, but there is no ground via to "
                "carry the return current between reference planes. Every layer change is "
                "forcing its return current on a detour."
            ),
        )
        return

    gxy = np.array([[v.x, v.y] for v in ground], dtype=np.float64)
    radius = float(ctx.setting("return-via", "max_distance_mm") or RETURN_VIA_RADIUS_MM)

    for via in signals:
        d = np.hypot(gxy[:, 0] - via.x, gxy[:, 1] - via.y)
        nearest = float(d.min())
        if nearest <= radius:
            continue
        x, y = ctx.pt(via.x, via.y)
        severity = "critical" if nearest > 3 * radius else "warning"
        yield Finding(
            rule="return-via",
            severity=severity,
            title=f"Layer change on {via.net} has no return via within {radius:g} mm",
            detail=(
                f"The nearest ground via is {nearest:.1f} mm away. The return current for "
                f"this layer change has to travel there and back, enclosing a loop roughly "
                f"{2 * nearest:.0f} mm around. Add a ground via beside this one."
            ),
            net=via.net,
            x=x, y=y,
        )


# --------------------------------------------------------------------------------------
# R2: via stubs
# --------------------------------------------------------------------------------------

def check_via_stubs(ctx: RuleContext) -> Iterator[Finding]:
    """A through via whose net only uses part of the stack.

    The unused remainder of the barrel is an open stub. It resonates at the frequency where
    it is a quarter wavelength, and at that frequency it shorts the signal out. Nothing in
    the layout hints at it, which is what makes it worth flagging.
    """
    names = ctx.model.copper_layer_names
    if len(names) < 3:
        return  # a two-layer board has no stub to leave

    # Which layers does each net actually use?
    used: dict[str, set[str]] = defaultdict(set)
    for track in ctx.model.tracks:
        if track.net:
            used[track.net].add(track.layer)
    for zone in ctx.model.zones:
        if zone.net:
            used[zone.net].add(zone.layer)

    # Stack height between adjacent copper layers, for the stub length.
    z = copper_z(ctx)

    for via in ctx.model.vias:
        if via.kind != "through" or not via.net:
            continue
        if classify_net(via.net) != "signal":
            continue
        layers_used = used.get(via.net, set())
        if len(layers_used) < 2:
            continue

        indices = sorted(names.index(n) for n in layers_used if n in names)
        if not indices:
            continue
        # The via spans the whole stack; the net only occupies indices[0]..indices[-1].
        top_stub = indices[0]
        bottom_stub = len(names) - 1 - indices[-1]
        if top_stub == 0 and bottom_stub == 0:
            continue

        stub_mm = 0.0
        if top_stub:
            stub_mm = max(stub_mm, abs(z.get(names[0], 0) - z.get(names[indices[0]], 0)))
        if bottom_stub:
            stub_mm = max(stub_mm, abs(z.get(names[-1], 0) - z.get(names[indices[-1]], 0)))
        if stub_mm < MIN_STUB_MM:
            continue

        # Quarter-wave resonance of the stub.
        f_res = (299_792_458.0 * ctx.velocity_factor) / (4 * stub_mm / 1000.0)
        margin = float(ctx.setting("via-stub", "resonance_margin") or 4.0)
        if f_res > margin * ctx.max_frequency_hz:
            continue

        x, y = ctx.pt(via.x, via.y)
        yield Finding(
            rule="via-stub",
            severity="warning" if f_res > ctx.max_frequency_hz else "critical",
            title=f"{stub_mm:.2f} mm via stub on {via.net}",
            detail=(
                f"This through via carries {via.net} between "
                f"{names[indices[0]]} and {names[indices[-1]]}, leaving {stub_mm:.2f} mm of "
                f"unused barrel. That stub is a quarter wavelength at about "
                f"{f_res / 1e9:.2f} GHz, where it will short the signal. Back-drill it or "
                f"use a blind via."
            ),
            net=via.net,
            x=x, y=y,
        )


# --------------------------------------------------------------------------------------
# R3: traces long enough to radiate
# --------------------------------------------------------------------------------------

def check_radiators(ctx: RuleContext) -> Iterator[Finding]:
    """Nets longer than a twentieth of a wavelength at the assumed top frequency."""
    lam = ctx.wavelength_mm
    threshold = lam * float(ctx.setting("radiator", "wavelength_fraction") or RADIATOR_FRACTION)
    report_at = lam * RADIATOR_REPORT_FRACTION

    length: dict[str, float] = defaultdict(float)
    where: dict[str, tuple[float, float]] = {}
    layers: dict[str, set[str]] = defaultdict(set)
    for track in ctx.model.tracks:
        if not track.net or classify_net(track.net) != "signal":
            continue
        length[track.net] += track.length_mm
        layers[track.net].add(track.layer)
        where.setdefault(track.net, track.pts[0])

    over_threshold = sum(1 for total in length.values() if total >= threshold)
    reported = 0

    for net, total in sorted(length.items(), key=lambda kv: -kv[1]):
        if total < report_at or reported >= RADIATOR_MAX_REPORTED:
            continue
        reported += 1
        x, y = ctx.pt(*where[net])
        ratio = total / lam
        yield Finding(
            rule="radiator",
            severity="warning" if ratio < RADIATOR_CRITICAL_FRACTION else "critical",
            title=f"{net} is {total:.0f} mm long ({ratio:.2f} wavelengths)",
            detail=(
                f"At {ctx.max_frequency_hz / 1e6:.0f} MHz a wavelength in this board is "
                f"about {lam:.0f} mm, so this net is {ratio:.2f} of one. Anything past "
                f"1/20 of a wavelength ({threshold:.0f} mm) couples to free space "
                f"efficiently. If it carries a clock or a switching edge, it is a likely "
                f"source; if it does not, this is not a problem."
            ),
            net=net,
            layer=", ".join(sorted(layers[net])),
            x=x, y=y,
        )

    if over_threshold > reported:
        yield Finding(
            rule="radiator",
            severity="info",
            title=f"{over_threshold} nets exceed 1/20 of a wavelength",
            detail=(
                f"At {ctx.max_frequency_hz / 1e6:.0f} MHz that threshold is "
                f"{threshold:.0f} mm. Only the {reported} longest are listed individually. "
                f"Length alone is not a fault -- it matters for nets that carry fast edges, "
                f"and not at all for ones that do not."
            ),
        )


# --------------------------------------------------------------------------------------
# R4: copper near the board edge
# --------------------------------------------------------------------------------------

def check_edge_proximity(ctx: RuleContext) -> Iterator[Finding]:
    """Signal traces running close to the board outline.

    Copper near the edge radiates from the edge instead of coupling back into its reference
    plane, and the effect is strongest exactly where the trace runs parallel to the cut.
    """
    # The outer outline only. Mounting holes and slots are rings in Edge.Cuts too, but a
    # trace beside a mounting hole is not radiating off the edge of the board.
    edges = ring_edges(outer_rings(ctx.model.outline))
    if edges.size == 0:
        return

    keepout = float(ctx.setting("edge-proximity", "min_clearance_mm") or EDGE_KEEPOUT_MM)
    worst: dict[str, tuple[float, tuple[float, float], str]] = {}
    for track in ctx.model.tracks:
        if not track.net or classify_net(track.net) != "signal":
            continue
        # Every segment, not only its end points: a long straight run hugs the edge in its
        # middle as often as at its ends.
        pts = track.pts if len(track.pts) > 1 else track.pts * 2
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            d, at = segment_to_edges(ax, ay, bx, by, edges)
            d -= track.width_mm / 2.0
            cur = worst.get(track.net)
            if cur is None or d < cur[0]:
                worst[track.net] = (d, at, track.layer)

    for net, (d, pt, layer) in sorted(worst.items(), key=lambda kv: kv[1][0]):
        if d >= keepout:
            continue
        x, y = ctx.pt(*pt)
        yield Finding(
            rule="edge-proximity",
            severity="warning" if d > 0.3 else "critical",
            title=f"{net} runs {max(d, 0):.2f} mm from the board edge",
            detail=(
                f"Copper within {keepout:g} mm of the outline radiates from the "
                f"edge rather than coupling back to its reference plane. Pull this trace "
                f"inward, or add a stitched ground guard between it and the edge."
            ),
            net=net,
            layer=layer,
            x=x, y=y,
        )


# --------------------------------------------------------------------------------------
# R5: traces crossing a gap in their reference plane
# --------------------------------------------------------------------------------------

def _rasterize_layer(ctx: RuleContext, layer: str) -> tuple[np.ndarray, float, float, float] | None:
    """Boolean copper mask for one layer, plus its origin and resolution.

    Rasterised from the layer's zone fills only. A reference plane *is* its pours; the
    tracks on a plane layer are incidental and including them would fill in the very gaps
    this check is looking for.
    """
    zones = [z for z in ctx.model.zones if z.layer == layer]
    if not zones:
        return None

    xs = [p[0] for z in zones for p in z.ring]
    ys = [p[1] for z in zones for p in z.ring]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    w = int((max_x - min_x) / PLANE_RASTER_MM) + 2
    h = int((max_y - min_y) / PLANE_RASTER_MM) + 2
    if w <= 0 or h <= 0 or w * h > 40_000_000:
        return None

    mask = np.zeros((h, w), dtype=bool)

    # Scanline fill, vectorised per row.
    #
    # Even-odd *within* one polygon, OR *across* polygons. Both halves matter. KiCad emits
    # a pour's voids by letting a single outline travel into the void and back out along a
    # degenerate bridge, so even-odd is what carves the holes correctly without relying on
    # ring winding. But separate filled_polygon entries are separate copper islands, not
    # holes -- XOR-ing those together would make two overlapping pours cancel each other
    # and invent a plane gap that is not there.
    for zone in zones:
        ring = zone.ring
        n = len(ring)
        if n < 3:
            continue
        pts = np.asarray(ring, dtype=np.float64)
        x0 = pts[:, 0]
        y0 = pts[:, 1]
        x1 = np.roll(x0, -1)
        y1 = np.roll(y0, -1)

        row_lo = max(0, int((min(y0.min(), y1.min()) - min_y) / PLANE_RASTER_MM))
        row_hi = min(h - 1, int((max(y0.max(), y1.max()) - min_y) / PLANE_RASTER_MM) + 1)

        for row in range(row_lo, row_hi + 1):
            sy = min_y + row * PLANE_RASTER_MM
            hits = ((y0 <= sy) & (y1 > sy)) | ((y1 <= sy) & (y0 > sy))
            if not hits.any():
                continue
            t = (sy - y0[hits]) / (y1[hits] - y0[hits])
            xs_hit = np.sort(x0[hits] + t * (x1[hits] - x0[hits]))
            # Spans between crossing pairs are inside this polygon; OR them in.
            for i in range(0, len(xs_hit) - 1, 2):
                a = int((xs_hit[i] - min_x) / PLANE_RASTER_MM)
                b = int((xs_hit[i + 1] - min_x) / PLANE_RASTER_MM)
                if b >= a:
                    mask[row, max(0, a):min(w, b + 1)] = True

    return mask, min_x, min_y, PLANE_RASTER_MM


def check_plane_gaps(ctx: RuleContext) -> Iterator[Finding]:
    """Signal traces passing over a gap in their reference plane.

    This is the highest-value check in the set. A trace whose reference plane is
    interrupted underneath it forces the return current to detour around the gap, and the
    resulting loop is the most common single cause of a board failing radiated emissions.
    """
    pairs = _layer_pairs(ctx)
    if not pairs:
        return

    rasters: dict[str, tuple[np.ndarray, float, float, float]] = {}
    for plane in set(pairs.values()):
        r = _rasterize_layer(ctx, plane)
        if r is not None:
            rasters[plane] = r

    # A plane with no filled copper in the file (pours never filled, or too large to
    # rasterise) is unknown, not a gap: flagging every trace over it would report the
    # whole layer as missing.
    unknown = sorted(p for p in set(pairs.values()) if p not in rasters)
    if unknown:
        ctx.notes.append(
            "No filled copper on " + ", ".join(unknown)
            + ", so traces over it were not checked for plane gaps. Refill zones in KiCad (B) "
            "and save."
        )
    if not rasters:
        return

    def covered(plane: str, x: float, y: float) -> bool:
        mask, ox, oy, res = rasters[plane]
        col = int((x - ox) / res)
        row = int((y - oy) / res)
        if row < 0 or col < 0 or row >= mask.shape[0] or col >= mask.shape[1]:
            return False
        return bool(mask[row, col])

    # A net is routed as many separate track segments, and a single plane split crosses
    # several of them. Reporting per segment turns one layout problem into a dozen
    # near-identical rows, so the worst crossing per (net, layer, plane) is kept instead.
    worst_by_key: dict[tuple[str, str, str], tuple[float, tuple[float, float]]] = {}

    for track in ctx.model.tracks:
        if not track.net or classify_net(track.net) != "signal":
            continue
        plane = pairs.get(track.layer)
        if plane is None or plane == track.layer or plane not in rasters:
            continue

        # Walk the track, sampling the plane beneath it.
        gap_start: tuple[float, float] | None = None
        gap_len = 0.0
        worst_gap = 0.0
        worst_at: tuple[float, float] | None = None

        for i in range(len(track.pts) - 1):
            (ax, ay), (bx, by) = track.pts[i], track.pts[i + 1]
            seg_len = math.dist((ax, ay), (bx, by))
            steps = max(1, int(seg_len / PLANE_RASTER_MM))
            for s in range(steps + 1):
                t = s / steps
                px, py = ax + (bx - ax) * t, ay + (by - ay) * t
                if covered(plane, px, py):
                    if gap_start is not None and gap_len > worst_gap:
                        worst_gap, worst_at = gap_len, gap_start
                    gap_start, gap_len = None, 0.0
                else:
                    if gap_start is None:
                        gap_start = (px, py)
                    gap_len += seg_len / steps

        if gap_start is not None and gap_len > worst_gap:
            worst_gap, worst_at = gap_len, gap_start

        # A short gap is a via antipad the trace clipped, not a split plane.
        min_cross = float(ctx.setting("plane-gap", "min_crossing_mm") or 0.6)
        if worst_gap < min_cross or worst_at is None:
            continue

        key = (track.net, track.layer, plane)
        prev = worst_by_key.get(key)
        if prev is None or worst_gap > prev[0]:
            worst_by_key[key] = (worst_gap, worst_at)

    # A board with no dedicated plane layer -- a two-layer board, typically -- has no true
    # reference to be missing. The mechanism is still real, but calling it critical there
    # would mean every two-layer board opens with a wall of red.
    dedicated_planes = any(
        layer.kind in ("power", "mixed") for layer in ctx.model.copper_layers
    )

    for (net, layer, plane), (gap, at) in sorted(
        worst_by_key.items(), key=lambda kv: -kv[1][0]
    ):
        x, y = ctx.pt(*at)
        severity = "critical" if (gap > 2.0 and dedicated_planes) else "warning"
        yield Finding(
            rule="plane-gap",
            severity=severity,
            title=f"{net_label(net)} crosses {gap:.1f} mm of missing {plane}",
            detail=(
                f"For {gap:.1f} mm this trace on {layer} has no copper beneath "
                f"it on {plane}, its nearest reference plane. The return current cannot "
                f"follow the signal there and has to detour around the gap; the loop that "
                f"creates is the most common cause of a radiated-emissions failure. Route "
                f"around the gap, or bridge it with a stitching capacitor next to the "
                f"crossing."
            )
            + (
                ""
                if dedicated_planes
                else " This board has no dedicated plane layer, so every signal shares its "
                     "reference with other routing; treat these as places to check rather "
                     "than as defects."
            ),
            net=net,
            layer=layer,
            x=x, y=y,
        )


def net_label(net: str) -> str:
    return net or "(unnamed net)"



def run_rules(ctx: RuleContext, progress: Callable[[str, float], None] | None = None) -> RulesResult:
    """Run every check. One failing check must not lose the others' findings."""
    findings: list[Finding] = []
    total = len(RULES)
    for i, (name, fn) in enumerate(RULES):
        if progress:
            progress(name, 100.0 * i / total)
        # Switched off in settings. Checked here, for every rule, rather than inside each one:
        # the rules that predate settings never looked, so "radiator: false" silently did
        # nothing for them.
        if not ctx.enabled(name):
            continue
        try:
            findings.extend(fn(ctx))
        except Exception:
            log.exception("rule %s failed", name)
            ctx.notes.append(
                f"The {RULE_CATALOGUE.get(name, {}).get('title', name)} check failed to run. "
                "Other checks are unaffected.")

    # Severity overrides, applied the same way to every rule for the same reason.
    if ctx.settings is not None:
        for f in findings:
            f.severity = ctx.settings.severity(f.rule, f.severity)

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.rule))

    return RulesResult(
        findings=dedupe(findings),
        max_frequency_hz=ctx.max_frequency_hz,
        notes=ctx.notes,
    )


# ---------------------------------------------------------------------------------------
# Length matching
# ---------------------------------------------------------------------------------------

def check_length_matching(ctx: RuleContext) -> Iterator[Finding]:
    """Members of a matched group whose delay differs from their reference by too much.

    In picoseconds, not millimetres. On a four-layer board an inner-layer millimetre costs
    about 6.9 ps and an outer-layer one about 5.5 -- so 20 mm moved between layers shifts a
    net by nearly 30 ps while its length does not change at all. For a DDR4-3200 byte lane,
    whose whole budget is around 10 ps, a millimetre-based check would call that matched.
    """
    if not ctx.enabled("ddr-skew") or not ctx.groups or not ctx.electrics:
        return

    yield from _lane_to_lane(ctx)

    for group in ctx.groups:
        tol = float(ctx.setting("ddr-skew", group.tolerance_key, net=group.reference) or 0.0)
        if tol <= 0:
            # A tolerance of zero means the comparison is switched off -- lane-to-lane is
            # like this by default, because on a correct board it is a wall of noise.
            continue

        ref_delay = _group_delay(ctx, group.reference)
        if ref_delay is None:
            ctx.notes.append(
                f"{group.name}: no routed path for {group.reference}, so the group was not "
                "compared against it."
            )
            continue

        for net in group.members:
            if net == group.reference:
                continue
            delay = _group_delay(ctx, net)
            if delay is None:
                continue
            skew = delay - ref_delay
            if abs(skew) <= tol:
                continue

            topo = ctx.topology.get(net)
            path = topo.longest_path() if topo else None
            mm = (skew / ctx.electrics.ps_per_mm(path.runs[0].layer)) if (path and path.runs) else 0.0
            where = _net_midpoint(ctx, net)
            longer = "longer" if skew > 0 else "shorter"

            yield Finding(
                rule="ddr-skew",
                severity=ctx.settings.severity("ddr-skew", "warning") if ctx.settings else "warning",
                title=f"{net} is {abs(skew):.0f} ps {longer} than {group.reference}",
                detail=(
                    f"In {group.name}, {net} arrives {abs(skew):.0f} ps {longer} than its "
                    f"reference {group.reference} — about {abs(mm):.1f} mm of trace. The "
                    f"budget is ±{tol:.0f} ps ({_describe_tolerance(ctx, group)}). "
                    f"{_path_summary(ctx, net)} "
                    f"Group identified by {group.source}."
                    + (f" {group.note}" if group.note else "")
                ),
                net=net,
                x=where[0] if where else None,
                y=where[1] if where else None,
            )


def _lane_to_lane(ctx: RuleContext) -> Iterator[Finding]:
    """Byte lanes against each other, compared by their strobes. Off unless lane_to_lane_ps > 0.

    A controller with write levelling absorbs the difference between lanes, so by default this
    compares nothing: on a correct DDR3/DDR4 board it would be a wall of findings. It is for
    parts without levelling (DDR2, some LPDDR and FPGA soft controllers). The parameter was in
    the catalogue and the docs for a release before anything read it, so setting it silently
    did nothing.

    Each lane is compared with the median lane rather than the fastest, so one outlier is
    reported as the outlier instead of making every other lane look late.
    """
    tol = float(ctx.setting("ddr-skew", "lane_to_lane_ps") or 0.0)
    if tol <= 0:
        return
    lanes = [(g, _group_delay(ctx, g.reference)) for g in ctx.groups if g.kind == "byte-lane"]
    lanes = [(g, d) for g, d in lanes if d is not None]
    if len(lanes) < 2:
        return
    delays = sorted(d for _, d in lanes)
    mid = len(delays) // 2
    median = delays[mid] if len(delays) % 2 else (delays[mid - 1] + delays[mid]) / 2
    for group, delay in lanes:
        skew = delay - median
        if abs(skew) <= tol:
            continue
        where = _net_midpoint(ctx, group.reference)
        later = "later" if skew > 0 else "earlier"
        yield Finding(
            rule="ddr-skew",
            severity=ctx.settings.severity("ddr-skew", "warning") if ctx.settings else "warning",
            title=f"{group.name} arrives {abs(skew):.0f} ps {later} than the other lanes",
            detail=(
                f"Its strobe {group.reference} is {abs(skew):.0f} ps {later} than the median of "
                f"{len(lanes)} byte lanes. The lane-to-lane budget is ±{tol:.0f} ps. Only set "
                "this budget for a memory controller without write levelling; one with it "
                "absorbs the difference."
            ),
            net=group.reference,
            x=where[0] if where else None,
            y=where[1] if where else None,
        )


def _describe_tolerance(ctx: RuleContext, group) -> str:
    """Where a group's budget came from, naming the net group when one set it."""
    if not ctx.settings:
        return ""
    netclass = ctx.netclasses.of(group.reference) if ctx.netclasses else ""
    return ctx.settings.describe("ddr-skew", group.tolerance_key, "ps",
                                 net=group.reference, netclass=netclass)


def _group_delay(ctx: RuleContext, net: str) -> float | None:
    """The delay of a net's longest driver-to-receiver path.

    Longest rather than average, because on a fly-by net the receiver that arrives last is
    the one that fails. Which pad drives is not in the board file; until settings can say,
    the longest path is the honest worst case and the finding says what it measured.
    """
    topo = ctx.topology.get(net)
    if not topo:
        return None
    path = topo.longest_path()
    if path is None:
        return None
    return path.delay_ps(ctx.electrics)


def _path_summary(ctx: RuleContext, net: str) -> str:
    topo = ctx.topology.get(net)
    path = topo.longest_path() if topo else None
    if not path:
        return ""
    per_layer = ", ".join(f"{r.length_mm:.1f} mm on {r.layer}" for r in path.runs if r.length_mm > 0.05)
    vias = f", {path.vias} via{'s' if path.vias != 1 else ''}" if path.vias else ""
    return f"Routed {path.from_pad}→{path.to_pad}: {per_layer}{vias}."


def _net_midpoint(ctx: RuleContext, net: str) -> tuple[float, float] | None:
    for track in ctx.model.tracks:
        if track.net == net and track.pts:
            mid = track.pts[len(track.pts) // 2]
            return ctx.pt(*mid)
    return None


# ---------------------------------------------------------------------------------------
# Impedance
# ---------------------------------------------------------------------------------------

def check_impedance(ctx: RuleContext) -> Iterator[Finding]:
    """Computed impedance against a target, and discontinuities along a net.

    The discontinuity half needs no target and is often the more actionable of the two: a
    trace that changes width, or changes to a layer with a different dielectric height,
    changes impedance mid-flight whether or not anybody set a target.
    """
    if not ctx.enabled("impedance") or not ctx.electrics:
        return

    tol = float(ctx.setting("impedance", "tolerance_pct") or 10.0)
    disc_pct = float(ctx.setting("impedance", "discontinuity_pct") or 20.0)

    by_net: dict[str, list] = defaultdict(list)
    for track in ctx.model.tracks:
        if track.net and track.width_mm > 0:
            by_net[track.net].append(track)

    pair_of = {}
    for p in ctx.pairs:
        pair_of[p.positive] = p
        pair_of[p.negative] = p

    for net, tracks in sorted(by_net.items()):
        if classify_net(net) != "signal":
            continue

        target = float(ctx.setting("impedance", "single_ended_ohm", net=net) or 0.0)
        pair = pair_of.get(net)
        if pair:
            # A pair is one signal, so it gets one finding. Reported against the positive
            # half; the negative one is the same trace geometry and the same problem.
            if net == pair.negative:
                continue
            target = float(ctx.setting("impedance", "differential_ohm", net=net) or 0.0)

        # Impedance per distinct (layer, width) the net uses. Two runs of the same geometry
        # are one number; that is what makes a discontinuity visible.
        seen: dict[tuple[str, float], object] = {}
        for track in tracks:
            layer = ctx.electrics.layer(track.layer)
            if layer is None or not layer.reference_plane:
                continue
            key = (track.layer, round(track.width_mm, 3))
            if key in seen:
                continue
            if pair and pair.gap_mm > 0:
                z = impedance.differential(pair.width_mm or track.width_mm, pair.gap_mm, layer)
            else:
                z = impedance.single_ended(track.width_mm, layer)
            if z is not None:
                seen[key] = z

        if not seen:
            continue
        values = list(seen.values())

        # 1. Against the target, if there is one. Only flagged when the trace is outside
        #    tolerance *including* the model's own uncertainty -- otherwise the tool would
        #    be reporting its error as the board's.
        if target > 0:
            # One finding per net, not per geometry. A net routed at three widths is one
            # problem to fix, and three rows saying so is a list nobody reads -- the same
            # reason the plane-gap check reports per (net, layer, plane).
            off = {k: z for k, z in sorted(seen.items()) if not z.within(target, tol)}
            if off:
                worst = max(off.values(), key=lambda z: abs(z.ohm - target))
                geoms = "; ".join(
                    f"{w:.3f} mm on {layer} → {z.describe()}" for (layer, w), z in off.items()
                )
                notes = sorted({n for z in off.values() for n in z.notes})
                yield Finding(
                    rule="impedance",
                    severity=ctx.settings.severity("impedance", "warning") if ctx.settings else "warning",
                    title=(
                        f"{net} is {worst.ohm:.0f} Ω, target {target:.0f} Ω"
                        if len(off) == 1 else
                        f"{net} misses its {target:.0f} Ω target on {len(off)} geometries"
                    ),
                    detail=(
                        f"{'The pair' if worst.differential else 'The trace'} computes to "
                        f"{geoms}, against {target:.0f} Ω ±{tol:.0f}%. "
                        + (" ".join(notes) + " " if notes else "")
                        + "Closed-form model, so the range is what is claimed rather than the "
                        "midpoint; a 2D field solver would narrow it."
                    ),
                    net=net,
                    layer=", ".join(sorted({layer for layer, _ in off})),
                )

        # 2. Discontinuities, which need no target at all.
        if len(values) > 1:
            lo = min(v.ohm for v in values)
            hi = max(v.ohm for v in values)
            if lo > 0 and (hi - lo) / lo * 100 >= disc_pct:
                where = ", ".join(
                    f"{v.ohm:.0f} Ω on {k[0]} at {k[1]:.3f} mm" for k, v in sorted(seen.items())
                )
                yield Finding(
                    rule="impedance",
                    severity="info",
                    title=f"{net} changes impedance along its length ({lo:.0f}–{hi:.0f} Ω)",
                    detail=(
                        f"This net is routed with more than one geometry: {where}. Each change "
                        f"reflects part of the signal back toward the driver. Widths and layer "
                        f"changes are the usual cause; a change of reference plane does it too, "
                        f"and also breaks the return path."
                    ),
                    net=net,
                )


from .cable_resonance import check_cable_resonance  # noqa: E402
from .decoupling import check_decoupling  # noqa: E402
from .placement import check_copper_islands, check_crystals, check_stackup  # noqa: E402
from .stitching import check_edge_stitching, check_stitching  # noqa: E402
from .emc import (  # noqa: E402
    check_connector_shield, check_esd_protection, check_input_filter, check_reset_filter,
    check_switch_node,
)

RULES: list[tuple[str, Callable[[RuleContext], Iterator[Finding]]]] = [
    ("plane-gap", check_plane_gaps),
    ("return-via", check_return_vias),
    ("stitching", check_stitching),
    ("edge-stitching", check_edge_stitching),
    ("stackup", check_stackup),
    ("decoupling", check_decoupling),
    ("cable-resonance", check_cable_resonance),
    ("via-stub", check_via_stubs),
    ("ddr-skew", check_length_matching),
    ("impedance", check_impedance),
    ("radiator", check_radiators),
    ("edge-proximity", check_edge_proximity),
    ("copper-island", check_copper_islands),
    ("crystal", check_crystals),
    # EMC: immunity and conducted emissions.
    ("esd-protection", check_esd_protection),
    ("connector-shield", check_connector_shield),
    ("reset-filter", check_reset_filter),
    ("input-filter", check_input_filter),
    ("switch-node", check_switch_node),
]
