"""Cutting a net out of a board (openems/coupon.py): what is kept, where the ports go.

A coupon is only worth solving if it keeps what the net's current returns through and drops
what it does not; these pin both halves, and the port rules the browser mirrors
(webapp/src/lib/smallPart.ts).
"""

from __future__ import annotations

import math

import pytest

from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.normalize import board_extent
from emi_worker.openems import coupon
from emi_worker.openems.model import SolveParams, _layer_z, build_model

BOARD = """(kicad_pcb
  (version 20241229)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
          (25 "Edge.Cuts" user))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "prepreg") (thickness 0.2) (epsilon_r 4.4))
    (layer "In1.Cu" (type "copper") (thickness 0.0152))
    (layer "dielectric 2" (type "core") (thickness 1.0) (epsilon_r 4.6))
    (layer "In2.Cu" (type "copper") (thickness 0.0152))
    (layer "dielectric 3" (type "prepreg") (thickness 0.2) (epsilon_r 4.4))
    (layer "B.Cu" (type "copper") (thickness 0.035))))
  (net 0 "") (net 1 "GND") (net 2 "CLK") (net 3 "OTHER") (net 4 "USB_D+") (net 5 "USB_D-")
  (gr_rect (start 0 0) (end 60 40) (layer "Edge.Cuts") (width 0.1))
  (footprint "Q" (layer "F.Cu") (at 10 20)
    (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "CLK"))
    (pad "2" smd rect (at 0 1) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "3" smd rect (at 0 2) (size 0.5 0.5) (layers "F.Cu") (net 3 "OTHER")))
  (footprint "R" (layer "F.Cu") (at 30 20)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "CLK"))
    (pad "2" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu") (net 0 "")))
  (footprint "T" (layer "F.Cu") (at 20 20)
    (property "Reference" "TP1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "CLK")))
  (segment (start 10 20) (end 30 20) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 10 22) (end 30 22) (width 0.2) (layer "F.Cu") (net 3))
  (via (at 15 21) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 15 30) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (segment (start 40 10) (end 50 10) (width 0.2) (layer "F.Cu") (net 4))
  (segment (start 40 10.4) (end 50 10.4) (width 0.2) (layer "F.Cu") (net 5))
  (footprint "J" (layer "F.Cu") (at 40 10)
    (property "Reference" "J1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 4 "USB_D+"))
    (pad "2" smd rect (at 0 0.4) (size 0.3 0.3) (layers "F.Cu") (net 5 "USB_D-")))
  (footprint "U" (layer "F.Cu") (at 50 10)
    (property "Reference" "U2" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 4 "USB_D+"))
    (pad "2" smd rect (at 0 0.4) (size 0.3 0.3) (layers "F.Cu") (net 5 "USB_D-"))
    (pad "3" smd rect (at 0 0.8) (size 0.3 0.3) (layers "F.Cu") (net 1 "GND")))
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (hatch edge 0.5)
    (polygon (pts (xy 0 0) (xy 60 0) (xy 60 40) (xy 0 40)))
    (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 60 0) (xy 60 40) (xy 0 40))))
)
"""


@pytest.fixture(scope="module")
def board():
    b = parse_board(parse(BOARD))
    return b, board_extent(b)


def test_margin_is_five_outer_dielectric_heights_or_three_mm(board):
    b, _ = board
    assert coupon.margin_for(b) == pytest.approx(3.0)  # 5 x 0.2 mm is under the floor
    under = next(s for s in b.stackup if s.name == "dielectric 1")  # the prepreg under F.Cu
    under.thickness_mm = 1.0
    try:
        assert coupon.margin_for(b) == pytest.approx(5.0)
    finally:
        under.thickness_mm = 0.2


def test_the_region_is_the_net_plus_its_margin(board):
    b, t = board
    c = coupon.plan(b, t, ["CLK"], margin_mm=3.0)
    x0, y0 = t.pt(10, 20)
    x1, _ = t.pt(30, 20)
    assert c.roi[0] == pytest.approx(x0 - 0.25 - 3.0, abs=0.01)
    assert c.roi[2] == pytest.approx(x1 + 0.25 + 3.0, abs=0.01)
    assert c.roi[3] - c.roi[1] == pytest.approx(0.5 + 6.0, abs=0.01)


def test_ports_go_on_the_two_furthest_pads_and_the_many_pin_part_is_driven(board):
    b, t = board
    c = coupon.plan(b, t, ["CLK"])
    # TP1 sits between them and is not an end.
    assert c.port_pads == ["U1.1", "R1.1"]
    assert [p.excited for p in c.ports] == [True, False]
    assert all(p.half_width_mm == coupon.PORT_HALF_WIDTH_MM for p in c.ports)
    # The plane is In1, under the top layer.
    assert {p.reference_layer for p in c.ports} == {"In1.Cu"}


