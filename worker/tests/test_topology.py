"""Connectivity, delay, and the measurement that was wrong before.

`length_mm` on a net is the sum of all its copper. For a point-to-point net that happens to
equal the path length; for anything that branches it does not, and length matching is about
the branching case. These tests pin the difference.
"""

from __future__ import annotations

import math

from emi_worker import topology
from emi_worker.kicad.board import BoardModel, CopperLayer, Pad, StackupLayer, Track, Via
from emi_worker.stackup import BoardElectrics, LayerElectrics


def _pad(ref, num, net, x, y, layers=("F.Cu",)):
    return Pad(ref=ref, number=num, net=net, layers=list(layers), x=x, y=y, ring=[])


def _track(net, pts, layer="F.Cu", width=0.2):
    return Track(layer=layer, net=net, width_mm=width, pts=list(pts))


def _model(**kw):
    m = BoardModel()
    m.copper_layers = [CopperLayer(ordinal=0, name="F.Cu", kind="signal"),
                       CopperLayer(ordinal=2, name="B.Cu", kind="signal")]
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def test_a_straight_net_has_one_path():
    m = _model(
        pads=[_pad("U1", "1", "SIG", 0, 0), _pad("U2", "1", "SIG", 10, 0)],
        tracks=[_track("SIG", [(0, 0), (10, 0)])],
    )
    t = topology.build(m)["SIG"]
    assert t.kind == "point-to-point"
    assert sorted(t.pads) == ["U1.1", "U2.1"]
    assert t.path("U1.1", "U2.1").length_mm == 10.0


def test_a_branched_net_reports_paths_not_total_copper():
    """The reason this module exists.

    A trunk with two stubs -- the shape of a fly-by address net. Total copper is 20 mm and
    no signal travels 20 mm; the driver is 12 mm from one receiver and 14 from the other.
    """
    m = _model(
        pads=[
            _pad("U1", "1", "ADDR", 0, 0),
            _pad("U2", "1", "ADDR", 12, 0),
            _pad("U3", "1", "ADDR", 10, 4),
        ],
        tracks=[
            _track("ADDR", [(0, 0), (12, 0)]),
            _track("ADDR", [(10, 0), (10, 4)]),
        ],
    )
    t = topology.build(m)["ADDR"]

    assert t.kind == "branched"
    assert t.total_copper_mm == 16.0
    assert t.path("U1.1", "U2.1").length_mm == 12.0
    assert t.path("U1.1", "U3.1").length_mm == 14.0
    # And the longest path is neither the total nor the shortest branch.
    assert t.longest_path().length_mm == 14.0


def test_vias_join_layers_and_cost_time():
    m = _model(
        pads=[_pad("U1", "1", "SIG", 0, 0), _pad("U2", "1", "SIG", 10, 0, ("B.Cu",))],
        tracks=[_track("SIG", [(0, 0), (5, 0)]), _track("SIG", [(5, 0), (10, 0)], layer="B.Cu")],
        vias=[Via(x=5, y=0, size_mm=0.6, drill_mm=0.3, layers=["F.Cu", "B.Cu"], net="SIG")],
    )
    t = topology.build(m)["SIG"]
    path = t.path("U1.1", "U2.1")
    assert path.length_mm == 10.0
    assert path.vias == 1

    # Two layers at different speeds, plus the via.
    elec = BoardElectrics(
        layers={
            "F.Cu": LayerElectrics("F.Cu", 0, "microstrip", "In1.Cu", 0.2, 4.3),
            "B.Cu": LayerElectrics("B.Cu", 1, "stripline", "In2.Cu", 0.2, 4.3, height_above_mm=0.2),
        },
        via_ps=2.0,
    )
    expected = 5 * elec.ps_per_mm("F.Cu") + 5 * elec.ps_per_mm("B.Cu") + 2.0
    assert path.delay_ps(elec) == expected
    # The two layers really are different, or the test proves nothing.
    assert elec.ps_per_mm("B.Cu") > elec.ps_per_mm("F.Cu") * 1.15


