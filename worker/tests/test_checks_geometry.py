"""The geometry the core checks stand on: which plane is whose reference, where the board's
edge is, and what counts as a gap.
"""

from __future__ import annotations

import math

import numpy as np

from emi_worker.kicad import geometry as g
from emi_worker.kicad.board import BoardModel, CopperLayer, StackupLayer, Track, ZonePolygon
from emi_worker.kicad.normalize import board_extent
from emi_worker.rules import checks
from emi_worker.rules.model import RuleContext
from emi_worker.stackup import BoardElectrics, LayerElectrics


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def board(layers=("F.Cu", "B.Cu"), outline=None) -> BoardModel:
    m = BoardModel()
    m.copper_layers = [CopperLayer(ordinal=i, name=n, kind="signal") for i, n in enumerate(layers)]
    m.outline = outline if outline is not None else [square(0, 0, 40, 40)]
    return m


def ctx_for(m, electrics=None) -> RuleContext:
    return RuleContext(model=m, transform=board_extent(m), max_frequency_hz=1e9, electrics=electrics)


# ---- shared helpers ---------------------------------------------------------------------

def test_ring_helpers():
    ring = square(0, 0, 2, 3)
    assert g.ring_area(ring) == 6.0
    assert g.ring_area(list(reversed(ring))) == 6.0
    assert g.ring_area(ring[:2]) == 0.0
    assert g.point_in_ring(1, 1, ring) and not g.point_in_ring(3, 1, ring)
    assert g.point_segment_distance(0, 1, -1, 0, 1, 0) == 1.0
    assert g.point_segment_distance(3, 0, -1, 0, 1, 0) == 2.0


def test_segment_to_edges_finds_the_closest_approach_mid_segment():
    edges = g.ring_edges([[(0, 0), (10, 0), (5, 4)]])
    d, at = g.segment_to_edges(0, 5, 10, 5, edges)
    assert math.isclose(d, 1.0) and math.isclose(at[0], 5.0) and math.isclose(at[1], 5.0)
    # Crossing an edge is distance zero.
    assert g.segment_to_edges(5, -1, 5, 1, edges)[0] == 0.0
    assert g.segment_to_edges(0, 0, 1, 1, np.zeros((0, 4)))[0] == math.inf


def test_outer_rings_leave_the_cutouts_out():
    hole = g.circle(20, 20, 1.6)
    outline = [square(0, 0, 40, 40), hole]
    assert g.outer_rings(outline) == [outline[0]]


# ---- edge proximity ---------------------------------------------------------------------

def _signal(pts, width=0.2, layer="F.Cu"):
    return Track(layer=layer, net="SIG", width_mm=width, pts=pts)


def test_a_track_whose_middle_runs_near_the_edge_is_found():
    """Only the end points used to be tested. This run is 6 mm from the edge at both ends and
    0.9 mm from a notch in the outline at its middle."""
    notch = [(0, 0), (18, 0), (20, 5), (22, 0), (40, 0), (40, 40), (0, 40)]
    m = board(outline=[notch])
    m.tracks = [_signal([(5, 6), (35, 6)])]
    f = list(checks.check_edge_proximity(ctx_for(m)))
    assert len(f) == 1 and f[0].net == "SIG"
    assert "0.90 mm" in f[0].title


def test_a_mounting_hole_is_not_the_board_edge():
    m = board(outline=[square(0, 0, 40, 40), g.circle(20, 20, 1.6)])
    m.tracks = [_signal([(10, 22.0), (30, 22.0)])]  # 0.3 mm from the hole's edge
    assert list(checks.check_edge_proximity(ctx_for(m))) == []
    m.tracks = [_signal([(10, 0.5), (30, 0.5)])]  # the real edge still counts
    assert len(list(checks.check_edge_proximity(ctx_for(m)))) == 1


# ---- reference planes -------------------------------------------------------------------