def test_a_pair_gets_a_port_at_each_end_of_each_net_and_one_is_driven(board):
    b, t = board
    c = coupon.plan(b, t, ["USB_D+", "USB_D-"])
    assert len(c.ports) == 4
    assert sum(p.excited for p in c.ports) == 1
    assert c.ports[0].excited and c.port_pads[0] == "U2.1"


def test_a_ground_net_or_an_unknown_net_is_refused(board):
    b, t = board
    with pytest.raises(coupon.CouponError, match="ground"):
        coupon.plan(b, t, ["GND"])
    with pytest.raises(coupon.CouponError, match="not a net"):
        coupon.plan(b, t, ["NOPE"])


def test_a_coupon_past_the_largest_side_is_refused(board, monkeypatch):
    b, t = board
    monkeypatch.setattr(coupon, "MAX_SIDE_MM", 10.0)
    with pytest.raises(coupon.CouponError, match="draw a region"):
        coupon.plan(b, t, ["CLK"])


def test_extract_keeps_the_net_its_plane_and_near_ground_only(board):
    b, t = board
    c = coupon.plan(b, t, ["CLK"])
    cut, notes = coupon.extract(b, t, ["CLK"], c.roi)
    assert {tr.net for tr in cut.tracks} == {"CLK"}
    # The ground via 1 mm from the trace is stitching; the one 10 mm away is not.
    assert [(v.x, v.y) for v in cut.vias] == [(15, 21)]
    # U1's ground pin is 1 mm from the net and kept; the neighbour's pin is not.
    assert {(p.ref, p.number) for p in cut.pads} == {("U1", "1"), ("U1", "2"), ("R1", "1"),
                                                     ("TP1", "1")}
    assert len(cut.zones) == 1 and cut.zones[0].layer == "In1.Cu"
    assert any("1 other net" in n for n in notes)
    # The plane is clipped to the coupon, so it has vertices there and nothing far outside.
    xs = [t.pt(x, y)[0] for x, y in cut.zones[0].ring]
    assert min(xs) >= c.roi[0] - 2.0 - 1e-6 and max(xs) <= c.roi[2] + 2.0 + 1e-6


def test_extract_says_when_there_is_no_plane(board):
    b, t = board
    c = coupon.plan(b, t, ["CLK"])
    from dataclasses import replace

    bare, notes = coupon.extract(replace(b, zones=[]), t, ["CLK"], c.roi)
    assert any("no plane" in n for n in notes)


def test_clip_to_a_rectangle():
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    out = coupon._clip(square, 2.0, 3.0, 5.0, 20.0)
    xs, ys = sorted({p[0] for p in out}), sorted({p[1] for p in out})
    assert xs == [2.0, 5.0] and ys == [3.0, 10.0]
    assert coupon._clip(square, 20.0, 20.0, 30.0, 30.0) == []


def test_a_coupon_builds_a_model_with_its_ports_on_the_plane(board):
    b, t = board
    c = coupon.plan(b, t, ["CLK"])
    cut, _ = coupon.extract(b, t, ["CLK"], c.roi)
    built = build_model(cut, t, SolveParams(roi=c.roi, frequencies_hz=[1e8, 1e9], ports=c.ports,
                                            dx_um=150, dy_um=150, dz_um=100))
    lz = _layer_z(b)
    xml = built.doc.to_string()
    assert "p1_exc" in xml and "p2_res" in xml
    # The port box runs from the top layer to In1, not to some other layer.
    assert f"{lz['In1.Cu']:g}" in xml or f"{lz['In1.Cu'] * 1000:g}" in xml


def test_copper_closer_to_the_net_than_a_cell_is_counted(board):
    b, t = board
    c = coupon.plan(b, t, ["CLK"])
    cut, _ = coupon.extract(b, t, ["CLK"], c.roi)
    # The ground via's ring and U1's ground pin are 0.6 and 0.5 mm from the net's copper.
    assert coupon.tight_gaps(cut, ["CLK"], 0.15) == 0
    assert coupon.tight_gaps(cut, ["CLK"], 0.55) == 1
    assert coupon.tight_gaps(cut, ["CLK"], 0.7) == 2


