"""The net list as rows: lengths, delays, and skew against each net's reference.

This exists for people who want to look at the numbers themselves -- filter the DDR nets in
a spreadsheet, sort by skew, eyeball the byte lanes -- rather than only read findings. It is
structured data (nets.json); the browser turns it into a CSV formatted for the user's own
spreadsheet, which is where locale concerns belong.

Two length columns, deliberately, because they answer different questions and the older one
was easy to misread:

  copper_mm   all the copper on the net. What the net list used to show.
  path_mm     the longest pad-to-pad route. What a signal actually travels.

On a point-to-point net they agree. On a branched net they do not, and length matching is
about the second.
"""

from __future__ import annotations

from collections import defaultdict

from ..kicad.board import BoardModel
from .matching import CK_RE, DQS_RE, MatchGroup
from .model import classify_net

REPORT_VERSION = 1


def build(
    model: BoardModel,
    topology: dict,
    electrics,
    groups: list[MatchGroup],
    pairs: list,
    netclasses=None,
    settings=None,
) -> dict:
    widths: dict[str, list[float]] = defaultdict(list)
    for t in model.tracks:
        if t.net and t.width_mm > 0:
            widths[t.net].append(t.width_mm)

    partner: dict[str, str] = {}
    for p in pairs:
        partner[p.positive] = p.negative
        partner[p.negative] = p.positive

    def delay(net: str) -> float | None:
        topo = topology.get(net)
        path = topo.longest_path() if topo else None
        return path.delay_ps(electrics) if (path and electrics) else None

    def dominant_ps_per_mm(net: str) -> float:
        """The layer most of a net's route is on, for converting skew back to millimetres.

        A skew in picoseconds is the truth; the millimetre figure is a convenience for
        someone about to add a meander, and it is only meaningful on the layer they would
        add it on.
        """
        topo = topology.get(net)
        path = topo.longest_path() if topo else None
        if not (path and path.runs and electrics):
            return 0.0
        run = max(path.runs, key=lambda r: r.length_mm)
        return electrics.ps_per_mm(run.layer)

    # Which group speaks for each net. A net can sit in two -- DQS0_P is half of a pair and
    # the reference of a byte lane -- and the row should show the one where it is being
    # compared, not the one where it is the yardstick.
    membership: dict[str, MatchGroup] = {}
    rank = {"byte-lane": 0, "address-command": 0, "pair": 1}
    for g in sorted(groups, key=lambda g: rank.get(g.kind, 2)):
        for m in g.members:
            if m == g.reference:
                continue
            membership.setdefault(m, g)
    for g in groups:
        membership.setdefault(g.reference, g)

    # The DDR clock, for a column comparing every DDR net against it. Data is matched to
    # its strobe rather than to the clock, and the skew column does that -- but the spread
    # between strobe and clock is what write levelling has to absorb, and people want to see
    # it. Only offered when an address/command group identified a clock, and the column
    # header carries the clock's name so nobody has to guess what it was measured against.
    clock = next((g.reference for g in groups if g.kind == "address-command"), "")
    clock_delay = delay(clock) if clock else None
    ddr_nets: set[str] = set()
    for g in groups:
        if g.kind in ("byte-lane", "address-command"):
            ddr_nets.update(g.members)
            ddr_nets.add(g.reference)
        elif g.kind == "pair" and (DQS_RE.search(g.name + "_P") or CK_RE.search(g.name + "_P")):
            ddr_nets.update(g.members)

    rows = []
    for net in sorted(n for n in model.nets if n):
        topo = topology.get(net)
        path = topo.longest_path() if topo else None
        d = delay(net)
        row: dict = {
            "net": net,
            "netclass": netclasses.of(net) if netclasses else "",
            "kind": classify_net(net),
            "topology": topo.kind if topo else "unrouted",
            "pads": len(topo.pads) if topo else 0,
            "unreachable_pads": len(topo.unreachable) if topo else 0,
            "copper_mm": round(topo.total_copper_mm, 3) if topo else 0.0,
            "path_mm": round(path.length_mm, 3) if path else None,
            "path_from": path.from_pad if path else "",
            "path_to": path.to_pad if path else "",
            "path_by_layer": " | ".join(f"{r.layer}:{r.length_mm:.2f}" for r in path.runs) if path else "",
            "path_vias": path.vias if path else None,
            "vias_total": topo.vias if topo else 0,
            "delay_ps": round(d, 2) if d is not None else None,
            "width_min_mm": round(min(widths[net]), 3) if widths[net] else None,
            "width_max_mm": round(max(widths[net]), 3) if widths[net] else None,
            "diff_pair_partner": partner.get(net, ""),
            "match_group": "",
            "match_kind": "",
            "match_reference": "",
            "reference_delay_ps": None,
            "skew_ps": None,
            "skew_mm": None,
            "tolerance_ps": None,
            "within_tolerance": "",
            "clock_net": "",
            "vs_clock_ps": None,
            "vs_clock_mm": None,
        }

        g = membership.get(net)
        if g:
            ref_delay = delay(g.reference)
            row["match_group"] = g.name
            row["match_kind"] = g.kind
            row["match_reference"] = g.reference
            if ref_delay is not None:
                row["reference_delay_ps"] = round(ref_delay, 2)
            if d is not None and ref_delay is not None:
                skew = d - ref_delay
                row["skew_ps"] = round(skew, 2)
                per = dominant_ps_per_mm(net)
                row["skew_mm"] = round(skew / per, 3) if per else None
                tol = None
                if settings is not None:
                    tol = settings.param("ddr-skew", g.tolerance_key, net=g.reference)
                if tol:
                    row["tolerance_ps"] = float(tol)
                    row["within_tolerance"] = "yes" if abs(skew) <= float(tol) else "no"

        if net in ddr_nets and clock and d is not None and clock_delay is not None:
            row["clock_net"] = clock
            vs = d - clock_delay
            row["vs_clock_ps"] = round(vs, 2)
            per = dominant_ps_per_mm(net)
            row["vs_clock_mm"] = round(vs / per, 3) if per else None

        rows.append(row)

    return {
        "format_version": REPORT_VERSION,
        "clock_net": clock,
        "epsilon_assumed": bool(electrics.epsilon_assumed) if electrics else True,
        "rows": rows,
    }
