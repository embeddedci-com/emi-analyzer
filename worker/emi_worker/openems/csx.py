"""Build openEMS CSX XML documents.

We drive openEMS through its XML interface rather than its Python bindings: Debian ships
the binaries without the Python module, and the XML is stable across versions in a way the
Python API is not. See ``docs/csx-xml-notes.md`` for the schema, which was verified against
openEMS v0.0.35 / CSXCAD v0.6.2 by reading back ``--debug-CSX``.

This module knows the file format and nothing about PCBs. Turning a board into one of these
documents is model.py's job.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Sequence

#: Every coordinate in the document is in millimetres, matching board.json, so nothing
#: converts units anywhere between the KiCad file and the solver.
DELTA_UNIT_MM = 0.001

#: Priorities. Overlaps resolve to the higher number, so metal has to outrank the
#: dielectric it sits on, and ports have to outrank both.
PRIORITY_DIELECTRIC = 0
PRIORITY_METAL = 10
PRIORITY_PORT = 20


def _fmt(v: float) -> str:
    """Compact but unambiguous. %g would print 1e-05 as 1e-05, which openEMS reads fine."""
    return f"{v:.9g}"


def _lines(values: Sequence[float]) -> str:
    return ",".join(_fmt(v) for v in values)


@dataclass
class Excitation:
    """The source signal (not where it is applied — that is ExcitationProperty)."""

    #: 0 = Gaussian pulse, the broadband excitation every P2 run uses.
    type: int = 0
    f0: float = 0.0
    fc: float = 0.0

    def attrs(self) -> dict[str, str]:
        return {"Type": str(self.type), "f0": _fmt(self.f0), "fc": _fmt(self.fc)}


@dataclass
class Boundaries:
    """Per-face boundary condition.

    PML_8 absorbs; PEC reflects. A PML face needs at least 8 grid lines beyond the region
    of interest or openEMS silently substitutes PEC and warns — and a run that ignores that
    warning is measuring an echo chamber, not a board.
    """

    xmin: str = "PML_8"
    xmax: str = "PML_8"
    ymin: str = "PML_8"
    ymax: str = "PML_8"
    zmin: str = "PML_8"
    zmax: str = "PML_8"

    def attrs(self) -> dict[str, str]:
        return {
            "xmin": self.xmin, "xmax": self.xmax,
            "ymin": self.ymin, "ymax": self.ymax,
            "zmin": self.zmin, "zmax": self.zmax,
        }

    def pml_axes(self) -> list[tuple[int, bool, bool]]:
        """(axis, min_is_pml, max_is_pml) per axis, for mesh padding checks."""
        pairs = [(self.xmin, self.xmax), (self.ymin, self.ymax), (self.zmin, self.zmax)]
        return [
            (i, lo.startswith("PML"), hi.startswith("PML"))
            for i, (lo, hi) in enumerate(pairs)
        ]


# ---- primitives ----------------------------------------------------------------------


class Primitive:
    def to_xml(self) -> ET.Element:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class Box(Primitive):
    p1: tuple[float, float, float]
    p2: tuple[float, float, float]
    priority: int = PRIORITY_DIELECTRIC

    def to_xml(self) -> ET.Element:
        el = ET.Element("Box", {"Priority": str(self.priority)})
        ET.SubElement(el, "P1", {k: _fmt(v) for k, v in zip("XYZ", self.p1)})
        ET.SubElement(el, "P2", {k: _fmt(v) for k, v in zip("XYZ", self.p2)})
        return el


@dataclass
class Polygon(Primitive):
    """A flat polygon on a plane normal to one axis.

    Copper is modelled with zero thickness. Extruding 35 um of copper would force the
    vertical mesh finer than the copper itself, and since the timestep is pinned by the
    smallest cell in any axis, that multiplies the cost of the whole run for a detail that
    barely moves the answer.
    """

    #: In-plane vertices, as the two coordinates that are not the normal axis.
    vertices: Sequence[tuple[float, float]]
    elevation: float
    norm_dir: int = 2  # 2 = z, i.e. lying in the xy plane, which every copper layer does
    priority: int = PRIORITY_METAL

    def to_xml(self) -> ET.Element:
        el = ET.Element("Polygon", {
            "Priority": str(self.priority),
            "NormDir": str(self.norm_dir),
            "Elevation": _fmt(self.elevation),
        })
        for x1, x2 in self.vertices:
            # Vertices use X1/X2, not X/Y -- they are in-plane coordinates whose meaning
            # depends on NormDir.
            ET.SubElement(el, "Vertex", {"X1": _fmt(x1), "X2": _fmt(x2)})
        return el


# ---- properties ----------------------------------------------------------------------


class Property:
    name: str

    def to_xml(self) -> ET.Element:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def _with_primitives(el: ET.Element, prims: Iterable[Primitive]) -> ET.Element:
        prim_el = ET.SubElement(el, "Primitives")
        for p in prims:
            prim_el.append(p.to_xml())
        return el


@dataclass
class Material(Property):
    """A dielectric. Values live in a child <Property> element."""

    name: str
    epsilon: float = 1.0
    mue: float = 1.0
    #: Conductivity in S/m. For a lossy dielectric this is derived from the loss tangent:
    #: kappa = 2*pi*f * eps0 * eps_r * tan_d, evaluated at the frequency of interest.
    kappa: float = 0.0
    primitives: list[Primitive] = field(default_factory=list)

    def to_xml(self) -> ET.Element:
        el = ET.Element("Material", {"Name": self.name, "Isotropy": "1"})
        ET.SubElement(el, "Property", {
            "Epsilon": _fmt(self.epsilon),
            "Mue": _fmt(self.mue),
            "Kappa": _fmt(self.kappa),
        })
        return self._with_primitives(el, self.primitives)


@dataclass
class Metal(Property):
    """A perfect electric conductor."""

    name: str
    primitives: list[Primitive] = field(default_factory=list)

    def to_xml(self) -> ET.Element:
        el = ET.Element("Metal", {"Name": self.name})
        return self._with_primitives(el, self.primitives)


@dataclass
class ExcitationProperty(Property):
    """Where the source signal is applied.

    Note that ``Excite`` is an attribute on this element, unlike Material's values which
    live in a child <Property>. Getting that wrong is silent: the property parses as
    Unknown, openEMS warns "no excitation properties found" and then runs to completion
    producing a field of zeros.
    """

    name: str
    excite: tuple[float, float, float]
    number: int = 0
    type: int = 0  # 0 = soft E-field excitation
    primitives: list[Primitive] = field(default_factory=list)

    def to_xml(self) -> ET.Element:
        el = ET.Element("Excitation", {
            "Name": self.name,
            "Number": str(self.number),
            "Type": str(self.type),
            "Excite": ",".join(_fmt(v) for v in self.excite),
        })
        return self._with_primitives(el, self.primitives)


@dataclass
class LumpedElement(Property):
    """A lumped R, C or L across one cell.

    ``Caps=1`` adds PEC caps so the element actually connects to the metal either side of
    it; without them it floats and the port impedance is meaningless.

    **The shipped solver has no series R-L-C.** M0 established this against the source and
    against the shipped binary: CSXCAD 0.6.2 reads ``R``, ``C``, ``L``, ``Direction`` and
    ``Caps`` and nothing else, so whatever combination of the three is set is wired in
    **parallel**. Upstream master adds ``LEtype`` with ``PARALLEL = 0`` and ``SERIES = 1``.

    That matters because a capacitor model is a *series* R-L-C -- ESR and ESL in series with
    C -- and a parallel element with all three set is a different circuit at every frequency,
    not an approximation of the right one. Only one value is written here unless the caller
    explicitly asks for more, and ``series_rlc`` composes the real thing from three
    single-value elements in adjacent cells.

    A value left at ``None`` is omitted rather than written as zero: CSXCAD treats a missing
    attribute as "not this kind of element", while ``C="0"`` is a capacitor of zero farads,
    which is an open circuit.
    """

    name: str
    direction: int  # axis index: 0=x, 1=y, 2=z
    resistance: float | None = 50.0
    capacitance: float | None = None
    inductance: float | None = None
    caps: bool = True
    primitives: list[Primitive] = field(default_factory=list)

    def to_xml(self) -> ET.Element:
        attrs = {
            "Name": self.name,
            "Direction": str(self.direction),
            "Caps": "1" if self.caps else "0",
        }
        if self.resistance is not None:
            attrs["R"] = _fmt(self.resistance)
        if self.capacitance is not None:
            attrs["C"] = _fmt(self.capacitance)
        if self.inductance is not None:
            attrs["L"] = _fmt(self.inductance)
        if len(attrs) == 3:
            raise ValueError(
                f"lumped element {self.name!r} sets no R, C or L, so it is not an element at "
                f"all; openEMS would read it and change nothing"
            )
        el = ET.Element("LumpedElement", attrs)
        return self._with_primitives(el, self.primitives)


def series_rlc(
    name: str,
    direction: int,
    *,
    resistance: float,
    inductance: float,
    capacitance: float,
    cells: Sequence[tuple[tuple[float, float, float], tuple[float, float, float]]],
    caps: bool = True,
) -> list["LumpedElement"]:
    """A series R-L-C built from three single-value elements in adjacent cells.

    The shipped CSXCAD wires R, C and L in parallel (see ``LumpedElement``), so a real
    capacitor model -- ESR and ESL in series with C -- cannot be one element. Three elements
    in series along the gap between the pads are the same circuit, and they work on the
    solver that is actually installed rather than one that has to be built from source.

    ``cells`` gives the three (p1, p2) boxes, in order, along ``direction``. They must be
    adjacent: a gap between them is PEC and shorts nothing, but an overlap makes two elements
    share a cell and openEMS applies only one of them.

    A 0402 pad gap is around 0.5 mm against an in-plane mesh near 50 um, so there is room for
    three cells; a caller that cannot find three should refine the mesh rather than collapse
    the model into a parallel element that is wrong everywhere.
    """
    if len(cells) != 3:
        raise ValueError(
            f"a series R-L-C needs exactly three adjacent cells, got {len(cells)}. Refine "
            f"the mesh across the pad gap rather than approximating it with fewer"
        )
    values = (("r", resistance), ("l", inductance), ("c", capacitance))
    out: list[LumpedElement] = []
    for (suffix, value), (p1, p2) in zip(values, cells):
        kwargs: dict = {"resistance": None, "capacitance": None, "inductance": None}
        kwargs[{"r": "resistance", "l": "inductance", "c": "capacitance"}[suffix]] = value
        out.append(LumpedElement(
            name=f"{name}_{suffix}", direction=direction, caps=caps,
            primitives=[Box(p1=p1, p2=p2, priority=PRIORITY_PORT)], **kwargs,
        ))
    return out


@dataclass
class ProbeBox(Property):
    """A voltage (Type 0) or current (Type 1) probe.

    Output goes to a plain text file named after the probe, not to HDF5.
    """

    name: str
    type: int  # 0 = voltage (integrate E), 1 = current (integrate H)
    norm_dir: int
    weight: float = 1.0
    primitives: list[Primitive] = field(default_factory=list)

    def to_xml(self) -> ET.Element:
        el = ET.Element("ProbeBox", {
            "Name": self.name,
            "Type": str(self.type),
            "Weight": _fmt(self.weight),
            "NormDir": str(self.norm_dir),
        })
        return self._with_primitives(el, self.primitives)


#: DumpType values. Time-domain types are 0-3; add 10 for the frequency domain.
DUMP_E_TIME = 0
DUMP_H_TIME = 1
DUMP_J_TIME = 2
DUMP_E_FREQ = 10
DUMP_H_FREQ = 11
#: Conduction current density, frequency domain. **Not** what you want for copper: this is
#: J = kappa * E, which is identically zero inside a perfect conductor, because openEMS
#: models PEC by forcing E to zero there. A dump of this type placed on a copper layer
#: returns an all-zero map, and placed just off it returns the dielectric's loss current,
#: which looks plausible and means nothing. It is for lossy materials.
DUMP_J_FREQ = 12

#: Total current density (conduction + displacement), frequency domain.
DUMP_TOTAL_CURRENT_FREQ = 13


@dataclass
class DumpBox(Property):
    """Field output.

    Always give ``frequencies``. A time-domain dump writes every timestep and reaches
    hundreds of gigabytes; a frequency-domain dump writes one complex field per named
    frequency and lands in megabytes. That single choice is the difference between a
    feature that works over HTTP and one that does not.
    """

    name: str
    dump_type: int = DUMP_J_FREQ
    #: 0 = raw grid values, 1 = node interpolation, 2 = cell interpolation.
    #:
    #: **Node interpolation for anything sampled on copper.** Cell interpolation reports at
    #: cell *centres*, which for a plane placed on a copper layer means sampling half a cell
    #: away from the metal — and surface current exists only on the conductor, so the map
    #: comes back all zeros. Measured on a real board: mode 2 sampled 1.5336 mm for a layer
    #: at 1.5825 mm; mode 1 sampled 1.5825 mm exactly.
    dump_mode: int = 1
    file_type: int = 1  # HDF5
    frequencies: Sequence[float] = ()
    #: Record every Nth grid line in each axis. 1 writes the full mesh.
    #:
    #: For an NF2FF surface this is close to free: the transform only needs the face sampled
    #: finely against the **wavelength**, which at 1 GHz is 300 mm in air, while the board mesh
    #: is set by copper geometry. M0 measured the difference it makes to the artifact — six
    #: faces of E and H at 60 frequencies are 1.1-4.0 GB at full resolution and 0.07-0.25 GB at
    #: a quarter, and even a quarter of a 50 µm mesh is orders finer than the λ/10 any
    #: transform asks for.
    sub_sampling: int = 1
    primitives: list[Primitive] = field(default_factory=list)

    def to_xml(self) -> ET.Element:
        el = ET.Element("DumpBox", {
            "Name": self.name,
            "DumpType": str(self.dump_type),
            "DumpMode": str(self.dump_mode),
            "FileType": str(self.file_type),
        })
        if self.sub_sampling != 1:
            if self.sub_sampling < 1:
                raise ValueError(
                    f"dump {self.name!r} asks for sub-sampling {self.sub_sampling}; it is a "
                    f"stride in grid lines, so it cannot be less than 1"
                )
            n = str(int(self.sub_sampling))
            el.set("SubSampling", f"{n},{n},{n}")
        self._with_primitives(el, self.primitives)
        if self.frequencies:
            ET.SubElement(el, "FD_Samples").text = _lines(self.frequencies)
        elif self.dump_type >= 10:
            raise ValueError(
                f"dump {self.name!r} is a frequency-domain type but names no frequencies"
            )
        return el


# ---- document ------------------------------------------------------------------------


@dataclass
class CSXDocument:
    """A complete openEMS input file."""

    excitation: Excitation
    x_lines: Sequence[float]
    y_lines: Sequence[float]
    z_lines: Sequence[float]
    properties: list[Property] = field(default_factory=list)
    boundaries: Boundaries = field(default_factory=Boundaries)
    max_timesteps: int = 30000
    #: Energy decay at which the run stops early. 1e-4 is -40 dB, the usual convergence bar.
    end_criteria: float = 1e-4
    f_max: float = 0.0

    def add(self, prop: Property) -> Property:
        self.properties.append(prop)
        return prop

    def validate(self) -> list[str]:
        """Problems worth refusing to run over.

        openEMS reports most of these as warnings and then completes successfully, which is
        the worst possible failure mode: a plausible-looking result computed from a model
        that is not the one the user asked for.
        """
        problems: list[str] = []

        if not any(isinstance(p, ExcitationProperty) for p in self.properties):
            problems.append(
                "no excitation property: openEMS would warn and then produce a field of zeros"
            )

        counts = (len(self.x_lines), len(self.y_lines), len(self.z_lines))
        for axis, (lo_pml, hi_pml) in enumerate(
            [(b, c) for _, b, c in self.boundaries.pml_axes()]
        ):
            needed = (8 if lo_pml else 0) + (8 if hi_pml else 0)
            if counts[axis] < needed + 2:
                problems.append(
                    f"axis {'xyz'[axis]} has {counts[axis]} grid lines but its PML "
                    f"boundaries need at least {needed + 2}; openEMS would fall back to a "
                    f"reflecting PEC wall"
                )

        for axis, lines in zip("xyz", (self.x_lines, self.y_lines, self.z_lines)):
            if len(lines) < 2:
                problems.append(f"axis {axis} has fewer than two grid lines")
            elif any(b <= a for a, b in zip(lines, lines[1:])):
                problems.append(f"axis {axis} grid lines are not strictly increasing")

        if self.f_max <= 0:
            problems.append("f_max must be positive")

        return problems

    def to_xml(self) -> ET.Element:
        root = ET.Element("openEMS")

        fdtd = ET.SubElement(root, "FDTD", {
            "NumberOfTimesteps": str(int(self.max_timesteps)),
            "endCriteria": _fmt(self.end_criteria),
            "f_max": _fmt(self.f_max),
        })
        ET.SubElement(fdtd, "Excitation", self.excitation.attrs())
        ET.SubElement(fdtd, "BoundaryCond", self.boundaries.attrs())

        cs = ET.SubElement(root, "ContinuousStructure", {"CoordSystem": "0"})
        props = ET.SubElement(cs, "Properties")
        for p in self.properties:
            props.append(p.to_xml())

        grid = ET.SubElement(cs, "RectilinearGrid", {
            "DeltaUnit": _fmt(DELTA_UNIT_MM),
            "CoordSystem": "0",
        })
        ET.SubElement(grid, "XLines").text = _lines(self.x_lines)
        ET.SubElement(grid, "YLines").text = _lines(self.y_lines)
        ET.SubElement(grid, "ZLines").text = _lines(self.z_lines)

        return root

    def to_string(self) -> str:
        root = self.to_xml()
        ET.indent(root, space="  ")
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
            root, encoding="unicode"
        )

    def cell_count(self) -> int:
        return len(self.x_lines) * len(self.y_lines) * len(self.z_lines)
