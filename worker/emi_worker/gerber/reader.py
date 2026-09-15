"""Read a Gerber set into the same BoardModel the KiCad path produces.

Everything downstream — the viewer, the rules tier, the mesher — reads only BoardModel, so
this module is the whole of the Gerber support. Nothing else changes.

The hard part is that Gerbers carry no net information. Copper is reconstructed into
connected islands by rasterising each layer, and the IPC-D-356 netlist supplies the names:
each netlist point falls inside one island, and that island takes its net. Through-holes
then tie islands together across layers, which is how an inner plane gets a name despite
having no netlist point of its own.

Coordinates are converted to KiCad's convention (Y down) on the way in, so that the single
Y-flip in normalize.py stays the only place the axis is touched.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

from ..kicad import geometry as g
from ..kicad.board import (
    BoardModel, CopperLayer, Pad, StackupLayer, Track, Via, ZonePolygon,
)
from ..raster import LayerRaster, rasterize_rings
from .ipcd356 import Netlist, NetlistError, parse as parse_netlist

log = logging.getLogger(__name__)

#: Raster resolution for net reconstruction, in mm. Below any manufacturable clearance, so
#: two separate nets cannot merge, while keeping a large board's raster affordable.
NET_RASTER_MM = 0.05

#: How far a netlist point may be from copper and still be attached to it, in mm. A via's
#: recorded centre sits over its drill, which is not copper, so some tolerance is required.
NET_POINT_TOLERANCE_MM = 0.35

#: Assumed dielectric properties. A .gbrjob carries thicknesses but never permittivity, so
#: these are always guesses on the Gerber path and are always flagged as such.
ASSUMED_EPSILON_R = 4.4
ASSUMED_LOSS_TANGENT = 0.02


class GerberError(ValueError):
    """The Gerber set could not be read. The message is shown to the user."""


@dataclass
class GerberSet:
    """The files that make up an upload, already matched to their roles."""

    #: Copper layer name -> gerber text, in stack order.
    copper: dict[str, str]
    outline: str | None = None
    drill: str | None = None
    netlist: str | None = None
    job: str | None = None


# --- file identification ---------------------------------------------------------------

#: FileFunction values in a .gbrjob, e.g. "Copper,L1,Top". This is authoritative and is
#: preferred over filename guessing whenever a job file is present.
_COPPER_FN = re.compile(r"^Copper,L(?P<index>\d+),(?P<side>Top|Bot|Inr)", re.IGNORECASE)

#: Filename fallbacks, for uploads without a job file.
_LAYER_HINTS = [
    (re.compile(r"(^|[-_])F[._]?Cu\b|top[-_]?copper|\.gtl$", re.IGNORECASE), "F.Cu", 0),
    (re.compile(r"(^|[-_])B[._]?Cu\b|bottom[-_]?copper|\.gbl$", re.IGNORECASE), "B.Cu", 999),
    (re.compile(r"In(?P<n>\d+)[._]?Cu|\.g(?P<g>\d+)$", re.IGNORECASE), None, None),
]


def identify(files: dict[str, bytes]) -> GerberSet:
    """Work out which uploaded file is which."""
    texts: dict[str, str] = {}
    for name, blob in files.items():
        try:
            texts[name] = blob.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue

    job_name = next((n for n in texts if n.lower().endswith(".gbrjob")), None)
    job = texts.get(job_name) if job_name else None

    copper: dict[str, str] = {}
    outline = drill = netlist = None
    ordered: list[tuple[int, str, str]] = []

    if job:
        try:
            doc = json.loads(job)
        except json.JSONDecodeError:
            doc = None
        if doc:
            for entry in doc.get("FilesAttributes", []):
                path = entry.get("Path", "")
                fn = entry.get("FileFunction", "")
                match = next((t for n, t in texts.items() if n.endswith(path)), None)
                if match is None:
                    continue
                m = _COPPER_FN.match(fn)
                if m:
                    # The job file's own layer index is the stack order. Trusting it beats
                    # inferring order from names like "In1" that say nothing about depth.
                    ordered.append((int(m.group("index")), _layer_name_for(fn, path), match))
                elif fn.lower().startswith("profile"):
                    outline = match

    for name, text in texts.items():
        low = name.lower()
        if low.endswith((".drl", ".xln", ".txt")) and _looks_like_drill(text):
            drill = text
        elif low.endswith((".d356", ".ipc", ".net")) or _looks_like_netlist(text):
            netlist = text
        elif outline is None and ("edge" in low or "profile" in low or low.endswith(".gm1")):
            outline = text

    if ordered:
        for _, layer, text in sorted(ordered):
            copper[layer] = text
    else:
        copper = _identify_by_name(texts)

    return GerberSet(copper=copper, outline=outline, drill=drill, netlist=netlist, job=job)


def _layer_name_for(file_function: str, path: str) -> str:
    """A KiCad-style layer name, so both ingest paths produce the same vocabulary."""
    m = _COPPER_FN.match(file_function)
    side = (m.group("side") if m else "").lower()
    if side == "top":
        return "F.Cu"
    if side == "bot":
        return "B.Cu"
    stem = path.rsplit("/", 1)[-1]
    inner = re.search(r"In(\d+)[._]?Cu", stem, re.IGNORECASE)
    if inner:
        return f"In{inner.group(1)}.Cu"
    return f"In{m.group('index') if m else '?'}.Cu"


def _identify_by_name(texts: dict[str, str]) -> dict[str, str]:
    front: dict[str, str] = {}
    inner: list[tuple[int, str, str]] = []
    back: dict[str, str] = {}
    for name, text in texts.items():
        if "%FS" not in text[:2000] and "G04" not in text[:2000]:
            continue  # not a Gerber
        if re.search(r"(^|[-_])F[._]?Cu\b|\.gtl$", name, re.IGNORECASE):
            front["F.Cu"] = text
        elif re.search(r"(^|[-_])B[._]?Cu\b|\.gbl$", name, re.IGNORECASE):
            back["B.Cu"] = text
        else:
            m = re.search(r"In(\d+)[._]?Cu|\.g(\d+)$", name, re.IGNORECASE)
            if m:
                n = int(m.group(1) or m.group(2))
                inner.append((n, f"In{n}.Cu", text))
    out = dict(front)
    for _, layer, text in sorted(inner):
        out[layer] = text
    out.update(back)
    return out


def _looks_like_drill(text: str) -> bool:
    head = text[:2000]
    return "M48" in head or ("T01" in head and "%" in head)


def _looks_like_netlist(text: str) -> bool:
    from .ipcd356 import looks_like_netlist
    return looks_like_netlist(text)


# --- stackup ---------------------------------------------------------------------------

def _stackup_from_job(job_text: str | None, layers: list[str],
                      warnings: list[str]) -> tuple[list[StackupLayer], float]:
    """Build a stackup from the .gbrjob, or synthesise one.

    A job file gives real thicknesses, which matter: the dielectric between a signal layer
    and its reference plane sets the vertical mesh and therefore the run time. It never
    gives permittivity, so that is always assumed here and always flagged.
    """
    entries: list[StackupLayer] = []
    total = 0.0

    if job_text:
        try:
            doc = json.loads(job_text)
        except json.JSONDecodeError:
            doc = {}
        stack = doc.get("MaterialStackup") or []
        for e in stack:
            kind = str(e.get("Type", "")).lower()
            thickness = float(e.get("Thickness", 0.0) or 0.0)
            name = str(e.get("Name", "") or kind)
            if kind == "copper":
                entries.append(StackupLayer(name=name, type="copper",
                                            thickness_mm=thickness, from_file=True))
            elif kind == "dielectric":
                entries.append(StackupLayer(
                    name=name, type="core", thickness_mm=thickness,
                    material=str(e.get("Material", "") or ""),
                    epsilon_r=ASSUMED_EPSILON_R, loss_tangent=ASSUMED_LOSS_TANGENT,
                    # Thickness is real; the electrical properties are not.
                    from_file=False,
                ))
            elif kind in ("soldermask", "solderpaste", "legend"):
                entries.append(StackupLayer(name=name, type=kind,
                                            thickness_mm=thickness, from_file=True))
            total += thickness
        if entries:
            warnings.append(
                "Gerber job files carry layer thicknesses but never permittivity, so "
                f"epsilon_r {ASSUMED_EPSILON_R} and tan_d {ASSUMED_LOSS_TANGENT} were "
                "assumed for every dielectric. A wrong epsilon_r shifts every resonance."
            )
            return entries, (total or 1.6)

    warnings.append(
        "no Gerber job file was found, so the stackup was invented: 1.6 mm FR-4 with 35 um "
        "copper. Solve results will be wrong until this is corrected."
    )
    n = max(2, len(layers))
    each = max(0.05, (1.6 - 0.035 * n) / max(1, n - 1))
    for i, layer in enumerate(layers):
        entries.append(StackupLayer(name=layer, type="copper", thickness_mm=0.035,
                                    from_file=False))
        if i < n - 1:
            entries.append(StackupLayer(
                name=f"dielectric {i + 1}", type="core", thickness_mm=each,
                material="FR4", epsilon_r=ASSUMED_EPSILON_R,
                loss_tangent=ASSUMED_LOSS_TANGENT, from_file=False,
            ))
    return entries, 1.6


# --- geometry --------------------------------------------------------------------------

def _aperture_ring(aperture, x: float, y: float) -> list[tuple[float, float]]:
    """The outline of a flashed aperture, in mm.

    Circles and rectangles are exact. Anything else — a macro, a polygon, an obround — falls
    back to its bounding box, which over-covers rather than under-covers. That is the safe
    direction: a sliver of extra copper is a rounding error, a missing connection is a
    missing net.
    """
    name = type(aperture).__name__
    if name == "CircleAperture":
        return g.circle(x, y, float(aperture.diameter) / 2.0)
    if name == "RectangleAperture":
        return g.rect(x, y, float(aperture.w), float(aperture.h))
    if name == "ObroundAperture":
        return g.oval(x, y, float(aperture.w), float(aperture.h))

    (x0, y0), (x1, y1) = aperture.bounding_box()
    return g.rect(x + (x0 + x1) / 2, y + (y0 + y1) / 2, x1 - x0, y1 - y0)


def _read_layer(text: str, layer: str, warnings: list[str]):
    """Split one copper Gerber into tracks, pads and filled regions."""
    from gerbonara import GerberFile

    try:
        gf = GerberFile.from_string(text)
    except Exception as exc:  # noqa: BLE001 - gerbonara raises a variety of types
        raise GerberError(f"{layer} could not be parsed: {exc}") from exc

    tracks: list[Track] = []
    pads: list[Pad] = []
    zones: list[ZonePolygon] = []
    approximated = 0

    for obj in gf.objects:
        kind = type(obj).__name__
        # Gerber Y is up; KiCad's is down. Negate here so the rest of the system sees one
        # convention and normalize.py stays the only place the axis is flipped.
        if kind == "Line":
            ap = getattr(obj, "aperture", None)
            width = 0.0
            if ap is not None and type(ap).__name__ == "CircleAperture":
                width = float(ap.diameter)
            elif ap is not None:
                try:
                    width = float(ap.equivalent_width())
                    approximated += 1
                except Exception:  # noqa: BLE001
                    width = 0.0
            if width <= 0:
                continue
            tracks.append(Track(
                layer=layer, net="", width_mm=width,
                pts=[(float(obj.x1), -float(obj.y1)), (float(obj.x2), -float(obj.y2))],
            ))

        elif kind == "Arc":
            ap = getattr(obj, "aperture", None)
            width = float(ap.diameter) if ap is not None and hasattr(ap, "diameter") else 0.0
            if width <= 0:
                continue
            try:
                pts = [(float(px), -float(py)) for px, py in _arc_points(obj)]
            except Exception:  # noqa: BLE001
                pts = [(float(obj.x1), -float(obj.y1)), (float(obj.x2), -float(obj.y2))]
            if len(pts) >= 2:
                tracks.append(Track(layer=layer, net="", width_mm=width, pts=pts,
                                    is_arc=True))

        elif kind == "Flash":
            ap = getattr(obj, "aperture", None)
            if ap is None:
                continue
            ring = _aperture_ring(ap, float(obj.x), -float(obj.y))
            if type(ap).__name__ not in ("CircleAperture", "RectangleAperture",
                                          "ObroundAperture"):
                approximated += 1
            if ring:
                pads.append(Pad(ref="", number="", net="", layers=[layer],
                                x=float(obj.x), y=-float(obj.y), ring=ring))

        elif kind == "Region":
            try:
                ring = [(float(px), -float(py)) for px, py in _region_points(obj)]
            except Exception:  # noqa: BLE001
                continue
            if len(ring) >= 3:
                zones.append(ZonePolygon(layer=layer, net="", ring=ring))

    if approximated:
        warnings.append(
            f"{approximated} shapes on {layer} use aperture macros and were approximated by "
            f"their bounding box; they are slightly larger than the real copper"
        )
    return tracks, pads, zones


def _arc_points(obj) -> list[tuple[float, float]]:
    """Points along a Gerber arc, via its centre offset."""
    cx = float(obj.x1) + float(getattr(obj, "cx", 0.0))
    cy = float(obj.y1) + float(getattr(obj, "cy", 0.0))
    r = math.hypot(float(obj.x1) - cx, float(obj.y1) - cy)
    if r <= 0:
        return [(float(obj.x1), float(obj.y1)), (float(obj.x2), float(obj.y2))]
    a0 = math.atan2(float(obj.y1) - cy, float(obj.x1) - cx)
    a1 = math.atan2(float(obj.y2) - cy, float(obj.x2) - cx)
    ccw = not bool(getattr(obj, "clockwise", False))
    sweep = a1 - a0
    if ccw and sweep <= 0:
        sweep += 2 * math.pi
    elif not ccw and sweep >= 0:
        sweep -= 2 * math.pi
    n = max(8, min(180, int(abs(sweep) / 0.1)))
    return [
        (cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n))
        for i in range(n + 1)
    ]


def _region_points(region) -> list[tuple[float, float]]:
    """A filled region's outline, as plain points.

    gerbonara exposes ``Region.outline`` as a list of (x, y) tuples already; arcs within a
    region are flattened by it. Reaching for segment objects instead returns 3-tuples and
    fails, which is how filled pours silently came back empty.
    """
    return [(float(x), float(y)) for x, y in region.outline]
