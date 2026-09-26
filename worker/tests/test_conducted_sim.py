"""Conducted emissions through the real ngspice: the checks docs/verification/conducted-emissions.md
reports. Skipped where ngspice is not installed; the worker image has it:

    docker run --rm -u 0 -v "$PWD":/src -w /src --entrypoint sh <worker image> \\
        -c "pip install -q pytest && python -m pytest -q tests/test_conducted_sim.py -s"

Each check compares with a closed form worked out here, not with another run of the same code:

  * the LISN's impedance against the CISPR 16-1-2 curve, inside the standard's tolerance;
  * a buck's input ripple, simulated in time, against I·D(1-D)/(f·C), and its first harmonic at
    the LISN against the current divider between the input capacitor and the two networks;
  * an LC filter's attenuation against the same divider with the filter in it.
"""

from __future__ import annotations

import cmath
import math

import numpy as np
import pytest

from emi_worker.conducted import lisn, network, scan, sources
from emi_worker.conducted.network import Network, Series, Shunt
from emi_worker.transient import ngspice

pytestmark = pytest.mark.skipif(not ngspice.available(), reason="ngspice is not installed")

F_SW = 500e3
C_IN = 10e-6
ESR = 1e-3
I_IN = 1.0
DUTY = 0.3


def _db(x: float) -> float:
    return 20 * math.log10(x)


def _buck(ripple: float = 0.0) -> sources.RegulatorSource:
    s = sources.from_params("U1", {"frequency_hz": F_SW, "input_current_a": I_IN, "duty": DUTY,
                                   "rise_s": 10e-9}, None)
    s.ripple = ripple
    return s


def _zc(f: float, c: float = C_IN, esr: float = ESR, esl: float = 0.0) -> complex:
    w = 2 * math.pi * f
    return complex(esr, w * esl - 1 / (w * c))


def _to_lisn(f: float, z_node: complex, i: complex = 1.0) -> float:
    """|V| at one LISN for current i leaving a node with z_node to ground, the networks in series."""
    zl = lisn.network_impedance(f)
    i_loop = i * z_node / (z_node + 2 * zl)
    return abs(i_loop * zl)


# ---- the LISN ------------------------------------------------------------------------------

def test_lisn_impedance_is_inside_the_cispr_tolerance():
    deck = "\n".join([
        "* lisn impedance",
        *lisn.lines("k", "p", "k_eut"),
        "Ik 0 k_eut DC 0 AC 1",
        ".ac dec 50 150k 30meg",
        ".save v(k_eut)",
        ".end",
    ]) + "\n"
    r = ngspice.run_ac(deck)
    worst_mag = worst_phase = worst_closed = 0.0
    for f, z in zip(r.frequency, r["v(k_eut)"]):
        ideal = lisn.ideal_impedance(f)
        worst_mag = max(worst_mag, abs(abs(z) / abs(ideal) - 1))
        worst_phase = max(worst_phase, abs(math.degrees(cmath.phase(z) - cmath.phase(ideal))))
        worst_closed = max(worst_closed, abs(z / lisn.network_impedance(f) - 1))
    print(f"\nLISN: |Z| within {worst_mag * 100:.1f} %, phase within {worst_phase:.1f}°, "
          f"closed form within {worst_closed * 100:.3f} %")
    assert worst_mag < lisn.TOLERANCE
    assert worst_phase < lisn.PHASE_TOLERANCE_DEG
    assert worst_closed < 1e-3


# ---- a buck's input ------------------------------------------------------------------------

def _pwl_periodic(src: sources.RegulatorSource, periods: int) -> str:
    """The source's waveform, zero-mean, repeated. Zero mean because the networks here have no DC
    supply behind them: the DC current would otherwise ramp the LISN inductors for milliseconds."""
    t, i, period = src.waveform()
    mean = abs(sources.piecewise_linear_series(t, i, period, 0))
    # Zero at t = 0: the operating point is solved with the source's value there, and -mean
    # pushed through the 10 MΩ DC paths is ten megavolts, every later sample lost in rounding.
    pts = [(0.0, 0.0)]
    for k in range(periods):
        for tt, ii in zip(t, i):
            pts.append((k * period + tt + (1e-12 if k == 0 and tt == 0 else 0.0), ii - mean))
        pts.append((k * period + period * 0.999999, -mean))
    body = " ".join(f"{a:.9g} {b:.6g}" for a, b in pts)
    return body


