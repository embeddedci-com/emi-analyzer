"""What to change, from the findings already on the board (§16.5).

**Recommendations are never invented by the prediction.** Everything offered here is a rule
finding that the rules engine already produced, selected because it lies on the path doing the
radiating and ranked by how much it is likely to matter. That constraint is the whole design:
a prediction that generated its own advice would be writing EMC guidance from a number, and the
number is an estimate with several decibels of stated uncertainty. A finding, by contrast, is
something specific about this board that someone can look at.

When nothing on the dominant path has a finding, the view says so in those words and gives
general guidance for that path type — labelled as general, so it is not mistaken for something
the tool noticed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Which rules count as "on" each kind of path (§16.5's table).
#:
#: A cable radiates because common-mode current reaches it, so the findings that matter are the
#: ones about how that current gets to the connector and whether anything stops it. A board
#: region radiates from the driven net itself, so the findings are about that net's return path
#: and the decoupling of whatever drives it.
PATH_RULES: dict[str, tuple[str, ...]] = {
    "cable": ("esd-protection", "connector-shield", "input-filter", "stitching",
              "plane-gap", "return-path", "cable-resonance"),
    "board": ("plane-gap", "stitching", "edge-proximity", "radiator", "decoupling",
              "return-path"),
    "conducted": ("input-filter", "decoupling", "switch-node"),
}

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

#: How far from the path a finding can be and still count, in mm.
#:
#: Not unlimited: on a 100 mm board every finding is within 100 mm of everything, and a list
#: that long is not a recommendation, it is the findings table again. Set to cover the
#: connector plus its immediate approach.
NEAR_MM = 25.0

GENERAL: dict[str, tuple[str, ...]] = {
    "cable": (
        "Bond the connector shell to the board's ground copper with a short, wide connection "
        "on every side it can reach — common-mode current leaves through the shell, and its "
        "return path is what decides how much.",
        "A common-mode choke on the pairs leaving this connector reduces the current directly, "
        "and cannot make it worse.",
        "Keep the copper under the connector continuous. A gap under the exit forces return "
        "current around it, and the loop that makes is the radiator.",
    ),
    "board": (
        "Keep the driven net over an unbroken reference plane for its whole length; the return "
        "current follows the trace, and where it cannot, it makes a loop.",
        "Move the net away from the board edge. A trace near an edge radiates from the edge "
        "rather than into the plane.",
        "Decouple the driving IC close to its supply pin, with the loop from pin to capacitor "
        "to plane as short as the footprint allows.",
    ),
    "conducted": (
        "A filter at the power entry is what the conducted scan measures through; without one, "
        "the converter's switching current reaches the supply line directly.",
        "Reduce the switch node's copper area to what the thermal design needs — it is the "
        "capacitance to everything else that carries common-mode current.",
    ),
}


@dataclass
class Recommendation:
    #: The finding's id, so the UI can highlight it on the board.
    finding_id: str
    rule: str
    severity: str
    title: str
    detail: str
    net: str = ""
    #: Distance from the path this was selected for, mm. None when the finding has no place.
    distance_mm: float | None = None


@dataclass
class Recommendations:
    path_kind: str
    path_label: str
    items: list[Recommendation] = field(default_factory=list)
    #: Filled only when nothing specific was found, and then flagged as general.
    general: list[str] = field(default_factory=list)

    @property
    def general_only(self) -> bool:
        return not self.items and bool(self.general)


def _distance(finding: dict, x: float | None, y: float | None) -> float | None:
    if x is None or y is None:
        return None
    fx, fy = finding.get("x"), finding.get("y")
    if fx is None or fy is None:
        return None
    return math.hypot(float(fx) - x, float(fy) - y)


def gather(
    findings: list[dict],
    path_kind: str,
    path_label: str,
    *,
    nets: tuple[str, ...] = (),
    near: tuple[float, float] | None = None,
    limit: int = 6,
) -> Recommendations:
    """The findings that lie on this path, ranked by severity and then distance.

    ``nets`` are the nets the path involves — the driven net, and the nets leaving on the
    connector. ``near`` is a point on the board the path leaves from, so that a finding with a
    location can be judged by how close it is rather than only by what rule produced it.

    A finding qualifies by **rule and (net or proximity)**. Rule alone would pull in every
    plane gap on the board; net alone would miss a stitching finding that has no net at all but
    sits two millimetres from the connector.
    """
    rules = PATH_RULES.get(path_kind, ())
    wanted_nets = {n for n in nets if n}

    picked: list[tuple[int, float, Recommendation]] = []
    for f in findings:
        if f.get("rule") not in rules:
            continue
        net = f.get("net") or ""
        distance = _distance(f, *near) if near else None
        on_net = bool(net) and net in wanted_nets
        close = distance is not None and distance <= NEAR_MM
        if not (on_net or close):
            continue
        rank = SEVERITY_RANK.get(f.get("severity", "info"), 3)
        picked.append((
            rank,
            distance if distance is not None else float("inf"),
            Recommendation(
                finding_id=f.get("id", f"{f.get('rule')}-?"),
                rule=str(f.get("rule", "")),
                severity=str(f.get("severity", "info")),
                title=str(f.get("title", "")),
                detail=str(f.get("detail", "")),
                net=net,
                distance_mm=round(distance, 2) if distance is not None else None,
            ),
        ))

    picked.sort(key=lambda t: (t[0], t[1]))
    items = [r for _rank, _d, r in picked[:limit]]

    return Recommendations(
        path_kind=path_kind, path_label=path_label, items=items,
        general=list(GENERAL.get(path_kind, ())) if not items else [],
    )
