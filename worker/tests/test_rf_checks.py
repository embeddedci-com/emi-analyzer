"""Copper under antennas (Reddit r/PCB "I paid for this workmanship", picture 6).

A module's antenna is the pad-free end of its body; a separate antenna part is its body; a rule
area in the footprint wins over both. Ground counts as much as any signal.
"""

from __future__ import annotations

from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.board import BoardModel, CopperLayer, Footprint, Keepout, Pad, Track, Via, ZonePolygon
from emi_worker.kicad.normalize import board_extent
from emi_worker.rules import rf, settings
from emi_worker.rules.model import RuleContext


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def board() -> BoardModel:
    m = BoardModel()
    m.copper_layers = [CopperLayer(ordinal=i, name=n, kind="signal") for i, n in enumerate(("F.Cu", "B.Cu"))]
    m.outline = [square(0, 0, 60, 60) + [(0.0, 0.0)]]
    return m


def pad(ref, number, net, x, y, footprint="", value=""):
    return Pad(ref=ref, number=number, net=net, layers=["F.Cu"], x=x, y=y,
               ring=square(x - 0.4, y - 0.4, x + 0.4, y + 0.4), footprint=footprint, value=value)


WROOM = "RF_Module:ESP32-WROOM-32"


def module(footprint=WROOM, value="ESP32-WROOM-32"):
    """An 18 x 25.5 mm module at (30, 30): pads from y=-5 down, antenna in the top 7.75 mm."""
    m = board()
    m.footprints = [Footprint(ref="U1", footprint=footprint, value=value, x=30, y=30,
                              local_bbox=(-9.0, -12.75, 9.0, 12.75))]
    m.pads = [pad("U1", str(i), "GND" if i == 1 else f"/IO{i}", 30 + sx, 30 + y, footprint, value)
              for i, (sx, y) in enumerate(((-9, -5), (9, -5), (-9, 12), (9, 12)), start=1)]
    return m


def run(m, rules=None):
    ctx = RuleContext(model=m, transform=board_extent(m), max_frequency_hz=1e9)
    if rules:
        ctx.settings = settings.load(("file", {"rules": rules}))
    return list(rf.check_antennas(ctx))


def titles(findings):
    return [f.title for f in findings]


def test_a_trace_under_a_modules_antenna_is_found():
    m = module()
    m.tracks = [Track(layer="B.Cu", net="/SPI_CLK", width_mm=0.2, pts=[(10.0, 20.0), (50.0, 20.0)])]
    f = run(m)
    assert titles(f) == ["/SPI_CLK runs under the antenna of U1"]
    assert f[0].layer == "B.Cu"


def test_a_trace_under_the_modules_pads_is_not_the_antenna():
    m = module()
    m.tracks = [Track(layer="B.Cu", net="/SPI_CLK", width_mm=0.2, pts=[(10.0, 35.0), (50.0, 35.0)])]
    assert run(m) == []


def test_a_via_in_the_antenna_area_is_found():
    m = module()
    m.vias = [Via(x=30, y=20, size_mm=0.6, drill_mm=0.3, layers=["F.Cu", "B.Cu"], net="GND")]
    assert titles(run(m)) == ["GND runs under the antenna of U1"]


def test_a_ground_pour_under_the_antenna_is_found_and_cut_back_passes():
    m = module()
    m.zones = [ZonePolygon(layer="B.Cu", net="GND", ring=square(0, 0, 60, 60))]
    f = run(m)
    assert titles(f) == ["GND pour covers 100% of the antenna of U1 on B.Cu"]

    m.zones = [ZonePolygon(layer="B.Cu", net="GND", ring=square(0, 25, 60, 60))]
    assert run(m) == []


def test_a_module_with_a_ufl_connector_has_no_antenna():
    m = module("RF_Module:ESP32-WROOM-32U", "ESP32-WROOM-32U")
    m.tracks = [Track(layer="B.Cu", net="/SPI_CLK", width_mm=0.2, pts=[(10.0, 20.0), (50.0, 20.0)])]
    assert run(m) == []


def test_the_footprints_own_keepout_is_the_antenna_area():
    m = module()
    m.keepouts = [Keepout(ring=square(21, 17.25, 39, 19), layers=["F.Cu", "B.Cu"], ref="U1",
                          no_tracks=True, no_pour=True)]
    m.tracks = [Track(layer="B.Cu", net="/A", width_mm=0.2, pts=[(10.0, 18.0), (50.0, 18.0)]),
                Track(layer="B.Cu", net="/B", width_mm=0.2, pts=[(10.0, 21.0), (50.0, 21.0)])]
    assert titles(run(m)) == ["/A runs under the antenna of U1"]


def test_a_chip_antennas_feed_is_not_under_it_but_another_net_is():
    m = board()
    m.footprints = [Footprint(ref="AE1", footprint="RF_Antenna:Johanson_2450AT18", x=30, y=30,
                              local_bbox=(-1.6, -0.8, 1.6, 0.8))]
    m.pads = [pad("AE1", "1", "/RF", 28.8, 30), pad("AE1", "2", "", 31.2, 30)]
    m.tracks = [Track(layer="F.Cu", net="/RF", width_mm=0.3, pts=[(20.0, 30.0), (28.8, 30.0)]),
                Track(layer="B.Cu", net="/LED", width_mm=0.2, pts=[(30.0, 20.0), (30.0, 40.0)])]
    assert titles(run(m)) == ["/LED runs under the antenna of AE1"]


def test_the_rule_can_be_switched_off():
    m = module()
    m.tracks = [Track(layer="B.Cu", net="/SPI_CLK", width_mm=0.2, pts=[(10.0, 20.0), (50.0, 20.0)])]
    assert run(m, {"antenna": False}) == []


BOARD = """
(kicad_pcb (version 20240108) (generator pcbnew)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "") (net 1 "GND")
  (footprint "RF_Module:ESP32-C3-MINI-1" (layer "F.Cu") (at 50 50 90)
    (property "Reference" "U4") (property "Value" "ESP32-C3-MINI-1")
    (fp_line (start -6.6 -11) (end 6.6 -11) (layer "F.CrtYd"))
    (fp_line (start 6.6 -11) (end 6.6 5.6) (layer "F.CrtYd"))
    (fp_line (start -6.6 5.6) (end 6.6 5.6) (layer "F.CrtYd"))
    (fp_line (start -6.6 -5.6) (end 6.6 -5.6) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
    (zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (name "antenna")
      (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
      (polygon (pts (xy 39 43.4) (xy 44.4 43.4) (xy 44.4 56.6) (xy 39 56.6)))))
  (zone (net 0) (net_name "") (layer "F.Cu")
    (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
    (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))
)
"""


def test_footprint_bodies_and_keepouts_are_parsed():
    m = parse_board(parse(BOARD))
    fp = m.footprints[0]
    assert (fp.ref, fp.rotation, fp.local_bbox) == ("U4", 90.0, (-6.6, -11.0, 6.6, 5.6))
    # Rotated 90 degrees counterclockwise on screen: the body's -y end ends up at -x.
    xs = [round(p[0], 3) for p in fp.ring()]
    assert min(xs) == 39.0 and max(xs) == 55.6
    ko = {k.ref: k for k in m.keepouts}
    assert ko["U4"].no_tracks and ko["U4"].no_pour and ko["U4"].layers == ["F.Cu", "B.Cu"]
    assert not ko[""].no_tracks and ko[""].no_pour
