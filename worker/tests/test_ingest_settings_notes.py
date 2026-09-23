"""What rules.json tells the app about the settings a board ran with, and why.

The app shows which rules file was applied, every value with where it came from, and edits a
layer of its own on top ("run"). It can only do that honestly if rules.json carries the
settings both with and without that layer, and says plainly when a rules file was refused.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from emi_worker.client import RunToken
from emi_worker.kicad import parse, parse_board
from emi_worker.rules import settings
from emi_worker.stages import StageContext
from emi_worker.stages.ingest import run_ingest

FIXTURES = Path(__file__).parent / "fixtures"
TINY = (FIXTURES / "tiny.kicad_pcb").read_bytes()


class _Client:
    def __init__(self, data: bytes):
        self.data = data
        self.artifacts: dict[str, bytes] = {}

    def run_input(self, token):
        return {"input_url": "board"}

    def download(self, url):
        return self.data

    def progress(self, token, stage, pct, message="", **fields):
        pass

    def upload_artifact(self, token, name, blob, content_type):
        self.artifacts[name] = blob
        return {"name": name, "key": f"runs/r1/{name}", "size_bytes": len(blob)}


def _ingest(tmp_path, data: bytes, params: dict | None = None) -> dict:
    client = _Client(data)
    run_ingest(StageContext(
        client=client,
        token=RunToken(token="t", run_id="r1", jti="j", expires_in=3600),
        run={"params": params or {}},
        workdir=str(tmp_path), cores=1, max_cells=10**9, should_stop=lambda: False,
    ))
    return json.loads(client.artifacts["rules.json"])


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_a_bare_board_runs_on_defaults_and_says_so(tmp_path):
    rules = _ingest(tmp_path, TINY)
    s = rules["settings"]
    assert s["file"] == "" and s["file_error"] == ""
    assert s["applied"]["rules"]["radiator"]["params"]["wavelength_fraction"] == {
        "value": 0.05, "source": "default"}
    assert s["applied"] == s["base"]


def test_the_rules_file_and_the_app_layer_are_told_apart(tmp_path):
    data = _zip({
        "p/board.kicad_pcb": TINY,
        "p/emi.rules.yaml": b"rules:\n  radiator:\n    params: {wavelength_fraction: 0.1}\n",
    })
    rules = _ingest(tmp_path, data, {"settings": {"rules": {
        "radiator": {"enabled": False}, "edge-proximity": {"severity": "info"},
    }}})
    s = rules["settings"]
    assert s["file"] == "emi.rules.yaml"
    assert "Rules read from emi.rules.yaml." in rules["notes"]

    applied, base = s["applied"]["rules"], s["base"]["rules"]
    assert applied["radiator"]["params"]["wavelength_fraction"] == {"value": 0.1, "source": "file"}
    assert applied["radiator"]["enabled"] is False
    assert applied["radiator"]["enabled_source"] == "run"
    assert applied["edge-proximity"]["severity_source"] == "run"
    # Underneath the app's layer: what the app shows once an edit is cleared.
    assert base["radiator"]["enabled"] is True and base["radiator"]["enabled_source"] == "default"
    assert base["radiator"]["params"]["wavelength_fraction"]["source"] == "file"
    assert not any(f["rule"] == "radiator" for f in rules["findings"])


def test_a_broken_rules_file_is_named_with_the_line_to_fix(tmp_path):
    data = _zip({"board.kicad_pcb": TINY, "emi.rules.yaml": b"rules:\n  radiator: [\n"})
    rules = _ingest(tmp_path, data)
    s = rules["settings"]
    assert s["file"] == "" and "line" in s["file_error"]
    note = next(n for n in rules["notes"] if n.startswith("emi.rules.yaml could not be read"))
    assert "built-in defaults" in note and "\n" not in note


def test_a_bad_severity_is_refused_not_stored():
    cfg = settings.load(("run", {"rules": {"radiator": {"severity": "loud"}}}))
    assert cfg.rule("radiator").severity == ""
    assert any(w.startswith("App settings: radiator.severity must be one of") for w in cfg.warnings)


def test_warnings_name_the_source_in_words():
    cfg = settings.load(("file", {"rules": {"no-such-rule": True}}))
    assert cfg.warnings == ["Rules file: unknown rule 'no-such-rule'"]


def test_custom_pads_are_one_warning_not_one_each():
    pads = "".join(
        f'(pad "{i}" smd custom (at {i} 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))'
        for i in range(1, 8)
    )
    text = TINY.decode().replace(
        '(footprint "TestPad:TH"', f'(footprint "X" (layer "F.Cu") (at 30 30 0) '
        f'(property "Reference" "U9" (at 0 0 0) (layer "F.SilkS")) {pads})\n  (footprint "TestPad:TH"',
    )
    model = parse_board(parse(text))
    odd = [w for w in model.warnings if "custom shape" in w]
    assert len(odd) == 1
    assert odd[0].startswith("7 pads with a custom shape (U9.1, U9.2, U9.3, U9.4, U9.5 and 2 more)")
    assert not any("\x00" in w for w in model.warnings)
