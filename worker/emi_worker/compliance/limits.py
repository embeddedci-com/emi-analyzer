"""Limit tables and how to read them.

The tables themselves are data (``limits/fcc.json``), read by this module and imported
directly by the webapp so there is one copy of every number and it cannot drift. §15.2's
rule is that no limit is ever typed from memory: every value carries the clause it comes
from and whether it was taken from the standard's own text or from cross-checked public
secondary sources.

Two subtleties are the whole reason this is code rather than a dictionary lookup.

**Band edges take the tighter limit.** At 88 MHz exactly, FCC Class B is 100 µV/m and not
150: the frequency belongs to both segments and the stricter one governs. Picking a segment
with a half-open interval would quietly pass a device that fails.

**Conducted segments slope.** §15.107's 0.15–0.5 MHz segment falls from 66 to 56 dBµV, and
it falls linearly with the *logarithm* of frequency — so the limit at the geometric mean of
the endpoints is their arithmetic mean, 61 dBµV at 0.274 MHz.
"""

from __future__ import annotations

import functools
import json
import math
from dataclasses import dataclass
from pathlib import Path

LIMITS_DIR = Path(__file__).resolve().parent / "limits"


class LimitError(ValueError):
    """A limit that cannot be read, or a frequency no segment covers."""


@dataclass(frozen=True)
class Segment:
    f_lo_hz: float
    f_hi_hz: float
    level_db: float
    #: Level at ``f_hi_hz`` when the segment slopes; ``None`` for a flat one.
    level_db_hi: float | None = None
    #: The detector this segment's level is read with, when it differs from the standard's.
    #: Above 1 GHz §15.35(b) switches radiated limits to an average detector.
    detector: str | None = None
    #: A second, peak-detector limit that applies alongside ``level_db`` (§15.35(b): 20 dB
    #: above the average limit). ``None`` where the standard sets none.
    peak_level_db: float | None = None

    def covers(self, frequency_hz: float) -> bool:
        """Inclusive at both ends, so a band edge belongs to both of its segments."""
        return self.f_lo_hz <= frequency_hz <= self.f_hi_hz

    def level_at(self, frequency_hz: float) -> float:
        if self.level_db_hi is None:
            return self.level_db
        if frequency_hz <= self.f_lo_hz:
            return self.level_db
        if frequency_hz >= self.f_hi_hz:
            return self.level_db_hi
        span = math.log10(self.f_hi_hz / self.f_lo_hz)
        along = math.log10(frequency_hz / self.f_lo_hz) / span
        return self.level_db + along * (self.level_db_hi - self.level_db)


@dataclass(frozen=True)
class Standard:
    id: str
    name: str
    clause: str
    scan: str
    unit: str
    detector: str
    verified: str
    source: str
    segments: tuple[Segment, ...]
    device_class: str | None = None
    distance_m: float | None = None

    def range_hz(self) -> tuple[float, float]:
        return self.segments[0].f_lo_hz, self.segments[-1].f_hi_hz


def _parse(doc: dict) -> dict[str, Standard]:
    if doc.get("format") != "emi-limits":
        raise LimitError(f"not a limits document: format={doc.get('format')!r}")
    if doc.get("version") != 1:
        raise LimitError(f"unsupported limits version {doc.get('version')!r}")
    out: dict[str, Standard] = {}
    for raw in doc["standards"]:
        segments = tuple(
            Segment(
                f_lo_hz=float(s["f_lo_hz"]),
                f_hi_hz=float(s["f_hi_hz"]),
                level_db=float(s["level_db"]),
                level_db_hi=None if s.get("level_db_hi") is None else float(s["level_db_hi"]),
                detector=s.get("detector"),
                peak_level_db=(None if s.get("peak_level_db") is None
                               else float(s["peak_level_db"])),
            )
            for s in raw["segments"]
        )
        if not segments:
            raise LimitError(f"{raw['id']}: a standard needs at least one segment")
        for a, b in zip(segments, segments[1:]):
            if b.f_lo_hz < a.f_hi_hz:
                raise LimitError(
                    f"{raw['id']}: segments overlap at {b.f_lo_hz:g} Hz. Touching endpoints "
                    f"are fine and are how band edges work, but a real overlap means one of "
                    f"the two levels is wrong"
                )
            if b.f_lo_hz > a.f_hi_hz:
                raise LimitError(
                    f"{raw['id']}: a gap between {a.f_hi_hz:g} and {b.f_lo_hz:g} Hz would "
                    f"silently report no limit where the standard has one"
                )
        out[raw["id"]] = Standard(
            id=raw["id"], name=raw["name"], clause=raw["clause"], scan=raw["scan"],
            unit=raw["unit"], detector=raw["detector"], verified=raw["verified"],
            source=raw["source"], segments=segments,
            device_class=raw.get("class"), distance_m=raw.get("distance_m"),
        )
    return out