def test_the_same_length_on_two_layers_is_not_the_same_delay():
    """Why the check is in picoseconds.

    Two nets of identical routed length, one outer and one inner. A millimetre-based check
    would call them matched; they are ~25% apart.
    """
    elec = BoardElectrics(layers={
        "F.Cu": LayerElectrics("F.Cu", 0, "microstrip", "In1.Cu", 0.2, 4.3),
        "In1.Cu": LayerElectrics("In1.Cu", 1, "stripline", "In2.Cu", 0.4, 4.3, height_above_mm=0.4),
    })
    outer = topology.Path("a", "b", [topology.PathRun("F.Cu", 50.0)])
    inner = topology.Path("a", "b", [topology.PathRun("In1.Cu", 50.0)])

    assert outer.length_mm == inner.length_mm
    skew = inner.delay_ps(elec) - outer.delay_ps(elec)
    assert skew > 50, f"only {skew:.1f} ps apart; the layer difference has been lost"


def test_pads_joined_only_by_a_pour_are_reachable():
    """Decoupling capacitors are connected to ground through the plane, not by a track.

    Without this every one of them looked unrouted, which on a real board was 52 pads.
    """
    from emi_worker.kicad.board import ZonePolygon

    m = _model(
        pads=[_pad("C1", "2", "GND", 5, 5), _pad("C2", "2", "GND", 20, 20)],
        zones=[ZonePolygon(layer="F.Cu", net="GND",
                           ring=[(0, 0), (30, 0), (30, 30), (0, 30)])],
    )
    t = topology.build(m)["GND"]
    assert sorted(t.pads) == ["C1.2", "C2.2"]
    assert t.unreachable == []


def test_an_unrouted_pad_is_reported_not_hidden():
    m = _model(
        pads=[_pad("U1", "1", "SIG", 0, 0), _pad("U2", "1", "SIG", 50, 50)],
        tracks=[_track("SIG", [(0, 0), (5, 0)])],
    )
    t = topology.build(m)["SIG"]
    assert t.pads == ["U1.1"]
    assert t.unreachable == ["U2.1"]


def test_joining_tolerates_a_small_gap_but_not_a_real_one():
    """Gerber-sourced boards have been through a rasteriser; KiCad ones have not."""
    near = _model(
        pads=[_pad("U1", "1", "S", 0, 0), _pad("U2", "1", "S", 10, 0)],
        tracks=[_track("S", [(0.05, 0), (10, 0)])],
    )
    assert len(topology.build(near)["S"].pads) == 2

    far = _model(
        pads=[_pad("U1", "1", "S", 0, 0), _pad("U2", "1", "S", 10, 0)],
        tracks=[_track("S", [(0.5, 0), (10, 0)])],
    )
    assert topology.build(far)["S"].unreachable == ["U1.1"]


def test_a_fixup_segment_shorter_than_the_join_tolerance_does_not_break_the_route():
    """Regression: found on a real board, where a USB pair had copper but no path.

    Corners are often cleaned up with a segment a few hundredths of a millimetre long.
    Both its ends merge into one node, and the segment after it used to be dropped because
    that node sat just outside it -- so the route fell into two pieces and no path existed.
    """
    m = _model(
        pads=[_pad("D3", "4", "S", 0.0, 0.0), _pad("D4", "3", "S", 20.0, -0.02)],
        tracks=[
            _track("S", [(0.0, 0.0), (10.0, 0.0)]),
            _track("S", [(10.0, 0.0), (10.02, -0.02)]),   # 0.028 mm, under the tolerance
            _track("S", [(10.02, -0.02), (20.0, -0.02)]),
        ],
    )
    t = topology.build(m)["S"]
    path = t.path("D3.4", "D4.3")
    assert path is not None, "the route fell apart at a short fix-up segment"
    assert abs(path.length_mm - 20.0) < 0.1
