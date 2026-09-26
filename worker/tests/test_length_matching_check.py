"""The length-matching check itself: within a group, and byte lane against byte lane.

Delays are stubbed, so these check the comparison and its settings, not the geometry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from emi_worker.rules import checks, settings
from emi_worker.rules.matching import MatchGroup


def _lane(i: int) -> MatchGroup:
    return MatchGroup(name=f"byte lane {i}", kind="byte-lane",
                      members=[f"DQ{8 * i + k}" for k in range(8)] + [f"DQS{i}_P"],
                      reference=f"DQS{i}_P", tolerance_key="byte_lane_ps", source="pins")


def _run(monkeypatch, delays: dict[str, float], doc: dict) -> list:
    s = settings.load(("file", doc))
    ctx = SimpleNamespace(
        groups=[_lane(i) for i in range(4)], electrics=object(), topology={}, notes=[],
        netclasses=None, settings=s,
        enabled=lambda rule: s.enabled(rule) if hasattr(s, "enabled") else True,
        setting=lambda rule, key, net=None: s.param(rule, key, net=net),
    )
    monkeypatch.setattr(checks, "_group_delay", lambda _ctx, net: delays.get(net, 100.0))
    monkeypatch.setattr(checks, "_net_midpoint", lambda _ctx, net: None)
    monkeypatch.setattr(checks, "_path_summary", lambda _ctx, net: "")
    return list(checks.check_length_matching(ctx))


LATE_LANE = {"DQS0_P": 100.0, "DQS1_P": 102.0, "DQS2_P": 98.0, "DQS3_P": 160.0}


def test_lanes_are_not_compared_by_default(monkeypatch):
    """Write levelling absorbs lane-to-lane skew, so a correct board must stay quiet."""
    found = _run(monkeypatch, LATE_LANE, {})
    assert not [f for f in found if "other lanes" in f.title]


def test_a_late_lane_is_reported_against_the_median_when_asked(monkeypatch):
    found = _run(monkeypatch, LATE_LANE, {"rules": {"ddr-skew": {"params": {"lane_to_lane_ps": 20}}}})
    lane = [f for f in found if "other lanes" in f.title]
    assert [f.net for f in lane] == ["DQS3_P"], "only the outlier, not every lane against it"
    assert "59 ps later" in lane[0].title


def test_a_member_outside_its_lane_budget_is_reported(monkeypatch):
    delays = {**{f"DQS{i}_P": 100.0 for i in range(4)}, "DQ3": 125.0}
    found = _run(monkeypatch, delays, {"rules": {"ddr-skew": {"params": {"byte_lane_ps": 10}}}})
    assert [f.net for f in found] == ["DQ3"]
    assert "25 ps longer than DQS0_P" in found[0].title


def test_a_zero_budget_switches_the_group_comparison_off(monkeypatch):
    delays = {"DQ3": 500.0}
    assert _run(monkeypatch, delays, {"rules": {"ddr-skew": {"params": {"byte_lane_ps": 0}}}}) == []
