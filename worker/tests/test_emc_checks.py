"""EMC checks: ESD protection, shields, reset lines, input filtering, switch nodes.

Same shape as test_layout_checks: the smallest board that shows the problem, then the smallest
change that fixes it. The "is not" cases each come from a false finding on a real board -- a
locating peg read as a floating shell, a BAT54S counted as clamping its +5V rail, a +1V8 header
pin taken for a supply input, a big TVS pad measured from its centre, a SoC reset that fans out
to the PMIC -- because those are where a naive check drowns people in noise.
"""

from __future__ import annotations

from emi_worker.kicad.board import BoardModel, CopperLayer, Pad, Track, Via, ZonePolygon
from emi_worker.kicad.normalize import board_extent
from emi_worker.rules import emc, settings
from emi_worker.rules.model import RuleContext


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def board(size=40.0) -> BoardModel:
    m = BoardModel()
    m.copper_layers = [CopperLayer(ordinal=i, name=n, kind="signal") for i, n in enumerate(("F.Cu", "B.Cu"))]
    m.outline = [square(0, 0, size, size) + [(0.0, 0.0)]]
    return m


def pad(ref, number, net, x, y, value="", footprint="", kind="smd", half=0.3, drill=0.0):
    return Pad(ref=ref, number=number, net=net, layers=["F.Cu"], x=x, y=y,
               ring=square(x - half, y - half, x + half, y + half), pad_type=kind,
               drill_mm=drill, value=value, footprint=footprint)


def via(x, y, net="GND"):
    return Via(x=x, y=y, size_mm=0.6, drill_mm=0.3, layers=["F.Cu", "B.Cu"], net=net)


def ctx_for(m, rules=None) -> RuleContext:
    ctx = RuleContext(model=m, transform=board_extent(m), max_frequency_hz=1e9)
    if rules:
        ctx.settings = settings.load(("file", {"rules": rules}))
    return ctx


def run(check, m, rules=None):
    return list(check(ctx_for(m, rules)))


def titles(findings):
    return [f.title for f in findings]


# ---- ESD protection -----------------------------------------------------------------------

def _io(clamp_x=None, ic_x=20.0, conn_x=1.0, clamp_value="PESD5V0S1BA", ground_via=True):
    """A two-pin edge connector carrying /DATA to U1, optionally with a clamp at clamp_x."""
    m = board()
    m.pads = [
        pad("J1", "1", "/DATA", conn_x, 20), pad("J1", "2", "GND", conn_x, 22),
        pad("U1", "5", "/DATA", ic_x, 20),
    ]
    if clamp_x is not None:
        m.pads += [pad("D1", "1", "/DATA", clamp_x, 20, value=clamp_value),
                   pad("D1", "2", "GND", clamp_x, 18.5, value=clamp_value)]
        if ground_via:
            m.vias = [via(clamp_x, 17.5)]
    return m


def test_an_edge_connector_line_with_no_clamp_is_found():
    f = run(emc.check_esd_protection, _io())
    assert titles(f) == ["/DATA leaves the board at J1 with no ESD protection"]
    assert f[0].severity == "warning" and f[0].net == "/DATA"
    assert "U1" in f[0].detail


def test_a_clamp_beside_the_connector_passes():
    assert run(emc.check_esd_protection, _io(clamp_x=3.0)) == []


def test_a_clamp_far_from_the_connector_says_what_it_costs():
    f = run(emc.check_esd_protection, _io(clamp_x=21.0, ic_x=35.0))
    assert titles(f) == ["ESD clamp D1 is 20.0 mm from J1"]
    assert "V across the trace" in f[0].detail


def test_a_clamp_behind_the_ic_protects_nothing():
    f = run(emc.check_esd_protection, _io(clamp_x=15.0, ic_x=6.0))
    assert titles(f) == ["/DATA reaches U1 before its ESD clamp D1"]


def test_a_header_in_the_middle_of_the_board_is_not_io():
    assert run(emc.check_esd_protection, _io(conn_x=20.0, ic_x=30.0)) == []


def test_edge_distance_zero_checks_every_connector():
    f = run(emc.check_esd_protection, _io(conn_x=20.0, ic_x=30.0),
            rules={"esd-protection": {"params": {"edge_mm": 0}}})
    assert titles(f) == ["/DATA leaves the board at J1 with no ESD protection"]


