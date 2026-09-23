"""Rule settings: what runs, and with which thresholds.

Three things pushed this into existence rather than leaving thresholds as module constants:

  * A tolerance is a property of a *design*, not of the tool. A 1600 MT/s board and a
    3200 MT/s board do not share a skew budget, and neither belongs in our source.
  * A board has several budgets on it at once, so settings have to be scoped to a group of
    nets and not only to the board.
  * The second run of a real board is a list of findings somebody has already decided about.
    Without a way to say "this one is understood", the tool gets turned off. Suppressions are
    not a nicety; they are what makes it survivable.

Values arrive from several places and the more specific wins: built-in defaults, then the
project's settings, then a file committed beside the board, then this run's parameters.
Every effective value remembers where it came from, because a surprising threshold with no
provenance costs an hour.
"""

from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass, field, replace
from typing import Any

import yaml

#: Bumped when a threshold changes meaning. A silently reinterpreted number is worse than a
#: rejected file, because nobody goes looking for it.
SETTINGS_VERSION = 1

#: Where a value came from, most general first. Later sources win.
SOURCES = ("default", "project", "file", "run")

#: What a rule's severity may be overridden to. Anything else used to be stored as given and
#: sorted after info, so a typo quietly demoted every finding of that rule.
SEVERITIES = ("critical", "warning", "info")


@dataclass(frozen=True)
class Value:
    """A setting and its provenance."""

    value: Any
    source: str = "default"

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.value} ({self.source})"


@dataclass
class RuleSetting:
    enabled: bool = True
    #: Override the severity a rule emits, e.g. to demote a check to advisory on a board
    #: where it is understood.
    severity: str = ""
    params: dict[str, Value] = field(default_factory=dict)
    source: str = "default"
    #: Where ``enabled`` and ``severity`` were last set. ``source`` is the last layer that
    #: mentioned the rule at all, which is not the same thing: a file that only sets a
    #: parameter did not switch the rule on.
    enabled_source: str = "default"
    severity_source: str = "default"

    def get(self, key: str, default: Any = None) -> Any:
        v = self.params.get(key)
        return default if v is None else v.value

    def provenance(self, key: str) -> str:
        v = self.params.get(key)
        return v.source if v else "default"

    def describe(self, key: str, unit: str = "") -> str:
        """"25 ps, from emi.rules.yaml" -- what a finding quotes so the user can find it."""
        v = self.params.get(key)
        if v is None:
            return ""
        where = {"default": "built-in default", "project": "project settings",
                 "file": "emi.rules.yaml", "run": "app settings"}.get(v.source, v.source)
        return f"{v.value}{(' ' + unit) if unit else ''}, from {where}"


@dataclass
class NetGroupSetting:
    """Overrides for a set of nets, matched by glob or by netclass."""

    match: str = "*"
    netclass: str = ""
    params: dict[str, Value] = field(default_factory=dict)

    def matches(self, net: str, netclass: str = "") -> bool:
        if self.netclass:
            return netclass == self.netclass
        return fnmatch.fnmatch(net, self.match)


@dataclass
class Suppression:
    """A finding somebody has already decided about."""

    rule: str = "*"
    net: str = "*"
    reason: str = ""

    def covers(self, rule: str, net: str) -> bool:
        return fnmatch.fnmatch(rule, self.rule) and fnmatch.fnmatch(net or "", self.net)


