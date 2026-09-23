"""The discharge source, the vendor-model gate, and line geometry -- the parts with no ngspice.

The source is held to the standard's own calibration table. The model gate is tested on what
it must refuse as much as on what it accepts, because a netlist is closer to a script than to
data and an upload is somebody else's file running on our worker.
"""

from __future__ import annotations

import math

import pytest

from emi_worker import topology
from emi_worker.kicad.board import BoardModel, CopperLayer, Pad, Track, Via
from emi_worker.kicad.normalize import board_extent
from emi_worker.rules.model import RuleContext
from emi_worker.stackup import BoardElectrics, LayerElectrics
from emi_worker.transient import lines, spice_model, waveforms


# ---- the discharge source ------------------------------------------------------------------

@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_the_source_meets_table_3_at_every_level(level):
    assert waveforms.conformance(level) == []


def test_the_first_peak_is_the_first_peak_not_the_second_hump():
    m = waveforms.measure(waveforms.esd_samples(4.0))
    assert 13.0 < m.peak_a < 17.0
    assert 0.6 < m.rise_ns < 1.0


def test_negative_polarity_is_the_same_waveform_mirrored():
    pos = waveforms.esd_samples(8.0)
    neg = waveforms.esd_samples(8.0, polarity=-1)
    assert all(math.isclose(a[1], -b[1]) for a, b in zip(pos, neg))
    assert waveforms.conformance(4, neg) == []


def test_a_waveform_off_the_table_is_reported_in_words():
    halved = [(t, i / 2) for t, i in waveforms.esd_samples(8.0)]
    problems = waveforms.conformance(4, halved)
    assert any(p.startswith("first peak") for p in problems)


# ---- the vendor model gate -----------------------------------------------------------------

GOOD = b"""* ACME ESD array, rev B
.SUBCKT ACME_ESD IO1 GND IO2 VBUS
D1 IO1 VBUS DSTEER ; steering diode
D2 GND IO1 DSTEER
D3 IO2 VBUS DSTEER
D4 GND IO2 DSTEER
DZ GND VBUS DZEN
.MODEL DSTEER D(IS=1e-14 N=1.05 RS=0.4
+ CJO=0.6p)
.MODEL DZEN D(BV=6.1 IBV=1m RS=0.2 CJO=12p)
.ENDS ACME_ESD
.END
"""


def test_a_plain_subcircuit_model_is_accepted_with_its_pins():
    m = spice_model.validate(GOOD, "acme.lib")
    assert m.subckt("acme_esd") == ("ACME_ESD", ["IO1", "GND", "IO2", "VBUS"])
    assert ".END" not in m.text.splitlines()
    assert "CJO=0.6p" in m.text, "continuation lines are joined onto their statement"
    assert "steering diode" not in m.text, "comments are not carried into the netlist"


def test_pspice_parameters_on_a_subcircuit_are_not_pins():
    m = spice_model.validate(b".subckt TVS A K PARAMS: VBR=6.8\nD1 K A DT\n.model DT D(BV={VBR})\n.ends\n", "t.lib")
    assert m.subckts["TVS"] == ["A", "K"]


@pytest.mark.parametrize("snippet, reason", [
    (".control\nshell rm -rf /\n.endc", "runs ngspice commands"),
    (".include /etc/passwd", "reads another file"),
    (".lib models.lib TT", "reads another file"),
    (".options reltol=1", "changes simulator options"),
    (".tran 1n 10n", "analysis or output command"),
    ("R1 a b 1k", "outside any .subckt"),
])
def test_statements_a_model_has_no_business_with_are_refused(snippet, reason):
    data = GOOD.replace(b".END\n", snippet.encode() + b"\n")
    with pytest.raises(spice_model.ModelRejected) as exc:
        spice_model.validate(data, "bad.lib")
    assert any(reason in r for r in exc.value.reasons), exc.value.reasons


def test_code_model_devices_and_file_arguments_are_refused():
    data = b".subckt X a b\nA1 a b filesrc\nV1 a b PWL file=/etc/shadow\n.ends\n"
    with pytest.raises(spice_model.ModelRejected) as exc:
        spice_model.validate(data, "x.lib")
    text = " ".join(exc.value.reasons)
    assert "XSPICE code-model" in text and "file arguments" in text


def test_every_problem_is_reported_not_just_the_first():
    data = b".control\n.endc\n.include x\n.subckt Y a b\n"
    with pytest.raises(spice_model.ModelRejected) as exc:
        spice_model.validate(data, "y.lib")
    assert len(exc.value.reasons) >= 4