def test_the_clamp_needs_its_own_ground_via():
    m = _io(clamp_x=3.0, ground_via=False)
    m.vias = [via(30, 30)]
    assert titles(run(emc.check_esd_protection, m)) == ["ESD clamp D1's ground pad has no via within 2 mm"]


def test_the_ground_via_is_measured_from_the_pad_edge():
    """A via at the edge of a big SMB pad is beside it, even though the pad centre is 2 mm off."""
    m = _io(clamp_x=3.0, ground_via=False)
    m.pads[-1] = pad("D1", "2", "GND", 3.0, 18.5, value="SMBJ5.0A", half=1.1)
    m.vias = [via(3.0, 15.9)]  # 2.6 mm from the centre, 1.5 mm from the pad edge
    assert run(emc.check_esd_protection, m) == []


def test_a_clamp_behind_a_series_resistor_counts():
    m = board()
    m.pads = [
        pad("J1", "1", "/PIN", 1, 20), pad("R1", "1", "/PIN", 3, 20), pad("R1", "2", "/PIN_R", 4, 20),
        pad("D1", "1", "/PIN_R", 5, 20, value="BAT54S"), pad("D1", "2", "GND", 5, 19),
        pad("U1", "3", "/PIN_R", 12, 20),
    ]
    m.vias = [via(5, 18.2)]
    assert run(emc.check_esd_protection, m) == []


def test_protection_arrays_are_recognised_by_part_number_on_any_reference():
    m = _io()
    m.pads += [pad("U22", "1", "/DATA", 3, 20, value="SRV05-4.TCT"), pad("U22", "2", "GND", 3, 19, value="SRV05-4.TCT")]
    m.vias = [via(3, 18.2)]
    assert run(emc.check_esd_protection, m) == []


def test_an_led_to_ground_is_not_a_clamp():
    f = run(emc.check_esd_protection, _io(clamp_x=3.0, clamp_value="LED green"))
    assert titles(f) == ["/DATA leaves the board at J1 with no ESD protection"]


def test_a_rail_to_rail_diode_does_not_clamp_its_supply():
    """BAT54S clamps the signal against +5V; it does nothing for a surge on +5V itself."""
    m = board()
    m.pads = [
        pad("H3", "1", "+5V", 1, 20, footprint="HDR-TH_8P-P2_54-V-M"), pad("U2", "1", "+5V", 20, 20),
        pad("D2", "1", "GND", 3, 24, value="BAT54S"), pad("D2", "2", "+5V", 3, 25, value="BAT54S"),
        pad("D2", "3", "/DAC_INPUT", 3, 26, value="BAT54S"), pad("U2", "2", "/DAC_INPUT", 20, 26),
    ]
    m.vias = [via(3, 23.2)]
    assert titles(run(emc.check_esd_protection, m)) == ["+5V leaves the board at H3 with no ESD protection"]


def test_a_supply_tvs_does_clamp_its_supply():
    m = board()
    m.pads = [pad("J1", "1", "+24V", 1, 20), pad("U2", "1", "+24V", 20, 20),
              pad("D1", "1", "+24V", 3, 20, value="SMBJ24A"), pad("D1", "2", "GND", 3, 18.5, value="SMBJ24A")]
    m.vias = [via(3, 17.5)]
    assert run(emc.check_esd_protection, m) == []


def test_a_rail_passing_through_a_connector_is_not_an_input():
    m = board()
    m.pads = [pad("J7", "4", "/Ethernet/VDDA", 1, 20), pad("U6", "1", "/Ethernet/VDDA", 15, 20)]
    assert run(emc.check_esd_protection, m) == []


def test_a_low_voltage_reference_on_a_header_is_not_a_supply_input():
    m = board()
    m.pads = [pad("J1", "19", "+1V8", 1, 20), pad("U5", "1", "+1V8", 15, 20)]
    assert run(emc.check_esd_protection, m) == []
    assert run(emc.check_input_filter, m) == []


def test_a_power_input_at_the_edge_is_checked():
    m = board()
    m.pads = [pad("J1", "1", "+24V", 1, 20), pad("U2", "1", "+24V", 15, 20)]
    assert titles(run(emc.check_esd_protection, m)) == ["+24V leaves the board at J1 with no ESD protection"]


# ---- shields, chassis, mounting holes ------------------------------------------------------

