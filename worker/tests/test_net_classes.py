"""Which nets are grounds, which are supplies, and which are signals.

About thirty checks ask this, so a wrong answer shows up as a false finding on every via of a
net. Names are matched word by word: substring matching once called KiCad's standard +3.3V a
signal, +10V and VBUS_20V grounds (they contain "0v"), and /SIGNDIR a ground.
"""

from __future__ import annotations

import pytest

from emi_worker.rules.model import classify_net

GROUNDS = ["GND", "AGND", "DGND", "PGND", "GNDA", "GND1", "/GND", "GND_ISO", "VSS", "VSSA",
           "Earth", "0V", "/Power/GND"]
SUPPLIES = ["+3.3V", "+3V3", "+5V", "+24V", "-12V", "+10V", "+48V", "+1V8", "VBUS_20V", "VBUS",
            "USB_VBUS", "VCC", "VCCA", "VDD", "VDDIO", "AVDD", "DVDD", "VIN", "VBAT", "VSYS",
            "3V3", "5V", "/Power/+3V3", "VEE"]
SIGNALS = ["/SIGNDIR", "SDA", "Net-(U1-Pad3)", "Net-(R1-Pad2)", "3V3_EN", "VBUS_DET",
           "VIN_SENSE", "5V_PG", "USB_D+", "CLK_50M", "LED_GREEN", "ADC_IN0", "GPIO10", "DIVIDER"]


@pytest.mark.parametrize("name", GROUNDS)
def test_grounds(name):
    assert classify_net(name) == "ground"


@pytest.mark.parametrize("name", SUPPLIES)
def test_supplies(name):
    assert classify_net(name) == "power"


@pytest.mark.parametrize("name", SIGNALS)
def test_signals(name):
    assert classify_net(name) == "signal"
