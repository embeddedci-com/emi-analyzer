"""The decoupling view: per supply rail and IC, what the capacitors filter and what they don't.

The decoupling check says "C12 is 8 mm from U3.4". That is true and not enough: whether it
matters depends on what else is on the rail, and whether a 10 nF beside the 100 nF helps or
makes an anti-resonance. This builds the curve that answers it, per IC, from the layout alone:

  * every capacitor on the rail as a series R-L-C (the part from the component library, the
    loop from the pads, vias and stackup; see pdn.py for the formulas and their sources),
  * the plane pair's own capacitance where the rail has a plane,
  * a target impedance from the rail voltage, an allowed ripple and a current step,
  * where the combination is above the target, and a few changes ranked by how much they
    close the worst gap, each one computed with the same model rather than suggested.

It is a lumped estimate and says so. It runs at ingest, in milliseconds per IC, because every
what-if is one admittance swapped in a precomputed sum rather than a new network.

The output goes into rules.json as ``decoupling``. Every curve can be rebuilt from the
branches it carries, which is what the browser does: the target is editable there, and the
ranking of the recommendations follows it without another analysis.
"""

from __future__ import annotations

import functools
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from ..kicad.board import BoardModel, Pad
from ..kicad.geometry import ring_area
from . import pdn
from .decoupling import CAP_RE, IC_RE, cap_farads
from .model import RuleContext, classify_net
from .planes import plane_layers

log = logging.getLogger(__name__)

FORMAT_VERSION = 1

#: Used when the net name carries no voltage. 3.3 V is the commonest logic rail; it is labeled
#: assumed wherever it is shown, and a per-net setting replaces it.
ASSUMED_RAIL_V = 3.3

#: Enough for any real IC; the nearest are kept. Every capacitor on the rail counts, however far:
#: a first version kept only those within 25 mm, and every rail whose bulk capacitor sat at the
#: regulator reported a gap at 100 kHz that the bulk capacitor fills. Far away it is behind tens
#: of nanohenries, which is nothing at 100 kHz and is exactly what the curve shows above it.
MAX_CAPS_PER_IC = 40

#: When nothing is known about a part: its value is 100 nF and its own inductance is that of
#: a small MLCC. Both are shown as assumed. Same figures as the text check (decoupling.py).
ASSUMED_CAP_F = 100e-9
ASSUMED_ESL_H = 0.5e-9
ASSUMED_VIA_RADIUS_MM = 0.15
ASSUMED_EPS_R = 4.4
ASSUMED_LOSS_TANGENT = 0.02

#: Center-to-center pad pitch per package, for the capacitor's own length in the loop.
PAD_PITCH_MM = {"0201": 0.5, "0402": 1.0, "0603": 1.6, "0805": 2.0, "1206": 3.2, "1210": 3.2,
                "1812": 4.5, "2220": 5.7}
DEFAULT_PITCH_MM = 1.6

#: What an added capacitor can be. E3 values in the packages people actually place.
ADD_VALUES_F = (1e-9, 2.2e-9, 4.7e-9, 10e-9, 22e-9, 47e-9, 100e-9, 220e-9, 470e-9,
                1e-6, 2.2e-6, 4.7e-6, 10e-6, 22e-6)
ADD_PACKAGES = ("0402", "0603", "0805")
#: Where an added part is assumed to go: one pad pitch plus half a millimetre to its via.
ADD_VIA_MM = 0.5
MAX_RECOMMENDATIONS = 4

#: Where the board stops being the answer. Above roughly 100 MHz an IC's supply current is
#: served by its package and die capacitance, and no capacitor on the board can reach it
#: through the few nanohenries of any real mounting: 1 nH is 0.6 ohm at 100 MHz. Judging the
#: target above that would put every IC ever made in a gap up to 1 GHz and rank what-ifs by
#: how they move a number nothing on the board can fix. Settable per rail.
BOARD_MAX_HZ = 100e6

_FREQ_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kmg]?)\s*hz", re.I)
_FREQ_SCALE = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}
#: Crystals and oscillators. Their Value is the frequency ("16MHz", "32.768kHz").
CLOCK_PART_RE = re.compile(r"^(Y|X|XTAL|OSC|XO)\d", re.I)


def rail_voltage(net: str) -> float | None:
    """"+3V3" -> 3.3, "VCC_1V8" -> 1.8, "+5V" -> 5.0, "VDD" -> None."""
    leaf = net.lower().rstrip("/").rsplit("/", 1)[-1]
    for w in re.split(r"[^a-z0-9.]+", leaf):
        m = re.fullmatch(r"(\d+)v(\d+)", w)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        m = re.fullmatch(r"(\d+(?:\.\d+)?)v", w)
        if m:
            return float(m.group(1))
    return None