def test_a_floating_connector_shell():
    m = board()
    m.pads = [pad("USB1", "A6", "/D+", 1, 20), pad("U1", "1", "/D+", 10, 20),
              pad("USB1", "S1", "", 1, 22, kind="thru_hole", half=0.6, drill=0.6)]
    assert titles(run(emc.check_connector_shield, m)) == ["USB1 has 1 plated shell pad connected to nothing"]


def test_a_locating_peg_drawn_as_a_plated_pad_is_not_a_shell():
    """Converted footprints give pegs a 'pad' exactly the size of the drill: no copper at all."""
    m = board()
    m.pads = [pad("USB1", "", "", 1, 22, kind="thru_hole", half=0.33, drill=0.65)]
    assert run(emc.check_connector_shield, m) == []


def test_unplated_pegs_are_not_shells():
    m = board()
    m.pads = [pad("USB1", "", "", 1, 22, kind="np_thru_hole")]
    assert run(emc.check_connector_shield, m) == []


def test_a_chassis_net_with_no_path_to_ground():
    m = board()
    m.pads = [pad("J7", "8", "/Ethernet/CHSGND", 1, 20)]
    assert titles(run(emc.check_connector_shield, m)) == [
        "Chassis net /Ethernet/CHSGND has no connection to ground"
    ]


def test_a_chassis_net_tied_through_a_capacitor_passes():
    m = board()
    m.pads = [pad("J7", "8", "/Ethernet/CHSGND", 1, 20),
              pad("C9", "1", "/Ethernet/CHSGND", 3, 20), pad("C9", "2", "GND", 4, 20)]
    assert run(emc.check_connector_shield, m) == []


def test_a_plated_mounting_hole_on_no_net_is_advisory():
    m = board()
    m.pads = [pad("H1", "1", "", 3, 3, footprint="MountingHole:MountingHole_3.2mm_M3_Pad",
                  kind="thru_hole", half=3.0, drill=3.2)]
    f = run(emc.check_connector_shield, m)
    assert titles(f) == ["Plated mounting hole H1 is on no net"] and f[0].severity == "info"


def test_an_unplated_mounting_hole_is_fine():
    m = board()
    m.pads = [pad("H1", "", "", 3, 3, footprint="MountingHole:MountingHole_3.2mm_M3", kind="np_thru_hole")]
    assert run(emc.check_connector_shield, m) == []


def test_a_header_named_h_is_a_connector_not_a_hole():
    m = board()
    m.pads = [pad("H1", "1", "GND", 3, 3, footprint="HDR-TH_4P-P2_54-V-M", kind="thru_hole", half=0.8, drill=1.0),
              pad("H1", "2", "", 3, 5.54, footprint="HDR-TH_4P-P2_54-V-M", kind="thru_hole", half=0.8, drill=1.0)]
    assert titles(run(emc.check_connector_shield, m)) == ["H1 has 1 plated shell pad connected to nothing"]


# ---- reset lines ---------------------------------------------------------------------------

def _reset(cap_x=None, net="/NRST"):
    m = board()
    m.pads = [pad("U1", "7", net, 10, 10),
              pad("R1", "1", net, 12, 12), pad("R1", "2", "+3V3", 12, 13)]
    if cap_x is not None:
        m.pads += [pad("C1", "1", net, cap_x, 10), pad("C1", "2", "GND", cap_x + 1, 10)]
    return m


def test_a_reset_held_only_by_a_pull_up():
    f = run(emc.check_reset_filter, _reset())
    assert titles(f) == ["Reset input U1.7 (/NRST) has no filter capacitor"]
    assert "R1" in f[0].detail


def test_a_pull_up_rail_with_capacitors_is_not_a_filter():
    m = _reset()
    m.pads += [pad("C5", "1", "+3V3", 20, 20), pad("C5", "2", "GND", 21, 20)]
    assert len(run(emc.check_reset_filter, m)) == 1


def test_a_capacitor_at_the_pin_passes():
    assert run(emc.check_reset_filter, _reset(cap_x=11.0)) == []


def test_a_distant_reset_capacitor_is_reported():
    assert titles(run(emc.check_reset_filter, _reset(cap_x=30.0))) == ["Reset filter C1 is 20.0 mm from U1.7"]


def test_a_reset_driven_by_another_ic_is_left_alone():
    m = _reset()
    m.pads.append(pad("U2", "3", "/NRST", 20, 10))
    assert run(emc.check_reset_filter, m) == []


