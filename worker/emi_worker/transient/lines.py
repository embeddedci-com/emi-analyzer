"""The lines a discharge arrives on, and the copper between connector, clamp and IC.

Discovery follows the ``esd-protection`` check exactly -- the simulation is a closer look at
the lines that check reports, so it must not find a different set. Geometry is then reduced to
the three segments that matter: the trunk from the connector to where the clamp branches off,
the stub to the clamp, and the stub to the IC.

Topology stores routed pad-to-pad paths but not the tree. On a tree the branch point falls out
of the three pairwise lengths: the distance from the root to where the paths to the clamp and
to the IC part is (L(root,clamp) + L(root,IC) - L(clamp,IC)) / 2. The same identity applied to
via counts says which segment each via sits on.

Two arrangements need more than a tree on one net:

  * an R-clamp, where the clamp sits behind a series resistor: the connector-to-resistor lead
    is kept, and the tree starts at the resistor's far pad;
  * a feed-through array (USBLC6-2SC6 pins 1 and 6), where the line passes through the package
    and the IC is on the other pin's net: the IC stub starts at that pin.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field

from .. import impedance
from ..rules import emc
from ..rules.decoupling import IC_RE, cap_farads
from ..rules.model import RuleContext
from . import parts as partlib

#: A branch-point estimate this far outside its bounds means the routed paths are not a tree
#: -- a loop through a pour, usually -- and straight-line placement is used instead.
TREE_TOLERANCE_MM = 0.5
#: Characteristic impedance assumed when a layer has no reference plane to compute one from.
FALLBACK_Z0_OHM = 60.0
#: Width assumed for a clamp's ground connection when converting its length to inductance.
GROUND_TRACE_WIDTH_MM = 0.5
DEFAULT_DRILL_MM = 0.3
DEFAULT_SUPPLY_V = 3.3
#: A capacitor whose value cannot be read counts as this.
ASSUMED_CAP_F = 100e-9


@dataclass
class Segment:
    length_mm: float
    td_ps: float
    z0_ohm: float
    vias: int = 0

    def as_dict(self) -> dict:
        return {"length_mm": round(self.length_mm, 2), "td_ps": round(self.td_ps, 2),
                "z0_ohm": round(self.z0_ohm, 1), "vias": self.vias}


@dataclass
class LineTree:
    trunk: Segment
    clamp_stub: Segment | None
    ic_stub: Segment | None
    routed: bool
    #: Whole root-to-clamp and root-to-IC paths, for what-ifs that move the clamp.
    clamp_path: Segment | None = None
    ic_path: Segment | None = None


@dataclass
class ExposedLine:
    net: str
    connector: str
    connector_pad: object
    #: The clamp on the line, or None for an unprotected line.
    clamp_ref: str | None = None
    clamp_pad: object | None = None
    clamp_ground_pad: object | None = None
    #: Every pad of the clamp part, for wiring a vendor model's pins.
    clamp_pads: list = field(default_factory=list)
    clamp_part: str = ""
    clamp_net: str = ""
    #: Voltage of the supply a clamp's rail pin sits on, or None when it has none.
    clamp_rail_v: float | None = None
    #: (ref, ohms, pad on the connector net, pad on the clamp net) for an R-clamp.
    series_resistor: tuple[str, float, object, object] | None = None
    #: Connector to the series resistor, when there is one.
    lead: Segment | None = None
    #: A feed-through clamp's far pin, when the IC is beyond the package.
    through_pad: object | None = None
    ic_pad: object | None = None
    ic_net: str = ""
    ic_supply_net: str = ""
    ic_supply_v: float = DEFAULT_SUPPLY_V
    #: Capacitance to ground on the IC's net: decoupling on a supply, a filter on a signal.
    net_c_nf: float = 0.0
    tree: LineTree | None = None
    ground_gap_mm: float = 0.0
    ground_nh: float = 0.0
    via_nh: float = 0.0
    notes: list[str] = field(default_factory=list)


_OHMS = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(meg|[rkmΩ]?)(\d*)\s*(?:ohms?|Ω)?\b", re.I)


def parse_ohms(value: str) -> float | None:
    """"100", "1k", "4k7", "22R", "0R", "10Ω", "1M", "2meg" -> ohms."""
    m = _OHMS.match(value or "")
    if not m:
        return None
    whole, suffix, frac = m.group(1).replace(",", "."), m.group(2), m.group(3)
    number = float(f"{whole}.{frac}" if frac and "." not in whole else whole)
    scale = {"": 1.0, "r": 1.0, "Ω": 1.0, "k": 1e3, "m": 1e-3, "meg": 1e6}
    if suffix == "M":
        return number * 1e6
    return number * scale.get(suffix.lower(), 1.0)


def via_inductance_nh(height_mm: float, drill_mm: float) -> float:
    """The common closed form for a through via: L = 0.2 h (ln(4h/d) + 1) nH, mm in."""
    h, d = max(height_mm, 0.05), max(drill_mm, 0.05)
    return 0.2 * h * (math.log(4.0 * h / d) + 1.0)


def named_volts(net: str) -> float | None:
    m = emc.VOLTS.search(net or "")
    return float(f"{m.group(1)}.{m.group(2) or 0}") if m else None


class _Electrics:
    """Per-net, per-layer line parameters, computed once."""

    def __init__(self, ctx: RuleContext):
        self.ctx = ctx
        self._widths: dict[tuple[str, str], float] = {}
        acc: dict[tuple[str, str], list[float]] = {}
        for t in ctx.model.tracks:
            if not t.net:
                continue
            run = sum(math.dist(a, b) for a, b in zip(t.pts, t.pts[1:]))
            w = acc.setdefault((t.net, t.layer), [0.0, 0.0])
            w[0] += run * t.width_mm
            w[1] += run
        for key, (area, run) in acc.items():
            if run > 0:
                self._widths[key] = area / run
        self.notes: list[str] = []

    def width(self, net: str, layer: str) -> float:
        return self._widths.get((net, layer)) or statistics.median(
            [t.width_mm for t in self.ctx.model.tracks if t.net == net] or [0.25]
        )

    def z0(self, net: str, layer: str, width: float | None = None) -> float:
        elec = self.ctx.electrics.layer(layer) if self.ctx.electrics else None
        z = impedance.single_ended(width or self.width(net, layer), elec) if elec else None
        if z is None:
            note = f"{layer} has no reference plane to compute impedance against; {FALLBACK_Z0_OHM:g} Ω assumed"
            if note not in self.notes:
                self.notes.append(note)
            return FALLBACK_Z0_OHM
        return z.ohm

    def ps_per_mm(self, layer: str) -> float:
        return self.ctx.ps_per_mm(layer)

    def path(self, net: str, a, b) -> tuple[float, bool, object | None]:
        """(length mm, routed?, topology Path or None)."""
        topo = self.ctx.topology.get(net) if self.ctx.topology else None
        path = topo.path(f"{a.ref}.{a.number}", f"{b.ref}.{b.number}") if topo else None
        if path is not None and path.length_mm > 0:
            return path.length_mm, True, path
        return math.dist((a.x, a.y), (b.x, b.y)), False, None

    def per_mm(self, net: str, path, fallback_layer: str) -> tuple[float, float]:
        """Length-weighted (ps/mm, Z0) along a path."""
        runs = [(r.layer, r.length_mm) for r in path.runs] if path is not None else []
        runs = [r for r in runs if r[1] > 0] or [(fallback_layer, 1.0)]
        total = sum(length for _, length in runs)
        ps = sum(self.ps_per_mm(layer) * length for layer, length in runs) / total
        z0 = sum(self.z0(net, layer) * length for layer, length in runs) / total
        return ps, z0


def _pad_layer(pad) -> str:
    return next((layer for layer in pad.layers if layer.endswith(".Cu") and "*" not in layer), "F.Cu")


def _segment(length: float, ps: float, z0: float, vias: int = 0) -> Segment:
    return Segment(length_mm=max(length, 0.0), td_ps=max(length, 0.0) * ps, z0_ohm=z0, vias=max(vias, 0))


def build_tree(el: _Electrics, net: str, root, clamp_pad, ic_pad) -> LineTree:
    layer = _pad_layer(root)

    def geometry(a, b):
        return el.path(net, a, b) if (a is not None and b is not None) else (0.0, False, None)

    l_rc, r_rc, p_rc = geometry(root, clamp_pad)
    l_ri, r_ri, p_ri = geometry(root, ic_pad)
    l_ci, _, p_ci = geometry(clamp_pad, ic_pad)
    routed = all(r for r, pad in ((r_rc, clamp_pad), (r_ri, ic_pad)) if pad is not None)

    ps_c, z_c = el.per_mm(net, p_rc, layer)
    ps_i, z_i = el.per_mm(net, p_ri, layer)

    def vias(p) -> int:
        return p.vias if p is not None else 0

    if clamp_pad is not None and ic_pad is not None:
        t = (l_rc + l_ri - l_ci) / 2.0
        if t < -TREE_TOLERANCE_MM or l_rc - t < -TREE_TOLERANCE_MM or l_ri - t < -TREE_TOLERANCE_MM:
            # Not a tree. Straight-line distances always are one (triangle inequality).
            l_rc = math.dist((root.x, root.y), (clamp_pad.x, clamp_pad.y))
            l_ri = math.dist((root.x, root.y), (ic_pad.x, ic_pad.y))
            l_ci = math.dist((clamp_pad.x, clamp_pad.y), (ic_pad.x, ic_pad.y))
            t = (l_rc + l_ri - l_ci) / 2.0
            routed = False
            el.notes.append(
                f"{net}: the routed paths do not form a tree (a loop through a pour?), so the "
                f"branch point was placed from straight-line distances"
            )
        t = min(max(t, 0.0), min(l_rc, l_ri))
        tv = max(0, round((vias(p_rc) + vias(p_ri) - vias(p_ci)) / 2))
        tree = LineTree(
            trunk=_segment(t, ps_c, z_c, tv),
            clamp_stub=_segment(l_rc - t, ps_c, z_c, vias(p_rc) - tv),
            ic_stub=_segment(l_ri - t, ps_i, z_i, vias(p_ri) - tv),
            routed=routed,
        )
    elif clamp_pad is not None:
        tree = LineTree(trunk=_segment(l_rc, ps_c, z_c, vias(p_rc)), clamp_stub=_segment(0.0, ps_c, z_c),
                        ic_stub=None, routed=routed)
    elif ic_pad is not None:
        tree = LineTree(trunk=_segment(l_ri, ps_i, z_i, vias(p_ri)), clamp_stub=None,
                        ic_stub=_segment(0.0, ps_i, z_i), routed=routed)
    else:
        tree = LineTree(trunk=_segment(0.0, ps_c, z_c), clamp_stub=None, ic_stub=None, routed=False)
    tree.clamp_path = _segment(l_rc, ps_c, z_c, vias(p_rc)) if clamp_pad is not None else None
    tree.ic_path = _segment(l_ri, ps_i, z_i, vias(p_ri)) if ic_pad is not None else None
    return tree


def _nearest(el: _Electrics, net: str, root, candidates: list):
    if not candidates:
        return None
    return min(candidates, key=lambda c: el.path(net, root, c[1] if isinstance(c, tuple) else c)[0])


def _supply(parts, ic_ref: str) -> tuple[str, float]:
    supplies = sorted({p.net for p in parts.by_ref[ic_ref] if p.net and emc._kind(p.net) == "power"})
    for net in supplies:
        volts = named_volts(net)
        if volts is not None:
            return net, volts
    return (supplies[0] if supplies else ""), DEFAULT_SUPPLY_V


def exposed_lines(ctx: RuleContext, edge_mm: float = 5.0, only: set[str] | None = None) -> tuple[list[ExposedLine], list[str]]:
    """Every line the esd-protection check looks at, with the geometry to simulate it.

    Returns the lines and any notes about how their geometry was estimated.
    """
    parts = emc._parts(ctx)
    el = _Electrics(ctx)
    drills = [v.drill_mm for v in ctx.model.vias if v.drill_mm > 0]
    via_nh = via_inductance_nh(ctx.model.thickness_mm / 2.0, statistics.median(drills) if drills else DEFAULT_DRILL_MM)
    stitches = parts.ground_stitches()
    out: list[ExposedLine] = []

    for ref, pads in sorted(parts.connectors.items()):
        if not parts.external(pads, edge_mm):
            continue
        for net, cpad in sorted(parts.lines_leaving(pads).items()):
            if emc._kind(net) == "power" and not emc._power_entry(net):
                continue
            if only and net not in only:
                continue
            line = ExposedLine(net=net, connector=ref, connector_pad=cpad, clamp_net=net, via_nh=via_nh)
            root = cpad
            clamps = parts.clamps.get(net, [])

            if not clamps:
                for rref, rpad, other in parts.series_r.get(net, []):
                    if emc._kind(other) != "signal" or not parts.clamps.get(other):
                        continue
                    far = next(p for p in parts.by_ref[rref] if p.net == other)
                    ohms = parse_ohms(rpad.value)
                    if ohms is None:
                        ohms = 100.0
                        line.notes.append(f"{rref}'s value {rpad.value!r} could not be read; 100 Ω assumed")
                    length, _, path = el.path(net, cpad, rpad)
                    ps, z0 = el.per_mm(net, path, _pad_layer(cpad))
                    line.lead = _segment(length, ps, z0, path.vias if path is not None else 0)
                    line.series_resistor = (rref, ohms, rpad, far)
                    line.clamp_net = other
                    clamps = parts.clamps[other]
                    root = far
                    break

            if clamps:
                cref, cpad_on_line, gnd = _nearest(el, line.clamp_net, root, clamps)
                line.clamp_ref, line.clamp_pad, line.clamp_ground_pad = cref, cpad_on_line, gnd
                line.clamp_part = cpad_on_line.value
                line.clamp_pads = list(parts.by_ref[cref])
                rails = [named_volts(p.net) for p in line.clamp_pads if p.net and emc._kind(p.net) == "power"]
                if any(p.net and emc._kind(p.net) == "power" for p in line.clamp_pads):
                    line.clamp_rail_v = next((v for v in rails if v is not None), DEFAULT_SUPPLY_V)

            line.ic_net = line.clamp_net
            ics = [q for q in parts.by_net[line.clamp_net] if IC_RE.match(q.ref) and q.ref != line.clamp_ref]
            if ics:
                line.ic_pad = _nearest(el, line.clamp_net, root, ics)
            elif line.clamp_pad is not None:
                spec = partlib.lookup(line.clamp_part)
                partner_number = spec.feedthrough.get(line.clamp_pad.number) if spec else None
                partner = next((p for p in line.clamp_pads if partner_number and p.number == partner_number and p.net), None)
                beyond = [q for q in parts.by_net[partner.net] if IC_RE.match(q.ref) and q.ref != line.clamp_ref] if partner else []
                if beyond:
                    line.through_pad, line.ic_net = partner, partner.net
                    line.ic_pad = _nearest(el, partner.net, partner, beyond)
            if line.ic_pad is not None:
                line.ic_supply_net, line.ic_supply_v = _supply(parts, line.ic_pad.ref)
            else:
                line.notes.append(f"no IC found on {line.clamp_net}, so there is no pin to measure; the clamp is still simulated")

            if line.through_pad is not None:
                line.tree = build_tree(el, line.clamp_net, root, line.clamp_pad, None)
                length, routed, path = el.path(line.ic_net, line.through_pad, line.ic_pad)
                ps, z0 = el.per_mm(line.ic_net, path, _pad_layer(line.through_pad))
                line.tree.ic_stub = line.tree.ic_path = _segment(length, ps, z0, path.vias if path is not None else 0)
                line.tree.routed = line.tree.routed and routed
            else:
                line.tree = build_tree(el, line.clamp_net, root, line.clamp_pad, line.ic_pad)

            # Capacitance to ground on the IC's net. On a supply it is the decoupling, and it
            # is what actually absorbs a discharge there; leaving it out made a supply pin look
            # like a bare logic input, at a hundred volts.
            guessed = 0
            total_f = 0.0
            for _, cap_pad in parts.ground_caps.get(line.ic_net, []):
                farads = cap_farads(cap_pad.value)
                if farads is None:
                    farads = ASSUMED_CAP_F
                    guessed += 1
                total_f += farads
            line.net_c_nf = total_f * 1e9
            if guessed:
                line.notes.append(
                    f"{guessed} capacitor value{'s' if guessed != 1 else ''} on {line.ic_net} could not be read; "
                    f"{ASSUMED_CAP_F * 1e9:.0f} nF assumed for each"
                )

            gnd = line.clamp_ground_pad
            if gnd is not None:
                gap = 0.0 if gnd.is_through or not stitches else min(emc._pad_gap(gnd, s) for s in stitches)
                layer = _pad_layer(gnd)
                nh_per_mm = el.z0(gnd.net, layer, GROUND_TRACE_WIDTH_MM) * el.ps_per_mm(layer) * 1e-3
                line.ground_gap_mm = gap
                line.ground_nh = gap * nh_per_mm + via_nh
            elif line.clamp_ref:
                line.notes.append(f"{line.clamp_ref} has no ground pin; it clamps between lines, not to ground")
            out.append(line)

    return out, el.notes
