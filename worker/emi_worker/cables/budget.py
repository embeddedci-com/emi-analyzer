"""Tier A: how much common-mode current a cable can carry before it fails (§4, §6.3).

This runs in seconds on any worker and needs no solve at all, which is what makes it the tier
worth reaching for first. It answers the question a designer actually asks — *how much current
is too much* — by inverting §6.3's closed form rather than predicting a field from a current
nobody has measured yet.

    E ≈ 2π × 10⁻⁷ · f · L · I_cm / r        V/m, with f in Hz and L, r in m

so the current that just reaches a limit E is

    I_max = E · r / (2π × 10⁻⁷ · f · L)

**Valid below λ/10 only.** Above that the current is no longer uniform along the cable, the
formula's central assumption fails, and it over-predicts. The budget says where that happens
instead of quietly continuing, because a bound that stops being a bound without saying so is
worse than no bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from emi_worker.cables.library import Cable

SPEED_OF_LIGHT = 299_792_458.0

#: 2π × 10⁻⁷, the constant in §6.3's closed form.
K = 2.0 * math.pi * 1e-7

#: A ground plane reflects the cable's field back in phase, which can add this much. The
#: measurement standards all specify a ground plane, so it is on by default: leaving it out
#: would make every budget look 6 dB more generous than the test bench will.
GROUND_REFLECTION_DB = 6.0

#: Above this fraction of a wavelength the uniform-current assumption fails.
UNIFORM_CURRENT_FRACTION = 0.1


def wavelength_m(frequency_hz: float) -> float:
    if frequency_hz <= 0:
        raise ValueError("frequency must be positive")
    return SPEED_OF_LIGHT / frequency_hz


def field_v_per_m(
    frequency_hz: float, length_m: float, current_a: float, distance_m: float,
    *, ground_reflection: bool = True,
) -> float:
    """§6.3's closed form: the field a uniform common-mode current produces."""
    if distance_m <= 0:
        raise ValueError("distance must be positive")
    e = K * frequency_hz * length_m * current_a / distance_m
    return e * (10 ** (GROUND_REFLECTION_DB / 20.0) if ground_reflection else 1.0)


def current_for_field(
    frequency_hz: float, length_m: float, field_v_per_m_limit: float, distance_m: float,
    *, ground_reflection: bool = True,
) -> float:
    """The inverse: the common-mode current that just reaches a field limit."""
    if frequency_hz <= 0 or length_m <= 0:
        raise ValueError("frequency and length must be positive")
    if distance_m <= 0:
        raise ValueError("distance must be positive")
    scale = 10 ** (GROUND_REFLECTION_DB / 20.0) if ground_reflection else 1.0
    return field_v_per_m_limit * distance_m / (K * frequency_hz * length_m * scale)


def dbuv_per_m_to_v_per_m(dbuv: float) -> float:
    return 10 ** (dbuv / 20.0) * 1e-6


def a_to_dbua(current_a: float) -> float:
    if current_a <= 0:
        raise ValueError("a current of zero has no dBµA value")
    return 20.0 * math.log10(current_a / 1e-6)


@dataclass(frozen=True)
class BudgetPoint:
    frequency_hz: float
    #: The limit at this frequency, in the standard's own unit.
    limit_dbuv_per_m: float
    #: The common-mode current that just reaches it.
    max_current_a: float
    #: True where the cable is longer than λ/10 and the closed form no longer bounds. Only
    #: meaningful for a closed-form budget; a solver budget has no such restriction, which is
    #: the reason it exists.
    beyond_uniform_current: bool
    #: Volts per metre, at the standard's distance, per amp of common-mode current. This is
    #: the quantity the budget is really made of; the current is it divided into the limit.
    e_per_amp: float = 0.0
    #: True at a local peak in radiation per amp — where this cable is at its best as an
    #: antenna, and so where a clock harmonic landing is worst.
    #:
    #: Detected from ``e_per_amp`` rather than from a reactance sign change. A sign change
    #: finds *series* resonances only; the strongest radiating peak of a cable against a
    #: ground plane is often a parallel one, where the reactance passes through a pole and
    #: never changes sign at all. Measured on a 1 m USB cable: a 14 dB peak in radiation at
    #: 40 MHz that a sign-change test does not see.
    radiation_peak: bool = False

    @property
    def max_current_dbua(self) -> float:
        return a_to_dbua(self.max_current_a)


