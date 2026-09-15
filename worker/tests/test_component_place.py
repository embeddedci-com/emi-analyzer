"""Placing a resolved component into the mesh (§12).

Every test here is really about one rule: nothing is placed where anything is uncertain. A
board with no matched parts solves bit-for-bit as it does today, and the same logic means a
board with *some* matched parts differs only where the model is sound.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from emi_worker.components import match_part, resolve_part
from emi_worker.components.place import (
    MIN_GAP_MM,
    modelled_parts,
    plan_all,
    plan_placement,
)

C0402 = "Capacitor_SMD:C_0402_1005Metric"
ROI = (0.0, 0.0, 20.0, 20.0)


class Identity:
    """The board transform, with board space already equal to model space."""

    @staticmethod
    def pt(x, y):
        return (x, y)


@dataclass
class FakePad:
    ring: list
    layers: list


def pad(cx: float, cy: float, w: float = 0.5, h: float = 0.5, layer: str = "F.Cu") -> FakePad:
    return FakePad(
        ring=[(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
              (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)],
        layers=[layer],
    )


def a_capacitor():
    return resolve_part(match_part("C12", "100nF", C0402))


def place(pads, roi=ROI):
    return plan_placement("C12", a_capacitor(), pads, Identity(), roi)


# ---- the happy path --------------------------------------------------------------------

def test_a_horizontal_part_bridges_along_x():
    p, why = place([pad(5.0, 5.0), pad(6.0, 5.0)])
    assert why is None and p is not None
    assert p.axis == 0
    # The gap is between the facing edges: 5.25 to 5.75.
    assert p.lo == pytest.approx(5.25)
    assert p.hi == pytest.approx(5.75)
    assert p.layer == "F.Cu"


def test_a_vertical_part_bridges_along_y():
    p, _ = place([pad(5.0, 5.0), pad(5.0, 6.0)])
    assert p.axis == 1
    assert (p.lo, p.hi) == pytest.approx((5.25, 5.75))


def test_the_order_of_the_pads_does_not_matter():
    forward, _ = place([pad(5.0, 5.0), pad(6.0, 5.0)])
    backward, _ = place([pad(6.0, 5.0), pad(5.0, 5.0)])
    assert (forward.lo, forward.hi) == pytest.approx((backward.lo, backward.hi))


def test_three_adjacent_cells_with_four_lines():
    """A series R-L-C is three elements, so the gap needs dividing exactly three ways."""
    p, _ = place([pad(5.0, 5.0), pad(6.0, 5.0)])
    lines = p.required_lines()
    assert len(lines) == 4
    assert lines[0] == pytest.approx(p.lo)
    assert lines[-1] == pytest.approx(p.hi)
    steps = [b - a for a, b in zip(lines, lines[1:])]
    assert steps == pytest.approx([steps[0]] * 3)

    cells = p.cells(z_mm=1.6)
    assert len(cells) == 3
    # Adjacent: each cell starts where the last one ended. A gap would be PEC and short the
    # element; an overlap would make two elements share a cell and only one would apply.
    for (_, end), (start, _) in zip(cells, cells[1:]):
        assert end[0] == pytest.approx(start[0])
    assert all(p1[2] == 1.6 and p2[2] == 1.6 for p1, p2 in cells)


def test_the_element_spans_only_where_the_pads_overlap():
    """An element wider than the narrower pad would bridge to copper that is not there."""
    p, _ = place([pad(5.0, 5.0, h=1.0), pad(6.0, 5.0, h=0.4)])
    assert (p.across_hi - p.across_lo) == pytest.approx(0.4)


# ---- the refusals ----------------------------------------------------------------------

def test_pads_on_different_layers_are_skipped():
    p, why = place([pad(5.0, 5.0, layer="F.Cu"), pad(6.0, 5.0, layer="B.Cu")])
    assert p is None
    assert "different layers" in why


def test_an_off_axis_rotation_is_skipped():
    """A capacitor at 45 degrees has no in-plane axis to bridge along, and guessing one
    would place the element across the wrong pair of edges."""
    p, why = place([pad(5.0, 5.0), pad(6.0, 6.0)])
    assert p is None
    assert "off-axis" in why


def test_a_part_straddling_the_region_boundary_is_skipped():
    p, why = place([pad(19.8, 5.0), pad(20.8, 5.0)])
    assert p is None
    assert "straddles the region boundary" in why


def test_a_gap_too_small_to_divide_is_skipped():
    """Below three cells the mesher collapses them and openEMS applies one element where
    three were meant — which is the parallel R-L-C K1 said is wrong at every frequency."""
    tiny = MIN_GAP_MM / 2
    p, why = place([pad(5.0, 5.0, w=1.0), pad(6.0 - (1.0 - tiny), 5.0, w=1.0)])
    assert p is None
    assert "too small to divide" in why


def test_a_part_with_the_wrong_number_of_pads_is_skipped():
    p, why = place([pad(5.0, 5.0)])
    assert p is None and "not two" in why


def test_pads_that_do_not_overlap_across_the_gap_are_skipped():
    p, why = place([pad(5.0, 5.0, h=0.4), pad(6.0, 6.0, h=0.4)])
    assert p is None


# ---- the plan --------------------------------------------------------------------------

def test_plan_all_separates_placed_from_skipped():
    resolved = {"C1": a_capacitor(), "C2": a_capacitor()}
    pads = {
        "C1": [pad(5.0, 5.0), pad(6.0, 5.0)],
        "C2": [pad(5.0, 8.0, layer="F.Cu"), pad(6.0, 8.0, layer="B.Cu")],
    }
    plan = plan_all(resolved, pads, Identity(), ROI)
    assert [p.ref for p in plan.placements] == ["C1"]
    assert plan.skipped[0][0] == "C2"
    assert "different layers" in plan.skipped[0][1]


def test_a_model_with_gaps_is_reported_not_placed():
    """A component that resolved but has no ESL cannot be a series R-L-C."""
    from emi_worker.components.document import Resolved, SeriesRLC

    incomplete = Resolved(ref="C9", component_id="x", component_name="x",
                          rlc=SeriesRLC(c_f=1e-7, esl_h=None, esr_ohm=None),
                          gaps=["no ESL, so there is no self-resonance to report"])
    plan = plan_all({"C9": incomplete}, {"C9": [pad(5.0, 5.0), pad(6.0, 5.0)]},
                    Identity(), ROI)
    assert not plan.placements
    assert "no ESL" in plan.skipped[0][1]


def test_required_lines_are_collected_per_axis():
    resolved = {"C1": a_capacitor(), "C2": a_capacitor()}
    pads = {"C1": [pad(5.0, 5.0), pad(6.0, 5.0)],       # along x
            "C2": [pad(9.0, 9.0), pad(9.0, 10.0)]}      # along y
    plan = plan_all(resolved, pads, Identity(), ROI)
    assert len(plan.required_x()) == 4
    assert len(plan.required_y()) == 4


def test_the_modelled_parts_list_names_the_model_and_its_provenance():
    """§12: results gain a list of reference, model, owner and source."""
    plan = plan_all({"C12": a_capacitor()},
                    {"C12": [pad(5.0, 5.0), pad(6.0, 5.0)]}, Identity(), ROI)
    parts = modelled_parts(plan)
    assert len(parts) == 1
    entry = parts[0]
    assert entry["ref"] == "C12"
    assert entry["component_id"] == "generic-mlcc-100n-0402"
    assert entry["generic"] is True
    assert entry["source"]
    assert entry["self_resonance_hz"] == pytest.approx(23.7e6, rel=0.02)
