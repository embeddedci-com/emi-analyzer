"""A switching regulator's input current, as a trapezoidal pulse train.

A buck draws its inductor current from the input while the high-side switch is on and nothing
while it is off: a pulse of average height I_in / D, width D / f_sw at half height, with the
switch's edges, and a top that slopes with the inductor's ripple. The slope is kept (an assumed
30 % ripple, the usual design figure) although it moves the first harmonic by at most 0.3 dB:
a flat top has exact nulls at every harmonic that is a multiple of 1/D, and there it reads
nothing at all where a real converter reads a line 30-40 dB down.

Every parameter is either given by the user or an assumed default, and a result carries which,
because a default switching frequency moves every harmonic and is not a property of the board.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..drivers.spectrum import RMS_PER_PEAK, piecewise_linear_series

DEFAULT_FREQUENCY_HZ = 500e3
DEFAULT_INPUT_CURRENT_A = 0.5
DEFAULT_DUTY = 0.5
DEFAULT_RISE_S = 10e-9
#: Peak-to-peak inductor ripple as a fraction of the average current. Not a user setting yet.
RIPPLE = 0.3

#: Accepted range of each parameter. The server checks the same ranges (server/emi/conducted.go).
LIMITS = {
    "frequency_hz": (10e3, 10e6),
    "input_current_a": (1e-3, 100.0),
    "duty": (0.02, 0.98),
    "rise_s": (0.1e-9, 1e-6),
}


@dataclass
class Param:
    value: float
    #: "user", "assumed", or "rail names" for a duty worked out from the input and output
    #: voltages the net names give.
    source: str

    @property
    def assumed(self) -> bool:
        return self.source != "user"

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "assumed": self.assumed}


@dataclass
class RegulatorSource:
    ref: str
    frequency_hz: Param
    input_current_a: Param
    duty: Param
    rise_s: Param
    ripple: float = RIPPLE

    @property
    def assumed(self) -> list[str]:
        return [name for name in LIMITS if getattr(self, name).assumed]

    def waveform(self) -> tuple[list[float], list[float], float]:
        """(times, amperes, period) of one period: rise, sloped top, fall, then zero."""
        f = self.frequency_hz.value
        d = self.duty.value
        tr = self.rise_s.value
        period = 1.0 / f
        width = d * period
        if width < tr or width + tr > period:
            raise ValueError(
                f"{self.ref}: a {tr * 1e9:.3g} ns edge does not fit a {d:.0%} duty cycle at "
                f"{f / 1e3:.4g} kHz"
            )
        top = self.input_current_a.value / d
        lo, hi = top * (1 - self.ripple / 2), top * (1 + self.ripple / 2)
        return [0.0, tr, width, width + tr], [0.0, lo, hi, 0.0], period

    def harmonics(self, first: int, last: int) -> list[tuple[int, float, float]]:
        """(n, frequency, RMS amperes) for harmonics first..last."""
        t, i, period = self.waveform()
        return [
            (n, n / period, 2.0 * abs(piecewise_linear_series(t, i, period, n)) * RMS_PER_PEAK)
            for n in range(first, last + 1)
        ]

    def as_dict(self) -> dict:
        return {name: getattr(self, name).as_dict() for name in LIMITS}


def from_params(ref: str, given: dict | None, duty_from_rails: float | None) -> RegulatorSource:
    """Merge what the user gave with the defaults. Values outside LIMITS are refused."""
    given = given or {}

    def pick(name: str, default: float, default_source: str = "assumed") -> Param:
        raw = given.get(name)
        if raw is None:
            return Param(default, default_source)
        if isinstance(raw, bool):
            raise ValueError(f"{ref}: {name} must be a number")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{ref}: {name} must be a number") from None
        lo, hi = LIMITS[name]
        if not lo <= value <= hi:
            raise ValueError(f"{ref}: {name} must be between {lo:g} and {hi:g}")
        return Param(value, "user")

    if duty_from_rails is not None:
        duty = pick("duty", duty_from_rails, "rail names")
    else:
        duty = pick("duty", DEFAULT_DUTY)
    return RegulatorSource(
        ref=ref,
        frequency_hz=pick("frequency_hz", DEFAULT_FREQUENCY_HZ),
        input_current_a=pick("input_current_a", DEFAULT_INPUT_CURRENT_A),
        duty=duty,
        rise_s=pick("rise_s", DEFAULT_RISE_S),
    )
