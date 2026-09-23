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

**Every input that could only remove a gap is derived, not taken on trust.** The first version
took them from the request: ``solved_f_max_hz=0`` skipped the band check, ``power_entry_found``
defaulted to true, and leaving ``driven_ports`` or ``connectors`` out removed those gaps -- so an
API client could get ``complete: true`` and a margin on numbers of its choosing. Now the band,
the excited ports, the far field, the connectors, the cables the solve modelled and the power
entry all come from the solve's artifacts and the board itself. What the user may still say
is what only they know -- which connectors carry no cable, which nets are sources, the
enclosure, the power -- and each of those can only *add* a requirement or record a decision
the result names.
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
    modelled_cables: dict,
    excited_ports: list,
    far_field_ports: list,
    covered: dict,
    required_band: tuple[float, float] | None,
    undriven_harmonics: dict,
    enclosure: str,
    power: str,
    power_entry_found: bool,
    problems: list = (),
    has_solve: bool = True,
) -> Completeness:
    """Every item §17.3 lists, in the order a user would fix them.

    ``drivers`` are the attached drivers (anything with a ``net``); ``source_nets`` the nets the
    user says are sources. ``connectors`` are the board's, found at ingest time from the upload.
    ``cable_assignments`` is the user's declaration per connector, where a type of ``None`` or
    ``"none"`` means "carries no cable"; ``modelled_cables`` is what the solve actually carries
    antenna terms for. ``excited_ports`` and ``far_field_ports`` come from the solve;
    ``covered`` maps each path to the band its transfer function covers, and ``required_band``
    is the scan's (``None`` when the highest frequency in the product is not known).
    ``problems`` are (key, message) pairs about the artifacts themselves.
    """
    c = Completeness()

    if not has_solve:
        c.add("solve",
              "No finished solve was given, so nothing radiating is modelled. Pick a finished "
              "solve on the Results tab.",
              "solve")

    for key, message in problems:
        c.add(key, message, "solve")

    if not drivers:
        c.add("no-driver",
              "No driver is attached, so the result is relative: it says which layout is "
              "quieter, not how many µV/m either radiates. Add one on the Drivers tab.",
              "drivers")
    else:
        named = {d for d in source_nets}
        covered_nets = {getattr(d, "net", None)
                        or (d.get("net") if isinstance(d, dict) else None) for d in drivers}
        for net in sorted(named - {n for n in covered_nets if n}):
            c.add(f"driver-net:{net}",
                  f"{net} is marked as a source but has no driver, so whatever it radiates is "
                  f"missing from this estimate rather than counted as zero.",
                  "drivers")

    if has_solve and len(excited_ports) > 1:
        c.add("sources",
              f"The solve excites {len(excited_ports)} ports at once. One driver can only "
              f"replace one source, so solve each source on its own.",
              "solve")

    for ref in sorted(connectors):
        spec = cable_assignments.get(ref)
        declared = None if spec is None else (spec.get("type") if isinstance(spec, dict)
                                              else spec)
        declared_none = spec is not None and declared in (None, "", "none")
        modelled = modelled_cables.get(ref)
        if declared_none:
            continue
        if modelled is None:
            if spec is None:
                c.add(f"cable:{ref}",
                      f"{ref} has no cable declared. An undeclared connector is not modelled "
                      f"at all. Say which cable it carries, or that it carries none.",
                      "cables")
            else:
                c.add(f"cable-solve:{ref}",
                      f"{ref} is declared as {declared}, but the solve did not model its "
                      f"cable. Run the solve again with cable emissions on.",
                      "solve")
            continue
        if declared and declared != modelled.get("cable_id"):
            c.add(f"cable-mismatch:{ref}",
                  f"{ref} is declared as {declared}, but the solve modelled "
                  f"{modelled.get('cable_id')}. Run the solve again with the cable you mean.",
                  "solve")
            continue
        length = spec.get("length_m") if isinstance(spec, dict) else None
        if length and modelled.get("length_m") and \
                abs(float(length) - float(modelled["length_m"])) > 1e-6:
            c.add(f"cable-mismatch:{ref}",
                  f"{ref} is declared {float(length):g} m long, but the solve modelled "
                  f"{float(modelled['length_m']):g} m. Run the solve again with that length.",
                  "solve")

    for port in excited_ports:
        if port not in far_field_ports:
            c.add(f"far-field:{port}",
                  f"The solve covering {port} recorded no far-field box, so the board's own "
                  f"radiation is missing. Run it again with the far field turned on.",
                  "solve")

    if required_band is None:
        if has_solve:
            c.add("highest-frequency",
                  "The product's highest frequency is not known, so neither is how far up "
                  "the scan runs. Attach a clock driver, or state the highest frequency.",
                  "product")
    else:
        lo, hi = required_band
        for label, (c_lo, c_hi) in sorted(covered.items()):
            # A part per million of slack: both edges are computed from the same numbers.
            if c_lo > lo * (1 + 1e-6) or c_hi < hi * (1 - 1e-6):
                c.add(f"band:{label}",
                      f"{label} is solved from {c_lo / 1e6:.0f} to {c_hi / 1e6:.0f} MHz and the "
                      f"scan runs from {lo / 1e6:.0f} to {hi / 1e6:.0f} MHz for this product. "
                      f"The rest of the scan is not covered.",
                      "solve")

    lo_hi = required_band or (0.0, float("inf"))
    for f, why in sorted(undriven_harmonics.items()):
        if not lo_hi[0] <= f <= lo_hi[1]:
            continue
        c.add(f"harmonic:{f:.0f}",
              f"Nothing is known at {f / 1e6:g} MHz ({why}). An undriven harmonic is a gap, "
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
