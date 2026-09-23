"""Settings, matched groups, and impedance.

The three pieces that make a rule with a tolerance possible: somewhere to put the tolerance,
something to apply it to, and a number to compare.
"""

from __future__ import annotations

import pytest

from emi_worker import impedance
from emi_worker.kicad.netclass import find_pairs, parse_project, split_pair_name
from emi_worker.rules import matching, settings
from emi_worker.stackup import LayerElectrics


# ---- settings ---------------------------------------------------------------------------

def test_later_sources_win_and_say_where_they_came_from():
    """A surprising threshold with no provenance costs an hour of hunting."""
    s = settings.load(
        ("project", {"rules": {"ddr-skew": {"params": {"byte_lane_ps": 8}}}}),
        ("file", {"rules": {"ddr-skew": {"params": {"byte_lane_ps": 6}}}}),
    )
    assert s.param("ddr-skew", "byte_lane_ps") == 6
    assert "emi.rules.yaml" in s.rule("ddr-skew").describe("byte_lane_ps", "ps")


def test_a_net_group_overrides_the_rule_default():
    s = settings.load(("file", {
        "rules": {"ddr-skew": {"params": {"byte_lane_ps": 10}}},
        "groups": [{"match": "DQ*", "params": {"byte_lane_ps": 4}}],
    }))
    assert s.param("ddr-skew", "byte_lane_ps") == 10
    assert s.param("ddr-skew", "byte_lane_ps", net="DQ7") == 4
    assert s.param("ddr-skew", "byte_lane_ps", net="SPI_CLK") == 10


def test_rules_can_be_switched_off_and_re_severitied():
    s = settings.load(("file", {"rules": {"radiator": False, "plane-gap": {"severity": "info"}}}))
    assert not s.enabled("radiator")
    assert s.enabled("plane-gap")
    assert s.severity("plane-gap", "critical") == "info"


def test_suppressions_need_a_reason():
    """A suppression nobody explained is a mystery to whoever finds it next year."""
    s = settings.load(("file", {"suppress": [{"rule": "edge-proximity", "net": "GND"}]}))
    assert any("no reason" in w for w in s.warnings)
    assert s.suppressed("edge-proximity", "GND") is not None
    assert s.suppressed("edge-proximity", "SIG") is None


def test_a_typo_is_warned_about_not_silently_ignored():
    s = settings.load(("file", {
        "rules": {"nonsense": {"enabled": False}, "radiator": {"params": {"wavelenght": 1}}},
        "board": {"max_frequenzy_hz": 1},
    }))
    assert len(s.warnings) == 3
    # ...and the run still happens with everything else intact.
    assert s.enabled("radiator")


def test_a_newer_settings_version_is_refused_rather_than_guessed_at():
    """Thresholds change meaning as checks improve. Reinterpreting one silently is worse
    than refusing the document."""
    s = settings.load(("file", {"version": settings.SETTINGS_VERSION + 1,
                                "rules": {"radiator": False}}))
    assert s.enabled("radiator"), "a document from the future was applied anyway"
    assert any("newer than this analyzer" in w for w in s.warnings)


def test_the_catalogue_covers_every_rule_that_runs():
    """The UI builds its settings page from the catalogue, so a rule missing from it is a
    rule nobody can configure."""
    from emi_worker.rules.checks import RULES

    listed = {r["id"] for r in settings.catalogue()}
    assert {name for name, _ in RULES} <= listed


# ---- matched groups ---------------------------------------------------------------------

DDR_NETS = (
    [f"DDR_DQ{i}" for i in range(16)]
    + ["DDR_DQS0_P", "DDR_DQS0_N", "DDR_DQS1_P", "DDR_DQS1_N", "DDR_DM0", "DDR_DM1"]
    + [f"DDR_A{i}" for i in range(14)]
    + ["DDR_CK_P", "DDR_CK_N", "DDR_RAS_N", "DDR_CAS_N", "DDR_WE_N", "DDR_CKE", "DDR_ODT"]
)


def _ddr_pads():
    pads = {n: ["U1.A1"] for n in DDR_NETS}
    for n in [f"DDR_DQ{i}" for i in range(8)] + ["DDR_DQS0_P", "DDR_DM0"]:
        pads[n] = ["U1.A1", "U5.B2"]
    for n in [f"DDR_DQ{i}" for i in range(8, 16)] + ["DDR_DQS1_P", "DDR_DM1"]:
        pads[n] = ["U1.A1", "U6.B2"]
    return pads


