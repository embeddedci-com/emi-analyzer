"""KiCad ingest tests.

Most of these run against ``fixtures/tiny.kicad_pcb``, a board small enough that every
expected answer can be worked out by hand — the pour has a deliberate 10 mm slot in it, and
the CLK trace crosses exactly 5 mm of that slot. Checking against a computed number rather
than against "whatever the code produced last time" is what makes these tests able to catch
a regression rather than merely record one.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from emi_worker.kicad import parse, parse_board
from emi_worker.kicad import geometry as g
from emi_worker.kicad.normalize import board_extent, _simplify, normalize
from emi_worker.kicad.sexpr import SexprError
from emi_worker.rules import run_rules
from emi_worker.rules.checks import PLANE_RASTER_MM, _rasterize_layer
from emi_worker.rules.model import RuleContext, classify_net

FIXTURE = Path(__file__).parent / "fixtures" / "tiny.kicad_pcb"


@pytest.fixture(scope="module")
def model():
    return parse_board(parse(FIXTURE.read_text()))


@pytest.fixture(scope="module")
def normalized(model):
    return normalize(model, {"filename": "tiny.kicad_pcb"})


# ---- s-expressions -------------------------------------------------------------------

def test_quoted_numbers_stay_strings():
    """A net literally named "5" must not become the number 5.

    KiCad 20260206 refers to nets by name rather than index, so a numeric-looking name in
    that position would silently be read as an index into a table that no longer exists.
    """
    tree = parse('(kicad_pcb (net "5") (width 5))')
    assert tree[1][1] == "5"
    assert tree[2][1] == 5.0


def test_escapes_are_unescaped():
    tree = parse(r'(kicad_pcb (name "a\"b"))')
    assert tree[1][1] == 'a"b'


@pytest.mark.parametrize("bad", ["(kicad_pcb", "kicad_pcb)", "", "(a) (b)"])
def test_malformed_input_raises(bad):
    with pytest.raises(SexprError):
        parse(bad)


# ---- board extraction ----------------------------------------------------------------

def test_layers_are_in_stack_order(model):
    """F.Cu is ordinal 0 and B.Cu is ordinal 2, but B.Cu is the *bottom* of the stack.

    Sorting by KiCad's ordinal would place the bottom layer in the middle of a 4-layer
    board.
    """
    assert model.copper_layer_names == ["F.Cu", "B.Cu"]


def test_nets_and_counts(model):
    assert set(model.nets) >= {"GND", "CLK", "DATA"}
    assert len(model.tracks) == 5   # 4 segments + 1 arc
    assert len(model.vias) == 3
    assert len(model.pads) == 3
    assert len(model.zones) == 2   # a split plane, two halves


def test_arc_is_expanded_to_a_polyline(model):
    arcs = [t for t in model.tracks if t.is_arc]
    assert len(arcs) == 1
    # An arc through (40,20) (43,22) (45,25) is subdivided, not left as three points.
    assert len(arcs[0].pts) > 3
    assert arcs[0].pts[0] == pytest.approx((40, 20), abs=1e-6)
    assert arcs[0].pts[-1] == pytest.approx((45, 25), abs=1e-6)


def test_stackup_epsilon_is_read_from_the_file(model):
    core = [s for s in model.stackup if s.is_dielectric][0]
    assert core.epsilon_r == 4.5
    assert core.loss_tangent == 0.02
    assert core.from_file is True


def test_solder_mask_is_not_treated_as_dielectric():
    """Board dielectric needs epsilon_r; solder mask does not.

    Demanding one for mask produces a warning on essentially every KiCad board.
    """
    tree = parse("""(kicad_pcb (version 1) (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
      (setup (stackup
        (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
        (layer "F.Cu" (type "copper") (thickness 0.035))
        (layer "d1" (type "core") (thickness 1.5) (epsilon_r 4.5))
        (layer "B.Cu" (type "copper") (thickness 0.035)))))""")
    m = parse_board(tree)
    assert not any("F.Mask" in w for w in m.warnings)


def test_missing_stackup_is_synthesised_and_flagged():
    tree = parse('(kicad_pcb (version 1) (layers (0 "F.Cu" signal) (2 "B.Cu" signal)))')
    m = parse_board(tree)
    assert any("No stackup" in w for w in m.warnings)
    assert all(not s.from_file for s in m.stackup)


def test_footprint_rotation_places_pads_on_their_tracks(model):
    """The empirical check that settled KiCad's rotation convention.

    KiCad stores angles counterclockwise but its Y axis points down, so the transform is a
    rotation by *minus* the stored angle. Measured on the real BenchPod board, the correct
    convention gives a median pad-to-same-net-track distance of 0.000 mm; the naive
    counterclockwise one gives 0.85 mm.

    Here, U1 is at (10,10) rotated 90 degrees with pad 1 offset (-1,0). Rotating (-1,0) by
    -90 degrees gives (0,+1), so pad 1 lands at (10,11) — and the CLK track starts at
    (10,10), within the pad.
    """
    pad = [p for p in model.pads if p.ref == "U1" and p.number == "1"][0]
    assert (pad.x, pad.y) == pytest.approx((10.0, 11.0), abs=1e-6)


def test_through_hole_pad_spans_all_copper(normalized):
    doc, _ = normalized
    j1 = [p for p in doc["pads"] if p["ref"] == "J1"][0]
    assert j1["type"] == "thru_hole"
    assert j1["drill_mm"] == 0.8


# ---- geometry ------------------------------------------------------------------------

def test_capsule_covers_the_track_width():
    ring = g.capsule((0, 0), (10, 0), 2.0)
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    # Round caps extend a radius past each end.
    assert min(xs) == pytest.approx(-1.0, abs=0.01)
    assert max(xs) == pytest.approx(11.0, abs=0.01)
    assert max(ys) == pytest.approx(1.0, abs=0.01)


def test_circle_never_loses_copper():
    """Curves are circumscribed, so the polygon covers at least the true circle.

    Erring the other way would shrink pads and vias, and a connection that disappears
    because of a rounding choice is a much worse failure than a sliver of extra copper.
    """
    r = 0.3
    ring = g.circle(0, 0, r)
    # Every edge midpoint must still be at least r from the centre.
    for i in range(len(ring)):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % len(ring)]
        assert math.hypot((ax + bx) / 2, (ay + by) / 2) >= r - 1e-9


def test_arc_polyline_through_collinear_points_is_a_line():
    pts = g.arc_polyline((0, 0), (5, 0), (10, 0))
    assert pts == [(0, 0), (10, 0)]


def test_triangulate_produces_whole_triangles():
    tris = g.triangulate([(0, 0), (10, 0), (10, 10), (0, 10)])
    assert tris.size % 6 == 0
    assert tris.size // 6 == 2  # a quad is two triangles


def test_simplify_keeps_the_shape():
    ring = [(i * 0.01, 0.0) for i in range(500)] + [(5.0, 5.0)]
    out = _simplify(ring, 0.02)
    assert len(out) < len(ring)
    assert out[0] == ring[0] and out[-1] == ring[-1]


# ---- normalisation -------------------------------------------------------------------

def test_coordinates_are_flipped_to_y_up(normalized, model):
    doc, _ = normalized
    assert doc["board"]["width_mm"] == 50.0
    assert doc["board"]["height_mm"] == 40.0
    # U1 pad 1 is at KiCad (10, 11) on a board spanning y 0..40, so Y-up puts it at 29.
    pad = [p for p in doc["pads"] if p["ref"] == "U1" and p["number"] == "1"][0]
    assert pad["y"] == pytest.approx(29.0, abs=1e-6)


def test_geometry_index_matches_the_buffer(normalized):
    doc, geo = normalized
    assert len(geo) == doc["geometry"]["byte_length"]
    assert doc["geometry"]["vertex_count"] * 8 == len(geo)
    # Groups must tile the buffer exactly: a gap or an overlap means the viewer draws the
    # wrong net's triangles.
    total = 0
    for grp in doc["geometry"]["groups"]:
        assert grp["offset"] == total
        total += grp["count"]
    assert total == doc["geometry"]["vertex_count"]


def test_copper_z_heights_come_from_the_stackup(normalized):
    doc, _ = normalized
    z = {layer["name"]: layer["z_mm"] for layer in doc["layers"]}
    assert z["F.Cu"] > z["B.Cu"]
    # 0.035 + 1.53 + 0.035 = 1.6 total; F.Cu centre sits half its thickness below the top.
    assert z["F.Cu"] == pytest.approx(1.6 - 0.035 / 2, abs=1e-6)
    assert z["B.Cu"] == pytest.approx(0.035 / 2, abs=1e-6)


# ---- rules ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rules(model):
    ctx = RuleContext(model=model, transform=board_extent(model), max_frequency_hz=1e9)
    return run_rules(ctx).as_dict()


def test_plane_raster_matches_the_known_pour(model):
    """B.Cu is a split plane: 5..20 and 30..45, both 5..35, with a channel between."""
    ctx = RuleContext(model=model, transform=board_extent(model), max_frequency_hz=1e9)
    mask, ox, oy, res = _rasterize_layer(ctx, "B.Cu")

    def filled(x, y):
        return bool(mask[int((y - oy) / res), int((x - ox) / res)])

    assert filled(10, 10) is True    # left half
    assert filled(35, 20) is True    # right half
    assert filled(25, 20) is False   # the channel, all the way through
    assert filled(25, 30) is False
    assert filled(25, 10) is False


def test_overlapping_pours_do_not_cancel():
    """Separate filled_polygon entries are islands, not holes.

    XOR-ing them together — which an even-odd fill does if applied across polygons rather
    than within one — makes two overlapping pours erase each other and invents a plane gap
    that is not on the board.
    """
    tree = parse("""(kicad_pcb (version 1) (layers (0 "F.Cu" signal) (2 "B.Cu" power))
      (net 1 "GND")
      (zone (net 1) (layer "B.Cu")
        (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
        (filled_polygon (layer "B.Cu") (pts (xy 10 10) (xy 30 10) (xy 30 30) (xy 10 30)))))""")
    m = parse_board(tree)
    ctx = RuleContext(model=m, transform=board_extent(m), max_frequency_hz=1e9)
    mask, ox, oy, res = _rasterize_layer(ctx, "B.Cu")
    # The overlap must still be copper.
    assert bool(mask[int((15 - oy) / res), int((15 - ox) / res)]) is True


def test_plane_gap_measures_the_real_crossing(rules):
    """CLK runs from (19,20) to (40,20) across a channel spanning x 20..30.

    So it crosses 10 mm of missing plane, and the finding must say so. The check walks a
    raster of the plane, so the reported figure lands within one cell (``PLANE_RASTER_MM``)
    of the true crossing rather than exactly on it.
    """
    gaps = [f for f in rules["findings"] if f["rule"] == "plane-gap" and f["net"] == "CLK"]
    assert len(gaps) == 1, "one finding per (net, layer, plane), not one per track segment"
    reported = float(re.search(r"crosses ([\d.]+) mm", gaps[0]["title"]).group(1))
    assert reported == pytest.approx(10.0, abs=PLANE_RASTER_MM)
    assert gaps[0]["x"] is not None and gaps[0]["y"] is not None


def test_return_via_finding_names_the_distance(rules):
    """CLK's via is at (25,20); the only ground via is at (6,6).

    hypot(19, 14) = 23.6 mm, and the finding has to quote that number — the whole point of
    the check is telling the user how far the return current detours.
    """
    # Both signal vias are far from the single ground via, so both are flagged; this is
    # about the CLK one, whose distance can be worked out by hand.
    f = [f for f in rules["findings"]
         if f["rule"] == "return-via" and f["net"] == "CLK"]
    assert len(f) == 1
    expected = math.hypot(25 - 6, 20 - 6)
    assert f"{expected:.1f} mm away" in f[0]["detail"]
    # And the loop it implies is the round trip.
    assert f"{2 * expected:.0f} mm around" in f[0]["detail"]


def test_findings_are_locatable(rules):
    """A finding the user cannot zoom to is a complaint, not a diagnosis."""
    for f in rules["findings"]:
        if f["severity"] == "info":
            continue
        assert f["x"] is not None and f["y"] is not None, f["title"]


def test_findings_are_sorted_by_severity(rules):
    order = {"critical": 0, "warning": 1, "info": 2}
    sev = [order[f["severity"]] for f in rules["findings"]]
    assert sev == sorted(sev)


def test_net_classification():
    assert classify_net("GND") == "ground"
    assert classify_net("/AGND") == "ground"
    assert classify_net("+3V3") == "power"
    assert classify_net("VBUS") == "power"
    assert classify_net("/I2C0_SCL") == "signal"
    assert classify_net("Net-(U1-PA5)") == "signal"


def test_two_layer_board_plane_gaps_are_never_critical(rules):
    """With no dedicated plane layer, every signal shares its reference with other routing.

    The mechanism is still real, but opening a two-layer board with a wall of red teaches
    the user to close the panel.
    """
    for f in rules["findings"]:
        if f["rule"] == "plane-gap":
            assert f["severity"] != "critical"
