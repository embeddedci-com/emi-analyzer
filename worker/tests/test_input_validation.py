"""Settings and sidecar files that are wrong in shape or type.

A settings value such as "1GHz" used to be stored as given and then fail the run as an
internal worker error; a negative one gave negative wavelengths; a project or job file that
was a list, or nested thousands deep, raised AttributeError or RecursionError. Each of these
now becomes a message the user can act on, and the run goes on where it can.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from emi_worker.client import RunToken
from emi_worker.gerber import reader
from emi_worker.kicad import netclass
from emi_worker.rules import settings
from emi_worker.stages import StageContext, StageError
from emi_worker.stages.ingest import run_ingest

FIXTURES = Path(__file__).parent / "fixtures"
DEEP = "[" * 100_000 + "]" * 100_000


# ---- settings values --------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1GHz", -1e9, 0, float("nan"), float("inf"), True, None, [1]])
def test_a_bad_maximum_frequency_is_ignored_with_a_warning(value):
    cfg = settings.load(("run", {"board": {"max_frequency_hz": value}}))
    assert cfg.value("max_frequency_hz") == settings.BOARD_DEFAULTS["max_frequency_hz"]
    assert any("max_frequency_hz" in w for w in cfg.warnings), cfg.warnings


def test_numbers_written_as_strings_are_read():
    cfg = settings.load(("file", {"board": {"max_frequency_hz": "2e9"},
                                  "rules": {"ddr-skew": {"params": {"min_group_size": "4"}}}}))
    assert cfg.value("max_frequency_hz") == 2e9
    assert cfg.param("ddr-skew", "min_group_size") == 4
    assert cfg.warnings == []


def test_negative_rule_parameters_are_refused():
    cfg = settings.load(("file", {"rules": {"radiator": {"params": {"wavelength_fraction": -0.1}}}}))
    assert cfg.param("radiator", "wavelength_fraction") == 0.05
    assert any("radiator.wavelength_fraction must be at least zero" in w for w in cfg.warnings)


def test_a_permittivity_below_one_is_refused():
    cfg = settings.load(("file", {"board": {"epsilon_r": 0.5}}))
    assert cfg.value("epsilon_r") == 0.0
    assert any("epsilon_r must be 1 or more" in w for w in cfg.warnings)


def test_group_overrides_are_checked_against_the_rule_they_override():
    cfg = settings.load(("file", {"groups": [
        {"match": "DDR_*", "params": {"byte_lane_ps": "fast", "intra_pair_ps": 3}},
    ]}))
    assert cfg.param("ddr-skew", "intra_pair_ps", net="DDR_DQ0") == 3.0
    assert cfg.param("ddr-skew", "byte_lane_ps", net="DDR_DQ0") == 10.0
    assert any("byte_lane_ps must be a number" in w for w in cfg.warnings)


@pytest.mark.parametrize("doc", [
    ["not", "a", "mapping"],
    {"board": ["max_frequency_hz", 1]},
    {"rules": {"radiator": 3}},
    {"rules": {"radiator": {"params": [1, 2]}}},
    {"groups": {"match": "*"}},
    {"groups": ["DDR_*"]},
    {"suppress": ["plane-gap"]},
    {"cables": {"connectors": ["J1"]}},
])
def test_a_document_of_the_wrong_shape_is_a_warning_not_a_crash(doc):
    cfg = settings.load(("run", doc))
    assert cfg.warnings
    assert cfg.value("max_frequency_hz") == settings.BOARD_DEFAULTS["max_frequency_hz"]


def test_a_deeply_nested_settings_file_is_refused_cleanly():
    # An ordinary error, which the sidecar reader turns into a note.
    with pytest.raises((RecursionError, ValueError, yaml.YAMLError)):
        settings.parse_document(DEEP)


# ---- project and job files -------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "[1, 2, 3]",
    '{"net_settings": ["Default"]}',
    '{"net_settings": {"classes": ["Default"]}}',
    '{"net_settings": {"classes": [{"name": "HS", "track_width": "wide"}]}}',
    DEEP,
])
def test_an_unreadable_project_file_means_no_netclasses(text):
    out = netclass.parse_project(text)
    assert not out.available
    assert out.warnings and "netclasses unavailable" in out.warnings[0]


@pytest.mark.parametrize("text", ["[]", '{"MaterialStackup": 3}', '{"FilesAttributes": ["x"]}', DEEP, "{"])
def test_an_unreadable_job_file_is_no_job_file(text):
    warnings: list[str] = []
    entries, total = reader._stackup_from_job(text, ["F.Cu", "B.Cu"], warnings)
    assert total == 1.6 and entries
    assert any("stackup was invented" in w for w in warnings)


def test_a_job_file_with_a_bad_thickness_keeps_the_rest():
    job = json.dumps({"MaterialStackup": [
        {"Type": "Copper", "Name": "F.Cu", "Thickness": "thick"},
        {"Type": "Dielectric", "Name": "core", "Thickness": 1.5},
        {"Type": "Copper", "Name": "B.Cu", "Thickness": -3},
    ]})
    warnings: list[str] = []
    entries, total = reader._stackup_from_job(job, ["F.Cu", "B.Cu"], warnings)
    assert [e.thickness_mm for e in entries] == [0.0, 1.5, 0.0]
    assert total == 1.5


# ---- the run parameter -----------------------------------------------------------------

class _Client:
    def __init__(self, data: bytes):
        self.data = data

    def run_input(self, token):
        return {"input_url": "board"}

    def download(self, url):
        return self.data

    def progress(self, token, stage, pct, message="", **fields):
        pass

    def upload_artifact(self, token, name, blob, content_type):
        return {"name": name, "key": f"runs/r1/{name}", "size_bytes": len(blob)}


@pytest.mark.parametrize("value", ["1GHz", -5, "0"])
def test_a_bad_run_frequency_fails_the_run_with_a_message(tmp_path, value):
    ctx = StageContext(
        client=_Client((FIXTURES / "tiny.kicad_pcb").read_bytes()),
        token=RunToken(token="t", run_id="r1", jti="j", expires_in=3600),
        run={"params": {"max_frequency_hz": value}},
        workdir=str(tmp_path), cores=1, max_cells=10**9, should_stop=lambda: False,
    )
    with pytest.raises(StageError, match="max_frequency_hz must be"):
        run_ingest(ctx)


def test_the_ingest_worker_reports_the_hash_it_computed(tmp_path):
    """The server records this hash, not the uploader's claim, for deduplication."""
    import hashlib

    data = (FIXTURES / "tiny.kicad_pcb").read_bytes()
    ctx = StageContext(
        client=_Client(data),
        token=RunToken(token="t", run_id="r1", jti="j", expires_in=3600),
        run={"params": {}},
        workdir=str(tmp_path), cores=1, max_cells=10**9, should_stop=lambda: False,
    )
    result = run_ingest(ctx)
    assert result.board["content_sha256"] == hashlib.sha256(data).hexdigest()
