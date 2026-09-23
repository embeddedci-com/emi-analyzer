"""Netclasses from a .kicad_pro, in every shape KiCad has written them.

The project file said which net is in which class three different ways across KiCad 6 to 10,
and the reader looked for a fourth that none of them use. Every explicitly assigned net fell
back to Default, so a USB pair lost its declared gap and width without a word. The snippets
below keep the keys and value shapes each version actually writes, trimmed to net_settings.
"""

from __future__ import annotations

import json

import pytest

from emi_worker.kicad.netclass import find_pairs, parse_project


def _class(name: str, **kw) -> dict:
    """A class entry with the fields every version writes, so the reader sees real noise."""
    base = {
        "bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
        "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3,
        "microvia_drill": 0.1, "name": name, "pcb_color": "rgba(0, 0, 0, 0.000)",
        "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": 0.25,
        "via_diameter": 0.8, "via_drill": 0.4, "wire_width": 6,
    }
    base.update(kw)
    return base


USB = {"diff_pair_gap": 0.15, "diff_pair_width": 0.3, "track_width": 0.3}
POWER = {"track_width": 0.5}

# KiCad 6: members listed inside each class; no assignment map, no patterns.
KICAD6 = {
    "meta": {"filename": "demo.kicad_pro", "version": 1},
    "net_settings": {
        "classes": [
            _class("Default"),
            _class("USB", nets=["/USB_D+", "/USB_D-"], **USB),
            _class("Power", nets=["+3V3", "+5V"], **POWER),
        ],
        "meta": {"version": 2},
        "net_colors": None,
    },
}

# KiCad 7 and 8: classes carry no members; a net -> class map plus wildcard patterns.
KICAD8 = {
    "meta": {"filename": "demo.kicad_pro", "version": 1},
    "net_settings": {
        "classes": [_class("Default"), _class("USB", **USB), _class("Power", **POWER)],
        "meta": {"version": 3},
        "net_colors": None,
        "netclass_assignments": {"/USB_D+": "USB", "/USB_D-": "USB"},
        "netclass_patterns": [{"netclass": "Power", "pattern": "+*V*"}],
    },
}

# KiCad 9 and 10: a net may be in several classes, so the map's values are lists, and
# classes gained a priority.
KICAD9 = {
    "meta": {"filename": "demo.kicad_pro", "version": 3},
    "net_settings": {
        "classes": [
            _class("Default", priority=2147483647),
            _class("USB", priority=0, **USB),
            _class("Power", priority=1, **POWER),
        ],
        "meta": {"version": 4},
        "net_colors": None,
        "netclass_assignments": {"/USB_D+": ["USB"], "/USB_D-": ["Unknown", "USB"]},
        "netclass_patterns": [{"netclass": "Power", "pattern": "+*V*"}],
    },
}


@pytest.mark.parametrize("doc", [KICAD6, KICAD8, KICAD9], ids=["kicad6", "kicad7-8", "kicad9-10"])
def test_every_version_assigns_nets_to_their_class(doc):
    classes = parse_project(json.dumps(doc, indent=2).encode())
    assert classes.available
    assert classes.of("/USB_D+") == "USB"
    assert classes.of("/USB_D-") == "USB"
    assert classes.of("+3V3") == "Power"
    assert classes.of("/SDA") == "Default"
    assert classes.spec("+3V3").track_width_mm == 0.5

    pair = find_pairs(["/USB_D+", "/USB_D-", "/SDA"], classes)[0]
    assert (pair.netclass, pair.source) == ("USB", "netclass")
    assert (pair.gap_mm, pair.width_mm) == (0.15, 0.3)


def test_null_assignments_are_no_assignments():
    """KiCad 7 and 8 write null, not {}, when nothing is assigned."""
    doc = json.loads(json.dumps(KICAD8))
    doc["net_settings"]["netclass_assignments"] = None
    doc["net_settings"]["netclass_patterns"] = None
    classes = parse_project(json.dumps(doc))
    assert classes.of("/USB_D+") == "Default"


def test_an_explicit_assignment_beats_a_pattern():
    doc = json.loads(json.dumps(KICAD8))
    doc["net_settings"]["netclass_assignments"]["+5V"] = "USB"
    classes = parse_project(json.dumps(doc))
    assert classes.of("+5V") == "USB"
    assert classes.of("+3V3") == "Power"


def test_an_unreadable_project_degrades_to_no_classes():
    classes = parse_project(b"{not json")
    assert not classes.available
    assert classes.warnings
