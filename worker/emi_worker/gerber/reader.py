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
from dataclasses import dataclass, field

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
    #: Every drill file, in name order. KiCad writes plated and non-plated holes to separate
    #: files by default, so there is usually more than one.
    drills: list[str] = field(default_factory=list)
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
    outline = netlist = None
    drills: list[tuple[str, str]] = []
    ordered: list[tuple[int, str, str]] = []

    if job:
        doc = _job_document(job)
        if doc:
            for entry in _job_list(doc, "FilesAttributes"):
                path = str(entry.get("Path", "") or "")
                fn = str(entry.get("FileFunction", "") or "")
                if not path:
                    continue
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
            drills.append((name, text))
        elif low.endswith((".d356", ".ipc", ".net")) or _looks_like_netlist(text):
            netlist = text
        elif outline is None and ("edge" in low or "profile" in low or low.endswith(".gm1")):
            outline = text

    if ordered:
        for _, layer, text in sorted(ordered):
            copper[layer] = text
    else:
        copper = _identify_by_name(texts)

    return GerberSet(copper=copper, outline=outline, drills=[t for _, t in sorted(drills)],
                     netlist=netlist, job=job)


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

def _job_document(text: str) -> dict | None:
    """The job file as an object, or None when it is not one.

    A job file is JSON, but one that is truncated, nested thousands deep or a bare list is
    still an upload somebody made, and the right answer is "no job file", not a crash.
    """
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _job_list(doc: dict, key: str) -> list[dict]:
    """The entries of one list in the job file that are objects; anything else is skipped."""
    val = doc.get(key)
    if not isinstance(val, list):
        return []
    return [e for e in val if isinstance(e, dict)]


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
        doc = _job_document(job_text) or {}
        for e in _job_list(doc, "MaterialStackup"):
            kind = str(e.get("Type", "")).lower()
            try:
                thickness = float(e.get("Thickness", 0.0) or 0.0)
            except (TypeError, ValueError):
                thickness = 0.0
            if not 0.0 <= thickness < 100.0:
                thickness = 0.0
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
#
# gerbonara hands back every coordinate and aperture size in the file's own unit and never
# converts on its own. Reading those numbers as millimetres made a %MOIN% board 25.4 times
# too small: a one-inch track came back one millimetre long, on a board that still parsed,
# rendered and named its nets. Everything below goes through _to_mm or asks gerbonara for
# MM explicitly.

def _to_mm(unit):
    """A converter from ``unit`` to millimetres. A file with no MO statement is taken as mm."""
    from gerbonara.utils import MM

    if unit is None:
        return float
    return lambda v: float(MM(float(v), unit))


def _aperture_ring(aperture, x: float, y: float) -> list[tuple[float, float]]:
    """The outline of a flashed aperture, in mm, with ``x`` and ``y`` already in mm.

    Circles and rectangles are exact. Anything else — a macro, a polygon, an obround — falls
    back to its bounding box, which over-covers rather than under-covers. That is the safe
    direction: a sliver of extra copper is a rounding error, a missing connection is a
    missing net.
    """
    from gerbonara.utils import MM

    mm = _to_mm(getattr(aperture, "unit", None))
    name = type(aperture).__name__
    if name == "CircleAperture":
        return g.circle(x, y, mm(aperture.diameter) / 2.0)
    if name == "RectangleAperture":
        return g.rect(x, y, mm(aperture.w), mm(aperture.h))
    if name == "ObroundAperture":
        return g.oval(x, y, mm(aperture.w), mm(aperture.h))

    (x0, y0), (x1, y1) = aperture.bounding_box(MM)
    return g.rect(x + (x0 + x1) / 2, y + (y0 + y1) / 2, x1 - x0, y1 - y0)


