"""Resolving a driver document to the voltage it actually pushes.

The interesting cases are all failures to drive. A tool that returns a small number where a
clock has no energy at all produces an absolute level that looks measured and is invented.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from emi_worker.drivers.document import parse
from emi_worker.drivers.resolve import (
    describe_corners,
    resolve,
    volts_to_dbuv,
)
from emi_worker.drivers.spectrum import DriverError, trapezoid_series

FIXTURES = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
            / "driver_document_fixtures.json")
DOCS = {c["name"]: c["document"] for c in json.loads(FIXTURES.read_text())["valid"]}


def driver(name: str):
    return parse(DOCS[name])


# ---- trapezoid -------------------------------------------------------------------------

def test_a_trapezoid_resolves_at_its_own_harmonics():
    d = driver("trapezoid-spi-clock")
    r = resolve(d, [25e6, 75e6, 125e6])
    assert r.is_complete()
    series = trapezoid_series(d.trapezoid(), 5)
    assert abs(r.volts[0]) == pytest.approx(series[0][1], rel=1e-12)
    assert abs(r.volts[1]) == pytest.approx(series[2][1], rel=1e-12)
    assert abs(r.volts[2]) == pytest.approx(series[4][1], rel=1e-12)


def test_a_frequency_between_harmonics_is_undriven_not_small():
    d = driver("trapezoid-spi-clock")
    r = resolve(d, [25e6, 40e6])
    assert r.volts[1] is None
    assert not r.is_complete()
    assert "not a harmonic" in r.undriven[40e6]
    assert r.driven_count() == 1


def test_an_even_harmonic_of_a_square_wave_is_driven_but_null():
    """50 MHz *is* a harmonic of a 25 MHz clock: the driver drives it, with zero amplitude.

    That is a different statement from 'not driven', and conflating the two would hide a
    real spectral null behind a completeness warning.
    """
    d = driver("trapezoid-spi-clock")
    r = resolve(d, [50e6])
    assert r.volts[0] is not None
    assert abs(r.volts[0]) < 1e-9
    assert r.is_complete()


def test_harmonic_matching_tolerates_float_arithmetic_only():
    d = driver("trapezoid-spi-clock")
    assert resolve(d, [25e6 * (1 + 1e-9)]).volts[0] is not None
    assert resolve(d, [25e6 * 1.01]).volts[0] is None


def test_the_preview_gets_both_corners():
    corners = describe_corners(driver("trapezoid-spi-clock"))
    assert corners is not None
    f1, f2 = corners
    assert f1 == pytest.approx(1 / (math.pi * 2e-8))
    assert f2 == pytest.approx(1 / (math.pi * 1.2e-9))
    assert describe_corners(driver("waveform-captured-edge")) is None


# ---- waveform --------------------------------------------------------------------------

def test_a_waveform_resolves_through_the_same_transform():
    d = driver("waveform-captured-edge")
    fundamental = 1.0 / d.values["period_s"].value
    r = resolve(d, [fundamental, 2 * fundamental])
    assert r.is_complete()
    assert all(v is not None for v in r.volts)
    assert abs(r.volts[0]) > 0


def test_above_the_capture_bandwidth_with_no_rise_time_is_undriven():
    """§9.3: those harmonics are not driven, and compliance is incomplete there."""
    doc = json.loads(json.dumps(DOCS["waveform-captured-edge"]))
    del doc["waveform"]["rise_s"]
    d = parse(doc)
    fundamental = 1.0 / d.values["period_s"].value
    above = 4 * fundamental               # 4 GHz, above the 2 GHz bandwidth
    r = resolve(d, [fundamental, above])
    assert r.volts[1] is None
    assert "bandwidth" in r.undriven[above]


def test_a_declared_rise_time_continues_the_envelope_without_a_step():
    """An unscaled envelope would step at the join, and a step in a source spectrum becomes
    a step in every result that reads it."""
    d = driver("waveform-captured-edge")
    fundamental = 1.0 / d.values["period_s"].value
    bandwidth = d.payload["bandwidth_hz"]
    freqs = [n * fundamental for n in range(1, 7)]
    r = resolve(d, freqs)
    assert r.is_complete()
    below = [(f, abs(v)) for f, v in zip(freqs, r.volts) if f <= bandwidth]
    above = [(f, abs(v)) for f, v in zip(freqs, r.volts) if f > bandwidth]
    assert below and above
    # The first point above the join must be within a few dB of the last one below it,
    # rather than jumping by whatever ratio the envelope happened to have.
    last_below = max(below, key=lambda p: p[0])[1]
    first_above = min(above, key=lambda p: p[0])[1]
    assert abs(20 * math.log10(first_above / last_below)) < 12


# ---- uploaded spectrum -----------------------------------------------------------------

def test_a_spectrum_interpolates_in_log_frequency():
    d = driver("spectrum-analyser-trace")
    # Points are 25 MHz -> 96.4 dBuV and 75 MHz -> 86.8 dBuV.
    mid = math.sqrt(25e6 * 75e6)
    r = resolve(d, [25e6, mid, 75e6])
    assert volts_to_dbuv(abs(r.volts[0])) == pytest.approx(96.4, abs=1e-6)
    assert volts_to_dbuv(abs(r.volts[2])) == pytest.approx(86.8, abs=1e-6)
    assert volts_to_dbuv(abs(r.volts[1])) == pytest.approx((96.4 + 86.8) / 2, abs=1e-6)


def test_outside_an_uploaded_spectrum_is_undriven():
    d = driver("spectrum-analyser-trace")
    r = resolve(d, [10e6, 25e6, 500e6])
    assert r.volts[0] is None and r.volts[2] is None
    assert r.volts[1] is not None
    assert "says nothing at" in r.undriven[500e6]


def test_an_uploaded_spectrum_declares_it_has_no_phase():
    """A spectrum analyser reading is magnitudes. Downstream combination is worst-case
    phase anyway (§16.2), but the result has to say which it is."""
    assert resolve(driver("spectrum-analyser-trace"), [25e6]).magnitude_only is True
    assert resolve(driver("trapezoid-spi-clock"), [25e6]).magnitude_only is False


# ---- refusals --------------------------------------------------------------------------

def test_no_frequencies_is_refused():
    with pytest.raises(DriverError, match="at least one frequency"):
        resolve(driver("trapezoid-spi-clock"), [])


def test_a_non_positive_frequency_is_refused():
    with pytest.raises(DriverError, match="must be positive"):
        resolve(driver("trapezoid-spi-clock"), [0.0])


def test_zero_volts_has_no_dbuv():
    with pytest.raises(DriverError, match="no dBuV"):
        volts_to_dbuv(0.0)