def parse_hz(text: str) -> float | None:
    """"16MHz", "32.768 kHz", "CLK_25MHZ" -> hertz. Only with an explicit Hz: "16M" on a
    resistor or "8M" in a net name mean too many other things to guess at."""
    m = _FREQ_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ".")) * _FREQ_SCALE[m.group(2).lower()]
    except ValueError:
        return None


# ---- the stackup, as heights --------------------------------------------------------------

@dataclass
class Heights:
    z: dict[str, float]
    eps_between: dict[tuple[str, str], tuple[float, float]]
    assumed: bool

    def gap(self, a: str, b: str) -> float:
        return abs(self.z.get(a, 0.0) - self.z.get(b, 0.0))

    def dielectric(self, a: str, b: str) -> tuple[float, float, bool]:
        """(eps_r, loss tangent, assumed) of what lies between two copper layers."""
        got = self.eps_between.get((a, b)) or self.eps_between.get((b, a))
        if got and got[0] > 0:
            return got[0], (got[1] if got[1] > 0 else ASSUMED_LOSS_TANGENT), False
        return ASSUMED_EPS_R, ASSUMED_LOSS_TANGENT, True


def heights(model: BoardModel) -> Heights:
    """The depth of each copper layer's center from the top, and the dielectric between each
    pair. From the board's stackup when it has one; evenly spaced through the board's
    thickness otherwise, and marked assumed."""
    coppers = [c.name for c in model.copper_layers]
    entries = [s for s in model.stackup if s.is_copper or s.is_dielectric]
    if entries and all(any(e.name == c for e in entries) for c in coppers):
        z: dict[str, float] = {}
        depth = 0.0
        for e in entries:
            if e.is_copper:
                z[e.name] = depth + e.thickness_mm / 2.0
            depth += e.thickness_mm
        eps: dict[tuple[str, str], tuple[float, float]] = {}
        names = [e for e in entries]
        for i, a in enumerate(names):
            if not a.is_copper:
                continue
            for b in names[i + 1:]:
                if b.is_copper:
                    between = [d for d in names[names.index(a) + 1:names.index(b)] if d.is_dielectric]
                    with_eps = [d for d in between if d.epsilon_r > 0]
                    if with_eps:
                        eps[(a.name, b.name)] = (with_eps[0].epsilon_r, with_eps[0].loss_tangent)
        return Heights(z=z, eps_between=eps, assumed=False)
    n = max(len(coppers), 1)
    step = (model.thickness_mm or 1.6) / max(n - 1, 1)
    return Heights(z={c: i * step for i, c in enumerate(coppers)}, eps_between={}, assumed=True)


def _pad_layer(p: Pad, coppers: list[str]) -> str:
    for layer in p.layers:
        if layer in coppers:
            return layer
    if any(layer.startswith("B.") for layer in p.layers):
        return coppers[-1] if coppers else "B.Cu"
    return coppers[0] if coppers else "F.Cu"


# ---- parts ---------------------------------------------------------------------------------

@dataclass
class CapPart:
    ref: str
    supply: Pad
    ground: Pad
    value: str
    c_f: float
    esl_h: float
    esr_ohm: float
    package: str
    model_name: str
    source: str  # library | assumed
    assumed_what: list[str]
    #: "datasheet (Samsung CL05B104KO5NNNC)" or "generic 0402 X7R"; empty when assumed.
    basis: str = ""
    matched_by: str = ""
    matched_on: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def pitch_mm(self) -> float:
        return math.dist((self.supply.x, self.supply.y), (self.ground.x, self.ground.y))


def _library(ref: str, value: str, footprint: str, part_numbers: dict[str, str] | None = None):
    from emi_worker.components import match_part, resolve_part
    part = match_part(ref, value, footprint, part_numbers)
    return part, resolve_part(part)


def _family_esr(c_f: float) -> float:
    from emi_worker.components.resolve import built_in
    for cand in built_in():
        if cand.component.model_type == "mlcc_family":
            got = cand.component.mlcc_family().esr_for(c_f)
            if got is not None:
                return got
    return 0.05


