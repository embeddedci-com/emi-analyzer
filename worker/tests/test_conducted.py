"""Conducted emissions without the simulator: the LISN's closed form, the source, discovery.

test_conducted_sim.py runs the same pieces through ngspice in the worker image.
"""

from __future__ import annotations

import cmath
import math
from pathlib import Path

import pytest

from emi_worker.conducted import lisn, network, rail, scan, sources
from emi_worker.conducted.network import Network, Series, Shunt
from emi_worker.kicad.board import Track, ZonePolygon
from emi_worker.transient import ngspice

from .test_emc_checks import board, ctx_for, pad


# ---- the network ---------------------------------------------------------------------------

def test_the_constructed_network_meets_the_cispr_curve_across_the_band():
    worst_mag, worst_phase = 0.0, 0.0
    f = lisn.F_LO_HZ
    while f <= lisn.F_HI_HZ:
        ideal, built = lisn.ideal_impedance(f), lisn.network_impedance(f)
        worst_mag = max(worst_mag, abs(abs(built) / abs(ideal) - 1))
        worst_phase = max(worst_phase, abs(math.degrees(cmath.phase(built) - cmath.phase(ideal))))
        f *= 1.05
    assert worst_mag < lisn.TOLERANCE
    assert worst_phase < lisn.PHASE_TOLERANCE_DEG


def test_the_cispr_curve_is_50_ohm_parallel_50_uh():
    # 150 kHz: jωL = j47.1 Ω, so |Z| = 50·47.1/√(50² + 47.1²).
    assert abs(lisn.ideal_impedance(150e3)) == pytest.approx(34.3, abs=0.05)
    assert abs(lisn.ideal_impedance(30e6)) == pytest.approx(50.0, abs=0.01)


# ---- the source ------------------------------------------------------------------------------

def _src(f=500e3, i=1.0, d=0.3, tr=10e-9, ripple=0.0):
    s = sources.from_params("U1", {"frequency_hz": f, "input_current_a": i, "duty": d, "rise_s": tr}, None)
    s.ripple = ripple
    return s


def test_a_flat_topped_pulse_train_has_the_textbook_harmonics():
    s = _src()
    top = 1.0 / 0.3
    for n, f, amps in s.harmonics(1, 5):
        sinc = lambda x: 1.0 if x == 0 else math.sin(math.pi * x) / (math.pi * x)  # noqa: E731
        expect = 2 * top * 0.3 * abs(sinc(n * 0.3)) * abs(sinc(n * 10e-9 * 500e3)) / math.sqrt(2)
        assert amps == pytest.approx(expect, rel=1e-3), n


def test_the_ripple_fills_the_nulls_a_flat_top_has():
    flat = dict((n, a) for n, _, a in _src(d=0.5).harmonics(1, 4))
    sloped = dict((n, a) for n, _, a in _src(d=0.5, ripple=0.3).harmonics(1, 4))
    assert flat[2] < 1e-6 * flat[1]
    assert sloped[2] > 1e-3 * sloped[1]
    assert abs(20 * math.log10(sloped[1] / flat[1])) < 0.3


def test_defaults_are_marked_assumed_and_given_values_are_not():
    s = sources.from_params("U1", {"frequency_hz": 1e6}, None)
    assert s.frequency_hz.source == "user" and not s.frequency_hz.assumed
    assert set(s.assumed) == {"input_current_a", "duty", "rise_s"}
    assert s.as_dict()["duty"] == {"value": 0.5, "source": "assumed", "assumed": True}
    from_rails = sources.from_params("U1", {}, 0.275)
    assert from_rails.duty.source == "rail names" and from_rails.duty.assumed


@pytest.mark.parametrize("given", [
    {"frequency_hz": 1.0}, {"duty": 1.5}, {"input_current_a": "lots"}, {"rise_s": True},
])
def test_out_of_range_parameters_are_refused(given):
    with pytest.raises(ValueError):
        sources.from_params("U1", given, None)


def test_an_edge_that_does_not_fit_the_pulse_is_refused():
    with pytest.raises(ValueError, match="does not fit"):
        _src(f=5e6, d=0.02, tr=50e-9).harmonics(1, 2)


def test_harmonics_in_band():
    assert network.harmonic_sweep(500e3) == (1, 60)
    assert network.harmonic_sweep(100e3) == (2, 300)
    assert network.harmonic_sweep(40e6)[1] < network.harmonic_sweep(40e6)[0]


# ---- the deck ------------------------------------------------------------------------------

def _simple() -> Network:
    net = Network()
    net.series.append(Series("in", "n1", 10e-9))
    net.shunts.append(Shunt("n1", "C1", "10uF", 10e-6, 0.005, 1e-9, 0.5e-9))
    net.sources["U1"] = "n1"
    return net


