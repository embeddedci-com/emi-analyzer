"""Choosing a model for a part (§12).

The property that matters most is the last one: a part with no match changes nothing. That is
what makes the library safe to grow — adding an entry can only add detail, never silently move
a result that has already been reported.
"""

from __future__ import annotations

import pytest

from emi_worker.components import match_part, resolve_all, resolve_part
from emi_worker.components.document import parse as parse_component
from emi_worker.components.resolve import Candidate, TIER_MINE, built_in

C0402 = "Capacitor_SMD:C_0402_1005Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"


def user_component(**over):
    base = {
        "format": "emi-component", "version": 1,
        "id": "mine-100n-0402", "kind": "capacitor", "name": "My 100 nF 0402",
        "provenance": "vendor",
        "match": {"value": "100n", "package": "0402"},
        "model": {"type": "series_rlc", "c_f": 1e-7, "esl_h": 3.0e-10, "esr_ohm": 0.04},
        "sources": [{"doc": "part datasheet", "rev": "C", "what": "ESL and ESR"}],
    }
    base.update(over)
    return Candidate(parse_component(base), TIER_MINE)


def test_the_built_in_family_models_a_common_part():
    got = resolve_part(match_part("C1", "100nF", C0402))
    assert got is not None
    assert got.component_id == "generic-mlcc-100n-0402"
    assert got.placeable
    assert got.rlc.self_resonance_hz() == pytest.approx(23.7e6, rel=0.02)


def test_the_value_is_compared_in_farads_not_as_text():
    """"100n", "100nF" and "0.1uF" are the same capacitor. A string comparison would model
    one of them and leave the other two as bare copper."""
    ids = {resolve_part(match_part("C1", v, C0402)).component_id
           for v in ("100n", "100nF", "0.1uF", "0.1µF")}
    assert ids == {"generic-mlcc-100n-0402"}


def test_a_nearby_value_is_never_substituted():
    """100 nF and 120 nF are different parts, so the 100 nF entry must not answer for the
    120 nF one. The family does answer — and computes from the part's OWN capacitance, which
    is the distinction that matters: a substituted model would report 100 nF's resonance."""
    got = resolve_part(match_part("C1", "120nF", C0402))
    assert got.component_id == "generic-mlcc-family"
    assert got.rlc.c_f == pytest.approx(1.2e-7)
    hundred = resolve_part(match_part("C2", "100nF", C0402))
    assert got.rlc.self_resonance_hz() != pytest.approx(hundred.rlc.self_resonance_hz())


def test_a_users_own_component_beats_the_built_in_family():
    got = resolve_part(match_part("C1", "100nF", C0402), [user_component()])
    assert got.component_id == "mine-100n-0402"
    # And it is a different part, with its own self-resonance.
    assert got.rlc.esl_h == pytest.approx(3.0e-10)


def test_candidate_order_is_the_precedence():
    """The server knows who owns what; the resolver just takes the list in order."""
    first = user_component(id="first")
    second = user_component(id="second")
    assert resolve_part(match_part("C1", "100nF", C0402), [first, second]).component_id == "first"


def test_the_package_has_to_match_too():
    """A 100 nF 0402 and a 100 nF 0805 have different inductance, so the 0402 entry must not
    answer for the 0805 part."""
    got = resolve_part(match_part("C1", "100nF", C0805))
    assert got.component_id == "generic-mlcc-100n-0805"
    assert got.rlc.esl_h > resolve_part(match_part("C2", "100nF", C0402)).rlc.esl_h


def test_an_unmatched_part_resolves_to_nothing_at_all():
    """Not a placeholder, not a default: None, so the caller places no element and the solve
    is bit-for-bit what it would have been.

    What is left unmatched now that a family answers for any known package: a part whose
    footprint is not a package this build reads, and one whose value is not a capacitance.
    Both are real cases on the boards — a custom LCSC footprint, and a part number typed into
    the Value field.
    """
    assert resolve_part(match_part("C1", "100nF", "lcsc:CAP-SMD_L4.5-W3.2_STC3MD20-T1")) is None
    assert resolve_part(match_part("C2", "STC3MA06-T1", C0402)) is None


def test_a_package_the_family_does_not_know_stays_bare_copper():
    """1812 is a real imperial package the matcher reads, and one the family has no ESL for.
    Answering anyway would mean inventing an inductance from nothing."""
    assert resolve_part(match_part("C1", "100nF", "C_1812_4532Metric")) is None


def test_a_model_with_missing_numbers_reports_gaps_instead_of_guessing():
    incomplete = user_component(
        id="no-esl",
        model={"type": "series_rlc", "c_f": 1e-7, "esl_h": None, "esr_ohm": None},
        provenance="user", sources=[])
    got = resolve_part(match_part("C1", "100nF", C0402), [incomplete])
    assert got.component_id == "no-esl"
    assert not got.placeable
    assert any("no ESL" in g for g in got.gaps)
    assert got.rlc.self_resonance_hz() is None


