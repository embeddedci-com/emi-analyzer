"""Composing a solve, a cable and a driver into a field (§7).

Three separate things meet here and each knows something the others do not: the solve knows
what this layout drives, the antenna solver knows what the cable does with it, and the driver
knows the absolute level. The tests are mostly about what happens when one of them is silent.
"""

from __future__ import annotations

import json
import math

from pathlib import Path

import numpy as np
import pytest

from emi_worker.compliance.assemble import compose_cable, port_impedance
from emi_worker.drivers.document import parse as parse_driver
from emi_worker.openems.post import ProbeTrace, cable_transfer


def _tone(freq: float, amplitude: float, phase: float = 0.0, n: int = 4000, dt: float = 2e-12):
    t = np.arange(n) * dt
    return ProbeTrace(t, amplitude * np.hanning(n) * np.cos(2 * np.pi * freq * t + phase))


# ---- the transfer function ---------------------------------------------------------------

def test_the_transfer_divides_by_the_source_not_the_port_voltage():
    """openEMS excites a lumped element, so the port's terminals see whatever is left after
    the structure has loaded the source. Dividing by that would fold the port's input
    impedance into a number that is meant to be independent of it."""
    gap = _tone(200e6, 0.5)
    v_port = _tone(200e6, 2.0)
    i_port = _tone(200e6, 0.02)          # 50 ohm source: V_src = 2.0 + 0.02*50 = 3.0
    out = cable_transfer(gap, v_port, i_port, [200e6], source_impedance_ohm=50.0)
    h = complex(out["h_real"][0], out["h_imag"][0])
    assert abs(h) == pytest.approx(0.5 / 3.0, rel=1e-3)
    # Dividing by the port voltage alone would have given 0.25, which is a different number
    # and would change with the port's impedance.
    assert abs(abs(h) - 0.25) > 0.05


def test_a_frequency_with_no_source_energy_is_marked_unusable():
    """There the transfer function is a ratio of two small numbers, not a measurement."""
    gap = _tone(200e6, 0.5)
    silent = ProbeTrace(np.arange(4000) * 2e-12, np.zeros(4000))
    out = cable_transfer(gap, silent, silent, [200e6])
    assert out["usable"] == [False]


def test_the_transfer_keeps_phase():
    gap = _tone(200e6, 0.5, phase=math.pi / 2)
    v_port = _tone(200e6, 2.0)
    i_port = _tone(200e6, 0.02)
    out = cable_transfer(gap, v_port, i_port, [200e6])
    assert abs(out["h_imag"][0]) > abs(out["h_real"][0])


# ---- the composition ----------------------------------------------------------------------
#
# The composition is compliance.assemble.compose_cable, which the estimate and (through its
# TypeScript copy) the Cables tab's chart both use. It used to be a second function that ran
# on the solve's grid and assumed the driver's source impedance was the port's.

def _compose(case: dict):
    inp = case["input"]
    return compose_cable(inp["port"]["transfer"], inp["antenna"],
                         port_impedance(inp["spectrum"]), inp["z_s"],
                         parse_driver(inp["driver"]), inp["standard_id"],
                         ref=inp["port"]["ref"])


def _volts_at(case: dict, f: float) -> float:
    from emi_worker.drivers.resolve import resolve

    return abs(resolve(parse_driver(case["input"]["driver"]), [f]).volts[0])


_FIXTURES = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
             / "cable_emission_fixtures.json")
_CASES = {c["name"]: c for c in json.loads(_FIXTURES.read_text())["cases"]}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_matches_shared_fixtures(name):
    """Asserted here and in webapp/src/lib/cableEmission.test.ts."""
    case = _CASES[name]
    comp = _compose(case)
    want = case["expected"]
    assert [p.frequency_hz for p in comp.points] == \
        [p["frequency_hz"] for p in want["points"]]
    for got, exp in zip(comp.points, want["points"]):
        assert got.current_a == pytest.approx(exp["current_a"], rel=1e-12)
        assert got.field_v_per_m == pytest.approx(exp["field_v_per_m"], rel=1e-12)
    assert sorted(comp.undriven) == want["undriven_hz"]
    assert comp.line == want["line"]


def test_the_composition_is_the_arithmetic_in_the_design_doc():
    """I_cm = |H| · |Z_s + Z_in| / |Z_d + Z_in| · V / |Z_ant| and E = I_cm · E_per_amp, by hand
    at a harmonic that sits on a grid point, where nothing is interpolated."""
    case = _CASES["driver-source-impedance-replaces-the-ports"]
    point = next(p for p in _compose(case).points if p.frequency_hz == 250e6)
    h = abs(complex(0.006, -0.002))
    z_in = complex(55.0, 20.0)
    factor = abs(50.0 + z_in) / abs(10.0 + z_in)
    i_cm = h * factor * _volts_at(case, 250e6) / abs(complex(180.0, 120.0))
    assert point.current_a == pytest.approx(i_cm, rel=1e-12)
    assert point.field_v_per_m == pytest.approx(i_cm * 40.0, rel=1e-12)


def test_a_driver_with_the_ports_own_impedance_has_no_factor():
    """Z_d = Z_s is the one case the old chart was right in; any other driver moves every
    point by the factor it left out."""
    case = _CASES["harmonics-between-grid-points"]
    same = _compose(case)
    other = _compose(_CASES["driver-source-impedance-replaces-the-ports"])
    for a, b in zip(same.points, other.points):
        if a.current_a > 0:          # even harmonics of a square wave are nulls in both
            assert b.current_a != pytest.approx(a.current_a, rel=1e-3)
    point = next(p for p in same.points if p.frequency_hz == 250e6)
    h = abs(complex(0.006, -0.002))
    assert point.current_a == pytest.approx(
        h * _volts_at(case, 250e6) / abs(complex(180.0, 120.0)), rel=1e-12)


def test_every_harmonic_is_evaluated_not_only_the_grid_points():
    """A 25 MHz clock against a six-point grid: 37 harmonics in 30-960 MHz, not the one grid
    point (250 MHz) that happens to be a harmonic."""
    comp = _compose(_CASES["harmonics-between-grid-points"])
    assert comp.line
    assert [round(p.frequency_hz / 25e6) for p in comp.points] == list(range(2, 39))


def test_every_reason_a_point_goes_missing_is_named():
    """§17.3's completeness gate reads this list, and a user who sees "no result at 250 MHz"
    needs to know whether the driver is silent there or the solve had no energy."""
    comp = _compose(_CASES["every-way-a-point-goes-missing"])
    assert "no source energy" in comp.undriven[120e6]
    assert "bandwidth" in comp.undriven[250e6]
    # The antenna solver's range caps the band rather than producing points past it.
    assert comp.band == (30e6, 480e6)
    assert all(p.frequency_hz <= 480e6 for p in comp.points)
