"""The compliance run: arithmetic on what other runs already produced (§16, §17).

The run reads everything that decides its answer -- the board, the solve's artifacts, the
attached driver -- through the run's input, and these tests hand it exactly that: a fake
server that serves the fixture board and synthetic solve artifacts with known numbers, so every
level can be checked by hand.

Three properties matter more than any number here:

* an incomplete estimate carries **no** margin or confidence key, not a margin with a warning;
* nothing the request says can make an estimate complete that the artifacts do not support;
* the spectrum is drawn anyway, because seeing which frequencies sit close is useful early.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from emi_worker.client import RunToken
from emi_worker.stages import STAGES, StageContext, StageError
from emi_worker.stages.compliance import run_compliance

BOARD = (Path(__file__).parent / "fixtures" / "tiny.kicad_pcb").read_bytes()

#: A 25 MHz, 50 % duty, 3.3 V clock behind 40 ohm, as the Drivers tab stores it.
CLOCK = {
    "format": "emi-driver", "version": 1, "name": "U1 clock", "net": "CLK",
    "kind": "trapezoid", "role": "signal",
    "trapezoid": {
        "amplitude_v": {"value": 3.3, "source": "scope"},
        "period_s": {"value": 4e-8, "source": "scope"},
        "pulse_width_s": {"value": 2e-8, "source": "scope"},
        "rise_s": {"value": 1.2e-9, "source": "datasheet"},
        "fall_s": {"value": 1.2e-9, "source": "datasheet"},
        "source_impedance_ohm": {"value": 40, "source": "assumed"},
    },
}


def log_grid(lo: float, hi: float, n: int = 60) -> list[float]:
    step = (hi / lo) ** (1 / (n - 1))
    return [lo * step ** k for k in range(n)]


def far_field(lo=30e6, hi=1e9, e_per_volt=1e-3, version=3) -> dict:
    fs = log_grid(lo, hi)
    doc = {
        "format": "emi-far-field", "format_version": version, "frequencies_hz": fs,
        "e_per_volt": [e_per_volt] * len(fs), "usable": [True] * len(fs),
        "z_in_real": [50.0] * len(fs), "z_in_imag": [0.0] * len(fs),
        "driven_by": "p1", "source_impedance_ohm": 50.0, "excited_ports": ["p1"],
        "distance_m": 3.0, "tenth_wavelength_above_hz": 1e9,
    }
    if version < 2:
        doc = {"format": "emi-far-field", "format_version": 1, "frequencies_hz": fs,
               "e_max_v_per_m": [1e-3] * len(fs)}
    return doc


MANIFEST = {"format_version": 2, "run": {
    "ports": [{"name": "p1", "resistance_ohm": 50.0, "excited": True}],
    "mesh_request": {"dx_um": 75, "dy_um": 75, "dz_um": 50},
}}


class FakeClient:
    """The server's side of the run input, with bytes served from memory."""

    def __init__(self, solve: dict | None, drivers: list | None = None, solve_error=None):
        self.blobs: dict[str, bytes] = {"board": BOARD}
        self.info: dict = {"input_url": "board"}
        if solve is not None:
            urls = {}
            for name, doc in solve.items():
                self.blobs[name] = json.dumps(doc).encode()
                urls[name] = name
            self.info["solve"] = {"run_id": "s1", "artifacts": urls}
        elif solve_error:
            self.info["solve"] = {"error": solve_error}
        self.info["drivers"] = drivers if drivers is not None else [
            {"id": "d1", "document": CLOCK}]
        self.uploads: dict[str, bytes] = {}
        self.progress_calls: list[tuple] = []

    def run_input(self, token):
        return self.info

    def download(self, url):
        return self.blobs[url]

    def progress(self, token, stage, pct, message="", **fields):
        self.progress_calls.append((stage, pct, message))

    def upload_artifact(self, token, name, blob, content_type):
        self.uploads[name] = blob
        return {"name": name, "size_bytes": len(blob)}


