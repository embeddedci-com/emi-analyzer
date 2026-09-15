"""What the stackup means electrically.

The board file gives thicknesses and permittivities; every check that reasons about time or
impedance needs those turned into per-layer numbers. This module is that translation, and it
is deliberately the only place it happens -- a second opinion about how fast a signal travels
on In1.Cu is how two checks come to disagree about the same board.

Two facts drive everything here:

  * A trace's speed depends on what surrounds it. On an inner layer with a plane above and
    below, the field is entirely in the dielectric and the effective permittivity is the
    material's. On an outer layer, half the field is in air, so it is markedly lower --
    about 6.9 ps/mm against 5.5 ps/mm for ordinary FR-4. That 25% difference is why length
    matching has to be done in time and not in millimetres.

  * A trace's impedance depends on how far it is from its reference plane, and which plane
    that is. The plane checks already answer "which"; this adds "how far".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .kicad.board import BoardModel, StackupLayer

#: Speed of light, mm/ns.
C_MM_NS = 299.792458

#: Used when the board file names no permittivity and none was configured. Ordinary FR-4 at
#: around 1 GHz. Anything derived from it is marked assumed -- see LayerElectrics.assumed.
DEFAULT_EPSILON_R = 4.4

#: Solder mask over an outer trace raises the effective permittivity a little, because some
#: of the field that would have been in air is now in resin. Small, but it is a systematic
#: bias rather than noise, so it is applied rather than ignored.
MASK_EPSILON_BUMP = 0.2


@dataclass(frozen=True)
class LayerElectrics:
    """Everything the checks need to know about one copper layer."""

    name: str
    index: int
    #: "microstrip" (a plane on one side only) or "stripline" (planes both sides). An outer
    #: layer with no plane anywhere under it is still called microstrip, because that is the
    #: closer approximation, but reference_plane is empty and impedance is not computed.
    kind: str
    #: The plane this layer references, and the dielectric between them.
    reference_plane: str = ""
    height_mm: float = 0.0
    epsilon_r: float = DEFAULT_EPSILON_R
    #: For stripline, the distance to the plane on the other side. Zero for microstrip.
    height_above_mm: float = 0.0
    copper_thickness_mm: float = 0.035
    #: True when any of the above rests on a default rather than on the board file. Findings
    #: derived from an assumed number have to say so.
    assumed: bool = True

    @property
    def epsilon_eff(self) -> float:
        """Effective permittivity: what the field actually travels through.

        Stripline sees the dielectric and nothing else. Microstrip splits its field between
        the dielectric and the air above, and the usual first-order approximation for the
        ratio is the one used here; it is good to a few percent for the trace geometries
        boards actually use, which is well inside the error the assumed epsilon_r brings.
        """
        if self.kind == "stripline":
            return self.epsilon_r
        return 0.475 * self.epsilon_r + 0.67

    @property
    def ps_per_mm(self) -> float:
        """Propagation delay. The number length matching is actually about."""
        return 1000.0 * math.sqrt(self.epsilon_eff) / C_MM_NS

    @property
    def velocity_factor(self) -> float:
        """Fraction of c, for the wavelength checks."""
        return 1.0 / math.sqrt(self.epsilon_eff)


@dataclass(frozen=True)
class BoardElectrics:
    layers: dict[str, LayerElectrics]
    #: Vias are not free. A through via in a 1.6 mm board is a couple of picoseconds, which
    #: is inside the budget of a DDR byte lane -- so a net with three more vias than its
    #: neighbour is skewed, and saying otherwise would be a rounding error with consequences.
    via_ps: float = 2.0
    #: True when no dielectric permittivity came from the board file at all.
    epsilon_assumed: bool = True
    notes: tuple[str, ...] = ()

    def layer(self, name: str) -> LayerElectrics | None:
        return self.layers.get(name)

    def ps_per_mm(self, layer: str) -> float:
        e = self.layers.get(layer)
        return e.ps_per_mm if e else 6.0

    @property
    def mean_velocity_factor(self) -> float:
        """One number for checks that are about the board rather than about a layer."""
        if not self.layers:
            return 0.5
        return sum(e.velocity_factor for e in self.layers.values()) / len(self.layers)


def _copper_and_dielectrics(model: BoardModel) -> list[StackupLayer]:
    return [s for s in model.stackup if s.is_copper or s.is_dielectric or s.thickness_mm > 0]


def analyse(
    model: BoardModel,
    plane_layers: set[str],
    *,
    epsilon_override: float = 0.0,
    via_ps: float = 2.0,
) -> BoardElectrics:
    """Work out the electrical properties of each copper layer.

    ``plane_layers`` is which layers are reference planes, decided elsewhere -- the rules
    tier resolves it from pour coverage and layer type, and passing it in keeps one answer
    rather than two.
    """
    notes: list[str] = []
    entries = _copper_and_dielectrics(model)
    coppers = [s for s in entries if s.is_copper]
    if not coppers:
        return BoardElectrics(layers={}, via_ps=via_ps, notes=("no copper layers in the stackup",))

    from_file = any(s.epsilon_r > 0 and s.from_file for s in entries if s.is_dielectric)
    if epsilon_override > 0:
        from_file = True
        notes.append(f"permittivity forced to {epsilon_override} by settings")
    elif not from_file:
        notes.append(
            f"no dielectric permittivity in the board file; assuming {DEFAULT_EPSILON_R}. "
            "Delays and impedances below carry that assumption."
        )

    # Walk the stack once, so every layer knows what sits above and below it.
    order = [s.name for s in entries]
    idx = {name: i for i, name in enumerate(order)}
    out: dict[str, LayerElectrics] = {}

    for ci, copper in enumerate(coppers):
        i = idx[copper.name]
        below = _nearest_plane(entries, i, +1, plane_layers)
        above = _nearest_plane(entries, i, -1, plane_layers)

        # A plane on both sides is stripline; one side, microstrip. A plane layer itself is
        # not a signal layer, but it can still carry traces, and treating it as stripline
        # when it has a neighbour plane is the right approximation.
        if below and above:
            kind = "stripline"
            near, far = (below, above) if below[1] <= above[1] else (above, below)
        elif below or above:
            kind = "microstrip"
            near, far = (below or above), None  # type: ignore[assignment]
        else:
            kind = "microstrip"
            near, far = None, None  # type: ignore[assignment]

        eps = epsilon_override or (near[2] if near and near[2] > 0 else 0.0) or DEFAULT_EPSILON_R
        # An outer layer is covered by solder mask; an inner one is not.
        outer = ci == 0 or ci == len(coppers) - 1
        if kind == "microstrip" and outer and _has_mask(entries, i):
            eps += MASK_EPSILON_BUMP

        out[copper.name] = LayerElectrics(
            name=copper.name,
            index=ci,
            kind=kind,
            reference_plane=near[0] if near else "",
            height_mm=near[1] if near else 0.0,
            height_above_mm=far[1] if far else 0.0,
            epsilon_r=eps,
            copper_thickness_mm=copper.thickness_mm or 0.035,
            assumed=not from_file,
        )

    return BoardElectrics(
        layers=out, via_ps=via_ps, epsilon_assumed=not from_file, notes=tuple(notes)
    )


def _nearest_plane(
    entries: list[StackupLayer], start: int, step: int, planes: set[str]
) -> tuple[str, float, float] | None:
    """The closest reference plane in one direction: (name, distance mm, dielectric epsilon).

    The permittivity returned is that of the dielectric actually between the two, not a
    board-wide average -- a board with a thin high-Dk core under the top layer and ordinary
    prepreg elsewhere behaves differently on each.
    """
    dist = 0.0
    eps = 0.0
    i = start + step
    while 0 <= i < len(entries):
        s = entries[i]
        if s.is_copper:
            if s.name in planes:
                return s.name, dist + s.thickness_mm / 2, eps
            # A copper layer that is not a plane blocks nothing electrically, but it does
            # mean the plane beyond it is not this layer's reference.
            return None
        dist += s.thickness_mm
        if s.is_dielectric and s.epsilon_r > 0 and eps <= 0:
            eps = s.epsilon_r
        i += step
    return None


def _has_mask(entries: list[StackupLayer], i: int) -> bool:
    for step in (-1, 1):
        j = i + step
        if 0 <= j < len(entries) and "mask" in entries[j].type.lower():
            return True
    return False
