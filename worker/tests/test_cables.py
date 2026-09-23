"""Cables: the library, the closed form, and the antenna solver (§19 cable tests 1-3).

The nec2c tests are skipped where the binary is absent, which is every developer machine and
no worker image. They are not optional in the place that matters.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

import pytest

from emi_worker.cables import CableError, built_in, get, nec, parse
from emi_worker.cables.budget import (
    SPEED_OF_LIGHT,
    UNIFORM_CURRENT_FRACTION,
    budget,
    current_for_field,
    dbuv_per_m_to_v_per_m,
    field_v_per_m,
)
from emi_worker.cables.nec import (
    Deck,
    NecError,
    ObservationRing,
    available,
    parse_output,
    segments_for,
    transmission_line_resonance_hz,
)
from emi_worker.cables.nec import run as nec_run

needs_nec = pytest.mark.skipif(not available(), reason="nec2c is not installed here")


# ---- the library -----------------------------------------------------------------------

def test_every_built_in_cable_parses():
    cables = built_in()
    assert len(cables) >= 8
    for cable in cables.values():
        assert cable.length_m > 0
        assert cable.length_m in cable.length_options_m


def test_an_unshielded_cable_cannot_have_a_bond():
    with pytest.raises(CableError, match="cannot have a"):
        parse({**_doc(), "shield": "none", "shield_bond": "360"})


def test_a_shield_that_is_not_bonded_says_it_is_not_a_shield():
    """Without an enclosure a shielded and an unshielded cable are the same antenna; an
    unbonded shield is not even the former."""
    cable = parse({**_doc(), "shield": "braid", "shield_bond": "none"})
    assert "electrically an unshielded cable" in cable.describe_shield()


def test_a_pigtail_bond_names_what_limits_it():
    cable = parse({**_doc(), "shield": "braid", "shield_bond": "pigtail", "pigtail_mm": 8})
    assert "pigtail" in cable.describe_shield()
    assert "inductance" in cable.describe_shield()


def test_an_unknown_cable_lists_the_known_ones():
    with pytest.raises(CableError, match="known: coax-pigtail"):
        get("usb9-quantum")


def _doc(**over):
    d = {
        "format": "emi-cable", "version": 1, "id": "x", "name": "X",
        "length_m": 1.0, "length_options_m": [1.0],
        "shield": "none", "shield_bond": "none", "pigtail_mm": 0,
        "far_end": "open", "routing": "horizontal", "connector_hints": [],
    }
    d.update(over)
    return d


# ---- cable test 1: the closed form -------------------------------------------------------

def test_the_closed_form_matches_the_design_docs_example():
    """§6.3: 10 uA on 1 m at 48 MHz gives 100 uV/m at 3 m."""
    e = field_v_per_m(48e6, 1.0, 10e-6, 3.0, ground_reflection=False)
    assert e * 1e6 == pytest.approx(100.0, rel=0.01)


def test_the_closed_form_inverts_exactly():
    f, length, distance = 48e6, 1.0, 3.0
    e = field_v_per_m(f, length, 10e-6, distance)
    assert current_for_field(f, length, e, distance) == pytest.approx(10e-6, rel=1e-12)


def test_the_ground_reflection_is_six_decibels():
    with_ground = field_v_per_m(48e6, 1.0, 10e-6, 3.0)
    without = field_v_per_m(48e6, 1.0, 10e-6, 3.0, ground_reflection=False)
    assert 20 * math.log10(with_ground / without) == pytest.approx(6.0)


def test_the_budget_says_where_it_stops_bounding():
    """Below lambda/10 only. For a 1 m cable that is 30 MHz — the bottom of the FCC radiated
    range — which is why the budget cannot be built from the closed form alone."""
    b = budget(get("usb2-shielded").with_length(1.0), [40e6, 100e6, 300e6])
    assert b.valid_up_to_hz() == pytest.approx(
        UNIFORM_CURRENT_FRACTION * SPEED_OF_LIGHT / 1.0)
    assert all(p.beyond_uniform_current for p in b.points)
    # And it refuses to name a tightest point from an extrapolation it does not believe.
    assert b.tightest() is None


def test_a_short_cable_is_inside_the_closed_form_where_it_matters():
    b = budget(get("debug-leads"), [40e6, 100e6, 140e6])
    assert b.valid_up_to_hz() == pytest.approx(149.9e6, rel=0.01)
    assert not b.points[0].beyond_uniform_current
    tightest = b.tightest()
    assert tightest is not None
    # A tighter limit or a higher frequency both allow less current.
    assert tightest.frequency_hz == 140e6


def test_the_budget_uses_the_standards_own_distance_and_limit():
    b = budget(get("debug-leads"), [50e6])
    assert b.distance_m == 3
    point = b.points[0]
    assert point.limit_dbuv_per_m == pytest.approx(40.0, abs=0.05)
    expected = current_for_field(
        50e6, get("debug-leads").length_m, dbuv_per_m_to_v_per_m(40.0), 3.0)
    assert point.max_current_a == pytest.approx(expected, rel=1e-9)


def test_a_conducted_standard_is_refused():
    with pytest.raises(ValueError, match="conducted"):
        budget(get("debug-leads"), [1e6], standard_id="fcc-15b-conducted-qp")


# ---- the deck --------------------------------------------------------------------------

def test_segments_are_odd_so_a_centre_feed_lands_on_one():
    for length, freq in ((1.0, 100e6), (0.2, 1e9), (5.0, 30e6)):
        assert segments_for(length, freq) % 2 == 1


def test_the_deck_always_carries_an_execution_card():
    """NEC computes only when it reaches one, and reports nothing at all when it does not.
    M0 lost a run to exactly this."""
    text = Deck(length_m=1.0, frequency_hz=100e6).to_text()
    assert "\nXQ" in text
    assert text.rstrip().endswith("EN")


def test_a_grounded_far_end_is_geometry_not_a_load():
    """A small series load on the end segment does nothing: the current there is already near
    zero. Grounding means the wire reaching the plane."""
    grounded = Deck(length_m=1.0, frequency_hz=100e6, far_end="ground").to_text()
    open_end = Deck(length_m=1.0, frequency_hz=100e6, far_end="open").to_text()
    assert grounded.count("GW ") == open_end.count("GW ") + 1


def test_every_wire_is_declared_before_the_ground_card():
    """GE has to follow every GW. A drop wire emitted after it is silently ignored."""
    text = Deck(length_m=1.0, frequency_hz=100e6, far_end="equipment").to_text()
    lines = [l.split()[0] for l in text.splitlines() if l[:2] in ("GW", "GE")]
    assert lines.index("GE") == len(lines) - 1


def test_free_space_drops_the_ground_card_and_everything_that_reaches_it():
    """``ground=False`` exists for cable test 4, and it has to remove three things at once.

    The FDTD side of that comparison is a domain absorbing on all six sides, so there is no
    image under the cable and nothing for a drop wire or a 150 Ohm termination to reach. A
    deck that kept them would model a wire ending in mid-air at a plane that is not there —
    and NEC would report it without complaint. The measurement that made this necessary: the
    first run of cable test 4 compared a NEC cable 1 m over perfect ground against an FDTD
    cable in free space and found a flat +12 dB, which was two antennas disagreeing rather
    than the composition failing.
    """
    over = Deck(length_m=1.0, frequency_hz=100e6, far_end="equipment").to_text()
    free = Deck(length_m=1.0, frequency_hz=100e6, far_end="equipment", ground=False).to_text()

    assert "GE 1" in over and "GN 1" in over and "LD 4 3" in over
    assert "GE 0" in free
    assert "GN" not in free
    assert "LD 4 3" not in free            # the 150 ohm lived in the drop
    assert free.count("GW ") == over.count("GW ") - 1   # and so did a wire

    # Still a complete deck: the arms, the feed and the execution card all survive.
    assert "EX 0 1 1 0 1.0 0.0" in free
    assert "\nXQ" in free


def test_output_with_no_results_block_is_refused():
    with pytest.raises(NecError, match="no results block"):
        parse_output("CM just an echo\nCE\nRUN TIME: 0.0\n", 100e6)


# ---- cable test 2: resonance against transmission-line theory ---------------------------

def test_the_prediction_counts_the_conductor_path_not_the_horizontal_run():
    """The feed drop adds the height once and a shorted far end adds it again. At bench
    heights that is several percent — enough to call a correct solver wrong."""
    assert transmission_line_resonance_hz(1.0, 0.0, "open") == pytest.approx(
        SPEED_OF_LIGHT / 4.0)
    assert transmission_line_resonance_hz(1.0, 0.0, "ground") == pytest.approx(
        SPEED_OF_LIGHT / 2.0)
    # A shorted far end resonates at twice an open one, which is §6.2's factor of two.
    assert (transmission_line_resonance_hz(1.0, 0.0, "ground")
            / transmission_line_resonance_hz(1.0, 0.0, "open")) == pytest.approx(2.0)
    assert transmission_line_resonance_hz(1.0, 0.05, "open") < SPEED_OF_LIGHT / 4.0


def _first_resonance(length_m: float, height_m: float, far_end: str) -> float:
    from emi_worker.cables.nec import run

    predicted = transmission_line_resonance_hz(length_m, height_m, far_end)
    lo, hi = predicted * 0.6, predicted * 1.4
    freqs = [lo + (hi - lo) * k / 40 for k in range(41)]
    xs = [run(Deck(length_m=length_m, frequency_hz=f, far_end=far_end,
                   feed="ground", height_m=height_m)).z_in.imag for f in freqs]
    for k in range(len(xs) - 1):
        if xs[k] < 0 <= xs[k + 1]:
            t = -xs[k] / (xs[k + 1] - xs[k])
            return freqs[k] + t * (freqs[k + 1] - freqs[k])
    raise AssertionError(f"no resonance found between {lo / 1e6:.0f} and {hi / 1e6:.0f} MHz")


@needs_nec
@pytest.mark.parametrize("far_end", ["open", "ground"])
def test_cable_2_resonance_within_five_percent(far_end):
    """§19's cable test 2, on the transmission-line fixture rather than the product geometry.

    The product model feeds the cable against the *board*, which is a dipole and resonates
    somewhere else entirely. Validating a solver against a prediction for a different structure
    would pass or fail for the wrong reason.
    """
    length, height = 1.0, 0.05
    predicted = transmission_line_resonance_hz(length, height, far_end)
    measured = _first_resonance(length, height, far_end)
    assert abs(measured / predicted - 1) < 0.05, (
        f"{far_end}: {measured / 1e6:.1f} MHz against {predicted / 1e6:.1f} predicted")


@needs_nec
def test_cable_2_the_far_end_moves_the_resonance():
    """§6.2's claim, measured: open and shorted differ by about a factor of two."""
    ratio = (_first_resonance(1.0, 0.05, "ground") / _first_resonance(1.0, 0.05, "open"))
    assert 1.7 < ratio < 2.3, f"ratio {ratio:.2f}"


