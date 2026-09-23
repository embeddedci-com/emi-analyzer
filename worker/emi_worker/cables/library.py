"""The cable library (§5).

A cable document describes the cable, not the board: how long it is, whether it is shielded,
how that shield is bonded, and what the far end looks like. The far end is the parameter most
people expect least and it matters most — open or grounded moves a 1 m cable's first resonance
by about a factor of two.

**A ground strap is a bond, not a cable.** It is an inductance to ground that shortens the
antenna, and modelling it as a radiating wire would get the sign of its effect wrong.
"""

from __future__ import annotations

import functools
import json
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
    z_ohm_at_100mhz: float


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
        z = (choke_raw or {}).get("z_ohm_at_100mhz")
        if not isinstance(z, (int, float)) or isinstance(z, bool) or z <= 0:
            raise CableError(f"{doc['id']}: cm_choke needs a positive z_ohm_at_100mhz")
        choke = Choke(z_ohm_at_100mhz=float(z))

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