#: The catalogue. Every rule the analyzer knows, its parameters and their defaults.
#:
#: Keyed by a stable id, deliberately decoupled from the title -- settings written today
#: must keep meaning when a finding is reworded tomorrow. The UI builds its settings page
#: from this rather than hard-coding a copy.
RULE_CATALOGUE: dict[str, dict] = {
    "plane-gap": {
        "title": "Reference plane gaps",
        "category": "Return path",
        "about": "A trace crossing a break in the plane beneath it; the return current has "
                 "to detour, and the loop that creates radiates.",
        "params": {"min_crossing_mm": 0.6},
    },
    "return-via": {
        "title": "Missing return vias",
        "category": "Return path",
        "about": "A signal changing layer with no nearby via joining the two reference "
                 "planes, so the return current has no path across.",
        "params": {"max_distance_mm": 2.0},
    },
    "via-stub": {
        "title": "Via stubs",
        "category": "Signal integrity",
        "about": "The unused length of a through via below the layer a signal leaves on, "
                 "which resonates.",
        "params": {"resonance_margin": 4.0},
    },
    "radiator": {
        "title": "Long nets",
        "category": "Radiation",
        "about": "A net long enough against the wavelength to couple to free space "
                 "efficiently.",
        "params": {"wavelength_fraction": 0.05},
    },
    "edge-proximity": {
        "title": "Copper near the board edge",
        "category": "Radiation",
        "about": "Traces or planes close to the edge, where fields are not contained.",
        "params": {"min_clearance_mm": 1.0},
    },
    "ddr-skew": {
        "title": "Length matching",
        "category": "Signal integrity",
        "about": "Members of a matched group whose delay differs from their reference by "
                 "more than the budget. Measured in time, not millimetres: an inner-layer "
                 "millimetre and an outer-layer one are about 25% apart.",
        "params": {
            # Tolerances in picoseconds. Millimetres are shown alongside for convenience,
            # but the comparison is in time -- see docs/length-matching-and-impedance.md.
            "intra_pair_ps": 2.0,
            "byte_lane_ps": 10.0,
            "address_command_ps": 25.0,
            # Lanes are independent; write levelling absorbs the difference between them.
            # Off by default because on a correct board it is a wall of noise.
            "lane_to_lane_ps": 0.0,
            "min_group_size": 3,
        },
    },
    "impedance": {
        "title": "Impedance",
        "category": "Signal integrity",
        "about": "Computed characteristic impedance against a target, and discontinuities "
                 "along a net where width or reference plane changes.",
        "params": {
            "single_ended_ohm": 0.0,     # 0 = no target; only discontinuities are checked
            "differential_ohm": 0.0,
            "tolerance_pct": 10.0,
            # A closed-form model is good to roughly this much. A finding is only raised
            # when the trace is outside tolerance *including* this, so the tool does not
            # claim precision it does not have.
            "model_uncertainty_pct": 8.0,
            "discontinuity_pct": 20.0,
        },
    },
    "decoupling": {
        "title": "Decoupling capacitors",
        "category": "Power integrity",
        "about": "IC supply pins far from a capacitor to ground, and decoupling capacitors whose "
                 "ground pad has no via nearby. The loop through them sets the frequency above "
                 "which they stop decoupling.",
        "params": {"max_distance_mm": 3.0, "critical_distance_mm": 10.0, "max_ground_via_mm": 1.0},
    },
    "stitching": {
        "title": "Plane stitching",
        "category": "Return path",
        "about": "Areas where two ground planes overlap with no via joining them within λ/20; "
                 "between stitching points the planes form a cavity that resonates.",
        # 0 = λ/20 at the board's maximum frequency, from the stackup-derived velocity.
        "params": {"max_spacing_mm": 0.0},
    },
    "edge-stitching": {
        "title": "Edge stitching",
        "category": "Return path",
        "about": "Stretches of board edge where a ground plane reaches the outline with no via "
                 "fence; an unstitched plane edge radiates like a slot antenna.",
        "params": {"max_spacing_mm": 0.0, "edge_band_mm": 2.0},
    },
    "stackup": {
        "title": "Stackup",
        "category": "Return path",
        "about": "Signal layers with no adjacent reference plane, and adjacent signal layers "
                 "with no plane between them to stop broadside coupling.",
        "params": {},
    },
    "copper-island": {
        "title": "Floating copper",
        "category": "Radiation",
        "about": "Pour islands connected to nothing, which pick up and re-radiate with no path "
                 "to ground.",
        "params": {"min_area_mm2": 2.0},
    },
    "crystal": {
        "title": "Crystals and oscillators",
        "category": "Radiation",
        "about": "Signals routed under a crystal, and crystals close to the board edge or to a "
                 "connector, where a cable becomes their antenna.",
        "params": {"min_edge_mm": 5.0, "min_connector_mm": 10.0, "keepout_margin_mm": 0.5},
    },
    # ---- EMC: immunity and conducted emissions (rules/emc.py) ----
    "esd-protection": {
        "title": "ESD protection at connectors",
        "category": "Immunity",
        "about": "Lines leaving the board through an edge connector with no clamp on them, clamps "
                 "placed far from the connector or after the IC they protect, and clamps with no "
                 "short path to ground.",
        # edge_mm: a connector counts as I/O when it is this close to the outline; 0 = all.
        # max_distance_mm: 10 flagged SRV05 arrays spaced along a 20-pin header on a real board,
        # which is a sensible layout; 15 still catches a clamp placed with the IC it protects.
        # max_ground_via_mm is measured from the ground pad's edge, not its centre.
        "params": {"edge_mm": 5.0, "max_distance_mm": 15.0, "max_ground_via_mm": 2.0},
    },
    "connector-shield": {
        "title": "Shield and chassis ground",
        "category": "Immunity",
        "about": "Connector shells and plated mounting holes connected to nothing, and chassis nets "
                 "with no path to the board's ground, so a discharge to the enclosure has to cross "
                 "the circuit.",
        "params": {},
    },
    "reset-filter": {
        "title": "Reset lines",
        "category": "Immunity",
        "about": "Reset inputs held only by a pull-up with no filter capacitor, or with the capacitor "
                 "far from the pin, where a fast transient on the trace resets the board.",
        "params": {"max_distance_mm": 10.0},
    },
    "input-filter": {
        "title": "Power input filtering",
        "category": "Conducted emissions",
        "about": "Power entering through a connector with no capacitor near it, so the loads' "
                 "switching current flows out along the supply cable and a surge meets nothing on "
                 "the way in.",
        "params": {"edge_mm": 5.0, "max_distance_mm": 15.0},
    },
    "switch-node": {
        "title": "Switching regulator nodes",
        "category": "Conducted emissions",
        "about": "Switch nodes with more copper or track than the current needs. The node swings the "
                 "full input voltage in nanoseconds, and its copper couples that into everything "
                 "nearby.",
        "params": {"max_area_mm2": 30.0, "max_length_mm": 15.0},
    },
}

