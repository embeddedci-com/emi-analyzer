"""From the layout to an input filter: the power entry, the rail behind it, and its regulators.

Discovery follows the ``input-filter`` and ``switch-node`` checks, so the scan looks at the
connector and the regulators those checks report and cannot find a different set:

  * the power entry is a supply net crossing an edge connector (``input-filter``);
  * the input rail is that net and whatever is one series part behind it -- a ferrite, an
    inductor, a fuse, a series diode, a low-value resistor -- up to three parts deep;
  * a regulator is a part with one pad on a switch node (``switch-node``) and one on the rail.

Each net of the rail becomes a ladder ordered by routed distance from where the rail enters that
net. A capacitor on a stub off the main run is therefore treated as if it sat on the run at the
same distance; on the boards this was tried on, input rails are short runs or pours and the
difference is a few nanohenries. Where routing cannot be traced, straight-line distance is used
and the network says so.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field

from ..components.match import match_part
from ..components.resolve import resolve_part
from ..rules import emc
from ..rules.decoupling import CAP_RE, IC_RE, cap_farads
from ..rules.model import RuleContext
from ..transient import lines as tlines
from .network import Network, Series, Shunt

#: How many series parts deep the input rail is followed.
MAX_DEPTH = 3
#: A series resistor above this is not a supply path (a pull-up, a sense divider).
MAX_SERIES_OHM = 10.0
#: A part in series with the supply whose value cannot be read.
ASSUMED_INDUCTOR_H = 1e-6
ASSUMED_FERRITE_OHM_100MHZ = 120.0
ASSUMED_FUSE_OHM = 0.05
ASSUMED_DIODE_OHM = 0.1
#: A capacitor the library does not know: ESR and ESL by kind.
ASSUMED_BULK = (0.1, 5e-9)    # electrolytic or tantalum: 0.1 Ω, 5 nH
ASSUMED_CERAMIC = (0.01, 1e-9)
ASSUMED_CAP_F = 1e-6
#: Width assumed for a pour when the rail runs through one, bounded because a pour's bounding
#: box says little about the width the current actually uses.
POUR_WIDTH_MM = (1.0, 20.0)

FUSE_RE = re.compile(r"^F\d", re.I)
DIODE_RE = re.compile(r"^D\d", re.I)
SWITCH_PART_RE = re.compile(r"^(U|IC|Q)\d", re.I)
BULK_HINT = re.compile(r"cp_|elec|tantal|polar|radial|\bcp\b|_cp", re.I)
_HENRIES = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([pnuµm]?)(\d*)\s*h?\b", re.I)
_PREFIX = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}


def parse_henries(value: str) -> float | None:
    """"4.7uH", "10u", "4u7", "100nH" -> henries. None when it does not parse.

    A bare number is not read as henries: "BLM18" or "600" on an inductor footprint is a part
    number or an impedance, and reading it as 600 H would silence the whole rail.
    """
    m = _HENRIES.match(value or "")
    if not m or not m.group(2):
        return None
    whole, prefix, frac = m.group(1).replace(",", "."), m.group(2).lower(), m.group(3)
    number = float(f"{whole}.{frac}" if frac and "." not in whole else whole)
    return number * _PREFIX[prefix]


_FERRITE_OHM = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:r|Ω|ohms?)\b", re.I)


def ferrite_ohms(value: str) -> float | None:
    m = _FERRITE_OHM.search(value or "")
    return float(m.group(1).replace(",", ".")) if m else None


@dataclass
class Entry:
    connector: str
    net: str
    pad: object
    ground_pad: object | None

    @property
    def id(self) -> str:
        return f"{self.connector}:{self.net}"


@dataclass
class Regulator:
    ref: str
    switch_net: str
    how: str
    input_pad: object
    input_net: str
    output_net: str = ""
    duty_from_rails: float | None = None
    node: str = ""


@dataclass
class Discovery:
    entries: list[Entry] = field(default_factory=list)
    entry: Entry | None = None
    rail_nets: list[str] = field(default_factory=list)
    network: Network = field(default_factory=Network)
    regulators: list[Regulator] = field(default_factory=list)
    #: (ref, why) for switching parts found and not modelled.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    routed: bool = True


class _Electrics(tlines._Electrics):
    """Line parameters that know a supply often runs through a pour, not a track."""

    def __init__(self, ctx: RuleContext):
        super().__init__(ctx)
        self._pours: dict[tuple[str, str], float] = {}
        for z in ctx.model.zones:
            if len(z.ring) < 3:
                continue
            xs = [p[0] for p in z.ring]
            ys = [p[1] for p in z.ring]
            w = min(max(xs) - min(xs), max(ys) - min(ys))
            key = (z.net, z.layer)
            self._pours[key] = max(self._pours.get(key, 0.0), w)

    def width(self, net: str, layer: str) -> float:
        pour = self._pours.get((net, layer))
        if pour:
            lo, hi = POUR_WIDTH_MM
            return min(max(pour, lo), hi)
        return super().width(net, layer)

    def nh_per_mm(self, net: str, path, layer: str) -> float:
        ps, z0 = self.per_mm(net, path, layer)
        return z0 * ps * 1e-3


def entries(ctx: RuleContext, edge_mm: float = 5.0) -> list[Entry]:
    """Every supply net crossing an edge connector, as the input-filter check finds them."""
    parts = emc._parts(ctx)
    out: list[Entry] = []
    for ref, pads in sorted(parts.connectors.items()):
        if not parts.external(pads, edge_mm):
            continue
        ground = next((p for p in pads if p.net and emc._kind(p.net) == "ground"), None)
        for net, pad in sorted(parts.lines_leaving(pads).items()):
            if emc._power_entry(net):
                out.append(Entry(connector=ref, net=net, pad=pad, ground_pad=ground))
    return out


def _series_parts(parts, net: str, switch: set[str]) -> list[tuple[str, object, object, str]]:
    """(ref, pad on this net, pad on the far net, kind) for the parts the supply passes through."""
    out = []
    for ref, pads in parts.by_ref.items():
        if len(pads) != 2 or not all(p.net for p in pads):
            continue
        a, b = pads
        if a.net == b.net or net not in (a.net, b.net):
            continue
        near, far = (a, b) if a.net == net else (b, a)
        if emc._kind(far.net) == "ground" or far.net in switch or not emc._usable(far.net):
            continue
        hint = f"{near.value} {near.footprint}"
        if emc.SERIES_L_RE.match(ref):
            kind = "ferrite" if (emc.FERRITE_HINT.search(hint) or ref.upper().startswith("FB")) else "inductor"
        elif emc.RES_RE.match(ref):
            ohms = tlines.parse_ohms(near.value)
            if ohms is None or ohms > MAX_SERIES_OHM:
                continue
            kind = "resistor"
        elif FUSE_RE.match(ref):
            kind = "fuse"
        elif DIODE_RE.match(ref) and not emc.LED_HINT.search(hint):
            kind = "diode"
        else:
            continue
        out.append((ref, near, far, kind))
    return sorted(out, key=lambda t: t[0])


def _series_element(ref: str, pad, kind: str) -> tuple[Series, str | None]:
    """The element for a series part, and a note when its value was assumed."""
    value = pad.value or ""
    if kind == "inductor":
        h = parse_henries(value)
        if h is None:
            return (Series("", "", ASSUMED_INDUCTOR_H, 0.02, kind, ref, value, True),
                    f"{ref}'s value {value!r} could not be read as an inductance; "
                    f"{ASSUMED_INDUCTOR_H * 1e6:g} µH assumed")
        return Series("", "", h, 0.02, kind, ref, value, False), None
    if kind == "ferrite":
        z = ferrite_ohms(value)
        note = None
        if z is None:
            z = ASSUMED_FERRITE_OHM_100MHZ
            note = f"{ref}'s impedance {value!r} could not be read; {z:g} Ω at 100 MHz assumed"
        # Below its peak a bead is mostly inductive: L from its impedance at 100 MHz.
        return Series("", "", z / (2 * math.pi * 100e6), 0.05, kind, ref, value, True), note
    if kind == "resistor":
        return Series("", "", 0.0, tlines.parse_ohms(value) or 0.0, kind, ref, value, False), None
    if kind == "fuse":
        return Series("", "", 0.0, ASSUMED_FUSE_OHM, kind, ref, value, True), None
    return Series("", "", 0.0, ASSUMED_DIODE_OHM, kind, ref, value, True), None


def _shunt(ref: str, pad, gnd_pad, mount_h: float) -> tuple[Shunt, str | None]:
    part = match_part(ref, pad.value, pad.footprint)
    got = resolve_part(part)
    if got is not None and got.placeable:
        rlc = got.rlc
        model = "library, generic for the package" if got.generic else f"library: {got.component_name}"
        return Shunt(node="", ref=ref, value=pad.value, c_f=rlc.c_f, esr_ohm=rlc.esr_ohm,
                     esl_h=rlc.esl_h, mount_h=mount_h, model=model), None
    farads = cap_farads(pad.value)
    note = None
    if farads is None:
        farads = ASSUMED_CAP_F
        note = f"{ref}'s value {pad.value!r} could not be read; {ASSUMED_CAP_F * 1e6:g} µF assumed"
    bulk = bool(BULK_HINT.search(pad.footprint or ""))
    esr, esl = ASSUMED_BULK if bulk else ASSUMED_CERAMIC
    return Shunt(node="", ref=ref, value=pad.value, c_f=farads, esr_ohm=esr, esl_h=esl, mount_h=mount_h,
                 model=f"assumed {'electrolytic' if bulk else 'ceramic'}: {esr * 1e3:g} mΩ, {esl * 1e9:g} nH",
                 assumed=True), note


def discover(ctx: RuleContext, choice: str | None = None, edge_mm: float = 5.0) -> Discovery:
    d = Discovery(entries=entries(ctx, edge_mm))
    if not d.entries:
        d.notes.append("no supply net crosses an edge connector, so there is no power input to measure")
        return d
    d.entry = next((e for e in d.entries if e.id == choice), d.entries[0])
    if choice and d.entry.id != choice:
        d.notes.append(f"{choice} is not a power input on this board; {d.entry.id} was used")

    parts = emc._parts(ctx)
    el = _Electrics(ctx)
    drills = [v.drill_mm for v in ctx.model.vias if v.drill_mm > 0]
    via_h = tlines.via_inductance_nh(
        ctx.model.thickness_mm / 2.0, statistics.median(drills) if drills else tlines.DEFAULT_DRILL_MM) * 1e-9
    stitches = parts.ground_stitches()
    switch = emc.switch_nodes(ctx)

    # The rail: nets reachable from the entry through series parts, breadth first.
    rail: dict[str, tuple[object, str]] = {d.entry.net: (d.entry.pad, "in")}
    crossings: dict[str, list[tuple[str, object, object, str]]] = {}
    frontier = [d.entry.net]
    for _ in range(MAX_DEPTH):
        nxt = []
        for net in frontier:
            for ref, near, far, kind in _series_parts(parts, net, set(switch)):
                if far.net in rail:
                    continue
                crossings.setdefault(net, []).append((ref, near, far, kind))
                rail[far.net] = (far, "")
                nxt.append(far.net)
        frontier = nxt
    d.rail_nets = list(rail)

    # Regulators: a part on a switch node and on the rail.
    boost: set[str] = set()
    for sw, how in sorted(switch.items()):
        for ref, pads in sorted(parts.by_ref.items()):
            if not emc.IND_RE.match(ref) or len(pads) != 2:
                continue
            nets = {p.net for p in pads}
            if sw in nets and nets & set(rail):
                boost.add(sw)
        on_sw = sorted({p.ref for p in parts.by_net[sw] if SWITCH_PART_RE.match(p.ref)})
        for ref in on_sw:
            if sw in boost:
                d.skipped.append((ref, f"{sw} is fed from the input through an inductor (a boost): its input "
                                       f"current is continuous and not a pulse train, which is not modelled"))
                continue
            sw_pad = next(p for p in parts.by_ref[ref] if p.net == sw)
            ins = [p for p in parts.by_ref[ref] if p.net in rail]
            if not ins:
                if IC_RE.match(ref):
                    d.skipped.append((ref, f"not fed from {d.entry.net}: its input ripple reaches the power "
                                           f"input only through another regulator, which is not modelled"))
                continue
            pin = min(ins, key=lambda p: math.dist((p.x, p.y), (sw_pad.x, sw_pad.y)))
            reg = Regulator(ref=ref, switch_net=sw, how=how, input_pad=pin, input_net=pin.net)
            ind = next((pads for r, pads in parts.by_ref.items()
                        if emc.IND_RE.match(r) and len(pads) == 2 and sw in {p.net for p in pads}), None)
            if ind:
                reg.output_net = next(p.net for p in ind if p.net != sw)
                v_in = tlines.named_volts(pin.net) or tlines.named_volts(d.entry.net)
                v_out = tlines.named_volts(reg.output_net)
                if v_in and v_out and 0 < v_out < v_in:
                    reg.duty_from_rails = round(v_out / v_in, 3)
            if any(r.ref == ref for r in d.regulators):
                continue
            d.regulators.append(reg)
    # A controller and its external switch are both on the node; one source is the regulator.
    controllers = {r.switch_net: r for r in d.regulators if IC_RE.match(r.ref)}
    d.regulators = [r for r in d.regulators if IC_RE.match(r.ref) or r.switch_net not in controllers]

    # The ladders.
    net_model = d.network
    counter = iter(range(10_000))

    def ladder(net: str, root, root_node: str) -> None:
        layer = tlines._pad_layer(root)
        items: list[tuple[float, int, str, object, object]] = []
        for cref, cpad in parts.ground_caps.get(net, []):
            if CAP_RE.match(cref):
                items.append((0.0, 0, "cap", cref, cpad))
        for reg in d.regulators:
            if reg.input_net == net:
                items.append((0.0, 0, "reg", reg, reg.input_pad))
        for ref, near, far, kind in crossings.get(net, []):
            items.append((0.0, 0, "series", (ref, far, kind), near))
        placed = []
        for _, _, what, obj, pad in items:
            length, routed, path = el.path(net, root, pad)
            if not routed and length > 0:
                d.routed = False
            placed.append((length, path.vias if path is not None else 0, what, obj, pad, path))
        placed.sort(key=lambda t: t[0])
        prev_node, prev_d, prev_v = root_node, 0.0, 0
        for length, vias, what, obj, pad, path in placed:
            node = f"n{next(counter)}"
            step = max(length - prev_d, 0.0)
            l_h = step * el.nh_per_mm(net, path, layer) * 1e-9 + max(vias - prev_v, 0) * via_h
            net_model.series.append(Series(prev_node, node, l_h, 0.0, "trace", "", "", False, round(step, 2)))
            prev_node, prev_d, prev_v = node, max(length, prev_d), max(vias, prev_v)
            if what == "cap":
                gnd = next((p for p in parts.by_ref[obj] if p is not pad), None)
                gap = 0.0
                if gnd is not None and not gnd.is_through and stitches:
                    gap = min(emc._pad_gap(gnd, s) for s in stitches)
                # The capacitor's ground connection: its via, and any track from the pad to it,
                # converted the way the ESD simulation converts a clamp's.
                glayer = tlines._pad_layer(gnd or pad)
                nh_mm = (el.z0(gnd.net if gnd else net, glayer, tlines.GROUND_TRACE_WIDTH_MM)
                         * el.ps_per_mm(glayer) * 1e-3)
                mount = via_h + gap * nh_mm * 1e-9
                shunt, note = _shunt(obj, pad, gnd, mount)
                shunt.node, shunt.distance_mm = node, round(length, 2)
                net_model.shunts.append(shunt)
                if note:
                    d.notes.append(note)
            elif what == "reg":
                obj.node = node
                net_model.sources[obj.ref] = node
            else:
                ref, far, kind = obj
                element, note = _series_element(ref, pad, kind)
                child = f"n{next(counter)}"
                element.a, element.b = node, child
                net_model.series.append(element)
                if note:
                    d.notes.append(note)
                ladder(far.net, far, child)

    ladder(d.entry.net, d.entry.pad, net_model.entry)
    if not d.routed:
        d.notes.append("some of the rail could not be traced through its copper; straight-line distances were used there")
    d.notes += [n for n in el.notes if n not in d.notes]
    return d