# ---- cable test 3: chokes and bonds -----------------------------------------------------

@needs_nec
def test_cable_3_a_choke_never_raises_the_current():
    from emi_worker.cables.nec import run

    plain = run(Deck(length_m=1.0, frequency_hz=100e6, far_end="open"))
    choked = run(Deck(length_m=1.0, frequency_hz=100e6, far_end="open", choke_ohm=220.0))
    assert abs(1.0 / choked.z_in) <= abs(1.0 / plain.z_in) * 1.001


@needs_nec
def test_cable_3_a_bond_never_lengthens_the_first_resonance():
    """A ground strap is an inductance to ground that SHORTENS the antenna. If a bond ever
    moved a resonance down, the model would have it the wrong way round."""
    from emi_worker.cables.nec import run

    def resonance(bond_nh):
        freqs = [80e6 + 4e6 * k for k in range(45)]
        xs = [run(Deck(length_m=1.0, frequency_hz=f, far_end="open",
                       bond_nh=bond_nh)).z_in.imag for f in freqs]
        for k in range(len(xs) - 1):
            if xs[k] < 0 <= xs[k + 1]:
                return freqs[k] - xs[k] * (freqs[k + 1] - freqs[k]) / (xs[k + 1] - xs[k])
        return None

    plain = resonance(None)
    bonded = resonance(20.0)
    assert plain is not None and bonded is not None
    assert bonded >= plain * 0.999


