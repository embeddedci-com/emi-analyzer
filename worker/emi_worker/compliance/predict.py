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

    def at(self, frequency_hz: float) -> float:
        for p in self.points:
            if p.frequency_hz == frequency_hz:
                return p.field_v_per_m
        return 0.0


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
    from emi_worker.compliance.limits import limit_at

    out: list[Point] = []
    for f in frequencies:
        by_driver: dict[str, float] = {}
        present: list[tuple[Path, float]] = []
        for p in paths:
            e = p.at(f)
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
            contributions=contributions, shares_indicative=indicative,
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
    def confidence(self) -> float | None:
        """P(true margin > 0) under the budget. Not a pass probability, and uncalibrated."""
        if self.worst is None or self.sigma_db <= 0:
            return None
        return phi(self.worst.margin_db / self.sigma_db)

    @property
    def range_80_db(self) -> tuple[float, float] | None:
        if self.worst is None:
            return None
        return (self.worst.margin_db - 1.28 * self.sigma_db,
                self.worst.margin_db + 1.28 * self.sigma_db)


def outlook(paths: list[Path], frequencies: list[float], standard_id: str,
            shared_terms: dict | None = None) -> Outlook:
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
