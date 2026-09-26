"""The decoupling view: the lumped PDN model (rules/pdn.py) and what it reports per IC.

The numbers here are the ones docs/verification/decoupling.md quotes. A test that fails moved
a number in that document, so update both or neither.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from emi_worker.kicad.board import StackupLayer, ZonePolygon
from emi_worker.rules import decoupling_view as dv
from emi_worker.rules import pdn
from emi_worker.rules.settings import load

from .test_layout_checks import board, ctx_for, pad, square, via

MIL = 0.0254
F0402 = "Capacitor_SMD:C_0402_1005Metric"


def db(a: float, b: float) -> float:
    return 20.0 * math.log10(a / b)


# ---- the model against closed forms --------------------------------------------------------

def test_two_cap_anti_resonance_matches_the_closed_form():
    """The criterion: peak |Z| within 0.5 dB and its frequency within 2 %: the closed form
    puts the peak where the reactances cancel, and ESR moves the true maximum a little."""
    bulk = pdn.Branch("C1", c_f=10e-6, l_h=2e-9, r_ohm=0.01)
    small = pdn.Branch("C2", c_f=100e-9, l_h=1.5e-9, r_ohm=0.03)
    f_closed, z_closed = pdn.closed_form_two_cap_peak(bulk, small)
    peaks = pdn.anti_resonances(pdn.Network([bulk, small]), pdn.frequencies())
    assert len(peaks) == 1
    f_peak, z_peak = peaks[0]
    assert abs(db(z_peak, z_closed)) < 0.5
    assert f_peak == pytest.approx(f_closed, rel=0.02)
    # And the absolute values docs/verification quotes.
    assert f_closed == pytest.approx(8.55e6, rel=0.01)
    assert z_closed == pytest.approx(0.291, rel=0.01)


def test_identical_capacitors_divide_esr_and_keep_their_resonance():
    one = pdn.Branch("C", c_f=100e-9, l_h=1e-9, r_ohm=0.05)
    bank = pdn.Network([pdn.Branch(f"C{i}", 100e-9, 1e-9, 0.05) for i in range(4)])
    srf = one.srf_hz
    z = bank.mag(np.array([srf]))[0]
    assert z == pytest.approx(0.05 / 4, rel=1e-6)


# Archambeault, Connor, Steffka, "Inductance: the misconceptions, myths and truth", In
# Compliance Magazine, Table 1 (0402/0603/0805 minimum mounts) and Table 2 (0402 at 50 mil
# from pad to via). Distance from board to planes in mils -> connection inductance in nH.
ARCHAMBEAULT = {
    ("0805 min", 148): [1.2, 1.8, 2.2, 2.5, 2.8, 3.1, 3.4, 3.6, 3.9, 4.2],
    ("0603 min", 128): [1.1, 1.6, 1.9, 2.2, 2.5, 2.7, 3.0, 3.2, 3.5, 3.7],
    ("0402 min", 106): [0.9, 1.3, 1.6, 1.9, 2.1, 2.3, 2.6, 2.8, 3.0, 3.2],
    ("0402 50 mil", 166): [1.4, 2.0, 2.4, 2.8, 3.1, 3.5, 3.7, 4.0, 4.3, 4.6],
}


def _connection_errors():
    out = []
    for (name, spacing), values in ARCHAMBEAULT.items():
        for i, published in enumerate(values):
            depth = (i + 1) * 10
            got = pdn.connection_nh(spacing * MIL, depth * MIL, 5 * MIL)
            out.append((name, depth, published, got, got / published - 1.0))
    return out


def test_connection_inductance_matches_the_published_table():
    """Criterion: every entry within 25 %, and within 10 % at the 20 to 40 mil depths real
    four- and six-layer boards put their first plane at."""
    errors = _connection_errors()
    assert len(errors) == 40
    assert max(abs(e[4]) for e in errors) < 0.25
    typical = [e for e in errors if 20 <= e[1] <= 40]
    assert max(abs(e[4]) for e in typical) < 0.10
    # The 0402 row docs/verification quotes: 0402 minimum mount, 10 mil to the planes.
    got = pdn.connection_nh(106 * MIL, 10 * MIL, 5 * MIL)
    assert got == pytest.approx(0.75, abs=0.01)


def test_plane_capacitance_matches_the_rule_of_thumb():
    """Bogatin's C[pF] = 0.225 eps_r A[in^2] / h[in]: 1 in^2 at 4 mil, eps_r 4 -> 225 pF."""
    c = pdn.plane_capacitance_f(25.4 ** 2, 4 * MIL, 4.0)
    assert c * 1e12 == pytest.approx(225.0, rel=0.005)


