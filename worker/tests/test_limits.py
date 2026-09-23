"""Limit tables — §19's C1 and C2.

The TypeScript half asserts the same things against the same JSON in
`webapp/src/lib/limits.test.ts`.
"""

from __future__ import annotations

import math

import pytest

from emi_worker.compliance.limits import (
    LimitError,
    limit_at,
    load_standards,
    scan_to_hz,
    standard,
)


# ---- C1: the segments match the cited text ---------------------------------------------

def test_c1_class_b_radiated_matches_15_109a():
    """100 / 150 / 200 / 500 uV/m at 3 m, per 47 CFR 15.109(a)."""
    for f_mhz, want_uv in [(40, 100), (87.9, 100), (100, 150), (215, 150),
                           (300, 200), (959, 200), (1000, 500)]:
        want_db = 20 * math.log10(want_uv)
        assert limit_at("fcc-15b-radiated-3m", f_mhz * 1e6) == pytest.approx(want_db, abs=0.05)


def test_c1_class_a_radiated_matches_15_109b():
    """90 / 150 / 210 / 300 uV/m at 10 m, per 47 CFR 15.109(b)."""
    for f_mhz, want_uv in [(40, 90), (100, 150), (300, 210), (1000, 300)]:
        want_db = 20 * math.log10(want_uv)
        assert limit_at("fcc-15a-radiated-10m", f_mhz * 1e6) == pytest.approx(want_db, abs=0.05)


def test_c1_the_decibel_and_microvolt_columns_agree():
    """Catches a transcription slip in either column: they are the same number twice."""
    import json
    from emi_worker.compliance.limits import LIMITS_DIR

    for path in sorted(LIMITS_DIR.glob("*.json")):
        doc = json.loads(path.read_text())
        for raw in doc["standards"]:
            for seg in raw["segments"]:
                if "microvolts_per_m" not in seg:
                    continue
                assert seg["level_db"] == pytest.approx(
                    20 * math.log10(seg["microvolts_per_m"]), abs=0.05
                ), f"{raw['id']} at {seg['f_lo_hz'] / 1e6:g} MHz"


def test_c1_a_band_edge_takes_the_tighter_limit():
    """At 88 MHz exactly, Class B is 100 uV/m and not 150.

    A half-open interval would pick whichever segment happened to be written first and
    could pass a device that fails.
    """
    assert limit_at("fcc-15b-radiated-3m", 88e6) == pytest.approx(40.0, abs=0.05)
    assert limit_at("fcc-15b-radiated-3m", 216e6) == pytest.approx(43.5, abs=0.05)
    assert limit_at("fcc-15b-radiated-3m", 960e6) == pytest.approx(46.0, abs=0.05)
    # And just above the edge the looser one applies.
    assert limit_at("fcc-15b-radiated-3m", 88.001e6) == pytest.approx(43.5, abs=0.05)


def test_c1_the_conducted_edge_at_5_mhz_takes_the_tighter_limit():
    """15.107(a) steps UP at 5 MHz, from 56 to 60, so the tighter side is the lower band."""
    assert limit_at("fcc-15b-conducted-qp", 5e6) == pytest.approx(56.0)
    assert limit_at("fcc-15b-conducted-qp", 5.001e6) == pytest.approx(60.0)


# ---- C2: conducted interpolation -------------------------------------------------------

def test_c2_conducted_interpolates_in_log_frequency():
    """66 dBuV at 0.15 MHz, 56 at 0.5, and 61 at their geometric mean, 0.274 MHz."""
    assert limit_at("fcc-15b-conducted-qp", 0.15e6) == pytest.approx(66.0)
    assert limit_at("fcc-15b-conducted-qp", 0.5e6) == pytest.approx(56.0)
    geometric_mean = math.sqrt(0.15e6 * 0.5e6)
    assert geometric_mean == pytest.approx(0.2739e6, rel=1e-3)
    assert limit_at("fcc-15b-conducted-qp", geometric_mean) == pytest.approx(61.0)


def test_c2_the_average_detector_slopes_the_same_way():
    assert limit_at("fcc-15b-conducted-avg", 0.15e6) == pytest.approx(56.0)
    assert limit_at("fcc-15b-conducted-avg", math.sqrt(0.15e6 * 0.5e6)) == pytest.approx(51.0)
    assert limit_at("fcc-15b-conducted-avg", 0.5e6) == pytest.approx(46.0)


