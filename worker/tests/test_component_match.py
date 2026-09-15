"""Matching parts to models — §19's K3.

"Every standard KiCad capacitor footprint on a set of real boards (EMI_TEST_BOARDS)
resolves; no 0201 is read as metric 0603."

The board half of that runs only where those boards exist, so it is skipped rather than
failed elsewhere. The reading rules themselves are pinned here unconditionally, because they
are what a skipped test would have been checking.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from emi_worker.components.match import (
    IMPERIAL_TO_METRIC,
    match_part,
    parse_package,
)

BOARDS = Path(os.environ.get("EMI_TEST_BOARDS", "/nonexistent"))

STANDARD = [
    ("Capacitor_SMD:C_0402_1005Metric", "0402", "1005"),
    ("Capacitor_SMD:C_0603_1608Metric", "0603", "1608"),
    ("Capacitor_SMD:C_0805_2012Metric", "0805", "2012"),
    ("Capacitor_SMD:C_1206_3216Metric", "1206", "3216"),
    ("Capacitor_SMD:C_1210_3225Metric", "1210", "3225"),
]


@pytest.mark.parametrize("footprint,imperial,metric", STANDARD)
def test_standard_footprints_resolve(footprint, imperial, metric):
    """These five cover 425 of the 429 capacitors on the real boards."""
    got = parse_package(footprint)
    assert got is not None
    assert (got.imperial, got.metric) == (imperial, metric)
    assert got.ambiguous is False


def test_k3_an_0201_is_not_read_as_metric_0603():
    """The named trap. Metric 0603 IS imperial 0201, a four-times difference in area, so a
    reader that takes the wrong code puts the self-resonance somewhere else entirely."""
    got = parse_package("Capacitor_SMD:C_0201_0603Metric")
    assert got is not None
    assert got.imperial == "0201"
    assert got.metric == "0603"
    assert got.ambiguous is False


def test_a_bare_code_is_read_as_imperial_and_says_so():
    """§12: read as imperial, and the match says so. Saying so is the whole point — the
    reading is a guess and the user is the only one who can confirm it."""
    got = parse_package("0603")
    assert got is not None
    assert got.imperial == "0603"
    assert got.ambiguous is True
    assert "read as imperial" in got.describe()
    # And it names what the other reading would have meant.
    assert "0201" in got.describe()


def test_the_library_prefix_is_ignored():
    """The reading is the same either way. The record still carries the name it came from,
    on purpose, so a finding can quote the footprint the user actually wrote."""
    with_prefix = parse_package("Capacitor_SMD:C_0402_1005Metric")
    without = parse_package("C_0402_1005Metric")
    assert with_prefix is not None and without is not None
    assert (with_prefix.imperial, with_prefix.metric, with_prefix.ambiguous) == \
           (without.imperial, without.metric, without.ambiguous)
    assert with_prefix.footprint == "Capacitor_SMD:C_0402_1005Metric"


def test_a_custom_footprint_is_not_guessed_at():
    """These two are real, from the boards. The name carries dimensions a reader could
    extract, but the part behind it is whatever the library author meant."""
    for footprint in (
        "lcsc:CAP-SMD_L4.5-W3.2_STC3MD20-T1",
        "lcsc_footprints:C22387799_CAP-SMD_BD10_0-L10_3-W10_3-FD",
    ):
        assert parse_package(footprint) is None


def test_a_hand_edited_mismatch_is_refused():
    """0402 is 1005 metric, never 1608. A name pairing codes KiCad never pairs has been
    edited by hand, and trusting either half would be a guess."""
    assert parse_package("C_0402_1608Metric") is None


def test_every_imperial_code_maps_to_one_metric_code():
    for imperial, metric in IMPERIAL_TO_METRIC.items():
        got = parse_package(f"C_{imperial}_{metric}Metric")
        assert got is not None, f"{imperial} does not parse"
        assert got.imperial == imperial


# ---- the whole part --------------------------------------------------------------------

def test_a_part_needs_both_a_value_and_a_package():
    good = match_part("C12", "100nF", "Capacitor_SMD:C_0402_1005Metric")
    assert good.modellable and good.why_not() is None

    no_value = match_part("C13", "STC3MA06-T1", "Capacitor_SMD:C_0402_1005Metric")
    assert not no_value.modellable
    assert "not a capacitance" in no_value.why_not()

    no_package = match_part("C14", "100nF", "lcsc:CAP-SMD_L4.5-W3.2_STC3MD20-T1")
    assert not no_package.modellable
    assert "not a standard package" in no_package.why_not()

    neither = match_part("C15", "?", "lcsc:whatever")
    assert "neither the value" in neither.why_not()


# ---- K3 against the real boards --------------------------------------------------------

@pytest.mark.skipif(not BOARDS.is_dir(), reason="EMI_TEST_BOARDS is not set to a directory of real boards")
def test_k3_on_the_real_boards():
    """Measured when this runs: 429 two-pad capacitors, 425 on standard footprints.

    The four that do not resolve are the BD10 electrolytic (3) and one STC3MD20, both custom
    LCSC footprints, which §12 says are out of scope for this phase. The assertion is on the
    *standard* ones: every footprint KiCad itself names must resolve.
    """
    from emi_worker.kicad import parse, parse_board
    from emi_worker.rules.decoupling import CAP_RE

    paths = [p for p in sorted(BOARDS.glob("*/*.kicad_pcb"))
             if not p.name.startswith("_autosave") and "backup" not in p.name.lower()]
    assert paths, "no boards found"

    total = 0
    unresolved: list[str] = []
    for path in paths:
        board = parse_board(parse(path.read_text()))
        by_ref: dict[str, list] = {}
        for pad in board.pads:
            if pad.ref:
                by_ref.setdefault(pad.ref, []).append(pad)
        for ref, pads in by_ref.items():
            if not CAP_RE.match(ref) or len(pads) != 2:
                continue
            total += 1
            footprint = pads[0].footprint or ""
            # Only KiCad's own standard names are in scope.
            if "Metric" not in footprint:
                continue
            if parse_package(footprint) is None:
                unresolved.append(f"{path.parent.name}/{ref}: {footprint}")

    assert total > 400, f"expected the four boards to carry 400+ capacitors, saw {total}"
    assert not unresolved, "standard footprints that did not resolve:\n" + "\n".join(unresolved)
