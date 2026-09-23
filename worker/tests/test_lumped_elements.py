"""In-plane R, C and L elements, and the series element a capacitor model is.

A capacitor is a series R-L-C: one element with ``LEtype="1"``, which only an openEMS with the
lumped-RLC extension models (research/verify_lumped_rlc.py). These tests pin what each element
writes; whether the solver can use it is ``run.solver_has_series_rlc``'s question.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from emi_worker.openems import csx


def attrs(el: csx.LumpedElement) -> dict:
    return ET.tostring(el.to_xml(), encoding="unicode") and el.to_xml().attrib


def test_a_resistor_writes_only_r():
    """C='0' would be a capacitor of zero farads, which is an open circuit -- not 'no
    capacitor'. An omitted attribute is the only way to say the latter."""
    a = attrs(csx.LumpedElement(name="port", direction=2, resistance=50.0))
    assert a["R"] == "50"
    assert "C" not in a and "L" not in a


def test_a_capacitor_writes_only_c():
    a = attrs(csx.LumpedElement(
        name="c1", direction=0, resistance=None, capacitance=100e-9))
    assert "R" not in a and "L" not in a
    assert float(a["C"]) == pytest.approx(100e-9)


def test_an_inductor_writes_only_l():
    a = attrs(csx.LumpedElement(name="l1", direction=1, resistance=None, inductance=0.5e-9))
    assert "R" not in a and "C" not in a
    assert float(a["L"]) == pytest.approx(0.5e-9)


def test_an_element_with_no_values_is_refused():
    """openEMS would read it happily and change nothing, which is the worst outcome."""
    el = csx.LumpedElement(name="nothing", direction=0, resistance=None)
    with pytest.raises(ValueError, match="not an element at all"):
        el.to_xml()


def test_direction_can_be_in_plane():
    """A decoupling capacitor lies between two pads in the board plane, not vertically."""
    for direction in (0, 1):
        a = attrs(csx.LumpedElement(name="c", direction=direction,
                                    resistance=None, capacitance=1e-9))
        assert a["Direction"] == str(direction)


# ---- the series element ----------------------------------------------------------------

BOX = ((0.0, 0.0, 0.0), (0.15, 0.5, 0.0))


def test_a_series_element_writes_all_three_values_and_letype():
    """One element, LEtype 1. The three-cell R, L, C construction it replaced was an open
    circuit on openEMS 0.0.35, which skips an element with only L (verify_lumped_rlc.py)."""
    el = csx.series_rlc_element("c1", 0, resistance=0.02, inductance=0.5e-9,
                                capacitance=100e-9, box=BOX)
    a = attrs(el)
    assert a["LEtype"] == "1"
    assert float(a["R"]) == pytest.approx(0.02)
    assert float(a["L"]) == pytest.approx(0.5e-9)
    assert float(a["C"]) == pytest.approx(100e-9)
    assert (el.primitives[0].p1, el.primitives[0].p2) == BOX


def test_letype_is_written_only_when_asked_for():
    """0.0.35 ignores the attribute; a port's resistor must stay exactly as it was."""
    assert "LEtype" not in attrs(csx.LumpedElement(name="port", direction=2, resistance=50.0))


# ---- frequency formatting in findings ---------------------------------------------------

def test_sub_megahertz_frequencies_survive_formatting():
    """"%.0f MHz" reads "0 MHz" below half a megahertz, which is exactly where a bulk
    capacitor behind a long trace lands — so the formatting was destroying the worst
    findings, the ones most worth reading."""
    from emi_worker.rules.decoupling import _fmt_mhz

    assert _fmt_mhz(0.262) == "262 kHz"
    assert _fmt_mhz(0.738) == "738 kHz"
    assert _fmt_mhz(1.1) == "1.1 MHz"
    assert _fmt_mhz(5.3) == "5.3 MHz"
    assert _fmt_mhz(23.7) == "24 MHz"
    assert _fmt_mhz(480.0) == "480 MHz"
