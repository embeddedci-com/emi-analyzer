"""Finding types and the shared rule context."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Iterable

from ..kicad.board import BoardModel

FORMAT_VERSION = 1

#: Ground and power net names, matched case-insensitively. Several checks need to know
#: which nets are references rather than signals -- a "long trace" finding on a ground pour
#: is noise, and a return-via check has to know what a return via looks like.
GROUND_HINTS = ("gnd", "ground", "agnd", "dgnd", "pgnd", "vss", "earth", "0v")
POWER_HINTS = ("vcc", "vdd", "vbus", "vin", "vout", "+3v", "+5v", "+1v", "+12v", "+2v",
               "3v3", "5v", "vbat", "vsys", "avdd", "vddio")


def classify_net(name: str) -> str:
    """"ground", "power" or "signal"."""
    low = name.lower().lstrip("/")
    if any(h in low for h in GROUND_HINTS):
        return "ground"
    if any(low.startswith(h) or h in low for h in POWER_HINTS):
        return "power"
    return "signal"


@dataclass
class Finding:
    rule: str
    severity: str  # critical | warning | info
    title: str
    detail: str
    net: str = ""
    layer: str = ""
    x: float | None = None
    y: float | None = None
    bbox: tuple[float, float, float, float] | None = None

    def as_dict(self, index: int) -> dict:
        d = asdict(self)
        d["id"] = f"{self.rule}-{index}"
        if self.bbox is not None:
            d["bbox"] = [round(v, 4) for v in self.bbox]
        else:
            d.pop("bbox", None)
        # Drop absent coordinates rather than emitting nulls. A finding with no single
        # place on the board -- a summary count, say -- should simply not carry x and y,
        # so a consumer checking "is this key present" gets the right answer.
        for k in ("x", "y"):
            if d[k] is None:
                d.pop(k)
            else:
                d[k] = round(d[k], 4)
        return d


@dataclass
class RulesResult:
    findings: list[Finding] = field(default_factory=list)
    max_frequency_hz: float = 1e9
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        sev = {"critical": 0, "warning": 0, "info": 0}
        for f in self.findings:
            sev[f.severity] = sev.get(f.severity, 0) + 1
        return {
            "format_version": FORMAT_VERSION,
            "findings": [f.as_dict(i) for i, f in enumerate(self.findings)],
            "summary": {
                **sev,
                "assumed_max_frequency_hz": self.max_frequency_hz,
            },
            "notes": self.notes,
        }


@dataclass
class RuleContext:
    """Everything the checks share, prepared once.

    Coordinates here are in **board space** (mm, Y up, origin at the board's bottom-left),
    matching board.json, so that a finding's x/y can be handed straight to the viewer.
    """

    model: BoardModel
    transform: object  # normalize._Transform
    max_frequency_hz: float
    #: Everything that is configurable. Defaults when the caller passes nothing, so a check
    #: can always read a threshold without asking whether settings exist.
    settings: object = None  # settings.Settings
    #: Per-layer delay and reference-plane geometry, from the stackup.
    electrics: object = None  # stackup.BoardElectrics
    #: Net connectivity: pin-to-pin paths, not total copper.
    topology: dict = field(default_factory=dict)  # str -> topology.NetTopology
    netclasses: object = None  # netclass.NetClasses
    pairs: list = field(default_factory=list)  # netclass.DiffPair
    groups: list = field(default_factory=list)  # matching.MatchGroup
    #: Propagation velocity factor in FR-4, used for the wavelength checks. c/sqrt(er) with
    #: er ~4.4 gives roughly 0.48c; microstrip sees a lower effective er, so 0.5 is a fair
    #: middle. Being slightly optimistic here means slightly fewer findings, not more.
    velocity_factor: float = 0.5
    notes: list[str] = field(default_factory=list)

    def setting(self, rule_id: str, key: str, net: str = "") -> object:
        """A rule's threshold, with any net-group override applied."""
        from .settings import RULE_CATALOGUE, defaults
        if self.settings is None:
            self.settings = defaults()
        netclass = self.netclasses.of(net) if (self.netclasses and net) else ""
        return self.settings.param(rule_id, key, net=net, netclass=netclass)  # type: ignore[union-attr]

    def enabled(self, rule_id: str) -> bool:
        from .settings import defaults
        if self.settings is None:
            self.settings = defaults()
        return self.settings.enabled(rule_id)  # type: ignore[union-attr]

    def ps_per_mm(self, layer: str) -> float:
        return self.electrics.ps_per_mm(layer) if self.electrics else 6.0  # type: ignore[union-attr]

    def pt(self, x: float, y: float) -> tuple[float, float]:
        return self.transform.pt(x, y)  # type: ignore[attr-defined]

    @property
    def wavelength_mm(self) -> float:
        # Velocity comes from the stackup when it is known. The old constant 0.5 was a fair
        # middle for FR-4 microstrip, but a board with a different dielectric -- or a signal
        # on an inner layer, which is ~20% slower -- was measured against the wrong ruler.
        vf = self.electrics.mean_velocity_factor if self.electrics else self.velocity_factor  # type: ignore[union-attr]
        c_mm_s = 299_792_458.0 * 1000.0
        return (c_mm_s * vf) / self.max_frequency_hz

    def copper_layer_index(self, name: str) -> int:
        for i, layer in enumerate(self.model.copper_layers):
            if layer.name == name:
                return i
        return -1


def net_kinds(model: BoardModel) -> dict[str, str]:
    return {name: classify_net(name) for name in model.nets}


def dedupe(findings: Iterable[Finding], per_rule_limit: int = 40) -> list[Finding]:
    """Cap how many findings any single rule can emit.

    A board with a systematic problem produces hundreds of identical findings, and a list
    of four hundred rows tells the user less than a list of forty. The cap is per rule so a
    noisy check cannot crowd out a quiet, serious one.
    """
    seen: dict[str, int] = {}
    out: list[Finding] = []
    truncated: dict[str, int] = {}
    for f in findings:
        n = seen.get(f.rule, 0)
        if n < per_rule_limit:
            out.append(f)
            seen[f.rule] = n + 1
        else:
            truncated[f.rule] = truncated.get(f.rule, 0) + 1

    for rule, extra in truncated.items():
        out.append(Finding(
            rule=rule,
            severity="info",
            title=f"{extra} more {rule.replace('-', ' ')} findings not listed",
            detail=(
                f"Only the first {per_rule_limit} are shown. A count this high usually "
                f"means one systematic cause rather than {extra + per_rule_limit} "
                f"separate problems."
            ),
        ))
    return out


def distance_point_segment(px: float, py: float,
                           x0: float, y0: float, x1: float, y1: float) -> float:
    dx, dy = x1 - x0, y1 - y0
    denom = dx * dx + dy * dy
    if denom < 1e-15:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / denom))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))
