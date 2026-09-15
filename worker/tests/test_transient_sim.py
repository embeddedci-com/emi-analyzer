"""ESD simulation through the real ngspice: the circuit pieces, the line engine, vendor models.

Skipped where ngspice is not installed. The worker image has it, and these run there:

    docker run --rm -u 0 -v "$PWD":/src -w /src --entrypoint sh <worker image> \\
        -c "pip install -q pytest && python -m pytest -q tests/test_transient_sim.py"

Each check is the kind that must hold before a number is shown to anyone: the source still
meets the standard after ngspice has interpolated it, an inductor and a transmission line do
what physics says, a clamp model hits the clamping voltage it was built from, and moving a
clamp to the connector actually lowers what the pin sees.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from emi_worker.kicad.board import Pad
from emi_worker.rules.model import classify_net
from emi_worker.transient import lines, models, netlist, ngspice, parts, simulate, waveforms

from .test_transient_models import GOOD

pytestmark = pytest.mark.skipif(not ngspice.available(), reason="ngspice is not installed")


def _run(body: str, end: str, step: str = "5p", tmax: str = "1p", saves: str = "") -> ngspice.Waveforms:
    text = f"* test\n{body}\n.options interp\n.tran {step} {end} 0 {tmax}\n.save {saves}\n.end\n"
    return ngspice.run(text)


# ---- circuit pieces ------------------------------------------------------------------------

def test_the_discharge_source_still_meets_the_standard_after_ngspice():
    samples = waveforms.esd_samples(8.0, end_s=60e-9)
    body = "\n".join(netlist._pwl("s", "n1", samples)) + "\nR1 n1 0 1"
    r = _run(body, "60n", saves="v(n1)")
    # Across 1 Ω the voltage is the current, so the node voltage is the waveform itself.
    assert waveforms.conformance(4, list(zip(r.time, r["v(n1)"]))) == []
    assert r["v(n1)"].max() > 25, "a source written as I 0 n pushes current into n"


def test_an_inductor_obeys_v_equals_l_di_dt():
    r = _run("I1 0 n PWL(0 0 1n 1 2n 1)\nL1 n 0 10n\nR1 n 0 1e9", "1.5n", saves="v(n)")
    at = float(np.interp(0.5e-9, r.time, r["v(n)"]))
    assert math.isclose(at, 10.0, rel_tol=0.02)  # 10 nH x 1 A/ns


def test_a_matched_line_delays_without_reflecting():
    body = "I1 0 a PWL(0 0 10p 1)\nRa a 0 50\nT1 a 0 b 0 Z0=50 TD=100p\nRb b 0 50"
    r = _run(body, "400p", step="1p", tmax="0.5p", saves="v(a) v(b)")
    assert float(np.interp(80e-12, r.time, r["v(b)"])) < 0.5
    assert math.isclose(float(np.interp(200e-12, r.time, r["v(b)"])), 25.0, rel_tol=0.03)
    assert math.isclose(float(np.interp(300e-12, r.time, r["v(a)"])), 25.0, rel_tol=0.03)


def test_a_generic_clamp_hits_the_clamping_voltage_it_was_built_from():
    spec = parts.ClampSpec(part="TEST", topology="tvs-uni", v_br_v=6.0, i_t_a=1e-3, r_dyn_ohm=0.5, c_j_pf=20)
    text, pins = parts.clamp_subckt(spec, "EMI_TEST")
    r = ngspice.run(models._quasi_static_netlist(text, "EMI_TEST", pins, 10.0), expect_end_s=10e-6)
    # Breakdown plus the dynamic resistance's drop: about 6 V + 10 A x 0.5 Ω, with the knee.
    assert math.isclose(float(r["v(io)"].max()), 11.0, rel_tol=0.1)


def test_every_topology_builds_and_clamps():
    for topology in parts.TOPOLOGIES:
        spec = parts.ClampSpec(part=topology, topology=topology, v_br_v=6.0, r_dyn_ohm=0.5, c_j_pf=10,
                               v_f_points=[(1e-3, 0.3), (0.1, 0.8)])
        text, pins = parts.clamp_subckt(spec, parts.safe_name(topology))
        roles = [p if p != "rail" else "rail" for p in pins]
        r = ngspice.run(models._sanity_netlist(text, parts.safe_name(topology), roles), expect_end_s=20e-9)
        assert max(abs(r["v(pio)"]).max(), abs(r["v(nio)"]).max()) < models.SANITY_LIMIT_V, topology


# ---- a line ----------------------------------------------------------------------------------

def _pad(ref, number, net, x=0.0, y=0.0):
    return Pad(ref=ref, number=number, net=net, layers=["F.Cu"], x=x, y=y, ring=[], value="")


def _line(clamp_stub_mm: float, with_clamp: bool = True) -> lines.ExposedLine:
    seg = lambda mm: lines.Segment(length_mm=mm, td_ps=6.0 * mm, z0_ohm=50.0)  # noqa: E731
    line = lines.ExposedLine(net="/DATA", connector="J1", connector_pad=_pad("J1", "1", "/DATA"), clamp_net="/DATA")
    line.ic_pad = _pad("U1", "5", "/DATA")
    line.via_nh = 1.2
    if with_clamp:
        line.clamp_ref, line.clamp_part = "D1", "UNLISTED-TVS"
        line.clamp_pad, line.clamp_ground_pad = _pad("D1", "1", "/DATA"), _pad("D1", "2", "GND")
        line.clamp_pads = [line.clamp_pad, line.clamp_ground_pad]
        line.ground_nh = 2.0
        line.tree = lines.LineTree(trunk=seg(3.0), clamp_stub=seg(clamp_stub_mm), ic_stub=seg(2.0), routed=True,
                                   clamp_path=seg(3.0 + clamp_stub_mm), ic_path=seg(5.0))
    else:
        line.tree = lines.LineTree(trunk=seg(5.0), clamp_stub=None, ic_stub=seg(0.0), routed=True, ic_path=seg(5.0))
    return line


def _simulate(line):
    reference = simulate.reference_clamp()
    clamp = simulate.resolve_clamp(line, {}, {}) if line.clamp_ref else None
    out = simulate.simulate_line(line, clamp, reference, 4, [1, -1], classify_net)
    return {v["id"]: v for v in out["variants"]}, out


def test_a_clamp_far_behind_the_ic_protects_less_than_one_at_the_connector():
    variants, out = _simulate(_line(clamp_stub_mm=40.0))
    assert set(variants) == {"as_laid_out", "clamp_at_connector", "ideal_ground"}
    laid, moved = variants["as_laid_out"], variants["clamp_at_connector"]
    assert abs(moved["v_pin_peak_v"]) < abs(laid["v_pin_peak_v"])
    assert abs(variants["ideal_ground"]["v_pin_peak_v"]) <= abs(laid["v_pin_peak_v"]) + 0.5
    assert len(laid["v_pin"]) == len(out["t_ns"]) == simulate.WAVEFORM_POINTS
    assert not laid["unclamped"]


def test_an_unprotected_line_is_compared_with_a_clamp_at_the_connector():
    variants, _ = _simulate(_line(0.0, with_clamp=False))
    assert set(variants) == {"as_laid_out", "reference_clamp"}
    assert abs(variants["reference_clamp"]["i_pin_peak_a"]) < abs(variants["as_laid_out"]["i_pin_peak_a"])


# ---- vendor models ---------------------------------------------------------------------------

def _board_part():
    pads = [_pad("U9", "1", "/D+"), _pad("U9", "2", "GND"), _pad("U9", "3", "/D-"), _pad("U9", "4", "+5V")]
    return {"ACME": [("U9", pads)]}


def _entry(**over):
    entry = {"part": "ACME", "key": "k", "filename": "acme.lib", "subckt": "ACME_ESD",
             "pins": {"1": "IO1", "2": "GND", "3": "IO2", "4": "VBUS"}}
    entry.update(over)
    return entry


def test_a_real_clamp_model_is_accepted_after_simulating_it():
    vm = models.load([_entry()], {"k": GOOD}, _board_part(), classify_net)["ACME"]
    assert vm.accepted, vm.reasons
    assert [c.name for c in vm.checks] == ["is a SPICE model", "pins", "clamps a discharge"]
    assert all(c.ok for c in vm.checks)


def test_a_model_that_does_not_clamp_is_rejected_by_simulation():
    open_model = b".subckt ACME_ESD IO1 GND IO2 VBUS\nR1 IO1 GND 1meg\nR2 IO2 GND 1meg\nR3 VBUS GND 1meg\n.ends\n"
    vm = models.load([_entry()], {"k": open_model}, _board_part(), classify_net)["ACME"]
    assert not vm.accepted
    assert any("does not clamp" in r for r in vm.reasons)


def test_a_file_that_is_not_a_model_never_reaches_ngspice():
    hostile = b".control\nshell touch /tmp/pwned\n.endc\n"
    vm = models.load([_entry()], {"k": hostile}, _board_part(), classify_net)["ACME"]
    assert not vm.accepted
    assert vm.checks[0].name == "is a SPICE model" and not vm.checks[0].ok


def test_incomplete_pin_mappings_are_rejected_with_what_is_missing():
    vm = models.load([_entry(pins={"1": "IO1", "2": "GND"})], {"k": GOOD}, _board_part(), classify_net)["ACME"]
    assert not vm.accepted
    assert any("IO2, VBUS" in r for r in vm.reasons)


def test_a_model_for_a_part_that_is_not_on_the_board():
    vm = models.load([_entry(part="OTHER")], {"k": GOOD}, _board_part(), classify_net)["OTHER"]
    assert any("no part with value OTHER" in r for r in vm.reasons)


# ---- the datasheet table through ngspice ------------------------------------------------------

from emi_worker.transient import parts_table  # noqa: E402


@pytest.mark.parametrize("part", sorted(p for p, e in parts_table.TABLE.items() if e.get("v_c_points")))
def test_every_table_clamp_reproduces_its_rated_clamping_voltage(part):
    spec = parts.lookup(part)
    name = parts.safe_name(part)
    text, pins = parts.clamp_subckt(spec, name)
    amps, volts, _ = max(spec.v_c_points)
    roles = ["nc" if p == "rail" else p for p in pins]
    r = ngspice.run(models._quasi_static_netlist(text, name, roles, amps), expect_end_s=10e-6)
    got = float(r["v(io)"].max())
    assert abs(got - volts) / volts <= models.DATASHEET_TOLERANCE, f"{part}: {got:.2f} V against {volts} V"


def test_a_trace_ladder_delays_like_the_line_it_stands_for():
    """Traces are LC ladders; a matched one must still delay by its TD and settle to the right level."""
    out: list[str] = []
    netlist._line(out, "t", "tr", "a", "b", lines.Segment(length_mm=17.0, td_ps=100.0, z0_ohm=50.0), 1.0)
    body = "I1 0 a PWL(0 0 10p 1)\nRa a 0 50\n" + "\n".join(out) + "\nRb b 0 50"
    r = _run(body, "600p", step="1p", tmax="0.5p", saves="v(a) v(b)")
    assert float(np.interp(60e-12, r.time, r["v(b)"])) < 2.0, "the far end moves before the delay"
    assert math.isclose(float(np.interp(500e-12, r.time, r["v(b)"])), 25.0, rel_tol=0.05)
    # Half-way up the edge at the far end should come about TD after it did at the near end.
    t_a = float(np.interp(12.5, r["v(a)"][:200], r.time[:200]))
    rising = r["v(b)"][:500]
    t_b = float(np.interp(12.5, np.maximum.accumulate(rising), r.time[:500]))
    assert 80e-12 < t_b - t_a < 125e-12


# ---- regressions from real boards -------------------------------------------------------------

def test_element_names_are_unique_even_with_zero_length_segments():
    """A clamp directly on the line gave its zero-length stub the connector shunt's name, and
    ngspice refused every such netlist with "circuit not parsed"."""
    zero = lines.Segment(length_mm=0.0, td_ps=0.0, z0_ohm=50.0)
    c = netlist.Circuit(key="k0", trunk=zero, clamp_stub=zero, ic_stub=zero, ground_nh=1.0, via_nh=1.0,
                        clamp=netlist.ClampInstance(subckt="EMI_X", roles=["io", "gnd", "rail"], rail_v=5.0),
                        lead=zero, series_ohm=100.0, net_c_nf=100.0)
    names = [ln.split()[0].lower() for ln in netlist.circuit_lines(c, [(0.0, 0.0), (1e-9, 1.0)])
             if ln and not ln.startswith(("*", "+", "."))]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, dupes


def test_a_line_where_nothing_takes_the_current_is_flagged():
    line = _line(0.0, with_clamp=False)
    line.ic_pad = None
    line.tree = lines.LineTree(trunk=lines.Segment(5.0, 30.0, 50.0), clamp_stub=None, ic_stub=None, routed=True)
    variants, _ = _simulate(line)
    assert variants["as_laid_out"]["unclamped"]


def test_a_clamp_behind_a_series_resistor_is_not_mistaken_for_no_clamp():
    line = _line(2.0)
    line.series_resistor = ("R1", 100.0, None, None)
    line.lead = lines.Segment(length_mm=2.0, td_ps=12.0, z0_ohm=50.0)
    variants, _ = _simulate(line)
    assert not variants["as_laid_out"]["unclamped"]


def test_decoupling_on_the_net_absorbs_a_discharge_at_a_supply_pin():
    bare = _line(0.0, with_clamp=False)
    decoupled = _line(0.0, with_clamp=False)
    decoupled.net_c_nf = 10_100.0  # 10 µF + 100 nF on the supply
    peak_bare = abs(_simulate(bare)[0]["as_laid_out"]["v_pin_peak_v"])
    peak_decoupled = abs(_simulate(decoupled)[0]["as_laid_out"]["v_pin_peak_v"])
    assert peak_decoupled < peak_bare / 2
