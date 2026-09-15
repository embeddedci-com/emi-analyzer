"""Tier B's gap port: where the board hands current to a cable (§7).

The port exists so the solve never has to contain the cable. M0 measured that this works —
swapping a 10 mm root for a 300 mm cable moved the open-circuit voltage by about 1 dB, and
composing V_oc / Z_ant reproduced a fully coupled solve to 1.3 dB — which is what makes Tier B
affordable at all: a meshed 1 m cable costs roughly eight times a board solve.
"""

from __future__ import annotations

import pytest

from emi_worker.cables.attach import EDGE_TOLERANCE_MM, Anchor
from emi_worker.openems.gapport import STUB_LENGTH_MM, fit


def anchor(**over) -> Anchor:
    base = dict(ref="USB1", x_mm=0.0, y_mm=19.2, nx=-1.0, ny=0.0,
                distance_to_edge_mm=5.5, width_mm=9.6)
    base.update(over)
    return Anchor(**base)


def test_a_westward_port_has_its_gap_just_outside_the_board():
    port, why = fit(anchor(), z_mm=1.5, gap_mm=0.2)
    assert why is None
    assert port.axis == 0 and port.sign == -1
    lo, hi = port.gap_span()
    # The gap starts at the board edge and runs outward, never into the board.
    assert hi == pytest.approx(0.0)
    assert lo == pytest.approx(-0.2)


def test_the_stub_runs_outward_from_the_gap():
    port, _ = fit(anchor(), z_mm=1.5, gap_mm=0.2)
    gap_lo, _ = port.gap_span()
    stub_lo, stub_hi = port.stub_span()
    assert stub_hi == pytest.approx(gap_lo)
    assert stub_lo == pytest.approx(gap_lo - STUB_LENGTH_MM)


def test_an_eastward_port_mirrors_it():
    port, _ = fit(anchor(nx=1.0, x_mm=60.0), z_mm=1.5, gap_mm=0.2)
    assert port.sign == 1
    lo, hi = port.gap_span()
    assert lo == pytest.approx(60.0) and hi == pytest.approx(60.2)
    stub_lo, stub_hi = port.stub_span()
    assert stub_lo == pytest.approx(60.2)
    assert stub_hi == pytest.approx(60.2 + STUB_LENGTH_MM)


def test_a_northward_port_gaps_along_y():
    port, _ = fit(anchor(nx=0.0, ny=1.0, x_mm=14.5, y_mm=40.0), z_mm=1.5, gap_mm=0.2)
    assert port.axis == 1 and port.sign == 1
    assert port.gap_span() == pytest.approx((40.0, 40.2))
    # And the width now spans x, not y.
    assert port.across_span() == pytest.approx((14.5 - 4.8, 14.5 + 4.8))


def test_the_gap_gets_grid_lines_on_both_sides():
    """A gap that falls between lines contains no cells, and openEMS then applies the lumped
    element to nothing at all — with no error, which is the failure §7 warns about."""
    port, _ = fit(anchor(), z_mm=1.5, gap_mm=0.2)
    lines = port.required_lines()
    lo, hi = port.gap_span()
    assert any(abs(v - lo) < 1e-9 for v in lines), f"no line at the gap's inner edge {lo}"
    assert any(abs(v - hi) < 1e-9 for v in lines), f"no line at the gap's outer edge {hi}"
    assert len(lines) >= 3


def test_the_domain_must_reach_past_the_stub():
    """A stub ending at the edge of the domain is terminated by the PML rather than by open
    air, which makes it a different antenna."""
    port, _ = fit(anchor(), z_mm=1.5, gap_mm=0.2)
    assert port.reach_mm() > STUB_LENGTH_MM


# ---- the refusals ------------------------------------------------------------------------

def test_a_mid_board_connector_is_refused():
    """Where its cable runs is a guess about the enclosure, not something the layout says."""
    port, why = fit(anchor(distance_to_edge_mm=EDGE_TOLERANCE_MM + 10), z_mm=1.5, gap_mm=0.2)
    assert port is None
    assert "guess about the enclosure" in why


