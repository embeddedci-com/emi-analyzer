"""What a net carries, and which IC pins draw from a supply.

Both cases come from a real board (the BenchPod digital board) where they made the findings
list open with the wrong things: I2C, reset and LED nets as critical radiators, and a current
monitor's sense input and a power mux's status line as IC supplies with no capacitor.
"""

from __future__ import annotations

import pytest

from emi_worker.kicad.board import Track
from emi_worker.rules import checks, decoupling
from emi_worker.rules.model import Finding, dedupe, is_supply_pin, net_activity

from .test_layout_checks import board, pad, run, via


@pytest.mark.parametrize("net,want", [
    # Slow or static by name.
    ("/I2C0_SCL", "slow"), ("/I2C0_SDA", "slow"), ("SCL", "slow"), ("/NRST", "slow"),
    ("/NRST_CONTROL", "slow"), ("/ESP32_BOOT", "slow"), ("/ESP32_EN", "slow"),
    ("/CAN_TERM_CTRL", "slow"), ("/CC_SENSE1", "slow"), ("/ICE_CDONE", "slow"),
    ("Net-(LED1-K)", "slow"), ("/Ethernet/GREEN_LED", "slow"), ("/Power/EFUSE2_FLT", "slow"),
    ("/LA_VCCIO_ST", "slow"), ("CLK_EN", "slow"), ("ETH_RST", "slow"),
    # Fast by name.
    ("/RMII_RXD0", "fast"), ("/ICE_STM32_MOSI", "fast"), ("/ICE_SCLK", "fast"),
    ("/Ethernet/TXP", "fast"), ("Net-(USB1-D+)", "fast"), ("/ICE_SS_PSRAM", "fast"),
    ("/SPI1_SCK", "fast"), ("DDR_DQ3", "fast"),
    # Nothing either way.
    ("/FDCAN1_TX", "unknown"), ("/ICE_IO2", "unknown"), ("/ESP32_CTS", "slow"), ("UART_TX", "unknown"),
    ("/BACKLIGHT", "unknown"), ("/SDA_DIR", "slow"), ("/RMII_MDIO", "slow"), ("/RMII_MDC", "slow"),
    ("PHY_MDI0_P", "fast"), ("MDI+", "fast"),
])
def test_net_activity(net, want):
    assert net_activity(net) == want


def _p(function="", kind=""):
    p = pad("U1", "1", "+5V", 0, 0)
    p.pin_function, p.pin_type = function, kind
    return p


@pytest.mark.parametrize("function,kind,want", [
    ("", "", True),                    # no pin data: every pad counts, as before
    ("", "input", False),
    ("VDD", "power_in", True),
    ("VCCIO_0_33", "power_in", True),  # iCE40, numbered pin name
    ("VIN_1", "power_in", True),
    ("IN_5", "unspecified", True),     # eFuse supply input
    ("OUT_6", "unspecified", True),
    ("VS", "", True),                  # INA226 supply
    ("IN+_10", "unspecified", False),  # INA226 sense input on the rail it measures
    ("ST_8", "unspecified", False),    # TPS2116 status output
    ("PR1_4", "unspecified", False),   # TPS2116 priority input
    ("PG3_88", "unspecified", False),  # STM32 GPIO
    ("EN_3", "input", False),          # LDO enable tied to its input
    ("EN_3", "unspecified", False),
    ("+INB_5", "unspecified", False),  # op-amp input on a net named like a rail ("TO_5V")
    ("INB+_5", "unspecified", False),
    ("-INA", "", False),
    ("S3_4", "unspecified", False),    # analog mux source
    ("D", "", False),
    ("OUTA", "", False),
    ("VS\u2013_5", "unspecified", True),  # THS4551 negative supply, with an en dash
    ("V-", "", True),
    ("VREF", "input", True),           # a reference input needs its capacitor
    ("REF", "", True),
    ("FOO", "unspecified", True),      # unknown name: no evidence, so it still counts
    ("GPIO12", "power_in", True),      # the type wins
])
def test_is_supply_pin(function, kind, want):
    assert is_supply_pin(_p(function, kind)) is want


def test_a_sense_pin_on_a_supply_needs_no_capacitor():
    m = board()
    m.pads = [pad("U35", "10", "+5V_IN", 10, 10), pad("U35", "1", "GND", 10, 11),
              pad("U35", "2", "+3V3", 12, 10), pad("C1", "1", "+3V3", 12.5, 10, value="100n"),
              pad("C1", "2", "GND", 13.5, 10, value="100n")]
    m.pads[0].pin_function, m.pads[0].pin_type = "IN+_10", "unspecified"
    m.pads[2].pin_function, m.pads[2].pin_type = "VS_2", "power_in"
    m.vias = [via(13.7, 10.3)]
    assert run(decoupling.check_decoupling, m) == []
    # The same pad with no pin data is still an unserved supply pin.
    m.pads[0].pin_function = m.pads[0].pin_type = ""
    assert [f.title for f in run(decoupling.check_decoupling, m)] == ["U35 has no decoupling capacitor on +5V_IN"]


def _long(net, length, x=5.0):
    return Track(net=net, layer="F.Cu", width_mm=0.2, pts=[(x, 1.0), (x, 1.0 + length)])


