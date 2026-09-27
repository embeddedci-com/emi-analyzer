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


def test_precision_probe_circuit_is_overdamped_and_its_decay_is_the_slow_root():
    """The probe's pass mark is the circuit's slow time constant, 0.887 R C."""
    from emi_worker.openems import run

    rc = (run.PRECISION_PROBE_R + run.PRECISION_PROBE_ESR) * run.PRECISION_PROBE_C
    assert run.precision_probe_expected_tau() == pytest.approx(0.887 * rc, rel=2e-3)
    xml = run.series_rlc_precision_probe_xml()
    assert 'LEtype="1"' in xml and 'Name="part_ut"' in xml


def test_precision_probe_reads_a_decay_and_refuses_a_hold():
    import numpy as np

    from emi_worker.openems import run

    t = np.linspace(0, 400e-9, 2000)
    assert run.precision_probe_tau(t, np.exp(-t / 89e-9)) == pytest.approx(89e-9, rel=1e-6)
    # What an unpatched openEMS does: the charge stays, or rings about zero.
    assert run.precision_probe_tau(t, np.full_like(t, 5e-3)) is None
    assert run.precision_probe_tau(t, 5e-3 * np.cos(t / 20e-9)) is None
    # A hold that rounding tilts slightly downward is still a hold: on amd64 a constant fitted
    # a 1e8 s "decay" and was taken for one.
    assert run.precision_probe_tau(t, 5e-3 * (1 - 1e-12 * np.arange(t.size))) is None


def test_series_element_refused_when_the_precision_probe_fails(monkeypatch, tmp_path):
    """An openEMS with the element but not the precision passes the first probe only."""
    import numpy as np

    from emi_worker.openems import post, run

    monkeypatch.setattr(run, "_run_probe", lambda xml, tmp: "")
    t = np.linspace(0, 400e-9, 2000)
    for values, want in ((np.exp(-t / run.precision_probe_expected_tau()), True),
                         (np.exp(-t / 14e-9), False), (np.full_like(t, 1e-3), False)):
        monkeypatch.setattr(post, "read_probe",
                            lambda path, v=values: post.ProbeTrace(time_s=t, values=v))
        run.solver_has_series_rlc.cache_clear()
        assert run.solver_has_series_rlc() is want
    run.solver_has_series_rlc.cache_clear()
