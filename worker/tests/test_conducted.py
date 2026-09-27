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


def _boost(d=0.4, l_h=None, v_in=5.0, i=1.0, f=500e3):
    return sources.from_params("U3", {"frequency_hz": f, "input_current_a": i, "duty": d}, None, "boost",
                               l_h, v_in)


def test_a_boost_draws_a_triangle_with_the_textbook_harmonics():
    s = _boost(d=0.4)  # no inductance: the assumed 30 % ripple
    ripple, source = s.ripple_pp()
    assert (ripple, source) == (pytest.approx(0.3), "assumed")
    for n, _, amps in s.harmonics(1, 6):
        # A triangle ΔI peak to peak, rising for D: ΔI·|sin(πnD)| / (π²n²D(1-D)) peak.
        expect = 0.3 * abs(math.sin(math.pi * n * 0.4)) / (math.pi ** 2 * n * n * 0.4 * 0.6) / math.sqrt(2)
        assert amps == pytest.approx(expect, rel=1e-6, abs=1e-12), n
    assert s.assumed == ["inductance_h"]


def test_a_boost_ripple_comes_from_its_inductor_and_is_capped_at_discontinuous_mode():
    s = _boost(d=0.4, l_h=4.7e-6, v_in=5.0)
    ripple, source = s.ripple_pp()
    assert ripple == pytest.approx(5.0 * 0.4 / (500e3 * 4.7e-6)) and source == "inductor"
    assert s.inductance_h.source == "board" and "inductance_h" not in s.assumed
    assert "rise_s" not in s.relevant(), "a triangle has no switching edges"
    tiny = _boost(d=0.4, l_h=0.1e-6, v_in=5.0, i=0.1)
    ripple, source = tiny.ripple_pp()
    assert ripple == pytest.approx(0.2) and tiny.capped and "capped" in source


def test_a_phase_delays_harmonic_n_by_n_times_the_phase():
    a = sources.from_params("U1/VLX1", {"frequency_hz": 1e6, "duty": 0.3, "phase_deg": 0}, None)
    b = sources.from_params("U1/VLX2", {"frequency_hz": 1e6, "duty": 0.3, "phase_deg": 90}, None)
    for (n, _, pa), (_, _, pb) in zip(a.phasors(1, 4), b.phasors(1, 4)):
        assert abs(pa) == pytest.approx(abs(pb))
        assert cmath.phase(pb / pa) == pytest.approx(cmath.phase(cmath.exp(-1j * n * math.pi / 2)), abs=1e-9)
    assert "phase_deg" in b.relevant() and "phase_deg" not in b.assumed
    shared = sources.from_params("U1/VLX1", {}, None, phased=True)
    assert "phase_deg" in shared.assumed, "one of a PMIC's outputs: its phase matters"
    alone = sources.from_params("U2", {}, None)
    assert "phase_deg" not in alone.assumed


def test_a_topology_the_user_chose_replaces_the_one_found():
    s = sources.from_params("U1", {"topology": "boost"}, 0.275, "buck")
    assert s.topology == "boost" and s.duty.source == "assumed", "the rail names' duty was for a buck"
    with pytest.raises(ValueError, match="topology"):
        sources.from_params("U1", {"topology": "flyback"}, None)


def test_phased_lines_add_as_phasors_and_the_rest_in_magnitude():
    def line(ref, vp, phased):
        return {"f_hz": 1e6, "dbuv": lisn.dbuv(abs(vp)), "sources": [{"ref": ref}], "_vp": vp, "_vn": -vp,
                "_phased": phased}
    out = scan._combine([line("A", 1e-3, True), line("B", -1e-3, True), line("C", 1e-3, False)])
    assert len(out) == 1 and out[0]["dbuv"] == pytest.approx(60.0), "A and B cancel; C is left"
    both = scan._combine([line("A", 1e-3, False), line("B", -1e-3, False)])
    assert both[0]["dbuv"] == pytest.approx(66.02, abs=0.01), "phase unknown: the worst case"


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