RULE_CATALOGUE["cable-resonance"] = {
    "title": "Cable resonance",
    "category": "Radiated emissions",
    "about": "Cables assigned to connectors that are at their best as antennas at a frequency "
             "this board produces. A cable's resonances depend on the cable, not the layout, so "
             "this needs no solve — and it decides more about a radiated result than the board "
             "does.",
    "params": {
        # The clock whose harmonics are checked against the peaks. Zero means no clock is
        # declared, and the rule reports where the cables peak without claiming anything about
        # whether the board excites them.
        "clock_hz": 0.0,
    },
}


#: Board-wide settings, not tied to one rule.
BOARD_DEFAULTS: dict[str, Any] = {
    "max_frequency_hz": 1e9,
    # Force a permittivity when the board file has none or has the wrong one. The frequency
    # matters: a datasheet 1 MHz figure is optimistic at DDR rates.
    "epsilon_r": 0.0,
    "epsilon_r_at_hz": 1e9,
    "via_ps": 2.0,
}


@dataclass
class Settings:
    board: dict[str, Value] = field(default_factory=dict)
    rules: dict[str, RuleSetting] = field(default_factory=dict)
    groups: list[NetGroupSetting] = field(default_factory=list)
    suppressions: list[Suppression] = field(default_factory=list)
    #: Cable assignments per connector reference (docs/implementation.md §5). ``{"J1": {"type":
    #: "usb2-shielded", "length_m": 2.0}}``. A reference that is absent is **not** assigned a
    #: default: an unassigned connector is not modelled and is reported as incomplete, and a cable
    #: the user never declared would change a result without appearing in it.
    cables: dict[str, dict] = field(default_factory=dict)
    #: Which antenna solver to use. Printed on every cable result (docs/implementation.md §5.1).
    cable_solver: str = "nec2c"
    warnings: list[str] = field(default_factory=list)

    # ---- reading ----

    def value(self, key: str, default: Any = None) -> Any:
        v = self.board.get(key)
        return v.value if v is not None else BOARD_DEFAULTS.get(key, default)

    def rule(self, rule_id: str) -> RuleSetting:
        return self.rules.get(rule_id) or _default_rule(rule_id)

    def enabled(self, rule_id: str) -> bool:
        return self.rule(rule_id).enabled

    def param(self, rule_id: str, key: str, net: str = "", netclass: str = "") -> Any:
        """A rule's parameter, with any matching net-group override applied."""
        for g in reversed(self.groups):
            if key in g.params and g.matches(net, netclass):
                return g.params[key].value
        return self.rule(rule_id).get(key, RULE_CATALOGUE.get(rule_id, {}).get("params", {}).get(key))

    def suppressed(self, rule_id: str, net: str = "") -> Suppression | None:
        for s in self.suppressions:
            if s.covers(rule_id, net):
                return s
        return None

    def severity(self, rule_id: str, natural: str) -> str:
        return self.rule(rule_id).severity or natural


