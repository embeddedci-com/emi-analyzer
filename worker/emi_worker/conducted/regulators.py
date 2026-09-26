"""Which switching regulators a board has, what kind each is, and how each was recognised.

The scan starts from the ``switch-node`` check's nodes and adds what that check cannot see or
does not need to tell apart. On the four real boards it was first run on it found the power
input on all four and a regulator on one; the others had:

  * a boost, whose inductor runs from the input rail to its switch node. It was skipped, because
    the same layout is also a buck making the rail. The part's other supplies now decide: a
    buck making the rail draws from another power input, a boost does not;
  * a buck module (TPS82130) with its inductor inside, so no switch node on the board at all.
    Known by its part number;
  * a four-switch buck-boost controller with external FETs, whose inductor has a U reference and
    whose input is the high-side FET's drain, not a pin of the controller. Inductors are also
    known by footprint, and a FET's pad on the rail is preferred to the controller's own;
  * a PMIC with seven bucks, read as one source. Each switch node with its own inductor is now
    its own source.

Every regulator carries the cues it was found by and a confidence, so a wrong guess is visible
and the user can change its type or remove it:

  * high: the layout says the topology (which side of the inductor is on the input rail) and a
    second cue agrees, the switch node's name or the part number;
  * medium: one cue only, the layout or the part number;
  * low: the cues disagree, or the topology is a default (a buck-boost's mode).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..rules import emc
from ..rules.decoupling import IC_RE
from ..transient import lines as tlines

TOPOLOGIES = ("buck", "boost", "buck-boost", "inverting")

#: An inductor by footprint or value when its reference is not L: a four-switch controller's
#: inductor on a real board was U3, on an "IND-SMD" footprint.
IND_HINT = re.compile(r"(^|[^a-z])ind([^a-z]|$)|inductor", re.I)
FET_RE = re.compile(r"^Q\d", re.I)
DIODE_RE = re.compile(r"^D\d", re.I)
#: A pass element (charger, power path, LDO) is a small IC; an MCU with a pad on two supplies
#: is not one.
MAX_PASS_PADS = 16

#: Part numbers whose topology is known: (pattern, topologies, what it is). First match wins, so
#: a specific part goes before the family it would otherwise fall into (TPS65131 before TPS65).
#: Not a complete list: a part not here is recognised by its layout alone.
PART_NUMBERS: list[tuple[re.Pattern, tuple[str, ...], str]] = [
    (re.compile(r"^(TPS8\d{4}|TPSM\d|LMZM?\d|MPM\d|LTM\d|MAXM1\d|RPM\d|MIC45\d)"), ("buck",), "buck module"),
    (re.compile(r"^TPS6513"), ("boost", "inverting"), "boost and inverting converter"),
    (re.compile(r"^(STPMIC|AXP\d|RK8\d\d|PCA945|TPS65\d|DA90\d|DA92\d|MP5416|BD718|ACT88)"), ("buck",), "PMIC"),
    (re.compile(r"^(TPS63\d|TPS55288|LM517[5-8]|LT839\d|LTC378\d|ISL8180\d|MP886\d|MP28167|SC881\d)"),
     ("buck-boost",), "buck-boost"),
    (re.compile(r"^(TPS61\d|TPS5534\d|MT3608|SX1308|AP3012|LM2735|LM3478|LMR6242|XL6009|SY7208|FP6291|"
                r"MP3426|LT1930|LTC3426)"), ("boost",), "boost"),
    (re.compile(r"^(TPS40\d|LM514\d|LM5116|LTC389\d|LTC383\d)"), ("buck",), "buck controller"),
    (re.compile(r"^(TPS5[46]\d|TPS62\d|TLV62\d|LMR[1-3]\d|LM259\d|LM267\d|MP1\d{3}|MP2\d{3}|MP8\d{3}|"
                r"AP6[2-5]\d|SY8\d{3}|RT[68]\d{3}|LT86\d\d|MIC24\d|ADP2[13]\d|AOZ\d|SGM61\d|JW5\d)"),
     ("buck",), "buck"),
]


def part_number(value: str) -> tuple[tuple[str, ...], str]:
    """(topologies, what it is) for a known part number, or ((), "")."""
    v = re.sub(r"\s+", "", value or "").upper()
    for pattern, topologies, what in PART_NUMBERS:
        if pattern.match(v):
            return topologies, what
    return (), ""


def is_inductor(ref: str, pads: list) -> bool:
    """A two-pad power inductor: an L reference or an inductor footprint, never a ferrite."""
    if len(pads) != 2 or ref.upper().startswith("FB"):
        return False
    hint = f"{pads[0].value} {pads[0].footprint}"
    if emc.FERRITE_HINT.search(hint):
        return False
    return bool(emc.IND_RE.match(ref) or IND_HINT.search(pads[0].footprint or "") or IND_HINT.search(pads[0].value or ""))


def negative(net: str) -> bool:
    return bool(re.match(r"^[/]?-\d", net or "") or re.search(r"(^|[/_])(-|neg|vneg|vee)", net or "", re.I))


def short(ref: str, net: str, inductor: str) -> str:
    """A name for one of a part's outputs: "VLX1" from Net-(U1-VLX1), "PHASE1" from /PHASE1."""
    m = re.match(r"^Net-\((.+)-([^-]+)\)$", net or "")
    if m:
        return m.group(2) if m.group(1) == ref else (inductor or m.group(2))
    tail = (net or "").rstrip("/").split("/")[-1]
    return tail or inductor or net