def _run(tmp_path, params, client) -> dict:
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


#: What the user alone can say: J1 on the fixture board carries no cable, and the product
#: runs from DC.
DECLARED = {"solve_run_id": "s1", "cable_assignments": {"J1": {"type": "none"}},
            "power": "dc", "enclosure": "plastic"}


def _solve(**over) -> dict:
    return {"manifest.json": MANIFEST, "farfield.json": far_field(), **over}


def test_it_is_a_registered_run_kind():
    assert STAGES["compliance"] is run_compliance


def test_every_worker_can_take_it(tmp_path):
    """No solver is involved, so a cable swap or a driver change must not queue behind one."""
    from emi_worker.config import Config, detect_capabilities

    caps = detect_capabilities(Config(
        server_url="http://server.invalid", api_key="eci_test", name="t",
        workdir=str(tmp_path)))
    assert "compliance" in caps.kinds


def test_a_complete_project_gets_a_margin_computable_by_hand(tmp_path):
    doc = _run(tmp_path, DECLARED, FakeClient(_solve()))["doc"]
    assert doc["gaps"] == [], doc["gaps"]
    assert doc["complete"] is True
    # The worst point is a harmonic of the 25 MHz clock, and its level is
    #   E = e_per_volt · |Z_s + Z_in| / |Z_d + Z_in| · V_rms(n)
    # with Z_s = 50, Z_in = 50, Z_d = 40, and V_rms the RMS of that harmonic.
    from emi_worker.drivers.spectrum import Trapezoid, trapezoid_series

    f = doc["worst"]["frequency_hz"]
    n = round(f * 4e-8)
    assert f == pytest.approx(n / 4e-8)
    v = trapezoid_series(Trapezoid(3.3, 4e-8, 2e-8, 1.2e-9, 1.2e-9), n)[-1][1]
    want = 1e-3 * (100.0 / 90.0) * v
    assert doc["worst"]["field_dbuv_per_m"] == pytest.approx(20 * math.log10(want / 1e-6),
                                                            abs=1e-9)
    assert 0.0 <= doc["confidence_uncalibrated"] <= 1.0
    assert doc["range_80_db"][0] < doc["margin_db"] < doc["range_80_db"][1]


def test_every_harmonic_in_band_is_driven_not_two_of_sixty(tmp_path):
    """Composition on the 60-point grid drove a harmonic only where one landed within
    100 ppm of a grid point. Evaluated at the harmonics, 30 MHz-1 GHz holds n = 2..40."""
    doc = _run(tmp_path, DECLARED, FakeClient(_solve()))["doc"]
    board = next(p for p in doc["paths"] if p["kind"] == "board")
    assert board["points"] == 39
    assert board["line_spectrum"] is True
    driven = [p for p in doc["spectrum"] if p["field_dbuv_per_m"] is not None]
    # A 50 % duty clock has no even harmonics: 3, 5, ..., 39 are the odd ones in band.
    assert len(driven) == 19


def test_only_the_uncalibrated_confidence_exists(tmp_path):
    out = _run(tmp_path, DECLARED, FakeClient(_solve()))
    doc = out["doc"]
    assert "confidence_uncalibrated" in doc
    assert "confidence" not in doc
    assert "confidence" not in out["result"].summary
    assert "confidence_uncalibrated" in out["result"].summary


def test_the_request_cannot_substitute_for_the_artifacts(tmp_path):
    """What the first version trusted from the request, sent by a client that wants a margin:
    none of it is read. The solve only reaches 500 MHz, and J1 is not declared."""
    liar = {"solve_run_id": "s1", "power": "dc", "enclosure": "plastic",
            "connectors": [], "driven_ports": [], "far_field_refs": ["p1"],
            "solved_f_max_hz": 0, "required_f_max_hz": 0, "power_entry_found": True,
            "paths": [{"kind": "board", "label": "x", "driver_id": "d",
                       "frequencies_hz": [100e6], "field_v_per_m": [1e-9]}]}
    doc = _run(tmp_path, liar,
               FakeClient(_solve(**{"farfield.json": far_field(hi=500e6)})))["doc"]
    assert doc["complete"] is False
    assert "margin_db" not in doc
    keys = {g["key"] for g in doc["gaps"]}
    assert "cable:J1" in keys, "the connector comes from the board, not the request"
    assert any(k.startswith("band:") for k in keys), "the band comes from the far field"
    assert all(p["label"] != "x" for p in doc["paths"])


