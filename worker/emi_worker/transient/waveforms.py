"""Standard disturbance waveforms, as the sources that drive a simulation.

IEC 61000-4-2 specifies an ESD generator by the current it delivers, not by its components: the
150 pF / 330 Ω circuit in the standard is "typical", and conformance is judged on the waveform.
So the source here is the waveform itself, from the equation the standard gives with its
Figure 2, and the calibration points in its Table 3 are what the tests hold it to.

The source is sampled into a piecewise-linear current rather than written as an expression for
ngspice to evaluate. That keeps one implementation of the equation -- this one, which the tests
check -- instead of a second copy in another language that nobody measures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

STANDARD = "IEC 61000-4-2:2008"


@dataclass(frozen=True)
class EsdLevel:
    """One row of Table 3, contact discharge."""

    level: int
    kv: float
    peak_a: float
    i30_a: float
    i60_a: float


#: IEC 61000-4-2:2008 Table 1 and Table 3, contact discharge.
CONTACT_LEVELS: dict[int, EsdLevel] = {
    1: EsdLevel(1, 2.0, 7.5, 4.0, 2.0),
    2: EsdLevel(2, 4.0, 15.0, 8.0, 4.0),
    3: EsdLevel(3, 6.0, 22.5, 12.0, 6.0),
    4: EsdLevel(4, 8.0, 30.0, 16.0, 8.0),
}

#: Table 3 tolerances: first peak ±15 %, rise time 0.8 ns ±25 %, 30 ns and 60 ns points ±30 %.
RISE_NS = 0.8
PEAK_TOLERANCE = 0.15
RISE_TOLERANCE = 0.25
TAIL_TOLERANCE = 0.30

#: The equation given with Figure 2, parameters at 4 kV. Scaled linearly for other levels.
TAU1_S, TAU2_S, TAU3_S, TAU4_S = 1.1e-9, 2e-9, 12e-9, 37e-9
I1_AT_4KV, I2_AT_4KV = 16.6, 9.3
N = 1.8


def _k(ta: float, tb: float, n: float) -> float:
    return math.exp(-(ta / tb) * (n * tb / ta) ** (1.0 / n))


K1 = _k(TAU1_S, TAU2_S, N)
K2 = _k(TAU3_S, TAU4_S, N)

#: How long a simulation runs. The standard measures to 60 ns after the 10 % crossing; by
#: 100 ns the current is a small fraction of its peak and nothing new happens on a board.
DURATION_S = 100e-9


def esd_current(t: float, kv: float) -> float:
    """Contact discharge current in amps at time t (seconds), for a charge voltage in kV."""
    if t <= 0.0:
        return 0.0
    a = (t / TAU1_S) ** N
    b = (t / TAU3_S) ** N
    scale = kv / 4.0
    return scale * (
        I1_AT_4KV / K1 * a / (1.0 + a) * math.exp(-t / TAU2_S)
        + I2_AT_4KV / K2 * b / (1.0 + b) * math.exp(-t / TAU4_S)
    )


def esd_samples(kv: float, polarity: int = 1, end_s: float = DURATION_S) -> list[tuple[float, float]]:
    """The current as (time, amps) breakpoints for a piecewise-linear source.

    Dense through the first peak, where the 0.8 ns rise lives, and coarse over the tail,
    where the current changes on a scale of tens of nanoseconds.
    """
    times: list[float] = []
    t = 0.0
    while t < 5e-9:
        times.append(t)
        t += 20e-12
    while t <= end_s + 1e-15:
        times.append(t)
        t += 0.5e-9
    return [(t, polarity * esd_current(t, kv)) for t in times]


@dataclass(frozen=True)
class WaveformMeasurement:
    peak_a: float
    rise_ns: float
    t10_ns: float
    i30_a: float
    i60_a: float


def _interp(samples: list[tuple[float, float]], t: float) -> float:
    if t <= samples[0][0]:
        return samples[0][1]
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        if t0 <= t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return samples[-1][1]


def _crossing(samples: list[tuple[float, float]], level: float) -> float:
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        if v0 < level <= v1:
            return t0 + (level - v0) * (t1 - t0) / (v1 - v0)
    return math.nan


def measure(samples: list[tuple[float, float]]) -> WaveformMeasurement:
    """Measure a current waveform the way Table 3 does.

    The first peak is the maximum in the first 5 ns -- the second hump peaks near 20 ns and
    must not be mistaken for it. The 30 ns and 60 ns points are timed from the instant the
    current first reaches 10 % of that peak.
    """
    mags = [(t, abs(v)) for t, v in samples]
    head = [s for s in mags if s[0] <= 5e-9] or mags
    peak = max(v for _, v in head)
    t10 = _crossing(mags, 0.1 * peak)
    t90 = _crossing(mags, 0.9 * peak)
    return WaveformMeasurement(
        peak_a=peak,
        rise_ns=(t90 - t10) * 1e9,
        t10_ns=t10 * 1e9,
        i30_a=_interp(mags, t10 + 30e-9),
        i60_a=_interp(mags, t10 + 60e-9),
    )


def conformance(level: int, samples: list[tuple[float, float]] | None = None) -> list[str]:
    """Where a waveform misses Table 3, in words. Empty when it conforms."""
    spec = CONTACT_LEVELS[level]
    m = measure(samples if samples is not None else esd_samples(spec.kv))
    problems = []

    def within(name: str, got: float, want: float, tol: float, unit: str) -> None:
        if abs(got - want) > tol * want:
            problems.append(f"{name} {got:.3g} {unit}, want {want:g} {unit} ±{tol:.0%}")

    within("first peak", m.peak_a, spec.peak_a, PEAK_TOLERANCE, "A")
    within("rise time", m.rise_ns, RISE_NS, RISE_TOLERANCE, "ns")
    within("current at 30 ns", m.i30_a, spec.i30_a, TAIL_TOLERANCE, "A")
    within("current at 60 ns", m.i60_a, spec.i60_a, TAIL_TOLERANCE, "A")
    return problems
