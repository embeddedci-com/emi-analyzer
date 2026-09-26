"""The physics behind the decoupling view: a lumped power-distribution impedance.

Every capacitor that serves an IC is a series R-L-C branch, the branches are in parallel, and
the IC sees their combination. That is the whole model, and it is deliberately the model a
power-integrity engineer draws on a whiteboard, because it answers the question the text
findings could not: *which frequencies does this set of capacitors actually cover*.

What goes into each branch's L is the part that needs care, because it is where layouts
differ. A 100 nF 0402 has about 0.45 nH of its own; the loop that connects it to the IC is
routinely 1 to 3 nH, and that is what decides where it stops working. The formulas, all
closed form and all cited:

* **Connection loop** (pads, trace, vias down to the plane): a rectangular loop of round
  wire over a ground plane, which by image theory is half a free-space rectangle twice as
  tall. Rectangle formula from Grover, *Inductance Calculations* (1946), as restated in
  Paul, *Inductance: Loop and Partial* (2009), eq. 5.20. Checked against Archambeault,
  Connor and Steffka's published connection inductances for 0402/0603/0805 mounts
  (In Compliance Magazine, "Inductance: the misconceptions, myths and truth", Tables 1-2):
  within -17 % to +24 % over 30 entries, within 6 % at 20 to 40 mil depth.
  See docs/verification/decoupling.md.

* **Spreading inductance** between a capacitor's vias and the IC's, through a power/ground
  plane pair of separation t: two parallel wires of length t, (mu0 t / pi) ln(D / r). The
  same two-wire formula as Paul (2009) §4.2, with the cavity as the wire length.

* **Plane capacitance**: eps0 eps_r A / t, the parallel-plate formula. Its branch inductance
  is the radial parallel-plate inductance from the via to the plane's equivalent radius,
  (mu0 t / 2 pi) ln(R / r), and its loss comes from the dielectric's loss tangent.

* **First cavity resonance**: c / (2 a sqrt(eps_r)) for the longest side a. Above it a plane
  pair is a resonator, not a capacitor, and this model says so rather than pretending.

The model does not know about the regulator's output impedance, the package and die
capacitance, or anything the vias do beyond their inductance. Those are the known issues.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

MU0_NH_PER_MM = 4e-7 * math.pi * 1e-3 * 1e9  # 1.2566 nH/mm
EPS0_F_PER_MM = 8.8541878128e-15  # F/mm
C_MM_S = 299_792_458.0 * 1000.0

#: The chart's range. 100 kHz is where a regulator's loop stops being the answer; 1 GHz is as
#: far as a lumped model of a board is worth reading.
F_MIN_HZ = 1e5
F_MAX_HZ = 1e9
POINTS_PER_DECADE = 40


def frequencies() -> np.ndarray:
    n = int(round(math.log10(F_MAX_HZ / F_MIN_HZ) * POINTS_PER_DECADE)) + 1
    return np.logspace(math.log10(F_MIN_HZ), math.log10(F_MAX_HZ), n)


# ---- inductance formulas -----------------------------------------------------------------

def rectangle_loop_nh(a_mm: float, b_mm: float, r_mm: float) -> float:
    """Inductance of a rectangular loop of round wire, sides a and b, wire radius r.

    Grover (1946); Paul (2009) eq. 5.20. External inductance only: the wire's internal
    inductance vanishes at the frequencies decoupling is about (skin effect).
    """
    a = max(a_mm, 2.0 * r_mm)
    b = max(b_mm, 2.0 * r_mm)
    d = math.hypot(a, b)
    return MU0_NH_PER_MM / math.pi * (
        -2.0 * (a + b) + 2.0 * d
        - a * math.log((a + d) / b) - b * math.log((b + d) / a)
        + a * math.log(2.0 * a / r_mm) + b * math.log(2.0 * b / r_mm)
    )


def connection_nh(length_mm: float, height_mm: float, via_radius_mm: float,
                  ground_vias: int = 1) -> float:
    """The loop from the capacitor's pads down to the plane and back: half a rectangle of
    ``length`` by ``2 * height``, the plane being the mirror (image theory).

    A second ground via only parallels one of the loop's two vertical legs, so it is modeled
    as halving that leg rather than the whole loop: n vias scale the loop by (1 + n) / 2n.
    Mutual inductance between the paralleled vias is ignored, which makes it slightly
    optimistic for vias closer than about a millimetre.
    """
    loop = 0.5 * rectangle_loop_nh(length_mm, 2.0 * height_mm, via_radius_mm)
    n = max(1, ground_vias)
    return loop * (1.0 + n) / (2.0 * n)


def spreading_nh(cavity_mm: float, distance_mm: float, via_radius_mm: float) -> float:
    """Two vias through a plane pair, ``distance`` apart: (mu0 t / pi) ln(D / r)."""
    d = max(distance_mm, 2.0 * via_radius_mm)
    return MU0_NH_PER_MM / math.pi * cavity_mm * math.log(d / via_radius_mm)


def plane_capacitance_f(area_mm2: float, cavity_mm: float, eps_r: float) -> float:
    return EPS0_F_PER_MM * eps_r * area_mm2 / cavity_mm


def plane_branch_nh(area_mm2: float, cavity_mm: float, via_radius_mm: float) -> float:
    """Radial parallel-plate inductance from a via out to the plane's equivalent radius."""
    radius = math.sqrt(area_mm2 / math.pi)
    return MU0_NH_PER_MM / (2.0 * math.pi) * cavity_mm * math.log(max(radius / via_radius_mm, 1.0))


