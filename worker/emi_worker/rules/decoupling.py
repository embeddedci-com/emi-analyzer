"""Decoupling: is every IC supply pin close to a capacitor, and is that capacitor close to ground?

The most common EMI and power-integrity problem on real boards, and the one most often
invisible in a schematic review, because the schematic says a 100 nF capacitor is on the pin
and only the layout says it is 12 mm away.

What matters is the loop: pin -> capacitor -> ground via -> plane -> back to the IC's ground
pin. Every millimetre of that loop is roughly a nanohenry, and a capacitor behind 10 nH of
trace stops decoupling somewhere in the low tens of MHz -- exactly where a microcontroller's
clock harmonics live. So the check measures both halves: pin to capacitor, and capacitor to
its ground via.

Components are recognised by reference designator (U/IC for ICs, C for capacitors), and a
capacitor only counts when one pad is on a supply net and the other on ground. That is a
heuristic; findings quote the parts they matched so a wrong guess is visible.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Iterator

from .model import Finding, RuleContext, classify_net
from .planes import severity

IC_RE = re.compile(r"^(U|IC)\d", re.I)
CAP_RE = re.compile(r"^C\d", re.I)

#: Rough loop inductance per millimetre of pin-to-capacitor distance, counted twice (out and
#: back). An order-of-magnitude figure, used only to put a frequency on "too far" so the
#: finding says why it matters.
NH_PER_MM = 1.0

#: The capacitor's own inductance, when nothing is known about the part. Kept as the fallback
#: for a part the component library cannot resolve — a custom footprint, or a value that is
#: not a capacitance. When the library *does* resolve it, the part's own ESL is used instead
#: and the finding says which package it came from, which is the difference between "roughly
#: 9 nH" and "this 0402 self-resonates at 24 MHz".
MOUNT_NH = 0.5
ASSUMED_CAP_F = 100e-9

_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0}


def cap_farads(value: str) -> float | None:
    """"100nF", "100n", "0.1uF", "4.7µF" -> farads. None when it does not parse."""
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*([pnuµm]?)\s*F?\b", value or "", re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ".")) * _PREFIX[m.group(2).lower()]
    except (KeyError, ValueError):
        return None


def _fmt_mhz(mhz: float) -> str:
    """A frequency a reader can act on.

    "%.0f MHz" reads "0 MHz" for anything under half a megahertz, which is where a bulk
    capacitor behind a long trace actually lands — so the worst findings were the ones the
    formatting destroyed.
    """
    if mhz < 1.0:
        return f"{mhz * 1000:.0f} kHz"
    if mhz < 10.0:
        return f"{mhz:.1f} MHz"
    return f"{mhz:.0f} MHz"


def _useful_up_to_mhz(distance_mm: float, farads: float, part_esl_nh: float | None = None) -> float:
    """Where this capacitor stops decoupling: its own inductance plus the loop to the pin."""
    own = MOUNT_NH if part_esl_nh is None else part_esl_nh
    l_h = (own + 2.0 * distance_mm * NH_PER_MM) * 1e-9
    return 1.0 / (2.0 * math.pi * math.sqrt(l_h * farads)) / 1e6


def _resolve_cap(pad) -> "object | None":
    """The component library's model for this capacitor, or None when it has none.

    Imported here rather than at module scope: the rules package runs on every upload,
    including on workers that never solve, and this keeps the component library out of that
    import path until something actually asks for it.
    """
    from emi_worker.components import match_part, resolve_part

    return resolve_part(match_part(
        getattr(pad, "ref", "") or "",
        getattr(pad, "value", "") or "",
        getattr(pad, "footprint", "") or "",
    ))


def check_decoupling(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled("decoupling"):
        return
    max_d = float(ctx.setting("decoupling", "max_distance_mm") or 3.0)
    crit_d = float(ctx.setting("decoupling", "critical_distance_mm") or 10.0)
    via_d = float(ctx.setting("decoupling", "max_ground_via_mm") or 1.0)

    by_ref: dict[str, list] = defaultdict(list)
    for p in ctx.model.pads:
        if p.ref:
            by_ref[p.ref].append(p)

    # Capacitors that decouple something: one pad on a supply, one on ground.
    caps_by_net: dict[str, list[tuple[str, object, object]]] = defaultdict(list)
    for ref, pads in by_ref.items():
        if not CAP_RE.match(ref) or len(pads) != 2:
            continue
        kinds = [classify_net(p.net) if p.net else "" for p in pads]
        if sorted(kinds) != ["ground", "power"]:
            continue
        supply = pads[kinds.index("power")]
        gnd = pads[kinds.index("ground")]
        caps_by_net[supply.net].append((ref, supply, gnd))

    serving: dict[str, tuple[str, object, object]] = {}

    for ref, pads in sorted(by_ref.items()):
        if not IC_RE.match(ref):
            continue
        pins: dict[str, list] = defaultdict(list)
        for p in pads:
            if p.net and classify_net(p.net) == "power":
                pins[p.net].append(p)

        for net, supply_pins in sorted(pins.items()):
            candidates = caps_by_net.get(net, [])
            worst = None  # (distance, pin, cap)
            for pin in supply_pins:
                if candidates:
                    cap = min(candidates, key=lambda c: math.dist((pin.x, pin.y), (c[1].x, c[1].y)))
                    d = math.dist((pin.x, pin.y), (cap[1].x, cap[1].y))
                    serving[cap[0]] = cap
                else:
                    cap, d = None, math.inf
                if worst is None or d > worst[0]:
                    worst = (d, pin, cap)
            if worst is None:
                continue
            d, pin, cap = worst
            if d <= max_d:
                continue
            x, y = ctx.pt(pin.x, pin.y)

            if cap is None:
                yield Finding(
                    rule="decoupling",
                    severity=severity(ctx, "decoupling", "critical"),
                    title=f"{ref} has no decoupling capacitor on {net}",
                    detail=(
                        f"{ref} draws from {net} (pin {pin.number}), and no capacitor on the "
                        f"board joins {net} to ground. Every current step the IC makes is "
                        f"then supplied through the whole supply trace, which radiates it. "
                        f"Place a capacitor, typically 100 nF, next to the pin."
                    ),
                    net=net, x=x, y=y,
                )
                continue

            farads = cap_farads(getattr(cap[1], "value", "")) or ASSUMED_CAP_F
            assumed = cap_farads(getattr(cap[1], "value", "")) is None

            # §12's payoff: when the library knows this part, the finding stops guessing at
            # its inductance and quotes the part's own self-resonance instead.
            model = _resolve_cap(cap[1])
            part_esl_nh = None
            srf_note = ""
            if model is not None and model.rlc.esl_h is not None:
                part_esl_nh = model.rlc.esl_h * 1e9
                srf = model.rlc.self_resonance_hz()
                if srf is not None:
                    # A generic figure is described as generic and never attributed: it is a
                    # class average, so naming a manufacturer would claim something about it
                    # that is not true. A vendor part cites its datasheet instead.
                    provenance = (
                        "a generic figure for the package, not a specific part"
                        if model.generic
                        else (model.source.describe() if model.source else "from its model")
                    )
                    srf_note = (
                        f" On its own this part self-resonates at {_fmt_mhz(srf / 1e6)} "
                        f"({provenance}), and the loop is what moves that down."
                    )
            useful = _useful_up_to_mhz(d, farads, part_esl_nh)
            total_nh = (part_esl_nh if part_esl_nh is not None else MOUNT_NH) + 2 * d * NH_PER_MM
            yield Finding(
                rule="decoupling",
                severity=severity(ctx, "decoupling", "critical" if d > crit_d else "warning"),
                title=f"{ref}.{pin.number} is {d:.1f} mm from its nearest decoupling capacitor",
                detail=(
                    f"The nearest capacitor on {net} to {ref} pin {pin.number} is {cap[0]}"
                    f"{' (' + cap[1].value + ')' if getattr(cap[1], 'value', '') else ''}, "
                    f"{d:.1f} mm away (budget {max_d:g} mm). The loop through it is roughly "
                    f"{total_nh:.0f} nH, so it stops decoupling somewhere "
                    f"around {_fmt_mhz(useful)}{' assuming 100 nF' if assumed else ''} — above "
                    f"that the IC's switching current returns through the supply trace instead."
                    f"{srf_note} "
                    f"Move the capacitor to the pin, on the same side of the board."
                ),
                net=net, x=x, y=y,
            )

    # The other half of the loop: each capacitor that serves a pin, to its ground via.
    stitches = [
        (v.x, v.y) for v in ctx.model.vias if v.net and classify_net(v.net) == "ground"
    ] + [
        (p.x, p.y) for p in ctx.model.pads
        if p.net and classify_net(p.net) == "ground" and p.is_through
    ]
    if not stitches:
        return
    for cref, (_, _, gnd) in sorted(serving.items()):
        if gnd.is_through:
            continue
        near = min(math.dist((gnd.x, gnd.y), s) for s in stitches)
        if near <= via_d:
            continue
        x, y = ctx.pt(gnd.x, gnd.y)
        yield Finding(
            rule="decoupling",
            severity=severity(ctx, "decoupling", "warning"),
            title=f"{cref}'s ground pad has no via within {via_d:g} mm",
            detail=(
                f"{cref} decouples an IC supply, but the nearest ground via to its ground pad "
                f"is {near:.1f} mm away. That trace is part of the decoupling loop just as much "
                f"as the supply side is. Put a via at the pad, into the ground plane."
            ),
            net=gnd.net, x=x, y=y,
        )