def cap_part(ref: str, supply: Pad, ground: Pad) -> CapPart:
    value = supply.value or ground.value
    footprint = supply.footprint or ground.footprint
    part, resolved = _library(ref, value, footprint,
                              getattr(supply, "part_numbers", None)
                              or getattr(ground, "part_numbers", None))
    package = part.package.imperial if part.package else ""
    if resolved is not None and resolved.rlc.esl_h is not None and resolved.rlc.esr_ohm is not None:
        return CapPart(ref=ref, supply=supply, ground=ground, value=value, c_f=resolved.rlc.c_f,
                       esl_h=resolved.rlc.esl_h, esr_ohm=resolved.rlc.esr_ohm, package=package,
                       model_name=resolved.component_name, source="library", assumed_what=[],
                       basis=resolved.basis, matched_by=resolved.matched_by,
                       matched_on=resolved.matched_on, notes=list(resolved.notes))
    c_f = cap_farads(value)
    assumed = []
    if c_f is None:
        c_f = ASSUMED_CAP_F
        assumed.append("value")
    assumed += ["ESL", "ESR"]
    return CapPart(ref=ref, supply=supply, ground=ground, value=value, c_f=c_f,
                   esl_h=ASSUMED_ESL_H, esr_ohm=_family_esr(c_f), package=package,
                   model_name="", source="assumed", assumed_what=assumed)


# ---- the builder ---------------------------------------------------------------------------

@dataclass
class _Geo:
    """What the layout gives every branch of one rail."""

    coppers: list[str]
    h: Heights
    ground_layers: list[str]
    #: Ground vias and through-hole ground pads as rows of (x, y, drill), and the rail's vias
    #: as (x, y). Arrays, because a busy board asks thousands of times.
    gnd_pts: np.ndarray
    rail_pts: np.ndarray
    plane: dict | None
    max_ground_via_mm: float

    def height_to_ground(self, layer: str) -> tuple[float, bool]:
        """Depth from a mounting layer to the nearest ground plane: the loop's height."""
        others = [g for g in self.ground_layers if g != layer]
        if layer in self.ground_layers:
            # A pour on the mounting layer itself. The loop still closes through the nearest
            # other plane when there is one; with none, the pour is the return and the loop
            # is as flat as the copper.
            if not others:
                return 0.1, True
        if not others:
            return self.h.gap(self.coppers[0], self.coppers[-1]) or 1.6, True
        return min(self.h.gap(layer, g) for g in others), self.h.assumed

    def ground_vias_near(self, p: Pad) -> tuple[float, int, float]:
        """(distance to the nearest ground via, vias within reach, via radius)."""
        if p.is_through:
            return 0.0, 1, max(p.drill_mm / 2.0, ASSUMED_VIA_RADIUS_MM)
        if not len(self.gnd_pts):
            return math.inf, 0, ASSUMED_VIA_RADIUS_MM
        ds = np.hypot(self.gnd_pts[:, 0] - p.x, self.gnd_pts[:, 1] - p.y)
        i = int(np.argmin(ds))
        near = int(np.count_nonzero(ds <= self.max_ground_via_mm))
        drill = float(self.gnd_pts[i, 2])
        radius = drill / 2.0 if drill > 0 else ASSUMED_VIA_RADIUS_MM
        return float(ds[i]), max(1, near), radius

    def rail_via_distance(self, p: Pad) -> float:
        if p.is_through:
            return 0.0
        if self.plane and self.plane["layer"] == _pad_layer(p, self.coppers):
            return 0.0
        if not len(self.rail_pts):
            return math.inf
        return float(np.min(np.hypot(self.rail_pts[:, 0] - p.x, self.rail_pts[:, 1] - p.y)))


def _rail_plane(model: BoardModel, rail: str, planes: dict[str, str], geo_h: Heights,
                coppers: list[str]) -> dict | None:
    """The plane pair this rail has, if it has one: its layer, the nearest ground plane, the
    cavity between them and what that makes."""
    rail_layers = [l for l, n in planes.items() if n == rail]
    grounds = [l for l, n in planes.items() if classify_net(n) == "ground"]
    if not rail_layers or not grounds:
        return None
    best = min(((geo_h.gap(r, g), r, g) for r in rail_layers for g in grounds if r != g),
               default=None)
    if best is None or best[0] <= 0:
        return None
    t, layer, ground = best
    eps_r, tand, eps_assumed = geo_h.dielectric(layer, ground)
    rings = [z.ring for z in model.zones if z.layer == layer and z.net == rail]
    area = sum(ring_area(r) for r in rings)
    if area <= 0:
        return None
    xs = [x for r in rings for x, _ in r]
    ys = [y for r in rings for _, y in r]
    longest = max(max(xs) - min(xs), max(ys) - min(ys))
    return {
        "layer": layer, "ground_layer": ground, "cavity_mm": round(t, 4),
        "area_mm2": round(area, 1), "eps_r": eps_r, "loss_tangent": tand,
        "assumed": bool(eps_assumed or geo_h.assumed),
        "c_f": pdn.plane_capacitance_f(area, t, eps_r),
        "resonance_hz": pdn.cavity_resonance_hz(longest, eps_r),
    }