def cavity_resonance_hz(longest_side_mm: float, eps_r: float) -> float:
    return C_MM_S / (2.0 * longest_side_mm * math.sqrt(eps_r))


# ---- branches and their combination ------------------------------------------------------

@dataclass(frozen=True)
class Branch:
    """One series R-L-C path from the IC's supply pins to ground."""

    id: str
    c_f: float
    #: Total series inductance: the part's own ESL plus every loop in series with it.
    l_h: float
    r_ohm: float
    kind: str = "cap"  # cap | plane
    #: The part's own ESL, kept separately so the table can say how much is the layout.
    esl_h: float = 0.0

    def z(self, f: np.ndarray) -> np.ndarray:
        w = 2.0 * np.pi * f
        return self.r_ohm + 1j * (w * self.l_h - 1.0 / (w * self.c_f))

    @property
    def srf_hz(self) -> float:
        return 1.0 / (2.0 * math.pi * math.sqrt(self.l_h * self.c_f))


@dataclass
class Network:
    """The branches in parallel, behind a series inductance common to all of them.

    The common term is the IC's own connection to the planes: on a board with a power plane
    every capacitor reaches the IC through it, so it is in series with the whole bank.
    """

    branches: list[Branch] = field(default_factory=list)
    series_l_h: float = 0.0

    def z(self, f: np.ndarray) -> np.ndarray:
        if not self.branches:
            return np.full(f.shape, np.inf, dtype=complex)
        y = sum(1.0 / b.z(f) for b in self.branches)
        return 1.0 / y + 1j * 2.0 * np.pi * f * self.series_l_h

    def mag(self, f: np.ndarray) -> np.ndarray:
        return np.abs(self.z(f))

    def without(self, branch_id: str) -> "Network":
        return Network([b for b in self.branches if b.id != branch_id], self.series_l_h)

    def replaced(self, branch_id: str, **changes) -> "Network":
        return Network([replace(b, **changes) if b.id == branch_id else b for b in self.branches],
                       self.series_l_h)

    def plus(self, branch: Branch) -> "Network":
        return Network(self.branches + [branch], self.series_l_h)


def _refine_max(net: "Network", lo: float, hi: float) -> tuple[float, float]:
    """The peak between two grid points, found on two nested dense grids.

    The grid is 40 points a decade, a 6 % step, and a sharp anti-resonance between two good
    capacitors is narrower than that: sampling alone reads a 25 dB peak as 15. A golden-section
    search found it too, one frequency at a time, and was 70 % of the view's run time on a
    busy board; two vectorised passes of 64 points land within 0.003 % of it.
    """
    for _ in range(2):
        fs = np.geomspace(lo, hi, 64)
        z = net.mag(fs)
        i = int(np.argmax(z))
        lo, hi = fs[max(i - 1, 0)], fs[min(i + 1, len(fs) - 1)]
    return float(fs[i]), float(z[i])


def anti_resonances(net: Network, f: np.ndarray) -> list[tuple[float, float]]:
    """Local maxima of |Z|: (frequency, |Z|), refined off the grid.

    A maximum at either end of the range is not a peak, it is a slope that continues.
    """
    z = net.mag(f)
    return [_refine_max(net, f[i - 1], f[i + 1])
            for i in range(1, len(f) - 1) if z[i] > z[i - 1] and z[i] >= z[i + 1]]


def closed_form_two_cap_peak(c1: Branch, c2: Branch) -> tuple[float, float]:
    """The textbook anti-resonance of two branches: where the loop L1 + L2 resonates with
    C1 and C2 in series, and |Z| there is |Z1||Z2| / (R1 + R2) because the reactances cancel
    in the sum. Used only to verify the numeric combination (docs/verification)."""
    cs = c1.c_f * c2.c_f / (c1.c_f + c2.c_f)
    f = 1.0 / (2.0 * math.pi * math.sqrt((c1.l_h + c2.l_h) * cs))
    fa = np.array([f])
    z1 = abs(c1.z(fa)[0])
    z2 = abs(c2.z(fa)[0])
    return f, z1 * z2 / (c1.r_ohm + c2.r_ohm)


# ---- gaps against a target ---------------------------------------------------------------

def gaps(f: np.ndarray, zmag: np.ndarray, target: float) -> list[tuple[float, float]]:
    """Contiguous frequency ranges where |Z| exceeds the target: [(f_lo, f_hi)]."""
    over = zmag > target
    out: list[tuple[float, float]] = []
    start = None
    for i, o in enumerate(over):
        if o and start is None:
            start = i
        if not o and start is not None:
            out.append((float(f[start]), float(f[i - 1])))
            start = None
    if start is not None:
        out.append((float(f[start]), float(f[-1])))
    return out


def worst_excess_db(zmag: np.ndarray, target: float) -> tuple[float, int]:
    """How far above the target the worst point is, and where. Negative when it never is."""
    ratio = 20.0 * np.log10(zmag / target)
    i = int(np.argmax(ratio))
    return float(ratio[i]), i
