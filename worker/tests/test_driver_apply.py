"""Applying a driver to a solve: the offsets that make a result absolute (§8, §10).

The TypeScript half asserts the same fixtures in `webapp/src/lib/driverApply.test.ts`, since
the near-field shader applies these offsets in the browser.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from emi_worker.drivers.apply import (
    MICRO,
    NULL_FLOOR,
    PortSpectrum,
    apply_driver,
    reference_offset_db,
)
from emi_worker.drivers.document import parse
from emi_worker.drivers.spectrum import DriverError

FIXTURES = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
            / "driver_document_fixtures.json")
DATA = json.loads(FIXTURES.read_text())
PORT = PortSpectrum.from_json(DATA["port_spectrum"])
REFERENCE = DATA["reference_magnitude"]
CASES = [c for c in DATA["valid"] if "apply" in c]
BY_NAME = {c["name"]: c for c in DATA["valid"]}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_fixtures_reproduce(case):
    applied = apply_driver(parse(case["document"]), PORT, REFERENCE,
                           case["apply"]["frequencies_hz"])
    assert applied.is_complete() == case["apply"]["complete"]
    for got, want in zip(applied.offset_db, case["apply"]["offset_db"]):
        if want is None:
            assert got is None
        else:
            assert got == pytest.approx(want, abs=1e-9)


def test_the_reference_offset_is_the_micro_prefix_and_nothing_else():
    """0.5 A/m peak is 113.98 dBuA/m. Getting this wrong is a 120 dB error that still looks
    like a plausible emissions figure."""
    assert reference_offset_db(0.5) == pytest.approx(20 * math.log10(0.5 / MICRO))
    assert reference_offset_db(MICRO) == pytest.approx(0.0)
    assert reference_offset_db(1.0) == pytest.approx(120.0)


def test_the_offset_is_computable_by_hand():
    """V_s = 2.0977 V behind 40 ohm into Z_in = 100 ohm, against a solve that used 20 mA."""
    d = parse(BY_NAME["trapezoid-spi-clock"]["document"])
    applied = apply_driver(d, PORT, REFERENCE, [25e6])
    i_new = 2.097736453123487 / (40 + 100)
    want = 20 * math.log10(REFERENCE / MICRO) + 20 * math.log10(i_new / 0.02)
    assert applied.offset_db[0] == pytest.approx(want, abs=1e-6)


def test_a_zero_reference_is_refused():
    d = parse(BY_NAME["trapezoid-spi-clock"]["document"])
    with pytest.raises(DriverError, match="nothing to put a unit on"):
        apply_driver(d, PORT, 0.0, [25e6])


# ---- the two ways a frequency ends up with no offset -----------------------------------

def test_a_null_and_an_undriven_frequency_are_told_apart():
    """Both leave the map with nothing to show, but for opposite reasons, and the message
    has to say which: one means 'this clock has no energy here', the other 'this clock does
    not reach here at all'."""
    d = parse(BY_NAME["trapezoid-spi-clock"]["document"])
    applied = apply_driver(d, PORT, REFERENCE, [40e6, 50e6])
    assert applied.offset_db == [None, None]
    assert "not a harmonic" in applied.undriven[40e6]
    assert "null of this driver's spectrum" in applied.undriven[50e6]


def test_a_null_is_deterministic_not_float_noise():
    """A 50 % duty clock's even harmonics come back around 1e-16; 20*log10 of that turns a
    few ulp into tens of dB. The floor makes the answer the same in any implementation."""
    d = parse(BY_NAME["trapezoid-spi-clock"]["document"])
    applied = apply_driver(d, PORT, REFERENCE, [50e6])
    assert applied.offset_db[0] is None
    assert NULL_FLOOR < 1e-6


# ---- the port record -------------------------------------------------------------------

def test_a_frequency_the_solve_never_recorded_is_refused():
    """175 MHz is a real harmonic of this 25 MHz clock, so the driver does drive it -- the
    solve simply never recorded a port spectrum there. That is the solve's gap, not the
    driver's, and it must not be confused with an undriven frequency."""
    d = parse(BY_NAME["trapezoid-spi-clock"]["document"])
    with pytest.raises(DriverError, match="no port spectrum at"):
        apply_driver(d, PORT, REFERENCE, [175e6])


def test_port_spectrum_rejects_ragged_arrays():
    with pytest.raises(DriverError, match="different lengths"):
        PortSpectrum.from_json({
            "frequencies_hz": [1e6, 2e6], "v_real": [1.0], "v_imag": [0.0],
            "i_real": [0.1], "i_imag": [0.0],
        })


def test_port_spectra_round_trip_from_post_py():
    """The shape post.build_artifacts writes is the shape this reads."""
    from emi_worker.openems import post

    t = __import__("numpy").arange(2000) * 2e-12
    v = __import__("numpy").hanning(2000) * 2.0 * __import__("numpy").cos(2 * math.pi * 25e6 * t)
    i = __import__("numpy").hanning(2000) * 0.02 * __import__("numpy").cos(2 * math.pi * 25e6 * t)
    block = post.port_spectra(post.ProbeTrace(t, v), post.ProbeTrace(t, i), [25e6])
    spectrum = PortSpectrum.from_json(block)
    assert spectrum.frequencies_hz == [25e6]
    v_port, i_port = spectrum.at(25e6)
    assert abs(v_port / i_port) == pytest.approx(100.0, rel=1e-6)


def test_the_json_shape_names_its_unit():
    d = parse(BY_NAME["trapezoid-spi-clock"]["document"])
    blob = apply_driver(d, PORT, REFERENCE, [25e6, 40e6]).as_json()
    assert blob["unit"] == "dBuA/m"
    assert blob["complete"] is False
    assert "40000000.0" in blob["undriven"]