# ---- the solver budget ------------------------------------------------------------------

@needs_nec
def test_the_solver_budget_needs_no_uniform_current_assumption():
    """The reason it exists. A closed-form budget for a 1 m cable is an extrapolation
    everywhere in the radiated band; this one has no such restriction."""
    from emi_worker.cables.budget import solver_budget

    b = solver_budget(get("usb2-shielded"), [30e6 * 1.2 ** k for k in range(20)])
    assert b.method == "solver"
    assert b.valid_up_to_hz() is None
    assert all(not p.beyond_uniform_current for p in b.points)
    assert b.tightest() is not None


@needs_nec
def test_a_longer_cable_resonates_lower_and_allows_less():
    """The behaviour a user should be able to predict: length sets where it radiates, and a
    better antenna carries less current before it fails."""
    from emi_worker.cables.budget import solver_budget

    freqs = [30e6 * 1.12 ** k for k in range(34)]
    short = solver_budget(get("debug-leads"), freqs)          # 0.2 m
    long = solver_budget(get("ethernet-utp"), freqs)          # 2.0 m
    assert long.radiation_peaks()[0] < short.radiation_peaks()[0]
    assert long.tightest().max_current_a < short.tightest().max_current_a


@needs_nec
def test_a_radiation_peak_is_found_where_the_reactance_never_changes_sign():
    """A parallel resonance moves the reactance through a pole, not a zero. Detecting peaks
    from a sign change would miss the strongest radiating frequency of a cable over ground —
    measured as a 14 dB peak at 40 MHz on a 1 m cable that a sign test does not see."""
    from emi_worker.cables.budget import solver_budget

    freqs = [30e6 * 1.15 ** k for k in range(12)]
    b = solver_budget(get("usb2-shielded"), freqs)
    peaks = [p for p in b.points if p.radiation_peak]
    assert peaks, "no radiation peak found in a band that contains one"
    for p in peaks:
        i = b.points.index(p)
        # It really is a local maximum in radiation, whatever the reactance did.
        assert p.e_per_amp > b.points[i - 1].e_per_amp
        assert p.e_per_amp > b.points[i + 1].e_per_amp


@needs_nec
def test_the_budget_is_the_limit_divided_by_the_radiation():
    from emi_worker.cables.budget import dbuv_per_m_to_v_per_m, solver_budget

    b = solver_budget(get("usb2-shielded"), [100e6, 200e6])
    for p in b.points:
        expected = dbuv_per_m_to_v_per_m(p.limit_dbuv_per_m) / p.e_per_amp
        assert p.max_current_a == pytest.approx(expected, rel=1e-9)


@needs_nec
def test_the_solver_budget_runs_in_well_under_a_second():
    """§4 sizes this tier in seconds on any worker. One nec2c run is about 2.5 ms."""
    import time

    from emi_worker.cables.budget import solver_budget

    started = time.monotonic()
    solver_budget(get("usb2-shielded"), [30e6 * 1.15 ** k for k in range(28)])
    assert time.monotonic() - started < 2.0


# ---- cable test 5: suggestions on real boards --------------------------------------------