def test_an_incomplete_project_carries_no_number_at_all(tmp_path):
    """§17.3. A warning is skippable; a missing field is not."""
    params = {**DECLARED, "cable_assignments": {}}
    doc = _run(tmp_path, params, FakeClient(_solve()))["doc"]
    assert doc["complete"] is False
    for key in ("margin_db", "confidence_uncalibrated", "sigma_db", "contributions"):
        assert key not in doc
    assert any(g["key"] == "cable:J1" for g in doc["gaps"])
    # ...and the spectrum is drawn anyway.
    assert doc["spectrum"]


def test_no_solve_says_so(tmp_path):
    client = FakeClient(None, solve_error="that solve is not in this project")
    doc = _run(tmp_path, DECLARED, client)["doc"]
    assert doc["complete"] is False
    assert doc["spectrum"] == []
    assert "no_paths" in doc
    assert any("not in this project" in g["message"] for g in doc["gaps"])


def test_no_driver_is_a_gap_and_no_level(tmp_path):
    doc = _run(tmp_path, DECLARED, FakeClient(_solve(), drivers=[]))["doc"]
    assert any(g["key"] == "no-driver" for g in doc["gaps"])
    assert doc["spectrum"] == []


def test_a_far_field_in_pulse_units_asks_for_a_rerun(tmp_path):
    doc = _run(tmp_path, DECLARED,
               FakeClient(_solve(**{"farfield.json": far_field(version=1)})))["doc"]
    assert any(g["key"] == "far-field-format" for g in doc["gaps"])
    assert "margin_db" not in doc


def test_a_ten_metre_or_conducted_standard_is_refused(tmp_path):
    for sid, match in (("fcc-15a-radiated-10m", "measured at 10 m"),
                       ("fcc-15b-conducted-qp", "conducted scan")):
        with pytest.raises(StageError, match=match):
            _run(tmp_path, {**DECLARED, "standard_id": sid}, FakeClient(_solve()))


def test_frequencies_outside_the_scan_are_not_scored(tmp_path):
    """A far field reaching below 30 MHz used to raise an uncaught LimitError."""
    doc = _run(tmp_path, DECLARED,
               FakeClient(_solve(**{"farfield.json": far_field(lo=10e6)})))["doc"]
    assert min(p["frequency_hz"] for p in doc["spectrum"]) >= 30e6


def test_detectors_are_named_per_point(tmp_path):
    doc = _run(tmp_path, {**DECLARED, "highest_frequency_hz": 300e6},
               FakeClient(_solve(**{"farfield.json": far_field(hi=2e9)})))["doc"]
    det = {p["detector"] for p in doc["spectrum"]}
    assert det == {"quasi-peak", "average"}


def test_a_stated_highest_frequency_can_only_raise_the_scan(tmp_path):
    """300 MHz in the product means a 2 GHz scan (§15.33(b)); the far field stops at 1 GHz."""
    doc = _run(tmp_path, {**DECLARED, "highest_frequency_hz": 300e6},
               FakeClient(_solve()))["doc"]
    assert doc["inputs"]["required_hz"][1] == pytest.approx(2e9)
    assert any(g["key"].startswith("band:") for g in doc["gaps"])
    low = _run(tmp_path, {**DECLARED, "highest_frequency_hz": 1e6}, FakeClient(_solve()))
    assert low["doc"]["inputs"]["required_hz"][1] == pytest.approx(1e9)


