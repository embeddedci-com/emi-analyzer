"""Driver spectra.

This is the Python half of a computation that also lives in TypeScript
(``webapp/src/lib/driverSpectrum.ts``). Both are checked against
``server/emi/testdata/driver_fixtures.json`` so they cannot drift — the cost model's rule
(``estimate.py``), for the same reason: a number shown in the browser and a number used in a
worker result have to be the same number.

Two things live here.

**A line spectrum from a driver description** (§9). A trapezoid is not special-cased: it is
four breakpoints of a piecewise-linear periodic waveform, and the exact Fourier coefficient
of such a waveform has a closed form. That buys three things at once — the textbook
sinc-times-sinc result falls out of it for a symmetric edge, an asymmetric rise and fall is
*exact* rather than approximated by an average, and the same routine will transform an
uploaded ``waveform`` later, since a sampled waveform is piecewise linear between samples.

**Re-weighting a finished solve** (§8). openEMS solves a linear structure, so a solve already
contains the response to every source inside its band; attaching a driver is arithmetic on
the port spectra the solve recorded. M0 measured that this is exact to 0.01 dB wherever the
stored record has energy at the frequency (see ``docs/m0-findings.md``, D2).
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass


class DriverError(ValueError):
    """A driver description that cannot be turned into a spectrum."""


@dataclass(frozen=True)
class Trapezoid:
    """A trapezoidal pulse train, in the units the ``emi-driver`` document uses.

    ``pulse_width_s`` is the width at 50 % amplitude, which is both what the closed form in
    §9.1 means by tau and what an instrument reports as pulse width. The flat top is
    therefore ``pulse_width_s - (rise_s + fall_s) / 2``, and a description whose edges do not
    leave room for it is rejected rather than silently clipped.
    """

    amplitude_v: float
    period_s: float
    pulse_width_s: float
    rise_s: float
    fall_s: float

    def flat_top_s(self) -> float:
        return self.pulse_width_s - (self.rise_s + self.fall_s) / 2.0

    def validate(self) -> None:
        if self.period_s <= 0:
            raise DriverError("period must be positive")
        if self.rise_s <= 0 or self.fall_s <= 0:
            raise DriverError("rise and fall must be positive")
        if self.pulse_width_s <= 0:
            raise DriverError("pulse width must be positive")
        flat = self.flat_top_s()
        if flat < 0:
            raise DriverError(
                f"the edges do not fit inside the pulse: a {self.pulse_width_s * 1e9:.3g} ns "
                f"pulse at 50 % cannot hold a {self.rise_s * 1e9:.3g} ns rise and a "
                f"{self.fall_s * 1e9:.3g} ns fall"
            )
        if self.rise_s + flat + self.fall_s > self.period_s:
            raise DriverError(
                f"one pulse is longer than the period: "
                f"{(self.rise_s + flat + self.fall_s) * 1e9:.4g} ns in a "
                f"{self.period_s * 1e9:.4g} ns period"
            )

    def breakpoints(self) -> tuple[list[float], list[float]]:
        """Times and values of the waveform's corners, one period starting at t = 0."""
        self.validate()
        flat = self.flat_top_s()
        t = [0.0, self.rise_s, self.rise_s + flat, self.rise_s + flat + self.fall_s]
        v = [0.0, self.amplitude_v, self.amplitude_v, 0.0]
        if t[-1] < self.period_s:
            t.append(self.period_s)
            v.append(0.0)
        return t, v


