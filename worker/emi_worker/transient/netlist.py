"""Turn a line and a variant into netlist text.

Each variant of a line -- as laid out, clamp at the connector, ideal ground -- and each polarity
becomes an isolated circuit in one netlist, sharing only the ideal ground node. One ngspice run
per line then covers every comparison, which matters on a one-core worker.

Node and element names carry a per-circuit prefix, so the circuits cannot touch each other,
and every node has a path to ground so none floats. Every element name is built from a tag no
other element uses: two elements with one name make ngspice refuse the whole netlist, and that
is exactly what happened on real boards whenever a clamp sat directly on the line.

No node may sit between inductors with no capacitance of its own. Current pushed into such a
node has nowhere to go, the simulator's timestep collapses to nothing, and the run aborts in
the first nanosecond. Real circuits never have one -- an IC's supply pin has on-die
capacitance, a diode's internal node has junction and package capacitance -- so each modelled
node carries the small capacitance it physically has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import parts
from .lines import Segment

#: Below this a segment is a wire, not a transmission line.
MIN_TD_PS = 0.05
#: On-die capacitance at a modelled supply node.
ON_DIE_PF = 100.0
#: Package and bond-wire capacitance inside an IC pin, behind its series resistance.
PIN_INTERNAL_PF = 0.5
#: The generator's discharge resistor (IEC 61000-4-2 Figure 1, typical value). The source is
#: the calibrated current in parallel with it: into a clamped line almost all the current goes
#: to the line, as in calibration, while a high-impedance load sees at most about the charge
#: voltage instead of whatever a pure current source would force.
GENERATOR_OHM = 330.0
#: Anything but printable ASCII in the title line. The title carries a net name and a reference
#: designator from the board, and a newline there would start a new netlist line -- a
#: ``.control`` block that ngspice runs, shell commands included.
_UNSAFE_TITLE = re.compile(r"[^\x20-\x7e]")
MAX_TITLE = 120

#: A trace is a ladder of sections no longer than this. At about 2 mm of FR-4 microstrip a
#: section's cutoff is above 20 GHz, far past the band a 0.8 ns rise occupies.
SECTION_PS = 12.0
MAX_SECTIONS = 60


@dataclass
class ClampInstance:
    """How a clamp subcircuit is wired into the circuit."""

    subckt: str
    #: Role of each subcircuit pin, in the subcircuit's order: "io", "gnd", "rail", "through",
    #: or "nc" for an unused channel.
    roles: list[str]
    rail_v: float | None = None


@dataclass
class Circuit:
    key: str
    trunk: Segment
    clamp_stub: Segment | None
    ic_stub: Segment | None
    ground_nh: float
    via_nh: float
    clamp: ClampInstance | None = None
    lead: Segment | None = None
    series_ohm: float | None = None
    #: Where the IC stub starts: at the branch point, or at a feed-through clamp's far pin.
    ic_from_through: bool = False
    has_ic: bool = True
    supply_v: float = 3.3
    #: Capacitance to ground on the line at the IC -- decoupling on a supply, a filter on a signal.
    net_c_nf: float = 0.0
    polarity: int = 1

    def vectors(self) -> dict[str, str]:
        k = self.key
        out = {"v_connector": f"v({k}_c)"}
        if self.clamp is not None:
            out["v_clamp"] = f"v({k}_d)"
            out["i_clamp"] = f"i(v{k}_cl)"
        if self.has_ic:
            out["v_pin"] = f"v({k}_u)"
            out["i_pin"] = f"i(v{k}_pin)"
        return out


def _pwl(key: str, node: str, samples: list[tuple[float, float]]) -> list[str]:
    pairs = [f"{t:.6g} {i:.6g}" for t, i in samples]
    lines = [f"I{key} 0 {node} PWL("]
    for n in range(0, len(pairs), 8):
        lines.append("+ " + " ".join(pairs[n:n + 8]))
    lines.append("+ )")
    return lines


def _line(out: list[str], key: str, tag: str, a: str, b: str, seg: Segment | None, via_nh: float) -> None:
    """A trace from node a to node b, with its vias as series inductance at the far end.

    A lumped LC ladder, not ngspice's lossless T element. The T element is exact, but it
    schedules a breakpoint for every reflection, and on a line with a clamp stub and a nonlinear
    pin the reflections multiply until a single line took over two minutes. Pi sections -- half
    the capacitance at each end -- keep capacitance on the end nodes too, so a current source
    driving the line never looks into a bare inductor.

    Element names are ``<kind><key>_ln<tag>...``; nothing else in a circuit uses ``_ln``.
    """
    mid = b
    vias = seg.vias if seg is not None else 0
    if vias:
        mid = f"{b}_t{tag}"
    name = f"{key}_ln{tag}"
    if seg is None or seg.td_ps < MIN_TD_PS:
        out.append(f"R{name}w {a} {mid} 1m")
    else:
        n = min(MAX_SECTIONS, max(1, int(-(-seg.td_ps // SECTION_PS))))
        td = seg.td_ps / n
        l_nh = seg.z0_ohm * td * 1e-3
        c_pf = td / seg.z0_ohm
        nodes = [a] + [f"{b}_{tag}{i}" for i in range(n - 1)] + [mid]
        for i in range(n):
            out.append(f"L{name}l{i} {nodes[i]} {nodes[i + 1]} {l_nh:.6g}n")
        for j, node in enumerate(nodes):
            out.append(f"C{name}c{j} {node} 0 {(c_pf / 2 if j in (0, n) else c_pf):.6g}p")
    if vias:
        out.append(f"L{name}v {mid} {b} {vias * via_nh:.6g}n")
        out.append(f"R{name}vs {mid} 0 1e9")
    out.append(f"R{name}s {b} 0 1e9")


def _supply(out: list[str], key: str, tag: str, node: str, volts: float) -> None:
    name = f"{key}_sp{tag}"
    out += [
        f"V{name}s {node}_vs 0 {volts:.4g}",
        f"L{name}l {node}_vs {node} {parts.SUPPLY_L_NH:g}n",
        f"C{name}c {node} {node}_cd {parts.DECOUPLING_NF:g}n",
        f"L{name}e {node}_cd 0 {parts.DECOUPLING_ESL_NH:g}n",
        f"C{name}o {node} 0 {ON_DIE_PF:g}p",
        f"R{name}r {node} 0 1e9",
    ]


def circuit_lines(c: Circuit, samples: list[tuple[float, float]]) -> list[str]:
    k = c.key
    out = [f"* circuit {k}"]
    out += _pwl(k, f"{k}_c", [(t, c.polarity * i) for t, i in samples])
    out.append(f"R{k}_gen {k}_c 0 {GENERATOR_OHM:g}")

    root = f"{k}_c"
    if c.lead is not None and c.series_ohm is not None:
        _line(out, k, "ld", root, f"{k}_r", c.lead, c.via_nh)
        out.append(f"R{k}_ser {k}_r {k}_root {max(c.series_ohm, 1e-3):.6g}")
        out.append(f"R{k}_rootg {k}_root 0 1e9")
        root = f"{k}_root"

    branch = f"{k}_b"
    _line(out, k, "tr", root, branch, c.trunk, c.via_nh)

    through = f"{k}_th"
    if c.clamp is not None:
        _line(out, k, "cs", branch, f"{k}_d", c.clamp_stub, c.via_nh)
        out.append(f"V{k}_cl {k}_d {k}_dio 0")
        nodes = []
        for i, role in enumerate(c.clamp.roles):
            if role == "io":
                nodes.append(f"{k}_dio")
            elif role == "gnd":
                nodes.append(f"{k}_dg")
            elif role == "rail":
                nodes.append(f"{k}_rail")
            elif role == "through":
                nodes.append(through)
            else:
                node = f"{k}_nc{i}"
                nodes.append(node)
                out.append(f"R{k}_unused{i} {node} 0 1e6")
        out.append(f"X{k}_cl {' '.join(nodes)} {c.clamp.subckt}")
        out.append(f"L{k}_gnd {k}_dg {k}_gv {max(c.ground_nh, 1e-3):.6g}n")
        out.append(f"R{k}_gnd {k}_gv 0 1m")
        out.append(f"C{k}_gnd {k}_dg 0 {PIN_INTERNAL_PF:g}p")
        if "rail" in c.clamp.roles:
            _supply(out, k, "rl", f"{k}_rail", c.clamp.rail_v if c.clamp.rail_v is not None else c.supply_v)
        if "through" in c.clamp.roles:
            out.append(f"R{k}_thg {through} 0 1e9")

    if c.has_ic:
        # Through a feed-through clamp the IC continues from the package's far pin. A vendor
        # model has that pin; a generic one does not, so the line continues from the clamp pad.
        start = branch
        if c.ic_from_through and c.clamp is not None:
            start = through if "through" in c.clamp.roles else f"{k}_d"
        _line(out, k, "is", start, f"{k}_u", c.ic_stub, c.via_nh)
        out += [
            f"V{k}_pin {k}_u {k}_up 0",
            f"R{k}_pin {k}_up {k}_up2 {parts.PIN_R_OHM:g}",
            f"C{k}_pin {k}_u 0 {parts.PIN_C_PF:g}p",
            f"C{k}_pini {k}_up2 0 {PIN_INTERNAL_PF:g}p",
            f"D{k}_pinh {k}_up2 {k}_vdd {parts.PIN_DIODE_MODEL}",
            f"D{k}_pinl 0 {k}_up2 {parts.PIN_DIODE_MODEL}",
        ]
        _supply(out, k, "vd", f"{k}_vdd", c.supply_v)

    if c.net_c_nf > 0:
        at = f"{k}_u" if c.has_ic else branch
        out += [
            f"C{k}_netc {at} {at}_ncd {c.net_c_nf:.6g}n",
            f"L{k}_netc {at}_ncd 0 {parts.DECOUPLING_ESL_NH:g}n",
        ]
    return out


def safe_title(title: str) -> str:
    """The title reduced to one line of printable ASCII, so board text cannot add netlist lines."""
    return _UNSAFE_TITLE.sub("?", title)[:MAX_TITLE]


def build(
    circuits: list[Circuit],
    definitions: list[str],
    samples: list[tuple[float, float]],
    end_s: float,
    output_step_s: float,
    max_step_s: float,
    title: str = "emi transient",
) -> str:
    lines = [f"* {safe_title(title)}", *parts.pin_model_lines(), *definitions]
    saves: list[str] = []
    for c in circuits:
        lines += circuit_lines(c, samples)
        saves += c.vectors().values()
    # interp writes results on the output grid instead of at every internal step: the step is
    # still bounded by max_step, but the results file stays small enough to parse quickly.
    lines += [
        ".options interp",
        f".tran {output_step_s:.6g} {end_s:.6g} 0 {max_step_s:.6g}",
        ".save " + " ".join(saves),
        ".end",
    ]
    return "\n".join(lines) + "\n"
