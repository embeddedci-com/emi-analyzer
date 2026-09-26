"""The public sample board: what "Try the sample board" shows a first-time user.

The board is drawn so each quick analysis has something to find (scripts/make_sample_board.py
says what and where). If a rule change makes one of these go quiet, the sample stops showing
that feature, so this pins the findings it was built for rather than an exact list.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from emi_worker import stackup, topology
from emi_worker.cables.attach import suggest_all
from emi_worker.client import RunToken
from emi_worker.kicad.normalize import board_extent, normalize
from emi_worker.rules.model import RuleContext
from emi_worker.stages import StageContext
from emi_worker.stages.ingest import load_board, run_ingest
from emi_worker.transient import lines

SAMPLE = Path(__file__).parent / "fixtures" / "sample.kicad_pcb"


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


def _findings() -> list[dict]:
    client = _Client(SAMPLE.read_bytes())
    ctx = StageContext(
        client=client,
        token=RunToken(token="t", run_id="r1", jti="j", expires_in=3600),
        run={"params": {}},
        workdir=tempfile.mkdtemp(), cores=1, max_cells=10**9, should_stop=lambda: False,
    )
    run_ingest(ctx)
    return json.loads(client.artifacts["rules.json"])["findings"]


def test_the_sample_board_shows_each_kind_of_finding():
    found = _findings()
    titles = [f["title"] for f in found]
    by_rule = {f["rule"] for f in found}

    assert {"plane-gap", "return-via", "decoupling", "esd-protection", "reset-filter",
            "input-filter", "radiator"} <= by_rule
    assert "/SPI_CLK crosses 1.9 mm of missing In1.Cu" in titles
    assert "/UART_TX reaches U1 before its ESD clamp D2" in titles
    assert "/UART_RX leaves the board at J2 with no ESD protection" in titles
    assert any(t.startswith("Layer change on /SPI_MOSI") for t in titles)
    assert any(t.startswith("U3.8 is") for t in titles)
    # Clean where it was drawn clean: the USB lines are clamped at the connector.
    assert not any("/USB_D" in t and "ESD" in t for t in titles)
    # Every finding but a board-wide summary has a place, so the viewer can mark it.
    assert all(f["x"] is not None and f["y"] is not None for f in found)
    assert sum(f["severity"] == "critical" for f in found) >= 1


def test_the_sample_board_has_lines_to_simulate_a_discharge_on():
    model, _, _ = load_board(SAMPLE.read_bytes())
    doc, _ = normalize(model, {"filename": SAMPLE.name, "key": "", "size_bytes": 0, "sha256": ""})
    planes = {layer["name"] for layer in doc["layers"] if layer.get("plane_net")}
    ctx = RuleContext(model=model, transform=board_extent(model), max_frequency_hz=1e9,
                      electrics=stackup.analyse(model, planes), topology=topology.build(model))
    found, _ = lines.exposed_lines(ctx)
    by_net = {line.net: line for line in found}

    for net in ("/USB_DP", "/USB_DN"):
        assert by_net[net].connector == "J1"
        assert by_net[net].clamp_ref == "D1"
        assert by_net[net].ic_pad.ref == "U1"
    assert by_net["/UART_TX"].clamp_ref == "D2"
    assert by_net["/UART_RX"].clamp_ref is None


def test_the_sample_board_suggests_a_usb_cable():
    model, _, _ = load_board(SAMPLE.read_bytes())
    suggestions = {s.ref: s for s in suggest_all(model)}
    assert set(suggestions) == {"J1", "J2"}
    assert "usb" in (suggestions["J1"].cable_id or "")


def _kicad_cli() -> str | None:
    found = shutil.which("kicad-cli")
    if found:
        return found
    for p in ("/opt/homebrew/bin/kicad-cli",
              "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"):
        if Path(p).exists():
            return p
    return None


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli is not installed")
def test_kicad_reads_the_sample_board(tmp_path):
    """The sample has to be a board KiCad itself opens, not only one this parser accepts."""
    done = subprocess.run(
        [_kicad_cli(), "pcb", "export", "gerbers", str(SAMPLE), "-o", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    # KiCad converts a zone it reads as the pre-6 fill format, and says so.
    assert "Legacy" not in done.stdout + done.stderr
    assert (tmp_path / "sample-In1_Cu.g1").exists()
