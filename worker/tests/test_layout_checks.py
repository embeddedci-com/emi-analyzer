"""The checks that close the gaps: decoupling, stitching, islands, crystals, stackup.

Each test builds the smallest board that shows the problem, then the smallest change that
fixes it -- a check that fires on the bad board but not on the good one is the only kind
worth trusting on a real one.
"""

from __future__ import annotations

import pytest

from emi_worker.kicad.board import BoardModel, CopperLayer, Pad, Track, Via, ZonePolygon
from emi_worker.kicad.normalize import _board_extent
from emi_worker.rules import decoupling, placement, stitching
from emi_worker.rules.model import RuleContext
from emi_worker.stackup import BoardElectrics, LayerElectrics


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def board(layers=("F.Cu", "B.Cu"), size=40.0) -> BoardModel:
    m = BoardModel()
    m.copper_layers = [CopperLayer(ordinal=i, name=n, kind="signal") for i, n in enumerate(layers)]
    m.outline = [square(0, 0, size, size) + [(0.0, 0.0)]]
    return m


def ctx_for(m, electrics=None, freq=1e9) -> RuleContext:
    return RuleContext(model=m, transform=_board_extent(m), max_frequency_hz=freq, electrics=electrics)


def pad(ref, number, net, x, y, layers=("F.Cu",), value="", kind="smd"):
    return Pad(ref=ref, number=number, net=net, layers=list(layers), x=x, y=y,
               ring=square(x - 0.3, y - 0.3, x + 0.3, y + 0.3), pad_type=kind, value=value)


def via(x, y, net="GND"):
    return Via(x=x, y=y, size_mm=0.6, drill_mm=0.3, layers=["F.Cu", "B.Cu"], net=net)


def run(check, m, **kw):
    return list(check(ctx_for(m, **kw)))


# ---- decoupling ---------------------------------------------------------------------------

def _ic_and_cap(cap_x):
    m = board()
    m.pads = [
        pad("U1", "1", "+3V3", 10, 10), pad("U1", "2", "GND", 10, 11),
        pad("C1", "1", "+3V3", cap_x, 10, value="100nF"), pad("C1", "2", "GND", cap_x + 1, 10, value="100nF"),
    ]
    m.vias = [via(cap_x + 1.2, 10.3)]
    return m


def test_a_capacitor_beside_the_pin_is_fine():
    assert run(decoupling.check_decoupling, _ic_and_cap(11.5)) == []


def test_a_distant_capacitor_says_where_it_stops_working():
    f = run(decoupling.check_decoupling, _ic_and_cap(18.0))
    assert len(f) == 1
    assert f[0].title == "U1.1 is 8.0 mm from its nearest decoupling capacitor"
    assert f[0].severity == "warning"
    assert "C1 (100nF)" in f[0].detail and "MHz" in f[0].detail


def test_beyond_the_critical_distance_it_is_critical():
    assert run(decoupling.check_decoupling, _ic_and_cap(25.0))[0].severity == "critical"


def test_a_supply_with_no_capacitor_at_all():
    m = board()
    m.pads = [pad("U1", "1", "+3V3", 10, 10), pad("U1", "2", "GND", 10, 11)]
    f = run(decoupling.check_decoupling, m)
    assert [x.title for x in f] == ["U1 has no decoupling capacitor on +3V3"]
    assert f[0].severity == "critical"


def test_the_capacitor_ground_pad_needs_its_own_via():
    m = _ic_and_cap(11.5)
    m.vias = [via(30, 30)]
    titles = [x.title for x in run(decoupling.check_decoupling, m)]
    assert titles == ["C1's ground pad has no via within 1 mm"]


@pytest.mark.parametrize("text,farads", [
    ("100nF", 100e-9), ("100n", 100e-9), ("0.1uF", 0.1e-6), ("4.7µF", 4.7e-6), ("10u", 10e-6),
    ("1,5nF", 1.5e-9), ("junk", None), ("", None),
])
def test_capacitor_values_parse(text, farads):
    got = decoupling.cap_farads(text)
    assert got == pytest.approx(farads) if farads else got is None


# ---- stitching ----------------------------------------------------------------------------

def _ground_planes(m, size=40.0):
    m.zones = [ZonePolygon("F.Cu", "GND", square(0, 0, size, size)),
               ZonePolygon("B.Cu", "GND", square(0, 0, size, size))]
    return m


def test_a_well_stitched_board_passes():
    m = _ground_planes(board())
    m.vias = [via(x, y) for x in range(2, 40, 5) for y in range(2, 40, 5)]
    assert run(stitching.check_stitching, m) == []


def test_an_unstitched_region_is_found_with_its_size():
    m = _ground_planes(board())
    m.vias = [via(x, y) for x in (2, 7) for y in range(2, 40, 5)]
    f = run(stitching.check_stitching, m)
    assert f, "half the board has no stitching and nothing was reported"
    assert "no stitching via within 7.5 mm" in f[0].title
    assert f[0].severity == "critical"


def test_one_ground_plane_has_nothing_to_stitch():
    m = board()
    m.zones = [ZonePolygon("F.Cu", "GND", square(0, 0, 40, 40))]
    assert run(stitching.check_stitching, m) == []


def test_planes_with_no_vias_at_all_are_reported_once():
    f = run(stitching.check_stitching, _ground_planes(board()))
    assert len(f) == 1 and f[0].title.startswith("No vias stitch the ground planes")


def test_spacing_follows_the_frequency():
    """λ/20 at 3 GHz is a third of that at 1 GHz, so a board that passes at 1 GHz can fail."""
    m = _ground_planes(board())
    m.vias = [via(x, y) for x in range(2, 40, 5) for y in range(2, 40, 5)]
    assert run(stitching.check_stitching, m, freq=1e9) == []
    assert run(stitching.check_stitching, m, freq=3e9) != []


