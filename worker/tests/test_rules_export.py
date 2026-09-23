"""The emi.rules.yaml the app exports is a rules file this worker reads as intended.

tests/fixtures/rules_export.yaml is written by the webapp's own writer
(webapp/src/lib/rulesSettings.test.ts compares its output with this file). Reading it here
closes the loop: a value the app exports that the worker would refuse, or read as another
type, fails in one of the two suites.
"""

from __future__ import annotations

from pathlib import Path

from emi_worker.rules import settings

FIXTURE = Path(__file__).parent / "fixtures" / "rules_export.yaml"


def test_the_exported_rules_file_reads_back_without_warnings():
    cfg = settings.load(("file", settings.parse_document(FIXTURE.read_text(encoding="utf-8"))))
    assert cfg.warnings == []

    assert cfg.value("max_frequency_hz") == 1.6e9
    assert cfg.board["max_frequency_hz"].source == "file"
    assert not cfg.enabled("radiator")
    assert cfg.severity("plane-gap", "critical") == "info"
    assert cfg.severity("edge-proximity", "warning") == "info"
    assert cfg.param("via-stub", "resonance_margin") == 6
    assert cfg.param("ddr-skew", "byte_lane_ps") == 6
    # The group written from the rules file it came from, still scoped to its nets.
    assert cfg.param("ddr-skew", "byte_lane_ps", net="DDR_DQ3") == 5
    assert cfg.suppressed("edge-proximity", "GND").reason == "guard ring, intentional"
    # Untouched values are left to the defaults rather than written out.
    assert cfg.rule("ddr-skew").params["intra_pair_ps"].source == "default"
