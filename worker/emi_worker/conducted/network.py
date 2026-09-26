"""The input filter as a circuit, and the AC deck that measures it through the LISNs.

A board's input rail is reduced to a ladder: nodes along the rail, series elements between
them (the trace's loop inductance, vias, a ferrite or inductor someone placed), capacitors from
a node to ground, and the regulators' inputs as current sources. Ground is one node, the board's
return terminal: a ground plane is assumed, so the return path's inductance is already in each
trace's loop inductance (Z0 times delay over its plane) and is not added again.

Each circuit in a deck has one source and one variant of the network, and every node and element
carries the circuit's key, so one ngspice run covers every variant of one regulator: an AC
analysis cannot give two sources different frequency lists, but the variants share one.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from ..transient.netlist import safe_title
from . import lisn

#: Smallest resistance written, so a zero-ohm link or a missing ESR is not a zero in the matrix.
MIN_R_OHM = 1e-6


@dataclass
class Series:
    """A two-terminal element in the rail, from node a (the connector side) to node b."""

    a: str
    b: str
    l_h: float
    r_ohm: float = 0.0
    #: "trace", "via", "inductor", "ferrite", "resistor", "fuse", "diode", "switch" (an eFuse),
    #: "pass" (a charger or LDO a regulator draws from) or "added".
    kind: str = "trace"
    ref: str = ""
    value: str = ""
    assumed: bool = False
    length_mm: float = 0.0


@dataclass
class Shunt:
    """A capacitor from a rail node to ground: C, its ESR and ESL, and its mounting inductance."""

    node: str
    ref: str
    value: str
    c_f: float
    esr_ohm: float
    esl_h: float
    mount_h: float = 0.0
    #: Where the ESR and ESL came from, for the UI: "library", "assumed" ...
    model: str = ""
    assumed: bool = False
    #: Added by a what-if, not on the board.
    added: bool = False
    distance_mm: float = 0.0

    def impedance(self, frequency_hz: float) -> complex:
        w = 2 * math.pi * frequency_hz
        return complex(max(self.esr_ohm, MIN_R_OHM), w * (self.esl_h + self.mount_h) - 1 / (w * self.c_f))


@dataclass
class Network:
    """The rail from the power entry (node ``entry``) to every regulator input."""

    entry: str = "in"
    series: list[Series] = field(default_factory=list)
    shunts: list[Shunt] = field(default_factory=list)
    #: Regulator reference -> the node its input pin is on.
    sources: dict[str, str] = field(default_factory=dict)

    def copy(self) -> "Network":
        return copy.deepcopy(self)

    def nodes(self) -> list[str]:
        seen = [self.entry]
        for s in self.series:
            for n in (s.a, s.b):
                if n not in seen:
                    seen.append(n)
        for c in self.shunts:
            if c.node not in seen:
                seen.append(c.node)
        for n in self.sources.values():
            if n not in seen:
                seen.append(n)
        return seen

    def without(self, ref: str) -> "Network":
        out = self.copy()
        out.shunts = [c for c in out.shunts if c.ref != ref]
        return out

    def insert_after_entry(self, element: Series) -> "Network":
        """A series element between the connector and everything else on the rail.

        The connector's own node keeps nothing but the new element; whatever was attached there
        moves behind it. That is where an added filter goes: between the cable and the board.
        """
        out = self.copy()
        behind = "in_f"
        for s in out.series:
            if s.a == out.entry:
                s.a = behind
            if s.b == out.entry:
                s.b = behind
        for c in out.shunts:
            if c.node == out.entry:
                c.node = behind
        for ref, node in list(out.sources.items()):
            if node == out.entry:
                out.sources[ref] = behind
        element.a, element.b = out.entry, behind
        out.series.insert(0, element)
        return out


def _r(v: float) -> str:
    return f"{max(v, MIN_R_OHM):.6g}"


def circuit_lines(key: str, net: Network, source: str) -> tuple[list[str], dict[str, str]]:
    """One circuit: both LISNs, the rail, and the named regulator's source at 1 A.

    Returns the lines and the vectors to save: "v_p" and "v_n" at the two LISN terminals, and
    "i:<ref>" for the current in each capacitor.
    """
    k = key

    def node(name: str) -> str:
        return f"{k}_{name}"

    gnd = f"{k}_gnd"
    out = [f"* circuit {k}"]
    out += lisn.lines(k, "p", node(net.entry))
    out += lisn.lines(k, "n", gnd)
    for i, s in enumerate(net.series):
        a, b = node(s.a), node(s.b)
        if s.l_h > 0:
            mid = f"{k}_x{i}"
            out.append(f"R{k}_x{i} {a} {mid} {_r(s.r_ohm)}")
            out.append(f"L{k}_x{i} {mid} {b} {s.l_h:.6g}")
        else:
            out.append(f"R{k}_x{i} {a} {b} {_r(s.r_ohm)}")
    saves = {"v_p": f"v({node(net.entry)})", "v_n": f"v({gnd})"}
    for i, c in enumerate(net.shunts):
        n0 = node(c.node)
        n1, n2, n3 = f"{k}_c{i}a", f"{k}_c{i}b", f"{k}_c{i}c"
        out.append(f"V{k}_m{i} {n0} {n1} 0")
        out.append(f"R{k}_c{i} {n1} {n2} {_r(c.esr_ohm)}")
        l_h = c.esl_h + c.mount_h
        if l_h > 0:
            out.append(f"L{k}_c{i} {n2} {n3} {l_h:.6g}")
        else:
            out.append(f"R{k}_c{i}l {n2} {n3} {MIN_R_OHM:g}")
        out.append(f"C{k}_c{i} {n3} {gnd} {c.c_f:.6g}")
        saves[f"i:{i}"] = f"i(v{k}_m{i})"
    # The source draws from the rail into ground, as a regulator's input does. Only its
    # magnitude matters: every result is a magnitude.
    out.append(f"I{k}_src {node(net.sources[source])} {gnd} DC 0 AC 1")
    return out, saves


def harmonic_sweep(frequency_hz: float, f_lo: float = lisn.F_LO_HZ, f_hi: float = lisn.F_HI_HZ) -> tuple[int, int]:
    """(first, last) harmonic number of a switching frequency inside the band, or (0, -1)."""
    first = max(1, math.ceil(f_lo / frequency_hz - 1e-9))
    last = math.floor(f_hi / frequency_hz + 1e-9)
    return first, last


def build(circuits: list[tuple[str, Network, str]], frequency_hz: float, first: int, last: int,
          title: str = "emi conducted") -> tuple[str, dict[str, dict[str, str]]]:
    """A deck of isolated circuits and an AC analysis at exactly the harmonics first..last.

    ``.ac lin`` from the first harmonic to the last with one point per harmonic puts a solution
    on every harmonic and nowhere else, so nothing is interpolated.
    """
    lines = [f"* {safe_title(title)}"]
    saves: dict[str, dict[str, str]] = {}
    for key, net, source in circuits:
        body, vectors = circuit_lines(key, net, source)
        lines += body
        saves[key] = vectors
    points = last - first + 1
    names = " ".join(v for vectors in saves.values() for v in vectors.values())
    lines += [
        f".ac lin {points} {first * frequency_hz:.9g} {last * frequency_hz:.9g}",
        ".save " + names,
        ".end",
    ]
    return "\n".join(lines) + "\n", saves