def test_references_must_resolve_inside_the_file():
    data = b".subckt TOP a b\nX1 a b MISSING\nD1 a b NOMODEL\n.ends\n"
    with pytest.raises(spice_model.ModelRejected) as exc:
        spice_model.validate(data, "r.lib")
    text = " ".join(exc.value.reasons)
    assert "MISSING, which the file does not define" in text
    assert "NOMODEL, which the file does not define" in text


def test_binary_and_empty_files_are_not_models():
    with pytest.raises(spice_model.ModelRejected, match="binary"):
        spice_model.validate(b"\x7fELF\x02\x01\x01\x00\x00", "a.lib")
    with pytest.raises(spice_model.ModelRejected, match="no .subckt"):
        spice_model.validate(b"* just a comment\n", "empty.lib")


def test_names_the_simulation_uses_itself_are_reserved():
    with pytest.raises(spice_model.ModelRejected, match="reserved"):
        spice_model.validate(b".subckt EMI_PIN a b\nR1 a b 1\n.ends\n", "clash.lib")


def test_an_oversized_file_is_refused_before_it_is_parsed():
    with pytest.raises(spice_model.ModelRejected, match="MB"):
        spice_model.validate(b"*" * (spice_model.MAX_MODEL_BYTES + 1), "huge.lib")


# ---- values and closed forms ---------------------------------------------------------------

@pytest.mark.parametrize("text, ohms", [
    ("100", 100.0), ("1k", 1000.0), ("4k7", 4700.0), ("22R", 22.0), ("0R", 0.0),
    ("10Ω", 10.0), ("1M", 1e6), ("2meg", 2e6), ("4.7k", 4700.0), ("", None), ("DNP", None),
])
def test_resistor_values_parse(text, ohms):
    assert lines.parse_ohms(text) == ohms


def test_via_inductance_is_about_a_nanohenry_for_a_board_via():
    nh = lines.via_inductance_nh(1.6, 0.3)
    assert 1.0 < nh < 1.5


# ---- line geometry -------------------------------------------------------------------------

def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def pad(ref, number, net, x, y, value=""):
    return Pad(ref=ref, number=number, net=net, layers=["F.Cu"], x=x, y=y,
               ring=square(x - 0.3, y - 0.3, x + 0.3, y + 0.3), value=value)


def _electrics():
    f = LayerElectrics(name="F.Cu", index=0, kind="microstrip", reference_plane="In1.Cu",
                       height_mm=0.2, epsilon_r=4.4, assumed=False)
    return BoardElectrics(layers={"F.Cu": f})


def _ctx(m, routed=True):
    return RuleContext(model=m, transform=board_extent(m), max_frequency_hz=1e9,
                       electrics=_electrics(), topology=topology.build(m) if routed else {})


def _t_board():
    """J1 at the edge, a T at x=10 with the clamp 4 mm up it, the IC 15 mm further on."""
    m = BoardModel()
    m.thickness_mm = 1.6
    m.copper_layers = [CopperLayer(ordinal=0, name="F.Cu", kind="signal")]
    m.outline = [square(0, 0, 40, 40) + [(0.0, 0.0)]]
    m.nets = ["/DATA", "GND"]
    m.pads = [
        pad("J1", "1", "/DATA", 1, 20), pad("J1", "2", "GND", 1, 22),
        pad("D1", "1", "/DATA", 10, 24, value="PESD5V0S1BA"), pad("D1", "2", "GND", 10, 25.5, value="PESD5V0S1BA"),
        pad("U1", "5", "/DATA", 25, 20),
    ]
    m.tracks = [Track(layer="F.Cu", net="/DATA", width_mm=0.3, pts=[(1, 20), (10, 20)]),
                Track(layer="F.Cu", net="/DATA", width_mm=0.3, pts=[(10, 20), (10, 24)]),
                Track(layer="F.Cu", net="/DATA", width_mm=0.3, pts=[(10, 20), (25, 20)])]
    m.vias = [Via(x=10, y=26.0, size_mm=0.6, drill_mm=0.3, layers=["F.Cu", "B.Cu"], net="GND")]
    return m


def test_the_branch_point_comes_from_the_routed_paths():
    found, notes = lines.exposed_lines(_ctx(_t_board()))
    assert [(l.net, l.connector, l.clamp_ref, l.ic_pad.ref) for l in found] == [("/DATA", "J1", "D1", "U1")]
    tree = found[0].tree
    assert tree.routed
    assert math.isclose(tree.trunk.length_mm, 9.0, abs_tol=0.05)
    assert math.isclose(tree.clamp_stub.length_mm, 4.0, abs_tol=0.05)
    assert math.isclose(tree.ic_stub.length_mm, 15.0, abs_tol=0.05)
    # Delay per mm is the stackup's, not a board constant; impedance is computed, not assumed.
    assert math.isclose(tree.trunk.td_ps / tree.trunk.length_mm, _electrics().ps_per_mm("F.Cu"), rel_tol=1e-6)
    assert 30 < tree.trunk.z0_ohm < 80 and not notes


