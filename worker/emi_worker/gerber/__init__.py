"""Gerber ingest: a Gerber set plus an IPC-D-356 netlist, in; a BoardModel, out.

The whole point of producing a BoardModel is that nothing downstream needs to know which
format the board arrived in. The viewer, the rules tier and the mesher all read the same
structure whether it came from a .kicad_pcb or from fabrication output.

A netlist is required, not optional. Gerbers carry no net information at all, and without
names there is no net picker, no way to tell a signal from its reference, and no way to
place a port by name — which is most of the tool.
"""

from __future__ import annotations

import logging

from ..kicad.board import BoardModel, CopperLayer, Via
from . import connectivity as conn_mod
from .connectivity import Connectivity, apply_nets, attach_parts, rings_for_geometry
from .drill import Hole, holes_to_vias, parse_holes
from .ipcd356 import Netlist, NetlistError, parse as parse_netlist
from .reader import GerberError, GerberSet, identify, _read_layer, _stackup_from_job

log = logging.getLogger(__name__)

__all__ = [
    "GerberError", "GerberSet", "NetlistError", "identify", "load_gerber_board",
]


def load_gerber_board(files: dict[str, bytes]) -> BoardModel:
    """Build a BoardModel from an uploaded Gerber set."""
    warnings: list[str] = []
    gset = identify(files)

    if not gset.copper:
        raise GerberError(
            "no copper layers were found. Upload the Gerber files together with the drill "
            "file, the job file (.gbrjob) and an IPC-D-356 netlist — a zip of your "
            "fabrication output folder is the easiest way."
        )

    if not gset.netlist:
        raise GerberError(
            "no IPC-D-356 netlist was found. Gerbers carry no net information, so without "
            "one there is no way to tell which copper is which signal. Export it from your "
            "CAD tool — in KiCad it is File > Fabrication Outputs > IPC-D-356 Netlist — and "
            "include it in the upload."
        )

    netlist = parse_netlist(gset.netlist)
    warnings.extend(netlist.warnings)

    layers = list(gset.copper)
    log.info("gerber set: %d copper layers %s", len(layers), layers)

    # The outline first: a negative plane image is copper everywhere inside it.
    outline = _read_outline(gset.outline, warnings)
    extent = _outline_extent(outline)

    tracks, pads, zones = [], [], []
    for layer, text in gset.copper.items():
        t, p, z = _read_layer(text, layer, warnings, extent)
        tracks.extend(t)
        pads.extend(p)
        zones.extend(z)

    if not (tracks or pads or zones):
        raise GerberError("the Gerber files contain no copper")

    vias: list[Via] = []
    if gset.drills:
        holes: list[Hole] = []
        for text in gset.drills:
            h, drill_warnings = parse_holes(text)
            holes.extend(h)
            warnings.extend(drill_warnings)
        vias = holes_to_vias(holes, layers)
        if not vias:
            warnings.append("the drill files contained no plated holes")
    else:
        warnings.append(
            "no drill file was found, so vias are missing from the model. Layer transitions "
            "will not be simulated and the return-via check cannot run."
        )

    stackup, thickness = _stackup_from_job(gset.job, layers, warnings)

    # --- nets ---
    bounds = _bounds(tracks, pads, zones, outline)
    conn = Connectivity(layers, bounds)
    conn.build_rasters({
        layer: rings_for_geometry(tracks, pads, zones, layer) for layer in layers
    })
    conn.assign(netlist, vias)
    apply_nets(conn, tracks, pads, zones, vias)
    warnings.extend(conn.warnings)
    pads, vias, part_warnings = attach_parts(conn, netlist, pads, vias, layers)
    warnings.extend(part_warnings)

    named = sum(1 for t in tracks if t.net) + sum(1 for z in zones if z.net)
    total = len(tracks) + len(zones)
    if total and named / total < 0.5:
        warnings.append(
            f"only {named} of {total} copper features could be matched to a net. The "
            f"netlist and the Gerbers may be from different revisions of the board."
        )

    # Nets in netlist order, so the most meaningful names come first.
    seen: dict[str, None] = {}
    for point in netlist.points:
        seen.setdefault(point.net, None)

    model = BoardModel(
        version=0,
        generator="gerber",
        thickness_mm=thickness,
        copper_layers=[
            CopperLayer(ordinal=i, name=name, kind="signal")
            for i, name in enumerate(layers)
        ],
        stackup=stackup,
        nets=list(seen),
        tracks=tracks,
        vias=vias,
        pads=pads,
        zones=zones,
        outline=outline,
        warnings=warnings,
    )
    log.info(
        "gerber board: %d tracks, %d pads, %d zones, %d vias, %d nets",
        len(tracks), len(pads), len(zones), len(vias), len(model.nets),
    )
    return model


def _read_outline(text: str | None, warnings: list[str]) -> list[list[tuple[float, float]]]:
    if not text:
        warnings.append(
            "no board outline (profile) Gerber was found; the board extent was inferred "
            "from the copper, so it may be slightly smaller than the real board"
        )
        return []
    try:
        from gerbonara import GerberFile
        gf = GerberFile.from_string(text)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"the board outline could not be read ({exc}); using the copper extent")
        return []

    from .reader import _arc_points, _to_mm

    rings: list[list[tuple[float, float]]] = []
    for obj in gf.objects:
        kind = type(obj).__name__
        # In the file's own unit until converted; an inch outline read as mm made the
        # board 25.4 times too small.
        mm = _to_mm(getattr(obj, "unit", None))
        if kind == "Line":
            rings.append([
                (mm(obj.x1), -mm(obj.y1)), (mm(obj.x2), -mm(obj.y2)),
            ])
        elif kind == "Arc":
            try:
                rings.append([(float(x), -float(y)) for x, y in _arc_points(obj)])
            except Exception:  # noqa: BLE001
                continue
    return rings


def _outline_extent(outline) -> tuple[float, float, float, float] | None:
    pts = [p for ring in outline for p in ring]
    if not pts:
        return None
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def _bounds(tracks, pads, zones, outline) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for t in tracks:
        for x, y in t.pts:
            xs.append(x)
            ys.append(y)
    for p in pads:
        for x, y in p.ring:
            xs.append(x)
            ys.append(y)
    for z in zones:
        for x, y in z.ring:
            xs.append(x)
            ys.append(y)
    for ring in outline:
        for x, y in ring:
            xs.append(x)
            ys.append(y)
    if not xs:
        raise GerberError("the board has no geometry to measure")
    # A small margin so copper exactly on the edge still has a pixel to live in.
    return (min(xs) - 0.5, min(ys) - 0.5, max(xs) + 0.5, max(ys) + 0.5)
