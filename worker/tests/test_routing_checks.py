"""Routing checks from Reddit r/PCB "I paid for this workmanship": test point stubs (picture 2),
pair halves coupling unequally to a pour (picture 1) and thin necks in power copper (picture 7).

Same shape as test_layout_checks: the smallest board that shows the problem, then the smallest
change that fixes it.
"""

from __future__ import annotations

from emi_worker import topology
from emi_worker.kicad.board import BoardModel, CopperLayer, Pad, Track
from emi_worker.kicad.netclass import find_pairs
from emi_worker.kicad.normalize import board_extent
from emi_worker.rules import settings, stubs
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
