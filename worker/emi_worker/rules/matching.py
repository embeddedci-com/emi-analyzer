"""Matched groups, and the skew between their members.

What has to match what is the part most easily got wrong, because "match everything to the
clock" is not how DDR works:

    DQ and DM within a byte lane   ->  their own DQS pair, not CK
    DQS+/-, CK+/-                  ->  their own complement (tightest of all)
    address, command, control      ->  CK
    byte lane vs byte lane         ->  nothing; write levelling absorbs it

Getting that backwards is costly in both directions. Matching lanes to each other produces a
wall of findings on a correct board; matching DQ to CK instead of to DQS misses the error
that actually stops the board working.

Everything here is measured in **picoseconds**. See docs/length-matching-and-impedance.md:
an inner-layer millimetre and an outer-layer millimetre differ by about 25% in delay, so a
check that compared millimetres would pass a board whose lanes are skewed by ten times
their budget.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..kicad.netclass import DiffPair, NetClasses, split_pair_name

#: Name patterns, the last resort when neither netclass nor component pins identify a group.
#: Deliberately anchored: "ADDR_SEL" is not an address bus and "CLKOUT" is not a DDR clock.
DQ_RE = re.compile(r"(?:^|[_/])D(?:Q)?(\d{1,2})$", re.I)
DQS_RE = re.compile(r"(?:^|[_/])([LU]?DQS)(\d*)(?:[_-]?[PN])?$", re.I)
DM_RE = re.compile(r"(?:^|[_/])(?:DM|DQM|LDM|UDM)(\d*)$", re.I)
ADDR_RE = re.compile(r"(?:^|[_/])(?:A|BA|MA)(\d{1,2})$", re.I)
CMD_RE = re.compile(r"(?:^|[_/])(?:RAS|CAS|WE|CKE|CS|ODT|ACT|PAR)(?:_?N|#)?(\d*)$", re.I)
CK_RE = re.compile(r"(?:^|[_/])(?:CK|CLK|MCK)(\d*)(?:[_-]?[PN])?$", re.I)


@dataclass
class MatchGroup:
    """A set of nets that must arrive together, and what they arrive against."""

    name: str
    kind: str                      # byte-lane | address-command | pair
    members: list[str] = field(default_factory=list)
    reference: str = ""            # the net the members are matched to
    tolerance_key: str = "byte_lane_ps"
    #: How the group was identified: netclass, pins or name. Stated in every finding,
    #: because a user's trust in the result depends on it.
    source: str = "name"
    note: str = ""


def _strip_pair(net: str) -> str:
    split = split_pair_name(net)
    return split[0] if split else net


def find_groups(
    nets: list[str],
    pads_by_net: dict[str, list[str]],
    classes: NetClasses | None,
    pairs: list[DiffPair],
    *,
    min_group_size: int = 3,
) -> list[MatchGroup]:
    """Identify matched groups, most trustworthy method first."""
    groups: list[MatchGroup] = []

    # 1. Differential pairs. Each is a group of two matched to each other, and this is the
    #    one case that needs no guessing at all -- the two halves are named as a pair.
    for p in pairs:
        groups.append(MatchGroup(
            name=p.base,
            kind="pair",
            members=[p.positive, p.negative],
            reference=p.positive,
            tolerance_key="intra_pair_ps",
            source="netclass" if p.source == "netclass" else "name",
        ))

    # 2. Netclasses. A class called DDR_DQ0 is the designer stating the group.
    by_class: dict[str, list[str]] = defaultdict(list)
    if classes and classes.available:
        for net in nets:
            cls = classes.of(net)
            if cls and re.search(r"ddr|dq|byte|lane", cls, re.I):
                by_class[cls].append(net)

    claimed: set[str] = set()
    for cls, members in sorted(by_class.items()):
        if len(members) < min_group_size:
            continue
        ref = _reference_for(members)
        groups.append(MatchGroup(
            name=cls, kind="byte-lane", members=sorted(members), reference=ref,
            tolerance_key="byte_lane_ps", source="netclass",
        ))
        claimed.update(members)

    # 3. Component pins. The DQ nets landing on one memory device are that device's lanes,
    #    which is robust against unusual net names and uses data already parsed. Bit
    #    swapping within a lane and byte-lane swapping are both legal, so lane membership
    #    must never come from assuming DQ0-7 is lane 0.
    dq_nets = [n for n in nets if n not in claimed and DQ_RE.search(n)]
    dqs_nets = [n for n in nets if DQS_RE.search(n)]
    dm_nets = [n for n in nets if DM_RE.search(n)]
    ck_nets = [n for n in nets if CK_RE.search(n)]
    addr_cmd = [n for n in nets if n not in claimed and (ADDR_RE.search(n) or CMD_RE.search(n))]

    if dq_nets and dqs_nets:
        lanes = _lanes_from_pins(dq_nets, dm_nets, dqs_nets, pads_by_net)
        for lane_name, members, ref, source in lanes:
            if len(members) < min_group_size:
                continue
            groups.append(MatchGroup(
                name=lane_name, kind="byte-lane", members=sorted(members),
                reference=ref, tolerance_key="byte_lane_ps", source=source,
            ))
            claimed.update(members)

    # 4. Address and command, matched to the clock.
    if addr_cmd and ck_nets:
        # Prefer the positive half of the clock pair; either would do for skew, but a
        # finding that quotes CK_N reads as though something is inverted.
        ref = next((n for n in sorted(ck_nets) if split_pair_name(n) and split_pair_name(n)[1]),
                   sorted(ck_nets)[0])
        groups.append(MatchGroup(
            name="address/command", kind="address-command",
            members=sorted(addr_cmd), reference=ref,
            tolerance_key="address_command_ps", source="name",
            note="matched to the clock, which is what the DRAM captures them on",
        ))

    return groups


def _positive_first(nets: list[str]) -> list[str]:
    """Sorted, with the positive half of any pair ahead of its complement.

    Plain sorting puts "DQS0_N" before "DQS0_P", so a group picked its reference from the
    negative half and every finding and export row quoted DQS0_N. Skew against either half
    is similar on a matched pair, but a reference that reads as inverted sends people
    looking for a polarity problem that is not there -- and on a pair that is itself skewed,
    it moves every member of the lane.
    """
    def key(n: str) -> tuple[int, str]:
        split = split_pair_name(n)
        return (0 if (split is None or split[1]) else 1, n)
    return sorted(nets, key=key)


def _reference_for(members: list[str]) -> str:
    """The strobe if the group has one, else the clock, else the first member."""
    for m in _positive_first(members):
        if DQS_RE.search(m):
            return m
    for m in _positive_first(members):
        if CK_RE.search(m):
            return m
    return sorted(members)[0] if members else ""


def _lanes_from_pins(
    dq_nets: list[str],
    dm_nets: list[str],
    dqs_nets: list[str],
    pads_by_net: dict[str, list[str]],
) -> list[tuple[str, list[str], str, str]]:
    """Group DQ/DM nets into byte lanes, by the memory device they land on.

    Falls back to the DQ index when pad data cannot separate them -- a board with one DRAM
    has every DQ on the same component, so the device alone does not identify a lane, and
    then the index is all there is.
    """
    device_of: dict[str, str] = {}
    for net in dq_nets + dm_nets + dqs_nets:
        refs = {p.split(".")[0] for p in pads_by_net.get(net, []) if "." in p}
        # The controller is on every net of the interface; the memory is not. So the useful
        # discriminator is the reference that appears on *fewest* of them.
        if refs:
            device_of[net] = sorted(refs)[-1]

    devices = {device_of.get(n, "") for n in dq_nets}
    use_pins = len(devices - {""}) > 1

    lanes: dict[str, list[str]] = defaultdict(list)
    for net in dq_nets + dm_nets:
        if use_pins:
            key = device_of.get(net, "?")
        else:
            m = DQ_RE.search(net) or DM_RE.search(net)
            index = int(m.group(1)) if m and m.group(1) else 0
            key = f"byte {index // 8}"
        lanes[key].append(net)

    out: list[tuple[str, list[str], str, str]] = []
    for key, members in sorted(lanes.items()):
        strobe = _strobe_for(key, members, dqs_nets, device_of, use_pins)
        if not strobe:
            continue
        out.append((f"byte lane {key}", members + [strobe], strobe,
                    "pins" if use_pins else "name"))
    return out


def _strobe_for(
    key: str,
    members: list[str],
    dqs_nets: list[str],
    device_of: dict[str, str],
    use_pins: bool,
) -> str:
    if use_pins:
        for s in _positive_first(dqs_nets):
            if device_of.get(s) == key:
                return s
        return ""
    # Match the strobe index to the lane index: DQS0 goes with byte 0.
    want = key.rsplit(" ", 1)[-1]
    for s in _positive_first(dqs_nets):
        m = DQS_RE.search(s)
        if m and (m.group(2) or "0") == want:
            return s
    return sorted(dqs_nets)[0] if len(dqs_nets) == 1 else ""
