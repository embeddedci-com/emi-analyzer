"""The built-in named-part library (library/vendor.json).

These numbers are copied from manufacturers' models by a person, so the tests check what a
copying mistake would break: a missing citation, a part number two entries both answer to, or
a value in the wrong unit. The last is the likely one, and it shows up as a self-resonance in
the wrong decade for the package.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import emi_worker.components as components
from emi_worker.components.document import ComponentError, parse
from emi_worker.rules.decoupling import cap_farads

LIBRARY = Path(components.__file__).parent / "library" / "vendor.json"

#: ESL a part of each package can plausibly have, excluding the mounting loop, in henries.
#: Wide on purpose: this catches a value in the wrong unit, not a disagreement between vendors.
ESL_RANGE = {
    "0402": (0.1e-9, 0.7e-9),
    "0603": (0.15e-9, 1.0e-9),
    "0805": (0.15e-9, 1.2e-9),
    "1206": (0.2e-9, 2.0e-9),
}


def entries():
    doc = json.loads(LIBRARY.read_text())
    assert doc["format"] == "emi-component-library"
    return doc["components"]


def ids(raw):
    return raw["id"]


def test_the_library_is_not_empty_and_every_entry_parses():
    raw = entries()
    assert len(raw) >= 50
    for r in raw:
        c = parse(r)
        assert c.provenance == "vendor"
        assert c.model_type == "series_rlc"
        assert c.series_rlc().complete


def test_ids_and_part_numbers_are_unique():
    """Two entries answering to one part number would make the match depend on file order."""
    raw = entries()
    assert len({r["id"] for r in raw}) == len(raw)
    seen: dict[str, str] = {}
    for r in raw:
        for pn in parse(r).part_numbers():
            assert pn not in seen, f"{pn} is claimed by both {seen[pn]} and {r['id']}"
            seen[pn] = r["id"]


@pytest.mark.parametrize("raw", entries(), ids=ids)
def test_every_entry_cites_a_fetchable_source(raw):
    c = parse(raw)
    assert c.manufacturer
    assert c.mpn and c.match.get("lcsc", "").startswith("C")
    for what in ("ESL", "ESR"):
        s = c.cites(what)
        assert s is not None, f"{c.id}: no source for {what}"
        assert s.url.startswith("https://"), f"{c.id}: the {what} source has no URL"
        assert s.rev, f"{c.id}: the {what} source has no revision"
    if c.dc_bias:
        assert c.cites("DC bias").url.startswith("https://")


@pytest.mark.parametrize("raw", entries(), ids=ids)
def test_values_are_plausible_for_the_package(raw):
    c = parse(raw)
    rlc = c.series_rlc()
    package = c.match["package"]
    lo, hi = ESL_RANGE[package]
    assert lo <= rlc.esl_h <= hi, f"{c.id}: ESL {rlc.esl_h:g} H is outside {package}'s range"
    # The self-resonance follows from C and ESL; check it lands where the package allows.
    srf = rlc.self_resonance_hz()
    assert 1 / (2 * math.pi * math.sqrt(hi * rlc.c_f)) <= srf
    assert srf <= 1 / (2 * math.pi * math.sqrt(lo * rlc.c_f))
    assert 0.5e-3 <= rlc.esr_ohm <= 2.0
    # The model's capacitance is the part's, within what a class II ceramic loses to its
    # measurement conditions. A factor of 1000 off is a unit slip.
    nominal = cap_farads(c.match["value"])
    ratio = rlc.c_f / nominal
    if raw.get("dielectric") == "C0G":
        assert ratio == pytest.approx(1.0, rel=0.1)
    else:
        assert 0.4 <= ratio <= 1.1, f"{c.id}: C is {ratio:.2f} of the nominal value"


#: Parts whose single R-L-C should track the manufacturer's model to within 1 dB around the
#: notch. Bulk X5R parts are not here: their notch is a broad valley that one R-L-C cannot
#: follow, and they are held to the looser bound below.
TIGHT = ("CL05B104KO5NNNC", "CL05B103KB5NNNC", "CL10B473KB8NNNC", "CL10C100JB8NNNC",
         "CL21B104KCFNNNE", "CL31B104KBCNNNC")


def _db_errors(c):
    rlc = c.series_rlc()
    return [20 * math.log10(abs(rlc.impedance_at(f)) / z) for f, z in c.reference[1]]


@pytest.mark.parametrize("raw", entries(), ids=ids)
def test_the_rlc_reproduces_the_source_models_notch(raw):
    """The decoupling view reports where each capacitor stops working, which is its
    self-resonance, so the stored R-L-C must put it where the manufacturer's model does."""
    c = parse(raw)
    assert c.reference is not None, f"{c.id} has no reference |Z| to check against"
    notch, points = c.reference
    assert c.series_rlc().self_resonance_hz() == pytest.approx(notch, rel=0.05)
    assert any(f == pytest.approx(notch, rel=1e-3) for f, _ in points)
    assert max(map(abs, _db_errors(c))) <= 2.0


@pytest.mark.parametrize("mpn", TIGHT)
def test_small_parts_track_the_model_within_a_decibel(mpn):
    raw = next(r for r in entries() if r["match"]["mpn"] == mpn)
    errors = _db_errors(parse(raw))
    assert len(errors) == 5
    assert max(map(abs, errors)) <= 1.0


@pytest.mark.parametrize("raw", [r for r in entries() if r.get("dc_bias")], ids=ids)
def test_dc_bias_never_raises_the_capacitance(raw):
    c = parse(raw)
    c0 = c.series_rlc().c_f
    last = c0
    for v, c_f in c.dc_bias:
        assert v <= raw["rated_v"]
        assert c_f <= last * 1.01, f"{c.id}: C rises to {c_f:g} F at {v} V"
        last = c_f


def test_dc_bias_needs_a_source():
    raw = dict(entries()[-1])
    raw["sources"] = [s for s in raw["sources"] if "DC bias" not in s["what"]]
    raw["dc_bias"] = [{"v": 3.3, "c_f": 1e-7}]
    with pytest.raises(ComponentError, match="DC bias"):
        parse(raw)


def test_a_malformed_part_number_is_refused():
    raw = dict(entries()[-1])
    raw["match"] = dict(raw["match"], lcsc=1525)
    with pytest.raises(ComponentError, match="match.lcsc"):
        parse(raw)
    raw["match"] = dict(raw["match"], lcsc="C1525", mpn_aliases="CL05")
    with pytest.raises(ComponentError, match="mpn_aliases"):
        parse(raw)
