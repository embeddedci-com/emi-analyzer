"""Simulate every exposed line and reduce the waveforms to what a person can compare.

For a protected line there are three circuits: as laid out, the clamp moved to the connector,
and the clamp's ground ideal. For an unprotected line: as laid out, and an illustrative clamp at
the connector. The difference between them is the result; the absolute volts are estimates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from . import ngspice, parts, waveforms
from .lines import ExposedLine, Segment
from .models import VendorModel, roles_for
from .netlist import GENERATOR_OHM, Circuit, ClampInstance, build

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
END_S = 60e-9  # the standard measures to 60 ns after the 10 % crossing
OUTPUT_STEP_S = 5e-12
MAX_STEP_S = 2e-12
#: A connector node above this share of the generator's open-circuit voltage means the line
#: barely loads it: nothing on the line takes the discharge, and the voltage figures describe the
#: generator, not the board.
UNCLAMPED_OPEN_CIRCUIT_SHARE = 0.9
IDEAL_GROUND_NH = 0.1
MOVED_CLAMP_STUB_MM = 1.0
WAVEFORM_POINTS = 240

VARIANT_LABELS = {
    "as_laid_out": "As laid out",
    "clamp_at_connector": "Clamp at the connector",
    "ideal_ground": "Clamp ground ideal",
    "reference_clamp": "With a clamp at the connector",
}

# numpy 2 renamed trapz.
_integrate = getattr(np, "trapezoid", None) or np.trapz


@dataclass
class ResolvedClamp:
    kind: str  # vendor | table | illustrative
    part: str
    subckt: str
    definition: str
    pins: list[str]
    source: str
    assumptions: list[str] = field(default_factory=list)
    vendor: VendorModel | None = None


def _like(template: Segment | None, length: float, vias: int = 0) -> Segment:
    if template is not None and template.length_mm > 0:
        ps, z0 = template.td_ps / template.length_mm, template.z0_ohm
    else:
        ps, z0 = 6.0, (template.z0_ohm if template is not None else 50.0)
    return Segment(length_mm=max(length, 0.0), td_ps=max(length, 0.0) * ps, z0_ohm=z0, vias=vias)


def resolve_clamp(line: ExposedLine, vendor: dict[str, VendorModel], defined: dict[str, ResolvedClamp]) -> ResolvedClamp:
    key = parts._norm(line.clamp_part)
    vm = vendor.get(key)
    if vm is not None and vm.accepted:
        name = vm.subckt
        if name not in defined:
            defined[name] = ResolvedClamp("vendor", line.clamp_part, name, vm.text, vm.pins,
                                          f"uploaded model {vm.filename}", vendor=vm)
        return defined[name]
    spec = parts.lookup(line.clamp_part)
    kind = "table"
    extra: list[str] = []
    if spec is None:
        spec = parts.illustrative_clamp(line.clamp_part)
        kind = "illustrative"
        extra.append(f"{line.clamp_part or line.clamp_ref} is not in the datasheet table")
    if vm is not None and not vm.accepted:
        extra.append(f"the uploaded model for {vm.part} was rejected, so it was not used")
    name = parts.safe_name(spec.part + ("_ILL" if kind == "illustrative" else ""))
    if name not in defined:
        text, pins = parts.clamp_subckt(spec, name)
        defined[name] = ResolvedClamp(kind, spec.part, name, text, pins, spec.source or "illustrative",
                                      assumptions=list(spec.assumptions))
    base = defined[name]
    return ResolvedClamp(base.kind, base.part, base.subckt, base.definition, base.pins, base.source,
                         assumptions=base.assumptions + extra)


def _instance(line: ExposedLine, clamp: ResolvedClamp, kind_of) -> ClampInstance:
    if clamp.kind == "vendor" and clamp.vendor is not None:
        roles = roles_for(clamp.pins, clamp.vendor.pad_to_pin, line.clamp_pads, line.clamp_pad,
                          line.through_pad, kind_of)
    else:
        # A generic model's rail is supplied only when the part's rail pin is on a supply; an
        # unconnected rail floats behind the model's own gigaohm.
        roles = ["nc" if (pin == "rail" and line.clamp_rail_v is None) else pin for pin in clamp.pins]
    return ClampInstance(subckt=clamp.subckt, roles=roles, rail_v=line.clamp_rail_v)


def _reference_instance(reference: ResolvedClamp) -> ClampInstance:
    return ClampInstance(subckt=reference.subckt,
                         roles=["nc" if pin == "rail" else pin for pin in reference.pins])


def _circuits(line: ExposedLine, clamp: ResolvedClamp | None, reference: ResolvedClamp, kind_of,
              polarities: list[int]) -> list[tuple[str, Circuit]]:
    tree = line.tree
    has_ic = line.ic_pad is not None
    through = line.through_pad is not None
    base = dict(via_nh=line.via_nh, lead=line.lead,
                series_ohm=line.series_resistor[1] if line.series_resistor else None,
                has_ic=has_ic, supply_v=line.ic_supply_v, ic_from_through=through,
                net_c_nf=line.net_c_nf)
    variants: list[tuple[str, dict]] = []

    if clamp is not None:
        laid = dict(trunk=tree.trunk, clamp_stub=tree.clamp_stub, ic_stub=tree.ic_stub,
                    ground_nh=line.ground_nh, clamp=_instance(line, clamp, kind_of))
        variants.append(("as_laid_out", laid))
        if line.series_resistor is None:
            if through:
                ic_len = (tree.ic_stub.length_mm if tree.ic_stub else 0.0) + max(tree.trunk.length_mm - MOVED_CLAMP_STUB_MM, 0.0)
                moved = dict(laid, trunk=_like(tree.trunk, MOVED_CLAMP_STUB_MM), clamp_stub=_like(tree.trunk, 0.0),
                             ic_stub=_like(tree.ic_stub, ic_len, tree.ic_stub.vias if tree.ic_stub else 0))
            else:
                ic_path = tree.ic_path
                moved = dict(laid, trunk=_like(tree.trunk, 0.0),
                             clamp_stub=_like(tree.clamp_path or tree.trunk, MOVED_CLAMP_STUB_MM),
                             ic_stub=_like(ic_path, ic_path.length_mm, ic_path.vias) if ic_path else None)
            variants.append(("clamp_at_connector", moved))
        variants.append(("ideal_ground", dict(laid, ground_nh=IDEAL_GROUND_NH)))
    else:
        variants.append(("as_laid_out", dict(trunk=tree.trunk, clamp_stub=None, ic_stub=tree.ic_stub,
                                             ground_nh=0.0, clamp=None)))
        if has_ic:
            full = tree.ic_path or tree.trunk
            variants.append(("reference_clamp", dict(
                trunk=_like(full, 0.0), clamp_stub=_like(full, MOVED_CLAMP_STUB_MM),
                ic_stub=_like(full, full.length_mm, full.vias),
                ground_nh=line.via_nh, clamp=_reference_instance(reference), ic_from_through=False)))

    out = []
    n = 0
    for variant, fields in variants:
        for polarity in polarities:
            out.append((variant, Circuit(key=f"k{n}", polarity=polarity, **{**base, **fields})))
            n += 1
    return out


def _grid() -> np.ndarray:
    dense = np.linspace(0.0, 5e-9, WAVEFORM_POINTS * 2 // 3, endpoint=False)
    coarse = np.linspace(5e-9, END_S, WAVEFORM_POINTS - len(dense))
    return np.concatenate([dense, coarse])


def _metrics(result: ngspice.Waveforms, circuit: Circuit, source_peak_a: float) -> dict:
    v = circuit.vectors()
    t = result.time
    out: dict = {}

    def signed_peak(name: str) -> float:
        arr = result[v[name]]
        return float(arr[int(np.argmax(np.abs(arr)))])

    if "v_pin" in v:
        vp, ip = result[v["v_pin"]], result[v["i_pin"]]
        out["v_pin_peak_v"] = round(signed_peak("v_pin"), 2)
        out["i_pin_peak_a"] = round(signed_peak("i_pin"), 3)
        out["e_pin_uj"] = round(abs(float(_integrate(vp * ip, t))) * 1e6, 3)
    if "v_clamp" in v:
        out["v_clamp_peak_v"] = round(signed_peak("v_clamp"), 2)
        out["i_clamp_peak_a"] = round(signed_peak("i_clamp"), 3)
    out["v_connector_peak_v"] = round(signed_peak("v_connector"), 1)
    # Judged at the connector, not by how much current the clamp takes: a series resistor in
    # front of a clamp rightly limits that current, and the first version of this test called
    # every R-clamp on a real board unclamped. What "nothing takes the current" really means is
    # that the line barely loads the generator, so the connector reaches its open-circuit voltage.
    open_circuit_v = source_peak_a * GENERATOR_OHM
    out["unclamped"] = abs(out["v_connector_peak_v"]) > UNCLAMPED_OPEN_CIRCUIT_SHARE * open_circuit_v
    return out


def simulate_line(line: ExposedLine, clamp: ResolvedClamp | None, reference: ResolvedClamp, level: int,
                  polarities: list[int], kind_of) -> dict:
    spec = waveforms.CONTACT_LEVELS[level]
    samples = waveforms.esd_samples(spec.kv, end_s=END_S)
    circuits = _circuits(line, clamp, reference, kind_of, polarities)
    definitions = []
    if clamp is not None:
        definitions.append(clamp.definition)
    uses_reference = any(c.clamp is not None and c.clamp.subckt == reference.subckt for _, c in circuits)
    if uses_reference and (clamp is None or clamp.subckt != reference.subckt):
        definitions.append(reference.definition)

    netlist = build([c for _, c in circuits], definitions, samples, END_S, OUTPUT_STEP_S, MAX_STEP_S,
                    title=f"{line.net} at {line.connector}")
    result = ngspice.run(netlist, expect_end_s=END_S)

    grid = _grid()
    by_variant: dict[str, list[tuple[Circuit, dict]]] = {}
    for variant, circuit in circuits:
        by_variant.setdefault(variant, []).append((circuit, _metrics(result, circuit, spec.peak_a)))

    def severity(item) -> float:
        _, m = item
        return abs(m.get("v_pin_peak_v", m.get("v_clamp_peak_v", m["v_connector_peak_v"])))

    variants_out = []
    for variant, runs in by_variant.items():
        circuit, worst = max(runs, key=severity)
        entry = {"id": variant, "label": VARIANT_LABELS[variant],
                 "worst_polarity": "positive" if circuit.polarity > 0 else "negative", **worst}
        vecs = circuit.vectors()
        if "v_pin" in vecs:
            entry["v_pin"] = [round(float(x), 3) for x in np.interp(grid, result.time, result[vecs["v_pin"]])]
        if "i_clamp" in vecs:
            entry["i_clamp"] = [round(float(x), 4) for x in np.interp(grid, result.time, result[vecs["i_clamp"]])]
        variants_out.append(entry)

    return {"variants": variants_out, "t_ns": [round(float(x) * 1e9, 4) for x in grid]}


def reference_clamp() -> ResolvedClamp:
    spec = parts.illustrative_clamp()
    name = parts.safe_name(spec.part + "_REF")
    text, pins = parts.clamp_subckt(spec, name)
    return ResolvedClamp("illustrative", spec.part, name, text, pins, spec.source or "illustrative",
                         assumptions=list(spec.assumptions))


def simulate(lines: list[ExposedLine], level: int, polarities: list[int], vendor: dict[str, VendorModel],
             transform, kind_of, progress=None, check_stop=None) -> dict:
    spec = waveforms.CONTACT_LEVELS[level]
    defined: dict[str, ResolvedClamp] = {}
    reference = reference_clamp()

    out_lines = []
    for i, line in enumerate(lines):
        if check_stop:
            check_stop()
        if progress:
            progress(i, len(lines), line)
        clamp = resolve_clamp(line, vendor, defined) if line.clamp_ref else None
        x, y = transform.pt(line.connector_pad.x, line.connector_pad.y)
        entry = {
            "net": line.net,
            "connector": {"ref": line.connector, "pad": line.connector_pad.number},
            "x": round(x, 3), "y": round(y, 3),
            "clamp": None if clamp is None else {
                "ref": line.clamp_ref, "pad": line.clamp_pad.number, "part": line.clamp_part,
                "model": clamp.kind, "source": clamp.source, "assumptions": clamp.assumptions,
            },
            "series_resistor": None if line.series_resistor is None else {
                "ref": line.series_resistor[0], "ohm": line.series_resistor[1]},
            "through": None if line.through_pad is None else {"ref": line.through_pad.ref, "pad": line.through_pad.number},
            "ic": None if line.ic_pad is None else {"ref": line.ic_pad.ref, "pad": line.ic_pad.number,
                                                     "supply": line.ic_supply_net, "supply_v": line.ic_supply_v},
            "geometry": {
                "routed": line.tree.routed,
                "trunk": line.tree.trunk.as_dict(),
                "clamp_stub": line.tree.clamp_stub.as_dict() if line.tree.clamp_stub else None,
                "ic_stub": line.tree.ic_stub.as_dict() if line.tree.ic_stub else None,
                "lead": line.lead.as_dict() if line.lead else None,
                "clamp_ground_mm": round(line.ground_gap_mm, 2),
                "clamp_ground_nh": round(line.ground_nh, 3),
                "via_nh": round(line.via_nh, 3),
                "net_c_nf": round(line.net_c_nf, 2),
            },
            "notes": list(line.notes),
        }
        try:
            entry.update(simulate_line(line, clamp, reference, level, polarities, kind_of))
        except ngspice.SimulationError as exc:
            log.warning("simulation of %s failed: %s", line.net, exc)
            entry["error"] = str(exc)
        out_lines.append(entry)

    return {
        "format_version": FORMAT_VERSION,
        "standard": waveforms.STANDARD,
        "discharge": "contact",
        "level": level,
        "kv": spec.kv,
        "polarities": ["positive" if p > 0 else "negative" for p in polarities],
        "source_check": waveforms.conformance(level),
        "reference_clamp": {"part": reference.part, "model": reference.kind, "assumptions": reference.assumptions},
        "lines": out_lines,
        "models": [vm.as_dict() for vm in vendor.values()],
    }