@dataclass
class Candidate:
    """A switching regulator on the board, before it is known whether the input rail feeds it."""

    ref: str
    #: "switch node", "controller" (external FETs) or "module" (no switch node on the board).
    kind: str
    switch_nets: list[str]
    how: list[str]
    inductor: str = ""
    inductance_h: float | None = None
    #: The inductor's other side and its pad there.
    far_net: str = ""
    far_pad: object = None
    #: The external switches' pads off the switch node: the high-side drain is among them.
    fet_pads: list = field(default_factory=list)
    #: The part's own pads on anything but ground and its switch nodes.
    ic_pads: list = field(default_factory=list)
    #: The switch pin, to choose the input pin nearest to it.
    anchor: object = None
    part_number: str = ""
    pn_topologies: tuple[str, ...] = ()
    pn_what: str = ""
    #: "inverting" (inductor to ground) or "buck-boost" (inductor between two switch nodes).
    layout_hint: str = ""
    #: The far side of a diode off the switch node: a boost's or an inverting stage's output.
    diode_net: str = ""
    #: The switch node was named like one: a second cue for the confidence.
    named: bool = False


def _supply(net: str) -> bool:
    return bool(net) and emc._usable(net) and emc._kind(net) != "ground"


def find(ctx, parts) -> list[Candidate]:
    """Every switching regulator on the board, and parts that look like one but cannot be
    modelled (kind "unmatched"). Whether the input rail feeds each is decided by ``attach``."""
    from .rail import parse_henries  # rail imports this module

    nodes = dict(emc.switch_nodes(ctx))
    inductors = {ref: pads for ref, pads in parts.by_ref.items() if is_inductor(ref, pads)}

    def ics_on(net: str) -> list[str]:
        return sorted({p.ref for p in parts.by_net[net] if IC_RE.match(p.ref) and p.ref not in inductors})

    # An inverting stage: its inductor runs from the switch node to ground, which the
    # switch-node check does not look for (a filter inductor never goes to ground, so it is
    # safe here, and the side with an IC pin and no capacitor is the switch node).
    for ref, pads in sorted(inductors.items()):
        a, b = pads
        for side, other in ((a, b), (b, a)):
            if (other.net and emc._kind(other.net) == "ground" and _supply(side.net)
                    and emc._kind(side.net) == "signal" and ics_on(side.net)
                    and not parts.ground_caps.get(side.net)):
                nodes.setdefault(side.net, f"inductor {ref} to ground")

    def inductor_on(sw: str) -> tuple[str, object, object] | None:
        for ref, pads in sorted(inductors.items()):
            near = next((p for p in pads if p.net == sw), None)
            if near is not None:
                far = next(p for p in pads if p is not near)
                return ref, near, far
        return None

    out: list[Candidate] = []
    done: set[str] = set()
    for sw in sorted(nodes):
        if sw in done:
            continue
        how_sw = nodes[sw]
        ind = inductor_on(sw)
        on_sw = [p for p in parts.by_net[sw] if p.ref not in inductors]
        ics = sorted({p.ref for p in on_sw if IC_RE.match(p.ref)})
        fets = sorted({p.ref for p in on_sw if FET_RE.match(p.ref)})
        if ind is None:
            # Named like a switch node with no inductor on it: on a real board, an MCU's PH0 and
            # PH1 pins. Reported only if the part turns out to be on the input rail.
            for ref in ics:
                out.append(Candidate(ref=ref, kind="unmatched", switch_nets=[sw],
                                     how=[f"{sw} is named like a switch node but has no inductor on it"],
                                     ic_pads=[p for p in parts.by_ref[ref] if _supply(p.net) and p.net != sw]))
            continue
        lref, lnear, lfar = ind
        switch_nets = [sw]
        hint = ""
        if lfar.net and emc._kind(lfar.net) == "ground":
            hint = "inverting"
        elif lfar.net in nodes:
            hint = "buck-boost"
            switch_nets.append(lfar.net)
            done.add(lfar.net)
        # The part that owns the node: an IC on it, else the IC driving its FETs.
        owner = ics[0] if ics else ""
        fet_pads = []
        for q in fets:
            fet_pads += [p for p in parts.by_ref[q] if p.net not in switch_nets and _supply(p.net)]
        if hint == "buck-boost":
            for q in sorted({p.ref for p in parts.by_net[lfar.net] if FET_RE.match(p.ref)}):
                fet_pads += [p for p in parts.by_ref[q] if p.net not in switch_nets and _supply(p.net)]
            if not owner:
                owner = next(iter(ics_on(lfar.net)), "")
        if not owner and fet_pads:
            owner = next((r for p in fet_pads for r in ics_on(p.net)), fets[0])
        if not owner:
            continue
        diode_net = ""
        for p in parts.by_net[sw]:
            if DIODE_RE.match(p.ref) and not emc.LED_HINT.search(f"{p.value} {p.footprint}"):
                other = next((q for q in parts.by_ref[p.ref] if q.net != sw and _supply(q.net)), None)
                if other is not None:
                    diode_net = other.net
                    break
        value = parts.by_ref[owner][0].value if parts.by_ref.get(owner) else ""
        topologies, what = part_number(value)
        how = [f"switch node {sw} ({how_sw})"]
        if hint == "buck-boost":
            how.append(f"inductor {lref} between two switch nodes, {sw} and {lfar.net}")
        elif hint == "inverting":
            how.append(f"inductor {lref} from {sw} to ground")
        if fets and not ics:
            how.append(f"external switches {', '.join(fets)}")
        elif fets:
            how.append(f"external switches {', '.join(fets)} driven by {owner}")
        anchor = next((p for p in parts.by_ref[owner] if p.net == sw), None) or lnear
        out.append(Candidate(
            ref=owner, kind="controller" if fets else "switch node", switch_nets=switch_nets, how=how,
            inductor=lref, inductance_h=parse_henries(lnear.value or ""), far_net=lfar.net, far_pad=lfar,
            fet_pads=fet_pads,
            ic_pads=[p for p in parts.by_ref[owner] if _supply(p.net) and p.net not in switch_nets],
            anchor=anchor, part_number=value, pn_topologies=topologies, pn_what=what, layout_hint=hint,
            diode_net=diode_net, named=how_sw == "its name",
        ))

    # Modules: the inductor is inside, so there is no switch node on the board. Only the part
    # number says what they are.
    seen = {c.ref for c in out}
    for ref, pads in sorted(parts.by_ref.items()):
        if ref in seen or not IC_RE.match(ref) or ref in inductors:
            continue
        topologies, what = part_number(pads[0].value)
        if not topologies:
            continue
        supplies = [p for p in pads if _supply(p.net)]
        if what == "buck module":
            out.append(Candidate(ref=ref, kind="module", switch_nets=[], how=[
                f"part number {pads[0].value}: a {what}, with its inductor inside"],
                ic_pads=supplies, anchor=supplies[0] if supplies else pads[0], part_number=pads[0].value,
                pn_topologies=topologies, pn_what=what))
        else:
            out.append(Candidate(ref=ref, kind="unmatched", switch_nets=[], how=[
                f"part number {pads[0].value} is a {what}, but no switch node with an inductor was found on it"],
                ic_pads=supplies))
    return out