def test_a_reset_reaching_another_ic_through_a_resistor_is_driven():
    m = _reset(net="/NRSTC1MS")
    m.pads += [pad("R4", "1", "/NRSTC1MS", 14, 10), pad("R4", "2", "Net-(U2-PWRCTRL3)", 15, 10),
               pad("U2", "9", "Net-(U2-PWRCTRL3)", 20, 10)]
    assert run(emc.check_reset_filter, m) == []


def test_an_rc_through_a_series_resistor_counts():
    m = board()
    m.pads = [pad("U1", "4", "Net-(U1-RSTn)", 10, 10),
              pad("R5", "1", "Net-(U1-RSTn)", 12, 10), pad("R5", "2", "/NRST_BTN", 13, 10),
              pad("C1", "1", "/NRST_BTN", 15, 10), pad("C1", "2", "GND", 16, 10),
              pad("SW1", "1", "/NRST_BTN", 25, 25)]
    assert run(emc.check_reset_filter, m) == []


def test_words_containing_rst_are_not_resets():
    assert run(emc.check_reset_filter, _reset(net="/FIRST_PIN")) == []


# ---- power input filtering -----------------------------------------------------------------

def _power_in(cap_x=None, behind_bead=False):
    m = board()
    m.pads = [pad("J1", "1", "+24V", 1, 20), pad("J1", "2", "GND", 1, 22), pad("U2", "1", "+24V", 25, 20)]
    if cap_x is not None:
        net = "+24V"
        if behind_bead:
            m.pads += [pad("FB1", "1", "+24V", 3, 20, value="FerriteBead"),
                       pad("FB1", "2", "Net-(FB1-Pad2)", 4, 20, value="FerriteBead")]
            net = "Net-(FB1-Pad2)"
        m.pads += [pad("C1", "1", net, cap_x, 20), pad("C1", "2", "GND", cap_x, 21)]
    return m


def test_power_crossing_a_connector_with_no_capacitor():
    assert titles(run(emc.check_input_filter, _power_in())) == ["+24V crosses J1 with no capacitor to ground on it"]


def test_an_input_capacitor_at_the_connector_passes():
    assert run(emc.check_input_filter, _power_in(cap_x=4.0)) == []


def test_a_capacitor_behind_a_ferrite_counts_as_the_filter():
    assert run(emc.check_input_filter, _power_in(cap_x=6.0, behind_bead=True)) == []


def test_a_distant_input_capacitor():
    assert titles(run(emc.check_input_filter, _power_in(cap_x=31.0))) == [
        "The nearest capacitor on +24V is 30.0 mm from J1"
    ]


# ---- switch nodes --------------------------------------------------------------------------

def _buck(net="/SW", track_len=2.0, width=1.0, pour=0.0, inductor_value="4.7u"):
    m = board()
    m.pads = [pad("U1", "3", net, 10, 10),
              pad("L1", "1", net, 12, 10, value=inductor_value), pad("L1", "2", "+3V3", 14, 10, value=inductor_value),
              pad("C1", "1", "+3V3", 16, 10), pad("C1", "2", "GND", 17, 10)]
    m.tracks = [Track(layer="F.Cu", net=net, width_mm=width, pts=[(10, 10), (10 + track_len, 10)])]
    if pour:
        m.zones = [ZonePolygon(layer="F.Cu", net=net, ring=square(20, 20, 20 + pour, 20 + pour))]
    return m


def test_a_compact_switch_node_passes():
    assert run(emc.check_switch_node, _buck()) == []


def test_a_sprawling_switch_node_is_found():
    f = run(emc.check_switch_node, _buck(pour=8.0))
    assert titles(f) == ["Switch node /SW has 66 mm² of copper"]
    assert "its name" in f[0].detail


def test_a_long_thin_switch_node_trace():
    assert titles(run(emc.check_switch_node, _buck(track_len=25.0, width=0.3))) == [
        "Switch node /SW has 25 mm of track"
    ]


def test_found_by_its_inductor_when_the_name_says_nothing():
    f = run(emc.check_switch_node, _buck(net="Net-(U9-REG_OUT)", pour=8.0))
    assert titles(f) == ["Switch node Net-(U9-REG_OUT) has 66 mm² of copper"]
    assert "inductor L1" in f[0].detail


def test_a_ferrite_bead_is_not_a_regulator():
    assert run(emc.check_switch_node, _buck(net="Net-(U9-VDDA)", pour=8.0, inductor_value="100Ω @100MHz")) == []