def test_data_matches_its_strobe_and_address_matches_the_clock():
    """The distinction the whole check turns on.

    DQ is captured on DQS, not on CK. Matching data to the clock would miss the error that
    actually stops a board working, and matching lanes to each other would bury a correct
    board in findings.
    """
    groups = matching.find_groups(DDR_NETS, _ddr_pads(), None, find_pairs(DDR_NETS, None))
    by_kind = {g.kind: g for g in groups if g.kind != "pair"}

    lanes = [g for g in groups if g.kind == "byte-lane"]
    assert len(lanes) == 2
    for lane in lanes:
        assert "DQS" in lane.reference, f"{lane.name} is matched to {lane.reference}, not a strobe"
        assert lane.tolerance_key == "byte_lane_ps"

    addr = by_kind["address-command"]
    assert addr.reference == "DDR_CK_P"
    assert addr.tolerance_key == "address_command_ps"
    assert "DDR_A0" in addr.members and "DDR_RAS_N" in addr.members
    # Data must not be in the address group.
    assert not any(m.startswith("DDR_DQ") for m in addr.members)


def test_lanes_are_grouped_by_device_not_by_bit_number():
    """Bit swapping within a lane and byte-lane swapping are both legal and common, so an
    index is not lane membership."""
    groups = matching.find_groups(DDR_NETS, _ddr_pads(), None, find_pairs(DDR_NETS, None))
    lanes = {g.name: set(g.members) for g in groups if g.kind == "byte-lane"}
    assert all(g.source == "pins" for g in groups if g.kind == "byte-lane")
    assert len(lanes) == 2
    # The two lanes are disjoint apart from nothing at all.
    a, b = lanes.values()
    assert not (a & b)


def test_pairs_are_matched_to_each_other_with_the_tightest_budget():
    groups = matching.find_groups(DDR_NETS, _ddr_pads(), None, find_pairs(DDR_NETS, None))
    pairs = [g for g in groups if g.kind == "pair"]
    assert {g.name for g in pairs} == {"DDR_CK", "DDR_DQS0", "DDR_DQS1"}
    assert all(g.tolerance_key == "intra_pair_ps" for g in pairs)


def test_a_lone_net_ending_in_p_is_not_half_of_a_pair():
    """Inventing the other half would produce a finding about a signal that is not there."""
    assert split_pair_name("CLK_P") == ("CLK", True)
    assert find_pairs(["CLK_P", "VCC", "GND"], None) == []
    assert len(find_pairs(["CLK_P", "CLK_N"], None)) == 1


# ---- impedance ----------------------------------------------------------------------------

def _microstrip(assumed=False, er=4.3, h=0.2):
    return LayerElectrics(name="F.Cu", index=0, kind="microstrip", reference_plane="In1.Cu",
                          height_mm=h, epsilon_r=er, copper_thickness_mm=0.035, assumed=assumed)


def test_impedance_falls_as_the_trace_widens():
    z = [impedance.single_ended(w, _microstrip()).ohm for w in (0.15, 0.25, 0.35, 0.5)]
    assert z == sorted(z, reverse=True)
    # And lands where a 4-layer board actually lands: ~50 ohm at ~0.35 mm over 0.2 mm.
    assert 45 <= impedance.single_ended(0.35, _microstrip()).ohm <= 55


def test_stripline_is_slower_and_lower_impedance_than_microstrip():
    sl = LayerElectrics(name="In1.Cu", index=1, kind="stripline", reference_plane="In2.Cu",
                        height_mm=0.2, height_above_mm=0.2, epsilon_r=4.3)
    assert sl.ps_per_mm > _microstrip().ps_per_mm
    assert impedance.single_ended(0.2, sl).ohm < impedance.single_ended(0.2, _microstrip()).ohm


def test_an_assumed_permittivity_widens_the_claim():
    """The tool must not report a number it has no right to."""
    known = impedance.single_ended(0.25, _microstrip(assumed=False))
    guessed = impedance.single_ended(0.25, _microstrip(assumed=True))
    assert guessed.uncertainty_pct > known.uncertainty_pct
    assert any("assumed" in n for n in guessed.notes)


def test_a_trace_is_only_flagged_when_the_whole_range_misses():
    """Otherwise the tool reports its own model error as the board's problem."""
    z = impedance.Impedance(ohm=44, uncertainty_pct=10, kind="microstrip")
    assert z.within(40, 10)      # 39.6-48.4 overlaps 36-44
    assert not z.within(90, 10)


