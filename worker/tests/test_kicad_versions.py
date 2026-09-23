"""One board, written in the file format of every KiCad from 5 to 10.

The KiCad tests used to run on one hand-written file in one format, so format drift between
versions was invisible until a real board hit it: a KiCad 5 board loaded with no pads at all,
a KiCad 6 pad on ``F&B.Cu`` was never drawn, and a KiCad 8 Bezier corner or a footprint's own
board cut-out left the outline with holes in it.

The fixtures in ``fixtures/kicad_versions`` are synthetic (no real board is ever committed).
5 to 9 are hand-written to each version's syntax; 10 is what ``kicad-cli pcb upgrade`` made
of 9. What differs between them:

  5   ``module``, unquoted names, fp_text references, centre-and-angle arcs, fills with no layer
  6   ``footprint``, quoted names, ``locked`` as a bare word, tstamp, a pad on ``F&B.Cu``
  7   stroke blocks, and a footprint that cuts a round hole in the board
  8   uuid, ``(locked yes)``, ``property "Reference"``, a Bezier (``gr_curve``) corner
  9   renumbered layers (B.Cu is 2, Edge.Cuts 25), a rotated footprint slot on Edge.Cuts
  10  no net table: ``(net "GND")`` inline everywhere

The board: U1 drives CLK to the header J1, C1 decouples +3V3, and a GND pour on B.Cu joins
U1, C1 and J1 through a via. It is 40 x 30 mm with one rounded corner.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from emi_worker import topology
from emi_worker.kicad import geometry as g
from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.board import ZONES_UNFILLED_NOTE
from emi_worker.kicad.normalize import normalize

FIXTURES = Path(__file__).parent / "fixtures" / "kicad_versions"
VERSIONS = {5: 20171130, 6: 20211014, 7: 20221018, 8: 20240108, 9: 20241229, 10: 20260206}


def _load(v: int):
    return parse_board(parse((FIXTURES / f"kicad{v}.kicad_pcb").read_text()))


@pytest.fixture(scope="module", params=sorted(VERSIONS), ids=lambda v: f"kicad{v}")
def board(request):
    return request.param, _load(request.param)


def test_the_file_is_the_version_it_claims(board):
    v, model = board
    assert model.version == VERSIONS[v]


def test_nets(board):
    _, model = board
    assert {n for n in model.nets if n} == {"GND", "+3V3", "CLK"}


def test_pads_have_refs_nets_and_places(board):
    _, model = board
    pads = {f"{p.ref}.{p.number}": p for p in model.pads}
    assert {k: p.net for k, p in pads.items()} == {
        "U1.1": "CLK", "U1.2": "GND", "C1.1": "+3V3", "C1.2": "GND", "J1.1": "CLK", "J1.2": "GND",
    }
    # C1 is rotated 90 degrees; its pads must land on the tracks that reach them.
    assert (pads["C1.1"].x, pads["C1.1"].y) == pytest.approx((110.0, 120.5))
    assert (pads["C1.2"].x, pads["C1.2"].y) == pytest.approx((110.0, 119.5))
    assert pads["J1.1"].is_through and pads["J1.1"].drill_mm == pytest.approx(1.0)
    assert {p.value for p in model.pads if p.ref == "C1"} == {"100nF"}


def test_every_through_hole_pad_is_drawn_on_both_layers(board):
    """KiCad 6's F&B.Cu matched no copper layer, so the pad was never drawn."""
    _, model = board
    doc, _ = normalize(model, {})
    j1 = [p for p in doc["pads"] if p["ref"] == "J1"]
    for p in j1:
        layers = set(p["layers"])
        # board.json lists only layers normalize draws the pad on.
        assert layers == {"*.Cu"} or layers == {"F.Cu", "B.Cu"}, p


def test_tracks_vias_and_zone(board):
    _, model = board
    assert len(model.tracks) == 5
    assert [(v.x, v.y, v.net) for v in model.vias] == [(110.0, 113.0, "GND")]
    assert [(z.net, z.layer) for z in model.zones] == [("GND", "B.Cu")]
    # A 37.75 x 16.75 mm pour with one corner cut off at 45 degrees.
    assert abs(g.signed_area(model.zones[0].ring)) == pytest.approx(
        37.75 * 16.75 - 4.927 ** 2 / 2, rel=0.002)
    assert not model.warnings or all("zone" not in w for w in model.warnings)


def test_outline_is_closed_round_the_corner(board):
    """The rounded corner is a KiCad 5 centre-angle arc, a three-point arc, or a Bezier."""
    _, model = board
    pts = [p for ring in model.outline for p in ring]
    assert min(x for x, _ in pts) == pytest.approx(100)
    assert max(x for x, _ in pts) == pytest.approx(140)
    assert min(y for _, y in pts) == pytest.approx(100)
    assert max(y for _, y in pts) == pytest.approx(130)
    corner = min(
        _to_segment((138.5355, 128.5355), ring[i], ring[i + 1])
        for ring in model.outline for i in range(len(ring) - 1)
    )
    assert corner < 0.05, "the rounded corner is missing from the outline"


def _to_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ((dx * dx + dy * dy) or 1)))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def test_topology(board):
    _, model = board
    topo = topology.build(model)
    assert sorted(topo["CLK"].pads) == ["J1.1", "U1.1"]
    assert sorted(topo["GND"].pads) == ["C1.2", "J1.2", "U1.2"]
    assert topo["CLK"].path("U1.1", "J1.1").length_mm == pytest.approx(20.5 + 1.5 * math.sqrt(2))


def test_a_footprint_cut_out_is_part_of_the_outline():
    """Edge.Cuts inside a footprint were skipped, so the board looked solid there."""
    circle = [r for r in _load(7).outline if len(r) > 10 and max(x for x, _ in r) < 125]
    assert len(circle) == 1
    assert all(math.dist(p, (120, 104)) == pytest.approx(1.5, abs=0.01) for p in circle[0])

    for v in (9, 10):
        # A 4 x 1 mm slot in a footprint rotated 90 degrees: tall, not wide.
        slot = [r for r in _load(v).outline if len(r) == 5 and max(x for x, _ in r) < 125]
        assert len(slot) == 1
        xs = [x for x, _ in slot[0]]
        ys = [y for _, y in slot[0]]
        assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((119.5, 120.5, 102, 106))


def test_an_unfilled_zone_is_reported_not_silently_dropped():
    """Plane checks read only fills; a zone never filled made them all go quiet."""
    text = (FIXTURES / "kicad9.kicad_pcb").read_text()
    text = re.sub(r"\(filled_polygon.*?\n    \)\n", "", text, flags=re.S)
    text = text.replace("(fill yes ", "(fill ")
    model = parse_board(parse(text))
    assert model.zones == []
    assert ZONES_UNFILLED_NOTE.format(n=1, s="") in model.warnings


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli is not installed")
@pytest.mark.parametrize("v", sorted(VERSIONS))
def test_kicad_itself_loads_the_fixture(v, tmp_path):
    """The fixtures are only worth anything if KiCad agrees they are its own format."""
    # A copy, because KiCad writes a .kicad_prl beside any board it opens.
    board = tmp_path / f"kicad{v}.kicad_pcb"
    shutil.copy(FIXTURES / board.name, board)
    out = tmp_path / "out"
    result = subprocess.run(
        ["kicad-cli", "pcb", "export", "gerbers", "--output", f"{out}/",
         "--layers", "F.Cu,B.Cu,Edge.Cuts", str(board)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert list(out.glob("*.g*")), "kicad-cli wrote no Gerbers"
