"""Routing checks from Reddit r/PCB "I paid for this workmanship": test point stubs (picture 2),
pair halves coupling unequally to a pour (picture 1) and thin necks in power copper (picture 7).

Same shape as test_layout_checks: the smallest board that shows the problem, then the smallest
change that fixes it.
"""

from __future__ import annotations

from emi_worker import topology
from emi_worker.kicad.board import BoardModel, CopperLayer, Pad, Track, ZonePolygon
from emi_worker.kicad.netclass import find_pairs
from emi_worker.kicad.normalize import board_extent
from emi_worker.rules import pairs, settings, stubs
from emi_worker.rules.model import RuleContext


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def board() -> BoardModel:
    m = BoardModel()
    m.copper_layers = [CopperLayer(ordinal=i, name=n, kind="signal") for i, n in enumerate(("F.Cu", "B.Cu"))]
    m.outline = [square(0, 0, 60, 60) + [(0.0, 0.0)]]
    return m


def pad(ref, number, net, x, y, footprint="", half=0.3):
    return Pad(ref=ref, number=number, net=net, layers=["F.Cu"], x=x, y=y,
               ring=square(x - half, y - half, x + half, y + half), footprint=footprint)


def track(net, *pts, layer="F.Cu", width=0.2):
    return Track(layer=layer, net=net, width_mm=width, pts=list(pts))


def ctx_for(m, rules=None) -> RuleContext:
    m.nets = sorted({p.net for p in m.pads if p.net} | {t.net for t in m.tracks if t.net}
                    | {z.net for z in m.zones if z.net})
    ctx = RuleContext(model=m, transform=board_extent(m), max_frequency_hz=1e9,
                      topology=topology.build(m))
    ctx.pairs = find_pairs(m.nets)
    if rules:
        ctx.settings = settings.load(("file", {"rules": rules}))
    return ctx


def run(check, m, rules=None):
    return list(check(ctx_for(m, rules)))


def titles(findings):
    return [f.title for f in findings]


# ---- test point stubs ---------------------------------------------------------------------

def _spi(net="/SPI_SCK", branch=20.0, tp_ref="TP1", footprint="TestPoint:TestPoint_Pad_D1.0mm"):
    """U1 to U2 along y=10, with a test point reached by a branch of the given length at x=20."""
    m = board()
    m.pads = [pad("U1", "1", net, 5, 10), pad("U2", "1", net, 45, 10),
              pad(tp_ref, "1", net, 20, 10 + branch, footprint)]
    m.tracks = [track(net, (5, 10), (45, 10))]
    if branch:
        m.tracks.append(track(net, (20, 10), (20, 10 + branch)))
    return m


def test_a_test_point_at_the_end_of_a_branch_on_a_fast_net_is_found():
    f = run(stubs.check_test_point_stubs, _spi())
    assert titles(f) == ["Test point TP1 hangs 20.0 mm off /SPI_SCK"]
    assert "quarter wavelength" in f[0].detail


def test_a_test_point_on_the_trace_passes():
    assert run(stubs.check_test_point_stubs, _spi(branch=0.0)) == []


def test_a_short_branch_passes():
    # λ/20 at 1 GHz on the default velocity is 7.5 mm.
    assert run(stubs.check_test_point_stubs, _spi(branch=5.0)) == []


def test_a_slow_net_is_not_checked_unless_asked():
    m = _spi(net="/LED_EN")
    assert run(stubs.check_test_point_stubs, m) == []
    f = run(stubs.check_test_point_stubs, m, {"test-point-stub": {"params": {"fast_nets_only": False}}})
    assert titles(f) == ["Test point TP1 hangs 20.0 mm off /LED_EN"]


def test_the_budget_can_be_set():
    assert run(stubs.check_test_point_stubs, _spi(), {"test-point-stub": {"params": {"max_stub_mm": 25.0}}}) == []


def test_a_test_point_found_by_its_footprint_alone():
    f = run(stubs.check_test_point_stubs, _spi(tp_ref="J9"))
    assert titles(f) == ["Test point J9 hangs 20.0 mm off /SPI_SCK"]


