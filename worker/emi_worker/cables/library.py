"""The cable library (§5).

A cable document describes the cable, not the board: how long it is, whether it is shielded,
how that shield is bonded, and what the far end looks like. The far end is the parameter most
people expect least and it matters most — open or grounded moves a 1 m cable's first resonance
by about a factor of two.

**A ground strap is a bond, not a cable.** It ties the board to the ground plane at the
connector, and that turns the cable from a dipole against the board into a line over the plane.
Which way the first resonance moves depends on the far end: down for an open one (a half wave
becomes a quarter wave), up for a grounded one (both ends on the plane). ``nec.Deck.bond_nh``
models it as the strap it is.
"""

from __future__ import annotations

import functools
import json
import math
from dataclasses import dataclass
from pathlib import Path

LIBRARY_PATH = Path(__file__).resolve().parent / "library.json"

FORMAT = "emi-cable"
VERSION = 1

SHIELDS = ("none", "foil", "braid", "foil+braid")
BONDS = ("360", "pigtail", "none")
FAR_ENDS = ("open", "equipment", "ground")
ROUTINGS = ("horizontal", "horizontal-then-drop", "vertical")


#: The longest cable modelled, in metres. It is what nec.MAX_SEGMENTS is sized for.
MAX_LENGTH_M = 10.0


class CableError(ValueError):
    """A cable document that cannot be read."""


@dataclass(frozen=True)
class Choke:
    """A common-mode choke at the board end of the cable, as a series impedance.

    **From a datasheet curve**, ``impedance``: (frequency, R, X) points, the common-mode
    columns of a ferrite's impedance table. A ferrite is inductive below its peak, resistive
    at it and capacitive above, so X changes sign across the band and the curve is the only
    honest description. R and X are interpolated linearly in log frequency between points and
    held at the end values outside them, which the budget states.

    **From one number**, ``z_ohm_at_100mhz`` alone: a simplification, not a model. The choke is
    taken as a pure resistance of that value at 100 MHz, scaled linearly with frequency. That
    is roughly right for the rising, inductive side of a ferrite and wrong above its peak,
    where a real part falls and this keeps climbing (10x the 100 MHz figure at 1 GHz). A
    curve replaces it whenever one is given.
    """

    z_ohm_at_100mhz: float | None = None
    #: (f_hz, r_ohm, x_ohm), sorted by frequency.
    impedance: tuple[tuple[float, float, float], ...] = ()

    @property
    def from_curve(self) -> bool:
        return bool(self.impedance)

    def z_at(self, frequency_hz: float) -> complex:
        """The choke's series impedance at one frequency, ohms."""
        if frequency_hz <= 0:
            raise ValueError("frequency must be positive")
        pts = self.impedance
        if not pts:
            return complex((self.z_ohm_at_100mhz or 0.0) * frequency_hz / 100e6, 0.0)
        if frequency_hz <= pts[0][0]:
            return complex(pts[0][1], pts[0][2])
        if frequency_hz >= pts[-1][0]:
            return complex(pts[-1][1], pts[-1][2])
        for (f0, r0, x0), (f1, r1, x1) in zip(pts, pts[1:]):
            if f0 <= frequency_hz <= f1:
                t = math.log(frequency_hz / f0) / math.log(f1 / f0)
                return complex(r0 + t * (r1 - r0), x0 + t * (x1 - x0))
        raise AssertionError("unreachable: the curve is sorted and brackets the frequency")


@dataclass(frozen=True)
class Cable:
    id: str
    name: str
    length_m: float
    length_options_m: tuple[float, ...]
    shield: str
    shield_bond: str
    pigtail_mm: float
    far_end: str
    routing: str
    connector_hints: tuple[str, ...]
    balance_lcl_db: float | None = None
    cm_choke: Choke | None = None

    def with_length(self, length_m: float) -> "Cable":
        if not (length_m > 0):
            raise CableError("a cable length must be positive")
        if length_m > MAX_LENGTH_M:
            raise CableError(
                f"a cable longer than {MAX_LENGTH_M:g} m is not modelled. A radiated test bundles "
                f"the excess length, so a straight {length_m:g} m run is not what a lab measures")
        return Cable(**{**self.__dict__, "length_m": float(length_m)})

    def describe_shield(self) -> str:
        """What the shield actually buys, which is less than most people expect.

        Without an enclosure a shielded and an unshielded cable are the same antenna: the
        common-mode current flows on the outside of the shield either way. A shield changes
        where the current is launched and how balanced the pair is, not whether the cable
        radiates.
        """
        if self.shield == "none":
            return "unshielded"
        if self.shield_bond == "none":
            return f"{self.shield} shield, not bonded — electrically an unshielded cable"
        if self.shield_bond == "pigtail":
            return (
                f"{self.shield} shield, pigtail bonded"
                + (f" ({self.pigtail_mm:g} mm)" if self.pigtail_mm else "")
                + " — the pigtail's inductance is what limits it"
            )
        return f"{self.shield} shield, 360° bonded"


