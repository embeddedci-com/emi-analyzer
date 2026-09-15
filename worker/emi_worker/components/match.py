"""Recognising a part well enough to model it (§12).

§21's rule for this milestone is matching before physics: a wrong match is worse than no
model, because bare copper is visibly incomplete while a capacitor modelled as the wrong
package is a confident number with a self-resonance in the wrong place.

Measured against four real KiCad boards: 429 two-pad capacitors, of
which 425 sit on KiCad's standard ``C_<imperial>_<metric>Metric`` footprints (0402, 0603,
0805, 1206, 1210) and 4 on custom LCSC footprints — the ``BD10`` electrolytic and one
``STC3MD20``. Those four are left unmatched, which is the designed outcome rather than a gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: KiCad's standard two-terminal SMD naming: imperial code, then the metric one, then
#: "Metric". "C_0402_1005Metric" is a 0402 imperial part. The prefix is the part class, so
#: R_, L_ and C_ all parse the same way and the caller decides what it wanted.
_STANDARD = re.compile(
    r"^(?P<cls>[A-Z]+)_(?P<imperial>\d{4})_(?P<metric>\d{4})Metric\b", re.I)

#: A footprint whose name is only a four-digit code. Ambiguous: metric 0603 is imperial 0201.
_BARE = re.compile(r"^(?:(?P<cls>[A-Z]+)_)?(?P<code>\d{4})\b", re.I)

#: Imperial code -> the metric code KiCad pairs it with. Used to tell a bare code that agrees
#: with the imperial reading from one that does not exist as an imperial part at all.
IMPERIAL_TO_METRIC = {
    "0201": "0603",
    "0402": "1005",
    "0603": "1608",
    "0805": "2012",
    "1206": "3216",
    "1210": "3225",
    "1812": "4532",
    "2220": "5750",
}


@dataclass(frozen=True)
class PackageMatch:
    """A package size read out of a footprint name."""

    imperial: str
    metric: str | None
    #: True when the name carried only one code and it had to be guessed which system it was.
    ambiguous: bool
    #: The footprint name this came from, so a finding can quote it.
    footprint: str

    def describe(self) -> str:
        if not self.ambiguous:
            return f"{self.imperial} (imperial), {self.metric} metric"
        return (
            f"{self.imperial} — read as imperial. The footprint name carries one code and "
            f"no system, and metric {self.imperial} would be imperial "
            f"{_METRIC_TO_IMPERIAL.get(self.imperial, '?')} instead"
        )


_METRIC_TO_IMPERIAL = {m: i for i, m in IMPERIAL_TO_METRIC.items()}


def parse_package(footprint: str) -> PackageMatch | None:
    """Read a package size from a KiCad footprint name, or None when it is not standard.

    Custom footprints are not guessed at. ``CAP-SMD_L4.5-W3.2_STC3MD20-T1`` carries its
    dimensions in the name and a reader could extract them, but the part behind it is
    whatever the library author meant, and inventing a package for it would produce exactly
    the confident-and-wrong model this module exists to avoid.
    """
    if not footprint:
        return None
    # "Capacitor_SMD:C_0402_1005Metric" -> the part after the library prefix.
    name = footprint.split(":", 1)[-1].strip()

    m = _STANDARD.match(name)
    if m:
        imperial = m.group("imperial")
        metric = m.group("metric")
        # Both codes present: they check each other. A name pairing codes that KiCad never
        # pairs is a hand-edited footprint, and trusting it would be a guess.
        if IMPERIAL_TO_METRIC.get(imperial) not in (None, metric):
            return None
        return PackageMatch(imperial=imperial, metric=metric, ambiguous=False,
                            footprint=footprint)

    m = _BARE.match(name)
    if m:
        code = m.group("code")
        # §12: read as imperial, and say so. Metric 0603 is imperial 0201, a four-times
        # difference in area, so the two readings are not close enough to shrug at.
        if code in IMPERIAL_TO_METRIC:
            return PackageMatch(imperial=code, metric=IMPERIAL_TO_METRIC[code],
                                ambiguous=True, footprint=footprint)
        return None
    return None


@dataclass(frozen=True)
class PartMatch:
    """What is known about one part, before any model is chosen."""

    ref: str
    value: str
    footprint: str
    farads: float | None
    package: PackageMatch | None

    @property
    def modellable(self) -> bool:
        """Enough is known to look for a model: a readable value and a known package."""
        return self.farads is not None and self.package is not None

    def why_not(self) -> str | None:
        if self.modellable:
            return None
        if self.farads is None and self.package is None:
            return (
                f"{self.ref}: neither the value {self.value!r} nor the footprint "
                f"{self.footprint!r} is one this build recognises"
            )
        if self.farads is None:
            return f"{self.ref}: {self.value!r} is not a capacitance this build can read"
        return f"{self.ref}: {self.footprint!r} is not a standard package name"


def match_part(ref: str, value: str, footprint: str) -> PartMatch:
    """Everything that can be read off the board about one part.

    Deliberately does not decide on a model. Choosing between an MPN match, the user's own
    library, a shared component and the built-in family is a separate decision with its own
    precedence (§12), and keeping it separate means this half can be tested against real
    boards without any library existing yet.
    """
    from emi_worker.rules.decoupling import cap_farads

    return PartMatch(
        ref=ref, value=value, footprint=footprint,
        farads=cap_farads(value),
        package=parse_package(footprint),
    )