def test_a_pin_and_its_test_point_alone_is_not_a_stub():
    m = board()
    m.pads = [pad("U1", "1", "/SPI_SCK", 5, 10), pad("TP1", "1", "/SPI_SCK", 45, 10)]
    m.tracks = [track("/SPI_SCK", (5, 10), (45, 10))]
    assert run(stubs.check_test_point_stubs, m) == []


def test_a_net_group_can_mark_a_net_fast():
    m = _spi(net="/LED_EN")
    ctx = ctx_for(m)
    ctx.settings = settings.load(("file", {"groups": [
        {"match": "/LED_EN", "params": {"fast_nets_only": False}}]}))
    assert titles(stubs.check_test_point_stubs(ctx)) == ["Test point TP1 hangs 20.0 mm off /LED_EN"]


# ---- differential pair routing ------------------------------------------------------------

def _pair(split=0.0, pour_above=None, pour_below=None):
    """USB_DP at y=20.15 and USB_DM at y=19.85 (0.15 mm wide, 0.15 mm gap), x=5 to x=45.

    split: USB_DM detours this far down between x=20 and x=30. pour_above/below: a GND pour whose
    edge sits that far from the nearest half's copper, above DP or below DM.
    """
    m = board()
    m.pads = [pad("J1", "2", "USB_DM", 5, 19.85, half=0.1), pad("J1", "3", "USB_DP", 5, 20.15, half=0.1),
              pad("U1", "1", "USB_DM", 45, 19.85, half=0.1), pad("U1", "2", "USB_DP", 45, 20.15, half=0.1)]
    m.tracks = [track("USB_DP", (5, 20.15), (45, 20.15), width=0.15)]
    if split:
        m.tracks.append(track("USB_DM", (5, 19.85), (20, 19.85), (20, 19.85 - split), (30, 19.85 - split),
                              (30, 19.85), (45, 19.85), width=0.15))
    else:
        m.tracks.append(track("USB_DM", (5, 19.85), (45, 19.85), width=0.15))
    if pour_above is not None:
        y = 20.225 + pour_above
        m.zones.append(ZonePolygon(layer="F.Cu", net="GND", ring=square(0, y, 60, 60)))
    if pour_below is not None:
        y = 19.775 - pour_below
        m.zones.append(ZonePolygon(layer="F.Cu", net="GND", ring=square(0, 0, 60, y)))
    return m


def test_a_coupled_pair_passes():
    assert run(pairs.check_pair_coupling, _pair()) == []


def test_halves_that_split_are_found():
    """Reddit r/PCB picture 1: one half takes its own shortest path."""
    f = run(pairs.check_pair_coupling, _pair(split=3.0))
    # The detour is 3 + 10 + 3 mm of USB_DM, less the half millimetre at each end still
    # within reach of USB_DP.
    assert titles(f) == ["USB_DP and USB_DM run apart for 15.0 mm"]


def test_a_pour_beside_one_half_only_is_found():
    """Reddit r/PCB picture 1: a plane routed close to one half of the pair."""
    f = run(pairs.check_pair_coupling, _pair(pour_above=0.15))
    assert len(f) == 1
    assert f[0].title.startswith("USB_DP runs beside the GND pour for ")
    assert f[0].title.endswith(" mm more than USB_DM")
    assert f[0].layer == "F.Cu"


def test_a_pour_on_both_sides_is_a_coplanar_pair_and_passes():
    assert run(pairs.check_pair_coupling, _pair(pour_above=0.15, pour_below=0.15)) == []


def test_a_pour_well_clear_of_the_pair_passes():
    assert run(pairs.check_pair_coupling, _pair(pour_above=1.0)) == []


def test_each_part_can_be_switched_off():
    rules = {"pair-coupling": {"params": {"max_uncoupled_mm": 0, "max_asymmetry_mm": 0}}}
    m = _pair(split=3.0)
    m.zones.append(ZonePolygon(layer="F.Cu", net="GND", ring=square(0, 20.375, 60, 60)))
    assert run(pairs.check_pair_coupling, m, rules) == []
