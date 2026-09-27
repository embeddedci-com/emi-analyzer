"""Choosing a model for a part (§12).

The order is fixed and first match wins:

  1. part number: a footprint field (MPN, LCSC and the like) naming a component's MPN, one
     of its aliases or its LCSC number, searched in the order of 2 to 5
  2. the user's own component
  3. a component shared with their organisation
  4. an unsaved component from the browser tab
  5. the built-in generic family
  6. otherwise bare copper, listed by reference

The interesting property is 6. **A part with no match changes nothing**: no element is placed,
the solve is bit-for-bit what it would have been, and the reference is reported. That is what
makes the library safe to grow — adding an entry can only ever add detail, never silently
move a result that was already reported.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path

from emi_worker.components.document import (
    Component,
    ComponentError,
    Resolved,
    normalise_part_number,
)
from emi_worker.components.document import parse as parse_component
from emi_worker.components.match import PartMatch

LIBRARY_DIR = Path(__file__).resolve().parent / "library"

#: Candidate tiers, best first. The names are what a result reports as the model's owner.
TIER_MPN = "mpn"
TIER_MINE = "mine"
TIER_SHARED = "shared"
TIER_UNSAVED = "unsaved"
TIER_BUILTIN = "built-in"


@dataclass(frozen=True)
class Candidate:
    """A component offered to the resolver, and which tier it came from."""

    component: Component
    tier: str


@functools.lru_cache(maxsize=1)
def built_in() -> tuple[Candidate, ...]:
    """Every built-in component, vendor parts before generic ones.

    Vendor entries win because they describe a part somebody actually buys, while a generic
    entry is a class average that exists so a board with no library behind it still lands in
    the right decade.
    """
    vendor: list[Candidate] = []
    generic: list[Candidate] = []
    families: list[Candidate] = []
    for path in sorted(LIBRARY_DIR.glob("*.json")):
        doc = json.loads(path.read_text())
        if doc.get("format") != "emi-component-library":
            raise ComponentError(f"{path.name} is not a component library")
        for raw in doc.get("components", []):
            c = parse_component(raw)
            if not c.is_generic:
                vendor.append(Candidate(c, TIER_BUILTIN))
            elif c.model_type == "mlcc_family":
                families.append(Candidate(c, TIER_BUILTIN))
            else:
                generic.append(Candidate(c, TIER_BUILTIN))
    # Most specific first: a named part, then a generic entry for this exact value and
    # package, then the family that answers for anything in that package.
    return tuple(vendor + generic + families)


def _matches(c: Component, part: PartMatch) -> bool:
    """Does this component describe this part?

    Compared on farads rather than on the value string: "100n", "100nF" and "0.1uF" are the
    same capacitor, and a string comparison would model one and miss the other two.
    """
    m = c.match or {}
    if c.part_numbers():
        # A component that names its part number is that part, and answers only when the
        # board names it too. A 100 nF 0402 on a board is not evidence that it is this
        # 100 nF 0402: two of JLCPCB's Basic parts alone share that value and package.
        return False
    if c.model_type == "mlcc_family":
        # A family answers for any value in a package it knows. It carries no match rules of
        # its own precisely because enumerating them is what it exists to avoid.
        return part.package is not None and c.mlcc_family().esl_for(part.package.imperial) is not None
    want_package = m.get("package")
    if want_package and (part.package is None or part.package.imperial != want_package):
        return False
    want_value = m.get("value")
    if want_value:
        from emi_worker.rules.decoupling import cap_farads

        want_farads = cap_farads(want_value)
        if want_farads is None or part.farads is None:
            return False
        # Exact rather than near: 100 nF and 120 nF are different parts, and a tolerance here
        # would quietly model one as the other.
        if abs(part.farads - want_farads) > want_farads * 1e-6:
            return False
    return True


def resolve_part(part: PartMatch, candidates: list[Candidate] | None = None) -> Resolved | None:
    """The model for one part, or None when nothing matches and it stays bare copper.

    ``candidates`` are the user's own, shared and unsaved components, already in precedence
    order — the server knows who owns what and this does not need to. Built-ins are appended,
    so a user's component always beats the library.
    """
    ordered = list(candidates or []) + list(built_in())

    # Tier 1. A part number is tried first and needs neither a readable value nor a standard
    # footprint: an LCSC number on a custom footprint still says exactly which part it is.
    wanted = [(f, v, normalise_part_number(v)) for f, v in part.part_numbers]
    if wanted:
        for cand in ordered:
            c = cand.component
            numbers = c.part_numbers()
            if c.kind != "capacitor" or not numbers:
                continue
            hit = next(((f, v) for f, v, n in wanted if n in numbers), None)
            if hit is None:
                continue
            rlc = _rlc_for(c, part)
            if rlc is None:
                continue
            return _resolved(c, part, rlc, "part number", f"{hit[0]} {hit[1]}",
                             _disagreements(c, part))

    if not part.modellable:
        return None
    for cand in ordered:
        c = cand.component
        if c.kind != "capacitor" or not _matches(c, part):
            continue
        rlc = c.resolve_for(part.farads, part.package.imperial)
        if rlc is None:
            continue
        how = "package" if c.model_type == "mlcc_family" else "value and package"
        return _resolved(c, part, rlc, how, "", [])
    return None


def _rlc_for(c: Component, part: PartMatch):
    if c.model_type == "series_rlc":
        return c.series_rlc()
    if not part.modellable:
        return None
    return c.resolve_for(part.farads, part.package.imperial)


def _disagreements(c: Component, part: PartMatch) -> list[str]:
    """What the board says that the part number contradicts. The part number wins, because it
    is what gets ordered, but a board whose value field says otherwise deserves a note."""
    from emi_worker.rules.decoupling import cap_farads

    notes = []
    want = cap_farads(str((c.match or {}).get("value") or ""))
    if want is not None and part.farads is not None and abs(part.farads - want) > want * 1e-6:
        notes.append(f"the board's value {part.value!r} disagrees with {c.mpn or c.name}; "
                     f"modelled as the part number says")
    pkg = (c.match or {}).get("package")
    if pkg and part.package is not None and part.package.imperial != pkg:
        notes.append(f"the footprint is {part.package.imperial} but {c.mpn or c.name} is {pkg}; "
                     f"modelled as the part number says")
    return notes


_DIELECTRIC = re.compile(r"\b(C0G|NP0|X5R|X6S|X7R|X7S|X7T|X8R|Y5V|Z5U)\b", re.I)


def basis_label(c: Component, part: PartMatch) -> str:
    """One short label for where a model came from: "datasheet (Samsung CL05B104KO5NNNC)"
    for a named part, "generic 0402 X7R" for a class average. The generic label never names
    a manufacturer, because a class average describes none."""
    if c.is_generic:
        bits = ["generic"]
        if part.package is not None:
            bits.append(part.package.imperial)
        d = _DIELECTRIC.search(part.value or "")
        if d:
            bits.append(d.group(1).upper().replace("NP0", "C0G"))
        return " ".join(bits)
    named = f"{c.manufacturer} {c.mpn}".strip() if c.mpn else c.name
    if c.provenance == "vendor":
        return f"datasheet ({named})"
    if c.provenance == "measured":
        return f"measured ({named})"
    return f"your library ({named})"


def _resolved(c: Component, part: PartMatch, rlc, matched_by: str, matched_on: str,
              notes: list[str]) -> Resolved:
    gaps: list[str] = []
    if rlc.esl_h is None:
        gaps.append("no ESL, so there is no self-resonance to report")
    if rlc.esr_ohm is None:
        gaps.append("no ESR, so the impedance at resonance is unknown")
    return Resolved(
        ref=part.ref, component_id=c.id, component_name=c.name, rlc=rlc,
        gaps=gaps, source=c.cites("ESL"), generic=c.is_generic,
        matched_by=matched_by, matched_on=matched_on, basis=basis_label(c, part),
        notes=notes,
    )


@dataclass
class Coverage:
    """What a board's capacitors resolved to, for the modelled-parts list (§12)."""

    modelled: list[Resolved]
    unmatched: list[PartMatch]

    @property
    def total(self) -> int:
        return len(self.modelled) + len(self.unmatched)

    @property
    def fraction(self) -> float:
        return len(self.modelled) / self.total if self.total else 0.0

    def summary(self) -> str:
        if not self.total:
            return "no two-pad capacitors found"
        if not self.unmatched:
            return f"all {self.total} capacitors modelled"
        return (
            f"{len(self.modelled)} of {self.total} capacitors modelled; "
            f"{len(self.unmatched)} left as bare copper"
        )


def resolve_all(parts: list[PartMatch],
                candidates: list[Candidate] | None = None) -> Coverage:
    modelled: list[Resolved] = []
    unmatched: list[PartMatch] = []
    for part in parts:
        got = resolve_part(part, candidates)
        (modelled.append(got) if got is not None else unmatched.append(part))
    return Coverage(modelled=modelled, unmatched=unmatched)