def _default_rule(rule_id: str) -> RuleSetting:
    spec = RULE_CATALOGUE.get(rule_id, {})
    return RuleSetting(
        params={k: Value(v, "default") for k, v in spec.get("params", {}).items()}
    )


def defaults() -> Settings:
    return Settings(
        board={k: Value(v, "default") for k, v in BOARD_DEFAULTS.items()},
        rules={rid: _default_rule(rid) for rid in RULE_CATALOGUE},
    )


def load(*layers: tuple[str, dict | None]) -> Settings:
    """Merge settings documents, later layers winning.

    Each layer is (source, document); an unparseable or absent document is skipped with a
    warning rather than failing the run, because a typo in a settings file should not cost
    somebody their board analysis.
    """
    s = defaults()
    for source, doc in layers:
        if not doc:
            continue
        try:
            _apply(s, source, doc)
        except Exception as exc:  # noqa: BLE001
            s.warnings.append(f"{_label(source)} ignored: {exc}")
    return s


#: Board settings that must be above zero rather than merely not negative: a maximum
#: frequency of zero makes every wavelength infinite.
_POSITIVE = frozenset({"max_frequency_hz", "epsilon_r_at_hz"})


def check_value(where: str, value: Any, default: Any, positive: bool = False) -> Any:
    """A setting as the type its default has, or ValueError saying what is wrong with it.

    Every parameter in the catalogue is a number or a switch. A string such as "1GHz" used to
    be stored as given and fail the run later as an internal error, and a negative value gave
    negative wavelengths; both are refused here with a message naming the setting.
    """
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise ValueError(f"{where} must be true or false, not {value!r}")
    if not isinstance(default, (int, float)):
        return value
    if isinstance(value, bool):
        raise ValueError(f"{where} must be a number, not {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where} must be a number, not {value!r}") from None
    if not math.isfinite(num) or num < 0 or (positive and num == 0):
        raise ValueError(f"{where} must be {'above' if positive else 'at least'} zero, not {value!r}")
    if where.endswith("epsilon_r") and 0 < num < 1:
        raise ValueError(f"{where} must be 1 or more (or 0 to use the board file's), not {value!r}")
    if isinstance(default, int) and num.is_integer():
        return int(num)
    return num