def test_a_boost_fed_from_the_rail_draws_through_its_inductor():
    d = _discover(_buck_board(boost=True))
    assert [(r.id, r.topology, r.topology_from) for r in d.regulators] == [("U1", "boost", "layout")]
    reg = d.regulators[0]
    # The input current flows into the inductor, so the source sits at its pad, not at U1's.
    assert reg.input_pad.ref == "L1" and reg.input_net == "/VIN"
    assert reg.inductance_h == pytest.approx(4.7e-6) and reg.v_in == 12.0
    assert "inductor L1 from the input rail" in reg.how and reg.confidence == "high"


def _ctx(m):
    from emi_worker import topology
    ctx = ctx_for(m)
    ctx.topology = topology.build(m)
    return ctx


def _entry(m, net="+24V", x=1, y=20):
    m.pads += [pad("J1", "1", net, x, y, footprint="Connector:Barrel_Jack"),
               pad("J1", "2", "GND", x, y + 4, footprint="Connector:Barrel_Jack"),
               pad("C1", "1", net, x + 5, y, value="10uF", footprint="Capacitor_SMD:C_1206_3216Metric"),
               pad("C1", "2", "GND", x + 5, y + 2, value="10uF", footprint="Capacitor_SMD:C_1206_3216Metric")]


def test_a_controller_with_external_fets_draws_through_the_high_side_drain():
    """The layout of a real board: a sense resistor into the high-side FET's drain, the
    controller's own VIN through 10 Ω, and an inductor with a U reference."""
    m = board(60)
    _entry(m)
    m.pads += [
        pad("R1", "1", "+24V", 10, 20, value="4m"), pad("R1", "2", "Net-(Q1-D)", 12, 20, value="4m"),
        pad("R2", "1", "+24V", 10, 30, value="10"), pad("R2", "2", "Net-(U2-VIN)", 12, 30, value="10"),
        pad("Q1", "1", "/UG", 14, 22), pad("Q1", "2", "/PHASE", 16, 22), pad("Q1", "5", "Net-(Q1-D)", 15, 20),
        pad("Q2", "1", "/LG", 14, 26), pad("Q2", "2", "GND", 16, 26), pad("Q2", "5", "/PHASE", 15, 24),
        pad("U2", "1", "Net-(U2-VIN)", 20, 30), pad("U2", "2", "/UG", 20, 31), pad("U2", "3", "/PHASE", 20, 32),
        pad("U2", "4", "/LG", 20, 33), pad("U2", "5", "GND", 20, 34),
        pad("U3", "1", "/PHASE", 18, 24, value="6.8uH", footprint="lib:IND-SMD_L17_0-W17_0"),
        pad("U3", "2", "+5V_OUT", 24, 24, value="6.8uH", footprint="lib:IND-SMD_L17_0-W17_0"),
        pad("C5", "1", "+5V_OUT", 26, 24, value="22uF"), pad("C5", "2", "GND", 26, 26, value="22uF"),
    ]
    d = rail.discover(_ctx(m))
    assert [(r.id, r.topology) for r in d.regulators] == [("U2", "buck")]
    reg = d.regulators[0]
    assert reg.input_pad.ref == "Q1" and reg.input_net == "Net-(Q1-D)"
    assert reg.output_net == "+5V_OUT" and reg.inductor == "U3"
    assert reg.duty_from_rails == pytest.approx(5 / 24, abs=1e-3)
    assert "external switches Q1, Q2 driven by U2" in reg.how and "Q1's drain" in reg.how


def test_each_buck_of_a_pmic_is_its_own_source():
    m = board(60)
    _entry(m, "+5V")
    m.pads += [pad("U1", "1", "+5V", 20, 20), pad("U1", "2", "Net-(U1-VLX1)", 22, 20),
               pad("U1", "3", "+5V", 20, 24), pad("U1", "4", "Net-(U1-VLX2)", 22, 24),
               pad("U1", "5", "GND", 21, 22), pad("U1", "6", "VDDCORE", 21, 26)]
    for i, (y, out) in enumerate(((20, "+3V3"), (24, "VDDCORE")), start=1):
        m.pads += [pad(f"L{i}", "1", f"Net-(U1-VLX{i})", 26, y, value="1uH"),
                   pad(f"L{i}", "2", out, 28, y, value="1uH"),
                   pad(f"C{i + 5}", "1", out, 30, y, value="22uF"),
                   pad(f"C{i + 5}", "2", "GND", 30, y + 1, value="22uF")]
    d = rail.discover(_ctx(m))
    got = {r.id: r for r in d.regulators}
    assert set(got) == {"U1/VLX1", "U1/VLX2"}
    assert {r.group for r in d.regulators} == {"U1"}
    assert got["U1/VLX1"].duty_from_rails == pytest.approx(0.66) and got["U1/VLX2"].duty_from_rails is None
    assert got["U1/VLX1"].input_pad.number == "1" and got["U1/VLX2"].input_pad.number == "3", "the nearest input pin"
    assert set(d.network.sources) == {"U1/VLX1", "U1/VLX2"}
    assert "one of 2 outputs of U1" in got["U1/VLX1"].how


