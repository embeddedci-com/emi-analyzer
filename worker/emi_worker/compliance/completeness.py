"""§17.3: no margin until every input holds.

The failure this prevents is specific. A board with three connectors, one of which nobody has
declared a cable for, will produce a perfectly reasonable-looking margin from the other two —
and that margin is not wrong so much as *about a different product*. The same goes for a
harmonic with no rise time declared: an undriven harmonic is a gap in the inputs, not a zero in
the answer, and the arithmetic cannot tell the difference.

So the gate is a hard one. An incomplete result carries no margin and no confidence fields at
all, rather than carrying them with a warning attached — because a warning is something a
reader can skip and a missing field is not, and because an export or an API client will happily
print a number next to a caveat it never read.

Each item says what to do about it, in the user's terms, and the list doubles as the to-do list
the view shows in place of the number.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Gap:
    #: Stable key, so the UI can link to the tab that fixes it.
    key: str
    #: What is missing, and what to do — one sentence, written for the user.
    message: str
    #: Which tab fixes it: "drivers", "cables", "solve", "product".
    fixed_on: str


@dataclass
class Completeness:
    gaps: list[Gap] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.gaps

    def add(self, key: str, message: str, fixed_on: str) -> None:
        self.gaps.append(Gap(key=key, message=message, fixed_on=fixed_on))


def check(
    *,
    drivers: list,
    source_nets: list,
    connectors: list,
    cable_assignments: dict,
    driven_ports: list,
    far_field_refs: list,
    solved_f_max_hz: float,
    required_f_max_hz: float,
    undriven_harmonics: dict,
    enclosure: str,
    power: str,
    power_entry_found: bool,
) -> Completeness:
    """Every item §17.3 lists, in the order a user would fix them."""
    c = Completeness()

    if not drivers:
        c.add("no-driver",
              "No driver is attached, so the result is relative: it says which layout is "
              "quieter, not how many µV/m either radiates. Add one on the Drivers tab.",
              "drivers")
    else:
        named = {d for d in source_nets}
        covered = {getattr(d, "net", None) or (d.get("net") if isinstance(d, dict) else None)
                   for d in drivers}
        for net in sorted(named - {n for n in covered if n}):
            c.add(f"driver-net:{net}",
                  f"{net} is marked as a source but has no driver, so whatever it radiates is "
                  f"missing from this estimate rather than counted as zero.",
                  "drivers")

    for ref in sorted(connectors):
        if ref not in cable_assignments:
            c.add(f"cable:{ref}",
                  f"{ref} has no cable declared. An undeclared connector is not modelled at "
                  f"all — say which cable it carries, or that it carries none.",
                  "cables")

    for port in driven_ports:
        if port not in far_field_refs:
            c.add(f"far-field:{port}",
                  f"The solve covering {port} recorded no far-field box, so the board's own "
                  f"radiation is missing. Re-run it with the far field turned on.",
                  "solve")

    if solved_f_max_hz > 0 and solved_f_max_hz < required_f_max_hz:
        c.add("band",
              f"The solve reaches {solved_f_max_hz / 1e6:.0f} MHz and the scan runs to "
              f"{required_f_max_hz / 1e6:.0f} MHz for this product's highest clock. The band "
              f"above that is not covered.",
              "solve")

    for f, why in sorted(undriven_harmonics.items()):
        c.add(f"harmonic:{f:.0f}",
              f"Nothing is declared for {f / 1e6:g} MHz ({why}). An undriven harmonic is a gap, "
              f"not a zero.",
              "drivers")

    if enclosure == "metal":
        c.add("metal-enclosure",
              "A metal enclosure is not modelled. It usually helps a great deal, so a radiated "
              "estimate without it would be pessimistic by an unknown amount rather than "
              "conservative by a known one.",
              "product")

    if not power:
        c.add("power",
              "The power declaration is missing, and it decides whether a conducted scan "
              "applies at all.",
              "product")
    elif power != "dc" and not power_entry_found:
        c.add("power-entry",
              "The power-entry path was not recognised on this board, so the conducted "
              "estimate has nowhere to measure.",
              "product")

    return c
