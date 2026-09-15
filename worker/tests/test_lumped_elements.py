"""In-plane R, C and L elements, and the series composition K1 forced (§18.2, M1 P1).

M0 established that the shipped CSXCAD 0.6.2 has no LEtype, so R, C and L on one element are
wired in *parallel*. A capacitor model is a series R-L-C, which is a different circuit at
every frequency — not an approximation of the right one. These tests pin both halves: what a
single element writes, and that the series form is really three elements.
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


# ---- the series composition ------------------------------------------------------------

CELLS = [
    ((0.0, 0.0, 0.0), (0.05, 0.5, 0.0)),
    ((0.05, 0.0, 0.0), (0.10, 0.5, 0.0)),
    ((0.10, 0.0, 0.0), (0.15, 0.5, 0.0)),
]


def test_series_rlc_is_three_single_value_elements():
    parts = csx.series_rlc(
        "c1", direction=0, resistance=0.02, inductance=0.5e-9, capacitance=100e-9,
        cells=CELLS)
    assert [p.name for p in parts] == ["c1_r", "c1_l", "c1_c"]
    for part in parts:
        a = attrs(part)
        set_values = [k for k in ("R", "C", "L") if k in a]
        assert len(set_values) == 1, f"{part.name} sets {set_values}, which openEMS parallels"


def test_series_rlc_carries_the_values_through():
    parts = csx.series_rlc(
        "c1", direction=0, resistance=0.02, inductance=0.5e-9, capacitance=100e-9,
        cells=CELLS)
    by_name = {p.name: p for p in parts}
    assert by_name["c1_r"].resistance == pytest.approx(0.02)
    assert by_name["c1_l"].inductance == pytest.approx(0.5e-9)
    assert by_name["c1_c"].capacitance == pytest.approx(100e-9)


def test_series_rlc_places_the_cells_in_order_along_the_direction():
    parts = csx.series_rlc(
        "c1", direction=0, resistance=0.02, inductance=0.5e-9, capacitance=100e-9,
        cells=CELLS)
    starts = [p.primitives[0].p1[0] for p in parts]
    assert starts == sorted(starts)


def test_series_rlc_needs_exactly_three_cells():
    """Fewer means collapsing into a parallel element, which is wrong at every frequency."""
    with pytest.raises(ValueError, match="exactly three adjacent cells"):
        csx.series_rlc("c1", direction=0, resistance=0.02, inductance=0.5e-9,
                       capacitance=100e-9, cells=CELLS[:2])


def test_series_rlc_elements_validate_in_a_document():
    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=1e9, fc=1e9),
        x_lines=[0.0, 0.05, 0.10, 0.15, 0.2], y_lines=[0.0, 0.25, 0.5],
        z_lines=[0.0, 0.1], f_max=2e9,
    )
    for part in csx.series_rlc("c1", direction=0, resistance=0.02, inductance=0.5e-9,
                               capacitance=100e-9, cells=CELLS):
        doc.add(part)
    xml = doc.to_string()
    assert xml.count("<LumpedElement") == 3
    assert 'Name="c1_c"' in xml


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