def test_buck_input_ripple_and_first_harmonic_match_closed_form():
    src = _buck()
    periods = 60
    deck = "\n".join([
        "* buck input",
        *lisn.lines("k", "p", "k_in"),
        *lisn.lines("k", "n", "k_gnd"),
        f"Rk_esr k_in k_c {ESR}",
        f"Ck_in k_c k_gnd {C_IN}",
        f"Ik k_in k_gnd PWL({_pwl_periodic(src, periods)})",
        ".options interp",
        f".tran 2n {periods / F_SW:.9g} 0 2n",
        ".save v(k_in) v(k_gnd)",
        ".end",
    ]) + "\n"
    r = ngspice.run(deck, expect_end_s=periods / F_SW)
    t = r.time
    v_cap = r["v(k_in)"] - r["v(k_gnd)"]
    period = 1 / F_SW
    # The networks and the input capacitor ring at about 5 kHz from the moment the source starts,
    # hardly damped, and over one 2 µs period that is a straight line of about 0.2 V: more than
    # the ripple. The switching ripple repeats every period, so the line through the period's two
    # ends is the ringing, and what is left is the ripple.
    grid = np.linspace((periods - 1) * period, periods * period, 2001)
    v = np.interp(grid, t, v_cap)
    v = v - np.linspace(v[0], v[-1], len(v))
    ripple = float(v.max() - v.min())
    i_pk = I_IN / DUTY
    closed = i_pk * DUTY * (1 - DUTY) / (F_SW * C_IN) + i_pk * ESR
    print(f"\nripple: {ripple * 1e3:.2f} mV p-p, closed form {closed * 1e3:.2f} mV, "
          f"{_db(ripple / closed):+.2f} dB")
    assert abs(_db(ripple / closed)) < 1.0

    # The first harmonic at the LISN three ways: the scan's AC pipeline, the closed-form
    # divider, and a Fourier transform of the time-domain run over its last 50 periods.
    net = Network()
    net.shunts.append(Shunt("in", "C1", "10uF", C_IN, ESR, 0.0))
    net.sources["U1"] = "in"
    got = scan.run(net, [src], "B")["variants"][0]["lines"][0]
    i1 = src.harmonics(1, 1)[0][2]
    closed_v1 = _to_lisn(F_SW, _zc(F_SW), i1)
    window = t >= (periods - 50) * period
    grid = np.linspace((periods - 50) * period, periods * period, 50 * 400, endpoint=False)
    vp = np.interp(grid, t[window], r["v(k_in)"][window])
    # A Hann window, whose leakage falls fast enough that the same ringing, 50 bins below, does
    # not reach the switching frequency's bin; its coherent gain of one half is divided out.
    spectrum = np.fft.rfft(vp * np.hanning(len(grid))) / (len(grid) * 0.5)
    fft_v1 = 2 * abs(spectrum[50]) / math.sqrt(2)
    print(f"first harmonic: scan {got['dbuv']:.2f} dBµV, closed form {lisn.dbuv(closed_v1):.2f}, "
          f"transient FFT {lisn.dbuv(fft_v1):.2f}")
    assert got["f_hz"] == pytest.approx(F_SW)
    assert abs(got["dbuv"] - lisn.dbuv(closed_v1)) < 1.0
    assert abs(got["dbuv"] - lisn.dbuv(fft_v1)) < 1.0


# ---- an ideal filter -------------------------------------------------------------------------

def test_an_lc_filter_attenuates_as_the_closed_form_says():
    l_f, c_f = 10e-6, 10e-6
    bare = Network()
    bare.shunts.append(Shunt("in", "C1", "10uF", C_IN, ESR, 0.0))
    bare.sources["U1"] = "in"
    filtered = bare.insert_after_entry(Series("", "", l_f, 0.0, "added"))
    filtered.shunts.append(Shunt("in", "C2", "10uF", c_f, ESR, 0.0))
    # A 150 kHz comb, so the harmonics cover the band.
    first, last = 1, 200
    deck, saves = network.build([("a", bare, "U1"), ("b", filtered, "U1")], 150e3, first, last)
    r = ngspice.run_ac(deck)
    worst = 0.0
    for idx, f in enumerate(r.frequency):
        sim = _db(abs(r[saves["a"]["v_p"]][idx]) / abs(r[saves["b"]["v_p"]][idx]))
        z1, z2 = _zc(f), _zc(f, c_f)
        zl2 = 2 * lisn.network_impedance(f)
        zb = z2 * zl2 / (z2 + zl2)
        v_with = abs(z1 / (z1 + 1j * 2 * math.pi * f * l_f + zb) * zb / zl2 * lisn.network_impedance(f))
        closed = _db(_to_lisn(f, z1) / v_with)
        worst = max(worst, abs(sim - closed))
    at_1mhz = _db(abs(r[saves["a"]["v_p"]][5]) / abs(r[saves["b"]["v_p"]][5]))
    print(f"\nLC filter: within {worst:.4f} dB of closed form from 150 kHz to 30 MHz; {at_1mhz:.1f} dB at 900 kHz")
    assert worst < 0.1
    # Well above its 16 kHz corner, 10 µH into 10 µF behind 100 Ω of networks is
    # |1 - ω²LC| ≈ ω²LC: 70 dB at 900 kHz.
    w = 2 * math.pi * 900e3
    assert at_1mhz == pytest.approx(_db(w * w * l_f * c_f - 1), abs=1.0)


