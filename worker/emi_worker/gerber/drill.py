"""Excellon drill parsing.

Holes matter for two separate reasons, and both are easy to underrate:

* A plated hole is a conductor joining layers. Without it, an inner plane has no electrical
  path to anything and net reconstruction cannot name it.
* A via is a vertical current path in the field solve, and the return-via rule is entirely
  about where they are.

Excellon is a loose format — the header declares units and tools, the body plots them — and
generators vary in what they emit. Anything not understood is skipped and counted rather
than guessed at.

A board usually arrives as more than one drill file. KiCad writes plated and non-plated
holes separately by default (``-PTH.drl`` and ``-NPTH.drl``), and keeping only one of them
dropped every via whenever the NPTH file happened to be read last. So the parser returns
holes with their plating, and the caller merges every file it was given.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..kicad.board import Via

log = logging.getLogger(__name__)

_TOOL_DEF = re.compile(r"^T(?P<tool>\d+)(?:[CF](?P<dia>[\d.]+))", re.IGNORECASE)
_TOOL_SEL = re.compile(r"^T(?P<tool>\d+)\s*$")
_COORD = re.compile(r"^X(?P<x>[+-]?[\d.]+)Y(?P<y>[+-]?[\d.]+)", re.IGNORECASE)
_HEADER_UNITS = re.compile(r"^(METRIC|INCH)", re.IGNORECASE)

#: Gerber X2 attributes carried in Excellon comments, as KiCad and most modern tools write
#: them: ``; #@! TF.FileFunction,NonPlated,1,2,NPTH`` for the file, and
#: ``; #@! TA.AperFunction,Plated,PTH,ViaDrill`` just before each tool it describes. A mixed
#: file relies on the per-tool attribute alone to say which holes are unplated.
_FILE_FUNCTION = re.compile(r"TF\.FileFunction,(?P<plating>NonPlated|Plated|MixedPlating)",
                            re.IGNORECASE)
_APER_FUNCTION = re.compile(r"TA\.AperFunction,(?P<plating>NonPlated|Plated)(?:,[^,]*)?"
                            r"(?:,(?P<use>\w+))?", re.IGNORECASE)


@dataclass
class Hole:
    """One drilled hole, in mm, in the model's Y-down convention."""

    x: float
    y: float
    drill_mm: float
    plated: bool
    #: "via", "component" or "" when the file does not say.
    function: str = ""


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


def parse_holes(text: str) -> tuple[list[Hole], list[str]]:
    """Parse an Excellon file into holes, plated and unplated alike."""
    warnings: list[str] = []
    #: tool -> (diameter mm, plated per its own attribute or None, function)
    tools: dict[str, tuple[float, bool | None, str]] = {}
    metric = True
    decimals = 3
    current: str | None = None
    #: The file-level plating, overridden per tool by an AperFunction attribute.
    plated = True
    #: An AperFunction attribute applies to the next tool definition only.
    pending: tuple[bool, str] | None = None
    holes: list[Hole] = []
    skipped = 0
    in_header = True

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            m = _FILE_FUNCTION.search(line)
            if m:
                plated = m.group("plating").lower() != "nonplated"
                continue
            m = _APER_FUNCTION.search(line)
            if m:
                use = (m.group("use") or "").lower()
                pending = (
                    m.group("plating").lower() == "plated",
                    "via" if use.startswith("via") else "component" if use.startswith("comp") else "",
                )
                continue
            # Older mixed files mark the switch with a comment rather than an attribute.
            if "TYPE=NON_PLATED" in line.upper():
                plated = False
            elif "TYPE=PLATED" in line.upper():
                plated = True
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
            tool_plated, function = pending if pending is not None else (None, "")
            tools[m.group("tool")] = (dia if metric else dia * 25.4, tool_plated, function)
            pending = None
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
            drill, tool_plated, function = tools[current]
            holes.append(Hole(
                x=_decode(m.group("x"), metric, decimals),
                # Excellon Y is up, like Gerber; the model uses KiCad's Y-down convention.
                y=-_decode(m.group("y"), metric, decimals),
                drill_mm=drill,
                # A tool's own attribute wins; otherwise whatever the file last declared.
                plated=plated if tool_plated is None else tool_plated,
                function=function,
            ))

    if skipped:
        warnings.append(f"{skipped} drill holes had no tool size and were skipped.")
    log.info("drill: %d holes (%d plated), %d tools",
             len(holes), sum(1 for h in holes if h.plated), len(tools))
    return holes, warnings


def holes_to_vias(holes: list[Hole], layers: list[str]) -> list[Via]:
    """The plated holes, as vias.

    Every hole is treated as spanning the whole stack. Excellon has no concept of blind or
    buried holes on its own — those live in separate files whose layer span is named in the
    job file — so a drill file means through-holes, and assuming otherwise would invent
    layer transitions that are not there.

    An unplated hole is a mounting hole: it conducts nothing and joins nothing, so
    including it would invent connections between layers.
    """
    return [
        Via(
            x=h.x, y=h.y,
            # A drill file gives the hole, not the annular ring. The pad around it comes
            # from the Gerbers, so the via's own copper is just the barrel.
            size_mm=h.drill_mm,
            drill_mm=h.drill_mm,
            layers=list(layers),
            net="",
            kind="through",
        )
        for h in holes if h.plated
    ]


def parse_drill(text: str, layers: list[str]) -> tuple[list[Via], list[str]]:
    """Parse one Excellon file into vias (its plated holes)."""
    holes, warnings = parse_holes(text)
    vias = holes_to_vias(holes, layers)
    if not vias:
        warnings.append("The drill file has no plated holes, so the board has no vias.")
    return vias, warnings