# ---- the built-in library ---------------------------------------------------------------

def test_the_library_is_ordered_most_specific_first():
    """A named part, then a generic entry for this exact value and package, then the family
    that answers for anything in that package. Any other order lets a class average win over
    something that describes the actual part."""
    order = []
    for cand in built_in():
        c = cand.component
        order.append(0 if not c.is_generic else (1 if c.model_type != "mlcc_family" else 2))
    assert order == sorted(order)


def test_the_family_is_only_reached_when_nothing_more_specific_matches():
    assert resolve_part(match_part("C1", "100nF", C0402)).component_id == "generic-mlcc-100n-0402"
    assert resolve_part(match_part("C2", "47nF", C0402)).component_id == "generic-mlcc-family"


def test_every_built_in_generic_entry_is_labelled_generic():
    """They must never read as a datasheet number, because they are not one."""
    for cand in built_in():
        if cand.component.is_generic:
            described = cand.component.describe_provenance()
            assert "generic" in described
            assert "not a specific part" in described


# ---- coverage --------------------------------------------------------------------------

def test_coverage_counts_both_sides():
    parts = [match_part("C1", "100nF", C0402),
             match_part("C2", "100nF", "lcsc:CAP-SMD_L4.5-W3.2_STC3MD20-T1")]
    cov = resolve_all(parts)
    assert cov.total == 2
    assert len(cov.modelled) == 1 and len(cov.unmatched) == 1
    assert cov.fraction == pytest.approx(0.5)
    assert "1 of 2 capacitors modelled" in cov.summary()
    assert "bare copper" in cov.summary()


def test_coverage_of_nothing_says_so_rather_than_dividing_by_zero():
    cov = resolve_all([])
    assert cov.total == 0 and cov.fraction == 0.0
    assert "no two-pad capacitors" in cov.summary()


# ---- against the real boards -----------------------------------------------------------

def test_coverage_on_the_real_boards():
    """Measured when this runs: 425 of 429 capacitors modelled, 99 %.

    The four that are not are the three BD10 electrolytics and one STC3MD20, both on custom
    LCSC footprints — precisely the cases §12 leaves out of this phase. Enumerated
    value-and-package entries alone reached 46 %; the family entry closed the rest, because
    what was missing were common values nobody had typed in rather than anything unusual.
    """
    import os
    from pathlib import Path

    boards = Path(os.environ.get("EMI_TEST_BOARDS", "/nonexistent"))
    if not boards.is_dir():
        pytest.skip("EMI_TEST_BOARDS is not set to a directory of real boards")

    from emi_worker.kicad import parse, parse_board
    from emi_worker.rules.decoupling import CAP_RE

    parts = []
    for path in sorted(boards.glob("*/*.kicad_pcb")):
        if path.name.startswith("_autosave") or "backup" in path.name.lower():
            continue
        board = parse_board(parse(path.read_text()))
        by_ref: dict[str, list] = {}
        for pad in board.pads:
            if pad.ref:
                by_ref.setdefault(pad.ref, []).append(pad)
        parts += [match_part(ref, pads[0].value, pads[0].footprint)
                  for ref, pads in by_ref.items()
                  if CAP_RE.match(ref) and len(pads) == 2]

    cov = resolve_all(parts)
    assert cov.total > 400
    assert cov.fraction > 0.98, cov.summary()
    # Everything left over must be a footprint this build does not read, not a value gap.
    for part in cov.unmatched:
        assert part.package is None or part.farads is None, (
            f"{part.ref} has a known package and a readable value but did not resolve")


# ---- K5: the decoupling finding quotes the SRF and where it came from -------------------

def test_k5_a_finding_quotes_the_self_resonance_and_its_provenance():
    """§19's K5. The rule runs on every upload, so this is the tier that pays off without a
    solve at all — and the number has to arrive with a statement of what kind of number it
    is, or it reads as a measurement of the part on the board."""
    from emi_worker.rules.decoupling import _fmt_mhz

    got = resolve_part(match_part("C12", "100nF", C0402))
    assert got.rlc.self_resonance_hz() is not None
    assert got.generic is True
    assert got.source is not None
    assert _fmt_mhz(got.rlc.self_resonance_hz() / 1e6) == "24 MHz"


def test_a_resolved_vendor_part_is_not_marked_generic():
    got = resolve_part(match_part("C1", "100nF", C0402), [user_component()])
    assert got.generic is False
    assert "datasheet" in got.source.describe()