def _branch_dict(b: pdn.Branch) -> dict:
    return {"id": b.id, "c_f": b.c_f, "l_h": b.l_h, "r_ohm": b.r_ohm, "kind": b.kind}


def _fmt_hz(f: float) -> str:
    if f >= 1e9:
        return f"{f / 1e9:.3g} GHz"
    if f >= 1e6:
        return f"{f / 1e6:.3g} MHz"
    return f"{f / 1e3:.3g} kHz"


def _fmt_f(c: float) -> str:
    for scale, unit in ((1e-6, "µF"), (1e-9, "nF"), (1e-12, "pF")):
        if c >= scale:
            return f"{c / scale:.3g} {unit}"
    return f"{c:.3g} F"


def _fixes(f: np.ndarray, before: np.ndarray, after: np.ndarray, target: float) -> list[list[float]]:
    fixed = (before > target) & (after <= target)
    out: list[list[float]] = []
    start = None
    for i, x in enumerate(fixed):
        if x and start is None:
            start = i
        if not x and start is not None:
            out.append([float(f[start]), float(f[i - 1])])
            start = None
    if start is not None:
        out.append([float(f[start]), float(f[-1])])
    return out


class _Entry:
    """One IC on one rail, and the arithmetic every what-if shares."""

    def __init__(self, f: np.ndarray, branches: list[pdn.Branch], series_l_h: float,
                 band_hz: float):
        self.f = f
        #: The target is judged up to here, and no further. See BOARD_MAX_HZ.
        self.kb = max(2, int(np.searchsorted(f, band_hz, side="right")))
        self.branches = branches
        self.series_l_h = series_l_h
        self.w = 2.0 * np.pi * f
        self.y = sum((1.0 / b.z(f) for b in branches), np.zeros_like(f, dtype=complex))

    def mag(self, y: np.ndarray) -> np.ndarray:
        return np.abs(1.0 / y + 1j * self.w * self.series_l_h)

    def swap(self, old: pdn.Branch | None, new: pdn.Branch | None) -> np.ndarray:
        y = self.y.copy()
        if old is not None:
            y = y - 1.0 / old.z(self.f)
        if new is not None:
            y = y + 1.0 / new.z(self.f)
        return self.mag(y)


