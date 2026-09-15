"""Electrical models of the parts on a protected line: the clamp and the IC pin.

Clamps get a generic model built from numbers every protection datasheet states -- breakdown
voltage, clamping voltage at a rated current, capacitance -- in one of four shapes. That is
deliberately less than a vendor model and deliberately enough: at the currents of a discharge a
clamp is a breakdown knee and a slope, and the slope is what the datasheet's clamping voltage
measures. A vendor model, when one is uploaded and passes validation, replaces it.

The IC pin is the largest unknown and is modelled generically: capacitance, a series
resistance, and a diode to each rail, with the supply as a capacitor behind some inductance.
Results therefore compare layouts; they do not predict whether a particular IC survives.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import parts_table

#: Thermal voltage at room temperature.
VT = 0.02585
#: A silicon junction conducting amps in the forward direction, for fitting clamp slopes.
FORWARD_DROP_V = 0.85

#: Generic CMOS input.
PIN_C_PF = 5.0
PIN_R_OHM = 1.0
PIN_DIODE_MODEL = "EMI_DPIN"
#: A supply as the pin sees it through a nanosecond: its decoupling capacitor, that capacitor's
#: mounting inductance, and the inductance of the rail back to the regulator.
SUPPLY_L_NH = 5.0
DECOUPLING_NF = 100.0
DECOUPLING_ESL_NH = 1.0

TOPOLOGIES = ("tvs-uni", "tvs-bi", "steering-array", "rail-clamp")


@dataclass
class ClampSpec:
    part: str
    topology: str
    v_br_v: float | None = None
    i_t_a: float | None = None
    r_dyn_ohm: float | None = None
    c_j_pf: float | None = None
    v_f_points: list[tuple[float, float]] = field(default_factory=list)
    v_c_points: list[tuple[float, float, str]] = field(default_factory=list)
    pins: dict[str, str] = field(default_factory=dict)
    feedthrough: dict[str, str] = field(default_factory=dict)
    source: str = ""
    assumptions: list[str] = field(default_factory=list)
    #: Not a datasheet part: an illustrative clamp for "what if there were one".
    illustrative: bool = False


def _norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9.]", "", (value or "").upper())


def _from_entry(part: str, e: dict) -> ClampSpec:
    spec = ClampSpec(
        part=part,
        topology=e["topology"],
        v_br_v=e.get("v_br_v"),
        i_t_a=(e["i_t_ma"] / 1000.0) if e.get("i_t_ma") else None,
        r_dyn_ohm=e.get("r_dyn_ohm"),
        c_j_pf=e.get("c_j_pf"),
        v_f_points=[(float(a), float(v)) for a, v, *_ in e.get("v_f_points", [])],
        v_c_points=[(float(a), float(v), str(c[0]) if c else "") for a, v, *c in e.get("v_c_points", [])],
        pins=dict(e.get("pins", {})),
        feedthrough=dict(e.get("feedthrough", {})),
        source=" ".join(x for x in (e.get("manufacturer", ""), part, "datasheet",
                                    e.get("revision", ""), e.get("datasheet", "")) if x),
    )
    if spec.r_dyn_ohm is None and spec.v_br_v and spec.v_c_points:
        # Fit the slope so the model passes through the datasheet's highest rated point. The
        # breakdown knee and, for parts that conduct through a second junction in the forward
        # direction (a steering diode, the other half of a bidirectional TVS), that junction's
        # drop are subtracted first; without them the model sits about 20 % low at rated current.
        amps, volts, conditions = max(spec.v_c_points)
        ibv = spec.i_t_a or 1e-3
        knee = VT * math.log(max(amps / ibv, 1.0))
        forward = FORWARD_DROP_V if spec.topology in ("steering-array", "tvs-bi") else 0.0
        r = (volts - spec.v_br_v - knee - forward) / amps if amps > 0 else 0.0
        if r > 0:
            spec.r_dyn_ohm = r
            spec.assumptions.append(
                f"dynamic resistance {r:.2f} Ω fitted to the datasheet's {volts:g} V at {amps:g} A"
                + (f" ({conditions})" if conditions else "")
            )
    return spec


def lookup(value: str) -> ClampSpec | None:
    """The table entry for a part value such as "USBLC6-2SC6_C2687116" or "PESD3V3L4UG,115"."""
    key = _norm(value)
    best: tuple[str, dict] | None = None
    for part, entry in parts_table.TABLE.items():
        k = _norm(part)
        if key.startswith(k) and (best is None or len(k) > len(_norm(best[0]))):
            best = (part, entry)
    return _from_entry(*best) if best else None


#: Used for a recognised clamp with no table entry, and as "a clamp at the connector" on an
#: unprotected line. Stated as illustrative everywhere it appears.
def illustrative_clamp(part: str = "") -> ClampSpec:
    reference = parts_table.TABLE.get("PESD5V0S1BA")
    if reference:
        spec = _from_entry("PESD5V0S1BA", reference)
        spec.assumptions.append("modelled as a PESD5V0S1BA-class clamp from its datasheet")
    else:
        spec = ClampSpec(part=part or "generic 5 V TVS", topology="tvs-uni", v_br_v=6.0,
                         i_t_a=1e-3, r_dyn_ohm=0.5, c_j_pf=20.0)
        spec.assumptions.append("an illustrative 5 V TVS (6 V breakdown, 0.5 Ω, 20 pF), not a real part")
    spec.illustrative = True
    return spec


def _num(x: float) -> str:
    return f"{x:.6g}"


def _schottky(points: list[tuple[float, float]]) -> tuple[float, float, list[str]]:
    """IS and RS for N=1 from forward-voltage points: the lowest current fixes IS, the highest RS."""
    notes: list[str] = []
    if not points:
        notes.append("no forward-voltage data; a small-signal Schottky is assumed")
        return 2e-7, 1.0, notes
    pts = sorted(points)
    i1, v1 = pts[0]
    is_ = i1 / math.exp(v1 / VT)
    rs = 1.0
    if len(pts) > 1:
        i2, v2 = pts[-1]
        rs = max((v2 - VT * math.log(i2 / is_)) / i2, 0.01)
    return is_, rs, notes


def clamp_subckt(spec: ClampSpec, name: str) -> tuple[str, list[str]]:
    """A generic subcircuit for a clamp. Returns (text, pin names)."""
    if spec.topology not in TOPOLOGIES:
        raise ValueError(f"unknown clamp topology {spec.topology!r}")
    bv = spec.v_br_v or 6.0
    ibv = spec.i_t_a or 1e-3
    rs = max(spec.r_dyn_ohm or 0.5, 1e-3)
    cj = (spec.c_j_pf or 10.0) * 1e-12
    z = f"{name}_Z"

    if spec.topology == "tvs-uni":
        pins = ["io", "gnd"]
        body = [f".model {z} D(IS=1e-14 N=1 RS={_num(rs)} BV={_num(bv)} IBV={_num(ibv)} CJO={_num(cj)})",
                f"D1 gnd io {z}"]
    elif spec.topology == "tvs-bi":
        # Two junctions in series halve the capacitance, so each carries twice the line value.
        pins = ["io", "gnd"]
        body = [f".model {z} D(IS=1e-14 N=1 RS={_num(rs / 2)} BV={_num(bv)} IBV={_num(ibv)} CJO={_num(2 * cj)})",
                f"D1 m io {z}", f"D2 m gnd {z}", "Rm m gnd 1e9"]
    elif spec.topology == "steering-array":
        pins = ["io", "gnd", "rail"]
        f = f"{name}_F"
        body = [f".model {f} D(IS=1e-12 N=1.1 RS={_num(rs / 2)} CJO={_num(cj / 2)})",
                f".model {z} D(IS=1e-14 N=1 RS={_num(rs / 2)} BV={_num(bv)} IBV={_num(ibv)} CJO=10p)",
                f"Dh io rail {f}", f"Dl gnd io {f}", f"Dz gnd rail {z}", "Rr rail gnd 1e9"]
    else:  # rail-clamp
        pins = ["io", "gnd", "rail"]
        s = f"{name}_S"
        is_, srs, notes = _schottky(spec.v_f_points)
        for n in notes:
            if n not in spec.assumptions:
                spec.assumptions.append(n)
        body = [f".model {s} D(IS={_num(is_)} N=1 RS={_num(srs)} BV={_num(spec.v_br_v or 30.0)} "
                f"IBV={_num(ibv)} CJO={_num(cj)})",
                f"Dh io rail {s}", f"Dl gnd io {s}", "Rr rail gnd 1e9"]

    text = "\n".join([f".subckt {name} {' '.join(pins)}", *body, f".ends {name}"])
    return text, pins


def pin_model_lines() -> list[str]:
    return [f".model {PIN_DIODE_MODEL} D(IS=1e-14 N=1.05 RS=0.5 CJO=1p)"]


def safe_name(text: str) -> str:
    return "EMI_" + re.sub(r"[^A-Za-z0-9]", "_", text or "part").upper()[:40]