def _line_width(aperture) -> tuple[float, bool]:
    """(width in mm, approximated?) for the aperture a line is drawn with."""
    from gerbonara.utils import MM

    if aperture is None:
        return 0.0, False
    if type(aperture).__name__ == "CircleAperture":
        return _to_mm(aperture.unit)(aperture.diameter), False
    try:
        return float(aperture.equivalent_width(MM)), True
    except Exception:  # noqa: BLE001
        return 0.0, False


#: Negative images. ``%IPNEG*%`` is the deprecated RS-274X statement, and gerbonara already
#: inverts every object's polarity when it sees it. The X2 attribute says the same thing
#: but gerbonara leaves the objects alone, so the reader inverts them itself.
_IP_NEG = re.compile(r"%IPNEG\*%")
_X2_NEG = re.compile(r"%TF\.FilePolarity,Negative\*%", re.IGNORECASE)


@dataclass
class _Pour:
    """A dark region, and the clear shapes cut out of it so far."""

    zone: ZonePolygon
    bbox: tuple[float, float, float, float]
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    hole_bboxes: list[tuple[float, float, float, float]] = field(default_factory=list)


def _bbox(ring) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_overlap(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _inside(x: float, y: float, ring) -> bool:
    """Even-odd point-in-polygon."""
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _segments_cross(p, q, r, s) -> bool:
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = orient(r, s, p), orient(r, s, q)
    d3, d4 = orient(p, q, r), orient(p, q, s)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


def _rings_overlap(a, b) -> bool:
    """Whether two simple polygons share any area. Only called after a bbox test."""
    if any(_inside(x, y, b) for x, y in a) or any(_inside(x, y, a) for x, y in b):
        return True
    na, nb = len(a), len(b)
    return any(
        _segments_cross(a[i], a[(i + 1) % na], b[j], b[(j + 1) % nb])
        for i in range(na) for j in range(nb)
    )


def _cut(pours: list[_Pour], ring) -> bool | None:
    """Cut a clear shape out of the dark pour that encloses it.

    True if it was cut (or lands where there is no copper anyway), False if it overlaps
    copper in a way this model cannot represent, None if it touches no pour at all.

    The model holds a pour as one ring, the way KiCad stores a zone fill: holes are joined
    to the outline by zero-width bridges (see _keyhole). That represents a clearance that
    sits wholly inside one pour and apart from every other clearance, which is what plane
    anti-pads and pad clearances are. A clear line that splits a plane in two, or two
    clearances that overlap, would need real polygon clipping, and is reported instead.
    """
    box = _bbox(ring)
    touched = False
    # Latest first: a clear shape applies to everything drawn before it, and the most recent
    # pour is the one a clearance is almost always cutting.
    for pour in reversed(pours):
        if not _bbox_overlap(box, pour.bbox):
            continue
        touched = True
        # Contained means no edge of the shape crosses the pour's outline and one of its
        # points is inside. Only edges near the shape are tested: a real pour outline runs
        # to thousands of vertices, and a plane has as many anti-pads.
        outline = pour.zone.ring
        n = len(outline)
        near = [
            (outline[j], outline[(j + 1) % n]) for j in range(n)
            if _bbox_overlap(box, _bbox((outline[j], outline[(j + 1) % n])))
        ]
        if any(_segments_cross(ring[i], ring[(i + 1) % len(ring)], a, b)
               for a, b in near for i in range(len(ring))):
            continue
        if not _inside(ring[0][0], ring[0][1], outline):
            continue
        clash = [
            h for h, hb in zip(pour.holes, pour.hole_bboxes)
            if _bbox_overlap(box, hb) and _rings_overlap(ring, h)
        ]
        if clash:
            # Already clear if it lies wholly inside an existing cut; otherwise the two
            # would overlap, and even-odd filling would put copper back in the overlap.
            return all(_inside(x, y, clash[0]) for x, y in ring) or False
        pour.holes.append(list(ring))
        pour.hole_bboxes.append(box)
        return True
    return False if touched else None


def _keyhole(outer, holes) -> list[tuple[float, float]]:
    """One ring for a polygon with holes, each hole joined to the outline by a bridge.

    This is the form KiCad stores zone fills in, and the one every consumer of ZonePolygon
    already handles: even-odd filling carves the holes, the shoelace area subtracts them and
    earcut triangulates around them. Each hole is bridged from its rightmost vertex to the
    nearest edge straight to its right, working from the rightmost hole inward, so no bridge
    can cross a hole that has not been joined yet.
    """
    if not holes:
        return list(outer)
    ring = list(outer)
    if g.signed_area(ring) < 0:
        ring.reverse()
    for hole in sorted(holes, key=lambda h: -max(p[0] for p in h)):
        h = list(hole)
        if g.signed_area(h) > 0:
            h.reverse()
        mi = max(range(len(h)), key=lambda i: h[i][0])
        mx, my = h[mi]
        best: tuple[float, int] | None = None
        n = len(ring)
        for i in range(n):
            (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % n]
            if (y0 > my) == (y1 > my):
                continue
            xi = x0 + (my - y0) * (x1 - x0) / (y1 - y0)
            if xi >= mx and (best is None or xi < best[0]):
                best = (xi, i)
        if best is None:
            continue
        xi, i = best
        p = (xi, my)
        loop = h[mi:] + h[:mi] + [h[mi]]
        ring = ring[:i + 1] + [p] + loop + [p] + ring[i + 1:]
    return ring


def _read_layer(text: str, layer: str, warnings: list[str],
                extent: tuple[float, float, float, float] | None = None):
    """Split one copper Gerber into tracks, pads and filled regions.

    ``extent`` is the board's bounding box in the model's coordinates, used as the copper of
    a negative image. Without it, the extent of what the layer draws stands in.
    """
    from gerbonara import GerberFile
    from gerbonara.utils import MM

    try:
        gf = GerberFile.from_string(text)
    except Exception as exc:  # noqa: BLE001 - gerbonara raises a variety of types
        raise GerberError(f"{layer} could not be parsed: {exc}") from exc

    tracks: list[Track] = []
    pads: list[Pad] = []
    zones: list[ZonePolygon] = []
    approximated = 0

    # A negative image is copper everywhere except where it draws. Model that as one pour
    # covering the board with every drawn shape cut out of it, which is the same machinery
    # as a clear shape on a positive layer.
    head = text[:20000]
    ip_neg = bool(_IP_NEG.search(head))
    negative = ip_neg or bool(_X2_NEG.search(head))
    invert = negative and not ip_neg

    pours: list[_Pour] = []
    #: Bounding boxes of dark tracks and pads, to tell a clear shape that sits on copper this
    #: model cannot cut from one that sits on bare board.
    other_dark: list[tuple[float, float, float, float]] = []
    uncut = 0

    if negative:
        box = extent or _layer_extent(gf)
        if box is not None:
            x0, y0, x1, y1 = box
            base = ZonePolygon(layer=layer, net="",
                               ring=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
            zones.append(base)
            pours.append(_Pour(zone=base, bbox=box))

    for obj in gf.objects:
        kind = type(obj).__name__
        mm = _to_mm(getattr(obj, "unit", None))
        dark = bool(getattr(obj, "polarity_dark", True)) != invert
        # Gerber Y is up; KiCad's is down. Negate here so the rest of the system sees one
        # convention and normalize.py stays the only place the axis is flipped.
        rings: list[list[tuple[float, float]]] = []

        if kind == "Line":
            width, approx = _line_width(getattr(obj, "aperture", None))
            if width <= 0:
                continue
            pts = [(mm(obj.x1), -mm(obj.y1)), (mm(obj.x2), -mm(obj.y2))]
            if dark:
                approximated += approx
                tracks.append(Track(layer=layer, net="", width_mm=width, pts=pts))
                other_dark.append(_bbox(g.thick_polyline(pts, width)[0]))
            else:
                rings = g.thick_polyline(pts, width)

        elif kind == "Arc":
            width, _ = _line_width(getattr(obj, "aperture", None))
            if width <= 0:
                continue
            try:
                pts = [(px, -py) for px, py in _arc_points(obj)]
            except Exception:  # noqa: BLE001
                pts = [(mm(obj.x1), -mm(obj.y1)), (mm(obj.x2), -mm(obj.y2))]
            if len(pts) < 2:
                continue
            if dark:
                tracks.append(Track(layer=layer, net="", width_mm=width, pts=pts,
                                    is_arc=True))
                other_dark.append(_bbox(pts))
            else:
                rings = g.thick_polyline(pts, width)

        elif kind == "Flash":
            ap = getattr(obj, "aperture", None)
            if ap is None:
                continue
            x, y = mm(obj.x), -mm(obj.y)
            ring = _aperture_ring(ap, x, y)
            if not ring:
                continue
            if dark:
                if type(ap).__name__ not in ("CircleAperture", "RectangleAperture",
                                              "ObroundAperture"):
                    approximated += 1
                pads.append(Pad(ref="", number="", net="", layers=[layer],
                                x=x, y=y, ring=ring))
                other_dark.append(_bbox(ring))
            else:
                rings = [ring]

        elif kind == "Region":
            try:
                ring = [(px, -py) for px, py in _region_points(obj)]
            except Exception:  # noqa: BLE001
                continue
            if len(ring) < 3:
                continue
            if dark:
                zone = ZonePolygon(layer=layer, net="", ring=ring)
                zones.append(zone)
                pours.append(_Pour(zone=zone, bbox=_bbox(ring)))
            else:
                rings = [ring]

        for ring in rings:
            if len(ring) < 3:
                continue
            result = _cut(pours, ring)
            if result is None:
                box = _bbox(ring)
                if any(_bbox_overlap(box, b) for b in other_dark):
                    uncut += 1
            elif result is False:
                uncut += 1

    for pour in pours:
        if pour.holes:
            pour.zone.ring = _keyhole(pour.zone.ring, pour.holes)

    if uncut:
        warnings.append(
            f"{layer}: {uncut} clear (cut-out) shapes could not be applied, so some copper "
            f"there is overstated"
        )
    if approximated:
        warnings.append(
            f"{approximated} shapes on {layer} use aperture macros and were approximated by "
            f"their bounding box; they are slightly larger than the real copper"
        )
    return tracks, pads, zones


def _layer_extent(gf) -> tuple[float, float, float, float] | None:
    """What a layer draws, in model coordinates, for a negative image with no outline."""
    from gerbonara.utils import MM

    xs: list[float] = []
    ys: list[float] = []
    for obj in gf.objects:
        try:
            (x0, y0), (x1, y1) = obj.bounding_box(MM)
        except Exception:  # noqa: BLE001
            continue
        xs += [x0, x1]
        ys += [-y0, -y1]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _arc_points(obj) -> list[tuple[float, float]]:
    """Points along a Gerber arc, via its centre offset, in mm (Gerber's Y-up)."""
    mm = _to_mm(getattr(obj, "unit", None))
    x1, y1, x2, y2 = mm(obj.x1), mm(obj.y1), mm(obj.x2), mm(obj.y2)
    cx = x1 + mm(getattr(obj, "cx", 0.0))
    cy = y1 + mm(getattr(obj, "cy", 0.0))
    r = math.hypot(x1 - cx, y1 - cy)
    if r <= 0:
        return [(x1, y1), (x2, y2)]
    a0 = math.atan2(y1 - cy, x1 - cx)
    a1 = math.atan2(y2 - cy, x2 - cx)
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
    """A filled region's outline, as plain points in mm.

    gerbonara exposes ``Region.outline`` as a list of (x, y) tuples already; arcs within a
    region are flattened by it. Reaching for segment objects instead returns 3-tuples and
    fails, which is how filled pours silently came back empty.
    """
    mm = _to_mm(getattr(region, "unit", None))
    return [(mm(x), mm(y)) for x, y in region.outline]