def test_the_deck_passes_the_safety_check_and_measures_every_harmonic():
    deck, saves = network.build([("v0", _simple(), "U1")], 500e3, 1, 60, title="x\n.control\nshell rm")
    ngspice.check_deck(deck)
    assert deck.splitlines()[0].startswith("* x?.control")
    assert ".ac lin 60 500000 30000000" in deck
    assert saves["v0"]["v_p"] == "v(v0_in)" and saves["v0"]["i:0"] == "i(vv0_m0)"


def test_a_filter_inserted_after_the_entry_takes_everything_behind_it():
    net = _simple()
    net.shunts.append(Shunt("in", "C0", "1uF", 1e-6, 0.01, 1e-9))
    out = net.insert_after_entry(Series("", "", 4.7e-6, kind="added"))
    assert out.series[0].a == "in" and out.series[0].b == "in_f"
    assert {c.node for c in out.shunts if c.ref == "C0"} == {"in_f"}
    assert net.shunts[-1].node == "in", "the original is not changed"


def test_what_ifs_include_each_capacitor_removed():
    ids = [v.id for v in scan.variants(_simple())]
    assert ids[:4] == ["as_laid_out", "cap_at_connector", "cap_at_regulator", "lc_filter"]
    assert "without:C1" in ids


def test_lines_a_receiver_cannot_separate_are_added():
    got = scan._combine([
        {"f_hz": 1e6, "dbuv": 60.0, "sources": [{"ref": "U1"}]},
        {"f_hz": 1.005e6, "dbuv": 60.0, "sources": [{"ref": "U2"}]},
        {"f_hz": 2e6, "dbuv": 50.0, "sources": [{"ref": "U1"}]},
    ])
    assert len(got) == 2
    assert got[0]["dbuv"] == pytest.approx(66.02, abs=0.01)
    assert [s["ref"] for s in got[0]["sources"]] == ["U1", "U2"]


def test_margins_use_the_average_limit_for_a_steady_line():
    lines = [{"f_hz": 1e6, "dbuv": 50.0, "sources": []}]
    worst = scan._score(lines, "B")
    assert lines[0]["avg_limit"] == 46.0 and lines[0]["qp_limit"] == 56.0
    assert worst["margin_db"] == -4.0 and worst["detector"] == "average"


# ---- discovery ------------------------------------------------------------------------------

def _buck_board(with_ferrite=True, boost=False):
    """J1 (+12V, GND) at the edge, C1 at the connector, FB1 into /VIN, C2 and a buck U1."""
    m = board(60)
    vin = "/VIN" if with_ferrite else "+12V"
    m.pads = [
        pad("J1", "1", "+12V", 1, 20, footprint="Connector:Barrel_Jack"),
        pad("J1", "2", "GND", 1, 24, footprint="Connector:Barrel_Jack"),
        pad("C1", "1", "+12V", 6, 20, value="10uF", footprint="Capacitor_SMD:C_1206_3216Metric"),
        pad("C1", "2", "GND", 6, 22, value="10uF", footprint="Capacitor_SMD:C_1206_3216Metric"),
        pad("C2", "1", vin, 28, 20, value="4.7uF", footprint="Capacitor_SMD:C_0805_2012Metric"),
        pad("C2", "2", "GND", 28, 22, value="4.7uF", footprint="Capacitor_SMD:C_0805_2012Metric"),
        pad("U1", "1", vin, 30, 20), pad("U1", "2", "/SW", 32, 20), pad("U1", "3", "GND", 31, 22),
        pad("C3", "1", "+3V3", 40, 20, value="22uF", footprint="Capacitor_SMD:C_0805_2012Metric"),
        pad("C3", "2", "GND", 40, 22, value="22uF", footprint="Capacitor_SMD:C_0805_2012Metric"),
    ]
    if boost:
        m.pads += [pad("L1", "1", vin, 34, 20, value="4.7uH"), pad("L1", "2", "/SW", 36, 20, value="4.7uH")]
    else:
        m.pads += [pad("L1", "1", "/SW", 34, 20, value="4.7uH"), pad("L1", "2", "+3V3", 36, 20, value="4.7uH")]
    if with_ferrite:
        m.pads += [pad("FB1", "1", "+12V", 12, 20, value="600R@100MHz"),
                   pad("FB1", "2", "/VIN", 14, 20, value="600R@100MHz")]
    m.tracks = [Track("F.Cu", "+12V", 1.0, [(1, 20), (12, 20)]), Track("F.Cu", vin, 1.0, [(14, 20), (30, 20)])]
    return m


