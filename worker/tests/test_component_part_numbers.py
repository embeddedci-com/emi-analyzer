"""Matching a capacitor by the part number its footprint carries.

tests/fixtures/part_numbers.kicad_pcb is hand-written: six 2-pad capacitors, each carrying a
different kind of part-number field, or a field that must be ignored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emi_worker.components import match_part, resolve_part
from emi_worker.components.document import parse as parse_component
from emi_worker.components.resolve import Candidate, TIER_MINE
from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.board import part_number_field

FIXTURE = Path(__file__).parent / "fixtures" / "part_numbers.kicad_pcb"
C0402 = "Capacitor_SMD:C_0402_1005Metric"


@pytest.fixture(scope="module")
def parts():
    model = parse_board(parse(FIXTURE.read_text()))
    by_ref = {}
    for pad in model.pads:
        by_ref.setdefault(pad.ref, pad)
    return {ref: resolve_part(match_part(ref, p.value, p.footprint, p.part_numbers))
            for ref, p in by_ref.items()}, by_ref


def test_ingest_keeps_the_part_number_fields_as_written(parts):
    _, pads = parts
    assert pads["C1"].part_numbers == {"LCSC": "C1525"}
    assert pads["C2"].part_numbers == {"mpn": "cl05b104kb54pnc"}
    assert pads["C3"].part_numbers == {"Manufacturer  Part Number": "GRM21BR61H106KE43#"}
    # A "~" placeholder names nothing, and "Datasheet" is not a part-number field even when
    # it holds one.
    assert pads["C6"].part_numbers == {}


@pytest.mark.parametrize("name", ["MPN", "mpn", "Manufacturer Part Number", "LCSC",
                                  "LCSC Part", "JLCPCB Part #", "Part Number", "lcsc  part"])
def test_the_field_names_are_matched_case_insensitively(name):
    assert part_number_field(name)


@pytest.mark.parametrize("name", ["Value", "Datasheet", "Description", "Manufacturer"])
def test_other_fields_are_not_part_numbers(name):
    assert not part_number_field(name)


def test_an_lcsc_number_selects_the_named_part(parts):
    got, _ = parts
    c1 = got["C1"]
    assert c1.component_id == "samsung-cl05b104ko5nnnc"
    assert c1.matched_by == "part number" and c1.matched_on == "LCSC C1525"
    assert c1.basis == "datasheet (Samsung CL05B104KO5NNNC)"
    assert not c1.generic and not c1.notes


def test_two_parts_with_the_same_value_and_package_stay_apart(parts):
    """C1 and C2 are both 100 nF 0402. Their part numbers say they are different parts
    (16 V and 50 V), and each gets its own model."""
    got, _ = parts
    assert got["C2"].component_id == "samsung-cl05b104kb54pnc"
    assert got["C2"].rlc.esl_h != got["C1"].rlc.esl_h


def test_a_part_number_works_on_a_custom_footprint(parts):
    """The footprint name gives no package, so value and package could never match it. The
    part number says exactly what it is, and the alias with Murata's '#' is accepted."""
    got, _ = parts
    assert got["C3"].component_id == "murata-grm21br61h106ke43l"
    assert got["C3"].basis == "datasheet (Murata GRM21BR61H106KE43L)"


def test_an_unknown_part_number_falls_back_to_the_generic_tiers(parts):
    got, _ = parts
    assert got["C4"].component_id == "generic-mlcc-100n-0402"
    assert got["C4"].matched_by == "value and package"
    assert got["C4"].basis == "generic 0402"


def test_a_part_number_that_disagrees_with_the_value_wins_and_says_so(parts):
    got, _ = parts
    c5 = got["C5"]
    assert c5.component_id == "samsung-cl05b104ko5nnnc"
    assert c5.matched_on == "JLCPCB Part # C1525"
    assert c5.placeable
    assert "disagrees" in c5.notes[0]


def test_the_generic_label_names_the_dielectric_when_the_value_does(parts):
    got, _ = parts
    assert got["C6"].generic
    assert got["C6"].basis == "generic 0402 X7R"
    assert got["C6"].matched_by == "value and package"


def test_no_named_part_answers_for_value_and_package_alone():
    """A 100 nF 0402 with no part number must never be modelled as a Samsung part."""
    got = resolve_part(match_part("C9", "100nF", C0402))
    assert got.generic


def test_a_users_component_with_the_same_part_number_wins():
    mine = Candidate(parse_component({
        "format": "emi-component", "version": 1, "id": "mine-c1525", "kind": "capacitor",
        "name": "my measured C1525", "provenance": "measured",
        "match": {"lcsc": "C1525"},
        "model": {"type": "series_rlc", "c_f": 9e-8, "esl_h": 3e-10, "esr_ohm": 0.02},
        "sources": [{"doc": "VNA, shunt-through", "what": "ESL and ESR"}],
    }), TIER_MINE)
    got = resolve_part(match_part("C1", "100nF", C0402, {"LCSC": "c1525"}), [mine])
    assert got.component_id == "mine-c1525"
    assert got.basis == "measured (my measured C1525)"
