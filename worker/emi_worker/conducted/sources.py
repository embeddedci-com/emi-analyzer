"""A switching regulator's input current: a pulse train for a buck, a triangle for a boost.

A buck draws its inductor current from the input while the high-side switch is on and nothing
while it is off: a pulse of average height I_in / D, width D / f_sw at half height, with the
switch's edges, and a top that slopes with the inductor's ripple. The slope is kept (an assumed
30 % ripple, the usual design figure) although it moves the first harmonic by at most 0.3 dB:
a flat top has exact nulls at every harmonic that is a multiple of 1/D, and there it reads
nothing at all where a real converter reads a line 30-40 dB down. An inverting buck-boost draws
the same shape (its input switch carries the inductor current for D of the period), and so does
a four-switch buck-boost in buck mode, which is how one is modelled.

A boost's inductor sits in its input, so the input current is the inductor current: continuous,
I_in on average with a triangle on top, rising for D of the period and falling for the rest. The
triangle's peak to peak is the inductor ripple, V_in·D / (f_sw·L) with D = 1 - V_in/V_out (TI
SLVA372C, "Basic Calculation of a Boost Converter's Power Stage", the inductor ripple current).
With no inductance or input voltage to work it from, it is the same assumed 30 % of I_in. Its
nth harmonic is ΔI·|sin(πnD)| / (π²n²D(1-D)) peak, which falls as 1/n² where a pulse train's
falls as 1/n: a boost is far quieter on its input than a buck of the same power.

Every parameter is either given by the user or an assumed default, and a result carries which,
because a default switching frequency moves every harmonic and is not a property of the board.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field

from ..drivers.spectrum import RMS_PER_PEAK, piecewise_linear_series
from .regulators import TOPOLOGIES

DEFAULT_FREQUENCY_HZ = 500e3
DEFAULT_INPUT_CURRENT_A = 0.5
DEFAULT_DUTY = 0.5
DEFAULT_RISE_S = 10e-9
DEFAULT_PHASE_DEG = 0.0
#: Peak-to-peak inductor ripple as a fraction of the average current, when it cannot be worked
#: out from the inductor. Not a user setting.
RIPPLE = 0.3

#: Accepted range of each parameter. The server checks the same ranges (server/emi/conducted.go).
LIMITS = {
    "frequency_hz": (10e3, 10e6),
    "input_current_a": (1e-3, 100.0),
    "duty": (0.02, 0.98),
    "rise_s": (0.1e-9, 1e-6),
    "phase_deg": (0.0, 360.0),
    "inductance_h": (10e-9, 10e-3),
}
#: Topologies whose input current is a pulse train; a boost's is a triangle.
PULSED = ("buck", "inverting", "buck-boost")


@dataclass
class Param:
    value: float
    #: "user", "assumed", "rail names" for a duty worked out from the input and output voltages
    #: the net names give, or "board" for an inductance read from the part's value.
    source: str

    @property
    def assumed(self) -> bool:
        return self.source not in ("user", "board")

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
    topology: str = "buck"
    phase_deg: Param = field(default_factory=lambda: Param(DEFAULT_PHASE_DEG, "assumed"))
    inductance_h: Param | None = None
    #: The input voltage from the rail's name, for a boost's inductor ripple.
    v_in: float | None = None
    #: The regulator shares its clock with another source (one of a PMIC's outputs), so its phase
    #: matters and is reported as assumed until given.
    phased: bool = False
    #: The ripple worked out from the inductor was more than twice the input current.
    capped: bool = False

    @property
    def pulsed(self) -> bool:
        return self.topology in PULSED

    def relevant(self) -> list[str]:
        """The parameters this source's waveform depends on."""
        names = ["frequency_hz", "input_current_a", "duty"]
        if self.pulsed:
            names.append("rise_s")
        elif self.v_in:
            names.append("inductance_h")
        if self.phased or self.phase_deg.source == "user":
            names.append("phase_deg")
        return names

    @property
    def assumed(self) -> list[str]:
        return [name for name in self.relevant() if self._param(name) is None or self._param(name).assumed]

    def _param(self, name: str) -> Param | None:
        return getattr(self, name)

    def ripple_pp(self) -> tuple[float, str]:
        """A boost's inductor ripple, peak to peak, and where it came from."""
        i_in = self.input_current_a.value
        if self.inductance_h is not None and self.v_in:
            d, f, l_h = self.duty.value, self.frequency_hz.value, self.inductance_h.value
            ripple = self.v_in * d / (f * l_h)
            if ripple > 2 * i_in:
                self.capped = True
                return 2 * i_in, "inductor, capped at the edge of discontinuous mode"
            return ripple, "inductor"
        return self.ripple * i_in, "assumed"

    def waveform(self) -> tuple[list[float], list[float], float]:
        """(times, amperes, period) of one period, starting at the switch turning on."""
        f = self.frequency_hz.value
        d = self.duty.value
        period = 1.0 / f
        if not self.pulsed:
            i_in = self.input_current_a.value
            ripple, _ = self.ripple_pp()
            lo, hi = i_in - ripple / 2, i_in + ripple / 2
            return [0.0, d * period, period], [lo, hi, lo], period
        tr = self.rise_s.value
        width = d * period
        if width < tr or width + tr > period:
            raise ValueError(
                f"{self.ref}: a {tr * 1e9:.3g} ns edge does not fit a {d:.0%} duty cycle at "
                f"{f / 1e3:.4g} kHz"
            )
        top = self.input_current_a.value / d
        lo, hi = top * (1 - self.ripple / 2), top * (1 + self.ripple / 2)
        return [0.0, tr, width, width + tr], [0.0, lo, hi, 0.0], period

    def phasors(self, first: int, last: int) -> list[tuple[int, float, complex]]:
        """(n, frequency, RMS amperes as a phasor) for harmonics first..last, with the phase.

        A delay of φ/360 of a period multiplies harmonic n by e^(-j·n·φ).
        """
        t, i, period = self.waveform()
        shift = math.radians(self.phase_deg.value)
        return [
            (n, n / period,
             2.0 * piecewise_linear_series(t, i, period, n) * RMS_PER_PEAK * cmath.exp(-1j * n * shift))
            for n in range(first, last + 1)
        ]

    def harmonics(self, first: int, last: int) -> list[tuple[int, float, float]]:
        """(n, frequency, RMS amperes) for harmonics first..last."""
        return [(n, f, abs(a)) for n, f, a in self.phasors(first, last)]

    def as_dict(self) -> dict:
        out = {name: getattr(self, name).as_dict() for name in ("frequency_hz", "input_current_a", "duty",
                                                                "rise_s", "phase_deg")}
        if self.inductance_h is not None:
            out["inductance_h"] = self.inductance_h.as_dict()
        return out