def test_an_edge_fence_passes():
    m = _ground_planes(board())
    m.vias = [via(p, q) for t in range(0, 41, 5) for p, q in ((1, t), (39, t), (t, 1), (t, 39))]
    assert run(stitching.check_edge_stitching, m) == []


def test_an_unfenced_edge_is_found():
    m = _ground_planes(board())
    m.vias = [via(x, y) for x in range(12, 30, 5) for y in range(12, 30, 5)]
    f = run(stitching.check_edge_stitching, m)
    assert f and all("of board edge with no stitching via" in x.title for x in f)


# ---- copper islands -----------------------------------------------------------------------

def test_an_unconnected_pour_is_an_island():
    m = board()
    m.zones = [ZonePolygon("F.Cu", "GND", square(5, 5, 15, 15))]
    f = run(placement.check_copper_islands, m)
    assert len(f) == 1 and "connected to nothing" in f[0].title


def test_a_pour_with_a_via_is_connected():
    m = board()
    m.zones = [ZonePolygon("F.Cu", "GND", square(5, 5, 15, 15))]
    m.vias = [via(10, 10)]
    assert run(placement.check_copper_islands, m) == []


def test_a_pad_in_a_thermal_relief_still_connects_the_pour():
    """The pad centre sits in the relief void, outside the copper, joined by spokes."""
    m = board()
    m.zones = [ZonePolygon("F.Cu", "GND", square(5, 5, 15, 15))]
    m.pads = [pad("C1", "2", "GND", 15.4, 10)]
    assert run(placement.check_copper_islands, m) == []


def test_a_pour_on_no_net_is_always_floating():
    m = board()
    m.zones = [ZonePolygon("F.Cu", "", square(5, 5, 15, 15))]
    m.vias = [via(10, 10, net="")]
    assert "no net" in run(placement.check_copper_islands, m)[0].detail


def test_slivers_below_the_minimum_area_are_ignored():
    m = board()
    m.zones = [ZonePolygon("F.Cu", "GND", square(5, 5, 6, 6))]
    assert run(placement.check_copper_islands, m) == []


# ---- crystals -----------------------------------------------------------------------------

def _crystal(m, x=20.0, y=20.0, ref="Y1", value=""):
    m.pads += [pad(ref, "1", "XIN", x, y, value=value), pad(ref, "2", "XOUT", x + 2, y, value=value)]
    return m


def test_a_signal_under_a_crystal_is_found():
    m = _crystal(board())
    m.tracks = [Track("F.Cu", "SPI_MOSI", 0.2, [(15, 20), (27, 20)])]
    assert [f.title for f in run(placement.check_crystals, m)] == [
        "SPI_MOSI is routed under crystal Y1 on F.Cu"
    ]


def test_a_ground_plane_between_shields_the_crystal():
    m = _crystal(board(layers=("F.Cu", "In1.Cu", "B.Cu")))
    m.zones = [ZonePolygon("In1.Cu", "GND", square(0, 0, 40, 40))]
    m.tracks = [Track("B.Cu", "SPI_MOSI", 0.2, [(15, 20), (27, 20)])]
    assert run(placement.check_crystals, m) == []


def test_a_crystal_near_the_edge():
    m = _crystal(board(), x=1.5)
    assert any("from the board edge" in f.title for f in run(placement.check_crystals, m))


def test_a_crystal_near_a_connector():
    m = _crystal(board())
    m.pads.append(pad("J1", "1", "USB_DP", 26, 20))
    assert any("from connector J1" in f.title for f in run(placement.check_crystals, m))


def test_recognised_by_its_value_not_only_its_reference():
    m = _crystal(board(), ref="U7", value="16MHz")
    m.tracks = [Track("F.Cu", "SPI_MOSI", 0.2, [(15, 20), (27, 20)])]
    assert [f.title for f in run(placement.check_crystals, m)] == [
        "SPI_MOSI is routed under crystal U7 on F.Cu"
    ]


# ---- stackup ------------------------------------------------------------------------------

def _elec(refs):
    return BoardElectrics(layers={
        n: LayerElectrics(n, i, "microstrip", ref, 0.2, 4.3) for i, (n, ref) in enumerate(refs)
    })


def test_a_signal_layer_with_no_plane_on_a_multilayer_board():
    m = board(layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    m.zones = [ZonePolygon("In1.Cu", "GND", square(0, 0, 40, 40))]
    m.tracks = [Track(l, "SIG", 0.2, [(1, 1), (10, 1)]) for l in ("F.Cu", "In2.Cu", "B.Cu")]
    elec = _elec([("F.Cu", "In1.Cu"), ("In1.Cu", ""), ("In2.Cu", "In1.Cu"), ("B.Cu", "")])
    f = {x.title: x.severity for x in run(placement.check_stackup, m, electrics=elec)}
    assert f == {
        "Signals on B.Cu have no reference plane": "critical",
        "In2.Cu and B.Cu are adjacent signal layers with no plane between": "warning",
    }


def test_a_two_layer_board_is_advised_not_alarmed():
    m = board()
    m.tracks = [Track(l, "SIG", 0.2, [(1, 1), (10, 1)]) for l in ("F.Cu", "B.Cu")]
    f = run(placement.check_stackup, m, electrics=_elec([("F.Cu", ""), ("B.Cu", "")]))
    assert {x.severity for x in f} == {"warning"}
    assert not any("adjacent signal layers" in x.title for x in f)
