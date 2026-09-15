"""Choosing a model for a part (§12).

The order is fixed and first match wins:

  1. MPN — phase 2, once ingest keeps the part-number fields
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
from dataclasses import dataclass
from pathlib import Path

from emi_worker.components.document import Component, ComponentError, Resolved
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
    if not part.modellable:
        return None

    ordered = list(candidates or []) + list(built_in())
    for cand in ordered:
        c = cand.component
        if c.kind != "capacitor" or not _matches(c, part):
            continue
        rlc = c.resolve_for(part.farads, part.package.imperial)
        if rlc is None:
            continue
        gaps: list[str] = []
        if rlc.esl_h is None:
            gaps.append("no ESL, so there is no self-resonance to report")
        if rlc.esr_ohm is None:
            gaps.append("no ESR, so the impedance at resonance is unknown")
        return Resolved(
            ref=part.ref, component_id=c.id, component_name=c.name, rlc=rlc,
            gaps=gaps, source=c.cites("ESL"), generic=c.is_generic,
        )
    return None


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