def build(ctx: RuleContext) -> dict:
    """The ``decoupling`` section of rules.json."""
    model = ctx.model
    f = pdn.frequencies()
    coppers = [c.name for c in model.copper_layers]
    h = heights(model)
    planes = plane_layers(model)
    ground_layers = [l for l, n in planes.items() if classify_net(n) == "ground"]
    max_d = float(ctx.setting("decoupling", "max_distance_mm") or 3.0)
    via_reach = float(ctx.setting("decoupling", "max_ground_via_mm") or 1.0)
    board_clock = float(ctx.setting("decoupling", "clock_hz") or 0.0) or float(
        ctx.setting("cable-resonance", "clock_hz") or 0.0)

    by_ref: dict[str, list[Pad]] = defaultdict(list)
    for p in model.pads:
        if p.ref:
            by_ref[p.ref].append(p)

    caps_by_rail: dict[str, list[CapPart]] = defaultdict(list)
    for ref, pads in by_ref.items():
        if not CAP_RE.match(ref) or len(pads) != 2:
            continue
        kinds = [classify_net(p.net) if p.net else "" for p in pads]
        if sorted(kinds) != ["ground", "power"]:
            continue
        s, g = pads[kinds.index("power")], pads[kinds.index("ground")]
        caps_by_rail[s.net].append(cap_part(ref, s, g))

    # Crystals and oscillators by net, for the noise each IC is likely to make.
    clocks_by_net: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for ref, pads in by_ref.items():
        if CLOCK_PART_RE.match(ref):
            hz = parse_hz(pads[0].value)
            if hz:
                for p in pads:
                    if p.net and classify_net(p.net) == "signal":
                        clocks_by_net[p.net].append((hz, f"{ref} {pads[0].value}"))

    gnd_pts = np.array(
        [(v.x, v.y, v.drill_mm) for v in model.vias if v.net and classify_net(v.net) == "ground"]
        + [(p.x, p.y, p.drill_mm) for p in model.pads
           if p.net and classify_net(p.net) == "ground" and p.is_through],
        dtype=float).reshape(-1, 3)

    rails: dict[str, list[dict]] = defaultdict(list)
    rail_parts: dict[str, dict] = defaultdict(dict)
    rail_info: dict[str, dict] = {}
    for ref, pads in sorted(by_ref.items()):
        if not IC_RE.match(ref):
            continue
        pins: dict[str, list[Pad]] = defaultdict(list)
        for p in pads:
            if p.net and classify_net(p.net) == "power":
                pins[p.net].append(p)
        ic_nets = {p.net for p in pads if p.net}
        for rail, supply_pins in sorted(pins.items()):
            if rail not in rail_info:
                plane = _rail_plane(model, rail, planes, h, coppers)
                v = rail_voltage(rail)
                rail_info[rail] = {
                    "plane": plane, "v": v if v is not None else ASSUMED_RAIL_V,
                    "v_assumed": v is None,
                    "rail_pts": np.array([(x.x, x.y) for x in model.vias if x.net == rail],
                                         dtype=float).reshape(-1, 2),
                }
            info = rail_info[rail]
            geo = _Geo(coppers=coppers, h=h, ground_layers=ground_layers, gnd_pts=gnd_pts,
                       rail_pts=info["rail_pts"], plane=info["plane"],
                       max_ground_via_mm=via_reach)
            entry = _ic_entry(ctx, f, ref, rail, supply_pins, caps_by_rail.get(rail, []), geo,
                              info, max_d, board_clock, clocks_by_net, ic_nets,
                              rail_parts[rail])
            rails[rail].append(entry)

    out_rails = []
    for rail, ics in sorted(rails.items()):
        info = rail_info[rail]
        plane = info["plane"]
        out_rails.append({
            "net": rail, "v": info["v"], "v_assumed": info["v_assumed"],
            "plane": None if plane is None else {
                k: (round(v, 6) if isinstance(v, float) and k not in ("c_f", "resonance_hz") else v)
                for k, v in plane.items()
            },
            "status": "gaps" if any(e["status"] == "gaps" for e in ics) else "ok",
            "parts": rail_parts[rail],
            "ics": ics,
        })

    resonances = [r["plane"]["resonance_hz"] for r in out_rails if r["plane"]]
    note = "Estimate: lumped model from the layout."
    if resonances:
        note += f" No plane resonances; the first is near {_fmt_hz(min(resonances))}."
    else:
        note += " No power plane pair, so no plane capacitance or resonances."
    return _rounded({
        "format_version": FORMAT_VERSION,
        "f_min_hz": pdn.F_MIN_HZ, "f_max_hz": pdn.F_MAX_HZ,
        "points_per_decade": pdn.POINTS_PER_DECADE,
        "stackup_assumed": h.assumed,
        "note": note,
        "rails": out_rails,
    })