# ---- the scan end to end ---------------------------------------------------------------------

def test_the_scan_ranks_what_ifs_and_names_the_capacitor_doing_the_work():
    net = Network()
    net.series.append(Series("in", "n1", 20e-9))
    net.shunts.append(Shunt("n1", "C1", "10uF", 10e-6, 0.005, 1e-9, 0.5e-9))
    net.series.append(Series("n1", "n2", 2e-9))
    net.shunts.append(Shunt("n2", "C2", "100nF", 100e-9, 0.02, 0.4e-9, 0.5e-9))
    net.sources["U1"] = "n2"
    out = scan.run(net, [_buck(0.3)], "B")
    assert out["worst"]["margin_db"] < 0, "a 1 A buck behind 10 µF fails class B"
    assert out["dominant"]["shares"][0]["ref"] == "C1"
    assert out["suggestions"][0]["id"] == "lc_filter" and out["suggestions"][0]["change_db"] > 20
    by_ref = {c["ref"]: c["without_change_db"] for c in out["components"]}
    assert by_ref["C1"] < by_ref["C2"] <= 0.5


# ---- the run, from an uploaded board -----------------------------------------------------------

class _Client:
    def __init__(self, board: bytes):
        self.board = board
        self.uploads: dict[str, bytes] = {}

    def run_input(self, token):
        return {"input_url": "board"}

    def download(self, url):
        return self.board

    def progress(self, token, stage, pct, message="", **fields):
        pass

    def upload_artifact(self, token, name, blob, content_type):
        self.uploads[name] = blob
        return {"name": name, "size_bytes": len(blob)}


def _stage(tmp_path, params: dict) -> tuple[dict, dict]:
    import json

    from emi_worker.client import RunToken
    from emi_worker.stages import STAGES, StageContext

    from .test_conducted import FIXTURE

    client = _Client(FIXTURE.read_bytes())
    ctx = StageContext(client=client, token=RunToken(token="t", run_id="r1", jti="j", expires_in=3600),
                       run={"params": params}, workdir=str(tmp_path), cores=1, max_cells=10**9,
                       should_stop=lambda: False)
    result = STAGES["conducted"](ctx)
    return json.loads(client.uploads["conducted.json"]), result.summary


def test_a_scan_of_the_fixture_board_marks_what_it_assumed(tmp_path):
    doc, summary = _stage(tmp_path, {})
    assert summary["regulators"] == 1 and summary["assumed"] is True
    reg = doc["regulators"][0]
    assert reg["ref"] == "U1" and set(reg["assumed"]) == {"frequency_hz", "input_current_a", "duty", "rise_s"}
    assert reg["params"]["duty"] == {"value": 0.275, "source": "rail names", "assumed": True}
    laid = doc["variants"][0]
    assert laid["id"] == "as_laid_out" and len(laid["lines"]) == 60  # 500 kHz to 30 MHz
    assert doc["worst"]["margin_db"] == summary["worst_margin_db"]
    print(f"\nfixture board, assumed 500 kHz / 0.5 A: worst {doc['worst']}")

    given = {"class": "A", "regulators": {"U1": {"frequency_hz": 2e6, "input_current_a": 0.3,
                                                  "duty": 0.3, "rise_s": 5e-9}}}
    doc2, summary2 = _stage(tmp_path, given)
    assert summary2["assumed"] is False and doc2["regulators"][0]["assumed"] == []
    assert doc2["standard"]["average"] == "fcc-15a-conducted-avg"
    assert doc2["variants"][0]["lines"][0]["f_hz"] == 2e6


def test_a_setting_out_of_range_fails_the_run_with_the_reason(tmp_path):
    from emi_worker.stages import StageError

    with pytest.raises(StageError, match="frequency_hz"):
        _stage(tmp_path, {"regulators": {"U1": {"frequency_hz": 5}}})