def test_c2_class_a_conducted_does_not_slope():
    """15.107(b) is flat in each segment; sloping it would invent 6 dB of headroom."""
    for f in (0.15e6, 0.3e6, 0.5e6):
        assert limit_at("fcc-15a-conducted-qp", f) == pytest.approx(79.0 if f < 0.5e6 else 73.0)


# ---- refusals --------------------------------------------------------------------------

def test_a_frequency_outside_the_standard_is_refused_not_extrapolated():
    with pytest.raises(LimitError, match="says nothing at"):
        limit_at("fcc-15b-radiated-3m", 10e6)
    with pytest.raises(LimitError, match="says nothing at"):
        limit_at("fcc-15b-conducted-qp", 50e6)


def test_an_unknown_standard_lists_the_known_ones():
    with pytest.raises(LimitError, match="known: fcc-15a"):
        limit_at("en-55032-b", 100e6)


def test_every_standard_is_gapless_and_declares_its_provenance():
    for std in load_standards().values():
        assert std.verified in ("standard text", "secondary sources")
        assert std.source
        assert std.clause
        for a, b in zip(std.segments, std.segments[1:]):
            assert b.f_lo_hz == a.f_hi_hz, f"{std.id} has a gap or overlap"


# ---- 15.33(b) scan range ---------------------------------------------------------------

def test_scan_range_follows_15_33b():
    assert scan_to_hz(1e6) == pytest.approx(30e6)
    assert scan_to_hz(16e6) == pytest.approx(1e9)
    assert scan_to_hz(100e6) == pytest.approx(1e9)
    assert scan_to_hz(200e6) == pytest.approx(2e9)
    assert scan_to_hz(600e6) == pytest.approx(5e9)


def test_above_1_ghz_the_scan_is_the_fifth_harmonic_capped_at_40_ghz():
    assert scan_to_hz(2e9) == pytest.approx(10e9)
    assert scan_to_hz(9e9) == pytest.approx(40e9)   # 45 GHz capped


def test_the_class_b_standard_knows_its_own_geometry():
    std = standard("fcc-15b-radiated-3m")
    assert std.distance_m == 3
    assert std.device_class == "B"
    assert std.unit == "dBuV/m"
    assert std.detector == "quasi-peak"


# ---- §15.35: which detector, and the peak limit above 1 GHz ------------------------------

def test_15_35_quasi_peak_up_to_1_ghz_and_average_above():
    """15.35(a) reads limits at or below 1000 MHz with a quasi-peak detector; 15.35(b) reads
    the ones above it with an average detector. The table used to call 960 MHz-40 GHz
    quasi-peak throughout."""
    from emi_worker.compliance.limits import detector_at

    for sid in ("fcc-15b-radiated-3m", "fcc-15a-radiated-10m"):
        assert detector_at(sid, 500e6) == "quasi-peak"
        assert detector_at(sid, 980e6) == "quasi-peak"
        assert detector_at(sid, 1000e6) == "quasi-peak", "1000 MHz is 'below or equal'"
        assert detector_at(sid, 1.001e9) == "average"
        assert detector_at(sid, 10e9) == "average"


def test_15_35b_the_peak_limit_is_20_db_above_the_average():
    """Class B above 1 GHz: 500 uV/m average (54 dBuV/m), so a 74 dBuV/m peak limit. Class A:
    300 uV/m average (49.5) and 69.5 peak."""
    from emi_worker.compliance.limits import peak_limit_at

    assert limit_at("fcc-15b-radiated-3m", 2e9) == pytest.approx(20 * math.log10(500), abs=0.05)
    assert peak_limit_at("fcc-15b-radiated-3m", 2e9) == pytest.approx(
        20 * math.log10(500) + 20, abs=0.05)
    assert limit_at("fcc-15a-radiated-10m", 2e9) == pytest.approx(20 * math.log10(300), abs=0.05)
    assert peak_limit_at("fcc-15a-radiated-10m", 2e9) == pytest.approx(
        20 * math.log10(300) + 20, abs=0.05)
    # Below 1 GHz there is no separate peak limit in the table.
    assert peak_limit_at("fcc-15b-radiated-3m", 500e6) is None


def test_a_frequency_outside_the_standard_is_out_of_range_not_an_error():
    from emi_worker.compliance.limits import in_range

    assert not in_range("fcc-15b-radiated-3m", 20e6)
    assert in_range("fcc-15b-radiated-3m", 30e6)
    assert not in_range("fcc-15b-conducted-qp", 100e6)
