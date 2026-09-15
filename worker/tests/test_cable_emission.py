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

from emi_worker.cables.emission import compose
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

TRANSFER = {
    "frequencies_hz": [100e6, 200e6],
    "h_real": [0.01, 0.02], "h_imag": [0.0, 0.0],
    "usable": [True, True],
}
SOURCE = {100e6: complex(1.0, 0), 200e6: complex(0.5, 0)}
ANTENNA = {100e6: (complex(300, 0), 25.0), 200e6: (complex(200, 0), 40.0)}


def test_the_composition_is_the_arithmetic_in_the_design_doc():
    """I_cm = H * V_src / Z_ant, E = I_cm * E_per_amp, checked by hand:
    0.01 * 1.0 / 300 = 33.3 uA, times 25 V/m/A = 833 uV/m."""
    e = compose("USB1", "usb2-shielded", TRANSFER, SOURCE, ANTENNA)
    first = e.points[0]
    assert first.current_a == pytest.approx(33.33e-6, rel=1e-3)
    assert first.field_v_per_m == pytest.approx(833e-6, rel=1e-3)
    assert first.current_dbua == pytest.approx(30.5, abs=0.1)
    assert first.field_dbuv_per_m == pytest.approx(58.4, abs=0.1)


def test_the_margin_is_positive_when_under_the_limit():
    e = compose("USB1", "usb2-shielded", TRANSFER, SOURCE, ANTENNA)
    # 58.4 dBuV/m against a 43.5 limit at 100 MHz is a predicted failure.
    assert e.points[0].margin_db == pytest.approx(43.5 - 58.4, abs=0.1)
    assert e.points[0].margin_db < 0


def test_the_worst_point_is_the_least_margin_not_the_largest_field():
    """They differ whenever the limit steps, which it does at 88, 216 and 960 MHz."""
    e = compose("USB1", "usb2-shielded", TRANSFER, SOURCE, ANTENNA)
    worst = e.worst()
    assert worst.margin_db == min(p.margin_db for p in e.points)


@pytest.mark.parametrize("missing,expect", [
    ("driver", "the driver says nothing"),
    ("antenna", "antenna solver was not run"),
    ("source", "no source energy"),
])
def test_a_silent_input_is_named_rather_than_dropped(missing, expect):
    """§17.3's completeness gate reads this list. A quietly shorter set of points would look
    like a cleaner result rather than an incomplete one."""
    transfer = dict(TRANSFER)
    source = dict(SOURCE)
    antenna = dict(ANTENNA)
    if missing == "driver":
        source.pop(200e6)
    elif missing == "antenna":
        antenna.pop(200e6)
    else:
        transfer = {**TRANSFER, "usable": [True, False]}

    e = compose("USB1", "usb2-shielded", transfer, source, antenna)
    assert len(e.points) == 1
    assert 200e6 in e.undriven
    assert expect in e.undriven[200e6]


def test_a_composition_with_nothing_usable_has_no_worst_point():
    e = compose("USB1", "usb2-shielded", {**TRANSFER, "usable": [False, False]},
                SOURCE, ANTENNA)
    assert e.points == []
    assert e.worst() is None
    assert len(e.undriven) == 2


# ---------------------------------------------------------------------------
# The shared fixtures, asserted here and in webapp/src/lib/cableEmission.test.ts.
# ---------------------------------------------------------------------------

_FIXTURES = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
             / "cable_emission_fixtures.json")


def _emission_cases():
    doc = json.loads(_FIXTURES.read_text())
    return [(c["name"], c) for c in doc["cases"]]


@pytest.mark.parametrize("name,case", _emission_cases(),
                         ids=[c[0] for c in _emission_cases()])
def test_matches_shared_fixtures(name, case):
    inp = case["input"]
    transfer = inp["transfer"]
    freqs = transfer["frequencies_hz"]
    ant = inp["antenna"]

    source = {f: complex(v[0], v[1])
              for f, v in zip(freqs, inp["source_volts"]) if v is not None}
    antenna = {f: (complex(ant["z_real"][k], ant["z_imag"][k]), ant["e_per_amp"][k])
               for k, f in enumerate(ant["frequencies_hz"])}

    em = compose(transfer["ref"], ant["cable_id"], transfer, source, antenna,
                 standard_id=inp["standard_id"])

    expected = case["expected"]
    assert [p["frequency_hz"] for p in expected["points"]] == \
           [p.frequency_hz for p in em.points]
    for want, got in zip(expected["points"], em.points):
        assert got.current_dbua == pytest.approx(want["current_dbua"], abs=1e-9)
        assert got.field_dbuv_per_m == pytest.approx(want["field_dbuv_per_m"], abs=1e-9)
        assert got.limit_dbuv_per_m == pytest.approx(want["limit_dbuv_per_m"], abs=1e-9)
        assert got.margin_db == pytest.approx(want["margin_db"], abs=1e-9)
    assert sorted(em.undriven) == expected["undriven_hz"]


def test_every_reason_a_point_goes_missing_is_distinct():
    """Four causes, four messages.

    §17.3's completeness gate reads this list, and a user who sees "no result at 160 MHz"
    needs to know whether their driver is silent there, the solve had no energy, or the
    antenna solver simply was not asked — the fix is different in each case.
    """
    doc = json.loads(_FIXTURES.read_text())
    case = next(c for c in doc["cases"] if c["name"] == "every-way-a-point-goes-missing")
    inp = case["input"]
    transfer = inp["transfer"]
    freqs = transfer["frequencies_hz"]
    ant = inp["antenna"]
    source = {f: complex(v[0], v[1])
              for f, v in zip(freqs, inp["source_volts"]) if v is not None}
    antenna = {f: (complex(ant["z_real"][k], ant["z_imag"][k]), ant["e_per_amp"][k])
               for k, f in enumerate(ant["frequencies_hz"])}

    em = compose(transfer["ref"], ant["cable_id"], transfer, source, antenna)

    assert em.points == []
    reasons = [em.undriven[f] for f in sorted(em.undriven)]
    assert len(set(reasons)) == 4, reasons
    assert "no source energy" in reasons[0]
    assert "driver says nothing" in reasons[1]
    assert "antenna solver was not run" in reasons[2]
    assert "zero input impedance" in reasons[3]