def _rounded(v):
    """Four significant figures everywhere. The model is good to tens of percent; seventeen
    digits of it tripled the size of rules.json on a busy board and said nothing more."""
    if isinstance(v, float):
        return float(f"{v:.4g}") if math.isfinite(v) else v
    if isinstance(v, dict):
        return {k: _rounded(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_rounded(x) for x in v]
    if isinstance(v, np.floating):
        return _rounded(float(v))
    return v


def _ic_entry(ctx: RuleContext, f: np.ndarray, ref: str, rail: str, supply_pins: list[Pad],
              caps: list[CapPart], geo: _Geo, info: dict, max_d: float, board_clock: float,
              clocks_by_net: dict, ic_nets: set[str], parts_seen: dict) -> dict:
    plane = info["plane"]
    ic_layer = _pad_layer(supply_pins[0], geo.coppers)
    cx = sum(p.x for p in supply_pins) / len(supply_pins)
    cy = sum(p.y for p in supply_pins) / len(supply_pins)

    def nearest_pin(c: CapPart) -> tuple[Pad, float]:
        pin = min(supply_pins, key=lambda p: math.dist((p.x, p.y), (c.supply.x, c.supply.y)))
        return pin, math.dist((pin.x, pin.y), (c.supply.x, c.supply.y))

    ranked = sorted(((nearest_pin(c), c) for c in caps), key=lambda t: t[0][1])[:MAX_CAPS_PER_IC]

    assumed_notes: set[str] = set()

    def mount(c: CapPart, distance: float, ground_vias: int | None = None,
              via_mm: float | None = None, rail_via_mm: float | None = None) -> tuple[float, dict]:
        """The layout's inductance in series with this part, and what it was made of."""
        layer = _pad_layer(c.supply, geo.coppers)
        g, n, r = geo.ground_vias_near(c.ground)
        if via_mm is not None:
            g, n = via_mm, 1
        if ground_vias is not None:
            n = ground_vias
        if math.isinf(g):
            # No ground via anywhere: the pad reaches ground through the pour it sits in, or
            # through a trace this model cannot follow. One pitch is the least it can be.
            g, n = c.pitch_mm or DEFAULT_PITCH_MM, 1
            assumed_notes.add("ground connection")
        pitch = c.pitch_mm or PAD_PITCH_MM.get(c.package, DEFAULT_PITCH_MM)
        if plane is not None:
            hh = min(geo.h.gap(layer, plane["layer"]), geo.h.gap(layer, plane["ground_layer"]))
            if layer in (plane["layer"], plane["ground_layer"]):
                hh = max(hh, plane["cavity_mm"])
            p = geo.rail_via_distance(c.supply) if rail_via_mm is None else rail_via_mm
            if math.isinf(p):
                # No via to the power plane near the pad: it is fed by a trace, so the trace
                # is in the loop. The distance to the pin is the most it can be.
                p = distance
            loop = p + pitch + g
            conn = pdn.connection_nh(loop, hh, r, n)
            spread = pdn.spreading_nh(plane["cavity_mm"], distance, r)
            return (conn + spread) * 1e-9, {
                "loop_mm": round(loop, 2), "height_mm": round(hh, 3), "ground_vias": n,
                "ground_via_mm": round(g, 2), "spreading_nh": round(spread, 3),
                "connection_nh": round(conn, 3),
            }
        hh, h_assumed = geo.height_to_ground(layer)
        if h_assumed:
            assumed_notes.add("height to ground")
        loop = distance + pitch + g
        conn = pdn.connection_nh(loop, hh, r, n)
        return conn * 1e-9, {
            "loop_mm": round(loop, 2), "height_mm": round(hh, 3), "ground_vias": n,
            "ground_via_mm": round(g, 2), "spreading_nh": 0.0, "connection_nh": round(conn, 3),
        }

    branches: list[pdn.Branch] = []
    cap_rows: list[dict] = []
    for (pin, d), c in ranked:
        l_mount, parts = mount(c, d)
        b = pdn.Branch(id=c.ref, c_f=c.c_f, l_h=c.esl_h + l_mount, r_ohm=c.esr_ohm,
                       esl_h=c.esl_h)
        branches.append(b)
        x, y = ctx.pt(c.supply.x, c.supply.y)
        cap_rows.append({
            "ref": c.ref, "pin": pin.number, "mount_l_h": l_mount, "l_h": b.l_h,
            "distance_mm": round(d, 2), "useful_up_to_hz": b.srf_hz, **parts,
        })
        # What the part is does not depend on which IC is asking, so it is listed once per
        # rail: a rail with 28 ICs and 32 capacitors repeated it 900 times.
        parts_seen.setdefault(c.ref, {
            "value": c.value, "package": c.package, "c_f": c.c_f, "esl_h": c.esl_h,
            "esr_ohm": c.esr_ohm, "source": c.source, "model": c.model_name,
            "basis": c.basis, "matched_by": c.matched_by, "matched_on": c.matched_on,
            "notes": c.notes,
            "assumed": c.assumed_what, "x": round(x, 4), "y": round(y, 4),
        })

    series_l = 0.0
    if plane is not None:
        # The IC's own way down to the plane pair: its supply pin to the nearest rail via and
        # back up through a ground via beside it. Common to every capacitor.
        pin = supply_pins[0]
        p = geo.rail_via_distance(pin)
        p = 0.5 if math.isinf(p) else p
        hh = min(geo.h.gap(ic_layer, plane["layer"]), geo.h.gap(ic_layer, plane["ground_layer"]))
        hh = max(hh, 0.05)
        series_l = pdn.connection_nh(p + 1.0, hh, ASSUMED_VIA_RADIUS_MM) * 1e-9
        lp = pdn.plane_branch_nh(plane["area_mm2"], plane["cavity_mm"], ASSUMED_VIA_RADIUS_MM) * 1e-9
        lp = max(lp, 1e-13)
        # Dielectric loss as a series resistance, valued at the branch's own resonance, where
        # it is the only thing limiting the dip.
        w0 = 1.0 / math.sqrt(lp * plane["c_f"])
        rp = plane["loss_tangent"] / (w0 * plane["c_f"])
        branches.append(pdn.Branch(id="plane", c_f=plane["c_f"], l_h=lp, r_ohm=rp, kind="plane"))

    ripple = float(ctx.setting("decoupling", "ripple_pct", net=rail) or 5.0)
    step = float(ctx.setting("decoupling", "step_current_a", net=rail) or 0.5)
    if ripple <= 0 or step <= 0:
        ripple, step = 5.0, 0.5
    target = info["v"] * ripple / 100.0 / step
    band = float(ctx.setting("decoupling", "board_max_hz", net=rail) or BOARD_MAX_HZ)
    x, y = ctx.pt(cx, cy)
    entry: dict = {
        "ref": ref, "pins": sorted({p.number for p in supply_pins}), "x": round(x, 4),
        "y": round(y, 4), "ripple_pct": ripple, "step_current_a": step,
        "target_ohm": target, "band_hz": band, "series_l_h": series_l,
        # The capacitors' rows carry their own branch (c_f, l_h, esr_ohm); only the plane's is
        # separate. Listing every branch twice made rules.json half again as large.
        "plane_branch": next((_branch_dict(b) for b in branches if b.kind == "plane"), None),
        "caps": cap_rows,
        "assumed": sorted(assumed_notes),
        "noise": _noise(ctx, rail, board_clock, clocks_by_net, ic_nets),
        "recommendations": [],
    }
    if not branches:
        entry.update(status="gaps", gaps=[[pdn.F_MIN_HZ, pdn.F_MAX_HZ]], anti_resonances=[],
                     worst=None)
        return entry

    e = _Entry(f, branches, series_l, band)
    before = e.mag(e.y)
    net = pdn.Network(branches, series_l)
    peaks = pdn.anti_resonances(net, f)
    excess, wi = pdn.worst_excess_db(before[:e.kb], target)
    entry["anti_resonances"] = [{"hz": pf, "ohm": pz} for pf, pz in peaks]
    entry["gaps"] = [list(g) for g in pdn.gaps(f[:e.kb], before[:e.kb], target)]
    entry["status"] = "gaps" if entry["gaps"] else "ok"
    entry["worst"] = {"hz": float(f[wi]), "ohm": float(before[wi]), "excess_db": excess}
    if entry["status"] == "gaps":
        entry["recommendations"] = _recommend(e, before, target, ranked, branches, mount, ref,
                                              max_d, geo.max_ground_via_mm)
    return entry


def _noise(ctx: RuleContext, rail: str, board_clock: float, clocks_by_net: dict,
           ic_nets: set[str]) -> list[dict]:
    out: dict[float, str] = {}
    for net in sorted(ic_nets):
        for hz, label in clocks_by_net.get(net, []):
            out.setdefault(hz, label)
        if classify_net(net) == "signal":
            hz = parse_hz(net)
            if hz:
                out.setdefault(hz, f"net {net}")
    if board_clock > 0:
        out.setdefault(board_clock, "clock (settings)")
    sw = float(ctx.setting("decoupling", "switching_hz", net=rail) or 0.0)
    if sw > 0:
        out.setdefault(sw, "regulator (settings)")
    return [{"hz": hz, "source": label} for hz, label in sorted(out.items())]


def _recommend(e: _Entry, before: np.ndarray, target: float, ranked, branches, mount, ic: str,
               max_d: float, via_reach: float) -> list[dict]:
    f = e.f[:e.kb]
    before = before[:e.kb]
    excess0, wi = pdn.worst_excess_db(before, target)
    by_id = {b.id: b for b in branches}
    cands: list[tuple[str, str, dict, np.ndarray]] = []

    for (pin, d), c in ranked:
        old = by_id[c.ref]
        if d > max_d:
            l_new, _ = mount(c, max_d)
            new = pdn.Branch(id=c.ref, c_f=c.c_f, l_h=c.esl_h + l_new, r_ohm=c.esr_ohm)
            cands.append(("move", f"Move {c.ref} to within {max_d:g} mm of {ic}.{pin.number}",
                          {"op": "replace", "branch": _branch_dict(new)}, e.swap(old, new)[:e.kb]))
        l_now, parts = mount(c, d)
        if parts["ground_vias"] < 4 and not c.ground.is_through:
            if parts["ground_via_mm"] <= via_reach:
                text = f"Add a second ground via to {c.ref}"
                l_new, _ = mount(c, d, ground_vias=parts["ground_vias"] + 1)
            else:
                text = f"Add a ground via at {c.ref}'s ground pad"
                l_new, _ = mount(c, d, ground_vias=1, via_mm=0.3)
            if l_new < l_now * 0.98:
                new = pdn.Branch(id=c.ref, c_f=c.c_f, l_h=c.esl_h + l_new, r_ohm=c.esr_ohm)
                cands.append(("via", text, {"op": "replace", "branch": _branch_dict(new)},
                              e.swap(old, new)[:e.kb]))
        if len(branches) > 1:
            cands.append(("remove", f"Remove {c.ref} ({_fmt_f(c.c_f)})",
                          {"op": "remove", "id": c.ref}, e.swap(old, None)[:e.kb]))

    # One added part: every value and package the library knows, placed at the distance the
    # check asks for, and the one that closes the most of the worst gap wins.
    best_add = None
    for pkg in ADD_PACKAGES:
        for c_f in ADD_VALUES_F:
            part = _candidate(c_f, pkg)
            if part.source != "library":
                continue
            # At the distance the check asks for, with one ground via and one supply via
            # half a millimetre from its pads: the same formula as every existing part.
            conn, _ = mount(part, max_d, ground_vias=1, via_mm=ADD_VIA_MM, rail_via_mm=ADD_VIA_MM)
            new = pdn.Branch(id="new", c_f=c_f, l_h=part.esl_h + conn, r_ohm=part.esr_ohm)
            after = e.swap(None, new)[:e.kb]
            score = excess0 - pdn.worst_excess_db(after, target)[0]
            if best_add is None or score > best_add[0] + 1e-9:
                best_add = (score, pkg, c_f, new, after)
    if best_add is not None:
        _, pkg, c_f, new, after = best_add
        cands.append(("add", f"Add {_fmt_f(c_f)} {pkg} within {max_d:g} mm of {ic} "
                             f"(self-resonance {_fmt_hz(new.srf_hz)})",
                      {"op": "add", "branch": _branch_dict(new)}, after))

    scored = []
    for kind, text, change, after in cands:
        improvement = 20.0 * math.log10(before[wi] / after[wi])
        new_excess = pdn.worst_excess_db(after, target)[0]
        reduction = excess0 - max(new_excess, 0.0)
        fixes = _fixes(f, before, after, target)
        # A change that does nothing at the worst frequency can still close a gap elsewhere:
        # moving a capacitor never helps at 100 kHz, where only more capacitance does, and
        # hiding it would leave a rail with a 100 kHz gap and a 50 MHz one showing a fix for
        # only the first. It ranks after everything that helps the worst point.
        fixed_decades = sum(math.log10(hi / lo) for lo, hi in fixes) if fixes else 0.0
        if not ((improvement > 0.1 and reduction > 0.1) or fixed_decades > 0):
            continue
        scored.append({
            "kind": kind, "text": text, "change": change,
            "improvement_db": round(improvement, 2), "at_hz": float(f[wi]),
            "worst_after_db": round(new_excess, 2),
            "fixes": fixes,
            "_rank": (max(reduction, 0.0), max(improvement, 0.0), fixed_decades),
        })
    scored.sort(key=lambda r: r["_rank"], reverse=True)
    for r in scored:
        r.pop("_rank")
    return scored[:MAX_RECOMMENDATIONS]


@functools.lru_cache(maxsize=None)
def _candidate(c_f: float, pkg: str) -> CapPart:
    """A part that could be added, resolved once: the library lookup is most of the cost of a
    recommendation, and the same 42 candidates are asked about for every IC."""
    value = _fmt_f(c_f).replace(" ", "")
    return cap_part("Cnew", _fake_pad(value, pkg, "+"), _fake_pad(value, pkg, "-"))


def _fake_pad(value: str, pkg: str, side: str) -> Pad:
    from ..components.match import IMPERIAL_TO_METRIC
    fp = f"Capacitor_SMD:C_{pkg}_{IMPERIAL_TO_METRIC[pkg]}Metric"
    return Pad(ref="Cnew", number="1" if side == "+" else "2", net="", layers=["F.Cu"],
               x=0.0 if side == "+" else PAD_PITCH_MM.get(pkg, DEFAULT_PITCH_MM), y=0.0,
               ring=[], value=value, footprint=fp)