def test_a_diagonal_exit_is_refused():
    """A diagonal stub has no single cell to put the gap in, and rounding it to an axis moves
    where the current leaves the board."""
    port, why = fit(anchor(nx=0.7, ny=0.71), z_mm=1.5, gap_mm=0.2)
    assert port is None
    assert "diagonally" in why


def test_a_nearly_axis_aligned_exit_is_accepted():
    """Board outlines are not perfectly rectangular; a couple of degrees is not a diagonal."""
    port, why = fit(anchor(nx=-0.99, ny=0.14), z_mm=1.5, gap_mm=0.2)
    assert why is None and port.axis == 0


# ---- in a built model --------------------------------------------------------------------

def _built(**over):
    from pathlib import Path

    from emi_worker.kicad import parse as parse_sexp
    from emi_worker.kicad import parse_board
    from emi_worker.kicad.normalize import _board_extent
    from emi_worker.openems.model import Port, SolveParams, build_model

    fixture = Path(__file__).parent / "fixtures" / "tiny.kicad_pcb"
    board = parse_board(parse_sexp(fixture.read_text()))
    params = SolveParams(
        roi=over.pop("roi", (4.0, 24.0, 30.0, 38.0)), frequencies_hz=[500e6, 1e9],
        ports=[Port(name="p1", x=10.0, y=30.0, layer="F.Cu", half_width_mm=0.15)],
        dx_um=250, dy_um=250, dz_um=250, air_mm=3.0, **over)
    return build_model(board, _board_extent(board), params)


def test_a_solve_without_cable_ports_is_unchanged():
    built = _built()
    assert built.cable_ports == []
    assert "cable_" not in built.doc.to_string()


def test_asking_for_a_connector_that_is_not_one_says_so():
    built = _built(cable_ports={"J99": {"type": "usb2-shielded"}})
    assert built.cable_ports == []
    assert any("not a connector on this board" in n for n in built.notes)


# ---- the dielectric ----------------------------------------------------------------------

def _dielectric_x(built) -> tuple[float, float]:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(built.doc.to_string())
    for material in root.iter("Material"):
        for box in material.iter("Box"):
            p1, p2 = box.find("P1"), box.find("P2")
            return float(p1.get("X")), float(p2.get("X"))
    raise AssertionError("no dielectric in the model")


def test_the_dielectric_is_clipped_to_the_board_outline():
    """§7. It used to fill the whole mesh, which was harmless while the mesh stopped at the
    region. Once a cable port extends the domain past the board edge it is not: the stub would
    sit on FR-4 instead of in air, which changes both where the cable resonates and how much
    it radiates.

    The region here deliberately reaches past the outline, because that is the only case where
    clipping does anything — a region entirely over the board is already clipped by the mesh.
    """
    from pathlib import Path

    from emi_worker.kicad import parse as parse_sexp
    from emi_worker.kicad import parse_board

    board = parse_board(parse_sexp(
        (Path(__file__).parent / "fixtures" / "tiny.kicad_pcb").read_text()))
    xs = [x for ring in board.outline for x, _y in ring]
    assert xs, "the fixture has no outline to clip to"
    board_hi = max(xs)

    built = _built(roi=(4.0, 24.0, board_hi + 20.0, 38.0))
    lo, hi = _dielectric_x(built)
    # The XML rounds to seven decimals, so compare at that precision rather than tighter.
    assert hi <= board_hi + 1e-6, f"dielectric reaches {hi} past a board ending at {board_hi}"
    assert float(built.mesh.x[-1]) > board_hi + 1.0, (
        "the mesh did not reach past the board, so this proves nothing"
    )


def test_a_region_entirely_over_the_board_keeps_its_dielectric():
    """The other half of it: clipping must not eat dielectric that should be there."""
    built = _built()
    lo, hi = _dielectric_x(built)
    assert lo <= float(built.mesh.x[0]) + 1e-6
    assert hi >= float(built.mesh.x[-1]) - 1e-6