def piecewise_linear_series(
    times: list[float], values: list[float], period_s: float, n: int
) -> complex:
    """Exact complex Fourier coefficient ``C_n`` of a periodic piecewise-linear waveform.

    ``times`` and ``values`` describe one period and must be non-decreasing in time. The
    waveform is linear between consecutive points and repeats with ``period_s``; a final
    point short of the period is held at its value to the end.

    The integral of ``(a + b*u) * exp(-j*w*u)`` over a segment is elementary, so no numerical
    quadrature is involved and the result does not degrade at high harmonic numbers the way a
    sampled DFT does.
    """
    if len(times) != len(values):
        raise DriverError("times and values must be the same length")
    if len(times) < 2:
        raise DriverError("a waveform needs at least two points")
    if period_s <= 0:
        raise DriverError("period must be positive")
    if n < 0:
        raise DriverError("harmonic number must not be negative")

    pts_t = list(times)
    pts_v = list(values)
    if pts_t[-1] < period_s:
        pts_t.append(period_s)
        pts_v.append(pts_v[-1])

    w = 2.0 * math.pi * n / period_s
    total = 0.0 + 0.0j
    for k in range(len(pts_t) - 1):
        length = pts_t[k + 1] - pts_t[k]
        if length < 0:
            raise DriverError("waveform times must not go backwards")
        if length == 0:
            continue
        a = pts_v[k]
        b = (pts_v[k + 1] - pts_v[k]) / length
        if n == 0:
            total += a * length + b * length * length / 2.0
            continue
        jw = 1j * w
        e = cmath.exp(-jw * length)
        total += cmath.exp(-jw * pts_t[k]) * (
            a * (1.0 - e) / jw - b * length * e / jw - b * (1.0 - e) / (w * w)
        )
    return total / period_s


def trapezoid_series(trap: Trapezoid, harmonics: int) -> list[tuple[float, float]]:
    """``(frequency_hz, amplitude_v)`` for harmonics 1..``harmonics``.

    The amplitude is the one-sided peak of that harmonic — ``2 * |C_n|`` — which is what
    §9.1's closed form gives and what a spectrum analyser in peak mode reads.
    """
    trap.validate()
    if harmonics < 1:
        raise DriverError("ask for at least one harmonic")
    t, v = trap.breakpoints()
    out = []
    for n in range(1, harmonics + 1):
        c = piecewise_linear_series(t, v, trap.period_s, n)
        out.append((n / trap.period_s, 2.0 * abs(c)))
    return out


def corner_frequencies(trap: Trapezoid) -> tuple[float, float]:
    """The envelope's two breakpoints, ``1/(pi*tau)`` and ``1/(pi*t_edge)``.

    The second corner uses the **faster** of the two edges. Both edges contribute, and above
    the corner the faster one dominates; taking the slower would put the corner too low and
    under-predict every harmonic above it, which is the wrong direction for an emissions
    tool.
    """
    trap.validate()
    edge = min(trap.rise_s, trap.fall_s)
    return 1.0 / (math.pi * trap.pulse_width_s), 1.0 / (math.pi * edge)


def envelope_v(trap: Trapezoid, frequency_hz: float) -> float:
    """The trapezoid's spectral envelope at one frequency, in volts.

    Flat at ``2*A*tau/T`` to the first corner, then -20 dB/decade, then -40 dB/decade. This
    bounds the line spectrum rather than reproducing it; it is what the preview draws and
    what continues an uploaded waveform above its capture bandwidth (§9.3).
    """
    trap.validate()
    if frequency_hz <= 0:
        raise DriverError("envelope is defined above DC")
    f1, f2 = corner_frequencies(trap)
    flat = 2.0 * abs(trap.amplitude_v) * trap.pulse_width_s / trap.period_s
    if frequency_hz <= f1:
        return flat
    if frequency_hz <= f2:
        return flat * (f1 / frequency_hz)
    return flat * (f1 / f2) * (f2 / frequency_hz) ** 2


def reweight(
    v_source: complex, source_impedance_ohm: float, v_port: complex, i_port: complex
) -> complex:
    """The factor every field at this frequency is multiplied by (§8).

    ``v_port`` and ``i_port`` are what the solve recorded at the port, so their ratio is the
    input impedance the driver will see and ``i_port`` is the current the solve actually
    used. The driver pushes ``v_source / (Z_s + Z_in)`` instead.
    """
    if i_port == 0:
        raise DriverError(
            "the solve recorded no current at this port, so there is nothing to re-weight; "
            "the frequency is outside the excitation band or the run produced zeros"
        )
    z_in = v_port / i_port
    denom = source_impedance_ohm + z_in
    if denom == 0:
        raise DriverError("source and input impedance cancel exactly; check the driver")
    return (v_source / denom) / i_port


def reweight_db(
    v_source: complex, source_impedance_ohm: float, v_port: complex, i_port: complex
) -> float:
    """``reweight`` as a dB offset, which is how the near-field shader applies it (§10)."""
    scale = abs(reweight(v_source, source_impedance_ohm, v_port, i_port))
    if scale <= 0:
        raise DriverError("a re-weighting factor of zero has no dB value")
    return 20.0 * math.log10(scale)