def _param_default(key: str) -> Any:
    """The default a parameter has in whichever rule defines it, for net-group overrides."""
    for spec in RULE_CATALOGUE.values():
        if key in spec.get("params", {}):
            return spec["params"][key]
    return None


def _label(source: str) -> str:
    """A source as a user names it. These warnings are shown on the board, where "file:" and
    "run:" meant nothing to anyone who had not read this module."""
    return {"project": "Project settings", "file": "Rules file",
            "run": "App settings"}.get(source, source)


def _section(s: Settings, source: str, doc: dict, key: str, kind: type) -> Any:
    """One section of a document, or an empty one with a warning when it has the wrong shape."""
    val = doc.get(key)
    if val is None:
        return kind()
    if not isinstance(val, kind):
        s.warnings.append(f"{_label(source)}: {key} should be a {'mapping' if kind is dict else 'list'}; ignored")
        return kind()
    return val


def _apply(s: Settings, source: str, doc: dict) -> None:
    if not isinstance(doc, dict):
        raise ValueError(f"expected a mapping of settings, found a {type(doc).__name__}")
    version = doc.get("version", SETTINGS_VERSION)
    if int(version) > SETTINGS_VERSION:
        raise ValueError(
            f"settings version {version} is newer than this analyzer understands "
            f"({SETTINGS_VERSION}); update the worker"
        )

    for key, val in _section(s, source, doc, "board", dict).items():
        if key not in BOARD_DEFAULTS:
            s.warnings.append(f"{_label(source)}: unknown board setting {key!r}")
            continue
        try:
            val = check_value(key, val, BOARD_DEFAULTS[key], positive=key in _POSITIVE)
        except ValueError as exc:
            s.warnings.append(f"{_label(source)}: {exc}; ignored")
            continue
        s.board[key] = Value(val, source)

    for rule_id, spec in _section(s, source, doc, "rules", dict).items():
        if rule_id not in RULE_CATALOGUE:
            s.warnings.append(f"{_label(source)}: unknown rule {rule_id!r}")
            continue
        rs = s.rules.setdefault(rule_id, _default_rule(rule_id))
        if isinstance(spec, bool):
            spec = {"enabled": spec}
        if not isinstance(spec, dict):
            s.warnings.append(f"{_label(source)}: rule {rule_id} should be true, false or a mapping; ignored")
            continue
        if "enabled" in spec:
            rs.enabled = bool(spec["enabled"])
            rs.enabled_source = source
        if spec.get("severity"):
            if str(spec["severity"]) not in SEVERITIES:
                s.warnings.append(
                    f"{_label(source)}: {rule_id}.severity must be one of {', '.join(SEVERITIES)}, "
                    f"not {spec['severity']!r}; ignored")
            else:
                rs.severity = str(spec["severity"])
                rs.severity_source = source
        known = RULE_CATALOGUE[rule_id].get("params", {})
        params = spec.get("params") or {}
        if not isinstance(params, dict):
            s.warnings.append(f"{_label(source)}: {rule_id}.params should be a mapping; ignored")
            params = {}
        for key, val in params.items():
            if key not in known:
                s.warnings.append(f"{_label(source)}: unknown parameter {rule_id}.{key}")
                continue
            try:
                val = check_value(f"{rule_id}.{key}", val, known[key])
            except ValueError as exc:
                s.warnings.append(f"{_label(source)}: {exc}; ignored")
                continue
            rs.params[key] = Value(val, source)
        rs.source = source

    for g in _section(s, source, doc, "groups", list):
        if not isinstance(g, dict):
            s.warnings.append(f"{_label(source)}: a net group is not a mapping; ignored")
            continue
        params: dict[str, Value] = {}
        raw = g.get("params") or {}
        for k, v in (raw.items() if isinstance(raw, dict) else ()):
            try:
                params[k] = Value(check_value(f"group {g.get('match', '*')}: {k}", v, _param_default(k)), source)
            except ValueError as exc:
                s.warnings.append(f"{_label(source)}: {exc}; ignored")
        s.groups.append(NetGroupSetting(
            match=str(g.get("match", "*")),
            netclass=str(g.get("netclass", "") or ""),
            params=params,
        ))

    cables = doc.get("cables")
    if isinstance(cables, dict):
        solver = cables.get("solver")
        if solver:
            if solver not in ("nec2c", "builtin"):
                s.warnings.append(
                    f"{_label(source)}: unknown antenna solver {solver!r}; keeping {s.cable_solver}")
            else:
                s.cable_solver = solver
        connectors = cables.get("connectors") or {}
        if not isinstance(connectors, dict):
            s.warnings.append(f"{_label(source)}: cables.connectors should be a mapping; ignored")
            connectors = {}
        for ref, spec in connectors.items():
            # Two spellings, because both read naturally: a bare cable id, or a block with a
            # length. "none" is a real answer - a debug header that is never cabled in the
            # product - and is kept rather than dropped, so the connector counts as decided.
            if isinstance(spec, str):
                s.cables[str(ref)] = {"type": spec}
            elif isinstance(spec, dict):
                s.cables[str(ref)] = dict(spec)
            else:
                s.warnings.append(
                    f"{_label(source)}: cable assignment for {ref} is neither a name nor a block")

    for sup in _section(s, source, doc, "suppress", list):
        if not isinstance(sup, dict):
            s.warnings.append(f"{_label(source)}: a suppression is not a mapping; ignored")
            continue
        if not sup.get("reason"):
            # A suppression without a reason is a mystery to whoever finds it later.
            s.warnings.append(f"{_label(source)}: suppression for {sup.get('rule', '*')} has no reason")
        s.suppressions.append(Suppression(
            rule=str(sup.get("rule", "*")), net=str(sup.get("net", "*")),
            reason=str(sup.get("reason", "")),
        ))


