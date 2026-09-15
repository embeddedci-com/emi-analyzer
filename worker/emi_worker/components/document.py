"""The ``emi-component`` document and the resolver registry (§11.2).

A component says how to model a part the analyzer would otherwise treat as bare copper. The
document is deliberately thin: an ``id``, what it ``match``es, and a ``model`` whose ``type``
names a resolver. Adding a kind of model means one schema, one resolver and fixtures — not a
change to everything that reads components.

**Built-in numbers are cited, never remembered.** A model carrying an ESL or ESR without a
`sources` entry saying where it came from is refused, not used with a shrug. This is the
difference between a tool that reports a self-resonance and one that reports a number that
sounds like a self-resonance: 0.4 nH and 0.6 nH for the same 0402 move the SRF by 20 %, and
nobody can tell which was meant unless the document says.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

FORMAT = "emi-component"
VERSION = 1

KINDS = ("capacitor",)

#: Where a component's numbers come from, which is a different question from which document
#: they are written in. It decides what the UI may claim about them.
#:
#:   generic   typical figures for a package and value class. A starting point, not a part.
#:             Shown as "generic" and never attributed to a manufacturer, because they do not
#:             describe any specific one.
#:   vendor    a named part, from its datasheet. Must cite that datasheet.
#:   measured  from an instrument, with the instrument named.
#:   user      entered by whoever owns the component.
PROVENANCE = ("generic", "vendor", "measured", "user")

#: Model types this build can resolve. §11.2 lists more for later phases; a document naming
#: one of those is a valid document that this build cannot use, and says so distinctly from
#: one that is malformed.
RESOLVERS = ("series_rlc", "mlcc_family")
FUTURE_RESOLVERS = ("touchstone", "ferrite", "regulator_output", "connector", "package")


class ComponentError(ValueError):
    """A component document that cannot be read, or a model that cannot be resolved."""


@dataclass(frozen=True)
class Source:
    """Where a number came from. Free text, but not optional."""

    doc: str
    rev: str = ""
    what: str = ""

    def describe(self) -> str:
        parts = [self.doc]
        if self.rev:
            parts.append(f"rev {self.rev}")
        return " ".join(parts) + (f" ({self.what})" if self.what else "")


@dataclass(frozen=True)
class SeriesRLC:
    """The circuit a capacitor actually is: ESR and ESL in series with C."""

    c_f: float
    esl_h: float | None
    esr_ohm: float | None
    #: When False, the part's ESL excludes the mounting loop, which the solve models itself.
    esl_includes_mount: bool = False

    def self_resonance_hz(self) -> float | None:
        """The frequency above which a capacitor is an inductor. None without an ESL."""
        if self.esl_h is None or self.esl_h <= 0 or self.c_f <= 0:
            return None
        return 1.0 / (2.0 * math.pi * math.sqrt(self.esl_h * self.c_f))

    def impedance_at(self, frequency_hz: float) -> complex:
        if self.esl_h is None or self.esr_ohm is None:
            raise ComponentError(
                "impedance needs both an ESL and an ESR; this model carries neither a "
                "measured nor a cited value for at least one of them"
            )
        w = 2.0 * math.pi * frequency_hz
        return complex(self.esr_ohm, w * self.esl_h - 1.0 / (w * self.c_f))

    @property
    def complete(self) -> bool:
        return self.esl_h is not None and self.esr_ohm is not None


@dataclass(frozen=True)
class MLCCFamily:
    """A whole class of MLCCs, rather than one value in one package (§11.2).

    ESL is very nearly a property of the **package** alone: it is the loop through the
    terminations and the body, which a 1 nF and a 10 uF 0402 share. ESR is not — it falls as
    capacitance rises — so it comes from a short table interpolated in log C.

    This is what makes a generic library tractable. Enumerating value-and-package pairs got
    46 % of the capacitors on four real boards; one family entry per package covers the rest,
    because the missing ones were common values nobody had typed in yet rather than anything
    unusual.
    """

    #: Imperial package code -> ESL in henries, excluding the mounting loop.
    esl_by_package: dict[str, float]
    #: (capacitance F, ESR ohm) in increasing capacitance, interpolated in log-log.
    esr_table: tuple[tuple[float, float], ...]

    def esl_for(self, package: str) -> float | None:
        return self.esl_by_package.get(package)

    def esr_for(self, c_f: float) -> float | None:
        """Interpolated ESR. Clamped at both ends rather than extrapolated: beyond the table
        the trend is not something this family knows, and a straight line off the end would
        invent a number with no basis."""
        if not self.esr_table or c_f <= 0:
            return None
        points = self.esr_table
        if c_f <= points[0][0]:
            return points[0][1]
        if c_f >= points[-1][0]:
            return points[-1][1]
        for (c0, r0), (c1, r1) in zip(points, points[1:]):
            if c0 <= c_f <= c1:
                t = (math.log10(c_f) - math.log10(c0)) / (math.log10(c1) - math.log10(c0))
                return 10 ** (math.log10(r0) + t * (math.log10(r1) - math.log10(r0)))
        return None


@dataclass(frozen=True)
class Component:
    id: str
    kind: str
    name: str
    match: dict
    model_type: str
    model: dict
    sources: tuple[Source, ...] = ()
    valid_hz: tuple[float, float] | None = None
    esl_includes_mount: bool = False
    provenance: str = "user"

    @property
    def is_generic(self) -> bool:
        return self.provenance == "generic"

    def describe_provenance(self) -> str:
        """One line for the UI. Never names a manufacturer for a generic entry, because a
        generic entry does not describe one."""
        if self.provenance == "generic":
            return "generic — typical for this package and value, not a specific part"
        if self.provenance == "vendor":
            cited = self.cites("ESL") or self.cites("")
            return f"from the part datasheet{f' ({cited.describe()})' if cited else ''}"
        if self.provenance == "measured":
            cited = self.cites("")
            return f"measured{f' ({cited.describe()})' if cited else ''}"
        return "entered by hand"

    def series_rlc(self) -> SeriesRLC:
        if self.model_type != "series_rlc":
            raise ComponentError(
                f"{self.id} is a {self.model_type} model, not a series R-L-C")
        return SeriesRLC(
            c_f=float(self.model["c_f"]),
            esl_h=_opt_float(self.model.get("esl_h")),
            esr_ohm=_opt_float(self.model.get("esr_ohm")),
            esl_includes_mount=self.esl_includes_mount,
        )

    def mlcc_family(self) -> MLCCFamily:
        if self.model_type != "mlcc_family":
            raise ComponentError(f"{self.id} is a {self.model_type} model, not an MLCC family")
        packages = self.model.get("esl_h_by_package") or {}
        table = self.model.get("esr_ohm_by_c") or []
        return MLCCFamily(
            esl_by_package={str(k): float(v) for k, v in packages.items()},
            esr_table=tuple(sorted((float(c), float(r)) for c, r in table)),
        )

    def resolve_for(self, c_f: float, package: str) -> SeriesRLC | None:
        """A per-part series R-L-C from whichever model type this component carries."""
        if self.model_type == "series_rlc":
            return self.series_rlc()
        family = self.mlcc_family()
        esl = family.esl_for(package)
        if esl is None:
            return None
        return SeriesRLC(c_f=c_f, esl_h=esl, esr_ohm=family.esr_for(c_f),
                         esl_includes_mount=self.esl_includes_mount)

    def cites(self, what: str) -> Source | None:
        """The source that covers ``what``, if any. Matching is deliberately loose."""
        for s in self.sources:
            if not s.what or what.lower() in s.what.lower():
                return s
        return None


def _opt_float(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ComponentError(f"expected a number or null, got {v!r}")
    return float(v)


def parse(doc: dict) -> Component:
    """Validate an ``emi-component`` document."""
    if not isinstance(doc, dict):
        raise ComponentError("a component document must be a JSON object")
    if doc.get("format") != FORMAT:
        raise ComponentError(f"not a component document: format={doc.get('format')!r}")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ComponentError(f"version must be a whole number from 1, got {version!r}")
    if version > VERSION:
        raise ComponentError(
            f"this component is version {version} and this build understands version "
            f"{VERSION}. Update the analyzer rather than reading the parts it recognises"
        )
    for required in ("id", "kind", "name"):
        if not isinstance(doc.get(required), str) or not doc[required].strip():
            raise ComponentError(f"a component needs a {required}")
    if doc["kind"] not in KINDS:
        raise ComponentError(
            f"unknown component kind {doc['kind']!r}; this build models {', '.join(KINDS)}")

    model = doc.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("type"), str):
        raise ComponentError("a component needs a model with a type")
    model_type = model["type"]
    if model_type in FUTURE_RESOLVERS:
        raise ComponentError(
            f"{model_type!r} is a model type this build does not resolve yet. The document "
            f"is valid; the resolver ships in a later phase"
        )
    if model_type not in RESOLVERS:
        raise ComponentError(f"unknown model type {model_type!r}")

    sources = tuple(
        Source(doc=str(s.get("doc", "")), rev=str(s.get("rev", "")), what=str(s.get("what", "")))
        for s in doc.get("sources", []) or []
        if isinstance(s, dict)
    )

    valid = doc.get("valid_hz")
    valid_hz = None
    if valid is not None:
        if (not isinstance(valid, (list, tuple)) or len(valid) != 2
                or not all(isinstance(v, (int, float)) for v in valid)):
            raise ComponentError("valid_hz must be a [low, high] pair in hertz")
        if valid[0] >= valid[1]:
            raise ComponentError("valid_hz must be increasing")
        valid_hz = (float(valid[0]), float(valid[1]))

    provenance = doc.get("provenance", "user")
    if provenance not in PROVENANCE:
        raise ComponentError(
            f"unknown provenance {provenance!r}; use one of {', '.join(PROVENANCE)}")

    component = Component(
        id=doc["id"].strip(), kind=doc["kind"], name=doc["name"].strip(),
        match=doc.get("match") or {}, model_type=model_type, model=model,
        sources=sources, valid_hz=valid_hz,
        esl_includes_mount=bool(doc.get("esl_includes_mount", False)),
        provenance=provenance,
    )

    # §11.2's rule, enforced rather than documented: a number with no source is not usable.
    #
    # "Source" means what kind of number this is, not who to credit. A generic entry says so
    # and is done; a vendor part has to name its datasheet, because that is the claim being
    # made about it. A value with nothing at all attached is the case this refuses: it is
    # indistinguishable from a remembered one, and remembered numbers are how a tool ends up
    # reporting a self-resonance 20 % away from the truth with no way to find out.
    if model_type == "series_rlc":
        rlc = component.series_rlc()
        for value, what in ((rlc.esl_h, "ESL"), (rlc.esr_ohm, "ESR")):
            if value is not None and component.cites(what) is None:
                raise ComponentError(
                    f"{component.id} gives an {what} of {value} but says nothing about where "
                    f"it came from. Add a sources entry — for a generic figure that is simply "
                    f"what it is, and for a vendor part it is the datasheet"
                )
    if model_type == "mlcc_family":
        family = component.mlcc_family()
        if not family.esl_by_package:
            raise ComponentError(f"{component.id} is an MLCC family with no packages")
        if component.cites("ESL") is None:
            raise ComponentError(
                f"{component.id} gives per-package ESL values but says nothing about where "
                f"they came from")
    if provenance == "vendor" and not sources:
        raise ComponentError(
            f"{component.id} claims to come from a vendor datasheet but cites none")
    return component


@dataclass
class Resolved:
    """A part, the component chosen for it, and what that component could not supply."""

    ref: str
    component_id: str
    component_name: str
    rlc: SeriesRLC
    #: Empty when the model is complete enough to place.
    gaps: list[str] = field(default_factory=list)
    source: Source | None = None
    #: True when the numbers are a class average rather than a specific part. Callers must
    #: say so and must not attribute them to a manufacturer, because they describe none.
    generic: bool = False

    @property
    def placeable(self) -> bool:
        return not self.gaps