def test_a_hint_matches_on_a_word_boundary_not_anywhere():
    """Substring matching is what puts a USB cable on a USBLC6 ESD diode."""
    from emi_worker.cables import suggest_for

    assert suggest_for("J1", "USB-C-SMD_TYPE-C16PIN").cable_id == "usb2-shielded"
    assert suggest_for("D1", "USBLC6-2SC6").cable_id is None
    assert suggest_for("U1", "SMAJ5.0A").cable_id is None


def test_the_longest_matching_hint_wins():
    """A board carrying both should get the more specific answer."""
    from emi_worker.cables import suggest_for

    got = suggest_for("J1", "CONN_USB_TYPE-C_16P")
    assert got.matched_hint == "type-c"


def test_an_unmatched_connector_says_so_rather_than_defaulting():
    """§5: an unassigned connector is not modelled, and the result names it. A default cable
    would be exactly the invisible assumption this is meant to avoid."""
    from emi_worker.cables import suggest_for

    got = suggest_for("J9", "lcsc:SOMETHING-ODD-2P")
    assert not got.assigned
    assert "nothing in the library matches" in got.reason


def test_a_suggestion_quotes_what_it_matched_on():
    from emi_worker.cables import suggest_for

    got = suggest_for("RJ1", "C54408_RJ45-TH_HR911130A")
    assert got.cable_id == "ethernet-utp"
    assert "rj45" in got.reason and "HR911130A" in got.reason


@pytest.mark.skipif(
    not __import__("os").environ.get("EMI_TEST_BOARDS"),
    reason="EMI_TEST_BOARDS is not set to a directory of real boards",
)
def test_cable_5_no_diode_package_gets_a_cable():
    """§19's cable test 5, on the four real boards.

    The trap is real and on three of them: `SMA_L4.3-W2.6-LS5.2-RD` is a DO-214AC diode
    package, not an SMA jack. So is `PCMF2US`, a common-mode filter whose part number contains
    "conn". Recognition is delegated to rules/emc.py rather than reimplemented here, which is
    what keeps the two from drifting into disagreeing about it.

    Known limitation, asserted so it is a decision rather than a surprise: a genuine connector
    carrying a non-connector reference is missed too — `U4` on solar-ppm is a real 2-pin
    terminal block. Erring this way keeps false cables off the board at the cost of a real
    connector going unlisted, and §5 already requires the user to confirm every assignment.
    """
    import collections
    from pathlib import Path

    from emi_worker.cables import suggest_all
    from emi_worker.kicad import parse as parse_sexp
    from emi_worker.kicad import parse_board
    from emi_worker.rules.emc import _is_connector

    root = Path(__import__("os").environ["EMI_TEST_BOARDS"])
    boards = [p for p in sorted(root.glob("*/*.kicad_pcb"))
              if not p.name.startswith("_autosave")]
    assert boards

    checked = 0
    for path in boards:
        board = parse_board(parse_sexp(path.read_text()))
        by_ref: dict[str, list] = collections.defaultdict(list)
        for pad in board.pads:
            if pad.ref:
                by_ref[pad.ref].append(pad)
        assigned = {s.ref for s in suggest_all(board) if s.assigned}
        for ref, pads in by_ref.items():
            footprint = (pads[0].footprint or "").lower()
            looks_like_one = any(h in footprint for h in ("sma", "usb", "rj45", "conn"))
            if looks_like_one and not _is_connector(ref, pads):
                checked += 1
                assert ref not in assigned, (
                    f"{path.parent.name}/{ref} ({pads[0].footprint}) was given a cable")
    assert checked >= 5, f"only {checked} footprint traps found; expected the diode packages"


# ---- the cable-resonance rule ------------------------------------------------------------

def _rule_ctx(assignments: dict, clock_hz: float = 0.0):
    from emi_worker.kicad import parse as parse_sexp
    from emi_worker.kicad import parse_board
    from emi_worker.kicad.normalize import _board_extent
    from emi_worker.rules import settings as settings_mod
    from emi_worker.rules.model import RuleContext

    fixture = (__import__("pathlib").Path(__file__).parent / "fixtures" / "tiny.kicad_pcb")
    board = parse_board(parse_sexp(fixture.read_text()))
    doc = {"cables": {"connectors": assignments}}
    if clock_hz:
        doc["rules"] = {"cable-resonance": {"params": {"clock_hz": clock_hz}}}
    cfg = settings_mod.load(("test", doc))
    assert not cfg.warnings, cfg.warnings
    return RuleContext(model=board, transform=_board_extent(board),
                       max_frequency_hz=1e9, settings=cfg)


def test_cable_assignments_take_a_name_or_a_block():
    ctx = _rule_ctx({"J1": "usb2-shielded", "J2": {"type": "ethernet-utp", "length_m": 5.0}})
    assert ctx.settings.cables["J1"] == {"type": "usb2-shielded"}
    assert ctx.settings.cables["J2"]["length_m"] == 5.0


def test_an_unknown_solver_is_warned_about_and_the_default_kept():
    from emi_worker.rules import settings as settings_mod

    cfg = settings_mod.load(("test", {"cables": {"solver": "telepathy"}}))
    assert cfg.cable_solver == "nec2c"
    assert any("telepathy" in w for w in cfg.warnings)


def test_a_connector_with_no_assignment_produces_nothing():
    """§5: an unassigned connector is not modelled. Guessing one here would put a finding on
    a cable the user never said existed."""
    from emi_worker.rules.cable_resonance import check_cable_resonance

    assert list(check_cable_resonance(_rule_ctx({}))) == []


