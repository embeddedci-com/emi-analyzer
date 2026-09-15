"""Characteristic impedance from the stackup.

Closed-form, not a field solver. The formulas are the standard IPC-2141 approximations, and
they are good to roughly 5-10% against a 2D solver for the geometries boards actually use --
which is worth being explicit about, because it decides what the tool is allowed to claim.

A DDR design targets 40 ohms with a 10% tolerance. If the model itself is 8% uncertain and
the permittivity was assumed rather than read from the board file, the honest answer for a
trace computed at 44 ohms is "between 39 and 49, so possibly fine". Reporting "44 ohms, out
of spec" would be a confident number the tool has no right to. So everything here returns an
uncertainty alongside the value, and the checks only flag what is outside tolerance *after*
that is taken into account.

A 2D quasi-static solver is the natural next step and would remove most of the model error.
openEMS does not help: it is a 3D FDTD solver for radiated fields, and a cross-section
impedance problem wants a different tool entirely.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .stackup import LayerElectrics

#: Roughly how far the closed-form results sit from a field solver, inside their validity
#: range. Outside it, see Impedance.notes.
MODEL_UNCERTAINTY_PCT = 8.0

#: Extra uncertainty when the permittivity is a default rather than the board's own. FR-4
#: spans about 4.2 to 4.8 depending on resin content and frequency, which is several ohms.
ASSUMED_EPSILON_UNCERTAINTY_PCT = 7.0

#: The width-to-height range the microstrip formula is valid over.
MICROSTRIP_W_OVER_H = (0.1, 3.0)


@dataclass(frozen=True)
class Impedance:
    ohm: float
    uncertainty_pct: float
    kind: str  # microstrip | stripline
    differential: bool = False
    notes: tuple[str, ...] = ()

    @property
    def low(self) -> float:
        return self.ohm * (1 - self.uncertainty_pct / 100)

    @property
    def high(self) -> float:
        return self.ohm * (1 + self.uncertainty_pct / 100)

    def within(self, target: float, tolerance_pct: float) -> bool:
        """Does the trace plausibly meet its target?

        True when the *range* overlaps the target band, not when the nominal does. A check
        that flagged everything whose nominal missed would be reporting the model's error as
        the board's.
        """
        lo, hi = target * (1 - tolerance_pct / 100), target * (1 + tolerance_pct / 100)
        return self.high >= lo and self.low <= hi

    def describe(self) -> str:
        return f"{self.ohm:.0f} Ω (±{self.uncertainty_pct:.0f}%, {self.low:.0f}–{self.high:.0f})"


def single_ended(width_mm: float, layer: LayerElectrics) -> Impedance | None:
    """Z0 of one trace over its reference plane."""
    h = layer.height_mm
    t = layer.copper_thickness_mm
    if width_mm <= 0 or h <= 0:
        return None

    notes: list[str] = []
    unc = MODEL_UNCERTAINTY_PCT
    if layer.assumed:
        unc += ASSUMED_EPSILON_UNCERTAINTY_PCT
        notes.append(
            f"permittivity {layer.epsilon_r:.1f} is assumed, not from the board file"
        )

    if layer.kind == "stripline" and layer.height_above_mm > 0:
        b = h + layer.height_above_mm
        z = (60.0 / math.sqrt(layer.epsilon_r)) * math.log(
            4.0 * b / (0.67 * math.pi * (0.8 * width_mm + t))
        )
        # The symmetric formula assumes the trace is centred. Real stacks are rarely exactly
        # symmetric, and the further off centre, the worse it fits.
        offset = abs(h - layer.height_above_mm) / b
        if offset > 0.2:
            unc += 5.0
            notes.append("the trace is well off centre between its planes; asymmetric stripline is approximated here")
    else:
        w_over_h = width_mm / h
        z = (87.0 / math.sqrt(layer.epsilon_r + 1.41)) * math.log(
            5.98 * h / (0.8 * width_mm + t)
        )
        if not (MICROSTRIP_W_OVER_H[0] <= w_over_h <= MICROSTRIP_W_OVER_H[1]):
            unc += 10.0
            notes.append(
                f"width/height is {w_over_h:.2f}, outside the range this formula is valid "
                f"over ({MICROSTRIP_W_OVER_H[0]}-{MICROSTRIP_W_OVER_H[1]})"
            )

    if z <= 0 or not math.isfinite(z):
        # A trace many times wider than its dielectric height drives the formula negative.
        # There is no useful answer to give, and a made-up one would be worse than silence,
        # so the net is simply not reported on.
        return None
    return Impedance(ohm=z, uncertainty_pct=unc, kind=layer.kind, notes=tuple(notes))


def differential(width_mm: float, gap_mm: float, layer: LayerElectrics) -> Impedance | None:
    """Differential impedance of a coupled pair.

    Derived from the single-ended value and the coupling between the two traces, which is
    what the exponential terms below approximate. Coupling falls off quickly with spacing:
    a pair separated by more than about three times the dielectric height is barely a pair
    at all, and Zdiff approaches 2*Z0.
    """
    se = single_ended(width_mm, layer)
    if se is None or gap_mm <= 0:
        return None

    h = layer.height_mm
    notes = list(se.notes)
    if layer.kind == "stripline" and layer.height_above_mm > 0:
        b = h + layer.height_above_mm
        factor = 1 - 0.347 * math.exp(-2.9 * gap_mm / b)
    else:
        factor = 1 - 0.48 * math.exp(-0.96 * gap_mm / h)

    if gap_mm / max(h, 1e-9) > 3.0:
        notes.append(
            "the traces are far enough apart to be barely coupled; this is close to twice "
            "the single-ended impedance and the pair is differential in name only"
        )
    return Impedance(
        ohm=2 * se.ohm * factor,
        uncertainty_pct=se.uncertainty_pct + 2.0,
        kind=layer.kind,
        differential=True,
        notes=tuple(notes),
    )