def parse_document(text: str) -> dict | None:
    """Read a settings document: YAML, which also reads JSON."""
    text = text.strip()
    if not text:
        return None
    doc = yaml.safe_load(text)
    if doc is not None and not isinstance(doc, dict):
        raise ValueError(f"expected a mapping of settings at the top level, found a {type(doc).__name__}")
    return doc


def snapshot(s: Settings) -> dict:
    """The effective settings as plain data, every value with the layer it came from.

    Written into rules.json so the app can show what a check ran with and where that came
    from, and export an equivalent emi.rules.yaml without re-deriving the merge.
    """
    return {
        "board": {k: {"value": v.value, "source": v.source} for k, v in s.board.items()},
        "rules": {
            rid: {
                "enabled": rs.enabled,
                "enabled_source": rs.enabled_source,
                "severity": rs.severity,
                "severity_source": rs.severity_source,
                "params": {k: {"value": v.value, "source": v.source} for k, v in rs.params.items()},
            }
            for rid, rs in s.rules.items()
        },
        "groups": [
            {**({"match": g.match} if not g.netclass else {"netclass": g.netclass}),
             "params": {k: v.value for k, v in g.params.items()}}
            for g in s.groups
        ],
        "suppress": [{"rule": x.rule, "net": x.net, "reason": x.reason} for x in s.suppressions],
        "cables": s.cables,
        "cable_solver": s.cable_solver,
    }


def board_catalogue() -> list[dict]:
    """The board-wide settings, for the app's settings view. ``positive``: zero is refused."""
    return [
        {"key": k, "default": v, "positive": k in _POSITIVE} for k, v in BOARD_DEFAULTS.items()
    ]


def catalogue() -> list[dict]:
    """The rule catalogue, for a UI that builds its settings page from the server."""
    return [
        {
            "id": rid,
            "title": spec["title"],
            "about": spec["about"],
            "category": spec.get("category", ""),
            "params": [
                {"key": k, "default": v} for k, v in spec.get("params", {}).items()
            ],
        }
        for rid, spec in RULE_CATALOGUE.items()
    ]