def test_a_buck_module_is_known_by_its_part_number():
    m = board(60)
    _entry(m, "+5V")
    m.pads += [pad("U30", "1", "+5V", 20, 20, value="TPS82130SILR"),
               pad("U30", "2", "GND", 20, 22, value="TPS82130SILR"),
               pad("U30", "3", "+3V3", 22, 20, value="TPS82130SILR"),
               pad("C9", "1", "+3V3", 24, 20, value="22uF"), pad("C9", "2", "GND", 24, 22, value="22uF")]
    d = rail.discover(_ctx(m))
    reg = d.regulators[0]
    assert (reg.id, reg.topology, reg.topology_from, reg.confidence) == ("U30", "buck", "part number", "medium")
    assert reg.output_net == "+3V3" and reg.duty_from_rails == pytest.approx(0.66)
    assert "buck module" in reg.how


def test_the_rail_follows_a_charger_to_the_boost_behind_it():
    """A real board: USB into a linear charger, whose system output feeds a boost."""
    m = board(60)
    _entry(m, "+5V")
    sys_net = "Net-(U7-SYS)"
    m.pads += [pad("U7", "1", "+5V", 12, 20, value="BQ25185"), pad("U7", "2", sys_net, 14, 20, value="BQ25185"),
               pad("U7", "3", "/BAT", 14, 22, value="BQ25185"), pad("U7", "4", "GND", 12, 22, value="BQ25185"),
               pad("C7", "1", sys_net, 16, 20, value="10uF"), pad("C7", "2", "GND", 16, 22, value="10uF"),
               pad("L1", "1", sys_net, 18, 20, value="1uH"), pad("L1", "2", "/SW", 20, 20, value="1uH"),
               pad("U3", "1", sys_net, 22, 22, value="TPS61023DRLR"), pad("U3", "2", "/SW", 22, 20, value="TPS61023DRLR"),
               pad("U3", "3", "GND", 22, 24, value="TPS61023DRLR"), pad("U3", "4", "/VOUT", 24, 20, value="TPS61023DRLR")]
    d = rail.discover(_ctx(m))
    assert d.rail_nets == ["+5V", sys_net]
    passed = next(s for s in d.network.series if s.ref == "U7")
    assert passed.kind == "pass" and passed.assumed
    assert [(r.id, r.topology, r.confidence) for r in d.regulators] == [("U3", "boost", "high")]
    assert any("worst case" in n for n in d.notes)


def test_an_inverting_stage_is_an_inductor_to_ground():
    m = board(60)
    _entry(m, "+5V")
    m.pads += [pad("U4", "1", "+5V", 20, 20), pad("U4", "2", "/SWN", 22, 20), pad("U4", "3", "GND", 20, 22),
               pad("U4", "4", "-12V", 24, 22),
               pad("L4", "1", "/SWN", 22, 24, value="10uH"), pad("L4", "2", "GND", 22, 26, value="10uH"),
               pad("D4", "1", "/SWN", 24, 20), pad("D4", "2", "-12V", 26, 20),
               pad("C4", "1", "-12V", 28, 20, value="10uF"), pad("C4", "2", "GND", 28, 22, value="10uF")]
    d = rail.discover(_ctx(m))
    reg = d.regulators[0]
    assert (reg.topology, reg.output_net) == ("inverting", "-12V")
    assert reg.duty_from_rails == pytest.approx(12 / 17, abs=1e-3)


def test_a_pin_named_like_a_switch_node_with_no_inductor_is_reported_not_scanned():
    m = board(60)
    _entry(m, "+5V")
    m.pads += [pad("U5", "1", "+5V", 20, 20), pad("U5", "2", "/PH0", 22, 20), pad("U5", "3", "GND", 20, 22)]
    d = rail.discover(_ctx(m))
    assert d.regulators == []
    assert d.skipped == [("U5", "/PH0 is named like a switch node but has no inductor on it")]