def parse(doc: dict) -> Cable:
    if not isinstance(doc, dict):
        raise CableError("a cable document must be a JSON object")
    if doc.get("format") != FORMAT:
        raise CableError(f"not a cable document: format={doc.get('format')!r}")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CableError(f"version must be a whole number from 1, got {version!r}")
    if version > VERSION:
        raise CableError(
            f"this cable is version {version} and this build understands version {VERSION}")

    for required in ("id", "name"):
        if not isinstance(doc.get(required), str) or not doc[required].strip():
            raise CableError(f"a cable needs a {required}")

    length = doc.get("length_m")
    if not isinstance(length, (int, float)) or isinstance(length, bool) or length <= 0:
        raise CableError(f"{doc['id']}: length_m must be a positive number")

    for field, allowed in (("shield", SHIELDS), ("shield_bond", BONDS),
                           ("far_end", FAR_ENDS), ("routing", ROUTINGS)):
        value = doc.get(field)
        if value not in allowed:
            raise CableError(
                f"{doc['id']}: {field} is {value!r}; use one of {', '.join(allowed)}")

    # A shield that is not bonded is not a shield, and saying "braid, none" is almost always
    # a mistake in the description rather than a deliberate choice.
    if doc["shield"] == "none" and doc["shield_bond"] != "none":
        raise CableError(
            f"{doc['id']}: an unshielded cable cannot have a {doc['shield_bond']} bond")

    choke_raw = doc.get("cm_choke")
    choke = None
    if choke_raw is not None:
        choke = _parse_choke(doc["id"], choke_raw)

    lcl = doc.get("balance_lcl_db")
    if lcl is not None and (not isinstance(lcl, (int, float)) or isinstance(lcl, bool)):
        raise CableError(f"{doc['id']}: balance_lcl_db must be a number or null")

    options = doc.get("length_options_m") or [length]
    if not all(isinstance(v, (int, float)) and v > 0 for v in options):
        raise CableError(f"{doc['id']}: every length option must be a positive number")

    return Cable(
        id=doc["id"].strip(), name=doc["name"].strip(), length_m=float(length),
        length_options_m=tuple(sorted(float(v) for v in options)),
        shield=doc["shield"], shield_bond=doc["shield_bond"],
        pigtail_mm=float(doc.get("pigtail_mm") or 0.0),
        far_end=doc["far_end"], routing=doc["routing"],
        connector_hints=tuple(str(h).lower() for h in doc.get("connector_hints") or ()),
        balance_lcl_db=None if lcl is None else float(lcl),
        cm_choke=choke,
    )


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _parse_choke(cable_id: str, raw) -> Choke:
    """``{"impedance": [{"f_hz", "r_ohm", "x_ohm"}, ...]}`` or ``{"z_ohm_at_100mhz"}``."""
    if not isinstance(raw, dict):
        raise CableError(f"{cable_id}: cm_choke must be an object")
    curve = raw.get("impedance")
    if curve is not None:
        if not isinstance(curve, list) or len(curve) < 2:
            raise CableError(
                f"{cable_id}: cm_choke.impedance needs at least two points from the datasheet")
        pts = []
        for p in curve:
            if not isinstance(p, dict) or not all(_is_number(p.get(k))
                                                  for k in ("f_hz", "r_ohm", "x_ohm")):
                raise CableError(
                    f"{cable_id}: every cm_choke.impedance point needs f_hz, r_ohm and x_ohm")
            if p["f_hz"] <= 0 or p["r_ohm"] < 0:
                raise CableError(
                    f"{cable_id}: a choke point needs a positive frequency and a resistance of "
                    f"zero or more; a negative resistance would add energy")
            pts.append((float(p["f_hz"]), float(p["r_ohm"]), float(p["x_ohm"])))
        pts.sort()
        if any(b[0] == a[0] for a, b in zip(pts, pts[1:])):
            raise CableError(f"{cable_id}: cm_choke.impedance repeats a frequency")
        return Choke(impedance=tuple(pts))
    z = raw.get("z_ohm_at_100mhz")
    if not _is_number(z) or z <= 0:
        raise CableError(
            f"{cable_id}: cm_choke needs an impedance curve or a positive z_ohm_at_100mhz")
    return Choke(z_ohm_at_100mhz=float(z))


@functools.lru_cache(maxsize=1)
def built_in() -> dict[str, Cable]:
    doc = json.loads(LIBRARY_PATH.read_text())
    if doc.get("format") != "emi-cable-library":
        raise CableError("library.json is not a cable library")
    out: dict[str, Cable] = {}
    for raw in doc.get("cables", []):
        cable = parse(raw)
        if cable.id in out:
            raise CableError(f"duplicate cable id {cable.id!r}")
        out[cable.id] = cable
    if not out:
        raise CableError("the cable library is empty")
    return out


def get(cable_id: str) -> Cable:
    try:
        return built_in()[cable_id]
    except KeyError:
        known = ", ".join(sorted(built_in()))
        raise CableError(f"unknown cable {cable_id!r}; known: {known}") from None