def test_none_is_a_real_answer_and_produces_nothing():
    """A debug header that is never cabled in the product. Kept in the assignments so the
    connector counts as decided, but nothing is modelled for it."""
    from emi_worker.rules.cable_resonance import check_cable_resonance

    assert list(check_cable_resonance(_rule_ctx({"J7": "none"}))) == []


@needs_nec
def test_the_rule_reports_where_a_cable_radiates_best():
    from emi_worker.rules.cable_resonance import check_cable_resonance

    found = list(check_cable_resonance(_rule_ctx({"J1": "usb2-shielded"})))
    assert len(found) == 1
    assert found[0].severity == "info"
    assert "radiates best" in found[0].title


@needs_nec
def test_the_rule_warns_when_a_harmonic_lands_on_a_peak():
    """§4's question, and the whole point of the tier: not whether a cable radiates — they all
    do — but whether it radiates at a frequency this board produces."""
    from emi_worker.rules.cable_resonance import check_cable_resonance

    found = list(check_cable_resonance(_rule_ctx({"J1": "usb2-shielded"}, clock_hz=25e6)))
    assert len(found) == 1
    assert found[0].severity == "warning"
    assert "harmonic" in found[0].title.lower()
    # The current quoted is the one at the frequency that lands, which is the number a
    # designer can act on — not the tightest point somewhere else in the band.
    assert "at its tightest point overall" in found[0].detail


@needs_nec
def test_the_rule_costs_a_fraction_of_a_second():
    """It runs on every upload, so it has to be cheap enough not to be noticed."""
    import time

    from emi_worker.rules.cable_resonance import check_cable_resonance

    ctx = _rule_ctx({"J1": "usb2-shielded", "J2": "ethernet-utp"})
    started = time.monotonic()
    list(check_cable_resonance(ctx))
    assert time.monotonic() - started < 2.0


# ---- peak detection is grid-independent --------------------------------------------------

def _peaks(points: int) -> list[float]:
    from emi_worker.cables.budget import solver_budget

    step = (1.2e9 / 30e6) ** (1.0 / (points - 1))
    return solver_budget(
        get("usb2-shielded"), [30e6 * step ** k for k in range(points)]).radiation_peaks()


@needs_nec
def test_peak_positions_do_not_move_with_the_grid():
    """Two bugs guarded here, both found by measuring rather than reasoning.

    Judging a peak against its two neighbours made detection depend on grid spacing: adjacent
    points move closer as the grid gets finer, so a BROAD resonance rises less between them
    and a finer grid found FEWER peaks. A 40 MHz resonance was found on a 15 % grid and missed
    on an 8 % one.

    Then a purely fractional window had the opposite problem high up. A cable's resonances are
    spaced evenly in absolute frequency — 150 MHz for a 1 m cable — so ±25 % is narrower than
    the spacing at 40 MHz and wider at 800, where the window covers several resonances and
    which one it calls the peak depends on sampling. The highest peak wandered between 632 and
    796 MHz until the window was capped at a third of the spacing.

    A finer grid still resolves MORE peaks above a few hundred megahertz, which is the real
    physics rather than instability: they are genuinely every 150 MHz up there.
    """
    coarse, fine = _peaks(48), _peaks(64)
    assert coarse and fine
    for f in coarse:
        assert any(abs(g - f) <= f * 0.1 for g in fine), f"{f / 1e6:.0f} MHz moved on a finer grid"


@needs_nec
def test_the_lowest_peaks_are_stable_on_any_usable_grid():
    """The ones that matter most: sparse, well separated, and where a cable does its damage."""
    for points in (28, 36, 48, 64):
        got = _peaks(points)
        assert got, f"{points}-point grid found no peaks at all"
        # 45 MHz at the 0.8 m table height. It was 39 MHz when the cable was modelled 1 m up:
        # the same structure at 1.0 m gives 38 MHz with today's ring and segmentation.
        assert abs(got[0] - 45e6) < 45e6 * 0.15, f"{points} points put the first peak at {got[0]}"


@needs_nec
def test_the_tightest_point_lands_on_a_peak():
    """Where a cable radiates best is where it may carry least. If the two disagree, one of
    them is being computed wrong."""
    from emi_worker.cables.budget import solver_budget

    step = (1.2e9 / 30e6) ** (1.0 / 47)
    b = solver_budget(get("usb2-shielded"), [30e6 * step ** k for k in range(48)])
    tightest = b.tightest()
    assert any(abs(tightest.frequency_hz - f) <= f * 0.05 for f in b.radiation_peaks())


@needs_nec
def test_a_grid_too_coarse_to_resolve_peaks_says_so():
    """An empty peak list otherwise reads as "this cable has no resonance" when it may only
    mean "nobody looked closely enough"."""
    from emi_worker.cables.budget import solver_budget

    step = (1.2e9 / 30e6) ** (1.0 / 11)
    coarse = solver_budget(get("usb2-shielded"), [30e6 * step ** k for k in range(12)])
    assert coarse.grid_too_coarse()

    step = (1.2e9 / 30e6) ** (1.0 / 47)
    fine = solver_budget(get("usb2-shielded"), [30e6 * step ** k for k in range(48)])
    assert not fine.grid_too_coarse()


