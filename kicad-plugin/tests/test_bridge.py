"""The channel the app's pages call KiCad through.

Needs PySide6, which is what KiCad installs for the plugin; skipped where it is absent so
the rest of the suite still runs.
"""

import json

import pytest

pytest.importorskip("PySide6.QtCore")

from emi_analyzer.bridge import BRIDGE_VERSION, Bridge  # noqa: E402


class Calls:
    """Stands in for the window's worker thread, running everything at once."""

    def __init__(self):
        self.answers = []

    def run(self, fn, callback):
        try:
            callback(fn(), None)
        except BaseException as e:  # noqa: BLE001
            callback(None, e)

    def collect(self, bridge):
        bridge.resultReady.connect(lambda rid, payload: self.answers.append((rid, json.loads(payload))))


def make(handlers):
    calls = Calls()
    bridge = Bridge(handlers, run=calls.run)
    calls.collect(bridge)
    return bridge, calls


def test_a_page_asking_to_select_gets_the_count_back():
    selected = []

    def select(args):
        selected.append(args["nets"])
        return {"selected": len(args["nets"]) * 3}

    bridge, calls = make({"select": select})

    bridge.call("r1", "select", json.dumps({"nets": ["GND", "DDR_A0"]}))

    assert selected == [["GND", "DDR_A0"]]
    assert calls.answers == [("r1", {"selected": 6})]


def test_an_answer_carries_the_id_it_was_asked_with():
    """The page pairs answers to questions by id; two in flight must not be confused."""
    bridge, calls = make({"select": lambda a: {"selected": a["nets"][0]}})

    bridge.call("r1", "select", json.dumps({"nets": [1]}))
    bridge.call("r2", "select", json.dumps({"nets": [2]}))

    assert calls.answers == [("r1", {"selected": 1}), ("r2", {"selected": 2})]


def test_a_failure_in_kicad_reaches_the_page_as_an_error():
    def boom(_args):
        raise RuntimeError("KiCad closed the board")

    bridge, calls = make({"select": boom})
    bridge.call("r1", "select", "{}")

    assert calls.answers == [("r1", {"error": "KiCad closed the board"})]


@pytest.mark.parametrize(
    "name,args",
    [("evaluate", "{}"), ("select", "not json"), ("select", '"a string"')],
)
def test_anything_the_plugin_does_not_offer_is_refused(name, args):
    """The page comes over HTTP from the app: only the two calls exist, and nothing else."""
    bridge, calls = make({"select": lambda a: {"selected": 0}})
    bridge.call("r1", name, args)

    assert len(calls.answers) == 1
    assert "error" in calls.answers[0][1]


def test_the_injected_script_declares_its_version():
    from emi_analyzer.bridge import ADAPTER_JS

    assert "__VERSION__" in ADAPTER_JS
    assert BRIDGE_VERSION == 1