def test_a_via_is_its_drilled_barrel_and_a_round_ring(board):
    # A box as wide as the ring reached 41 % further along its diagonal than the ring does, and
    # on a real coupon a ground via's corner shorted a 45-degree trace passing it.
    b, t = board
    c = coupon.plan(b, t, ["CLK"])
    cut, _ = coupon.extract(b, t, ["CLK"], c.roi)
    built = build_model(cut, t, SolveParams(roi=c.roi, frequencies_hz=[1e8, 1e9], ports=c.ports,
                                            dx_um=150, dy_um=150, dz_um=100))
    by_name = {getattr(m, "name", ""): m for m in built.doc.properties}
    (barrel,) = by_name["cu_vias"].primitives
    assert barrel.p2[0] - barrel.p1[0] == pytest.approx(0.3)
    cx, cy = (barrel.p1[0] + barrel.p2[0]) / 2, (barrel.p1[1] + barrel.p2[1]) / 2
    for layer in ("cu_F_Cu", "cu_B_Cu"):
        reach = [max(math.hypot(x - cx, y - cy) for x, y in p.vertices)
                 for p in by_name[layer].primitives
                 if all(math.hypot(x - cx, y - cy) < 0.4 for x, y in p.vertices)]
        assert reach and reach[0] == pytest.approx(0.3, rel=0.05), layer


@pytest.mark.parametrize("preset", [(150, 150, 100), (75, 75, 50), (50, 50, 25)])
def test_a_small_part_mesh_holds_the_bands_coarsest_cell_on_every_preset(board, preset):
    # The PML padding grew 1.2x a line past the wavelength bound: a real coupon's coarsest cell
    # was 5.3 mm against the 3.5 mm 2 GHz allows, on every preset, in cells nobody reads.
    from emi_worker.openems.mesh import max_cell_for_frequency
    from emi_worker.stages import small_part

    b, t = board
    c = coupon.plan(b, t, ["USB_D+", "USB_D-"])
    cut, _ = coupon.extract(b, t, ["USB_D+", "USB_D-"], c.roi)
    dx, dy, dz = preset
    params = small_part.apply({"mode": "small_part"}, SolveParams(
        roi=c.roi, frequencies_hz=[], ports=c.ports, dx_um=dx, dy_um=dy, dz_um=dz))
    built = build_model(cut, t, params)
    limit = max_cell_for_frequency(small_part.BAND_HZ[1], 4.6)
    assert built.mesh.max_cell_mm <= limit * 1.0001
    assert not any("coarsest cell" in n for n in built.notes)
    # Every preset reads the top layer's map at the same height above its copper: between two
    # grid lines, or on the first line when that is already about as high.
    lz = _layer_z(b)
    (box,) = next(p for p in built.doc.properties if getattr(p, "name", "") == "Hf_F_Cu").primitives
    height = built.dump_heights.get("F.Cu", box.p1[2]) - lz["F.Cu"]
    assert height == pytest.approx(small_part.MAP_HEIGHT_MM, rel=0.05)
    # A region solve keeps the growth its verification was run with.
    region = build_model(cut, t, SolveParams(roi=c.roi, frequencies_hz=[1e8, 2e9], ports=c.ports,
                                             dx_um=dx, dy_um=dy, dz_um=dz))
    assert region.mesh.max_cell_mm > limit


@pytest.mark.parametrize("cell_um", [150, 75])
def test_a_diagonal_trace_has_grid_lines_all_along_it(cell_um):
    # Lines at a 45-degree trace's ends alone graded out to 0.7 mm under it, whole columns of
    # cells missed its 0.42 mm span along x, and the net came out cut in two.
    import numpy as np

    text = BOARD.replace('(segment (start 10 22) (end 30 22) (width 0.2) (layer "F.Cu") (net 3))',
                         '(segment (start 10 22) (end 20 32) (width 0.2) (layer "F.Cu") (net 3))')
    b = parse_board(parse(text))
    t = board_extent(b)
    c = coupon.plan(b, t, ["OTHER"])
    cut, _ = coupon.extract(b, t, ["OTHER"], c.roi)
    built = build_model(cut, t, SolveParams(roi=c.roi, frequencies_hz=[1e8, 2e9], ports=c.ports,
                                            dx_um=cell_um, dy_um=cell_um, dz_um=100))
    (x0, y0), (x1, y1) = t.pt(10, 22), t.pt(20, 32)
    step = min(cell_um / 1000.0, 0.7 * 0.2) * 1.0001
    for lines, lo, hi in ((built.mesh.x, x0, x1), (built.mesh.y, min(y0, y1), max(y0, y1))):
        inside = lines[(lines >= lo) & (lines <= hi)]
        assert np.diff(inside).max() <= step
