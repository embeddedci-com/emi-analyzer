"""A finding can carry its own fix when the rule's usual one does not fit.

The decoupling check reports a missing capacitor, a distant one and one with no ground via,
and the app showed "move the capacitor next to the pin" for all three.
"""

from __future__ import annotations

from emi_worker.rules.model import Finding


def test_an_empty_action_is_left_out_so_the_rule_default_applies():
    d = Finding(rule="decoupling", severity="warning", title="t", detail="d").as_dict(0)
    assert "action" not in d


def test_a_set_action_is_written():
    d = Finding(rule="decoupling", severity="warning", title="t", detail="d",
                action="Add a ground via next to the capacitor's ground pad.").as_dict(0)
    assert d["action"].startswith("Add a ground via")
