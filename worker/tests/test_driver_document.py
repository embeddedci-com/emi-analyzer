"""The emi-driver document (§9.2), Python half.

The TypeScript validator asserts against the same fixtures in
`webapp/src/lib/driverDocument.test.ts`. A document one accepts and the other refuses is a
user allowed to save something the worker will then reject.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emi_worker.drivers.document import (
    MAX_DOCUMENT_BYTES,
    MAX_SAMPLES,
    SOURCE_SIGMA_DB,
    SOURCES,
    parse,
)
from emi_worker.drivers.spectrum import DriverError

FIXTURES = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
            / "driver_document_fixtures.json")
DATA = json.loads(FIXTURES.read_text())


@pytest.mark.parametrize("case", DATA["valid"], ids=lambda c: c["name"])
def test_valid_documents_parse(case):
    d = parse(case["document"])
    assert d.kind == case["expected"]["kind"]
    assert d.role == case["expected"]["role"]
    assert d.name == case["expected"]["driver_name"]
    assert d.weakest_source() == case["expected"]["weakest_source"]
    assert d.sigma_db() == pytest.approx(case["expected"]["sigma_db"])


@pytest.mark.parametrize("case", DATA["invalid"], ids=lambda c: c["name"])
def test_invalid_documents_are_refused_with_a_reason(case):
    with pytest.raises(DriverError) as exc:
        parse(case["document"])
    assert case["must_mention"] in str(exc.value)


# ---- provenance ------------------------------------------------------------------------

def test_the_weakest_source_wins_not_the_average():
    """One guessed number among four measured ones still makes the driver a guess.

    Averaging provenance would hide exactly the case §17.2 exists to price.
    """
    doc = DATA["valid"][0]["document"]
    d = parse(doc)
    assert d.weakest_source() == "assumed"
    assert d.sigma_db() == SOURCE_SIGMA_DB["assumed"]


def test_an_all_measured_driver_is_the_most_confident():
    by_name = {c["name"]: c for c in DATA["valid"]}
    measured = parse(by_name["all-measured"]["document"])
    mixed = parse(by_name["trapezoid-spi-clock"]["document"])
    assert measured.sigma_db() < mixed.sigma_db()


def test_every_source_has_a_sigma_and_they_are_ordered():
    assert set(SOURCES) == set(SOURCE_SIGMA_DB)
    assert SOURCE_SIGMA_DB["scope"] <= SOURCE_SIGMA_DB["benchpod"] <= SOURCE_SIGMA_DB["datasheet"]
    assert SOURCE_SIGMA_DB["datasheet"] < SOURCE_SIGMA_DB["assumed"]


# ---- caps ------------------------------------------------------------------------------

def test_an_oversized_document_is_refused_by_its_real_size():
    doc = DATA["valid"][0]["document"]
    with pytest.raises(DriverError, match="over the"):
        parse(doc, document_bytes=MAX_DOCUMENT_BYTES + 1)
    parse(doc, document_bytes=MAX_DOCUMENT_BYTES)


def test_too_many_samples_are_refused():
    doc = {
        "format": "emi-driver", "version": 1, "name": "huge", "kind": "waveform",
        "role": "signal",
        "waveform": {
            "sample_interval_s": 1e-12,
            "samples_v": [0.0] * (MAX_SAMPLES + 1),
            "period_s": {"value": 1e-9, "source": "benchpod"},
            "source_impedance_ohm": {"value": 50, "source": "assumed"},
        },
    }
    with pytest.raises(DriverError, match="over the"):
        parse(doc)


# ---- the trapezoid handoff -------------------------------------------------------------

def test_a_parsed_trapezoid_feeds_the_spectrum_module():
    from emi_worker.drivers.spectrum import trapezoid_series

    d = parse(DATA["valid"][0]["document"])
    series = trapezoid_series(d.trapezoid(), 5)
    assert series[0][0] == pytest.approx(25e6)
    assert d.source_impedance_ohm() == 40


def test_asking_a_waveform_for_trapezoid_parameters_is_refused():
    by_name = {c["name"]: c for c in DATA["valid"]}
    d = parse(by_name["waveform-captured-edge"]["document"])
    with pytest.raises(DriverError, match="no trapezoid parameters"):
        d.trapezoid()