# ---- the cable run kind -----------------------------------------------------------------

def test_the_worker_advertises_the_cable_kind_only_with_a_solver():
    """Advertising it without nec2c would let the server hand this worker work it can only
    fail, which is the rule every other kind already follows."""
    import shutil

    from emi_worker.config import Config, detect_capabilities

    caps = detect_capabilities(Config(server_url="http://x", api_key="k", name="test"))
    assert ("cable" in caps.kinds) == (shutil.which("nec2c") is not None)
    assert "ingest" in caps.kinds


def test_the_cable_stage_is_registered():
    from emi_worker.stages import STAGES

    assert "cable" in STAGES


# ---- cable tests 1, 2 and 3 (§19) ------------------------------------------------------
#
# These are the three gates that need only the antenna solver, which is milliseconds a point.
# They were listed and never run; each one checks something the budget silently depends on.


#: Where 3 m stops being the far field: below c/(2*pi*d) the near-field terms of a short
#: radiator dominate and no far-field formula applies there. 15.9 MHz at 3 m.
FAR_FIELD_MIN_HZ = SPEED_OF_LIGHT / (2 * math.pi * 3.0)


@needs_nec
def test_cable_1_the_closed_form_agrees_in_shape_and_stays_conservative():
    """§6.3's closed form against nec2c, in the window where both are entitled to an opinion.

    **The gate as originally written could not be met, and the reason is physics.** It asked for
    agreement within 1 dB for "a 1 m wire with uniform current". A fed wire does not carry
    uniform current: it tapers towards the open end, and a perfectly triangular taper would put
    the solver 6 dB below a uniform-current formula. Measured here it is 3.5-4 dB below, between
    uniform and triangular, which is what a real distribution looks like.

    So this checks the two things worth checking, which between them catch every error the
    closed form could plausibly contain -- a wrong K, a missing or doubled 2*pi, a dropped
    ground reflection, an exponent on f or L:

    * **Shape.** The ratio is constant across the band. Both must scale as f*L.
    * **Direction and size.** The closed form is conservative -- it predicts more field per amp,
      so the budget it computes is tighter -- by somewhere between 0 and the 6 dB a fully
      triangular current would give.

    The window is narrow and that is the point: for a 1 m cable observed at 3 m it is 16 to
    30 MHz, one third of an octave. This is the measurement behind the statement that the closed
    form cannot be the engine.
    """
    from emi_worker.cables.budget import field_v_per_m, wavelength_m

    ring = ObservationRing(distance_m=3.0)
    tested = [(f, 0.3) for f in (20e6, 40e6, 60e6, 80e6)]
    deltas = []
    for f, length_m in tested:
        # Free space, as the gate says: no ground, no image, and so no +6 dB.
        deck = Deck(length_m=length_m, frequency_hz=f, height_m=1.0, board_span_m=0.1,
                    far_end="open", ground=False, ring=ring)
        solver = nec_run(deck).e_per_amp()
        closed = field_v_per_m(f, length_m, 1.0, 3.0, ground_reflection=False)
        assert solver > 0 and closed > 0
        deltas.append(20.0 * math.log10(solver / closed))

    # Shape: a constant offset, not a drifting one. A units or exponent error shows up here as
    # a slope, which is why it is checked separately from the offset itself.
    far = [d for (f, _), d in zip(tested, deltas) if f >= 2 * FAR_FIELD_MIN_HZ]
    assert len(far) >= 3
    assert max(far) - min(far) <= 1.5, f"the ratio drifts with frequency: {deltas}"

    # Direction and size: conservative, by less than a fully triangular current would give.
    for d in far:
        assert -6.5 <= d <= 0.5, f"closed form is {d:+.2f} dB from the solver"

    # And the window really is as narrow as claimed for a 1 m cable: lambda/10 at the
    # far-field boundary is already shorter than the cable.
    assert wavelength_m(FAR_FIELD_MIN_HZ) / 10.0 < 2.0


@needs_nec
def test_cable_1_the_two_are_not_expected_to_agree_in_the_near_field():
    """Below c/(2*pi*d) the 3 m point is inside the near field, where a far-field formula has
    nothing to say. Recorded as a test so nobody later "fixes" the closed form to match a solver
    reading it was never meant to reproduce: at 5 MHz the gap is 25 dB, and every bit of that is
    the 1/r^2 and 1/r^3 terms the formula does not contain.
    """
    from emi_worker.cables.budget import field_v_per_m

    f = 5e6
    assert f < FAR_FIELD_MIN_HZ
    solver = nec_run(Deck(length_m=1.0, frequency_hz=f, height_m=1.0, board_span_m=0.1,
                          far_end="open", ground=False,
                          ring=ObservationRing(distance_m=3.0))).e_per_amp()
    closed = field_v_per_m(f, 1.0, 1.0, 3.0, ground_reflection=False)
    assert 20.0 * math.log10(solver / closed) > 10.0


