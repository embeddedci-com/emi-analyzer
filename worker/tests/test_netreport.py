"""The net report behind the CSV export.

What a user filters and sorts in a spreadsheet has to agree with what the findings say, and
the skew columns have to be measured against the right thing: data against its strobe,
address and command against the clock -- with a separate column against the clock for every
DDR net, because that spread is what write levelling has to absorb.
"""

from __future__ import annotations

import pytest

from emi_worker import topology
from emi_worker.kicad.board import BoardModel, CopperLayer, Pad, Track
from emi_worker.kicad.netclass import find_pairs
from emi_worker.rules import matching, netreport, settings
from emi_worker.stackup import BoardElectrics, LayerElectrics

ELEC = BoardElectrics(layers={"F.Cu": LayerElectrics("F.Cu", 0, "microstrip", "In1.Cu", 0.2, 4.3)})
PS = ELEC.ps_per_mm("F.Cu")

#: name -> routed length in mm. One memory device, so lanes are grouped by index.
LENGTHS = {
    "DDR_DQ0": 40.0, "DDR_DQ1": 41.0, "DDR_DQ2": 43.0,     # 43 mm is 3 mm long: ~17 ps
    "DDR_DQS0_P": 40.0, "DDR_DQS0_N": 40.0,
    "DDR_A0": 50.0, "DDR_A1": 50.5, "DDR_A2": 55.0,         # 55 mm is 5 mm long vs CK
    "DDR_CK_P": 50.0, "DDR_CK_N": 50.0,
    "GPIO_LED": 12.0,
}


def _board() -> BoardModel:
    m = BoardModel()
    m.copper_layers = [CopperLayer(ordinal=0, name="F.Cu", kind="signal")]
    m.nets = list(LENGTHS)
    for i, (net, length) in enumerate(LENGTHS.items()):
        y = 5.0 * i
        m.tracks.append(Track(layer="F.Cu", net=net, width_mm=0.2, pts=[(0.0, y), (length, y)]))
        m.pads.append(Pad(ref="U1", number=str(i), net=net, layers=["F.Cu"], x=0.0, y=y, ring=[]))
        m.pads.append(Pad(ref="U5", number=str(i), net=net, layers=["F.Cu"], x=length, y=y, ring=[]))
    return m


@pytest.fixture(scope="module")
def report():
    m = _board()
    topo = topology.build(m)
    pairs = find_pairs(m.nets, None)
    groups = matching.find_groups(m.nets, {k: v.pads for k, v in topo.items()}, None, pairs)
    doc = netreport.build(m, topo, ELEC, groups, pairs, None, settings.defaults())
    return {r["net"]: r for r in doc["rows"]}, doc


def test_every_net_gets_a_row(report):
    rows, _ = report
    assert set(rows) == set(LENGTHS)


def test_data_skew_is_measured_against_its_strobe(report):
    rows, _ = report
    dq2 = rows["DDR_DQ2"]
    assert dq2["match_reference"] == "DDR_DQS0_P"
    assert dq2["skew_ps"] == pytest.approx(3.0 * PS, abs=0.05)
    assert dq2["skew_mm"] == pytest.approx(3.0, abs=0.01)
    # ~17 ps against a 10 ps byte-lane budget.
    assert dq2["tolerance_ps"] == 10.0
    assert dq2["within_tolerance"] == "no"
    assert rows["DDR_DQ1"]["within_tolerance"] == "yes"


def test_address_skew_is_measured_against_the_clock(report):
    rows, _ = report
    a2 = rows["DDR_A2"]
    assert a2["match_kind"] == "address-command"
    assert a2["match_reference"] == "DDR_CK_P"
    assert a2["skew_mm"] == pytest.approx(5.0, abs=0.01)
    assert a2["tolerance_ps"] == 25.0
    assert a2["within_tolerance"] == "no"


def test_every_ddr_net_is_also_compared_to_the_clock(report):
    """Asked for explicitly: the spread between data and clock, not only data and strobe."""
    rows, doc = report
    assert doc["clock_net"] == "DDR_CK_P"
    dq0 = rows["DDR_DQ0"]
    assert dq0["clock_net"] == "DDR_CK_P"
    # DQ0 is 40 mm, CK is 50 mm: 10 mm early.
    assert dq0["vs_clock_mm"] == pytest.approx(-10.0, abs=0.01)
    assert dq0["vs_clock_ps"] == pytest.approx(-10.0 * PS, abs=0.05)


def test_a_net_that_is_not_ddr_has_no_clock_or_group_columns(report):
    rows, _ = report
    led = rows["GPIO_LED"]
    assert led["match_group"] == "" and led["skew_ps"] is None
    assert led["clock_net"] == "" and led["vs_clock_ps"] is None
    # ...but it still has its lengths, which is the rest of the point of the export.
    assert led["copper_mm"] == pytest.approx(12.0)
    assert led["path_mm"] == pytest.approx(12.0)


def test_a_strobe_is_shown_where_it_is_compared_not_where_it_is_the_yardstick(report):
    """DQS0_N is half of a pair; DQS0_P is both the pair's reference and the lane's."""
    rows, _ = report
    assert rows["DDR_DQS0_N"]["match_kind"] == "pair"
    assert rows["DDR_DQS0_N"]["match_reference"] == "DDR_DQS0_P"
    assert rows["DDR_DQS0_P"]["skew_ps"] == pytest.approx(0.0)
