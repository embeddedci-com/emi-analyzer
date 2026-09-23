"""Driver spectra — the Python half of §19's D1.

The TypeScript half asserts against the same fixtures in
``webapp/src/lib/driverSpectrum.test.ts``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from emi_worker.drivers.spectrum import (
    DriverError,
    Trapezoid,
    corner_frequencies,
    envelope_v,
    piecewise_linear_series,
    reweight,
    reweight_db,
    trapezoid_series,
)

FIXTURES = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
            / "driver_fixtures.json")


def _sinc(x: float) -> float:
    return 1.0 if x == 0 else math.sin(x) / x


def closed_form(trap: Trapezoid, n: int) -> float:
    """§9.1's textbook result, valid only for a symmetric edge, as an RMS amplitude.

    The textbook gives the one-sided peak; every amplitude in the tool is RMS, so this divides
    by sqrt(2) the same way the implementation does (spectrum.RMS_PER_PEAK)."""
    tau_over_t = trap.pulse_width_s / trap.period_s
    tr_over_t = trap.rise_s / trap.period_s
    return (math.sqrt(2.0) * trap.amplitude_v * tau_over_t
            * abs(_sinc(n * math.pi * tau_over_t))
            * abs(_sinc(n * math.pi * tr_over_t)))


# ---- D1: the closed form ---------------------------------------------------------------

@pytest.mark.parametrize("duty", [0.5, 0.25, 0.1, 0.4])
def test_d1_harmonics_match_the_closed_form(duty):
    """Within 0.1 dB, at every harmonic, for several duty cycles.

    The implementation does not use the closed form -- it integrates a piecewise-linear
    waveform -- so this is two independent derivations agreeing, not a tautology.
    """
    period = 4e-8
    trap = Trapezoid(amplitude_v=3.3, period_s=period, pulse_width_s=duty * period,
                     rise_s=1.2e-9, fall_s=1.2e-9)
    for n, (_f, amp) in enumerate(trapezoid_series(trap, 60), start=1):
        want = closed_form(trap, n)
        if want < 1e-9:          # a spectral null: the closed form's zero is exact
            assert amp < 1e-9
            continue
        assert abs(20 * math.log10(amp / want)) < 0.1, f"harmonic {n}"


def test_d1_holds_at_both_corners():
    """§19 calls out the corners specifically, because that is where an envelope is wrong."""
    trap = Trapezoid(amplitude_v=3.3, period_s=4e-8, pulse_width_s=1e-8,
                     rise_s=1.2e-9, fall_s=1.2e-9)
    f1, f2 = corner_frequencies(trap)
    for f in (f1, f2):
        n = max(1, round(f * trap.period_s))
        _fh, amp = trapezoid_series(trap, n)[n - 1]
        want = closed_form(trap, n)
        assert abs(20 * math.log10(amp / want)) < 0.1


def test_even_harmonics_vanish_at_fifty_percent():
    """A square wave has no even harmonics; if it does, the breakpoints are wrong."""
    trap = Trapezoid(amplitude_v=1.0, period_s=1e-8, pulse_width_s=5e-9,
                     rise_s=1e-11, fall_s=1e-11)
    series = trapezoid_series(trap, 8)
    for n, (_f, amp) in enumerate(series, start=1):
        if n % 2 == 0:
            assert amp < 1e-6, f"harmonic {n} should be a null"


def test_a_square_wave_matches_its_fourier_series():
    """2A/(n*pi) peak for odd n, so sqrt(2)*A/(n*pi) RMS, with edges short enough not to matter.

    Not 4A/(n*pi): that is the figure for a wave swinging between -A and +A. A driver
    output swings between 0 and A, so every harmonic is half of the textbook number most
    references quote, and getting this wrong would put every absolute level 6 dB high.
    """
    trap = Trapezoid(amplitude_v=1.0, period_s=1e-6, pulse_width_s=5e-7,
                     rise_s=1e-12, fall_s=1e-12)
    for n, (_f, amp) in enumerate(trapezoid_series(trap, 7), start=1):
        if n % 2 == 1:
            assert amp == pytest.approx(2.0 / (n * math.pi) / math.sqrt(2.0), rel=1e-5)


# ---- asymmetric edges ------------------------------------------------------------------

def test_asymmetric_edges_are_exact_not_averaged():
    """A 1 ns rise with a 5 ns fall is not the same as 3 ns on both.

    The closed form cannot express this at all, which is why the implementation integrates
    instead. The test pins that the two differ by something real.
    """
    common = dict(amplitude_v=3.3, period_s=4e-8, pulse_width_s=1.5e-8)
    skewed = trapezoid_series(Trapezoid(rise_s=1e-9, fall_s=5e-9, **common), 40)
    averaged = trapezoid_series(Trapezoid(rise_s=3e-9, fall_s=3e-9, **common), 40)
    diffs = [abs(20 * math.log10(a / b)) for (_f, a), (_f2, b) in zip(skewed, averaged)
             if a > 1e-9 and b > 1e-9]
    assert max(diffs) > 3.0, "averaging the edges should visibly change the high harmonics"


def test_reversing_the_edges_mirrors_the_waveform():
    """Swapping rise and fall is a time reversal, which leaves every magnitude alone."""
    common = dict(amplitude_v=2.5, period_s=2e-8, pulse_width_s=8e-9)
    a = trapezoid_series(Trapezoid(rise_s=1e-9, fall_s=4e-9, **common), 30)
    b = trapezoid_series(Trapezoid(rise_s=4e-9, fall_s=1e-9, **common), 30)
    for (_fa, va), (_fb, vb) in zip(a, b):
        assert va == pytest.approx(vb, rel=1e-9, abs=1e-12)


# ---- the envelope ----------------------------------------------------------------------

def test_the_envelope_bounds_the_line_spectrum():
    trap = Trapezoid(amplitude_v=3.3, period_s=4e-8, pulse_width_s=1e-8,
                     rise_s=1.2e-9, fall_s=1.2e-9)
    for f, amp in trapezoid_series(trap, 80):
        assert amp <= envelope_v(trap, f) * 1.001, f"line above envelope at {f / 1e6:.0f} MHz"


def test_the_envelope_has_the_stated_slopes():
    """Edges 100x shorter than the pulse, so the -20 dB/decade region is two decades wide
    and there is room to probe a full decade inside it."""
    trap = Trapezoid(amplitude_v=3.3, period_s=4e-8, pulse_width_s=1e-8,
                     rise_s=1e-10, fall_s=1e-10)
    f1, f2 = corner_frequencies(trap)
    flat = 2 * 3.3 * 1e-8 / 4e-8 / math.sqrt(2.0)
    assert envelope_v(trap, f1 / 10) == pytest.approx(flat)
    # a decade inside the middle region is 20 dB down
    mid = f1 * 10
    assert mid < f2
    assert 20 * math.log10(envelope_v(trap, mid) / flat) == pytest.approx(-20.0, abs=1e-9)
    # a decade above the second corner is 40 dB below the extrapolated -20 dB/decade line
    at_f2 = envelope_v(trap, f2)
    assert 20 * math.log10(envelope_v(trap, f2 * 10) / at_f2) == pytest.approx(-40.0, abs=1e-9)


def test_the_second_corner_uses_the_faster_edge():
    """Taking the slower edge would put the corner low and under-predict above it."""
    trap = Trapezoid(amplitude_v=1.0, period_s=4e-8, pulse_width_s=1e-8,
                     rise_s=1e-9, fall_s=5e-9)
    _f1, f2 = corner_frequencies(trap)
    assert f2 == pytest.approx(1.0 / (math.pi * 1e-9))


# ---- refusals --------------------------------------------------------------------------

def test_edges_that_do_not_fit_the_pulse_are_refused():
    trap = Trapezoid(amplitude_v=3.3, period_s=4e-8, pulse_width_s=1e-9,
                     rise_s=4e-9, fall_s=4e-9)
    with pytest.raises(DriverError, match="do not fit inside the pulse"):
        trap.validate()


def test_a_pulse_longer_than_its_period_is_refused():
    trap = Trapezoid(amplitude_v=3.3, period_s=4e-9, pulse_width_s=3.5e-9,
                     rise_s=2e-9, fall_s=2e-9)
    with pytest.raises(DriverError, match="longer than the period"):
        trap.validate()


def test_a_port_with_no_current_is_refused_not_divided_by():
    with pytest.raises(DriverError, match="nothing to re-weight"):
        reweight(1.0 + 0j, 50.0, 1.0 + 0j, 0j)


# ---- re-weighting ----------------------------------------------------------------------

def test_reweighting_is_the_ratio_of_currents():
    """Z_in = 100 ohm from the probes; a 1 V source behind 50 ohm pushes 1/150 A."""
    v_port, i_port = 2.0 + 0j, 0.02 + 0j          # Z_in = 100 ohm, solve used 20 mA
    factor = reweight(1.0 + 0j, 50.0, v_port, i_port)
    assert factor == pytest.approx((1.0 / 150.0) / 0.02)
    assert reweight_db(1.0 + 0j, 50.0, v_port, i_port) == pytest.approx(
        20 * math.log10((1.0 / 150.0) / 0.02))


def test_reweighting_keeps_phase():
    """A reactive input impedance rotates the factor; magnitude-only would lose that."""
    factor = reweight(1.0 + 0j, 50.0, 0.0 + 2.0j, 0.02 + 0j)
    assert abs(factor.imag) > 1e-9


def test_a_solve_reweighted_by_its_own_source_is_unchanged():
    """The identity case: if the driver pushes exactly what the solve pushed, nothing moves."""
    v_port, i_port = 1.5 - 0.4j, 0.03 + 0.01j
    z_in = v_port / i_port
    z_s = 50.0
    v_equivalent = i_port * (z_s + z_in)
    assert reweight(v_equivalent, z_s, v_port, i_port) == pytest.approx(1.0 + 0j)


# ---- the shared fixtures ---------------------------------------------------------------

def test_fixtures_reproduce():
    """Whatever `make driver-fixtures` wrote, this code still produces."""
    data = json.loads(FIXTURES.read_text())
    for case in data["cases"]:
        trap = Trapezoid(**case["input"])
        got = trapezoid_series(trap, case["harmonics"])
        want = case["expected"]["series"]
        assert len(got) == len(want)
        # Scaled by the peak of the series, the same criterion the TypeScript half uses. A
        # spectral null sits ~17 decades down, so its own relative error is float noise.
        peak = max(w["amplitude_v"] for w in want)
        for (f, amp), w in zip(got, want):
            assert f == pytest.approx(w["frequency_hz"], rel=1e-12)
            assert abs(amp - w["amplitude_v"]) / peak < 1e-12
        f1, f2 = corner_frequencies(trap)
        assert f1 == pytest.approx(case["expected"]["corner_1_hz"], rel=1e-12)
        assert f2 == pytest.approx(case["expected"]["corner_2_hz"], rel=1e-12)
        for point in case["expected"]["envelope"]:
            assert envelope_v(trap, point["frequency_hz"]) == pytest.approx(
                point["amplitude_v"], rel=1e-12)


def test_amplitudes_are_rms_not_peak():
    """A 1 V peak harmonic reads 1/sqrt(2) V on an analyser, and so must it here.

    Limits and uploaded analyser spectra are RMS. Leaving the trapezoid at its one-sided peak
    made every predicted level 3 dB high against both."""
    from emi_worker.drivers.spectrum import RMS_PER_PEAK, piecewise_linear_series

    trap = Trapezoid(amplitude_v=1.0, period_s=1e-6, pulse_width_s=5e-7,
                     rise_s=1e-9, fall_s=1e-9)
    t, v = trap.breakpoints()
    peak = 2.0 * abs(piecewise_linear_series(t, v, trap.period_s, 1))
    (_f, amp), = trapezoid_series(trap, 1)
    assert amp == pytest.approx(peak * RMS_PER_PEAK, rel=1e-12)
    assert 20 * math.log10(peak / amp) == pytest.approx(3.0103, abs=1e-4)
