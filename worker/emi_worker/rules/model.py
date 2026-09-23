"""Finding types and the shared rule context."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from typing import Iterable

from ..kicad.board import BoardModel

FORMAT_VERSION = 1

#: Ground and power net names are recognised word by word, never by substring: "+10V" holds
#: "0v" and "/SIGNDIR" holds "gnd", and neither is a ground. A name splits into words at
#: anything but a letter, a digit or a decimal point, so "+3.3V" is the one word "3.3v" and
#: "VBUS_20V" is "vbus" and "20v".
_WORD_SPLIT = re.compile(r"[^a-z0-9.]+")

#: A word that names a ground: GND and its A/D/P/S variants with any suffix (GNDA, GND1,
#: PGND2), VSS and its variants, and the plain words.
_GROUND_WORD = re.compile(r"^(?:[adps]?gnd[a-z0-9]*|vss[a-z0-9]*|ground|earth|0v)$")

#: A word that names a supply: the usual rail prefixes with any suffix (VDDIO, VCCA, VBAT1)...
_RAIL_WORD = re.compile(r"^(?:[ad]?v(?:cc|dd|bus|in|out|bat|sys|ee)[a-z0-9]*)$")
#: ...or a voltage: 5v, 12v, 3.3v, 3v3, 1v8, 20v, and the like.
_VOLTAGE_WORD = re.compile(r"^\d+(?:\.\d+)?v\d*$")
#: KiCad's power symbols put a sign in front of the voltage: +3.3V, +24V, -12V, +3V3.
_SIGNED_VOLTAGE = re.compile(r"^[+-]\d")

#: Words that make a rail-named net a signal about that rail: 3V3_EN, VBUS_DET, VIN_SENSE and
#: 5V_PG carry logic or a divided-down voltage to a pin, not the supply current.
_SIGNAL_WORDS = frozenset({
    "en", "enable", "pg", "pgood", "good", "ok", "det", "detect", "sense", "sns", "fb",
    "flt", "fault", "ctrl", "ctl", "sel", "mon", "alert", "int", "irq", "adc", "div",
})


def _words(name: str) -> tuple[list[str], str]:
    # Hierarchical names ("/Power/+3V3") are judged by their last part.
    leaf = name.lower().rstrip("/").rsplit("/", 1)[-1]
    return [w.strip(".") for w in _WORD_SPLIT.split(leaf) if w.strip(".")], leaf


def classify_net(name: str) -> str:
    """"ground", "power" or "signal"."""
    words, leaf = _words(name)
    if leaf.startswith("net-("):  # KiCad's name for an unnamed net
        return "signal"
    if any(_GROUND_WORD.match(w) for w in words):
        return "ground"
    rail = _SIGNED_VOLTAGE.match(leaf) is not None or any(
        _RAIL_WORD.match(w) or _VOLTAGE_WORD.match(w) for w in words)
    if rail and not any(w in _SIGNAL_WORDS for w in words):
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