def wanted_nets(cands: list[Candidate], parts) -> set[str]:
    """The nets a regulator draws from, for following the rail through a pass element.

    A buck's own pads include its output (a feedback or output-sense pin), so the inductor's far
    side is left out unless the part number says boost: following the rail onto a buck's output
    would make it look like a boost fed from there.
    """
    out: set[str] = set()
    for c in cands:
        # A module's pads include its output, and nothing on the board says which is which.
        if c.kind in ("unmatched", "module"):
            continue
        for p in c.fet_pads + c.ic_pads:
            if p.net != c.far_net and parts.ground_caps.get(p.net):
                out.add(p.net)
        if c.far_net and "boost" in c.pn_topologies and emc._kind(c.far_net) != "ground":
            out.add(c.far_net)
    return out


@dataclass
class Found:
    """A regulator the input rail feeds: one source in the scan."""

    id: str
    ref: str
    topology: str
    #: "layout", "part number" or "assumed".
    topology_from: str
    how: str
    confidence: str
    switch_net: str
    input_pad: object
    input_net: str
    output_net: str = ""
    inductor: str = ""
    inductance_h: float | None = None
    duty_from_rails: float | None = None
    v_in: float | None = None
    #: Several sources of one part share its clock.
    group: str = ""


def _volts(net: str) -> float | None:
    v = tlines.named_volts(net)
    if v is None:
        return None
    return -v if negative(net) else v