def test_a_second_ground_via_halves_one_leg_of_the_loop():
    one = pdn.connection_nh(2.0, 0.2, 0.15, ground_vias=1)
    two = pdn.connection_nh(2.0, 0.2, 0.15, ground_vias=2)
    assert two == pytest.approx(one * 0.75)


def test_textbook_pdn_landmarks():
    """One bulk, four 100 nF and a 10 nF on a 50 x 50 mm plane pair, 0.1 mm of FR-4.

    Not transcribed from a book: checked against the closed-form landmarks every textbook
    PDN curve is built from, each to the stated tolerance.
    """
    plane_c = pdn.plane_capacitance_f(50 * 50, 0.1, 4.4)
    lp = pdn.plane_branch_nh(50 * 50, 0.1, 0.15) * 1e-9
    branches = (
        [pdn.Branch("bulk", 10e-6, 3e-9, 0.015)]
        + [pdn.Branch(f"C{i}", 100e-9, 1.5e-9, 0.055) for i in range(4)]
        + [pdn.Branch("C10n", 10e-9, 1.3e-9, 0.12)]
        + [pdn.Branch("plane", plane_c, lp, 0.02 / (1 / math.sqrt(lp * plane_c) * plane_c),
                      kind="plane")]
    )
    net = pdn.Network(branches)
    f = pdn.frequencies()
    # 1. Plane capacitance: 0.97 nF, within 0.5 % of eps0 eps_r A / t.
    assert plane_c == pytest.approx(0.974e-9, rel=0.005)
    # 2. Low frequency: all the capacitance in parallel, within 1 %.
    total_c = sum(b.c_f for b in branches)
    z_low = net.mag(np.array([1e5]))[0]
    assert z_low == pytest.approx(1 / (2 * math.pi * 1e5 * total_c), rel=0.01)
    # 3. The 100 nF bank's dip: a quarter of one part's ESR, within 1 dB (its neighbours
    #    pull it a little).
    bank_srf = 1 / (2 * math.pi * math.sqrt(1.5e-9 * 100e-9))
    z_bank = net.mag(np.array([bank_srf]))[0]
    assert abs(db(z_bank, 0.055 / 4)) < 1.0
    # 4. The anti-resonance between the bulk and the bank, against the closed form of the
    #    two as single branches, within 0.5 dB.
    bank = pdn.Branch("bank", 400e-9, 1.5e-9 / 4, 0.055 / 4)
    f_closed, z_closed = pdn.closed_form_two_cap_peak(branches[0], bank)
    peak = min(pdn.anti_resonances(net, f), key=lambda p: abs(math.log(p[0] / f_closed)))
    assert abs(db(peak[1], z_closed)) < 0.5
    assert peak[0] == pytest.approx(f_closed, rel=0.02)


def test_gaps_are_the_ranges_above_the_target():
    f = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    z = np.array([0.1, 0.5, 0.6, 0.1, 0.5])
    assert pdn.gaps(f, z, 0.3) == [(2.0, 3.0), (5.0, 5.0)]


# ---- names ---------------------------------------------------------------------------------

@pytest.mark.parametrize("net,v", [
    ("+3V3", 3.3), ("VCC_1V8", 1.8), ("+5V", 5.0), ("/Power/+3.3V", 3.3), ("VDD", None),
    ("+12V", 12.0),
])
def test_rail_voltage_from_the_name(net, v):
    assert dv.rail_voltage(net) == v


@pytest.mark.parametrize("text,hz", [
    ("16MHz", 16e6), ("32.768kHz", 32768.0), ("CLK_25MHZ", 25e6), ("16M", None), ("", None),
])
def test_frequencies_need_an_explicit_hz(text, hz):
    assert dv.parse_hz(text) == hz


# ---- the view on a board -------------------------------------------------------------------

