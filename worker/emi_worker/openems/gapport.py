"""Tier B's gap port: where the board hands current to a cable (§7).

A short PEC stub leaves the board along the connector's exit normal, and a one-cell gap
between the board's ground copper and that stub carries a 1 MΩ lumped element with probes
across it. The element is effectively an open circuit, so what the probe measures is the
**open-circuit voltage** — the Thévenin source the cable sees.

That is the whole trick, and M0 measured that it works: swapping a 10 mm root for a 300 mm
cable moved the open-circuit voltage by about 1 dB, and composing ``V_oc / Z_ant`` reproduced
a fully coupled solve to 1.3 dB. The solve therefore never has to contain the cable, which is
what makes Tier B affordable — a meshed 1 m cable costs roughly eight times a board solve, and
more once the mesh is fine enough to place its resonance (see `docs/m0-findings.md`).

**The stub is short on purpose.** It exists to give the gap somewhere to be, not to radiate.
Making it longer would start to model the cable badly, in a grid sized for a board.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: How far the cable root extends past the board edge, in mm. §7 says 10 mm. Long enough that
#: the gap is clear of the board's own copper and short enough to stay inside the air margin
#: a solve already has.
STUB_LENGTH_MM = 10.0

#: How far past the stub's end the grid must reach before the absorbing boundary starts. A
#: stub that ends at the edge of the domain is terminated by the PML rather than by open air.
STUB_CLEARANCE_MM = 5.0


@dataclass(frozen=True)
class GapPort:
    """One connector's cable root and the gap that measures its drive."""

    ref: str
    #: Where the stub starts, on the board outline.
    x_mm: float
    y_mm: float
    #: Outward unit normal along which the stub runs.
    nx: float
    ny: float
    #: Height of the stub, which is the connector's reference copper layer.
    z_mm: float
    #: Width across the exit direction. §7: as wide as the shell.
    width_mm: float
    #: The gap's width along the normal — one cell, set by the mesh.
    gap_mm: float

    @property
    def axis(self) -> int:
        """0 for x, 1 for y. A stub that is not axis-aligned has no single cell to gap."""
        return 0 if abs(self.nx) >= abs(self.ny) else 1

    @property
    def sign(self) -> float:
        return math.copysign(1.0, self.nx if self.axis == 0 else self.ny)

    def gap_span(self) -> tuple[float, float]:
        """Start and end of the gap along the exit axis, in board mm, low first."""
        start = self.x_mm if self.axis == 0 else self.y_mm
        end = start + self.sign * self.gap_mm
        return (min(start, end), max(start, end))

    def stub_span(self) -> tuple[float, float]:
        """Start and end of the PEC stub along the exit axis, low first."""
        gap_lo, gap_hi = self.gap_span()
        far = (self.x_mm if self.axis == 0 else self.y_mm) + self.sign * (
            self.gap_mm + STUB_LENGTH_MM)
        outer = gap_hi if self.sign > 0 else gap_lo
        return (min(outer, far), max(outer, far))

    def across_span(self) -> tuple[float, float]:
        """Extent across the exit direction."""
        centre = self.y_mm if self.axis == 0 else self.x_mm
        half = self.width_mm / 2.0
        return centre - half, centre + half

    def required_lines(self) -> list[float]:
        """Grid lines the gap needs along the exit axis.

        Four of them: the board edge, both sides of the gap, and the stub's far end. A gap
        that falls between lines contains no cells, and openEMS then applies the lumped
        element to nothing at all — with no error, exactly like the port bug §7 already
        warns about.
        """
        gap_lo, gap_hi = self.gap_span()
        stub_lo, stub_hi = self.stub_span()
        return sorted({gap_lo, gap_hi, stub_lo, stub_hi})

    def across_lines(self) -> list[float]:
        return list(self.across_span())

    def reach_mm(self) -> float:
        """How far past the board edge the domain must extend for this port."""
        return STUB_LENGTH_MM + self.gap_mm + STUB_CLEARANCE_MM


def fit(anchor, z_mm: float, gap_mm: float) -> tuple[GapPort | None, str | None]:
    """Build a gap port for one connector, or say why it cannot have one.

    Refuses rather than approximates in three cases, each of which would otherwise produce a
    confident number from a model that is not the board:

    * a connector that is not on an edge — its cable's path is a guess about the enclosure,
      not something the layout says;
    * an exit normal that is not axis-aligned — a diagonal stub has no single cell to gap,
      and rounding it to an axis moves where the current leaves;
    * a connector with no reference copper at the exit — there is nothing for the gap to
      measure against.
    """
    if not anchor.on_edge:
        return None, (
            f"{anchor.ref} is {anchor.distance_to_edge_mm:.0f} mm from the board outline, so "
            f"where its cable runs is a guess about the enclosure rather than something the "
            f"layout decides"
        )
    off_axis = min(abs(anchor.nx), abs(anchor.ny))
    if off_axis > 0.2:
        return None, (
            f"{anchor.ref} leaves the board diagonally ({anchor.nx:+.2f}, {anchor.ny:+.2f}), "
            f"and a diagonal stub has no single cell to put the gap in"
        )
    return GapPort(
        ref=anchor.ref, x_mm=anchor.x_mm, y_mm=anchor.y_mm,
        nx=anchor.nx, ny=anchor.ny, z_mm=z_mm,
        width_mm=anchor.width_mm, gap_mm=gap_mm,
    ), None