def makes(c: Candidate, entry_nets: set[str]) -> bool:
    """The candidate's inductor runs to the rail and the part draws from another, higher power
    input: a buck making the rail, not a boost fed from it."""
    if c.layout_hint or "boost" in c.pn_topologies or not c.far_net:
        return False
    v = tlines.named_volts(c.far_net) or 0
    return any(p.net in entry_nets and p.net != c.far_net and (tlines.named_volts(p.net) or 0) > v
               for p in c.ic_pads)


def attach(cands: list[Candidate], rail: set[str], entry_net: str, other_entries: set[str],
           parts) -> tuple[list[Found], list[tuple[str, str]]]:
    """The candidates the rail feeds, each with its topology, input pin and duty from the rails."""
    found: list[Found] = []
    skipped: list[tuple[str, str]] = []

    def skip(ref: str, why: str) -> None:
        if all(r != ref for r, _ in skipped):
            skipped.append((ref, why))

    for c in cands:
        on_rail_ic = [p for p in c.ic_pads if p.net in rail and p.net != c.far_net]
        if c.kind == "unmatched":
            if on_rail_ic:
                skip(c.ref, c.how[0])
            continue
        how = list(c.how)
        topo, topo_from = "", "layout"
        pad = None
        if c.layout_hint:
            topo = c.layout_hint
        elif c.far_net and c.far_net in rail:
            # The inductor runs from the rail to the switch node: a boost fed from the rail, or
            # a buck making it. A buck making the rail draws from another power input.
            if makes(c, other_entries):
                skip(c.ref, f"{c.switch_nets[0]} reaches {c.far_net} through {c.inductor}, and {c.ref} also "
                            f"draws from another power input, so {c.far_net} is its output, not its input")
                continue
            topo = "boost"
            pad = c.far_pad
            how.append(f"inductor {c.inductor} from the input rail ({c.far_net}) to the switch node")
        elif c.kind == "module":
            topo, topo_from = c.pn_topologies[0], "part number"
        else:
            topo = "buck"
        if pad is None:
            fets = [p for p in c.fet_pads if p.net in rail]
            pool = fets or on_rail_ic
            if not pool:
                skip(c.ref, f"not fed from {entry_net}: its input ripple reaches the power input only "
                            f"through another regulator, which is not modelled")
                continue
            anchor = c.anchor or pool[0]
            pad = min(pool, key=lambda p: math.dist((p.x, p.y), (anchor.x, anchor.y)))
            if fets:
                how.append(f"high-side switch {pad.ref}'s drain on the input rail ({pad.net})")
            if topo == "buck" and c.far_net:
                how.append(f"inductor {c.inductor} from the switch node to {c.far_net}"
                           + (" with a capacitor to ground" if parts.ground_caps.get(c.far_net) else ""))

        # The part number: a second cue, or a disagreement to show.
        cues = 1 + (1 if c.named else 0)
        if c.pn_topologies:
            if topo in c.pn_topologies:
                if topo_from != "part number":
                    how.append(f"part number {c.part_number} ({c.pn_what})")
                    cues += 1
            elif topo_from == "layout" and topo != "buck-boost":
                how.append(f"part number {c.part_number} suggests a {c.pn_what}, which the layout does not match")
                cues = 0
        if topo == "buck-boost":
            confidence = "low" if cues == 0 else "medium"
        else:
            confidence = "low" if cues == 0 else "high" if cues >= 2 else "medium"

        v_in = _volts(pad.net) or _volts(entry_net)
        output = ""
        if topo == "buck":
            output = c.far_net
            if c.kind == "module":
                outs = sorted({p.net for p in c.ic_pads if p.net not in rail and parts.ground_caps.get(p.net)
                               and (_volts(p.net) or 0) > 0})
                output = outs[0] if outs else ""
        elif topo == "boost":
            highs = sorted({p.net for p in c.ic_pads if (_volts(p.net) or 0) > (v_in or 0) and p.net not in rail})
            output = c.diode_net or (highs[0] if highs else "")
        elif topo == "inverting":
            negs = sorted({p.net for p in c.ic_pads if negative(p.net)})
            output = c.diode_net or (negs[0] if negs else "")
        duty = None
        v_out = _volts(output) if output else None
        if v_in and v_out is not None and v_in > 0:
            if topo == "buck" and 0 < v_out < v_in:
                duty = v_out / v_in
            elif topo == "boost" and v_out > v_in:
                duty = 1 - v_in / v_out
            elif topo == "inverting" and v_out < 0:
                duty = -v_out / (-v_out + v_in)
            elif topo == "buck-boost" and v_out > 0:
                duty = v_out / v_in if v_out < v_in else 1 - v_in / v_out
        found.append(Found(
            id=c.ref, ref=c.ref, topology=topo, topology_from=topo_from, how="; ".join(how),
            confidence=confidence, switch_net=c.switch_nets[0] if c.switch_nets else "", input_pad=pad,
            input_net=pad.net, output_net=output, inductor=c.inductor, inductance_h=c.inductance_h,
            duty_from_rails=round(duty, 3) if duty is not None else None, v_in=v_in, group=c.ref,
        ))

    # One source per switch node: a part with several gets one id per output.
    counts: dict[str, int] = {}
    for f in found:
        counts[f.ref] = counts.get(f.ref, 0) + 1
    ids: set[str] = set()
    for f in found:
        if counts[f.ref] > 1:
            f.id = f"{f.ref}/{short(f.ref, f.switch_net, f.inductor)}"
            f.how += f"; one of {counts[f.ref]} outputs of {f.ref}, each its own source"
        base, n = f.id, 2
        while f.id in ids:
            f.id, n = f"{base}-{n}", n + 1
        ids.add(f.id)
    return found, skipped
