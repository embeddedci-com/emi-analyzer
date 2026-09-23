"""Combining paths into a margin, and the budget that says how much it can bear (§16, §17).

The arithmetic here decides the single number a user reads off the compliance view, so the
tests are mostly about the two places it would be easy to be quietly optimistic: summing
coherent paths as if they were incoherent, and quoting a σ that belongs to a path contributing
almost nothing.
"""

from __future__ import annotations

import math

import pytest

from emi_worker.compliance.predict import (
    SIGMA_CABLE_DB,
    Outlook,
    Path,
    PathPoint,
    combine,
    combine_sigma,
    from_dbuv,
    outlook,
    phi,
    to_dbuv,
)

STD = "fcc-15b-radiated-3m"


def _path(kind, label, driver, pairs, **sigma):
    return Path(kind=kind, label=label, driver_id=driver,
                points=[PathPoint(frequency_hz=f, field_v_per_m=e) for f, e in pairs],
                sigma_terms=sigma)


def test_dbuv_round_trips():
    for v in (1e-6, 1e-3, 0.5):
        assert from_dbuv(to_dbuv(v)) == pytest.approx(v, rel=1e-12)
    assert to_dbuv(1e-6) == pytest.approx(0.0)
    assert to_dbuv(0.0) == float("-inf")


def test_one_drivers_paths_add_in_amplitude():
    """Coherent: they came from the same clock edge, and the relative phase is unknown.

    Two equal paths adding in amplitude are 6 dB up; in power they would be 3 dB. Getting this
    backwards is optimistic by 3 dB exactly where two paths matter, which is the case anyone
    would be looking at the view to understand.
    """
    a = _path("board", "board U3", "clk", [(100e6, 1e-3)])
    b = _path("cable", "cable J1", "clk", [(100e6, 1e-3)])
    [pt] = combine([a, b], [100e6], STD)
    assert pt.field_v_per_m == pytest.approx(2e-3)
    assert pt.field_dbuv_per_m - to_dbuv(1e-3) == pytest.approx(6.0206, abs=1e-3)


def test_different_drivers_add_in_power():
    a = _path("board", "board U3", "clk", [(100e6, 1e-3)])
    b = _path("board", "board U7", "buck", [(100e6, 1e-3)])
    [pt] = combine([a, b], [100e6], STD)
    assert pt.field_v_per_m == pytest.approx(math.sqrt(2) * 1e-3)
    assert pt.field_dbuv_per_m - to_dbuv(1e-3) == pytest.approx(3.0103, abs=1e-3)


def test_shares_are_marked_indicative_only_when_a_driver_has_two_paths():
    """An amplitude sum is not a power sum, so the parts stop adding to the whole (§16.4)."""
    one = combine([_path("board", "b", "clk", [(100e6, 1e-3)]),
                   _path("board", "c", "buck", [(100e6, 1e-3)])], [100e6], STD)[0]
    assert not one.shares_indicative
    assert sum(c.share for c in one.contributions) == pytest.approx(1.0)

    two = combine([_path("board", "b", "clk", [(100e6, 1e-3)]),
                   _path("cable", "c", "clk", [(100e6, 1e-3)])], [100e6], STD)[0]
    assert two.shares_indicative


def test_contributions_are_ordered_loudest_first():
    pt = combine([_path("board", "quiet", "a", [(100e6, 1e-5)]),
                  _path("cable", "loud", "b", [(100e6, 1e-3)])], [100e6], STD)[0]
    assert [c.label for c in pt.contributions] == ["loud", "quiet"]


def test_a_silent_path_contributes_nothing_and_is_not_listed():
    pt = combine([_path("board", "on", "a", [(100e6, 1e-3)]),
                  _path("cable", "off", "b", [(100e6, 0.0)])], [100e6], STD)[0]
    assert [c.label for c in pt.contributions] == ["on"]


def test_margin_is_limit_minus_level():
    pt = combine([_path("board", "b", "a", [(100e6, from_dbuv(30.0))])], [100e6], STD)[0]
    assert pt.field_dbuv_per_m == pytest.approx(30.0)
    assert pt.margin_db == pytest.approx(pt.limit_dbuv_per_m - 30.0)


# ---- the budget ------------------------------------------------------------------------

def test_sigma_is_root_sum_square():
    assert combine_sigma({"a": 3.0, "b": 4.0}) == pytest.approx(5.0)
    assert combine_sigma({}) == 0.0


def test_the_design_docs_worked_example():
    """§17.2's radiated row, so the prose and the code cannot drift apart.

    cable 4.5, driver 3, mesh normal 2, interpolation 1  ->  sigma 5.85
    margin +5.1  ->  Phi(0.87) = 81 %,  80 % range -2.4 .. +12.6
    """
    sigma = combine_sigma({"cable": 4.5, "driver": 3.0, "mesh": 2.0, "interpolation": 1.0})
    assert sigma == pytest.approx(5.85, abs=0.01)
    assert phi(5.1 / sigma) == pytest.approx(0.81, abs=0.005)
    assert 5.1 - 1.28 * sigma == pytest.approx(-2.4, abs=0.05)
    assert 5.1 + 1.28 * sigma == pytest.approx(12.6, abs=0.05)