def _cable_solve() -> dict:
    """A 1 m cable on J1 with flat terms, on a grid the far field does not share."""
    fs = log_grid(30e6, 1e9, 48)
    return _solve(**{
        "ports.json": {"ports": [{"port": "p1", "dense": {
            "frequencies_hz": fs, "v_real": [50.0] * len(fs), "v_imag": [0.0] * len(fs),
            "i_real": [1.0] * len(fs), "i_imag": [0.0] * len(fs)}}]},
        "cable_ports.json": {"ports": [{"ref": "J1", "driven_by": "p1", "transfer": {
            "frequencies_hz": fs, "h_real": [0.01] * len(fs), "h_imag": [0.0] * len(fs),
            "usable": [True] * len(fs)}}]},
        "cable_antenna.json": {"cables": [{
            "ref": "J1", "cable_id": "usb2-shielded", "length_m": 1.0, "distance_m": 3.0,
            "frequencies_hz": fs, "z_real": [100.0] * len(fs), "z_imag": [0.0] * len(fs),
            "e_per_amp": [2.0] * len(fs)}]},
    })


def test_board_and_cable_from_one_driver_add_in_amplitude_at_every_harmonic(tmp_path):
    """The far field and the cable are on different grids. They used to meet nowhere, so the
    two paths of one driver were never added; at the harmonics they now always meet."""
    params = {**DECLARED, "cable_assignments": {}}
    doc = _run(tmp_path, params, FakeClient(_cable_solve()))["doc"]
    assert doc["complete"] is True, doc["gaps"]
    assert doc["shares_indicative"] is True
    labels = {c["label"] for c in doc["contributions"]}
    assert labels == {"board (solved region)", "cable J1 (usb2-shielded, 1 m)"}
    # Board: 1e-3 V/m/V. Cable: H/|Z_ant| * E/A = 0.01 / 100 * 2 = 2e-4 V/m/V. Both carry the
    # same source factor, so the sum is 1.2e-3 per volt: +1.58 dB over the board alone.
    board = next(c for c in doc["contributions"] if c["kind"] == "board")
    assert doc["worst"]["field_dbuv_per_m"] - board["field_dbuv_per_m"] == pytest.approx(
        20 * math.log10(1.2), abs=1e-6)


def test_a_declared_cable_the_solve_did_not_model_is_a_gap(tmp_path):
    params = {**DECLARED, "cable_assignments": {"J1": {"type": "usb2-shielded"}}}
    doc = _run(tmp_path, params, FakeClient(_solve()))["doc"]
    assert any(g["key"] == "cable-solve:J1" for g in doc["gaps"])


def test_the_budget_shows_its_own_working(tmp_path):
    doc = _run(tmp_path, DECLARED, FakeClient(_solve()))["doc"]
    assert doc["sigma_terms"], "a sigma a reader cannot interrogate is worse than no sigma"
    assert any(k.startswith("driver provenance") for k in doc["sigma_terms"])
    assert any(k.startswith("mesh preset (normal)") for k in doc["sigma_terms"])
    assert "placeholder" in doc["uncertainty_note"].lower()


def test_recommendations_come_from_findings_on_the_dominant_path(tmp_path):
    findings = [
        {"id": "plane-gap-1", "rule": "plane-gap", "severity": "critical",
         "title": "Gap under CLK", "detail": "d", "net": "CLK"},
        {"id": "switch-node-1", "rule": "switch-node", "severity": "critical",
         "title": "Big switch node", "detail": "d", "net": "SW"},
    ]
    doc = _run(tmp_path, {**DECLARED, "findings": findings}, FakeClient(_solve()))["doc"]
    recs = doc["recommendations"]
    assert [i["rule"] for i in recs["items"]] == ["plane-gap"]
    assert recs["general_only"] is False


def test_it_finishes_at_a_hundred_percent(tmp_path):
    out = _run(tmp_path, DECLARED, FakeClient(_solve()))
    assert out["client"].progress_calls[-1][0] == "done"
    assert out["client"].progress_calls[-1][1] == 100
