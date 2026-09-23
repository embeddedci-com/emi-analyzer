"""Combining paths into a predicted level, a margin, and how much weight it can bear (§16, §17).

Three things happen here, and keeping them apart matters more than any one of them:

* **Combination.** Several radiating paths reach the same receiving antenna at the same
  frequency. Paths belonging to one driver add in **amplitude** — they are coherent, they came
  from the same clock edge, and their relative phase is not something this model knows, so the
  worst case is the honest one. Different drivers are unrelated sources and add in **power**.
* **Margin.** Limit minus level, in dB, positive meaning under.
* **Confidence.** The probability the true margin is positive *under the model's own uncertainty
  budget* — not a pass probability, and labelled uncalibrated until real test results have been
  recorded against it (§17.4).

The budget's σ values are engineering placeholders, chosen conservatively and carried in the
result so a reader can see which ones are doing the work. They are replaced by residuals as the
§19 tests produce them, not by being quietly tuned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: One microvolt per metre, the reference for dBµV/m.
MICRO = 1e-6


def to_dbuv(v_per_m: float) -> float:
    return 20.0 * math.log10(v_per_m / MICRO) if v_per_m > 0 else float("-inf")


def from_dbuv(dbuv: float) -> float:
    return MICRO * 10.0 ** (dbuv / 20.0)


#: Two frequencies closer than this, in parts per million, are the same frequency. Paths from
#: one driver are evaluated at the same harmonics and agree to the last bit; this absorbs the
#: float arithmetic of two clocks whose harmonics coincide, not a disagreement.
SAME_FREQUENCY_PPM = 100.0


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= max(a, b) * SAME_FREQUENCY_PPM / 1e6


@dataclass(frozen=True)
class PathPoint:
    frequency_hz: float
    field_v_per_m: float


@dataclass
class Path:
    """One way energy gets from a driver to the receiving antenna."""

    #: "cable" or "board". Conducted paths are a different scan and do not mix here.
    kind: str
    #: Named in the user's terms, because this is what the report prints: "cable J1 (USB 2.0,
    #: 1 m)", "board region around U3".
    label: str
    #: Which driver drives it. Paths sharing a driver are coherent; paths that do not are not.
    driver_id: str
    points: list[PathPoint] = field(default_factory=list)
    #: σ terms that apply to this path alone (§17.2), as {name: dB}.
    sigma_terms: dict = field(default_factory=dict)
    #: True for a line spectrum -- a clock's harmonics. It has energy at its points and none
    #: between them, so it is never interpolated: a 25 MHz clock puts nothing at 40 MHz. False
    #: for a continuous one (an uploaded analyser spectrum through a transfer function), which
    #: is read between its points by interpolation.
    line: bool = False
    #: The band the path's transfer function covers, when it is wider than its points: a clock
    #: whose first harmonic in band is 50 MHz still says "nothing" at 40 MHz rather than "not
    #: covered". Defaults to the span of the points.
    covered_hz: tuple[float, float] | None = None

    def band(self) -> tuple[float, float] | None:
        if self.covered_hz is not None:
            return self.covered_hz
        if not self.points:
            return None
        fs = [p.frequency_hz for p in self.points]
        return min(fs), max(fs)

    def at(self, frequency_hz: float) -> float | None:
        """The field at one frequency, ``None`` where this path says nothing.

        This used to match frequencies by exact float equality, and the far-field grid and the
        cable grid never share a point, so two paths from one driver were never added. Now:

        * a line spectrum answers at its own lines (to ``SAME_FREQUENCY_PPM``) and is zero
          between them, which is the physics, not a gap;
        * a continuous one is interpolated in dB against log frequency, the way §16.2 reads a
          spectrum, **inside its own band only**. Outside it the answer is ``None``: not
          covered. It is never extrapolated.
        """
        pts = sorted(self.points, key=lambda p: p.frequency_hz)
        band = self.band()
        if band is None:
            return None
        lo, hi = band
        outside = (frequency_hz < lo and not _same(lo, frequency_hz)) \
            or (frequency_hz > hi and not _same(hi, frequency_hz))
        if self.line:
            for p in pts:
                if _same(p.frequency_hz, frequency_hz):
                    return p.field_v_per_m
            # Outside the band nothing was asked, which is not a zero; inside it, between
            # two lines, zero is the answer.
            return None if outside else 0.0
        if outside:
            return None
        for p in pts:
            if _same(p.frequency_hz, frequency_hz):
                return p.field_v_per_m
        if frequency_hz < pts[0].frequency_hz or frequency_hz > pts[-1].frequency_hz:
            return None
        for a, b in zip(pts, pts[1:]):
            if a.frequency_hz <= frequency_hz <= b.frequency_hz:
                if a.field_v_per_m <= 0 or b.field_v_per_m <= 0:
                    # No logarithm of a zero; the larger neighbour is the conservative read.
                    return max(a.field_v_per_m, b.field_v_per_m)
                along = (math.log(frequency_hz / a.frequency_hz)
                         / math.log(b.frequency_hz / a.frequency_hz))
                db = (1 - along) * to_dbuv(a.field_v_per_m) + along * to_dbuv(b.field_v_per_m)
                return from_dbuv(db)
        return None


def common_grid(paths: list[Path], standard_id: str) -> list[float]:
    """Every frequency any path has a point at, inside the standard's scan, merged.

    Combining happens here rather than on any one path's grid, so a board far field on a
    60-point log grid and a cable on its own grid meet at every point either of them has.
    """
    from emi_worker.compliance.limits import in_range

    out: list[float] = []
    for f in sorted({pt.frequency_hz for p in paths for pt in p.points}):
        if not in_range(standard_id, f):
            continue
        if out and _same(out[-1], f):
            continue
        out.append(f)
    return out


@dataclass
class Contribution:
    label: str
    kind: str
    driver_id: str
    field_v_per_m: float
    #: Share of the total **power** at this frequency, 0..1.
    share: float


@dataclass
class Point:
    frequency_hz: float
    field_v_per_m: float
    limit_dbuv_per_m: float
    contributions: list[Contribution] = field(default_factory=list)
    #: Paths that say nothing here -- outside their own band. The level is then a lower bound,
    #: and the completeness gate is what decides whether that matters.
    uncovered: list[str] = field(default_factory=list)
    #: True when any path here was summed in amplitude with another of the same driver, which
    #: makes the individual shares indicative rather than exact (§16.4).
    shares_indicative: bool = False

    @property
    def field_dbuv_per_m(self) -> float:
        return to_dbuv(self.field_v_per_m)

    @property
    def margin_db(self) -> float:
        return self.limit_dbuv_per_m - self.field_dbuv_per_m


def combine(paths: list[Path], frequencies: list[float], standard_id: str) -> list[Point]:
    """§16.2's combination, frequency by frequency.

        E_d(f) = Σ_paths |E_{d,path}(f)|      one driver's paths add in amplitude
        E(f)   = sqrt( Σ_d E_d(f)² )          different drivers add in power

    Amplitude for one driver is the worst case over the relative phase this model does not
    know. It is deliberately not an average: two paths from the same clock can be in phase, and
    a prediction that assumed otherwise would be optimistic exactly where it matters.
    """
    from emi_worker.compliance.limits import in_range, limit_at

    out: list[Point] = []
    for f in frequencies:
        # Outside the scan there is no limit and no margin. Skipped here, not caught: asking
        # limit_at raised an uncaught LimitError for a 20 MHz point against a 30 MHz scan.
        if not in_range(standard_id, f):
            continue
        by_driver: dict[str, float] = {}
        present: list[tuple[Path, float]] = []
        uncovered: list[str] = []
        for p in paths:
            e = p.at(f)
            if e is None:
                uncovered.append(p.label)
                continue
            if e <= 0:
                continue
            present.append((p, e))
            by_driver[p.driver_id] = by_driver.get(p.driver_id, 0.0) + e

        total = math.sqrt(sum(v * v for v in by_driver.values()))
        contributions = [
            Contribution(label=p.label, kind=p.kind, driver_id=p.driver_id,
                         field_v_per_m=e,
                         share=(e * e) / (total * total) if total > 0 else 0.0)
            for p, e in present
        ]
        contributions.sort(key=lambda c: -c.share)
        # Shares are exact only when no driver contributed through more than one path: an
        # amplitude sum is not a power sum, so the parts no longer add to the whole.
        indicative = any(
            sum(1 for p, _ in present if p.driver_id == d) > 1 for d in by_driver
        )
        out.append(Point(
            frequency_hz=f, field_v_per_m=total,
            limit_dbuv_per_m=limit_at(standard_id, f),
            contributions=contributions, shares_indicative=indicative, uncovered=uncovered,
        ))
    return out


# ---- the uncertainty budget (§17.2) ---------------------------------------------------

#: σ for the cable idealisation, radiated cable paths. Deliberately conservative: M0's
#: synthetic-board residual was 1.2 dB median and 2.2 dB worst, and cable test 4 has not yet
#: run on a real board, so this stays where it is until real residuals replace it.
SIGMA_CABLE_DB = 4.5

#: σ by mesh preset, radiated board path. A coarser mesh moves a resonance, and M0 measured how
#: far: λ/20 put a dipole 6 % low where λ/90 was 0.4 %.
SIGMA_MESH_DB = {"fine": 1.0, "normal": 2.0, "coarse": 4.0}

#: σ when the board's permittivity was assumed rather than given by a stackup.
SIGMA_ASSUMED_EPSILON_DB = 1.5

#: σ for interpolating the far-field transfer function in dB between grid points.
SIGMA_FAR_FIELD_INTERP_DB = 1.0

#: σ for unmodelled components, scaled by the fraction of capacitors on the path that matched
#: nothing in the library. All matched is zero, none matched is the full term.
SIGMA_COMPONENT_COVERAGE_DB = 3.0


def combine_sigma(terms: dict) -> float:
    """Root-sum-square, over the terms that apply. Independent errors, which they are not
    exactly — but treating them as correlated would inflate σ beyond anything the recorded
    results are likely to support, and §17.4 replaces the guess with measurement."""
    return math.sqrt(sum(float(v) ** 2 for v in terms.values()))


def sigma_at(point: Point, paths: list[Path], shared_terms: dict) -> tuple[float, dict]:
    """σ at one frequency, weighting each path's terms by its power share (§17.1).

    A budget that simply took the worst path's terms would report a cable's 4.5 dB for a
    frequency where the cable contributes 2 % of the power. Weighting by share means the number
    describes the prediction actually being made.
    """
    by_label = {p.label: p for p in paths}
    weighted: dict[str, float] = dict(shared_terms)
    for c in point.contributions:
        path = by_label.get(c.label)
        if path is None:
            continue
        for name, value in path.sigma_terms.items():
            # In quadrature, weighted by power share: a path contributing a tenth of the power
            # contributes a tenth of that term's variance.
            key = f"{name} ({c.label})"
            weighted[key] = math.sqrt(weighted.get(key, 0.0) ** 2 + c.share * float(value) ** 2)
    return combine_sigma(weighted), weighted


def phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass
class Outlook:
    """What the compliance view shows, when it is allowed to show anything."""

    standard_id: str
    points: list[Point]
    worst: Point | None
    sigma_db: float
    sigma_terms: dict
    #: Every frequency within one σ of the limit — §16.4's near misses. The confidence figure is
    #: evaluated at the worst frequency alone, so it is optimistic when several are close, and
    #: this list is what says so.
    near_misses: list[Point] = field(default_factory=list)

    @property
    def margin_db(self) -> float | None:
        return self.worst.margin_db if self.worst else None

    @property
    def confidence_uncalibrated(self) -> float | None:
        """P(true margin > 0) under the budget. Not a pass probability, and uncalibrated.

        Named that way everywhere, including here, so no caller can pass it on as a pass
        probability without writing the word. It used to travel as ``confidence`` too."""
        if self.worst is None or self.sigma_db <= 0:
            return None
        return phi(self.worst.margin_db / self.sigma_db)

    @property
    def range_80_db(self) -> tuple[float, float] | None:
        if self.worst is None:
            return None
        return (self.worst.margin_db - 1.28 * self.sigma_db,
                self.worst.margin_db + 1.28 * self.sigma_db)


def outlook(paths: list[Path], frequencies: list[float] | None, standard_id: str,
            shared_terms: dict | None = None) -> Outlook:
    """``frequencies`` of ``None`` means the paths' common grid (``common_grid``)."""
    if frequencies is None:
        frequencies = common_grid(paths, standard_id)
    points = combine(paths, frequencies, standard_id)
    scored = [p for p in points if p.field_v_per_m > 0]
    worst = min(scored, key=lambda p: p.margin_db) if scored else None
    if worst is None:
        return Outlook(standard_id=standard_id, points=points, worst=None,
                       sigma_db=0.0, sigma_terms={})
    sigma, terms = sigma_at(worst, paths, shared_terms or {})
    near = [p for p in scored
            if p is not worst and p.margin_db <= worst.margin_db + sigma]
    near.sort(key=lambda p: p.margin_db)
    return Outlook(standard_id=standard_id, points=points, worst=worst,
                   sigma_db=sigma, sigma_terms=terms, near_misses=near)
