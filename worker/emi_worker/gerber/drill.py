"""Excellon drill parsing.

Holes matter for two separate reasons, and both are easy to underrate:

* A plated hole is a conductor joining layers. Without it, an inner plane has no electrical
  path to anything and net reconstruction cannot name it.
* A via is a vertical current path in the field solve, and the return-via rule is entirely
  about where they are.

Excellon is a loose format — the header declares units and tools, the body plots them — and
generators vary in what they emit. Anything not understood is skipped and counted rather
than guessed at.
"""

from __future__ import annotations

import logging
import re

from ..kicad.board import Via

log = logging.getLogger(__name__)

_TOOL_DEF = re.compile(r"^T(?P<tool>\d+)(?:[CF](?P<dia>[\d.]+))", re.IGNORECASE)
_TOOL_SEL = re.compile(r"^T(?P<tool>\d+)\s*$")
_COORD = re.compile(r"^X(?P<x>[+-]?[\d.]+)Y(?P<y>[+-]?[\d.]+)", re.IGNORECASE)
_HEADER_UNITS = re.compile(r"^(METRIC|INCH)", re.IGNORECASE)


def _decode(value: str, metric: bool, decimals: int) -> float:
    """Excellon coordinates may carry an explicit point or be implicitly scaled."""
    if "." in value:
        v = float(value)
    else:
        neg = value.startswith("-")
        digits = value.lstrip("+-")
        v = int(digits) / (10 ** decimals)
        if neg:
            v = -v
    return v if metric else v * 25.4


def parse_drill(text: str, layers: list[str]) -> tuple[list[Via], list[str]]:
    """Parse an Excellon file into vias.

    Every hole is treated as spanning the whole stack. Excellon has no concept of blind or
    buried holes on its own — those live in separate files whose layer span is named in the
    job file — so a single drill file means through-holes, and assuming otherwise would
    invent layer transitions that are not there.
    """
    warnings: list[str] = []
    tools: dict[str, float] = {}
    metric = True
    decimals = 3
    current: str | None = None
    plated = True
    vias: list[Via] = []
    skipped = 0
    in_header = True

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue

        if line == "M48":
            in_header = True
            continue
        if line in ("%", "M95"):
            in_header = False
            continue
        if line in ("M30", "M00"):
            break

        m = _HEADER_UNITS.match(line)
        if m:
            metric = m.group(1).upper() == "METRIC"
            # KiCad writes e.g. "METRIC,TZ" or "INCH,TZ"; some tools add a format hint.
            fmt = re.search(r"0*\.(0+)", line)
            if fmt:
                decimals = len(fmt.group(1))
            else:
                decimals = 3 if metric else 4
            continue

        if "TYPE=PLATED" in line.upper():
            plated = True
            continue
        if "TYPE=NON_PLATED" in line.upper():
            plated = False
            continue

        m = _TOOL_DEF.match(line)
        if m and m.group("dia"):
            dia = float(m.group("dia"))
            tools[m.group("tool")] = dia if metric else dia * 25.4
            continue

        m = _TOOL_SEL.match(line)
        if m:
            current = m.group("tool")
            continue

        if in_header:
            continue

        m = _COORD.match(line)
        if m:
            if current is None or current not in tools:
                skipped += 1
                continue
            if not plated:
                # An unplated hole is a mounting hole: it conducts nothing and joins
                # nothing, so including it would invent connections between layers.
                continue
            drill = tools[current]
            vias.append(Via(
                x=_decode(m.group("x"), metric, decimals),
                # Excellon Y is up, like Gerber; the model uses KiCad's Y-down convention.
                y=-_decode(m.group("y"), metric, decimals),
                # A drill file gives the hole, not the annular ring. The pad around it comes
                # from the Gerbers, so the via's own copper is just the barrel.
                size_mm=drill,
                drill_mm=drill,
                layers=list(layers),
                net="",
                kind="through",
            ))

    if skipped:
        warnings.append(f"{skipped} drill coordinates had no active tool and were skipped")
    if not vias:
        warnings.append("the drill file contained no plated holes")

    log.info("drill: %d plated holes, %d tools", len(vias), len(tools))
    return vias, warnings