def test_a_supply_connector_away_from_the_edge_is_a_power_input():
    m = board(60)
    _entry(m, "+24V", x=12)  # 12 mm in: a screw terminal's pads sit well inside its body
    m.pads += [pad("J2", "1", "+5V", 1, 40, footprint="Connector:PinHeader"),
               pad("J2", "2", "GND", 1, 42, footprint="Connector:PinHeader"),
               pad("C2", "1", "+5V", 5, 40, value="1uF"), pad("C2", "2", "GND", 5, 42, value="1uF")]
    d = rail.discover(_ctx(m))
    assert [(e.id, e.at_edge) for e in d.entries] == [("J1:+24V", False), ("J2:+5V", True)]
    assert d.entry.id == "J1:+24V" and "more than 5 mm from the board edge" in d.notes[0]


def test_a_power_input_that_is_a_regulators_output_is_not_its_load():
    """A +3V3 pin on a connector, made by the board's own buck: the buck is not drawing from it."""
    m = _buck_board(with_ferrite=False)
    m.pads += [pad("J2", "1", "+3V3", 59, 20, footprint="Connector:PinHeader"),
               pad("J2", "2", "GND", 59, 22, footprint="Connector:PinHeader"),
               pad("U1", "4", "+3V3", 31, 18)]  # the buck's own feedback pin
    from emi_worker import topology
    ctx = ctx_for(m)
    ctx.topology = topology.build(m)
    d = rail.discover(ctx, "J2:+3V3")
    assert d.regulators == []
    assert "is its output, not its input" in d.skipped[0][1]
    # With no choice the highest voltage wins, and there U1 is a load again.
    assert [r.ref for r in rail.discover(ctx).regulators] == ["U1"]


def test_the_rail_continues_through_an_efuse_to_the_regulator():
    m = _buck_board(with_ferrite=False)
    # U7 between the connector's +12V_IN and the board's +12V; an LDO to +3V3 does not count.
    for p in m.pads:
        if p.ref in ("J1",) and p.net == "+12V":
            p.net = "+12V_IN"
    m.pads += [pad("U7", "1", "+12V_IN", 3, 20), pad("U7", "2", "+12V", 4, 20), pad("U7", "3", "GND", 4, 22),
               pad("U8", "1", "+12V", 20, 25), pad("U8", "2", "+3V3", 22, 25), pad("U8", "3", "GND", 21, 27)]
    d = _discover(m)
    assert d.entry.net == "+12V_IN"
    assert d.rail_nets == ["+12V_IN", "+12V"]
    assert [r.ref for r in d.regulators] == ["U1"]
    sw = next(s for s in d.network.series if s.ref == "U7")
    assert sw.kind == "switch" and sw.assumed


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
REGULATORS_FIXTURE = Path(__file__).parent / "fixtures" / "regulators.kicad_pcb"


def fixture_context(path=FIXTURE):
    """A fixture parsed the way the stage parses an upload, with its ground plane."""
    from emi_worker import stackup, topology
    from emi_worker.kicad.normalize import board_extent
    from emi_worker.rules.model import RuleContext
    from emi_worker.stages.ingest import load_board

    model, _, _ = load_board(path.read_bytes())
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


def test_the_regulators_fixture_is_read_as_two_bucks_a_boost_and_a_module():
    d = rail.discover(fixture_context(REGULATORS_FIXTURE))
    got = {r.id: (r.topology, r.topology_from, r.confidence, r.input_pad.ref, r.output_net, r.duty_from_rails)
           for r in d.regulators}
    assert got == {
        "U1/LX1": ("buck", "layout", "high", "U1", "+3V3", 0.275),
        "U1/LX2": ("buck", "layout", "high", "U1", "+1V8", 0.15),
        "U2": ("boost", "layout", "high", "L3", "+24V", 0.5),
        "U3": ("buck", "part number", "medium", "U3", "+5V", 0.417),
    }
    assert d.rail_nets == ["+12V"] and d.routed and d.skipped == []
    assert [c.ref for c in sorted(d.network.shunts, key=lambda c: c.distance_mm)] == ["C1", "C2", "C5", "C7"]