def _cap(ref, value, x, y, fp=F0402, rail="+3V3"):
    a = pad(ref, "1", rail, x, y, value=value)
    b = pad(ref, "2", "GND", x + 1, y, value=value)
    a.footprint = b.footprint = fp
    return [a, b]


def _two_layer():
    """U1 on +3V3 with a 100 nF beside it, a 10 nF 8 mm away and a 10 uF 18 mm away, a
    16 MHz crystal on one of its pins, and ground poured on the bottom."""
    m = board()
    m.pads = [
        pad("U1", "1", "+3V3", 10, 10), pad("U1", "2", "GND", 10, 11),
        pad("U1", "3", "XIN", 10, 12),
        pad("Y1", "1", "XIN", 12, 14, value="16MHz"), pad("Y1", "2", "XOUT", 13, 14, value="16MHz"),
    ]
    m.pads += (_cap("C1", "100nF", 11.5, 10) + _cap("C2", "10nF", 18, 10)
               + _cap("C3", "10uF", 25, 20, "Capacitor_SMD:C_0805_2012Metric"))
    m.vias = [via(12.7, 10.3), via(19.2, 10.3)]
    m.zones = [ZonePolygon("B.Cu", "GND", square(0, 0, 40, 40))]
    return m


def test_every_ic_on_every_rail_gets_a_curve():
    d = dv.build(ctx_for(_two_layer()))
    assert [r["net"] for r in d["rails"]] == ["+3V3"]
    rail = d["rails"][0]
    assert rail["v"] == 3.3 and rail["v_assumed"] is False
    ic = rail["ics"][0]
    assert ic["ref"] == "U1"
    assert [c["ref"] for c in ic["caps"]] == ["C1", "C2", "C3"]
    assert all(p["source"] == "library" for p in rail["parts"].values())
    # Target: 3.3 V x 5 % / 0.5 A.
    assert ic["target_ohm"] == pytest.approx(0.33)
    assert ic["noise"] == [{"hz": 16e6, "source": "Y1 16MHz"}]
    assert "lumped model" in d["note"]


def test_the_far_bulk_capacitor_makes_the_worst_gap_and_the_fix_is_ranked_first():
    ic = dv.build(ctx_for(_two_layer()))["rails"][0]["ics"][0]
    assert ic["status"] == "gaps"
    # The 10 uF behind 20 nH of loop resonates against the 100 nF near 3 MHz.
    assert 2e6 < ic["worst"]["hz"] < 5e6
    assert any(2e6 < p["hz"] < 5e6 for p in ic["anti_resonances"])
    recs = ic["recommendations"]
    assert 1 <= len(recs) <= dv.MAX_RECOMMENDATIONS
    assert recs[0]["improvement_db"] > 10
    kinds = {r["kind"] for r in recs}
    assert "move" in kinds and "add" in kinds
    move = next(r for r in recs if r["kind"] == "move")
    assert move["text"] == "Move C3 to within 3 mm of U1.1"
    # Every what-if carries what it changes, so the browser can redraw it.
    assert move["change"]["op"] == "replace" and move["change"]["branch"]["id"] == "C3"


def test_a_well_decoupled_ic_is_ok():
    m = board()
    m.pads = [pad("U1", "1", "+3V3", 10, 10), pad("U1", "2", "GND", 10, 11)]
    for i, x in enumerate((11.2, 11.2, 8.8, 8.8)):
        m.pads += _cap(f"C{i + 1}", "1uF", x, 10 + (i % 2) * 1.2, "Capacitor_SMD:C_0402_1005Metric")
    m.vias = [via(12.5, 10.2), via(12.5, 11.4), via(10.1, 10.2), via(10.1, 11.4)]
    m.zones = [ZonePolygon("B.Cu", "GND", square(0, 0, 40, 40))]
    # A small IC: a 0.1 A step leaves 1.65 ohm to work with.
    cfg = load(("run", {"rules": {"decoupling": {"params": {"step_current_a": 0.1}}}}))
    ctx = ctx_for(m)
    ctx.settings = cfg
    ic = dv.build(ctx)["rails"][0]["ics"][0]
    assert ic["target_ohm"] == pytest.approx(1.65)
    assert ic["status"] == "ok", ic["gaps"]
    assert ic["recommendations"] == []


