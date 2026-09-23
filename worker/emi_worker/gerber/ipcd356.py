"""IPC-D-356 netlist parsing.

Gerbers carry no net information whatsoever — they are a set of apertures and paths, and
nothing in them says which copper belongs to which signal. The netlist is what makes the
Gerber path usable: without it there is no net picker, no way to tell a signal from its
reference, and no way to place a port by name. A Gerber upload without one is refused.

The format is fixed-column, but generators disagree about the exact columns past the net
name, so the head is read positionally and the tail by pattern. That is deliberately more
forgiving than the spec: a netlist that half-parses silently would put net names on the
wrong copper, which is worse than not parsing at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Record types that carry a connection point. 317 is through-hole or via, 327 is surface.
CONNECTION_RECORDS = ("317", "327")


class NetlistError(ValueError):
    """The netlist could not be read. The message is shown to the user."""


@dataclass
class NetPoint:
    """One connection point: a pad or via, with the net it belongs to."""

    net: str
    x_mm: float
    y_mm: float
    #: Reference designator, e.g. "U3", or "VIA".
    ref: str = ""
    pin: str = ""
    #: Access code: 0 means all layers (a through-hole), 1 is the top layer, and so on.
    access: int = 0
    drill_mm: float = 0.0
    plated: bool = True

    @property
    def is_through(self) -> bool:
        return self.access == 0

    @property
    def is_via(self) -> bool:
        return self.ref.upper() == "VIA" or not self.ref


@dataclass
class Netlist:
    points: list[NetPoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def nets(self) -> list[str]:
        seen: dict[str, None] = {}
        for p in self.points:
            seen.setdefault(p.net, None)
        return list(seen)

    def by_net(self) -> dict[str, list[NetPoint]]:
        out: dict[str, list[NetPoint]] = {}
        for p in self.points:
            out.setdefault(p.net, []).append(p)
        return out


#: Units are declared by a parameter line, e.g. "P  UNITS CUST 0".
#:
#:   0 -> inches, 4 decimal places        1 -> millimetres, 3 decimal places
#:   2 -> inches, 5 decimal places        3 -> millimetres, 4 decimal places
#:
#: Getting this wrong scales the whole board by 25.4, which is obvious. Getting the number
#: of decimal places wrong scales it by 10, which is not.
_UNIT_SCALE_MM = {
    0: 25.4 / 10_000,
    1: 1 / 1_000,
    2: 25.4 / 100_000,
    3: 1 / 10_000,
}

_UNITS_RE = re.compile(r"^P\s+UNITS\s+CUST\s+(\d)", re.IGNORECASE)

#: The tail of a connection record: optional drill, plating, access code, then coordinates.
_TAIL_RE = re.compile(
    r"(?:D(?P<drill>\d+)(?P<plating>[PU]))?"
    r"A(?P<access>\d{2})"
    r"X(?P<x>[+-]\d+)"
    r"Y(?P<y>[+-]\d+)"
)


def parse(text: str) -> Netlist:
    """Parse an IPC-D-356 (or IPC-D-356A) netlist."""
    scale: float | None = None
    result = Netlist()
    skipped = 0

    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line:
            continue

        if line[0] in "PC":
            m = _UNITS_RE.match(line)
            if m:
                code = int(m.group(1))
                scale = _UNIT_SCALE_MM.get(code)
                if scale is None:
                    raise NetlistError(
                        f"the netlist declares unit code {code}, which is not one this "
                        f"tool understands (expected 0-3)"
                    )
            continue

        if line[:3] not in CONNECTION_RECORDS:
            continue

        if scale is None:
            # The spec requires the units line before any data. Guessing would silently
            # scale the whole board, so refuse instead.
            raise NetlistError(
                "the netlist has connection records before it declares its units "
                "(a 'P UNITS CUST n' line). Re-export it from your CAD tool."
            )

        m = _TAIL_RE.search(line)
        if not m:
            skipped += 1
            continue

        net = line[3:17].strip()
        if not net:
            skipped += 1
            continue

        # Fixed columns, per the spec: reference designator at 21-26, then a separator,
        # then the pin at 28-31. Slicing a wider window and splitting on "-" picks up the
        # midpoint marker and the drill field too, which quietly breaks via detection.
        ref = line[20:26].strip() if len(line) > 20 else ""
        pin = line[27:31].strip() if len(line) > 27 else ""

        drill_raw = m.group("drill")
        result.points.append(NetPoint(
            net=net,
            x_mm=int(m.group("x")) * scale,
            y_mm=int(m.group("y")) * scale,
            ref=ref,
            pin=pin,
            access=int(m.group("access")),
            drill_mm=(int(drill_raw) * scale) if drill_raw else 0.0,
            plated=(m.group("plating") or "P") == "P",
        ))

    if not result.points:
        raise NetlistError(
            "the netlist contains no connection points. It may be empty, or it may not be "
            "an IPC-D-356 file."
        )

    # The net name field is 14 characters. Longer names are truncated by the exporter
    # (KiCad keeps the tail and uppercases), so "Net-(U5-DNC-Pad6)" arrives as
    # "-(U5-DNC-PAD6)". Nothing can recover the original from the file, but a user
    # comparing against their schematic deserves to know why the names look mangled.
    truncated = sum(1 for p in result.points if len(p.net) >= 14)
    if truncated:
        result.warnings.append(
            f"{truncated} net names hit the 14-character limit of IPC-D-356 and were cut "
            f"short, so they may not match your schematic."
        )

    if skipped:
        result.warnings.append(
            f"{skipped} netlist records could not be read and were ignored "
            f"({len(result.points)} were read)."
        )

    log.info(
        "netlist: %d points across %d nets", len(result.points), len(result.nets),
    )
    return result


def looks_like_netlist(text: str) -> bool:
    """Cheap sniff, for picking the netlist out of an uploaded archive."""
    head = text[:4000]
    return "UNITS CUST" in head or any(
        line[:3] in CONNECTION_RECORDS for line in head.splitlines()
    )
