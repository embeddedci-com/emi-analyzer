"""The emi-component document (§11.2).

The rule worth testing is §11.2's: built-in numbers are cited, never remembered. It is
enforced rather than documented, because 0.4 nH and 0.6 nH for the same 0402 move the
self-resonance by 20 % and nobody can tell which was meant unless the document says.
"""

from __future__ import annotations

import math

import pytest

from emi_worker.components.document import (
    ComponentError,
    SeriesRLC,
    parse,
)


def doc(**over):
    base = {
        "format": "emi-component", "version": 1,
        "id": "mlcc-100n-0402-x7r", "kind": "capacitor", "name": "MLCC 100 nF 0402 X7R",
        "match": {"value": "100n", "package": "0402", "mpn": None},
        "model": {"type": "series_rlc", "c_f": 1.0e-7, "esl_h": None, "esr_ohm": None},
        "esl_includes_mount": False,
        "valid_hz": [1e5, 3e9],
        "sources": [],
    }
    base.update(over)
    return base


def test_the_design_docs_own_example_parses():
    c = parse(doc())
    assert c.id == "mlcc-100n-0402-x7r"
    assert c.model_type == "series_rlc"
    assert c.series_rlc().c_f == pytest.approx(1e-7)


def test_a_number_without_a_source_is_refused():
    with pytest.raises(ComponentError, match="says nothing about where it came from"):
        parse(doc(model={"type": "series_rlc", "c_f": 1e-7, "esl_h": 6e-10, "esr_ohm": None}))


def test_a_number_with_a_source_is_accepted():
    c = parse(doc(
        model={"type": "series_rlc", "c_f": 1e-7, "esl_h": 6e-10, "esr_ohm": 0.02},
        sources=[{"doc": "vendor data", "rev": "2024-03", "what": "ESL and ESR"}],
    ))
    rlc = c.series_rlc()
    assert rlc.complete
    assert c.cites("ESL") is not None
    assert "vendor data" in c.cites("ESR").describe()


def test_a_null_number_needs_no_source():
    """Placeholders are honest; it is a *value* that has to say where it came from."""
    c = parse(doc())
    assert not c.series_rlc().complete
    assert c.sources == ()


# ---- the circuit -----------------------------------------------------------------------

def test_self_resonance_is_the_textbook_formula():
    rlc = SeriesRLC(c_f=1e-7, esl_h=6e-10, esr_ohm=0.02)
    assert rlc.self_resonance_hz() == pytest.approx(
        1 / (2 * math.pi * math.sqrt(6e-10 * 1e-7)))


def test_there_is_no_self_resonance_without_an_esl():
    """Returning a number here would be inventing the thing the library exists to supply."""
    assert SeriesRLC(c_f=1e-7, esl_h=None, esr_ohm=None).self_resonance_hz() is None


def test_impedance_is_capacitive_below_resonance_and_inductive_above():
    rlc = SeriesRLC(c_f=1e-7, esl_h=6e-10, esr_ohm=0.02)
    srf = rlc.self_resonance_hz()
    assert rlc.impedance_at(srf / 10).imag < 0
    assert rlc.impedance_at(srf * 10).imag > 0
    # At resonance the reactance cancels and only the ESR is left.
    assert rlc.impedance_at(srf).imag == pytest.approx(0, abs=1e-6)
    assert rlc.impedance_at(srf).real == pytest.approx(0.02)


def test_impedance_without_the_numbers_is_refused_not_guessed():
    with pytest.raises(ComponentError, match="needs both an ESL and an ESR"):
        SeriesRLC(c_f=1e-7, esl_h=None, esr_ohm=None).impedance_at(1e8)


# ---- refusals --------------------------------------------------------------------------

def test_a_future_model_type_is_distinguished_from_a_broken_one():
    """'touchstone' is a valid document this build cannot use yet. Saying 'unknown type'
    would send someone looking for a typo."""
    with pytest.raises(ComponentError, match="does not resolve yet"):
        parse(doc(model={"type": "touchstone", "file": "x.s2p"}))
    with pytest.raises(ComponentError, match="unknown model type"):
        parse(doc(model={"type": "vibes"}))


def test_a_newer_version_is_refused():
    with pytest.raises(ComponentError, match="version 2"):
        parse(doc(version=2))


@pytest.mark.parametrize("field", ["id", "kind", "name"])
def test_the_required_fields_are_required(field):
    with pytest.raises(ComponentError, match=f"needs a {field}"):
        parse(doc(**{field: "  "}))


def test_an_unknown_kind_names_what_is_modelled():
    with pytest.raises(ComponentError, match="models capacitor"):
        parse(doc(kind="inductor"))


def test_valid_hz_must_be_an_increasing_pair():
    with pytest.raises(ComponentError, match="increasing"):
        parse(doc(valid_hz=[3e9, 1e5]))
    with pytest.raises(ComponentError, match="low, high"):
        parse(doc(valid_hz=[1e5]))


def test_asking_a_non_rlc_model_for_a_series_rlc_is_refused():
    c = parse(doc(
        model={"type": "mlcc_family", "esl_h_by_package": {"0402": 4.5e-10},
               "esr_ohm_by_c": [[1e-7, 0.05]]},
        sources=[{"doc": "typical for the class", "what": "ESL and ESR"}]))
    with pytest.raises(ComponentError, match="not a series R-L-C"):
        c.series_rlc()


# ---- the built-in library --------------------------------------------------------------

def test_the_built_in_library_parses_and_ships_no_uncited_numbers():
    """Every entry must load. They deliberately carry null ESL and ESR: the structure is
    complete, the numbers wait for a citable source, and parse() refuses any value added
    without one — so this cannot silently become a library of remembered numbers."""
    import json
    from pathlib import Path

    import emi_worker.components as components

    path = Path(components.__file__).parent / "library" / "mlcc.json"
    doc = json.loads(path.read_text())
    assert doc["format"] == "emi-component-library"
    assert doc["components"], "the built-in library is empty"
    for raw in doc["components"]:
        c = parse(raw)
        assert c.cites("ESL") is not None, f"{c.id} gives an ESL with no source"
        if c.model_type == "series_rlc":
            assert c.series_rlc().c_f > 0
        else:
            assert c.mlcc_family().esl_by_package
