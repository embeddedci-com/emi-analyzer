"""The compliance run: arithmetic on what other runs already produced (§16, §17).

Two properties matter more than any number here, and both are about refusing to publish:

* an incomplete estimate carries **no** margin or confidence key, not a margin with a warning;
* the spectrum is drawn anyway, because seeing which frequencies sit close is useful long
  before the estimate is whole.
"""

from __future__ import annotations

import json

import pytest

from emi_worker.client import RunToken
from emi_worker.stages import STAGES, StageContext, StageError
from emi_worker.stages.compliance import run_compliance


class FakeClient:
    def __init__(self):
        self.uploads: dict[str, bytes] = {}
        self.progress_calls: list[tuple] = []

    def progress(self, token, stage, pct, message="", **fields):
        self.progress_calls.append((stage, pct, message))

    def upload_artifact(self, token, name, blob, content_type):
        self.uploads[name] = blob
        return {"name": name, "size_bytes": len(blob)}


def _run(tmp_path, params) -> dict:
    client = FakeClient()
    ctx = StageContext(
        client=client,
        token=RunToken(token="t", run_id="r1", jti="j", expires_in=3600),
        run={"params": params},
        workdir=str(tmp_path),
        cores=1, max_cells=10**9, should_stop=lambda: False,
    )
    result = run_compliance(ctx)
    doc = json.loads(client.uploads["compliance.json"])
    return {"doc": doc, "result": result, "client": client}


COMPLETE_INPUTS = dict(
    drivers=[{"net": "CLK"}],
    source_nets=["CLK"],
    connectors=["J1"],
    cable_assignments={"J1": {"type": "usb2-shielded"}},
    driven_ports=["p1"],
    far_field_refs=["p1"],
    solved_f_max_hz=1e9,
    required_f_max_hz=1e9,
    undriven_harmonics={},
    enclosure="plastic",
    power="dc",
    power_entry_found=True,
)

# 1e-3 V/m is 60 dBuV/m, comfortably over every FCC Class B limit, so the margin is negative
# and unambiguous rather than sitting on a band edge.
LOUD_PATH = {
    "kind": "board", "label": "board region around U3", "driver_id": "clk",
    "frequencies_hz": [100e6, 300e6], "field_v_per_m": [1e-3, 5e-4],
    "sigma_terms": {"mesh": 2.0},
    "nets": ["CLK"],
}


def test_it_is_a_registered_run_kind():
    assert STAGES["compliance"] is run_compliance


def test_every_worker_can_take_it(tmp_path):
    """No solver is involved, so a cable swap or a driver change must not queue behind one.

    Checked against the real capability detection rather than against the source, because what
    matters is what a worker actually advertises to the server.
    """
    from emi_worker.config import Config, detect_capabilities

    caps = detect_capabilities(Config(
        server_url="http://server.invalid", api_key="eci_test", name="t",
        workdir=str(tmp_path)))
    assert "compliance" in caps.kinds
    # Even one with no solver at all: the ingest-only worker on the droplet takes these.
    assert "ingest" in caps.kinds


def test_a_complete_project_gets_a_margin(tmp_path):
    out = _run(tmp_path, {**COMPLETE_INPUTS, "paths": [LOUD_PATH]})
    doc = out["doc"]
    assert doc["complete"] is True
    assert doc["gaps"] == []
    assert doc["margin_db"] < 0, "60 dBuV/m is over every Class B limit"
    assert 0.0 <= doc["confidence"] <= 1.0
    assert doc["sigma_db"] > 0
    assert len(doc["range_80_db"]) == 2
    assert doc["range_80_db"][0] < doc["margin_db"] < doc["range_80_db"][1]


def test_an_incomplete_project_carries_no_number_at_all(tmp_path):
    """§17.3. A warning is skippable; a missing field is not."""
    out = _run(tmp_path, {**COMPLETE_INPUTS, "connectors": ["J1", "J2"],
                          "paths": [LOUD_PATH]})
    doc = out["doc"]
    assert doc["complete"] is False
    assert "margin_db" not in doc
    assert "confidence" not in doc
    assert "sigma_db" not in doc
    assert "contributions" not in doc
    assert any(g["key"] == "cable:J2" for g in doc["gaps"])


