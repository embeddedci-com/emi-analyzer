"""Extract a board model from a parsed ``.kicad_pcb`` tree.

Format drift is the main hazard here, and it is handled by reading defensively rather than
by version-switching. Two real examples from the boards in this repo:

* KiCad 20241229 declares nets up front as ``(net 3 "GND")`` and refers to them by index.
  KiCad 20260206 dropped the table entirely and writes ``(net "GND")`` inline. Both appear
  in this codebase's own hardware, so both are supported and the net table is built from
  whichever is present.
* Board outlines turn up as ``gr_line``, ``gr_rect``, ``gr_arc``, ``gr_circle`` or
  ``gr_poly`` depending on how the outline was drawn. The BenchPod board uses a single
  ``gr_rect``; the solar board uses lines.

Everything is in millimetres, in KiCad's coordinate system (X right, Y *down*). The flip to
a viewer-friendly Y-up space happens once, in normalize.py, so that this module can be
compared directly against the source file when something looks wrong.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import geometry as g
from .sexpr import Node, child, children, flag, number, points, sym, text, value, values

#: Layers whose names end in ".Cu" are copper. KiCad has never broken this convention.
COPPER_SUFFIX = ".Cu"

#: Stackup entry types that are board dielectric, as opposed to solder mask, silkscreen or
#: paste. Only these need epsilon_r: solder mask is a ~10 um surface film whose permittivity
#: barely moves a result, and demanding one for it produces a warning on every KiCad board
#: ever exported.
DIELECTRIC_TYPES = frozenset({"core", "prepreg", "dielectric"})

#: Shown when zones were saved without a fill. Ingest also carries it into the rules notes,
#: since the checks it affects are the ones that go quiet.
ZONES_UNFILLED_NOTE = (
    "{n} zone{s} not filled, so plane checks skip them. Refill zones in KiCad (B) and save "
    "before exporting."
)


@dataclass
class StackupLayer:
    """One physical layer, copper or dielectric.

    ``epsilon_r`` and ``loss_tangent`` are what make a solve meaningful, and they are the
    first thing missing from a hand-made stackup -- hence the explicit defaults and the
    ``from_file`` flag, so the UI can tell the user which numbers are theirs and which are
    ours.
    """

    name: str
    type: str
    thickness_mm: float = 0.0
    material: str = ""
    epsilon_r: float = 0.0
    loss_tangent: float = 0.0
    from_file: bool = True

    @property
    def is_copper(self) -> bool:
        return self.type == "copper"

    @property
    def is_dielectric(self) -> bool:
        """Board dielectric, as opposed to solder mask, silkscreen or paste."""
        return self.type in DIELECTRIC_TYPES

    @property
    def role(self) -> str:
        if self.is_copper:
            return "copper"
        if self.is_dielectric:
            return "dielectric"
        return "other"


@dataclass
class CopperLayer:
    ordinal: int
    name: str
    kind: str  # signal | power | mixed | jumper | user


@dataclass
class Track:
    """A track segment or arc, as a polyline plus a width."""

    layer: str
    net: str
    width_mm: float
    pts: list[tuple[float, float]]
    is_arc: bool = False

    @property
    def length_mm(self) -> float:
        return sum(
            math.dist(self.pts[i], self.pts[i + 1]) for i in range(len(self.pts) - 1)
        )


@dataclass
class Via:
    x: float
    y: float
    size_mm: float
    drill_mm: float
    layers: list[str]
    net: str
    kind: str = "through"  # through | blind | micro


@dataclass
class Pad:
    ref: str
    number: str
    net: str
    layers: list[str]
    x: float
    y: float
    ring: list[tuple[float, float]]
    pad_type: str = "smd"  # smd | thru_hole | np_thru_hole | connect
    drill_mm: float = 0.0
    #: The footprint's Value ("100nF", "16MHz") and library name ("Capacitor_SMD:C_0402").
    #: A reference designator alone says C12 is probably a capacitor; these say what it is,
    #: which is the difference between a decoupling check that guesses and one that knows.
    value: str = ""
    footprint: str = ""

    @property
    def is_through(self) -> bool:
        return self.pad_type in ("thru_hole", "np_thru_hole")


@dataclass
class ZonePolygon:
    layer: str
    net: str
    ring: list[tuple[float, float]]


@dataclass
class BoardModel:
    version: int = 0
    generator: str = ""
    thickness_mm: float = 1.6
    copper_layers: list[CopperLayer] = field(default_factory=list)
    stackup: list[StackupLayer] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=list)
    zones: list[ZonePolygon] = field(default_factory=list)
    outline: list[list[tuple[float, float]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def copper_layer_names(self) -> list[str]:
        return [layer.name for layer in self.copper_layers]


def _at(node: Node) -> tuple[float, float, float]:
    """Read an ``(at x y [angle])`` child."""
    vs = values(node, "at")
    x = float(vs[0]) if len(vs) > 0 and isinstance(vs[0], (int, float)) else 0.0
    y = float(vs[1]) if len(vs) > 1 and isinstance(vs[1], (int, float)) else 0.0
    a = float(vs[2]) if len(vs) > 2 and isinstance(vs[2], (int, float)) else 0.0
    return x, y, a


def _name(v) -> str:
    """An atom that is conceptually a name. KiCad 5 leaves pad numbers unquoted, so the
    tokeniser reads pad 1 as the number 1.0, and "U1.1.0" is not a pin anyone has."""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v)


def _layer_list(node: Node) -> list[str]:
    out: list[str] = []
    for c in children(node, "layers"):
        for v in c[1:]:
            if v == "F&B.Cu":
                # KiCad's name for "both outer layers and no inner ones". Left as written it
                # matched no copper layer, so the pad was never drawn.
                out.extend(("F.Cu", "B.Cu"))
            elif isinstance(v, str):
                out.append(v)
            elif isinstance(v, float):
                out.append(_name(v))
    if not out:
        single = text(node, "layer", default="")
        if single:
            out.append(single)
    return out


class _NetResolver:
    """Resolves a ``(net ...)`` reference to a net name across both file formats."""

    def __init__(self, root: Node):
        self.by_index: dict[int, str] = {}
        self.order: list[str] = []
        self._seen: set[str] = set()

        # Old format: a table of (net <index> "<name>") at the top level.
        for n in children(root, "net"):
            vs = [v for v in n[1:] if not isinstance(v, list)]
            if len(vs) >= 2 and isinstance(vs[0], float):
                name = str(vs[1])
                self.by_index[int(vs[0])] = name
                self._add(name)

    def _add(self, name: str) -> None:
        if name not in self._seen:
            self._seen.add(name)
            self.order.append(name)

    def resolve(self, node: Node) -> str:
        """Return the net name referenced by this node, or "" for no net."""
        c = child(node, "net")
        if c is None:
            return ""
        vs = [v for v in c[1:] if not isinstance(v, list)]
        if not vs:
            return ""
        first = vs[0]
        if isinstance(first, float):
            # Old format: an index. A trailing name may also be present on some nodes.
            if len(vs) >= 2 and isinstance(vs[1], str):
                name = vs[1]
                self.by_index.setdefault(int(first), name)
                self._add(name)
                return name
            name = self.by_index.get(int(first), "")
            if name:
                self._add(name)
            return name
        name = str(first)
        if name:
            self._add(name)
        return name


def _parse_layers(root: Node) -> list[CopperLayer]:
    layers_node = child(root, "layers")
    out: list[CopperLayer] = []
    if layers_node is None:
        return out
    for entry in layers_node[1:]:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        ordinal_raw, name = entry[0], entry[1]
        if not isinstance(name, str) or not name.endswith(COPPER_SUFFIX):
            continue
        ordinal = int(ordinal_raw) if isinstance(ordinal_raw, float) else 0
        kind = str(entry[2]) if len(entry) > 2 else "signal"
        out.append(CopperLayer(ordinal=ordinal, name=name, kind=kind))

    # KiCad's ordinals are not a stack order: F.Cu is 0, B.Cu is 2, and inner layers start
    # at 4. Sorting naively would put the bottom layer in the middle of the board.
    def stack_key(layer: CopperLayer) -> tuple[int, int]:
        if layer.name == "F.Cu":
            return (0, 0)
        if layer.name == "B.Cu":
            return (2, 0)
        return (1, layer.ordinal)

    out.sort(key=stack_key)
    return out


def _parse_stackup(root: Node, copper: list[CopperLayer], warnings: list[str]) -> list[StackupLayer]:
    setup = child(root, "setup")
    stackup_node = child(setup, "stackup") if setup is not None else None

    out: list[StackupLayer] = []
    if stackup_node is not None:
        for layer in children(stackup_node, "layer"):
            name = str(layer[1]) if len(layer) > 1 and isinstance(layer[1], str) else ""
            out.append(StackupLayer(
                name=name,
                type=text(layer, "type", default=""),
                thickness_mm=number(layer, "thickness", default=0.0),
                material=text(layer, "material", default=""),
                epsilon_r=number(layer, "epsilon_r", default=0.0),
                loss_tangent=number(layer, "loss_tangent", default=0.0),
            ))

    if not any(s.is_copper for s in out):
        # No stackup in the file: synthesise a plausible FR-4 one so the board can still be
        # viewed, and flag it loudly. Everything downstream keys off from_file=False to tell
        # the user these numbers are ours, not theirs.
        warnings.append(
            "no stackup in the board file; assuming 1.6 mm FR-4 with 35 um copper "
            "(epsilon_r 4.4, tan_d 0.02). Solve results will be wrong until this is corrected."
        )
        out = []
        n = max(2, len(copper))
        dielectric_total = 1.6 - 0.035 * n
        each = max(0.05, dielectric_total / max(1, n - 1))
        for i, layer in enumerate(copper):
            out.append(StackupLayer(
                name=layer.name, type="copper", thickness_mm=0.035, from_file=False,
            ))
            if i < n - 1:
                out.append(StackupLayer(
                    name=f"dielectric {i + 1}", type="core", thickness_mm=each,
                    material="FR4", epsilon_r=4.4, loss_tangent=0.02, from_file=False,
                ))
    else:
        missing = [
            s.name for s in out
            if s.is_dielectric and s.thickness_mm > 0 and s.epsilon_r <= 0
        ]
        if missing:
            warnings.append(
                f"dielectric layers {', '.join(missing)} have no epsilon_r; "
                "assuming 4.4. A wrong epsilon_r shifts every resonance."
            )
            for s in out:
                if s.is_dielectric and s.thickness_mm > 0 and s.epsilon_r <= 0:
                    s.epsilon_r = 4.4
                    s.loss_tangent = s.loss_tangent or 0.02
                    s.from_file = False
    return out


def _pad_ring(pad: Node, px: float, py: float, angle: float,
              warnings: list[str], label: str = "") -> list[tuple[float, float]]:
    """Build the copper outline of one pad, already placed and rotated."""
    shape = str(pad[3]) if len(pad) > 3 and isinstance(pad[3], str) else "rect"
    size = values(pad, "size")
    w = float(size[0]) if len(size) > 0 else 0.0
    h = float(size[1]) if len(size) > 1 else w

    if shape == "circle":
        return g.circle(px, py, w / 2.0)
    if shape == "rect":
        return g.rect(px, py, w, h, angle)
    if shape == "oval":
        return g.oval(px, py, w, h, angle)
    if shape == "roundrect":
        ratio = number(pad, "roundrect_rratio", default=0.25)
        return g.roundrect(px, py, w, h, min(w, h) * ratio, angle)
    if shape == "trapezoid":
        delta = values(pad, "rect_delta")
        dx = float(delta[0]) if len(delta) > 0 else 0.0
        dy = float(delta[1]) if len(delta) > 1 else 0.0
        return g.trapezoid(px, py, w, h, dx, dy, angle)
    if shape == "custom":
        # Custom pads are an arbitrary set of primitives. Approximating by the bounding
        # rectangle of the anchor is wrong in detail but never *smaller* than the real pad,
        # which is the safe direction for connectivity. Flagged so it is not silent.
        warnings.append(
            f"pad {label or '?'} uses a custom shape; approximated by its anchor"
        )
        return g.rect(px, py, max(w, 0.1), max(h, 0.1), angle)

    warnings.append(
        f"pad {label or '?'} has unknown shape {shape!r}; approximated as a rectangle"
    )
    return g.rect(px, py, max(w, 0.1), max(h, 0.1), angle)


def _footprints(root: Node) -> list[Node]:
    """Every placed part. KiCad 5 called them ``module``; reading only ``footprint`` gave a
    KiCad 5 board zero pads, so every part-based check ran on nothing and said nothing."""
    return [*children(root, "footprint"), *children(root, "module")]


def _parse_footprints(root: Node, nets: _NetResolver, warnings: list[str]) -> list[Pad]:
    pads: list[Pad] = []
    for fp in _footprints(root):
        fx, fy, frot = _at(fp)
        ref = ""
        value = ""
        footprint = str(fp[1]) if len(fp) > 1 and isinstance(fp[1], str) else ""
        for prop in children(fp, "property"):
            if len(prop) > 2 and prop[1] == "Reference" and not ref:
                ref = _name(prop[2])
            elif len(prop) > 2 and prop[1] == "Value" and not value:
                value = _name(prop[2])
        # KiCad 7 and earlier kept both as fp_text rather than property.
        for t in children(fp, "fp_text"):
            if len(t) > 2 and t[1] == "reference" and not ref:
                ref = _name(t[2])
            elif len(t) > 2 and t[1] == "value" and not value:
                value = _name(t[2])

        for pad in children(fp, "pad"):
            number_str = _name(pad[1]) if len(pad) > 1 else ""
            pad_type = str(pad[2]) if len(pad) > 2 and isinstance(pad[2], str) else "smd"
            ox, oy, prot = _at(pad)

            # The pad offset is expressed in the footprint's own frame, so it rotates with
            # the footprint. The pad's stored angle, by contrast, is already absolute in
            # KiCad's file format -- it is not added to the footprint's.
            rx, ry = g.rotate(ox, oy, frot)
            px, py = fx + rx, fy + ry

            ring = _pad_ring(pad, px, py, prot, warnings, f"{ref}.{number_str}")
            if not ring:
                continue

            drill = 0.0
            drill_node = child(pad, "drill")
            if drill_node is not None:
                for v in drill_node[1:]:
                    if isinstance(v, float):
                        drill = float(v)
                        break

            pads.append(Pad(
                ref=ref,
                number=number_str,
                net=nets.resolve(pad),
                layers=_layer_list(pad),
                x=px, y=py, ring=ring,
                pad_type=pad_type,
                drill_mm=drill,
                value=value,
                footprint=footprint,
            ))
    return pads


def _xy(node: Node, name: str) -> tuple[float, float] | None:
    vs = values(node, name)
    if len(vs) >= 2 and isinstance(vs[0], float) and isinstance(vs[1], float):
        return float(vs[0]), float(vs[1])
    return None


def _legacy_arc(center: tuple[float, float], start: tuple[float, float],
                angle_deg: float) -> list[tuple[float, float]]:
    """A KiCad 5 arc: ``(start <centre>) (end <start point>) (angle <sweep>)``.

    Same keywords as the three-point form KiCad 6 introduced, different meanings, so a
    KiCad 5 arc with no ``mid`` was dropped and the outline had a gap at every rounded
    corner. A positive sweep turns from +X toward +Y in the file's own Y-down frame, which
    is what KiCad 10 produces when it upgrades one of these files.
    """
    r = math.dist(center, start)
    if r <= 0:
        return [start]
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    sweep = math.radians(angle_deg)
    n = max(2, int(abs(angle_deg) / 5) + 1)
    return [
        (center[0] + r * math.cos(a0 + sweep * i / n), center[1] + r * math.sin(a0 + sweep * i / n))
        for i in range(n + 1)
    ]


def _bezier(pts: list[tuple[float, float]], n: int = 24) -> list[tuple[float, float]]:
    """A cubic Bezier (``gr_curve``), sampled. Was ignored, leaving a gap in the outline."""
    if len(pts) != 4:
        return pts
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((
            u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
            u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
        ))
    return out


def _edge_shape(node: Node, kind: str) -> list[tuple[float, float]] | None:
    """One Edge.Cuts graphic as a polyline, in the coordinates it is written in.

    ``kind`` is the name without its ``gr_`` or ``fp_`` prefix: the board and a footprint
    use the same shapes, and a footprint's are only ever offset and rotated.
    """
    if kind == "line":
        s, e = _xy(node, "start"), _xy(node, "end")
        return [s, e] if s and e else None
    if kind == "rect":
        s, e = _xy(node, "start"), _xy(node, "end")
        if not (s and e):
            return None
        (x0, y0), (x1, y1) = s, e
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    if kind == "arc":
        s, m, e = _xy(node, "start"), _xy(node, "mid"), _xy(node, "end")
        if s and m and e:
            return g.arc_polyline(s, m, e)
        angle = value(node, "angle")
        if s and e and isinstance(angle, float):
            return _legacy_arc(s, e, angle)
        return None
    if kind == "circle":
        c, e = _xy(node, "center"), _xy(node, "end")
        if not (c and e):
            return None
        ring = g.circle(c[0], c[1], math.dist(c, e))
        return ring + [ring[0]]
    if kind == "poly":
        pts = points(node)
        return pts + [pts[0]] if len(pts) >= 3 else None
    if kind == "curve":
        pts = points(node)
        return _bezier(pts) if len(pts) == 4 else None
    return None


def _parse_outline(root: Node, warnings: list[str]) -> list[list[tuple[float, float]]]:
    """Collect Edge.Cuts graphics as polylines, from the board and from its footprints.

    A footprint can carry its own Edge.Cuts: a slot for a connector, a cut-out for a
    display. Those were skipped, so the board looked solid where it has a hole.
    """
    out: list[list[tuple[float, float]]] = []

    for node in root:
        if not isinstance(node, list):
            continue
        kind = sym(node)
        if not kind.startswith("gr_") or text(node, "layer", default="") != "Edge.Cuts":
            continue
        shape = _edge_shape(node, kind[3:])
        if shape:
            out.append(shape)

    for fp in _footprints(root):
        fx, fy, frot = _at(fp)
        for node in fp:
            if not isinstance(node, list):
                continue
            kind = sym(node)
            if not kind.startswith("fp_") or text(node, "layer", default="") != "Edge.Cuts":
                continue
            shape = _edge_shape(node, kind[3:])
            if not shape:
                continue
            # Footprint graphics are stored in the footprint's own frame, before rotation.
            placed = []
            for x, y in shape:
                rx, ry = g.rotate(x, y, frot)
                placed.append((fx + rx, fy + ry))
            out.append(placed)

    if not out:
        warnings.append(
            "no Edge.Cuts outline found; the board extent was inferred from the copper"
        )
    return out


def parse_board(root: Node) -> BoardModel:
    """Build a BoardModel from a parsed ``.kicad_pcb`` tree."""
    if sym(root) != "kicad_pcb":
        raise ValueError("not a KiCad board file (expected a top-level 'kicad_pcb')")

    warnings: list[str] = []
    nets = _NetResolver(root)

    model = BoardModel(
        version=int(number(root, "version", default=0)),
        generator=text(root, "generator", default=""),
        warnings=warnings,
    )

    general = child(root, "general")
    if general is not None:
        model.thickness_mm = number(general, "thickness", default=1.6)

    model.copper_layers = _parse_layers(root)
    if not model.copper_layers:
        raise ValueError("board file declares no copper layers")

    model.stackup = _parse_stackup(root, model.copper_layers, warnings)

    # Tracks: straight segments and arcs share a representation.
    for seg in children(root, "segment"):
        s, e = values(seg, "start"), values(seg, "end")
        if len(s) < 2 or len(e) < 2:
            continue
        model.tracks.append(Track(
            layer=text(seg, "layer", default=""),
            net=nets.resolve(seg),
            width_mm=number(seg, "width", default=0.0),
            pts=[(float(s[0]), float(s[1])), (float(e[0]), float(e[1]))],
        ))

    for arc in children(root, "arc"):
        s, m, e = values(arc, "start"), values(arc, "mid"), values(arc, "end")
        if len(s) < 2 or len(m) < 2 or len(e) < 2:
            continue
        model.tracks.append(Track(
            layer=text(arc, "layer", default=""),
            net=nets.resolve(arc),
            width_mm=number(arc, "width", default=0.0),
            pts=g.arc_polyline(
                (float(s[0]), float(s[1])),
                (float(m[0]), float(m[1])),
                (float(e[0]), float(e[1])),
            ),
            is_arc=True,
        ))

    for via in children(root, "via"):
        at = values(via, "at")
        if len(at) < 2:
            continue
        kind = "through"
        if flag(via, "micro"):
            kind = "micro"
        elif flag(via, "blind"):
            kind = "blind"
        model.vias.append(Via(
            x=float(at[0]), y=float(at[1]),
            size_mm=number(via, "size", default=0.0),
            drill_mm=number(via, "drill", default=0.0),
            layers=_layer_list(via),
            net=nets.resolve(via),
            kind=kind,
        ))

    model.pads = _parse_footprints(root, nets, warnings)

    unfilled = 0
    for zone in children(root, "zone"):
        # A keepout zone has no copper; including it would invent a plane that is not there.
        if child(zone, "keepout") is not None:
            continue
        net = nets.resolve(zone)
        fills = list(children(zone, "filled_polygon"))
        for filled in fills:
            # KiCad 5 wrote no layer on the fill; it is the zone's own single layer.
            layer = text(filled, "layer", default="") or text(zone, "layer", default="")
            pts = points(filled)
            if len(pts) >= 3:
                model.zones.append(ZonePolygon(layer=layer, net=net, ring=pts))
        fill = child(zone, "fill")
        filled_flag = fill is not None and len(fill) > 1 and fill[1] == "yes"
        if not fills and not filled_flag and child(zone, "polygon") is not None:
            unfilled += 1

    if unfilled:
        # Only the fill is copper. A zone saved before it was ever filled has an outline and
        # nothing else, and without this note every plane check silently switched off: no
        # plane, so no reference-plane findings, and nothing saying why.
        #
        # The outline is deliberately NOT used as a stand-in. It covers every clearance,
        # anti-pad and split the fill would cut, so the plane checks would see solid copper
        # under traces that actually cross gaps, and pass boards they should flag. Missing
        # checks with a note beat checks that are quietly optimistic.
        warnings.append(ZONES_UNFILLED_NOTE.format(n=unfilled, s="" if unfilled == 1 else "s"))

    model.outline = _parse_outline(root, warnings)
    model.nets = nets.order
    return model
