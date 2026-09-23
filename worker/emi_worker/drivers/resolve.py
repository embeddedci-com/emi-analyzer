"""From a driver document to the voltage it pushes at each frequency.

This is the join between §9 (what the user described) and §8 (re-weighting a solve by it).
The three kinds reach it differently and, more importantly, **fail differently** — which is
the substance of this module.

A trapezoid or a captured waveform is a *line* spectrum: a 25 MHz clock has energy at
25 MHz and 75 MHz and nothing at 40 MHz. A solve asked for 40 MHz is therefore not driven by
that clock, and the honest answer is to say so rather than return a small number. §9.3 says
the same thing about a waveform above its capture bandwidth with no declared rise time:
"those harmonics are *not driven*, and compliance is incomplete there".

So every resolution returns ``None`` where the driver has nothing to say, and names the
frequencies it could not drive. §17.3's completeness gate reads that list. A zero would have
been indistinguishable from a real null, and a small number would have been a lie.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from emi_worker.drivers.document import Driver
from emi_worker.drivers.spectrum import (
    RMS_PER_PEAK,
    DriverError,
    Trapezoid,
    corner_frequencies,
    envelope_v,
    piecewise_linear_series,
)

#: How close a requested frequency must be to a harmonic to count as that harmonic, in parts
#: per million. A solve's frequencies come from the same clock the driver describes, so they
#: line up exactly in the normal case; this absorbs float arithmetic, not disagreement.
HARMONIC_TOLERANCE_PPM = 100.0


@dataclass
class Resolved:
    """What a driver pushes, per requested frequency."""

    frequencies_hz: list[float]
    #: Open-circuit source voltage, complex. ``None`` where the driver drives nothing.
    volts: list[complex | None]
    #: Why each undriven frequency is undriven, for the completeness gate and the UI.
    undriven: dict[float, str] = field(default_factory=dict)
    #: True when the values carry no meaningful phase (an uploaded spectrum has magnitudes).
    magnitude_only: bool = False
    #: The driver's own characteristic voltage, independent of which frequencies were asked
    #: for. A null has to be recognisable from a single-frequency request, so it cannot be
    #: judged against the peak of whatever happened to be requested.
    scale_v: float = 0.0

    def driven_count(self) -> int:
        return sum(1 for v in self.volts if v is not None)

    def is_complete(self) -> bool:
        return not self.undriven


def _harmonic_index(frequency_hz: float, period_s: float) -> int | None:
    """Which harmonic of ``1/period`` this is, or None if it falls between them."""
    exact = frequency_hz * period_s
    n = round(exact)
    if n < 1:
        return None
    if abs(exact - n) / n > HARMONIC_TOLERANCE_PPM / 1e6:
        return None
    return int(n)


def _resolve_lines(
    times: list[float], values: list[float], period_s: float, frequencies: list[float],
    *, what: str,
) -> tuple[list[complex | None], dict[float, str]]:
    volts: list[complex | None] = []
    undriven: dict[float, str] = {}
    fundamental_mhz = 1.0 / period_s / 1e6
    for f in frequencies:
        n = _harmonic_index(f, period_s)
        if n is None:
            volts.append(None)
            undriven[f] = (
                f"{f / 1e6:g} MHz is not a harmonic of this {what} "
                f"({fundamental_mhz:g} MHz fundamental), so it drives nothing there"
            )
            continue
        # An RMS phasor, like every amplitude here (spectrum.RMS_PER_PEAK).
        volts.append(2.0 * RMS_PER_PEAK * piecewise_linear_series(times, values, period_s, n))
    return volts, undriven


def resolve(driver: Driver, frequencies: list[float]) -> Resolved:
    """The open-circuit source spectrum of ``driver`` at ``frequencies``."""
    if not frequencies:
        raise DriverError("ask for at least one frequency")
    if any(f <= 0 for f in frequencies):
        raise DriverError("frequencies must be positive")

    if driver.kind == "trapezoid":
        trap = driver.trapezoid()
        times, vals = trap.breakpoints()
        volts, undriven = _resolve_lines(
            times, vals, trap.period_s, frequencies, what="clock")
        return Resolved(list(frequencies), volts, undriven,
                        scale_v=abs(trap.amplitude_v))

    if driver.kind == "waveform":
        return _resolve_waveform(driver, frequencies)

    return _resolve_spectrum(driver, frequencies)


def _resolve_waveform(driver: Driver, frequencies: list[float]) -> Resolved:
    samples = driver.payload["samples_v"]
    interval = driver.payload["sample_interval_s"]
    bandwidth = driver.payload["bandwidth_hz"]
    period = driver.values["period_s"].value

    # A sampled waveform is piecewise linear between its samples, so the same exact
    # transform the trapezoid uses applies unchanged. One period of it: transforming the
    # whole capture would fold the repetition into the answer.
    n_period = int(round(period / interval))
    if n_period < 2:
        raise DriverError(
            f"one period is {period * 1e9:.4g} ns and the sample interval is "
            f"{interval * 1e9:.4g} ns, which is fewer than two samples per period"
        )
    take = min(n_period + 1, len(samples))
    times = [k * interval for k in range(take)]
    values = [float(v) for v in samples[:take]]

    volts, undriven = _resolve_lines(times, values, period, frequencies, what="waveform")

    # Above the capture bandwidth the samples say nothing. §9.3: continue with the envelope
    # from a declared rise time, or declare those harmonics undriven.
    if bandwidth is not None:
        rise = driver.values.get("rise_s")
        envelope_trap = None
        if rise is not None:
            amplitude = max(values) - min(values)
            pulse_width = period / 2.0
            if amplitude > 0 and rise.value > 0:
                try:
                    envelope_trap = Trapezoid(
                        amplitude_v=amplitude, period_s=period,
                        pulse_width_s=pulse_width,
                        rise_s=rise.value, fall_s=rise.value)
                    envelope_trap.validate()
                except DriverError:
                    envelope_trap = None

        for i, f in enumerate(frequencies):
            if f <= bandwidth or volts[i] is None:
                continue
            if envelope_trap is None:
                volts[i] = None
                undriven[f] = (
                    f"{f / 1e6:g} MHz is above this capture's {bandwidth / 1e6:g} MHz "
                    f"bandwidth and the driver declares no rise time, so nothing is known "
                    f"about it"
                )
            else:
                # Continue with the envelope, scaled to meet the transform at the bandwidth
                # so the join has no step.
                volts[i] = complex(envelope_v(envelope_trap, f), 0.0)

        if envelope_trap is not None:
            _match_envelope_at_join(volts, frequencies, bandwidth, envelope_trap)

    return Resolved(list(frequencies), volts, undriven,
                    scale_v=max(values) - min(values))


def _match_envelope_at_join(
    volts: list[complex | None], frequencies: list[float], bandwidth: float,
    envelope_trap: Trapezoid,
) -> None:
    """Scale the envelope so it meets the transform at the bandwidth.

    §9.3 asks the waveform to "continue above that with the envelope"; an unscaled envelope
    would step at the join by whatever the ratio happens to be, and a step in a source
    spectrum becomes a step in every result that reads it.
    """
    below = [(f, v) for f, v in zip(frequencies, volts)
             if v is not None and f <= bandwidth]
    if not below:
        return
    f_join, v_join = max(below, key=lambda p: p[0])
    predicted = envelope_v(envelope_trap, f_join)
    if predicted <= 0:
        return
    scale = abs(v_join) / predicted
    for i, f in enumerate(frequencies):
        if f > bandwidth and volts[i] is not None:
            volts[i] = volts[i] * scale


def _resolve_spectrum(driver: Driver, frequencies: list[float]) -> Resolved:
    points: list[tuple[float, float]] = driver.payload["points"]
    lo, hi = points[0][0], points[-1][0]
    volts: list[complex | None] = []
    undriven: dict[float, str] = {}
    for f in frequencies:
        if f < lo or f > hi:
            volts.append(None)
            undriven[f] = (
                f"the uploaded spectrum covers {lo / 1e6:g}-{hi / 1e6:g} MHz and says "
                f"nothing at {f / 1e6:g} MHz"
            )
            continue
        volts.append(complex(_dbuv_to_volts(_interpolate_db(points, f)), 0.0))
    return Resolved(list(frequencies), volts, undriven, magnitude_only=True,
                    scale_v=_dbuv_to_volts(max(level for _f, level in points)))


def _interpolate_db(points: list[tuple[float, float]], frequency_hz: float) -> float:
    """Interpolate a level in dB against log frequency, the way §16.2 reads a spectrum."""
    for (f0, l0), (f1, l1) in zip(points, points[1:]):
        if f0 <= frequency_hz <= f1:
            if f1 == f0:
                return l0
            along = math.log10(frequency_hz / f0) / math.log10(f1 / f0)
            return l0 + along * (l1 - l0)
    raise DriverError(f"{frequency_hz} Hz is outside the uploaded spectrum")


def _dbuv_to_volts(dbuv: float) -> float:
    return 10.0 ** (dbuv / 20.0) * 1e-6


def volts_to_dbuv(volts: float) -> float:
    if volts <= 0:
        raise DriverError("a level of zero has no dBuV value")
    return 20.0 * math.log10(volts / 1e-6)


def describe_corners(driver: Driver) -> tuple[float, float] | None:
    """The two envelope corners, for the preview (§9.4). Trapezoids only."""
    if driver.kind != "trapezoid":
        return None
    return corner_frequencies(driver.trapezoid())