def test_the_spectrum_is_drawn_even_when_incomplete(tmp_path):
    """Seeing which frequencies sit close is useful long before the estimate is whole."""
    out = _run(tmp_path, {**COMPLETE_INPUTS, "drivers": [], "paths": [LOUD_PATH]})
    doc = out["doc"]
    assert doc["complete"] is False
    assert len(doc["spectrum"]) == 2
    assert all("limit_dbuv_per_m" in pt for pt in doc["spectrum"])


def test_an_empty_project_says_so_rather_than_drawing_nothing(tmp_path):
    out = _run(tmp_path, {**COMPLETE_INPUTS, "paths": []})
    doc = out["doc"]
    assert doc["spectrum"] == []
    assert "no_paths" in doc
    assert "far-field box" in doc["no_paths"]


def test_contributions_are_ranked_and_name_the_path(tmp_path):
    cable = {"kind": "cable", "label": "cable J1 (USB 2.0, 1 m)", "driver_id": "buck",
             "frequencies_hz": [100e6, 300e6], "field_v_per_m": [2e-3, 1e-5],
             "sigma_terms": {"cable": 4.5}}
    out = _run(tmp_path, {**COMPLETE_INPUTS, "paths": [LOUD_PATH, cable]})
    doc = out["doc"]
    assert doc["worst"]["frequency_hz"] == 100e6
    assert doc["contributions"][0]["label"] == "cable J1 (USB 2.0, 1 m)"
    assert doc["contributions"][0]["share"] > doc["contributions"][1]["share"]
    assert sum(c["share"] for c in doc["contributions"]) == pytest.approx(1.0)
    assert doc["shares_indicative"] is False


def test_confidence_is_labelled_uncalibrated_in_the_payload(tmp_path):
    """So no client can present it as a pass probability by accident (§17.1)."""
    doc = _run(tmp_path, {**COMPLETE_INPUTS, "paths": [LOUD_PATH]})["doc"]
    assert doc["confidence_uncalibrated"] == doc["confidence"]


def test_the_budget_shows_its_own_working(tmp_path):
    doc = _run(tmp_path, {**COMPLETE_INPUTS, "paths": [LOUD_PATH]})["doc"]
    assert doc["sigma_terms"], "a sigma a reader cannot interrogate is worse than no sigma"
    assert "placeholder" in doc["uncertainty_note"].lower()


def test_recommendations_come_from_findings_on_the_dominant_path(tmp_path):
    findings = [
        {"id": "plane-gap-1", "rule": "plane-gap", "severity": "critical",
         "title": "Gap under CLK", "detail": "d", "net": "CLK"},
        {"id": "switch-node-1", "rule": "switch-node", "severity": "critical",
         "title": "Big switch node", "detail": "d", "net": "SW"},
    ]
    doc = _run(tmp_path, {**COMPLETE_INPUTS, "paths": [LOUD_PATH],
                          "findings": findings})["doc"]
    recs = doc["recommendations"]
    assert [i["rule"] for i in recs["items"]] == ["plane-gap"]
    assert recs["general_only"] is False


def test_no_finding_on_the_path_gives_guidance_marked_general(tmp_path):
    doc = _run(tmp_path, {**COMPLETE_INPUTS, "paths": [LOUD_PATH], "findings": []})["doc"]
    recs = doc["recommendations"]
    assert recs["items"] == []
    assert recs["general_only"] is True
    assert recs["general"]


def test_an_unknown_standard_is_refused(tmp_path):
    with pytest.raises(StageError, match="unknown standard"):
        _run(tmp_path, {**COMPLETE_INPUTS, "standard_id": "not-a-standard",
                        "paths": [LOUD_PATH]})


def test_it_finishes_at_a_hundred_percent(tmp_path):
    out = _run(tmp_path, {**COMPLETE_INPUTS, "paths": [LOUD_PATH]})
    assert out["client"].progress_calls[-1][0] == "done"
    assert out["client"].progress_calls[-1][1] == 100