def test_radiators_follow_what_a_net_carries():
    m = board(size=200.0)
    m.tracks = [_long("/I2C0_SCL", 160, 5), _long("/RMII_RXD0", 60, 10), _long("/FDCAN1_TX", 64, 15)]
    found = run(checks.check_radiators, m)
    by_net = {f.net: f for f in found if f.net}
    assert set(by_net) == {"/RMII_RXD0", "/FDCAN1_TX"}  # the I2C line is only counted
    assert by_net["/RMII_RXD0"].severity == "critical"
    assert by_net["/FDCAN1_TX"].severity == "warning"
    assert "fast bus" in by_net["/RMII_RXD0"].detail
    # The fast net is listed first even though it is shorter.
    assert found[0].net == "/RMII_RXD0"
    summary = [f for f in found if not f.net]
    assert summary and "1 of them are slow or static lines" in summary[0].detail


def test_a_slow_net_with_no_return_via_is_advisory():
    m = board()
    m.vias = [via(30, 30)]
    for net, x in (("/I2C0_SDA", 5), ("/ICE_SCLK", 10)):
        v = via(x, 5, net)
        m.vias.append(v)
    found = {f.net: f for f in run(checks.check_return_vias, m)}
    assert found["/I2C0_SDA"].severity == "info" and "advisory" in found["/I2C0_SDA"].detail
    assert found["/ICE_SCLK"].severity == "critical"


def test_the_cap_keeps_the_most_severe():
    fs = [Finding(rule="r", severity="info", title=f"i{n}", detail="") for n in range(5)]
    fs += [Finding(rule="r", severity="critical", title="c", detail="")]
    fs += [Finding(rule="s", severity="info", title="other", detail="")]
    out = dedupe(fs, per_rule_limit=3)
    titles = [f.title for f in out]
    # The critical survives the cap, the order found is kept, and the cut is counted.
    assert titles[:4] == ["i0", "i1", "c", "other"]
    assert titles[-1] == "3 more r findings not listed"


def test_a_strap_inside_one_part_is_not_a_signal():
    # A USB charging port shorts D+ to D- at the receptacle: nothing drives the net.
    m = board()
    m.pads = [pad("USB1", "2", "Net-(USB1-D+)", 10, 10, kind="thru_hole"),
              pad("USB1", "3", "Net-(USB1-D+)", 10, 12, kind="thru_hole")]
    m.vias = [via(30, 30), via(10, 11, "Net-(USB1-D+)")]
    assert run(checks.check_return_vias, m) == []
    # The same via on a net that reaches a second part is checked as before.
    m.pads.append(pad("U1", "1", "Net-(USB1-D+)", 20, 10))
    assert [f.net for f in run(checks.check_return_vias, m)] == ["Net-(USB1-D+)"]


def _radiator(net):
    return Finding(rule="radiator", severity="critical", title=f"{net} is long", detail="Long.", net=net)


def test_a_long_net_is_critical_only_where_its_return_is_broken():
    from emi_worker.kicad.board import CopperLayer
    from emi_worker.rules.checks import _weigh_radiators

    from .test_layout_checks import ctx_for

    m = board(layers=("F.Cu", "In1.Cu", "B.Cu"))
    m.copper_layers[1] = CopperLayer(ordinal=1, name="In1.Cu", kind="power")
    findings = [_radiator("/SPI_SCK"), _radiator("/RMII_RXD0"),
                Finding(rule="return-via", severity="critical", title="", detail="", net="/SPI_SCK"),
                Finding(rule="return-via", severity="warning", title="", detail="", net="/RMII_RXD0")]
    _weigh_radiators(ctx_for(m), findings)
    sck, rmii = findings[0], findings[1]
    assert sck.severity == "critical" and "a layer change with no return via" in sck.detail
    # A return via a few millimeters off is a warning of its own, not a broken return path.
    assert rmii.severity == "warning" and "no serious break" in rmii.detail

    # Without a plane, length alone stays the rule.
    flat = [_radiator("/RMII_RXD0")]
    _weigh_radiators(ctx_for(board()), flat)
    assert flat[0].severity == "critical"


@pytest.mark.parametrize("net,kind", [
    ("/Ethernet/CHSGND", "ground"), ("CGND", "ground"), ("USB_SHIELD", "ground"),
    ("FGND", "ground"), ("/SIGNDIR", "signal"), ("PE4", "signal"),
])
def test_chassis_and_shield_nets_are_ground(net, kind):
    from emi_worker.rules.model import classify_net
    assert classify_net(net) == kind


def test_slow_pairs_and_a_board_with_no_dram_form_no_length_groups():
    from emi_worker.kicad.netclass import DiffPair
    from emi_worker.rules.matching import find_groups

    nets = ["/CAN+", "/CAN-", "/ETH_TXP", "/ETH_TXN", "/SPI2_CS0", "/MUX_A0", "/MUX_A1",
            "/RMII_REF_CLK"]
    pairs = [DiffPair(base="/CAN", positive="/CAN+", negative="/CAN-", source="name"),
             DiffPair(base="/ETH_TX", positive="/ETH_TXP", negative="/ETH_TXN", source="name")]
    names = [g.name for g in find_groups(nets, {}, None, pairs)]
    # CAN's skew is irrelevant at its bit rate; with no DRAM, A0/CS0 are not a DDR bus.
    assert names == ["/ETH_TX"]