def test_without_routing_the_tree_is_placed_from_straight_lines():
    tree = lines.exposed_lines(_ctx(_t_board(), routed=False))[0][0].tree
    assert not tree.routed
    # Straight lines: J1->D1 is sqrt(81+16), J1->U1 is 24, D1->U1 is sqrt(225+16).
    t = (math.hypot(9, 4) + 24 - math.hypot(15, 4)) / 2
    assert math.isclose(tree.trunk.length_mm, t, rel_tol=1e-6)


def test_the_clamp_ground_becomes_an_inductance():
    line = lines.exposed_lines(_ctx(_t_board()))[0][0]
    assert math.isclose(line.ground_gap_mm, 0.2, abs_tol=1e-6)  # via at 26.0, pad edge at 25.8
    assert line.ground_nh > line.via_nh > 0


def test_a_clamp_behind_a_series_resistor_keeps_the_resistor_in_the_circuit():
    m = BoardModel()
    m.thickness_mm = 1.6
    m.copper_layers = [CopperLayer(ordinal=0, name="F.Cu", kind="signal")]
    m.outline = [square(0, 0, 40, 40) + [(0.0, 0.0)]]
    m.pads = [
        pad("J1", "1", "/PIN", 1, 20),
        pad("R1", "1", "/PIN", 3, 20, value="1k"), pad("R1", "2", "/PIN_R", 4, 20, value="1k"),
        pad("D1", "1", "GND", 5, 19, value="BAT54S"), pad("D1", "3", "/PIN_R", 5, 20, value="BAT54S"),
        pad("U1", "3", "/PIN_R", 12, 20),
    ]
    line = lines.exposed_lines(_ctx(m, routed=False))[0][0]
    assert line.series_resistor[:2] == ("R1", 1000.0)
    assert math.isclose(line.lead.length_mm, 2.0)
    assert (line.clamp_net, line.clamp_ref) == ("/PIN_R", "D1")
    assert math.isclose(line.tree.ic_stub.length_mm, 7.0, abs_tol=1e-6)


def test_an_unprotected_line_is_still_simulated():
    m = _t_board()
    m.pads = [p for p in m.pads if p.ref != "D1"]
    m.tracks = m.tracks[:1] + m.tracks[2:]
    line = lines.exposed_lines(_ctx(m))[0][0]
    assert line.clamp_ref is None and line.tree.clamp_stub is None
    assert math.isclose(line.tree.trunk.length_mm, 24.0, abs_tol=0.05)


# ---- the datasheet table ---------------------------------------------------------------------

from emi_worker.transient import parts, parts_table  # noqa: E402


@pytest.mark.parametrize("value, part", [
    ("USBLC6-2SC6_C2687116", "USBLC6-2SC6"),
    ("USBLC6-4SC6", "USBLC6-4SC6"),
    ("PESD3V3L4UG,115", "PESD3V3L4UG"),
    ("SRV05-4.TCT", "SRV05-4"),
    ("SMAJ5.0A_C2925443", "SMAJ5.0A"),
    ("SMAJ36CA", "SMAJ36CA"),
    ("BAT54S", "BAT54S"),
])
def test_board_part_values_find_their_datasheet_entry(value, part):
    assert parts.lookup(value).part == part


@pytest.mark.parametrize("value", ["SMAJ5.0CA", "LBAT54SLT1G", "PESD5V0", "", "10k"])
def test_near_misses_do_not_borrow_another_parts_datasheet(value):
    assert parts.lookup(value) is None


@pytest.mark.parametrize("part", sorted(parts_table.TABLE))
def test_every_entry_says_where_its_numbers_came_from(part):
    entry = parts_table.TABLE[part]
    assert entry["topology"] in parts.TOPOLOGIES
    assert entry["datasheet"].startswith("https://") and entry["revision"]


def test_a_feed_through_array_knows_which_pins_are_one_line():
    spec = parts.lookup("USBLC6-2SC6")
    assert spec.feedthrough["1"] == "6" and spec.feedthrough["3"] == "4"


def test_the_clamp_slope_is_fitted_through_the_rated_point():
    spec = parts.lookup("USBLC6-2SC6")
    # 17 V at 5 A, less 6 V breakdown, the knee, and one forward junction.
    assert 1.8 < spec.r_dyn_ohm < 2.1
    assert any("fitted" in a for a in spec.assumptions)
