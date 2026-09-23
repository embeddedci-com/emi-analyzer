"""§17.3's gate: an incomplete estimate carries no number at all.

The point of these tests is that each gap is *reported* rather than silently absorbed. A margin
computed from two of three connectors is not a rough answer, it is an answer about a different
product -- and the arithmetic that produces it cannot tell the difference.
"""

from __future__ import annotations

from emi_worker.compliance.completeness import check

FULL = dict(
    drivers=[{"net": "CLK"}],
    source_nets=["CLK"],
    connectors=["J1"],
    cable_assignments={},
    modelled_cables={"J1": {"cable_id": "usb2-shielded", "length_m": 1.0}},
    excited_ports=["p1"],
    far_field_ports=["p1"],
    covered={"board": (30e6, 1e9), "cable J1": (30e6, 1e9)},
    required_band=(30e6, 1e9),
    undriven_harmonics={},
    enclosure="plastic",
    power="dc",
    power_entry_found=True,
)


def test_everything_present_is_complete():
    assert check(**FULL).complete


def _one(**over):
    c = check(**{**FULL, **over})
    assert not c.complete
    return c


def test_an_undeclared_unmodelled_connector_blocks_the_number():
    c = _one(connectors=["J1", "J2"])
    assert any(g.key == "cable:J2" for g in c.gaps)
    assert any("not modelled" in g.message for g in c.gaps)


def test_a_connector_declared_as_carrying_nothing_is_an_answer():
    """'none' is a decision; absent is not. Collapsing the two says a board is covered."""
    c = check(**{**FULL, "connectors": ["J1", "J2"],
                 "cable_assignments": {"J2": {"type": None}}})
    assert c.complete
    c = check(**{**FULL, "connectors": ["J1", "J2"], "cable_assignments": {"J2": "none"}})
    assert c.complete


def test_a_declared_cable_must_be_the_one_the_solve_modelled():
    c = _one(cable_assignments={"J1": {"type": "rj45-cat5e"}})
    assert any(g.key == "cable-mismatch:J1" for g in c.gaps)
    c = _one(cable_assignments={"J1": {"type": "usb2-shielded", "length_m": 2.0}})
    assert any(g.key == "cable-mismatch:J1" for g in c.gaps)
    c = _one(modelled_cables={}, cable_assignments={"J1": {"type": "usb2-shielded"}})
    assert any(g.key == "cable-solve:J1" for g in c.gaps)


def test_a_source_net_with_no_driver_is_named():
    c = _one(source_nets=["CLK", "SPI_SCK"])
    assert any(g.key == "driver-net:SPI_SCK" for g in c.gaps)


def test_no_driver_at_all_says_the_result_is_relative():
    c = _one(drivers=[])
    assert any(g.key == "no-driver" for g in c.gaps)
    assert any(g.fixed_on == "drivers" for g in c.gaps)


def test_a_solve_without_a_far_field_box_blocks_the_board_path():
    c = _one(far_field_ports=[])
    assert any(g.key == "far-field:p1" for g in c.gaps)
    assert any(g.fixed_on == "solve" for g in c.gaps)


def test_a_band_that_stops_short_is_reported_with_both_numbers():
    c = _one(covered={"board": (30e6, 500e6)})
    msg = next(g.message for g in c.gaps if g.key == "band:board")
    assert "500 MHz" in msg and "1000 MHz" in msg


def test_a_band_that_starts_late_is_reported_too():
    """The far field used to be asked for 30 MHz regardless; now it covers what was solved,
    and a solve starting at 100 MHz leaves 30-100 MHz uncovered."""
    c = _one(covered={"board": (100e6, 1e9)})
    assert any(g.key == "band:board" for g in c.gaps)


def test_an_unknown_highest_frequency_is_a_gap():
    c = _one(required_band=None)
    assert any(g.key == "highest-frequency" for g in c.gaps)


def test_more_than_one_excited_port_is_a_gap():
    c = _one(excited_ports=["p1", "p2"], far_field_ports=["p1", "p2"])
    assert any(g.key == "sources" for g in c.gaps)


def test_no_solve_is_a_gap():
    c = _one(has_solve=False, excited_ports=[], far_field_ports=[], covered={})
    assert any(g.key == "solve" for g in c.gaps)


def test_an_undriven_harmonic_is_a_gap_not_a_zero():
    c = _one(undriven_harmonics={300e6: "no rise time declared"})
    msg = next(g.message for g in c.gaps if g.key.startswith("harmonic:"))
    assert "no rise time declared" in msg
    assert "not a zero" in msg


def test_an_undriven_frequency_outside_the_scan_is_not_a_gap():
    assert check(**{**FULL, "undriven_harmonics": {2e9: "above the capture"}}).complete


def test_artifact_problems_are_gaps():
    c = _one(problems=[("far-field-format", "Run the solve again.")])
    assert any(g.key == "far-field-format" and g.fixed_on == "solve" for g in c.gaps)


def test_a_metal_enclosure_keeps_radiated_incomplete_with_that_reason():
    c = _one(enclosure="metal")
    g = next(g for g in c.gaps if g.key == "metal-enclosure")
    assert "not modelled" in g.message
    assert g.fixed_on == "product"


def test_an_unrecognised_power_entry_only_matters_when_there_is_one():
    assert check(**{**FULL, "power": "dc", "power_entry_found": False}).complete
    c = _one(power="mains", power_entry_found=False)
    assert any(g.key == "power-entry" for g in c.gaps)


def test_every_gap_names_a_tab_that_fixes_it():
    c = check(**{**FULL, "drivers": [], "connectors": ["J1", "J9"], "far_field_ports": [],
                 "enclosure": "metal", "power": ""})
    assert len(c.gaps) >= 5
    assert all(g.fixed_on in {"drivers", "cables", "solve", "product"} for g in c.gaps)
    assert all(g.message.endswith(".") for g in c.gaps)