def test_an_unreadable_part_is_modelled_and_says_it_was_assumed():
    m = _two_layer()
    for p in m.pads:
        if p.ref == "C1":
            p.value, p.footprint = "", "Custom:CAP_WEIRD"
    c1 = dv.build(ctx_for(m))["rails"][0]["parts"]["C1"]
    assert c1["source"] == "assumed"
    assert c1["assumed"] == ["value", "ESL", "ESR"]
    assert c1["c_f"] == pytest.approx(100e-9)


def _four_layer_with_planes():
    m = board(layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    m.stackup = [
        StackupLayer("F.Cu", "copper", 0.035), StackupLayer("p1", "prepreg", 0.2, epsilon_r=4.2),
        StackupLayer("In1.Cu", "copper", 0.035), StackupLayer("c", "core", 1.065, epsilon_r=4.5),
        StackupLayer("In2.Cu", "copper", 0.035), StackupLayer("p2", "prepreg", 0.2, epsilon_r=4.2),
        StackupLayer("B.Cu", "copper", 0.035),
    ]
    m.zones = [ZonePolygon("In1.Cu", "GND", square(0, 0, 40, 40)),
               ZonePolygon("In2.Cu", "+3V3", square(0, 0, 40, 40))]
    m.pads = [pad("U1", "1", "+3V3", 10, 10), pad("U1", "2", "GND", 10, 11)]
    m.pads += _cap("C1", "100nF", 12, 10) + _cap("C2", "100nF", 30, 30)
    m.vias = [via(12, 9.4, "+3V3"), via(13, 9.4), via(30, 29.4, "+3V3"), via(31, 29.4),
              via(10, 9.4, "+3V3")]
    return m


def test_a_plane_pair_adds_its_capacitance_and_its_first_resonance():
    d = dv.build(ctx_for(_four_layer_with_planes()))
    plane = d["rails"][0]["plane"]
    assert plane["layer"] == "In2.Cu" and plane["ground_layer"] == "In1.Cu"
    assert plane["cavity_mm"] == pytest.approx(1.1, abs=0.01)
    # eps0 * 4.5 * 1600 mm^2 / 1.1 mm.
    assert plane["c_f"] == pytest.approx(57.9e-12, rel=0.01)
    # c / (2 * 40 mm * sqrt(4.5)).
    assert plane["resonance_hz"] == pytest.approx(1.767e9, rel=0.01)
    assert "first is near 1.77 GHz" in d["note"]
    ic = d["rails"][0]["ics"][0]
    assert [c["ref"] for c in ic["caps"]] == ["C1", "C2"]
    assert ic["plane_branch"]["kind"] == "plane"
    assert ic["series_l_h"] > 0
    # Through the planes the far capacitor is barely worse than the near one: that is what a
    # plane pair is for.
    c1, c2 = ic["caps"]
    assert c2["mount_l_h"] < 2 * c1["mount_l_h"]


def test_it_is_fast():
    """Ingest runs it on every upload. Budget: well under a second for a busy rail."""
    import time
    m = board(size=100.0)
    m.pads = []
    for u in range(10):
        m.pads += [pad(f"U{u + 1}", "1", "+3V3", 5 + 9 * u, 50), pad(f"U{u + 1}", "2", "GND", 5 + 9 * u, 51)]
    for c in range(40):
        m.pads += _cap(f"C{c + 1}", ("100nF", "10nF", "1uF", "10uF")[c % 4], 3 + 2.4 * c, 45)
    m.zones = [ZonePolygon("B.Cu", "GND", square(0, 0, 100, 100))]
    t = time.perf_counter()
    d = dv.build(ctx_for(m))
    assert len(d["rails"][0]["ics"]) == 10
    assert time.perf_counter() - t < 1.0


def test_the_webapp_fixture_is_current():
    """Regenerate with: python scripts/gen_decoupling_fixture.py"""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "gen_decoupling_fixture.py"
    spec = importlib.util.spec_from_file_location("gen_decoupling_fixture", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.OUT.exists(), "run python scripts/gen_decoupling_fixture.py"
    assert json.loads(mod.OUT.read_text(encoding="utf-8")) == json.loads(mod.render()), (
        "decouplingFixture.json is stale; run python scripts/gen_decoupling_fixture.py")
