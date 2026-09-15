"""§16.5: recommendations are gathered from findings, never invented.

The constraint being tested is a design decision, not an implementation detail. A prediction
that wrote its own advice would be generating EMC guidance from a number that carries several
decibels of stated uncertainty; a finding is something specific about this board that someone
can go and look at. So every test here is ultimately about provenance.
"""

from __future__ import annotations

from emi_worker.compliance.recommend import GENERAL, NEAR_MM, gather


def _f(rule, severity="warning", net="", x=None, y=None, idx=0):
    d = {"id": f"{rule}-{idx}", "rule": rule, "severity": severity,
         "title": rule.replace("-", " "), "detail": "detail", "net": net}
    if x is not None:
        d["x"], d["y"] = x, y
    return d


def test_a_finding_on_the_paths_net_is_picked_up():
    r = gather([_f("plane-gap", net="USB_DP")], "cable", "cable J1", nets=("USB_DP",))
    assert [i.rule for i in r.items] == ["plane-gap"]
    assert not r.general_only


def test_a_finding_near_the_connector_counts_even_with_no_net():
    """A stitching finding often has no net at all, and sitting 2 mm from the exit is exactly
    what makes it relevant."""
    r = gather([_f("stitching", x=10.0, y=10.0)], "cable", "cable J1", near=(11.0, 10.0))
    assert [i.rule for i in r.items] == ["stitching"]
    assert r.items[0].distance_mm == 1.0


def test_a_finding_far_away_with_no_shared_net_is_left_out():
    """Rule alone would pull in every plane gap on the board, which is the findings table."""
    r = gather([_f("plane-gap", x=200.0, y=200.0)], "cable", "cable J1", near=(0.0, 0.0),
               nets=("USB_DP",))
    assert r.items == []
    assert r.general_only


def test_a_rule_that_is_not_on_this_kind_of_path_is_ignored():
    # switch-node belongs to the conducted path; it says nothing about why a cable radiates.
    r = gather([_f("switch-node", x=1.0, y=1.0)], "cable", "cable J1", near=(1.0, 1.0))
    assert r.items == []


def test_ranked_by_severity_first_then_distance():
    findings = [
        _f("plane-gap", "info", x=1.0, y=0.0, idx=1),
        _f("stitching", "critical", x=20.0, y=0.0, idx=2),
        _f("plane-gap", "warning", x=3.0, y=0.0, idx=3),
        _f("stitching", "warning", x=2.0, y=0.0, idx=4),
    ]
    r = gather(findings, "cable", "cable J1", near=(0.0, 0.0))
    assert [i.severity for i in r.items] == ["critical", "warning", "warning", "info"]
    # Within one severity, the nearer one first.
    assert [i.finding_id for i in r.items][1:3] == ["stitching-4", "plane-gap-3"]


def test_the_list_is_capped():
    findings = [_f("plane-gap", x=float(k), y=0.0, idx=k) for k in range(20)]
    r = gather(findings, "board", "board U3", near=(0.0, 0.0), limit=4)
    assert len(r.items) == 4


def test_nothing_found_gives_general_guidance_marked_as_general():
    r = gather([], "board", "board U3")
    assert r.general_only
    assert r.general == list(GENERAL["board"])
    assert r.items == []


def test_general_guidance_is_dropped_as_soon_as_something_specific_exists():
    r = gather([_f("decoupling", net="VDD")], "board", "board U3", nets=("VDD",))
    assert r.general == []
    assert not r.general_only


def test_every_item_carries_the_finding_id_so_it_can_be_highlighted():
    r = gather([_f("radiator", net="CLK", idx=7)], "board", "board U3", nets=("CLK",))
    assert r.items[0].finding_id == "radiator-7"


def test_the_proximity_window_is_bounded():
    """On a 100 mm board an unbounded window makes every finding 'near' everything."""
    assert 0 < NEAR_MM <= 50
    inside = gather([_f("stitching", x=NEAR_MM - 1, y=0.0)], "cable", "J1", near=(0.0, 0.0))
    outside = gather([_f("stitching", x=NEAR_MM + 1, y=0.0)], "cable", "J1", near=(0.0, 0.0))
    assert inside.items and not outside.items


def test_a_finding_with_no_location_still_qualifies_by_net():
    """A summary finding carries no x/y at all; that must not exclude it from its own net."""
    r = gather([_f("return-path", net="CLK")], "board", "board U3", nets=("CLK",),
               near=(0.0, 0.0))
    assert [i.rule for i in r.items] == ["return-path"]
    assert r.items[0].distance_mm is None