@dataclass
class Budget:
    cable_id: str
    length_m: float
    standard_id: str
    distance_m: float
    points: list[BudgetPoint]

    def tightest(self) -> BudgetPoint | None:
        """The frequency where the least current is allowed — the one worth quoting.

        Taken only from the points where the formula still holds. The tightest point of an
        invalid extrapolation is not a useful number to put in front of anybody.
        """
        usable = [p for p in self.points if not p.beyond_uniform_current]
        return min(usable, key=lambda p: p.max_current_a) if usable else None

    #: "closed-form" or "solver". A reader has to know which, because they are valid over
    #: different ranges and only one of them knows about resonance.
    method: str = "closed-form"

    def valid_up_to_hz(self) -> float | None:
        """Where λ/10 is reached for this length, above which the closed form stops bounding.

        None for a solver budget: it has no such limit, which is why §6.3 is a sanity bound
        rather than the engine.
        """
        if self.method == "solver" or self.length_m <= 0:
            return None
        return UNIFORM_CURRENT_FRACTION * SPEED_OF_LIGHT / self.length_m

    def radiation_peaks(self) -> list[float]:
        """Frequencies where this cable radiates best — the ones to keep harmonics away from."""
        return [p.frequency_hz for p in self.points if p.radiation_peak]

    def grid_too_coarse(self) -> bool:
        """Whether the frequency grid can resolve a peak at all.

        An empty peak list means one of two very different things: this cable has no strong
        resonance in the band, or nobody looked closely enough to see one. Without this a
        coarse grid reports "no peaks" and reads like the first.
        """
        if len(self.points) < 3:
            return True
        steps = sorted(b.frequency_hz / a.frequency_hz
                       for a, b in zip(self.points, self.points[1:]) if a.frequency_hz > 0)
        median = steps[len(steps) // 2]
        # Three points inside a peak window is the minimum that can show a rise and a fall.
        return (1.0 + PEAK_WINDOW) / (1.0 - PEAK_WINDOW) < median ** 3


def budget(
    cable: Cable,
    frequencies_hz: list[float],
    standard_id: str = "fcc-15b-radiated-3m",
    *,
    ground_reflection: bool = True,
) -> Budget:
    """How much common-mode current this cable may carry at each frequency."""
    from emi_worker.compliance.limits import limit_at, standard

    std = standard(standard_id)
    if std.scan != "radiated":
        raise ValueError(f"{standard_id} is a {std.scan} standard; a cable budget is radiated")
    distance = std.distance_m or 3.0

    limit_length = UNIFORM_CURRENT_FRACTION * SPEED_OF_LIGHT / cable.length_m
    points = []
    for f in sorted(frequencies_hz):
        limit_db = limit_at(standard_id, f)
        i_max = current_for_field(
            f, cable.length_m, dbuv_per_m_to_v_per_m(limit_db), distance,
            ground_reflection=ground_reflection,
        )
        points.append(BudgetPoint(
            frequency_hz=f, limit_dbuv_per_m=limit_db, max_current_a=i_max,
            beyond_uniform_current=f > limit_length,
        ))
    return Budget(cable_id=cable.id, length_m=cable.length_m, standard_id=standard_id,
                  distance_m=distance, points=points)


def solver_budget(
    cable: Cable,
    frequencies_hz: list[float],
    standard_id: str = "fcc-15b-radiated-3m",
    *,
    height_m: float = 1.0,
    board_span_m: float = 0.1,
) -> Budget:
    """The real Tier A budget: how much current this cable may carry, from the antenna solver.

    The closed form cannot do this job. It is valid below λ/10, which for a 1 m cable is
    30 MHz — the bottom of the radiated range — so a closed-form budget for any ordinary cable
    is an extrapolation everywhere it would be used. The solver has no such restriction and,
    unlike the closed form, knows where the cable resonates, which is the question §4 says this
    tier exists to answer.

    Costs a few hundred milliseconds: one nec2c run per frequency, each about 2.5 ms.
    """
    from emi_worker.cables import nec
    from emi_worker.compliance.limits import limit_at, standard

    std = standard(standard_id)
    if std.scan != "radiated":
        raise ValueError(f"{standard_id} is a {std.scan} standard; a cable budget is radiated")
    distance = std.distance_m or 3.0

    ring = nec.ObservationRing(distance_m=distance)
    results = []
    for f in sorted(frequencies_hz):
        deck = nec.Deck(
            length_m=cable.length_m, frequency_hz=f, height_m=height_m,
            board_span_m=board_span_m, far_end=cable.far_end, ring=ring,
            choke_ohm=(cable.cm_choke.z_ohm_at_100mhz * f / 100e6) if cable.cm_choke else None,
        )
        results.append((f, nec.run(deck)))

    points = []
    for i, (f, r) in enumerate(results):
        limit_db = limit_at(standard_id, f)
        e_per_amp = r.e_per_amp()
        # A cable that radiates nothing per amp has no current limit, which is true and
        # useless; report it as unbounded rather than dividing by zero.
        i_max = dbuv_per_m_to_v_per_m(limit_db) / e_per_amp if e_per_amp > 0 else math.inf

        points.append(BudgetPoint(
            frequency_hz=f, limit_dbuv_per_m=limit_db, max_current_a=i_max,
            beyond_uniform_current=False, e_per_amp=e_per_amp,
        ))

    points = _mark_radiation_peaks(points, cable.length_m)

    return Budget(cable_id=cable.id, length_m=cable.length_m, standard_id=standard_id,
                  distance_m=distance, points=points, method="solver")


#: How far a point has to stand above the surrounding band to count as a radiation peak, in
#: dB. Low enough to catch a broad parallel resonance, high enough that ripple on a smooth
#: curve does not produce a list of frequencies to avoid that is simply every frequency.
PEAK_MARGIN_DB = 1.0

#: The band a peak is judged against, as a fraction of its own frequency.
#:
#: Judged against a window rather than the two adjacent points, because adjacent points move
#: closer together as the grid gets finer and a *broad* resonance then rises less between
#: them — so a finer grid would detect fewer peaks, which is exactly backwards. Measured: a
#: 40 MHz parallel resonance found on a 15 % grid and missed on an 8 % one.
PEAK_WINDOW = 0.25

#: The window is also capped at this fraction of the cable's own resonance spacing, c/(2L).
#:
#: A cable's resonances are spaced roughly evenly in **absolute** frequency — about 150 MHz
#: for a 1 m cable — while a fractional window grows with frequency. Left uncapped, ±25 % is
#: ±10 MHz at 40 MHz and ±200 MHz at 800, which is wider than the spacing: the window then
#: contains several resonances and which one it calls the peak depends on where the grid
#: happens to sample. Measured before this cap: peaks below 300 MHz stable to 3 % across five
#: grids, and the highest one wandering between 632 and 796 MHz.
PEAK_WINDOW_OF_SPACING = 1.0 / 3.0


def _mark_radiation_peaks(
    points: list[BudgetPoint], length_m: float | None = None
) -> list[BudgetPoint]:
    """Mark local maxima in radiation per amp, judged against a band around each point."""
    out = list(points)
    cap = (SPEED_OF_LIGHT / (2.0 * length_m)) * PEAK_WINDOW_OF_SPACING if length_m else None
    for i, point in enumerate(out):
        if point.e_per_amp <= 0:
            continue
        half = point.frequency_hz * PEAK_WINDOW
        if cap is not None:
            half = min(half, cap)
        lo, hi = point.frequency_hz - half, point.frequency_hz + half
        band = [p for p in out if lo <= p.frequency_hz <= hi and p.e_per_amp > 0]
        # A window with nothing either side cannot say whether this is a peak. The ends of the
        # grid are the usual case, and claiming a peak there would put the lowest frequency on
        # every list of frequencies to avoid.
        if len(band) < 3 or point.frequency_hz in (band[0].frequency_hz, band[-1].frequency_hz):
            continue
        if point.e_per_amp < max(p.e_per_amp for p in band) - 1e-30:
            continue
        floor = min(p.e_per_amp for p in band)
        if 20 * math.log10(point.e_per_amp / floor) >= PEAK_MARGIN_DB:
            out[i] = BudgetPoint(**{**point.__dict__, "radiation_peak": True})
    return out
