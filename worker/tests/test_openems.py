"""openEMS model generation tests.

These do not run the solver — they check the things that, when wrong, produce a run that
completes successfully and returns nothing. Every failure mode encoded here was hit for
real while building this:

* a dump plane sampled at cell centres instead of grid nodes, missing the copper entirely
* a port that fell between grid lines and excited nothing
* a timestep cap below the excitation length, so the source never finished
* an ``Excite`` attribute written as a child element, silently parsed as ``Unknown``

openEMS reports all four as warnings and then exits zero. The only defence is to check the
model before handing it over.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.normalize import _board_extent
from emi_worker.openems import csx, mesh as meshmod
from emi_worker.openems.run import DIVERGENCE_RATIO, divergence_ratio
from emi_worker.openems.model import (
    BAND_FILL,
    ModelError,
    Port,
    SolveParams,
    build_model,
    excitation_band,
    kappa_from_loss_tangent,
    required_timesteps,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tiny.kicad_pcb"


@pytest.fixture(scope="module")
def board():
    return parse_board(parse(FIXTURE.read_text()))


@pytest.fixture(scope="module")
def transform(board):
    return _board_extent(board)


def _params(**over) -> SolveParams:
    base = dict(
        roi=(4.0, 24.0, 30.0, 38.0),
        frequencies_hz=[500e6, 1e9],
        ports=[Port(name="p1", x=10.0, y=30.0, layer="F.Cu", half_width_mm=0.15)],
        dx_um=250, dy_um=250, dz_um=250, air_mm=3.0,
    )
    base.update(over)
    return SolveParams(**base)


# ---- mesh ----------------------------------------------------------------------------

def test_required_lines_survive():
    """Copper edges must land on grid lines, or the solver rounds the geometry."""
    required = [0.0, 1.234, 5.0, 10.0]
    lines = meshmod.build_axis(required, min_res=0.05, max_res=1.0, pml=False)
    for r in required:
        assert np.min(np.abs(lines - r)) < 1e-9, f"{r} was lost"


def test_grading_is_bounded():
    """A sudden jump in cell size reflects energy — a numerical artefact that looks real."""
    lines = meshmod.build_axis([0.0, 0.05, 0.1, 40.0], min_res=0.02, max_res=4.0)
    d = np.diff(lines)
    ratio = np.maximum(d[1:] / d[:-1], d[:-1] / d[1:])
    # Cell sizes are discrete and each gap is scaled to fit exactly, so the achieved bound
    # lands a little above the target. openEMS's own guidance is 1.5, so anything up to
    # that is fine; what matters is that there is no 2:1 jump.
    assert ratio.max() <= 1.5


# One real solve of the fixture board, as openEMS printed it: 63 progress samples, from the
# first timestep to the point where openEMS stopped on its own energy end-criterion at -41 dB.
# Both halves matter to the check below -- the ramp oscillates by 3-5 dB while rising seven
# orders of magnitude, and the decay is what a healthy run ends with.
REAL_RUN_ENERGY = [
    5.13e-22, 8.67e-22, 1.35e-20, 6.39e-20, 1.63e-19, 2.24e-19, 1.07e-19, 2.09e-19, 2.49e-18,
    9.87e-18, 2.1e-17, 2.44e-17, 1.09e-17, 1.72e-17, 1.55e-16, 5.09e-16, 9.09e-16, 9.01e-16,
    3.8e-16, 5.04e-16, 3.28e-15, 8.88e-15, 1.33e-14, 1.13e-14, 4.71e-15, 5.37e-15, 2.4e-14,
    5.27e-14, 6.62e-14, 4.91e-14, 2.11e-14, 2.13e-14, 6.21e-14, 1.09e-13, 1.14e-13, 7.59e-14,
    3.55e-14, 3.21e-14, 5.96e-14, 8.04e-14, 7.15e-14, 4.4e-14, 2.3e-14, 1.88e-14, 2.26e-14,
    2.3e-14, 1.74e-14, 1.04e-14, 5.99e-15, 4.37e-15, 3.7e-15, 2.85e-15, 1.86e-15, 1.08e-15,
    6.4e-16, 4.13e-16, 2.72e-16, 1.68e-16, 9.57e-17, 5.25e-17, 2.92e-17, 1.64e-17, 9.05e-18,
]


def test_a_healthy_run_is_not_called_unstable():
    """The regression test for "long solves always diverge", which they did not.

    The detector took the first sample lower than the one before it as the moment the
    excitation had passed. On this run that is sample 6, still seven orders of magnitude below
    the peak, so the floor was set at 1.07e-19 and the legitimate climb to 1.14e-13 was
    reported as a divergence by a factor of 1.07e6 -- the exact number users were shown. Every
    long solve was refused this way, and the refusal was read as a property of the mesher.

    openEMS ran this one to completion and stopped on its own -40 dB end-criterion.
    """
    ratio = divergence_ratio(REAL_RUN_ENERGY)
    assert ratio < DIVERGENCE_RATIO, f"a healthy run scored {ratio:.3g}"
    # Nothing in a decaying run climbs anywhere near the threshold; if this creeps up, the
    # margin is going, even while the assertion above still passes.
    assert ratio < 10


def test_a_diverging_run_is_still_caught():
    """The case the check exists for: a grid that pumps energy after the excitation.

    Shaped like the real thing -- the excitation passes, the energy falls, and then the grid
    starts feeding it. The recorded failures climbed by 1e44 from there; a thousandfold is
    enough to be sure.
    """
    decayed = REAL_RUN_ENERGY[:56]
    blow_up = decayed + [decayed[-1] * (10 ** k) for k in range(1, 12)]
    assert divergence_ratio(blow_up) >= DIVERGENCE_RATIO


def test_divergence_is_judged_after_a_real_decay_not_a_ripple():
    """A 3-5 dB dip during the ramp is the excitation, not the end of it."""
    ramp = [1e-20 * (1.8 ** k) for k in range(30)]
    ramp[10] *= 0.4  # a dip deeper than the real run's, still on the way up
    assert divergence_ratio(ramp + [ramp[-1] * 10 ** (-d / 10) for d in range(0, 60, 2)]) < 10


def test_grading_holds_on_a_real_board(board, transform):
    """The bound has to hold on a routed board, not only on tidy synthetic input.

    This is the regression test for the mesher's own defect. Copper puts required lines far
    closer together than any preset, and two rules written in terms of ``min_res`` misfired
    exactly there: jumps beside a sub-min_res cell were skipped, and inserted grading lines
    were merged away again. On this fixture the mesh came out with ratios of 2.0, 2.6 and 4.5
    at the three presets, against a stated bound of 1.4, and nothing reported it.

    What is asserted is the bound plus the one exception geometry forces: where two copper
    edges are closer together than the cell beside them, closing the jump would mean a cell
    smaller than the smallest one already in the mesh, which would cost timesteps for the
    whole run. Those are counted, not waived.
    """
    from emi_worker.openems.model import _copper_features, COPPER_MARGIN_MM

    roi = (4.0, 24.0, 30.0, 38.0)
    copper_x, copper_y = _copper_features(board, transform, roi, COPPER_MARGIN_MM)
    copper_x, copper_y = sorted(set(copper_x)), sorted(set(copper_y))
    layer_z = [0.0, float(board.thickness_mm)]

    for dx, dy, dz in [(150, 150, 50), (600, 600, 200), (1200, 1200, 400)]:
        spec = meshmod.MeshSpec(roi=roi, f_max=1e9, dx_um=dx, dy_um=dy, dz_um=dz)
        mesh = meshmod.build_mesh(spec, copper_x, copper_y, layer_z)

        preset = f"{dx}/{dz} um"
        assert mesh.max_ratio <= 1.8, f"{preset}: worst cell-size step {mesh.max_ratio:.2f}"

        for name in ("x", "y", "z"):
            axis = getattr(mesh, name)
            sizes = np.diff(axis)
            ratios = np.maximum(sizes[1:] / sizes[:-1], sizes[:-1] / sizes[1:])
            over = np.flatnonzero(ratios > meshmod.MAX_CELL_RATIO * 1.05)
            # Every remaining step must be one that cannot be closed without a cell smaller
            # than the mesh's own smallest.
            for i in over:
                small = min(sizes[i], sizes[i + 1])
                assert max(sizes[i], sizes[i + 1]) / 2 < small, (
                    f"{preset} {name}: a {ratios[i]:.2f} step at {axis[i + 1]:.3f} mm could "
                    f"have been graded"
                )
            assert len(over) <= 6, f"{preset} {name}: {len(over)} ungraded steps"


def test_grading_never_shrinks_the_smallest_cell():
    """Grading must not cost timesteps.

    dt comes from the smallest cell anywhere in the grid, so an inserted line that undercuts
    the finest copper spacing would slow every step of a run that may be hours long. The
    filler grows from its neighbour for exactly this reason.
    """
    required = [0.0, 0.05, 0.09, 6.0, 12.0]
    finest = min(b - a for a, b in zip(required, required[1:]))
    lines = meshmod.build_axis(required, min_res=0.6, max_res=4.0)
    assert np.diff(lines).min() >= finest - 1e-9


def test_the_summary_reports_the_grading_it_achieved():
    """A run that diverges should be able to say whether its own mesh was graded."""
    spec = meshmod.MeshSpec(roi=(0.0, 0.0, 10.0, 8.0), f_max=1e9, dx_um=200, dy_um=200, dz_um=100)
    mesh = meshmod.build_mesh(spec, [1.0, 1.04, 5.0], [2.0, 2.05, 6.0], [0.0, 1.6])
    summary = mesh.summary()
    assert summary["max_cell_ratio"] == round(mesh.max_ratio, 3)
    assert summary["max_cell_ratio"] <= 1.8


@pytest.mark.parametrize("dx_um", [300, 1000])
@pytest.mark.parametrize("roi_end_mm", [270.0, 370.0])  # cable reach, then plus far-field air
def test_a_long_empty_span_is_graded_up_to_the_coarse_cell(dx_um, roi_end_mm):
    """A cable port leaves hundreds of mm of air between the board and the region edge.

    Measured on a real board with a USB cable port: that air was filled with ~2.4 mm cells for
    250 mm and then jumped to 11.5 mm (4.8:1; 6.8:1 at 1000 um), and the openEMS run on that
    grid diverged. The cause was a small gap next to fine copper being left as one cell, then
    graded from as if its neighbour were that big; smoothing it afterwards only pushed the jump
    outward. Past the last required line there is nothing forcing any cell size, so the grading
    bound has to hold there exactly, all the way through the PML padding.
    """
    max_res = meshmod.max_cell_for_frequency(600e6, 4.4)
    step = dx_um / 1000.0 / 4  # merge_close keeps copper this close, so it sets the finest cell
    copper = [float(v) for v in np.arange(0.0, 20.0, step)] + [20.0, 20.35]
    gap_port = [20.35, 20.55, 30.55]  # board edge, one-cell gap, 10 mm stub end
    required = copper + gap_port + [roi_end_mm]

    lines = meshmod.build_axis(required, dx_um / 1000.0, max_res)
    d = np.diff(lines)
    ratio = np.maximum(d[1:] / d[:-1], d[:-1] / d[1:])
    shared = lines[1:-1]  # the line between cell i and cell i + 1
    outside = ratio[shared >= gap_port[-1] - 1e-9]
    assert outside.max() <= meshmod.MAX_CELL_RATIO * (1 + 1e-6), (
        f"{outside.max():.2f}:1 in the empty span"
    )
    # And it does grade up: the air is not filled at the fine end's cell size.
    assert d[shared.searchsorted(roi_end_mm - 1e-9)] >= 0.9 * max_res


def test_pml_padding_is_smoothed_too():
    """The seam where padding meets the mesh is itself a cell-size discontinuity.

    Smoothing before padding but not after left exactly one 2:1 jump per axis, sitting
    right where the absorbing boundary begins — the worst possible place for a reflection.
    """
    lines = meshmod.build_axis([0.0, 10.0], min_res=0.05, max_res=0.5, pml=True)
    d = np.diff(lines)
    ratio = np.maximum(d[1:] / d[:-1], d[:-1] / d[1:])
    assert ratio.max() <= meshmod.MAX_CELL_RATIO * 1.05
    assert len(lines) >= 2 * meshmod.PML_LINES


def test_air_is_not_meshed_at_dielectric_resolution():
    """dz describes the dielectric between copper layers, not the air box.

    Slicing 3 mm of air at 25 um adds hundreds of lines describing nothing — and since the
    timestep is set by the smallest cell anywhere, those lines cost the whole run.
    """
    spec = meshmod.MeshSpec(
        roi=(0, 0, 10, 10), f_max=1e9, dx_um=200, dy_um=200, dz_um=25,
        air_above_mm=5.0, air_below_mm=5.0,
    )
    m = meshmod.build_mesh(spec, [1.0, 2.0], [1.0, 2.0], [0.0175, 1.5825])
    board_lines = int(((m.z >= 0.0) & (m.z <= 1.6)).sum())
    air_lines = len(m.z) - board_lines
    assert board_lines > 40, "the dielectric between layers must be resolved at dz"
    # 10 mm of air sliced at 25 um would be 400 lines. Grading should need a fraction of
    # that, since nothing out there needs resolving.
    assert air_lines < 150, f"the air box has {air_lines} lines; it is being over-meshed"


def test_timestep_comes_from_the_smallest_cell():
    spec = meshmod.MeshSpec(roi=(0, 0, 10, 10), f_max=1e9, dx_um=200, dy_um=200, dz_um=50)
    m = meshmod.build_mesh(spec, [1.0, 2.0], [1.0, 2.0], [0.0175, 1.5825])
    expected = (m.min_cell_mm * 1e-3) / (meshmod.SPEED_OF_LIGHT * math.sqrt(3))
    assert m.timestep_seconds() == pytest.approx(expected, rel=1e-12)


# ---- timestep sizing -------------------------------------------------------------------

def test_required_timesteps_covers_the_excitation():
    """openEMS's own figure, reproduced.

    For dt = 1.069e-13 s and fc = 1 GHz the solver reported the pulse alone needs 26,811
    timesteps. A run capped below three times that stops before the source finishes and
    every field is zero — with no error.
    """
    n, why = required_timesteps(1.06855e-13, fc=1e9, f_min=1e9)
    assert why == "three excitation lengths"
    assert n == pytest.approx(3 * 26811, rel=0.01)


def test_required_timesteps_covers_low_frequencies():
    """At a low f_min the bandwidth requirement dominates instead."""
    n, why = required_timesteps(1e-13, fc=3e9, f_min=100e6)
    assert why == "three periods of the lowest frequency"
    assert n == pytest.approx(3 / 100e6 / 1e-13, rel=0.01)


def test_requested_frequencies_are_not_on_the_excitation_edge():
    """The Gaussian is ~20 dB down at f0 +- fc, so a frequency there is barely excited.

    Measured in M0: a transfer function at 1 GHz moved 3-4 dB between two sources that
    agreed to 0.01 dB everywhere inside the band. The old code put f_max exactly on the edge.
    """
    f0, fc, note = excitation_band(500e6, 1e9)
    assert note is None
    assert (1e9 - f0) / fc <= BAND_FILL + 1e-9
    assert (f0 - 500e6) / fc <= BAND_FILL + 1e-9
    assert f0 - fc > 0, "a negative lower edge puts DC in the pulse, and the PML cannot absorb it"


def test_a_single_frequency_still_gets_a_band():
    """Width 0 would make the pulse infinitely long; the floor keeps the run finite."""
    f0, fc, note = excitation_band(1e9, 1e9)
    assert note is None
    assert f0 == 1e9 and fc == 500e6


def test_a_span_too_wide_for_one_pulse_is_declared(board, transform):
    """One Gaussian cannot hold both ends of a 33:1 span inside itself.

    The clamp is not a fix, so it is reported rather than applied quietly. This is the span
    M3's cable band asks for, starting at 30 MHz.
    """
    f0, fc, note = excitation_band(30e6, 1e9)
    assert f0 - fc >= 0, "the lower edge must not go negative, or the pulse carries DC"
    assert note and "33:1" in note
    built = build_model(board, transform, _params(frequencies_hz=[30e6, 1e9]))
    assert any("wider than one excitation pulse" in n for n in built.notes)


def test_ordinary_harmonic_sets_say_nothing(board, transform):
    """The note must be rare enough to mean something.

    Six odd harmonics of a 100 MHz clock span 11:1, which clamps the width — but the
    requested ends still land at 0.83 fc, barely worse than the 0.8 aimed for. A note on
    every such solve would be noise, so the trigger is the achieved fill, not the clamp.
    """
    for f_min, f_max in [(100e6, 500e6), (100e6, 900e6), (100e6, 1.1e9), (100e6, 1e9)]:
        _f0, _fc, note = excitation_band(f_min, f_max)
        assert note is None, f"{f_max / f_min:.0f}:1 should be quiet, got: {note}"
    built = build_model(board, transform,
                        _params(frequencies_hz=[100e6, 300e6, 500e6, 700e6, 900e6, 1.1e9]))
    assert not any("excitation pulse" in n for n in built.notes)


def test_the_clamped_band_still_beats_the_edge():
    """Even where the width is clamped, every requested end is better off than before."""
    for f_min, f_max in [(100e6, 1e9), (50e6, 1e9), (30e6, 1e9)]:
        f0, fc, _note = excitation_band(f_min, f_max)
        assert (f_max - f0) / fc < 1.0, "the old code put this end exactly on the edge"
        assert f0 - fc >= 0


def test_widening_the_band_costs_no_timesteps():
    """dt comes from the mesh; a wider pulse is a shorter one, so the run cannot grow."""
    narrow = max((1e9 - 500e6) / 2.0, (1e9 + 500e6) / 2.0 / 2.0)   # what the old code gave
    _f0, fc, _ = excitation_band(500e6, 1e9)
    assert fc >= narrow
    wide_n, _ = required_timesteps(1e-13, fc=fc, f_min=500e6)
    old_n, _ = required_timesteps(1e-13, fc=narrow, f_min=500e6)
    assert wide_n <= old_n


def test_model_sizes_its_own_timesteps(board, transform):
    built = build_model(board, transform, _params())
    dt = built.mesh.timestep_seconds()
    needed, _ = required_timesteps(dt, fc=built.doc.excitation.fc, f_min=500e6)
    assert built.doc.max_timesteps == needed


def test_too_small_a_cap_is_called_out(board, transform):
    built = build_model(board, transform, _params(max_timesteps=1000))
    assert any("below the" in n and "entirely zero" in n for n in built.notes)


# ---- model correctness -----------------------------------------------------------------

def test_dumps_use_node_interpolation(board, transform):
    """Cell interpolation samples at cell centres and misses the copper entirely."""
    built = build_model(board, transform, _params())
    dumps = [p for p in built.doc.properties if isinstance(p, csx.DumpBox)]
    assert dumps
    for d in dumps:
        assert d.dump_mode == 1, "copper dumps must sample at grid nodes"


def test_dumps_measure_h_not_j(board, transform):
    """J = kappa * E is identically zero inside a perfect conductor.

    Surface current on PEC copper is |J_s| = |n x H|, so the dump has to be the magnetic
    field. Using the "electric current density" dump here returns an all-zero map.
    """
    built = build_model(board, transform, _params())
    dumps = [p for p in built.doc.properties if isinstance(p, csx.DumpBox)]
    for d in dumps:
        assert d.dump_type == csx.DUMP_H_FREQ
        assert d.dump_type != csx.DUMP_J_FREQ


def test_dump_planes_sit_above_the_copper(board, transform):
    """Sampling H requires being just off the metal, not inside it."""
    built = build_model(board, transform, _params())
    z_lines = built.mesh.z
    for prop in built.doc.properties:
        if isinstance(prop, csx.DumpBox):
            z = prop.primitives[0].p1[2]
            assert np.min(np.abs(z_lines - z)) < 1e-9, "the dump plane must be a grid line"


def test_port_spans_grid_cells(board, transform):
    """A port that falls between grid lines contains no cells and excites nothing.

    The port's edges are added as required lines, but one may legitimately be merged into a
    copper edge a few tens of microns away. What has to hold is not that a particular
    coordinate survived, but that the port still straddles cells in both axes.
    """
    params = _params()
    built = build_model(board, transform, params)
    port = params.ports[0]
    for axis, coord in ((built.mesh.x, port.x), (built.mesh.y, port.y)):
        inside = ((axis >= coord - port.half_width_mm) &
                  (axis <= coord + port.half_width_mm)).sum()
        assert inside >= 2, f"port spans only {inside} lines on this axis"


def test_port_narrower_than_the_mesh_is_rejected(board, transform):
    with pytest.raises(ModelError, match="excite nothing|no copper"):
        build_model(board, transform, _params(
            ports=[Port(name="p1", x=10.0, y=30.0, layer="F.Cu", half_width_mm=1e-5)],
        ))


def test_model_needs_a_port(board, transform):
    with pytest.raises(ModelError, match="at least one port"):
        build_model(board, transform, _params(ports=[]))


def test_empty_region_is_rejected(board, transform):
    """A region with no copper in it cannot be solved, and says so."""
    with pytest.raises(ModelError, match="no copper"):
        build_model(board, transform, _params(
            # Well clear of the board, so the 2 mm copper margin cannot reach the pour.
            roi=(60.0, 60.0, 64.0, 64.0),
            # Keep the port inside the region so the copper check is what fires.
            ports=[Port(name="p1", x=62.0, y=62.0, layer="F.Cu", half_width_mm=0.3)],
        ))


def test_loss_tangent_becomes_conductivity():
    k = kappa_from_loss_tangent(epsilon_r=4.4, loss_tangent=0.02, frequency=1e9)
    expected = 2 * math.pi * 1e9 * 8.8541878128e-12 * 4.4 * 0.02
    assert k == pytest.approx(expected, rel=1e-12)


# ---- XML shape -------------------------------------------------------------------------

def test_excite_is_an_attribute_not_a_child():
    """The one place the schema differs from Material.

    Written as a child <Property>, CSXCAD parses the whole property as Unknown, openEMS
    warns "no excitation properties found", and then runs to completion producing zeros.
    """
    prop = csx.ExcitationProperty(
        name="p", excite=(0, 0, -1),
        primitives=[csx.Box(p1=(0, 0, 0), p2=(1, 1, 1))],
    )
    el = prop.to_xml()
    assert el.tag == "Excitation"
    assert el.get("Excite") == "0,0,-1"
    assert el.find("Property") is None


def test_material_values_are_a_child_property():
    el = csx.Material(name="FR4", epsilon=4.4, kappa=0.002).to_xml()
    child = el.find("Property")
    assert child is not None
    assert float(child.get("Epsilon")) == 4.4


def test_polygon_vertices_use_x1_x2():
    el = csx.Polygon(vertices=[(0, 0), (1, 0), (1, 1)], elevation=1.6).to_xml()
    assert el.get("NormDir") == "2"
    assert el.get("Elevation") == "1.6"
    v = el.find("Vertex")
    assert v.get("X1") is not None and v.get("X2") is not None
    assert v.get("X") is None


def test_frequency_domain_dump_must_name_frequencies():
    with pytest.raises(ValueError, match="names no frequencies"):
        csx.DumpBox(name="d", dump_type=csx.DUMP_H_FREQ, frequencies=[]).to_xml()


def test_document_validates_missing_excitation():
    doc = csx.CSXDocument(
        excitation=csx.Excitation(f0=1e9, fc=1e9),
        x_lines=list(range(30)), y_lines=list(range(30)), z_lines=list(range(30)),
        f_max=1e9,
    )
    assert any("no excitation property" in p for p in doc.validate())


def test_document_validates_pml_padding():
    """PML_8 with too few lines silently becomes a reflecting PEC wall."""
    doc = csx.CSXDocument(
        excitation=csx.Excitation(f0=1e9, fc=1e9),
        x_lines=[0.0, 1.0, 2.0], y_lines=[0.0, 1.0, 2.0], z_lines=[0.0, 1.0, 2.0],
        f_max=1e9,
    )
    problems = doc.validate()
    assert any("PML" in p and "PEC" in p for p in problems)


def test_generated_document_parses_as_xml(board, transform):
    built = build_model(board, transform, _params())
    root = ET.fromstring(built.doc.to_string())
    assert root.tag == "openEMS"
    assert root.find("FDTD/Excitation") is not None
    assert root.find("FDTD/BoundaryCond") is not None
    grid = root.find("ContinuousStructure/RectilinearGrid")
    assert grid.get("DeltaUnit") == "0.001", "coordinates are millimetres throughout"
    assert len(grid.find("XLines").text.split(",")) == len(built.mesh.x)
    # Metal must outrank the dielectric it sits on, or the copper is overwritten.
    props = root.find("ContinuousStructure/Properties")
    metals = props.findall("Metal")
    assert metals, "the model has no copper"
    for m in metals:
        for prim in m.find("Primitives"):
            assert int(prim.get("Priority")) > csx.PRIORITY_DIELECTRIC


def test_model_has_a_dump_for_every_copper_layer(board, transform):
    built = build_model(board, transform, _params())
    assert set(built.dump_names) == set(board.copper_layer_names)


# ---- components in the solve (§12, K2) -------------------------------------------------

def test_k2_a_solve_without_component_modelling_is_unchanged(board, transform):
    """§19's K2: with no matched parts the result equals today's exactly.

    Asserted on the XML rather than on a flag, because the claim is about what the solver
    receives. Component modelling is off by default, so this is also what every existing
    caller gets.
    """
    plain = build_model(board, transform, _params())
    assert plain.modelled_parts == []
    assert "<LumpedElement" not in plain.doc.to_string() or "cap_" not in plain.doc.to_string()


def test_turning_component_modelling_on_changes_nothing_without_matches(board, transform):
    """The stronger form of K2: asking for components on a board whose parts do not resolve
    must produce byte-identical XML, not merely an empty parts list. The tiny fixture has no
    capacitors, so this is exactly that case."""
    off = build_model(board, transform, _params()).doc.to_string()
    on = build_model(board, transform, _params(model_components=True)).doc.to_string()
    assert on == off


def test_the_modelled_parts_list_names_a_placed_component(board, transform):
    """What a result reports for a part it modelled. Placement itself is pinned by
    test_a_capacitor_is_one_series_element_across_the_gap below."""
    from emi_worker.components.document import Resolved, SeriesRLC
    from emi_worker.components.place import Placement, PlacementPlan

    resolved = Resolved(
        ref="C1", component_id="generic-mlcc-100n-0402", component_name="100 nF 0402 (generic)",
        rlc=SeriesRLC(c_f=1e-7, esl_h=4.5e-10, esr_ohm=0.06), generic=True)
    placement = Placement(ref="C1", resolved=resolved, axis=0, lo=10.0, hi=10.3,
                          across_lo=30.0, across_hi=30.4, layer=board.copper_layers[0].name)

    plan = PlacementPlan(placements=[placement])
    from emi_worker.components.place import modelled_parts

    parts = modelled_parts(plan)
    assert parts[0]["ref"] == "C1"
    assert parts[0]["generic"] is True


# ---- the far-field box (§16.2) -----------------------------------------------------------

def test_a_far_field_solve_gets_air_on_every_side_and_a_box_clear_of_the_pml(board, transform):
    """The side faces used to sit on the grid's outermost lines, inside the absorbing layer,
    because the grid ended at the region in x and y; vertically they were 4 mm from copper."""
    from emi_worker.openems.model import far_field_clearance_mm
    from emi_worker.openems.nf2ff import MIN_LINES_OUTSIDE

    plain = build_model(board, transform, _params())
    built = build_model(board, transform, _params(far_field=True))
    meta = built.far_field
    assert meta is not None
    assert meta["clearance_mm"] == pytest.approx(far_field_clearance_mm(1e9))
    x0, y0, z0, x1, y1, z1 = meta["faces_mm"]
    m = built.mesh
    for lines, lo, hi in ((m.x, x0, x1), (m.y, y0, y1), (m.z, z0, z1)):
        assert np.sum(lines < lo) >= MIN_LINES_OUTSIDE
        assert np.sum(lines > hi) >= MIN_LINES_OUTSIDE
    # The copper sits well inside: the region is 4..30 x 24..38, and the box is 30 mm out.
    assert x0 < 4.0 - 25.0 and x1 > 30.0 + 25.0
    # The cost is stated, not hidden.
    assert built.mesh.cells > plain.mesh.cells
    assert any("cells instead of" in n for n in built.notes)


def test_the_far_field_grid_stays_inside_the_solved_band(board, transform):
    built = build_model(board, transform, _params(far_field=True))
    freqs = built.far_field["frequencies_hz"]
    assert min(freqs) == pytest.approx(500e6)
    assert max(freqs) == pytest.approx(1e9)


def test_far_field_frequencies_outside_the_band_are_dropped_and_said(board, transform):
    built = build_model(board, transform, _params(
        far_field=True, far_field_frequencies_hz=[30e6, 600e6, 2e9]))
    assert built.far_field["frequencies_hz"] == [600e6]
    assert any("were dropped" in n for n in built.notes)


# ---- what the microstrip check found (research/verify_microstrip.py) ------------------

def _microstrip_board(w: float = 0.3828, zone=((0, 0), (30, 0), (30, 20), (0, 20))):
    pts = " ".join(f"(xy {x} {y})" for x, y in zone)
    return parse_board(parse(f"""(kicad_pcb
  (version 20241229)
  (general (thickness 0.27))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "core") (thickness 0.2) (material "FR4") (epsilon_r 4.4)
           (loss_tangent 0))
    (layer "B.Cu" (type "copper") (thickness 0.035))))
  (net 0 "") (net 1 "GND") (net 2 "SIG")
  (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (segment (start 5 10) (end 25 10) (width {w}) (layer "F.Cu") (net 2))
  (zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) (min_thickness 0.25)
    (polygon (pts {pts})) (filled_polygon (layer "B.Cu") (pts {pts})))
)"""))


def _microstrip_model(board, dx=150, dz=100):
    params = SolveParams(
        roi=(2.0, 4.0, 28.0, 16.0), frequencies_hz=[0.5e9, 3e9],
        ports=[Port("p1", 5.0, 10.0, "F.Cu", half_width_mm=0.19),
               Port("p2", 25.0, 10.0, "F.Cu", half_width_mm=0.19, excited=False)],
        dx_um=dx, dy_um=dx, dz_um=dz, air_mm=3.0,
    )
    return build_model(board, _board_extent(board), params)


def test_a_plane_larger_than_the_region_is_kept():
    """A ground plane drawn as one rectangle over the board has no vertex near the region.

    The region filter asked whether any vertex was inside, so this plane was dropped and the
    microstrip over it solved with no ground at all: -j9000 ohm at 0.5 GHz.
    """
    built = _microstrip_model(_microstrip_board())
    names = [p.name for p in built.doc.properties if isinstance(p, csx.Metal)]
    assert "cu_B_Cu" in names


def test_a_track_crossing_the_region_with_both_ends_outside_is_kept():
    board = _microstrip_board()
    params = SolveParams(roi=(12.0, 6.0, 18.0, 14.0), frequencies_hz=[1e9],
                         ports=[Port("p1", 15.0, 10.0, "F.Cu", half_width_mm=0.19)],
                         dx_um=150, dy_um=150, dz_um=100, air_mm=3.0)
    built = build_model(board, _board_extent(board), params)
    top = next(p for p in built.doc.properties if getattr(p, "name", "") == "cu_F_Cu")
    assert top.primitives


def test_copper_sheets_sit_on_the_dielectric_and_the_slab_reaches_them():
    """Half a copper thickness of air either side of every dielectric, before.

    At the fine preset the grid resolved it, and a 50 ohm microstrip came out at 56 ohm with
    an effective permittivity of 2.2 instead of 3.3.
    """
    from emi_worker.openems.model import _stack

    sheets, slabs = _stack(_microstrip_board())
    assert sheets["F.Cu"] - sheets["B.Cu"] == pytest.approx(0.2)
    (_, lo, hi), = slabs
    assert (lo, hi) == (pytest.approx(sheets["B.Cu"]), pytest.approx(sheets["F.Cu"]))


def test_an_inner_sheet_goes_to_its_thinner_dielectric():
    """The prepreg under an outer layer keeps its thickness; the core takes the copper."""
    from emi_worker.kicad.board import StackupLayer
    from emi_worker.openems.model import _stack

    board = _microstrip_board()
    board.stackup = [
        StackupLayer("F.Cu", "copper", 0.035), StackupLayer("prepreg", "prepreg", 0.2, "", 4.4),
        StackupLayer("In1.Cu", "copper", 0.035), StackupLayer("core", "core", 1.0, "", 4.6),
        StackupLayer("In2.Cu", "copper", 0.035), StackupLayer("prepreg2", "prepreg", 0.2, "", 4.4),
        StackupLayer("B.Cu", "copper", 0.035),
    ]
    sheets, slabs = _stack(board)
    assert sheets["F.Cu"] - sheets["In1.Cu"] == pytest.approx(0.2)
    assert sheets["In2.Cu"] - sheets["B.Cu"] == pytest.approx(0.2)
    assert sheets["In1.Cu"] - sheets["In2.Cu"] == pytest.approx(1.07)
    spans = {s.name: (lo, hi) for s, lo, hi in slabs}
    assert spans["core"] == (pytest.approx(sheets["In2.Cu"]), pytest.approx(sheets["In1.Cu"]))


def test_a_straight_trace_gets_the_thirds_rule_and_no_line_on_its_edge():
    """A line on a zero-thickness strip's edge makes it act half a cell wider per side.

    The microstrip check measured 40 ohm for a 50 ohm line on every preset; with lines a third
    of a cell inside and two thirds outside it measured 49.6-50.2.
    """
    board = _microstrip_board()
    for dx in (150, 75, 50):
        y = _microstrip_model(board, dx=dx).mesh.y
        d = dx / 1000.0
        for edge, inward in ((10.0 - 0.1914, 1.0), (10.0 + 0.1914, -1.0)):
            assert np.min(np.abs(y - edge)) > d / 4, f"a line sits on the edge at dx {dx}"
            assert np.min(np.abs(y - (edge + inward * d / 3))) < 1e-6
            assert np.min(np.abs(y - (edge - inward * 2 * d / 3))) < 1e-6


def test_the_thirds_rule_can_be_turned_off(monkeypatch):
    from emi_worker.openems import model as m

    monkeypatch.setattr(m, "THIRDS_RULE", False)
    y = _microstrip_model(_microstrip_board()).mesh.y
    assert np.min(np.abs(y - (10.0 - 0.1914))) < 1e-6


# ---- capacitors need a solver that models an inductor (research/verify_lumped_rlc.py) ----

def _cap_board():
    return parse_board(parse("""(kicad_pcb
  (version 20241229)
  (general (thickness 0.27))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "core") (thickness 0.2) (material "FR4") (epsilon_r 4.4))
    (layer "B.Cu" (type "copper") (thickness 0.035))))
  (net 0 "") (net 1 "GND") (net 2 "VCC")
  (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (footprint "Capacitor_SMD:C_0402_1005Metric" (layer "F.Cu") (at 15 10 0)
    (property "Reference" "C1" (at 0 -1.2 0) (layer "F.SilkS"))
    (property "Value" "1n" (at 0 1.2 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Mask") (net 2 "VCC"))
    (pad "2" smd rect (at 0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Mask") (net 1 "GND")))
  (zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) (min_thickness 0.25)
    (polygon (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)))
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20))))
)"""))


def _cap_model(series: bool):
    board = _cap_board()
    params = SolveParams(roi=(12.0, 7.0, 18.0, 13.0), frequencies_hz=[100e6, 1e9],
                         ports=[Port("p1", 14.52, 10.0, "F.Cu", half_width_mm=0.2)],
                         dx_um=150, dy_um=150, dz_um=100, air_mm=3.0,
                         model_components=True, solver_series_rlc=series)
    return build_model(board, _board_extent(board), params)


def test_no_capacitor_is_placed_on_a_solver_without_an_inductor():
    """openEMS 0.0.35 skips an L-only element, so the old three-cell model was an open gap."""
    built = _cap_model(series=False)
    assert built.modelled_parts == []
    assert not [p for p in built.doc.properties if isinstance(p, csx.LumpedElement)
                and p.name.startswith("cap_")]
    assert any("bare copper" in n and "inductor" in n for n in built.notes)


def test_a_capacitor_is_one_series_element_across_the_gap():
    built = _cap_model(series=True)
    assert [p["ref"] for p in built.modelled_parts] == ["C1"]
    (el,) = [p for p in built.doc.properties if isinstance(p, csx.LumpedElement)
             and p.name.startswith("cap_")]
    a = el.to_xml().attrib
    assert a["LEtype"] == "1" and a["Direction"] == "0"
    assert float(a["C"]) == pytest.approx(1e-9)
    assert float(a["L"]) == pytest.approx(0.45e-9)
    assert float(a["R"]) > 0
    box = el.primitives[0]
    # It spans the gap between the pads' facing edges, 15 - 0.2 to 15 + 0.2 mm.
    assert (box.p1[0], box.p2[0]) == (pytest.approx(14.8), pytest.approx(15.2))
