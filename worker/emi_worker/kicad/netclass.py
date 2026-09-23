"""Netclasses and differential pairs.

Neither is in the board file. KiCad keeps them in the *project* file next to it -- classes
with their track widths and diff-pair geometry, plus glob patterns assigning nets to
classes -- so both are available when somebody uploads a zipped project and absent when they
upload a bare ``.kicad_pcb``.

That is worth stating plainly rather than papering over, because a netclass is the designer
telling us the answer. A class called ``DDR_DQ0`` identifies a byte lane with no guessing at
all, and ``diff_pair_gap`` is the spacing differential impedance needs. Without the project
file both have to be inferred from net names, which is a guess, and findings say which of
the two happened.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field

#: How KiCad names the two halves of a pair, and how everyone else does too. Ordered so the
#: longest, least ambiguous suffixes are tried first: a net called "CLK_P" is a pair half,
#: but so is "CLKP", and stripping the wrong one turns "VCCP" into "VCC".
PAIR_SUFFIXES = (("_P", "_N"), ("_p", "_n"), ("+", "-"), ("P", "N"))


@dataclass
class NetClass:
    name: str
    track_width_mm: float = 0.0
    diff_pair_width_mm: float = 0.0
    diff_pair_gap_mm: float = 0.0
    via_diameter_mm: float = 0.0


@dataclass
class DiffPair:
    """Two nets routed as one signal."""

    base: str
    positive: str
    negative: str
    netclass: str = ""
    gap_mm: float = 0.0
    width_mm: float = 0.0
    #: "netclass" when the project file declared the geometry, "name" when only the naming
    #: convention suggested a pair. The difference matters to how much a finding claims.
    source: str = "name"


@dataclass
class NetClasses:
    classes: dict[str, NetClass] = field(default_factory=dict)
    #: net name -> class name
    assignment: dict[str, str] = field(default_factory=dict)
    patterns: list[tuple[str, str]] = field(default_factory=list)
    available: bool = False
    warnings: list[str] = field(default_factory=list)

    def of(self, net: str) -> str:
        if net in self.assignment:
            return self.assignment[net]
        for pattern, cls in self.patterns:
            if fnmatch.fnmatch(net, pattern):
                return cls
        return "Default" if "Default" in self.classes else ""

    def spec(self, net: str) -> NetClass | None:
        return self.classes.get(self.of(net))


def parse_project(data: bytes | str) -> NetClasses:
    """Read net settings out of a ``.kicad_pro``.

    Tolerant by design: a project file is large, mostly about things this tool does not care
    about, and its shape moves between KiCad versions. Anything unreadable degrades to "no
    netclasses" rather than failing the run.
    """
    out = NetClasses()
    try:
        doc = json.loads(data if isinstance(data, str) else data.decode("utf-8", "replace"))
    except (ValueError, AttributeError) as exc:
        out.warnings.append(f"project file could not be read ({exc}); netclasses unavailable")
        return out

    settings = doc.get("net_settings") or {}
    for c in settings.get("classes") or []:
        name = c.get("name") or ""
        if not name:
            continue
        out.classes[name] = NetClass(
            name=name,
            track_width_mm=float(c.get("track_width") or 0.0),
            diff_pair_width_mm=float(c.get("diff_pair_width") or 0.0),
            diff_pair_gap_mm=float(c.get("diff_pair_gap") or 0.0),
            via_diameter_mm=float(c.get("via_diameter") or 0.0),
        )

    # Three generations of saying which net is in which class, and all three are in use:
    #
    #   KiCad 6     each class lists its members: classes[].nets
    #   KiCad 7, 8  netclass_assignments maps net -> class name, plus netclass_patterns
    #   KiCad 9+    netclass_assignments maps net -> [class names], plus netclass_patterns
    #
    # This used to read a "classAssignments" key that no KiCad version writes, so every
    # project's explicit assignments were silently dropped and nets fell back to Default.
    for c in settings.get("classes") or []:
        for net in c.get("nets") or ():
            if isinstance(net, str) and c.get("name"):
                out.assignment[net] = c["name"]

    for net, cls in (settings.get("netclass_assignments") or {}).items():
        if isinstance(cls, list):
            # A KiCad 9 net can sit in several classes, which KiCad merges into one
            # effective class. This model holds one, so take the first the file defines.
            cls = next((c for c in cls if c in out.classes), cls[0] if cls else "")
        if isinstance(cls, str) and cls:
            out.assignment[net] = cls

    for p in settings.get("netclass_patterns") or []:
        pattern, cls = p.get("pattern"), p.get("netclass")
        if pattern and cls:
            out.patterns.append((pattern, cls))

    out.available = bool(out.classes)
    return out


def split_pair_name(net: str) -> tuple[str, bool] | None:
    """("BASE", is_positive) if this net name looks like half of a pair."""
    for pos, neg in PAIR_SUFFIXES:
        if net.endswith(pos) and len(net) > len(pos):
            return net[: -len(pos)].rstrip("_-"), True
        if net.endswith(neg) and len(net) > len(neg):
            return net[: -len(neg)].rstrip("_-"), False
    return None


def find_pairs(nets: list[str], classes: NetClasses | None = None) -> list[DiffPair]:
    """Pair nets up by name, taking geometry from the netclass where there is one.

    A pair is only reported when *both* halves exist. "CLK_P" alone is a net whose name ends
    in P, not half of a pair, and inventing its partner would produce a finding about a
    signal that is not there.
    """
    bases: dict[str, dict[bool, str]] = {}
    for net in nets:
        split = split_pair_name(net)
        if not split:
            continue
        base, positive = split
        bases.setdefault(base, {})[positive] = net

    out: list[DiffPair] = []
    for base, halves in sorted(bases.items()):
        if True not in halves or False not in halves:
            continue
        pair = DiffPair(base=base, positive=halves[True], negative=halves[False])
        spec = classes.spec(pair.positive) if classes else None
        if spec and (spec.diff_pair_gap_mm or spec.diff_pair_width_mm):
            pair.netclass = spec.name
            pair.gap_mm = spec.diff_pair_gap_mm
            pair.width_mm = spec.diff_pair_width_mm
            pair.source = "netclass"
        elif classes:
            pair.netclass = classes.of(pair.positive)
        out.append(pair)
    return out