@needs_nec
@pytest.mark.parametrize("far_end,tolerance", [("open", 0.05), ("ground", 0.05)])
def test_cable_2_a_wire_over_ground_resonates_where_transmission_line_theory_says(
    far_end, tolerance
):
    """Open at the far end resonates at a quarter wave, shorted at a half.

    The fixture feeds against the **plane**, not against the board: those are different
    structures that resonate at different frequencies, and conflating them is how a solver gets
    validated against a prediction for something else. `feed="ground"` exists for exactly this.
    """
    length_m, height_m = 1.0, 0.05
    predicted = transmission_line_resonance_hz(length_m, height_m, far_end)

    freqs = np.geomspace(predicted * 0.6, predicted * 1.6, 61)
    reactance = []
    for f in freqs:
        deck = Deck(length_m=length_m, frequency_hz=float(f), height_m=height_m,
                    far_end=far_end, feed="ground",
                    ring=ObservationRing(distance_m=3.0))
        reactance.append(nec_run(deck).z_in.imag)

    crossings = [
        float(np.exp(np.log(freqs[i]) + (-reactance[i] / (reactance[i + 1] - reactance[i]))
                     * (np.log(freqs[i + 1]) - np.log(freqs[i]))))
        for i in range(len(freqs) - 1)
        if reactance[i] < 0 <= reactance[i + 1]
    ]
    assert crossings, f"no series resonance found near {predicted/1e6:.0f} MHz"
    found = crossings[0]
    error = abs(found - predicted) / predicted
    assert error <= tolerance, (
        f"{far_end}: predicted {predicted/1e6:.1f} MHz, found {found/1e6:.1f} MHz "
        f"({error*100:.1f} %)"
    )


@needs_nec
def test_cable_3_a_choke_never_raises_the_current():
    """A common-mode choke is a series impedance. It can only reduce the current it carries.

    Stated as a monotonicity check rather than a value check because that is the property the
    UI relies on: a what-if that offered a choke and showed a *worse* number would be either a
    sign error or a units error, and both are easy to make in a term that is added to an
    impedance.
    """
    ring = ObservationRing(distance_m=3.0)
    for f in (50e6, 150e6, 400e6):
        currents = []
        for choke in (None, 100.0, 1000.0):
            deck = Deck(length_m=1.0, frequency_hz=f, height_m=1.0, far_end="open",
                        choke_ohm=choke, ring=ring)
            z = nec_run(deck).z_in
            currents.append(abs(1.0 / z) if z != 0 else 0.0)
        assert currents[0] >= currents[1] >= currents[2] - 1e-18, (
            f"{f/1e6:g} MHz: a choke raised the current {currents}"
        )


@needs_nec
def test_cable_3_a_bond_never_lengthens_the_first_resonance():
    """A ground strap at the board end SHORTENS the antenna, so its resonance moves up.

    The sign here is the whole point: a bond is an inductance to ground, and it is easy to add
    it as though it were a series element in the cable — which would lower the resonance and
    make the tool recommend a change that moves a harmonic onto a peak instead of off one.
    """
    ring = ObservationRing(distance_m=3.0)
    freqs = np.geomspace(40e6, 400e6, 80)

    def first_peak(bond_nh):
        best_f, best_e = 0.0, -1.0
        for f in freqs:
            deck = Deck(length_m=1.0, frequency_hz=float(f), height_m=1.0, far_end="open",
                        bond_nh=bond_nh, ring=ring)
            e = nec_run(deck).e_per_amp()
            if e > best_e:
                best_f, best_e = float(f), e
        return best_f

    unbonded = first_peak(None)
    bonded = first_peak(10.0)
    assert bonded >= unbonded * 0.99, (
        f"a 10 nH bond moved the peak down, {unbonded/1e6:.0f} -> {bonded/1e6:.0f} MHz"
    )


# ---- the stage itself -------------------------------------------------------------------
#
# The cable stage had no test that ran it. `test_the_cable_stage_is_registered` checks the
# dispatch table, which is not the same thing: the stage called `load_board(ctx, url, info)`
# against a one-argument signature and raised TypeError on every real run. Nothing caught it
# because nothing had ever executed the function, and the run kind was separately blocked by a
# database constraint, so no user could reach it either.