def test_geometry_outside_the_formula_is_flagged_not_silently_wrong():
    # w/h = 4, past the formula's validity but still computable: the answer comes with a
    # wider claim and a note saying why.
    wide = impedance.single_ended(0.8, _microstrip(h=0.2))
    assert any("outside the range" in n for n in wide.notes)
    assert wide.uncertainty_pct > impedance.MODEL_UNCERTAINTY_PCT

    # Far enough out and the formula returns a nonsense value; better nothing than that.
    assert impedance.single_ended(3.0, _microstrip(h=0.2)) is None


def test_differential_impedance_rises_as_the_pair_separates():
    ms = _microstrip()
    tight = impedance.differential(0.2, 0.15, ms).ohm
    loose = impedance.differential(0.2, 0.6, ms).ohm
    assert loose > tight
    # Far enough apart and it is two traces, not a pair -- and it says so.
    assert any("differential in name only" in n for n in impedance.differential(0.2, 1.0, ms).notes)


def test_no_reference_plane_means_no_impedance_rather_than_a_guess():
    floating = LayerElectrics(name="F.Cu", index=0, kind="microstrip", reference_plane="",
                              height_mm=0.0, epsilon_r=4.3)
    assert impedance.single_ended(0.25, floating) is None


def test_the_webapp_catalogue_matches_the_rules_that_run():
    """The front page table and the findings labels read this file. A stale copy would tell
    users about checks that no longer run, or hide ones that do.

    Regenerate with: python scripts/export_rule_catalogue.py
    """
    import json
    from pathlib import Path

    exported = Path(__file__).resolve().parents[2] / "webapp" / "src" / "lib" / "ruleCatalogue.json"
    assert exported.exists(), "run python scripts/export_rule_catalogue.py"
    assert json.loads(exported.read_text(encoding="utf-8")) == settings.catalogue(), (
        "ruleCatalogue.json is stale; run python scripts/export_rule_catalogue.py"
    )


def test_every_rule_has_a_category_for_the_front_page():
    missing = [r["id"] for r in settings.catalogue() if not r["category"]]
    assert missing == []


def test_disabling_a_rule_that_predates_settings_actually_stops_it():
    """Regression: run_rules ran every rule, so disabling one of the original five did nothing."""
    from pathlib import Path
    from emi_worker.kicad import parse, parse_board
    from emi_worker.kicad.normalize import _board_extent
    from emi_worker.rules import run_rules
    from emi_worker.rules.model import RuleContext

    fixture = Path(__file__).parent / "fixtures" / "tiny.kicad_pcb"
    model = parse_board(parse(fixture.read_text()))

    def rules_run(cfg):
        ctx = RuleContext(model=model, transform=_board_extent(model), max_frequency_hz=1e9, settings=cfg)
        return {f["rule"] for f in run_rules(ctx).as_dict()["findings"]}

    on = rules_run(settings.defaults())
    assert "return-via" in on, "fixture no longer exercises return-via; pick another rule"
    off = rules_run(settings.load(("file", {"rules": {"return-via": False}})))
    assert "return-via" not in off


def test_a_severity_override_reaches_rules_that_predate_settings():
    from pathlib import Path
    from emi_worker.kicad import parse, parse_board
    from emi_worker.kicad.normalize import _board_extent
    from emi_worker.rules import run_rules
    from emi_worker.rules.model import RuleContext

    model = parse_board(parse((Path(__file__).parent / "fixtures" / "tiny.kicad_pcb").read_text()))
    cfg = settings.load(("file", {"rules": {"return-via": {"severity": "info"}}}))
    ctx = RuleContext(model=model, transform=_board_extent(model), max_frequency_hz=1e9, settings=cfg)
    sev = {f["severity"] for f in run_rules(ctx).as_dict()["findings"] if f["rule"] == "return-via"}
    assert sev == {"info"}


def test_the_rules_file_in_the_docs_is_read_as_written():
    """The documented example is YAML. It once fell back to JSON in the image, failed, and the
    board was checked with built-in defaults while the user believed their rules applied."""
    import pathlib
    import re

    doc = (pathlib.Path(__file__).parents[2] / "docs" / "rules-file.md").read_text()
    example = re.search(r"```yaml\n(.*?)```", doc, re.S).group(1)
    s = settings.load(("file", settings.parse_document(example)))
    assert not [w for w in s.warnings if "could not" in w]
    assert s.rules or s.groups or s.suppressions


def test_a_rules_file_that_is_not_a_mapping_is_an_error_not_a_default():
    with pytest.raises(ValueError, match="mapping"):
        settings.parse_document("- just\n- a list\n")