def test_sigma_is_weighted_by_power_share():
    """A cable contributing 1 % of the power must not bring its whole 4.5 dB (§17.1).

    Taking the worst path's terms unweighted would report a cable-dominated uncertainty for a
    frequency where the board is doing all the radiating -- and then the confidence figure
    describes a different prediction from the one on screen.
    """
    board = _path("board", "board", "clk", [(100e6, 1e-3)], mesh=2.0)
    cable = _path("cable", "cable J1", "buck", [(100e6, 1e-4)], cable=SIGMA_CABLE_DB)
    o = outlook([board, cable], [100e6], STD)

    share = next(c.share for c in o.worst.contributions if c.label == "cable J1")
    assert share < 0.02
    cable_term = o.sigma_terms["cable (cable J1)"]
    assert cable_term == pytest.approx(math.sqrt(share) * SIGMA_CABLE_DB, rel=1e-9)
    assert cable_term < 0.7, "a 1 % contributor brought most of its 4.5 dB"


def test_a_shared_term_applies_whatever_the_paths_do():
    o = outlook([_path("board", "b", "clk", [(100e6, 1e-3)])], [100e6], STD,
                shared_terms={"driver provenance": 3.0})
    assert o.sigma_terms["driver provenance"] == 3.0


def test_the_worst_frequency_is_the_smallest_margin_not_the_largest_field():
    """The limit steps with frequency, so the loudest point is often not the closest one.

    40 MHz is limited at 40 dBuV/m and 300 MHz at 46. A board 5 dB louder at 300 MHz than at
    40 still has more room there, and a view that pointed at the loudest frequency would send
    someone to fix the wrong one.
    """
    p = _path("board", "b", "clk", [(40e6, from_dbuv(39.0)), (300e6, from_dbuv(44.0))])
    o = outlook([p], [40e6, 300e6], STD)

    louder = max(o.points, key=lambda q: q.field_v_per_m)
    assert louder.frequency_hz == 300e6
    assert o.worst.frequency_hz == 40e6
    assert o.worst.margin_db == pytest.approx(1.0)
    assert louder.margin_db == pytest.approx(2.0)


def test_near_misses_are_within_one_sigma_of_the_worst():
    """Confidence is evaluated at the worst frequency alone, so it is optimistic when several
    sit close; this list is what says so (§17.1)."""
    p = _path("board", "b", "clk",
              [(100e6, from_dbuv(30.0)), (200e6, from_dbuv(29.0)), (400e6, from_dbuv(5.0))],
              mesh=4.0)
    o = outlook([p], [100e6, 200e6, 400e6], STD)
    labels = {q.frequency_hz for q in o.near_misses}
    assert o.worst.frequency_hz in (100e6, 200e6)
    assert o.worst.frequency_hz not in labels
    assert 400e6 not in labels, "a point 25 dB quieter is not a near miss"


def test_no_path_radiates_and_there_is_no_margin_to_quote():
    o = outlook([_path("board", "b", "clk", [(100e6, 0.0)])], [100e6], STD)
    assert o.worst is None
    assert o.margin_db is None
    assert o.confidence_uncalibrated is None
    assert o.range_80_db is None


def test_confidence_is_a_probability_and_moves_the_right_way():
    def conf(margin_db, sigma):
        o = Outlook(standard_id=STD, points=[], worst=None, sigma_db=sigma, sigma_terms={})
        return phi(margin_db / sigma)

    assert conf(0.0, 5.0) == pytest.approx(0.5)
    assert conf(10.0, 5.0) > conf(5.0, 5.0) > conf(0.0, 5.0)
    # More uncertainty on a positive margin is less confidence, never more.
    assert conf(5.0, 10.0) < conf(5.0, 2.0)


# ---------------------------------------------------------------------------
# The shared fixtures, asserted here and in webapp/src/lib/compliance.test.ts.
# ---------------------------------------------------------------------------

import json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_FIXTURES = (_Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
             / "compliance_fixtures.json")


def _compliance_cases():
    return [(c["name"], c) for c in json.loads(_FIXTURES.read_text())["cases"]]


@pytest.mark.parametrize("name,case", _compliance_cases(),
                         ids=[c[0] for c in _compliance_cases()])
def test_matches_the_shared_compliance_fixtures(name, case):
    inp = case["input"]
    freqs = inp["frequencies_hz"]
    paths = [
        Path(kind=p["kind"], label=p["label"], driver_id=p["driver_id"],
             points=[PathPoint(frequency_hz=f, field_v_per_m=from_dbuv(v))
                     for f, v in zip(freqs, p["field_dbuv_per_m"])],
             sigma_terms=p["sigma_terms"])
        for p in inp["paths"]
    ]
    o = outlook(paths, freqs, inp["standard_id"], shared_terms=inp["shared_sigma_terms"])
    want = case["expected"]

    assert [p.frequency_hz for p in o.points] == [s["frequency_hz"] for s in want["spectrum"]]
    for got, w in zip(o.points, want["spectrum"]):
        assert got.field_dbuv_per_m == pytest.approx(w["field_dbuv_per_m"], abs=1e-9)
        assert got.margin_db == pytest.approx(w["margin_db"], abs=1e-9)
        assert got.shares_indicative == w["shares_indicative"]
        assert [c.label for c in got.contributions] == [c["label"] for c in w["contributions"]]

    assert (o.worst.frequency_hz if o.worst else None) == want["worst_frequency_hz"]
    assert o.margin_db == pytest.approx(want["margin_db"], abs=1e-9)
    assert o.sigma_db == pytest.approx(want["sigma_db"], abs=1e-9)
    assert o.confidence_uncalibrated == pytest.approx(want["confidence_uncalibrated"], abs=1e-9)
    assert [q.frequency_hz for q in o.near_misses] == want["near_misses_hz"]