def from_params(ref: str, given: dict | None, duty_from_rails: float | None, topology: str = "buck",
                inductance_h: float | None = None, v_in: float | None = None,
                phased: bool = False) -> RegulatorSource:
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

    chosen = given.get("topology")
    if chosen is not None:
        if chosen not in TOPOLOGIES:
            raise ValueError(f"{ref}: topology must be one of {', '.join(TOPOLOGIES)}")
        if chosen != topology:
            # The rail names' duty belongs to the topology they were worked out for.
            duty_from_rails = None
        topology = chosen
    if duty_from_rails is not None:
        duty = pick("duty", duty_from_rails, "rail names")
    else:
        duty = pick("duty", DEFAULT_DUTY)
    inductance = None
    if given.get("inductance_h") is not None:
        inductance = pick("inductance_h", 0.0)
    elif inductance_h is not None and LIMITS["inductance_h"][0] <= inductance_h <= LIMITS["inductance_h"][1]:
        inductance = Param(inductance_h, "board")
    return RegulatorSource(
        ref=ref,
        frequency_hz=pick("frequency_hz", DEFAULT_FREQUENCY_HZ),
        input_current_a=pick("input_current_a", DEFAULT_INPUT_CURRENT_A),
        duty=duty,
        rise_s=pick("rise_s", DEFAULT_RISE_S),
        topology=topology,
        phase_deg=pick("phase_deg", DEFAULT_PHASE_DEG),
        inductance_h=inductance,
        v_in=v_in,
        phased=phased,
    )