class _FakeClient:
    """Just enough of the worker client to run a stage."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.uploads: dict[str, bytes] = {}

    def run_input(self, token):
        return {"input_url": "http://blob.invalid/board"}

    def download(self, url):
        return self.payload

    def progress(self, token, stage, pct, message="", **fields):
        pass

    def upload_artifact(self, token, name, blob, content_type):
        self.uploads[name] = blob
        return {"name": name, "size_bytes": len(blob)}


def _cable_context(tmp_path, payload: bytes, params: dict):
    from emi_worker.client import RunToken
    from emi_worker.stages import StageContext

    client = _FakeClient(payload)
    ctx = StageContext(
        client=client,
        token=RunToken(token="t", run_id="cable-run", jti="j", expires_in=3600),
        run={"params": params},
        workdir=str(tmp_path),
        cores=1, max_cells=10**9, should_stop=lambda: False,
    )
    return ctx, client


@needs_nec
def test_the_cable_stage_runs_on_a_real_board(tmp_path):
    """End to end on a bare .kicad_pcb: the shape of failure that reached production.

    Asserts the artifact is produced and is readable, and that every connector the board has
    appears in exactly one of the two lists -- assigned or unassigned -- because that split is
    what stops an undeclared connector being read as one that carries nothing.
    """
    from emi_worker.stages.cable import run_cable

    board_path = (Path(__import__("os").environ.get("EMI_TEST_BOARDS", "/nonexistent"))
                  / "solar-ppm" / "solar-ppm.kicad_pcb")
    if not board_path.exists():
        pytest.skip("EMI_TEST_BOARDS does not contain solar-ppm/solar-ppm.kicad_pcb")

    ctx, client = _cable_context(
        tmp_path, board_path.read_bytes(),
        {"standard_id": "fcc-15b-radiated-3m",
         "connectors": {"USB1": {"type": "usb2-shielded", "length_m": 1.0}}},
    )
    result = run_cable(ctx)

    assert "cables.json" in client.uploads
    doc = json.loads(client.uploads["cables.json"])
    assert doc["format_version"] >= 1
    assert doc["standard_id"] == "fcc-15b-radiated-3m"
    assert doc["assumptions"], "a result with no stated assumptions hides its own model"

    refs = {c["ref"] for c in doc["cables"]} | {u["ref"] for u in doc["unassigned"]}
    assert "USB1" in refs
    assert len({c["ref"] for c in doc["cables"]} & {u["ref"] for u in doc["unassigned"]}) == 0

    modelled = [c for c in doc["cables"] if c.get("cable_id")]
    assert modelled, "USB1 was assigned a cable and produced no budget"
    assert modelled[0]["points"], "a modelled cable with no points has no budget"
    assert result.summary["stage"] == "cable"


@needs_nec
def test_the_cable_stage_reads_a_settings_document_from_the_archive(tmp_path):
    """Assignments committed beside the board are what make a cable run reproducible in CI.

    The run's own params win over them, because the params are what the user just chose in the
    Cables tab; this checks the file layer is consulted at all, which the broken call could not
    have done.
    """
    import io
    import zipfile

    from emi_worker.stages.cable import run_cable

    board_path = (Path(__import__("os").environ.get("EMI_TEST_BOARDS", "/nonexistent"))
                  / "solar-ppm" / "solar-ppm.kicad_pcb")
    if not board_path.exists():
        pytest.skip("EMI_TEST_BOARDS does not contain solar-ppm/solar-ppm.kicad_pcb")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("solar-ppm.kicad_pcb", board_path.read_text())
        # JSON rather than YAML on purpose: settings.py reads YAML only when PyYAML is
        # importable, and the worker image does not carry it. A test written in YAML passes
        # on a developer machine and skips the parse silently in the image that ships.
        z.writestr(
            "emi.rules.json",
            json.dumps({"cables": {"connectors": {"USB1": {"type": "usb2-shielded"}}}}),
        )

    ctx, client = _cable_context(tmp_path, buf.getvalue(), {})
    run_cable(ctx)

    doc = json.loads(client.uploads["cables.json"])
    assigned = {c["ref"] for c in doc["cables"] if c.get("cable_id")}
    assert "USB1" in assigned, "the committed settings document was not read"


# ---- where the receiving antenna is ------------------------------------------------------

def _wire_distance(point, x_min, x_max, height):
    x, y, _ = point
    nearest = min(max(x, x_min), x_max)
    return math.hypot(x - nearest, y)


@pytest.mark.parametrize("length_m", [0.2, 1.0, 2.0, 3.0, 10.0])
def test_the_antenna_is_never_closer_than_the_measurement_distance(length_m):
    """The ring was centred on the junction with a 3 m radius, so a 3 m cable ran through it."""
    deck = nec.Deck(length_m=length_m, frequency_hz=100e6)
    ring = deck.ring.points(-deck.board_span_m, length_m)
    closest = min(_wire_distance(p, -deck.board_span_m, length_m, deck.height_m) for p in ring)
    assert closest >= deck.ring.distance_m - 1e-9
    # and it is the measurement distance, not further: the scan touches the boundary circle
    assert closest == pytest.approx(deck.ring.distance_m, abs=1e-6)


def test_the_deck_puts_its_observation_points_on_that_ring():
    deck = nec.Deck(length_m=3.0, frequency_hz=100e6)
    points = [tuple(float(v) for v in line.split()[5:8])
              for line in deck.to_text().splitlines() if line.startswith("NE ")]
    assert points
    assert min(_wire_distance(p, -deck.board_span_m, 3.0, deck.height_m) for p in points) >= 3.0 - 1e-6


def test_cables_sit_at_the_same_table_height_as_the_far_field():
    from emi_worker.openems import nf2ff

    assert nec.TABLE_HEIGHT_M == nf2ff.TABLE_HEIGHT_M == nec.Deck(length_m=1, frequency_hz=1e8).height_m


def test_the_longest_cable_is_segmented_finely_at_the_top_of_the_grid():
    from emi_worker.cables.library import MAX_LENGTH_M

    lam = nec.SPEED_OF_LIGHT / 1.2e9
    assert MAX_LENGTH_M / nec.segments_for(MAX_LENGTH_M, 1.2e9) <= lam / 10
    board = nec.Deck(length_m=1, frequency_hz=1.2e9).board_span_m
    assert board / nec.segments_for(board, 1.2e9) <= lam / 10


def test_a_cable_longer_than_the_model_supports_is_refused():
    from emi_worker.cables.library import MAX_LENGTH_M

    with pytest.raises(CableError, match="not modelled"):
        get("usb2-shielded").with_length(MAX_LENGTH_M + 1)
