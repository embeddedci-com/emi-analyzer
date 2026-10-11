"""Finding types and the shared rule context."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from typing import TYPE_CHECKING, Any, Iterable

from ..kicad.board import BoardModel

if TYPE_CHECKING:  # annotations only; importing these at run time would be circular
    from ..kicad.netclass import DiffPair, NetClasses
    from ..kicad.normalize import Transform
    from ..stackup import BoardElectrics
    from ..topology import NetTopology
    from .matching import MatchGroup
    from .settings import Settings

FORMAT_VERSION = 1

#: Ground and power net names are recognized word by word, never by substring: "+10V" holds
#: "0v" and "/SIGNDIR" holds "gnd", and neither is a ground. A name splits into words at
#: anything but a letter, a digit or a decimal point, so "+3.3V" is the one word "3.3v" and
#: "VBUS_20V" is "vbus" and "20v".
_WORD_SPLIT = re.compile(r"[^a-z0-9.]+")

#: A word that names a ground: GND and its A/D/P/S variants with any suffix (GNDA, GND1,
#: PGND2), chassis and frame grounds (CHSGND, CGND, FGND, SHIELD), VSS and its variants, and
#: the plain words. A chassis ground carries no signal, so no signal check applies to it.
_GROUND_WORD = re.compile(
    r"^(?:(?:[adpscfe]|ch|chs|chas|chassis)?gnd[a-z0-9]*|vss[a-z0-9]*|ground|earth|0v|chassis|"
    r"shield)$")

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


# ---- what a net carries ----

#: Names that say a net carries fast edges. Word-ish matching: "SCK" in "/SPI1_SCK", not in
#: "/BACKLIGHT"; KiCad's unnamed nets count too, "Net-(USB1-D+)".
FAST_NET = re.compile(
    r"(^|[/_.(-])(clk\w*|\w*clk|sck|sclk|mclk|bclk|lrclk|mosi|miso|copi|cipo|sdo|sdi|spi\w*|"
    r"qspi\w*|ospi\w*|sdio\w*|sd_\w*|mmc\w*|emmc\w*|cmd|dat[0-7]|usb\w*|d[+-]|dp|dm|"
    r"eth\w*|rgmii\w*|rmii\w*|mdi|mdi[\d_+-]\w*|tx_en|rx_dv|tx_clk|rx_clk|ref_clk|hdmi\w*|"
    r"lvds\w*|mipi\w*|csi\w*|dsi\w*|pcie\w*|sata\w*|ddr\w*|dq\w*|dqs\w*|psram\w*)($|[/_.+)-])",
    re.I,
)

#: Names that say a net is slow or static: I2C, resets, enables and controls, status and
#: interrupt lines, LEDs, sense and detect lines, boot straps, a PHY's MDIO management bus,
#: UART flow control.
#: Their edges are slow (an I2C rise is set by its pull-up) or rare (a reset, an enable), so
#: a long run or a detour of the return path is not where a board's emissions come from.
#: Checked before FAST_NET, so that CLK_EN is an enable and ETH_RST a reset.
SLOW_NET = re.compile(
    r"(^|[/_.(-])(i2c\w*|scl\d*|sda\d*|smb\w*|n?rst\w*|n?reset\w*|n?en|enable\w*|ctrl|ctl|"
    r"sense\w*|sns|led\w*|boot\w*|btn\w*|button\w*|pg|pgood|int|n?irq|alert|n?fault|flt|"
    r"st|stat|status|det|detect|sel|mode|cdone|creset\w*|wake\w*|shdn|stby|term\w*|alarm|"
    r"mdio|mdc|n?cts|n?rts)"
    r"($|[/_.+)-])",
    re.I,
)


def net_activity(name: str) -> str:
    """"fast", "slow" or "unknown": what a signal net's name says about its edges.

    Used to weigh findings whose harm depends on the edges a net carries -- a long net, a
    return path that detours -- so that an I2C line or a reset is not reported as critical
    beside a 50 MHz RMII line. A name says nothing for most GPIOs: those are "unknown".
    """
    if SLOW_NET.search(name):
        return "slow"
    if FAST_NET.search(name):
        return "fast"
    return "unknown"


SLOW_NOTE = ("Its name says it is a slow or static line (I2C, a reset, an enable, a status or "
             "sense line), so this is advisory.")


# ---- which IC pins draw from a supply ----

#: Pin names of supply inputs and outputs, once a trailing pin number ("VDD_12") is dropped.
_SUPPLY_PIN = re.compile(
    r"^(?:[adp]?vcc\w*|[adp]?vdd\w*|vee|vs[+-]?\d*|vin\w*|pvin\w*|in\d*|vbat\w*|vbus\w*|"
    r"vout\w*|out\d*|vio\w*|vm|vpp|vreg\w*|ldo\w*|pwr\w*|supply\w*|v[+-]|vlogic|vl|vsys|"
    r"vcore\w*|vref\w*|ref|refin|refout)$",
    re.I,
)
#: Pin names that are clearly not a supply: GPIO port pins and the sense, control and status
#: pins that sit on a supply-named net (a current monitor's IN+, a power mux's ST).
_SIGNAL_PIN = re.compile(
    r"^(?:p[a-k]\d+|pio\d*_?\d+|gpio\w*|io\w*|iob_\w+|[+-]?in[a-d]?[+-]|[+-]in[a-d]?|inp|inn|"
    r"s[+-]|s\d+[ab]?|d[ab]?|[+-]?out[a-d][+-]?|sense\w*|sns\w*|"
    r"fb|adj|n?en\w*|st|stat\w*|pr\d*|pg\w*|n?flt\w*|fault|alert|n?int\w*|irq|scl|sda|"
    r"ilim\w*|imon\w*|adc\w*|mode|sel\w*|n?cs\w*|set|n?rst\w*|reset\w*|mon\w*|det\w*|"
    r"dvdt|ss|tr|nc|ctrl\w*)$",
    re.I,
)
_SIGNAL_PIN_TYPES = frozenset({"input", "output", "bidirectional", "tri_state",
                               "open_collector", "open_emitter"})


def pin_name(function: str) -> str:
    """A pad's pin name without the pin number some libraries append ("PG3_88" -> "PG3"), and
    with the typographic minus signs symbol libraries use ("VS–", "VS−") as a plain "-"."""
    name = re.sub(r"_\d+$", "", function.strip())
    return name.replace("\u2013", "-").replace("\u2212", "-")


def is_supply_pin(pad) -> bool:
    """Whether an IC pad on a supply-named net draws from (or drives) that supply.

    Without pin data -- a board never updated from its schematic, a Gerber set -- every pad on
    the net counts, as it always did. With it, a pad is left out only on positive evidence: a
    signal pin type, or a name that is clearly a GPIO or a sense, control or status pin. An
    INA226's IN+ on +5V_IN measures the rail; it does not need decoupling.
    """
    function = pin_name(getattr(pad, "pin_function", "") or "")
    kind = (getattr(pad, "pin_type", "") or "").lower()
    if kind in ("power_in", "power_out"):
        return True
    if not function:
        return kind not in _SIGNAL_PIN_TYPES
    if _SUPPLY_PIN.match(function):
        return True
    if kind in _SIGNAL_PIN_TYPES or _SIGNAL_PIN.match(function):
        return False
    return True


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
    #: What to do, in one short line, when it differs from the rule's usual fix. One rule can
    #: find different problems: decoupling reports a missing capacitor, a distant one and one
    #: with no ground via, and "move it closer" was shown for all three.
    action: str = ""

    def as_dict(self, index: int) -> dict:
        d = asdict(self)
        if not d["action"]:
            d.pop("action")
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
    transform: Transform
    max_frequency_hz: float
    #: Everything that is configurable. Defaults when the caller passes nothing, so a check
    #: can always read a threshold without asking whether settings exist.
    settings: Settings | None = None
    #: Per-layer delay and reference-plane geometry, from the stackup.
    electrics: BoardElectrics | None = None
    #: Net connectivity: pin-to-pin paths, not total copper.
    topology: dict[str, NetTopology] = field(default_factory=dict)
    netclasses: NetClasses | None = None
    pairs: list[DiffPair] = field(default_factory=list)
    groups: list[MatchGroup] = field(default_factory=list)
    #: Propagation velocity factor in FR-4, used for the wavelength checks. c/sqrt(er) with
    #: er ~4.4 gives roughly 0.48c; microstrip sees a lower effective er, so 0.5 is a fair
    #: middle. Being slightly optimistic here means slightly fewer findings, not more.
    velocity_factor: float = 0.5
    notes: list[str] = field(default_factory=list)

    def _settings(self) -> Settings:
        if self.settings is None:
            from .settings import defaults
            self.settings = defaults()
        return self.settings

    def setting(self, rule_id: str, key: str, net: str = "") -> Any:
        """A rule's threshold, with any net-group override applied."""
        netclass = self.netclasses.of(net) if (self.netclasses and net) else ""
        return self._settings().param(rule_id, key, net=net, netclass=netclass)

    def enabled(self, rule_id: str) -> bool:
        return self._settings().enabled(rule_id)

    def group_sets(self, key: str, net: str) -> bool:
        """A net group matching this net sets the parameter (not the board-wide default)."""
        netclass = self.netclasses.of(net) if (self.netclasses and net) else ""
        return any(key in g.params and g.matches(net, netclass) for g in self._settings().groups)

    def ps_per_mm(self, layer: str) -> float:
        return self.electrics.ps_per_mm(layer) if self.electrics else 6.0

    def single_part(self, net: str) -> bool:
        """The net joins two or more pins of one part and nothing else: a strap inside a
        connector (a USB charging port's D+ to D-), a loopback. Nothing on the board drives
        it, so its routing cannot radiate what the board does; the return-path checks skip
        it. A net with a single pad is not one of these: its other end may be a via, a test
        pad or a part the board file does not show."""
        cache = self.__dict__.get("_single_part")
        if cache is None:
            refs: dict[str, set[str]] = {}
            count: dict[str, int] = {}
            for p in self.model.pads:
                if p.net:
                    refs.setdefault(p.net, set()).add(p.ref)
                    count[p.net] = count.get(p.net, 0) + 1
            cache = {n for n, r in refs.items() if len(r) == 1 and count[n] >= 2}
            self.__dict__["_single_part"] = cache
        return net in cache

    @property
    def has_planes(self) -> bool:
        """A copper layer is a dedicated plane (power or mixed): routing has a reference."""
        return any(layer.kind in ("power", "mixed") for layer in self.model.copper_layers)

    def pt(self, x: float, y: float) -> tuple[float, float]:
        return self.transform.pt(x, y)

    @property
    def wavelength_mm(self) -> float:
        # Velocity comes from the stackup when it is known. The old constant 0.5 was a fair
        # middle for FR-4 microstrip, but a board with a different dielectric -- or a signal
        # on an inner layer, which is ~20% slower -- was measured against the wrong ruler.
        vf = self.electrics.mean_velocity_factor if self.electrics else self.velocity_factor
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
    noisy check cannot crowd out a quiet, serious one -- and within a rule the most severe
    are kept, so advisories on slow nets cannot crowd out warnings on fast ones. What is
    kept stays in the order the checks found it.
    """
    order = {"critical": 0, "warning": 1, "info": 2}
    findings = list(findings)
    by_rule: dict[str, list[int]] = {}
    for i, f in enumerate(findings):
        by_rule.setdefault(f.rule, []).append(i)
    keep: set[int] = set()
    truncated: dict[str, int] = {}
    for rule, idx in by_rule.items():
        ranked = sorted(idx, key=lambda i: order.get(findings[i].severity, 3))  # stable
        keep.update(ranked[:per_rule_limit])
        if len(ranked) > per_rule_limit:
            truncated[rule] = len(ranked) - per_rule_limit
    out: list[Finding] = [f for i, f in enumerate(findings) if i in keep]

    for rule, extra in truncated.items():
        out.append(Finding(
            rule=rule,
            severity="info",
            title=f"{extra} more {rule.replace('-', ' ')} findings not listed",
            detail=(
                f"Only the {per_rule_limit} most severe are shown. A count this high usually "
                f"means one systematic cause rather than {extra + per_rule_limit} "
                f"separate problems."
            ),
        ))
    return out