def _discover(m):
    from emi_worker import topology
    ctx = ctx_for(m)
    ctx.topology = topology.build(m)
    return rail.discover(ctx)


def test_the_input_rail_runs_through_a_ferrite_to_the_regulator():
    d = _discover(_buck_board())
    assert d.entry.id == "J1:+12V" and d.entry.ground_pad.net == "GND"
    assert d.rail_nets == ["+12V", "/VIN"]
    assert [r.ref for r in d.regulators] == ["U1"]
    reg = d.regulators[0]
    assert reg.switch_net == "/SW" and reg.output_net == "+3V3"
    assert reg.duty_from_rails == pytest.approx(0.275)
    caps = {c.ref: c for c in d.network.shunts}
    assert set(caps) == {"C1", "C2"}, "the output capacitor is not on the input rail"
    assert not caps["C1"].assumed and "library" in caps["C1"].model
    fb = next(s for s in d.network.series if s.ref == "FB1")
    assert fb.kind == "ferrite" and fb.assumed
    assert fb.l_h == pytest.approx(600 / (2 * math.pi * 100e6))
    assert d.network.sources == {"U1": reg.node}


def test_the_ladder_is_ordered_by_distance_with_trace_inductance_between():
    d = _discover(_buck_board(with_ferrite=False))
    net = d.network
    order = [c.ref for c in sorted(net.shunts, key=lambda c: c.distance_mm)]
    assert order == ["C1", "C2"]
    traces = [s for s in net.series if s.kind == "trace"]
    # 29 mm of 1 mm track over no plane: tens of nanohenries, not picohenries or microhenries.
    total = sum(s.l_h for s in traces)
    assert 5e-9 < total < 100e-9


def test_a_pour_is_wider_than_its_tracks():
    m = _buck_board(with_ferrite=False)
    m.zones = [ZonePolygon("F.Cu", "+12V", [(0, 15), (35, 15), (35, 25), (0, 25)])]
    from emi_worker import topology
    ctx = ctx_for(m)
    ctx.topology = topology.build(m)
    el = rail._Electrics(ctx)
    assert el.width("+12V", "F.Cu") == 10.0
    assert el.width("GND", "F.Cu") != 10.0


def test_a_boost_is_skipped_with_a_reason():
    d = _discover(_buck_board(boost=True))
    assert d.regulators == []
    assert d.skipped and "boost" in d.skipped[0][1]


def test_a_board_with_no_power_input_says_so():
    m = board()
    m.pads = [pad("U1", "1", "+3V3", 10, 10)]
    d = _discover(m)
    assert d.entry is None and "no supply net" in d.notes[0]


def test_henries_and_ferrite_values():
    assert rail.parse_henries("4.7uH") == pytest.approx(4.7e-6)
    assert rail.parse_henries("4u7") == pytest.approx(4.7e-6)
    assert rail.parse_henries("100nH") == pytest.approx(100e-9)
    assert rail.parse_henries("600") is None
    assert rail.parse_henries("BLM18") is None
    assert rail.ferrite_ohms("600R@100MHz") == 600
    assert rail.ferrite_ohms("120Ω") == 120


# ---- a real KiCad file ------------------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "buck.kicad_pcb"


def fixture_context():
    """The buck fixture parsed the way the stage parses an upload, with its ground plane."""
    from emi_worker import stackup, topology
    from emi_worker.kicad.normalize import board_extent
    from emi_worker.rules.model import RuleContext
    from emi_worker.stages.ingest import load_board

    model, _, _ = load_board(FIXTURE.read_bytes())
    return RuleContext(model=model, transform=board_extent(model), max_frequency_hz=1e9,
                       electrics=stackup.analyse(model, {"B.Cu"}), topology=topology.build(model))


def test_the_fixture_board_is_read_as_a_filtered_buck():
    d = rail.discover(fixture_context())
    for s in d.network.series:
        print(s)
    for c in d.network.shunts:
        print(c)
    print(d.notes)
    assert d.entry.id == "J1:+12V"
    assert d.rail_nets == ["+12V", "/VIN"]
    assert [(r.ref, r.duty_from_rails) for r in d.regulators] == [("U1", 0.275)]
    assert {c.ref for c in d.network.shunts} == {"C1", "C2", "C3"}
    assert d.routed
    assert all(not c.assumed for c in d.network.shunts)
    # 10 mm of 1 mm track over 1.53 mm of FR-4 to FB1, then 14 mm to U1: a few nH per cm.
    traces = sum(s.l_h for s in d.network.series if s.kind == "trace")
    assert 5e-9 < traces < 40e-9