@functools.lru_cache(maxsize=1)
def load_standards() -> dict[str, Standard]:
    """Every standard in every table file, by id."""
    out: dict[str, Standard] = {}
    for path in sorted(LIMITS_DIR.glob("*.json")):
        out.update(_parse(json.loads(path.read_text())))
    if not out:
        raise LimitError(f"no limit tables found in {LIMITS_DIR}")
    return out


def standard(standard_id: str) -> Standard:
    try:
        return load_standards()[standard_id]
    except KeyError:
        known = ", ".join(sorted(load_standards()))
        raise LimitError(f"unknown standard {standard_id!r}; known: {known}") from None


def limit_at(standard_id: str, frequency_hz: float) -> float:
    """The limit at one frequency, in the standard's own unit.

    At a band edge the tighter of the two segments wins, which is why this takes a minimum
    over every segment covering the frequency rather than finding one.
    """
    std = standard(standard_id)
    levels = [s.level_at(frequency_hz) for s in std.segments if s.covers(frequency_hz)]
    if not levels:
        lo, hi = std.range_hz()
        raise LimitError(
            f"{std.id} covers {lo / 1e6:g}-{hi / 1e6:g} MHz and says nothing at "
            f"{frequency_hz / 1e6:g} MHz. Reporting a margin here would be inventing one"
        )
    return min(levels)


def in_range(standard_id: str, frequency_hz: float) -> bool:
    """True when the standard has a limit at this frequency at all.

    The compliance run asks this before ``limit_at`` rather than catching its error: a
    frequency outside the scan is not part of the estimate, and deciding that by exception is
    how one slips through as a crash instead of a gap.
    """
    lo, hi = standard(standard_id).range_hz()
    return lo <= frequency_hz <= hi


def detector_at(standard_id: str, frequency_hz: float) -> str:
    """Which detector the governing limit at this frequency is read with.

    At a band edge this is the detector of the segment ``limit_at`` picked, so the two agree.
    """
    std = standard(standard_id)
    covering = [s for s in std.segments if s.covers(frequency_hz)]
    if not covering:
        raise LimitError(f"{std.id} has no limit at {frequency_hz / 1e6:g} MHz")
    seg = min(covering, key=lambda s: s.level_at(frequency_hz))
    return seg.detector or std.detector


def peak_limit_at(standard_id: str, frequency_hz: float) -> float | None:
    """The peak-detector limit that applies alongside the governing one, if any.

    For a steady harmonic the peak, quasi-peak and average detectors all read the same level
    (an analyser is calibrated to the RMS of a sine), so the governing limit is the tighter
    average one and this never decides a margin. It is here so the limit line can draw it and
    so a pulsed source, when one is modelled, has the number it needs.
    """
    std = standard(standard_id)
    peaks = [s.peak_level_db for s in std.segments
             if s.covers(frequency_hz) and s.peak_level_db is not None]
    return min(peaks) if peaks else None


@functools.lru_cache(maxsize=1)
def _scan_rules() -> dict:
    for path in sorted(LIMITS_DIR.glob("*.json")):
        doc = json.loads(path.read_text())
        if "scan_range" in doc:
            return doc["scan_range"]
    raise LimitError("no scan-range rules found")


def scan_to_hz(highest_frequency_hz: float) -> float:
    """How far up a radiated scan must go, per §15.33(b).

    Above 1 GHz the rule stops being a table and becomes the fifth harmonic of the highest
    frequency, or 40 GHz, whichever is lower.
    """
    if highest_frequency_hz <= 0:
        raise LimitError("the highest frequency in use must be positive")
    rules = _scan_rules()
    for rule in rules["rules"]:
        if highest_frequency_hz < rule["highest_below_hz"]:
            return float(rule["scan_to_hz"])
    above = rules["above"]
    return float(min(highest_frequency_hz * above["harmonic"], above["cap_hz"]))
