"""EMC checks: where a board lets disturbances in, and what it conducts out through its cables.

The rest of the rules tier is about emissions -- where a board radiates. An EMC test house also
does the opposite: it discharges into every connector and shell (IEC 61000-4-2), bursts fast
transients down the cables (61000-4-4), and measures what the power input conducts back onto
the supply. Boards fail those for layout reasons that are just as visible in the geometry:

  * An I/O line with no clamp at the connector hands the whole discharge to the first IC pin it
    reaches. A clamp placed after the IC, or far from the connector, protects much less than
    the schematic suggests: an ESD pulse rises in under a nanosecond, where every millimetre of
    trace in front of the clamp is roughly a nanohenry and tens of volts.
  * A connector shell or chassis net that is not tied to the board's ground has nowhere to put a
    discharge except through the circuit.
  * A reset input held only by a pull-up resets on the first transient that couples onto it.
  * Power crossing a connector with no capacitor beside it carries the loads' switching current
    along the cable, and a sprawling switch node is the strongest electric-field antenna on a
    board with a regulator.

Parts are recognised by reference designator, value and footprint, the way the decoupling check
does it, and every finding names the parts it matched so a wrong guess is visible. The
heuristics were tuned against real boards; each place one of them was wrong there is called out
where the fix lives. These are layout checks, not an immunity test: they cannot say what level
a board survives, only where the common failures are.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Iterator

from ..kicad.geometry import point_segment_distance, ring_area
from .decoupling import CAP_RE, IC_RE
from .model import Finding, RuleContext, classify_net
from .planes import severity

RES_RE = re.compile(r"^R\d", re.I)
#: Two-pad series parts a filter is built from: inductors and ferrite beads.
SERIES_L_RE = re.compile(r"^(L|FB)\d", re.I)
IND_RE = re.compile(r"^L\d", re.I)

CONN_REF = re.compile(r"^(J|P|CN|CON|USB|FPC|FFC)\d", re.I)
#: Headers are often numbered H1, H2 -- the same prefix as mounting holes -- so the footprint
#: decides for any reference that is not a connector prefix.
CONN_HINT = re.compile(
    r"conn|usb|rj45|hdr|header|terminal|fpc|ffc|jst|molex|socket|type-?c|d-?sub|barrel|jack",
    re.I,
)
NOT_CONN_REF = re.compile(r"^(C|R|L|D|U|IC|Q|Y|X|FB|F|TP|SW|K|LED|BT|RN|TVS|ESD|MH)\d", re.I)
HOLE_HINT = re.compile(r"mounting_?hole|standoff", re.I)

#: Protection parts by part number or footprint, on any reference: SRV05 arrays are often U,
#: and common-mode filters with integrated ESD (PCMF) are L.
PROT_HINT = re.compile(
    r"tvs|esd|tpd\d|usblc|pcmf|smaj|smbj|smcj|smf\d|sm712|prtr|rclamp|sp0\d|ip42|cdsot|nup\d|"
    r"srv05|lesd|pgb1|varistor",
    re.I,
)
#: A diode from a line to ground is a clamp even when its part number says nothing.
DIODE_REF = re.compile(r"^(D|TVS|ESD|Z|ZD|RV|VR)\d", re.I)
LED_HINT = re.compile(r"led|green|red\b|blue|yellow|white|amber|orange", re.I)

CHASSIS = re.compile(r"chassis|chs|shield|shld|earth|frame|fgnd|(^|[/_])pe$", re.I)
#: The lookbehind keeps "FIRST" and "BURST" out while letting "/NRST" and "U1-RSTn" in.
RESET_NET = re.compile(r"(?<![a-z]{2})rst|reset|mclr", re.I)
#: Supply names that suggest power crossing a connector rather than a rail passing through.
#: The generic classifier calls "+24V" and "/BAT" signals, so this is deliberately its own list.
POWER_ENTRY = re.compile(
    r"vbus|vin|vbat|(^|[/_])bat|vsup|supply|pwr|dc_?in|(^|[/_+])\d+v\d*$|^\+\d+v", re.I
)
VOLTS = re.compile(r"(\d+)v(\d*)", re.I)
SW_NET = re.compile(r"(^|[/_.(-])(sw\d*|lx\d*|vlx\d*|ph\d*|phase\d*)(\)|$)", re.I)
FERRITE_HINT = re.compile(r"ferrite|bead|Ω|ohm|@\s*\d+\s*mhz|pcmf|choke", re.I)

#: Peak current of an 8 kV contact discharge (IEC 61000-4-2 level 4) over its sub-nanosecond
#: rise, as volts per nanohenry. Order of magnitude only, to say why a millimetre matters.
ESD_V_PER_NH = 35.0


# ---------------------------------------------------------------------------------------
# Shared part index
# ---------------------------------------------------------------------------------------

def _usable(net: str) -> bool:
    return bool(net) and not net.lower().startswith("unconnected") and not CHASSIS.search(net)


def _kind(net: str) -> str:
    return classify_net(net) if net else ""


def _num(ctx: RuleContext, rule: str, key: str, default: float) -> float:
    # Not `or default`: zero is a meaningful setting here ("every connector").
    v = ctx.setting(rule, key)
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _power_entry(net: str) -> bool:
    """Power that plausibly crosses a connector.

    A rail named below 3 V is not: a +1V8 pin on a header on a real board is a logic-level
    reference going out, and treating it as a supply input produced two findings about nothing.
    """
    if not POWER_ENTRY.search(net):
        return False
    m = VOLTS.search(net)
    return m is None or float(f"{m.group(1)}.{m.group(2) or 0}") >= 3.0


def _plated(p) -> bool:
    """A plated hole with copper around it, as opposed to a locating peg drawn as a pad.

    Converted USB-C and RJ45 footprints mark their locating pegs as plated with a "pad" exactly
    the size of the drill -- no annular ring, so no copper and nothing to connect. On real boards
    every one of those came back as a floating shell.
    """
    if p.pad_type != "thru_hole":
        return False
    if not p.ring or p.drill_mm <= 0:
        return True
    xs = [q[0] for q in p.ring]
    ys = [q[1] for q in p.ring]
    return min(max(xs) - min(xs), max(ys) - min(ys)) > p.drill_mm + 0.1


def _pad_gap(p, point: tuple[float, float]) -> float:
    """Distance from a point to the edge of a pad, not its centre.

    Measured to the centre, a via at the edge of an SMB TVS's ground pad reads as 2 mm away, and
    every large clamp on a real board was flagged for a via that was right beside it.
    """
    if not p.ring:
        return math.dist((p.x, p.y), point)
    xs = [q[0] for q in p.ring]
    ys = [q[1] for q in p.ring]
    px, py = point
    return math.hypot(max(min(xs) - px, 0.0, px - max(xs)), max(min(ys) - py, 0.0, py - max(ys)))


def _is_connector(ref: str, pads: list) -> bool:
    hint = f"{pads[0].footprint} {pads[0].value}"
    if HOLE_HINT.search(hint):
        return False
    if CONN_REF.match(ref):
        return True
    return bool(CONN_HINT.search(hint)) and not NOT_CONN_REF.match(ref)


def _is_clamp(ref: str, pads: list, nets: set[str]) -> bool:
    hint = f"{pads[0].value} {pads[0].footprint}"
    if CONN_REF.match(ref) or CAP_RE.match(ref) or RES_RE.match(ref):
        return False
    if PROT_HINT.search(hint):
        return True
    if not DIODE_REF.match(ref) or LED_HINT.search(hint) or len(pads) < 2:
        return False
    kinds = {_kind(n) for n in nets}
    return "ground" in kinds and any(_usable(n) and _kind(n) != "ground" for n in nets)


class _Parts:
    """Every lookup the EMC checks share, built once per board."""

    def __init__(self, model) -> None:
        self.model = model
        self.by_ref: dict[str, list] = defaultdict(list)
        self.by_net: dict[str, list] = defaultdict(list)
        for p in model.pads:
            if p.ref:
                self.by_ref[p.ref].append(p)
            if p.net:
                self.by_net[p.net].append(p)
        self.pad_count = Counter(p.net for p in model.pads if p.net)
        self.edges = [
            (*ring[i], *ring[i + 1]) for ring in model.outline for i in range(len(ring) - 1)
        ]

        self.connectors: dict[str, list] = {}
        self.holes: dict[str, list] = {}
        #: net -> [(clamp ref, its pad on the net, its ground pad or None)]
        self.clamps: dict[str, list] = defaultdict(list)
        #: net -> [(capacitor ref, its pad on the net)] for capacitors to ground
        self.ground_caps: dict[str, list] = defaultdict(list)
        #: net -> [(ref, pad on the net, net on the other side)] for two-pad series parts
        self.series_r: dict[str, list] = defaultdict(list)
        self.series_l: dict[str, list] = defaultdict(list)

        for ref, pads in self.by_ref.items():
            nets = {p.net for p in pads if p.net}
            if HOLE_HINT.search(pads[0].footprint) or re.match(r"^MH\d", ref, re.I):
                self.holes[ref] = pads
            elif _is_connector(ref, pads):
                self.connectors[ref] = pads

            if _is_clamp(ref, pads, nets):
                gnd = next((p for p in pads if p.net and _kind(p.net) == "ground"), None)
                lines = [p for p in pads if _usable(p.net) and _kind(p.net) != "ground"]
                # A rail-to-rail diode (BAT54S) or an array's VBUS pin sits on the supply to
                # clamp *signals* against it, and does nothing for a surge on the supply itself.
                # Counting it there reported a +5V header pin as protected by a BAT54S 17 mm
                # away. Only a part with nothing but supply and ground on it clamps the supply.
                signals = [p for p in lines if _kind(p.net) == "signal"]
                for p in signals or lines:
                    self.clamps[p.net].append((ref, p, gnd))

            if len(pads) != 2 or not all(p.net for p in pads) or pads[0].net == pads[1].net:
                continue
            a, b = pads
            if CAP_RE.match(ref):
                kinds = [_kind(a.net), _kind(b.net)]
                if kinds.count("ground") == 1:
                    line = a if kinds[1] == "ground" else b
                    self.ground_caps[line.net].append((ref, line))
            for table, pattern in ((self.series_r, RES_RE), (self.series_l, SERIES_L_RE)):
                if pattern.match(ref):
                    table[a.net].append((ref, a, b.net))
                    table[b.net].append((ref, b, a.net))

        self._stitches: list[tuple[float, float]] | None = None

    def external(self, pads: list, edge_mm: float) -> bool:
        """A connector a cable plugs into, judged by how close it sits to the board edge."""
        if edge_mm <= 0 or not self.edges:
            return True
        return min(
            point_segment_distance(p.x, p.y, *e) for p in pads for e in self.edges
        ) <= edge_mm

    def lines_leaving(self, pads: list) -> dict[str, object]:
        """The nets a connector carries off the board, each with one of its pads."""
        out: dict[str, object] = {}
        for p in pads:
            if not _usable(p.net) or _kind(p.net) == "ground" or p.net in out:
                continue
            if self.pad_count[p.net] <= sum(1 for q in pads if q.net == p.net):
                continue  # only the connector itself is on this net: nothing to protect
            out[p.net] = p
        return out

    def behind(self, table: dict, net: str) -> list[tuple[str, str]]:
        """(series part, net beyond it) one hop away, stopping at supply rails.

        A series resistor into a clamp or a capacitor is a filter; a pull-up to +3V3 is not,
        even though the rail has capacitors on it.
        """
        return [(ref, other) for ref, _, other in table.get(net, []) if _kind(other) == "signal"]

    def ground_stitches(self) -> list[tuple[float, float]]:
        if self._stitches is None:
            self._stitches = [
                (v.x, v.y) for v in self.model.vias if v.net and _kind(v.net) == "ground"
            ] + [
                (p.x, p.y) for p in self.model.pads
                if p.net and _kind(p.net) == "ground" and p.is_through
            ]
        return self._stitches


def _parts(ctx: RuleContext) -> _Parts:
    cached = getattr(ctx, "_emc_parts", None)
    if cached is None or cached.model is not ctx.model:
        cached = _Parts(ctx.model)
        setattr(ctx, "_emc_parts", cached)
    return cached


def _route(ctx: RuleContext, net: str, a, b) -> tuple[float, bool]:
    """Distance from pad to pad along the routing when it is traced, else in a straight line."""
    topo = ctx.topology.get(net) if ctx.topology else None
    if topo is not None:
        path = topo.path(f"{a.ref}.{a.number}", f"{b.ref}.{b.number}")
        if path is not None and path.length_mm > 0:
            return path.length_mm, True
    return math.dist((a.x, a.y), (b.x, b.y)), False


def _others(parts: _Parts, net: str, exclude: str) -> str:
    refs = sorted({p.ref for p in parts.by_net[net] if p.ref and p.ref != exclude})
    return ", ".join(refs[:4]) + (f" and {len(refs) - 4} more" if len(refs) > 4 else "")


# ---------------------------------------------------------------------------------------
# ESD protection
# ---------------------------------------------------------------------------------------

def check_esd_protection(ctx: RuleContext) -> Iterator[Finding]:
    rule = "esd-protection"
    parts = _parts(ctx)
    edge_mm = _num(ctx, rule, "edge_mm", 5.0)
    max_d = _num(ctx, rule, "max_distance_mm", 15.0)
    via_d = _num(ctx, rule, "max_ground_via_mm", 2.0)
    where = f"within {edge_mm:g} mm of the board edge" if edge_mm > 0 else "a connector"

    used: dict[str, object] = {}
    far: dict[tuple[str, str], tuple[float, list[str], object, bool]] = {}

    for ref, pads in sorted(parts.connectors.items()):
        if not parts.external(pads, edge_mm):
            continue
        for net, cpad in sorted(parts.lines_leaving(pads).items()):
            if _kind(net) == "power" and not _power_entry(net):
                continue  # a rail passing through, such as a transformer centre tap
            clamps = parts.clamps.get(net, [])
            if not clamps:
                if any(parts.clamps.get(other) for _, other in parts.behind(parts.series_r, net)):
                    continue  # series resistor into a clamp: a deliberate R-clamp
                x, y = ctx.pt(cpad.x, cpad.y)
                yield Finding(
                    rule=rule,
                    severity=severity(ctx, rule, "warning"),
                    title=f"{net} leaves the board at {ref} with no ESD protection",
                    detail=(
                        f"{net} runs from {ref} ({where}) to {_others(parts, net, ref)} with no "
                        f"clamp on it. A cable plugged into {ref} is where a discharge arrives, and "
                        f"with no clamp at the connector the first IC pin it reaches absorbs it. "
                        f"Place a TVS diode or ESD array on the line right at the connector, with "
                        f"its ground pad straight into the plane. If this connector never leaves "
                        f"the enclosure, suppress the finding for its nets."
                    ),
                    net=net, x=x, y=y,
                )
                continue

            (d, routed), cref, clamp_pad, gnd = min(
                ((_route(ctx, net, cpad, cp), cr, cp, g) for cr, cp, g in clamps),
                key=lambda t: t[0][0],
            )
            if gnd is not None:
                used.setdefault(cref, gnd)
            measured = "along the routing" if routed else "in a straight line"

            ics = [q for q in parts.by_net[net] if IC_RE.match(q.ref) and q.ref != cref]
            if ics:
                (d_ic, _), ic = min(((_route(ctx, net, cpad, q), q) for q in ics),
                                    key=lambda t: t[0][0])
                if d_ic + 1.0 < d:
                    x, y = ctx.pt(clamp_pad.x, clamp_pad.y)
                    yield Finding(
                        rule=rule,
                        severity=severity(ctx, rule, "warning"),
                        title=f"{net} reaches {ic.ref} before its ESD clamp {cref}",
                        detail=(
                            f"From {ref}, {ic.ref}.{ic.number} is {d_ic:.1f} mm away and the clamp "
                            f"{cref} is {d:.1f} mm, {measured}. A discharge travels the trace in "
                            f"picoseconds and goes through {ic.ref} before the clamp can take the "
                            f"current. Route the line from the connector to {cref} first, then on "
                            f"to {ic.ref}."
                        ),
                        net=net, x=x, y=y,
                    )
                    continue

            if d > max_d:
                prev = far.get((ref, cref))
                far[(ref, cref)] = (
                    max(d, prev[0]) if prev else d,
                    (prev[1] if prev else []) + [net],
                    clamp_pad,
                    routed,
                )

    for (ref, cref), (d, nets, clamp_pad, routed) in sorted(far.items()):
        x, y = ctx.pt(clamp_pad.x, clamp_pad.y)
        yield Finding(
            rule=rule,
            severity=severity(ctx, rule, "warning"),
            title=f"ESD clamp {cref} is {d:.1f} mm from {ref}",
            detail=(
                f"{cref} protects {', '.join(nets)}, but sits {d:.1f} mm from {ref} "
                f"{'along the routing' if routed else 'in a straight line'} (budget {max_d:g} mm). "
                f"A discharge rises in under a nanosecond, where each millimetre of trace is roughly "
                f"a nanohenry: that is on the order of {ESD_V_PER_NH * d:.0f} V across the trace "
                f"before the clamp takes the current. Move {cref} against the connector."
            ),
            net=nets[0], x=x, y=y,
        )

    stitches = parts.ground_stitches()
    if not stitches:
        return
    for cref, gnd in sorted(used.items()):
        if gnd.is_through:
            continue
        near = min(_pad_gap(gnd, s) for s in stitches)
        if near <= via_d:
            continue
        x, y = ctx.pt(gnd.x, gnd.y)
        yield Finding(
            rule=rule,
            severity=severity(ctx, rule, "warning"),
            title=f"ESD clamp {cref}'s ground pad has no via within {via_d:g} mm",
            detail=(
                f"{cref} clamps a line at a connector, but the nearest ground via is {near:.1f} mm "
                f"from the edge of its ground pad. The clamp can only divert the discharge as well "
                f"as its path to the plane allows, and the inductance of that trace adds straight "
                f"onto the voltage the protected line sees. Put a via at the pad."
            ),
            net=gnd.net, x=x, y=y,
        )


# ---------------------------------------------------------------------------------------
# Shields, chassis and mounting holes
# ---------------------------------------------------------------------------------------

def check_connector_shield(ctx: RuleContext) -> Iterator[Finding]:
    rule = "connector-shield"
    parts = _parts(ctx)

    for ref, pads in sorted(parts.connectors.items()):
        floating = [p for p in pads if not p.net and _plated(p)]
        if not floating:
            continue
        x, y = ctx.pt(floating[0].x, floating[0].y)
        yield Finding(
            rule=rule,
            severity=severity(ctx, rule, "warning"),
            title=f"{ref} has {len(floating)} plated shell pad{'s' if len(floating) > 1 else ''} "
                  f"connected to nothing",
            detail=(
                f"Plated pads on {ref} with no net are almost always its shell or mounting tabs. The "
                f"shell is where a discharge to the cable lands; floating, it couples the discharge "
                f"capacitively into the lines inside instead of draining it. Connect the shell to "
                f"ground, or to a chassis net tied to ground through a capacitor."
            ),
            x=x, y=y,
        )

    for net in sorted(parts.by_net):
        if not net or net.lower().startswith("unconnected") or not CHASSIS.search(net):
            continue
        bridged = any(
            any(q.net and q.net != net and _kind(q.net) == "ground" and not CHASSIS.search(q.net)
                for q in parts.by_ref[p.ref])
            for p in parts.by_net[net]
            if p.ref and p.ref not in parts.connectors
        )
        if bridged:
            continue
        p = parts.by_net[net][0]
        x, y = ctx.pt(p.x, p.y)
        yield Finding(
            rule=rule,
            severity=severity(ctx, rule, "warning"),
            title=f"Chassis net {net} has no connection to ground",
            detail=(
                f"No part joins {net} to the board's ground. Current from a discharge to the "
                f"enclosure or a cable shield then has no deliberate path and returns through the "
                f"circuit instead. Tie it to ground at the connector, directly or through a "
                f"capacitor (and resistor, for an isolated interface such as Ethernet)."
            ),
            net=net, x=x, y=y,
        )

    for ref, pads in sorted(parts.holes.items()):
        if any(p.net for p in pads) or not any(_plated(p) for p in pads):
            continue
        x, y = ctx.pt(pads[0].x, pads[0].y)
        yield Finding(
            rule=rule,
            severity=severity(ctx, rule, "info"),
            title=f"Plated mounting hole {ref} is on no net",
            detail=(
                f"A metal screw or standoff in {ref} joins the enclosure to a ring of copper that goes "
                f"nowhere, so a discharge to the enclosure couples into nearby traces rather than into "
                f"ground. Put the hole on ground, or make it unplated if it must stay isolated."
            ),
            x=x, y=y,
        )


# ---------------------------------------------------------------------------------------
# Reset lines
# ---------------------------------------------------------------------------------------

def check_reset_filter(ctx: RuleContext) -> Iterator[Finding]:
    rule = "reset-filter"
    parts = _parts(ctx)
    max_d = _num(ctx, rule, "max_distance_mm", 10.0)

    for net in sorted(parts.by_net):
        if not _usable(net) or _kind(net) != "signal" or not RESET_NET.search(net):
            continue
        ics = sorted({p.ref for p in parts.by_net[net] if IC_RE.match(p.ref)})
        # Two ICs means one drives the other: an actively driven line does not need a
        # capacitor, and one would slow its edge.
        if len(ics) != 1 or parts.clamps.get(net):
            continue
        # The same through a series resistor. On a real board a lone-looking SoC reset pin ran
        # through a resistor to the PMIC: an output fanning out, not an input left floating.
        if any(
            any(IC_RE.match(q.ref) and q.ref != ics[0] for q in parts.by_net[other])
            for _, other in parts.behind(parts.series_r, net)
        ):
            continue
        pin = next(p for p in parts.by_net[net] if p.ref == ics[0])
        x, y = ctx.pt(pin.x, pin.y)
        caps = parts.ground_caps.get(net, [])

        if not caps:
            if any(parts.ground_caps.get(other) for _, other in parts.behind(parts.series_r, net)):
                continue  # an RC through a series resistor
            yield Finding(
                rule=rule,
                severity=severity(ctx, rule, "warning"),
                title=f"Reset input {ics[0]}.{pin.number} ({net}) has no filter capacitor",
                detail=(
                    f"{net} reaches only one IC, {ics[0]}, so nothing drives it: it is held by "
                    f"{_others(parts, net, ics[0]) or 'nothing else'}. A fast transient coupling onto "
                    f"the trace — a burst on a cable, a discharge nearby — resets the board. Put a "
                    f"capacitor to ground at the pin, typically 100 nF, or check the IC's datasheet "
                    f"for its recommended reset filter."
                ),
                net=net, x=x, y=y,
            )
            continue

        cref, cpad = min(caps, key=lambda c: math.dist((pin.x, pin.y), (c[1].x, c[1].y)))
        d = math.dist((pin.x, pin.y), (cpad.x, cpad.y))
        if d <= max_d:
            continue
        yield Finding(
            rule=rule,
            severity=severity(ctx, rule, "warning"),
            title=f"Reset filter {cref} is {d:.1f} mm from {ics[0]}.{pin.number}",
            detail=(
                f"{cref} filters {net}, but the {d:.1f} mm of trace between it and the pin (budget "
                f"{max_d:g} mm) is unfiltered and picks up whatever couples onto it. Move the "
                f"capacitor to the pin."
            ),
            net=net, x=x, y=y,
        )


# ---------------------------------------------------------------------------------------
# Power input filtering
# ---------------------------------------------------------------------------------------

def check_input_filter(ctx: RuleContext) -> Iterator[Finding]:
    rule = "input-filter"
    parts = _parts(ctx)
    edge_mm = _num(ctx, rule, "edge_mm", 5.0)
    max_d = _num(ctx, rule, "max_distance_mm", 15.0)

    for ref, pads in sorted(parts.connectors.items()):
        if not parts.external(pads, edge_mm):
            continue
        for net, cpad in sorted(parts.lines_leaving(pads).items()):
            if not _power_entry(net):
                continue
            entry = [q for q in pads if q.net == net]
            # Capacitors on the supply itself, and behind one ferrite or inductor: an LC or pi
            # filter keeps its capacitor on the far side of the bead.
            caps = list(parts.ground_caps.get(net, []))
            for _, _, other in parts.series_l.get(net, []):
                caps.extend(parts.ground_caps.get(other, []))
            x, y = ctx.pt(cpad.x, cpad.y)

            # Worded for either direction: a USB-A VBUS pin carries power out, a barrel jack
            # carries it in, and in both the loads' switching current flows along the cable.
            if not caps:
                yield Finding(
                    rule=rule,
                    severity=severity(ctx, rule, "warning"),
                    title=f"{net} crosses {ref} with no capacitor to ground on it",
                    detail=(
                        f"Power passes through {ref} on {net}, and no capacitor joins it to ground, "
                        f"there or behind a ferrite. Whatever the loads draw at their switching "
                        f"frequencies is then drawn along the cable, which carries it off the board "
                        f"as conducted noise and radiates it; the same capacitor is the first thing "
                        f"an incoming surge or burst meets. Place a bulk and a ceramic capacitor at "
                        f"the connector."
                    ),
                    net=net, x=x, y=y,
                )
                continue

            d, cref = min((math.dist((q.x, q.y), (cp.x, cp.y)), cr) for q in entry for cr, cp in caps)
            if d <= max_d:
                continue
            yield Finding(
                rule=rule,
                severity=severity(ctx, rule, "warning"),
                title=f"The nearest capacitor on {net} is {d:.1f} mm from {ref}",
                detail=(
                    f"{cref} is the closest capacitor to where {net} crosses {ref}, {d:.1f} mm away "
                    f"(budget {max_d:g} mm). Switching current from the loads flows along that whole "
                    f"stretch of supply and on along the cable. Put a capacitor at the connector."
                ),
                net=net, x=x, y=y,
            )


# ---------------------------------------------------------------------------------------
# Switching regulator nodes
# ---------------------------------------------------------------------------------------

def check_switch_node(ctx: RuleContext) -> Iterator[Finding]:
    rule = "switch-node"
    parts = _parts(ctx)
    max_area = _num(ctx, rule, "max_area_mm2", 30.0)
    max_len = _num(ctx, rule, "max_length_mm", 15.0)

    def has_ic(net: str) -> bool:
        return any(IC_RE.match(p.ref) for p in parts.by_net[net])

    nodes: dict[str, str] = {}
    for net in parts.by_net:
        if _usable(net) and _kind(net) == "signal" and SW_NET.search(net) and has_ic(net):
            nodes[net] = "its name"
    # By construction when the name says nothing: the side of a power inductor that has an IC
    # pin and no capacitor, opposite a side that is filtered.
    for ref, pads in sorted(parts.by_ref.items()):
        if not IND_RE.match(ref) or len(pads) != 2:
            continue
        if FERRITE_HINT.search(f"{pads[0].value} {pads[0].footprint}"):
            continue
        a, b = pads
        if not (a.net and b.net) or a.net == b.net:
            continue
        for side, other in ((a, b), (b, a)):
            if not _usable(side.net) or _kind(side.net) != "signal":
                continue
            if parts.ground_caps.get(side.net) or not has_ic(side.net):
                continue
            if _kind(other.net) == "power" or parts.ground_caps.get(other.net):
                nodes.setdefault(side.net, f"inductor {ref}")

    if not nodes:
        return

    lengths: dict[str, float] = defaultdict(float)
    areas: dict[str, float] = defaultdict(float)
    layers: dict[str, set[str]] = defaultdict(set)
    for t in ctx.model.tracks:
        if t.net in nodes:
            run = sum(math.dist(p0, p1) for p0, p1 in zip(t.pts, t.pts[1:]))
            lengths[t.net] += run
            areas[t.net] += run * t.width_mm
            layers[t.net].add(t.layer)
    for z in ctx.model.zones:
        if z.net in nodes and len(z.ring) >= 3:
            areas[z.net] += ring_area(z.ring)
            layers[z.net].add(z.layer)

    for net, how in sorted(nodes.items()):
        area, length = areas[net], lengths[net]
        if area <= max_area and length <= max_len:
            continue
        pin = next(p for p in parts.by_net[net] if IC_RE.match(p.ref))
        x, y = ctx.pt(pin.x, pin.y)
        what = f"{area:.0f} mm² of copper" if area > max_area else f"{length:.0f} mm of track"
        yield Finding(
            rule=rule,
            severity=severity(ctx, rule, "warning"),
            title=f"Switch node {net} has {what}",
            detail=(
                f"{net} (recognised by {how}) is the switching side of a regulator at {pin.ref}: it "
                f"swings between ground and the input voltage with edges of a few nanoseconds. Its "
                f"{area:.0f} mm² of copper and {length:.0f} mm of track (budgets {max_area:g} mm² and "
                f"{max_len:g} mm) couple those edges capacitively into everything nearby, which is "
                f"how regulator noise reaches cables and neighbouring traces. Put the inductor "
                f"against the switch pin, keep only as much copper as the current needs, keep a solid "
                f"ground plane directly beneath it, and route feedback and analog traces away."
            ),
            net=net, layer=next(iter(layers[net])) if len(layers[net]) == 1 else "",
            x=x, y=y,
        )