LAYERS4 = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def test_reference_planes_come_from_the_stackup_analysis():
    """Ingest decided the planes from pour coverage; the checks must not re-decide them from
    the layer type, which KiCad boards almost never set."""
    m = board(LAYERS4)
    electrics = BoardElectrics(layers={
        "F.Cu": LayerElectrics(name="F.Cu", index=0, kind="microstrip", reference_plane="In1.Cu"),
        "In1.Cu": LayerElectrics(name="In1.Cu", index=1, kind="microstrip"),
        "In2.Cu": LayerElectrics(name="In2.Cu", index=2, kind="microstrip"),
        "B.Cu": LayerElectrics(name="B.Cu", index=3, kind="microstrip", reference_plane="In2.Cu"),
    })
    assert checks._layer_pairs(ctx_for(m, electrics)) == {"F.Cu": "In1.Cu", "B.Cu": "In2.Cu"}


def test_without_the_analysis_the_nearest_plane_is_by_height_not_by_index():
    """In2.Cu sits one layer from each plane, but 0.1 mm from B.Cu and 1 mm from In1.Cu."""
    m = board(LAYERS4)
    m.stackup = [
        StackupLayer("F.Cu", "copper", 0.035), StackupLayer("p1", "prepreg", 0.2),
        StackupLayer("In1.Cu", "copper", 0.035), StackupLayer("core", "core", 1.0),
        StackupLayer("In2.Cu", "copper", 0.035), StackupLayer("p2", "prepreg", 0.1),
        StackupLayer("B.Cu", "copper", 0.035),
    ]
    m.zones = [ZonePolygon("In1.Cu", "GND", square(0, 0, 40, 40)),
               ZonePolygon("B.Cu", "GND", square(0, 0, 40, 40))]
    pairs = checks._layer_pairs(ctx_for(m))
    assert pairs["In2.Cu"] == "B.Cu"
    assert pairs["F.Cu"] == "In1.Cu"


def test_a_plane_with_no_filled_copper_is_unknown_not_a_gap():
    """Pours that were never filled leave no raster. That used to flag every trace over the
    layer as crossing a gap the length of the trace."""
    m = board(LAYERS4)
    m.zones = [ZonePolygon("In2.Cu", "GND", square(0, 0, 40, 40))]
    m.tracks = [_signal([(5, 5), (35, 5)])]
    electrics = BoardElectrics(layers={
        "F.Cu": LayerElectrics(name="F.Cu", index=0, kind="microstrip", reference_plane="In1.Cu"),
        "B.Cu": LayerElectrics(name="B.Cu", index=3, kind="microstrip", reference_plane="In2.Cu"),
    })
    ctx = ctx_for(m, electrics)
    assert list(checks.check_plane_gaps(ctx)) == []
    assert any("In1.Cu" in n and "not checked" in n for n in ctx.notes)


def test_an_impedance_finding_names_every_layer_it_misses_on():
    """The layer used to be `worst.kind and sorted(off)[0][0]`: one layer at most."""
    from emi_worker.rules import settings

    m = board()
    m.tracks = [_signal([(5, 5), (15, 5)], width=0.1, layer="F.Cu"),
                _signal([(5, 9), (15, 9)], width=0.1, layer="B.Cu")]
    le = dict(kind="microstrip", reference_plane="GND", height_mm=0.2, assumed=False)
    electrics = BoardElectrics(layers={
        "F.Cu": LayerElectrics(name="F.Cu", index=0, **le),
        "B.Cu": LayerElectrics(name="B.Cu", index=1, **le),
    })
    ctx = ctx_for(m, electrics)
    ctx.settings = settings.load(("run", {"rules": {"impedance": {"params": {"single_ended_ohm": 30}}}}))
    found = [f for f in checks.check_impedance(ctx) if f.severity != "info"]
    assert len(found) == 1
    assert found[0].layer == "B.Cu, F.Cu"
