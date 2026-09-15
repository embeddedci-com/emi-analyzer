"""The ``emi-driver`` document: parsing, validation, and provenance.

§9.2's format. The same rules run in the browser
(``webapp/src/lib/driverDocument.ts``) and here, because §9.2 asks for both and because the
two failure modes are different: the browser's job is to tell someone their numbers do not
make sense while they are typing them, and the worker's is to refuse a document that arrived
some other way — BenchPod, a CI commit, a hand-edited file.

**Every number carries where it came from.** That is not decoration: the weakest source in a
driver sets a term in the confidence budget (§17.2), travels with the result, and is what the
UI puts on a chip. A document whose amplitude was measured and whose rise time was guessed is
a different document from one measured throughout, and the tool has to say so.
"""

from __future__ import annotations

from dataclasses import dataclass

from emi_worker.drivers.spectrum import DriverError, Trapezoid

FORMAT = "emi-driver"
VERSION = 1

#: Where a number came from, worst last. §9.2's list.
SOURCES = ("scope", "spectrum-analyzer", "benchpod", "datasheet", "assumed")

#: Provisional sigma per source, in dB, from §17.2. These are engineering placeholders that
#: M5 replaces with residuals from recorded lab results; they are ordered, and the ordering
#: is the part that matters today.
SOURCE_SIGMA_DB = {
    "scope": 1.0,
    "spectrum-analyzer": 1.0,
    "benchpod": 1.5,
    "datasheet": 3.0,
    "assumed": 6.0,
}

KINDS = ("trapezoid", "waveform", "spectrum")
ROLES = ("signal", "switching-regulator")

#: §9.2's caps. A driver document is stored as a jsonb column and parsed in a browser tab.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_SAMPLES = 1_000_000

TRAPEZOID_FIELDS = (
    "amplitude_v", "period_s", "pulse_width_s", "rise_s", "fall_s", "source_impedance_ohm",
)


@dataclass(frozen=True)
class Sourced:
    """One number and where it came from."""

    value: float
    source: str
    detail: str | None = None


@dataclass(frozen=True)
class Driver:
    name: str
    kind: str
    role: str
    net: str | None
    values: dict[str, Sourced]
    #: Kind-specific payload that is not a sourced number: samples, points, bandwidth.
    payload: dict

    def weakest_source(self) -> str:
        """The source that sets this driver's confidence term (§17.2).

        Worst, not average: a spectrum is only as trustworthy as the number that shaped the
        part of it you are reading, and an averaged provenance would hide a guessed rise
        time behind four measured values.
        """
        return max(self.values.values(), key=lambda s: SOURCE_SIGMA_DB[s.source]).source

    def sigma_db(self) -> float:
        return SOURCE_SIGMA_DB[self.weakest_source()]

    def trapezoid(self) -> Trapezoid:
        if self.kind != "trapezoid":
            raise DriverError(f"a {self.kind} driver has no trapezoid parameters")
        return Trapezoid(
            amplitude_v=self.values["amplitude_v"].value,
            period_s=self.values["period_s"].value,
            pulse_width_s=self.values["pulse_width_s"].value,
            rise_s=self.values["rise_s"].value,
            fall_s=self.values["fall_s"].value,
        )

    def source_impedance_ohm(self) -> float:
        return self.values["source_impedance_ohm"].value


def _sourced(raw: object, where: str) -> Sourced:
    if not isinstance(raw, dict):
        raise DriverError(f"{where} must be an object with a value and a source")
    if "value" not in raw:
        raise DriverError(f"{where} has no value")
    if "source" not in raw:
        raise DriverError(
            f"{where} does not say where it came from. Every number in a driver carries a "
            f"source, one of: {', '.join(SOURCES)}"
        )
    source = raw["source"]
    if source not in SOURCES:
        raise DriverError(f"{where}: unknown source {source!r}; use one of {', '.join(SOURCES)}")
    value = raw["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DriverError(f"{where}: value must be a number, got {type(value).__name__}")
    if value != value or value in (float("inf"), float("-inf")):
        raise DriverError(f"{where}: value must be finite")
    detail = raw.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise DriverError(f"{where}: detail must be text")
    return Sourced(value=float(value), source=source, detail=detail)


def parse(doc: dict, *, document_bytes: int | None = None) -> Driver:
    """Validate an ``emi-driver`` document and return it in a usable shape.

    ``document_bytes`` is the encoded size when the caller has it, so the 2 MB cap is
    enforced against what actually arrived rather than against a re-serialisation of it.
    """
    if not isinstance(doc, dict):
        raise DriverError("a driver document must be a JSON object")
    if document_bytes is not None and document_bytes > MAX_DOCUMENT_BYTES:
        raise DriverError(
            f"driver document is {document_bytes / 1e6:.1f} MB, over the "
            f"{MAX_DOCUMENT_BYTES / 1e6:.0f} MB limit"
        )
    if doc.get("format") != FORMAT:
        raise DriverError(f"not a driver document: format={doc.get('format')!r}")

    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise DriverError(f"version must be a whole number, got {version!r}")
    if version > VERSION:
        raise DriverError(
            f"this driver is version {version} and this build understands version {VERSION}. "
            f"Update the analyzer rather than letting it read the parts it recognises"
        )
    if version < 1:
        raise DriverError(f"version must be at least 1, got {version}")

    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DriverError("a driver needs a name")

    kind = doc.get("kind")
    if kind not in KINDS:
        raise DriverError(f"unknown driver kind {kind!r}; use one of {', '.join(KINDS)}")

    role = doc.get("role", "signal")
    if role not in ROLES:
        raise DriverError(f"unknown role {role!r}; use one of {', '.join(ROLES)}")

    net = doc.get("net")
    if net is not None and not isinstance(net, str):
        raise DriverError("net must be text")

    values: dict[str, Sourced] = {}
    payload: dict = {}

    if kind == "trapezoid":
        body = doc.get("trapezoid")
        if not isinstance(body, dict):
            raise DriverError("a trapezoid driver needs a trapezoid block")
        for field in TRAPEZOID_FIELDS:
            if field not in body:
                raise DriverError(f"trapezoid.{field} is missing")
            values[field] = _sourced(body[field], f"trapezoid.{field}")
        # The physics check, which is also §9.2's "rise + fall < period".
        Trapezoid(
            amplitude_v=values["amplitude_v"].value,
            period_s=values["period_s"].value,
            pulse_width_s=values["pulse_width_s"].value,
            rise_s=values["rise_s"].value,
            fall_s=values["fall_s"].value,
        ).validate()
        if values["source_impedance_ohm"].value < 0:
            raise DriverError("source impedance must not be negative")

    elif kind == "waveform":
        body = doc.get("waveform")
        if not isinstance(body, dict):
            raise DriverError("a waveform driver needs a waveform block")
        samples = body.get("samples_v")
        if not isinstance(samples, list) or len(samples) < 2:
            raise DriverError("a waveform needs at least two samples")
        if len(samples) > MAX_SAMPLES:
            raise DriverError(
                f"{len(samples):,} samples is over the {MAX_SAMPLES:,} limit; decimate the "
                f"capture or shorten it"
            )
        for i, s in enumerate(samples):
            if isinstance(s, bool) or not isinstance(s, (int, float)):
                raise DriverError(f"waveform.samples_v[{i}] is not a number")
        interval = body.get("sample_interval_s")
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            raise DriverError("waveform.sample_interval_s must be a positive number")
        for field in ("period_s", "source_impedance_ohm"):
            if field not in body:
                raise DriverError(f"waveform.{field} is missing")
            values[field] = _sourced(body[field], f"waveform.{field}")
        # §9.2: at least one full period must be captured, or the transform is of a
        # fragment and every harmonic is wrong.
        captured = (len(samples) - 1) * float(interval)
        period = values["period_s"].value
        if captured < period:
            raise DriverError(
                f"the capture is {captured * 1e9:.4g} ns long and one period is "
                f"{period * 1e9:.4g} ns. Transforming less than a full period would put "
                f"every harmonic in the wrong place"
            )
        bandwidth = body.get("bandwidth_hz")
        if bandwidth is not None:
            if not isinstance(bandwidth, (int, float)) or bandwidth <= 0:
                raise DriverError("waveform.bandwidth_hz must be a positive number")
        if "rise_s" in body:
            values["rise_s"] = _sourced(body["rise_s"], "waveform.rise_s")
        payload = {
            "samples_v": [float(s) for s in samples],
            "sample_interval_s": float(interval),
            "bandwidth_hz": None if bandwidth is None else float(bandwidth),
        }

    else:  # spectrum
        body = doc.get("spectrum")
        if not isinstance(body, dict):
            raise DriverError("a spectrum driver needs a spectrum block")
        points = body.get("points")
        if not isinstance(points, list) or len(points) < 2:
            raise DriverError("a spectrum needs at least two points")
        if len(points) > MAX_SAMPLES:
            raise DriverError(f"{len(points):,} points is over the {MAX_SAMPLES:,} limit")
        parsed: list[tuple[float, float]] = []
        for i, p in enumerate(points):
            if not isinstance(p, dict) or "frequency_hz" not in p or "level_dbuv" not in p:
                raise DriverError(
                    f"spectrum.points[{i}] needs a frequency_hz and a level_dbuv"
                )
            f, lv = p["frequency_hz"], p["level_dbuv"]
            if not isinstance(f, (int, float)) or isinstance(f, bool) or f <= 0:
                raise DriverError(f"spectrum.points[{i}].frequency_hz must be positive")
            if not isinstance(lv, (int, float)) or isinstance(lv, bool):
                raise DriverError(f"spectrum.points[{i}].level_dbuv must be a number")
            parsed.append((float(f), float(lv)))
        if any(b[0] <= a[0] for a, b in zip(parsed, parsed[1:])):
            raise DriverError("spectrum points must be in increasing frequency order")
        if "source_impedance_ohm" not in body:
            raise DriverError("spectrum.source_impedance_ohm is missing")
        values["source_impedance_ohm"] = _sourced(
            body["source_impedance_ohm"], "spectrum.source_impedance_ohm")
        rbw = body.get("rbw_hz")
        if rbw is not None and (not isinstance(rbw, (int, float)) or rbw <= 0):
            raise DriverError("spectrum.rbw_hz must be a positive number")
        payload = {"points": parsed, "rbw_hz": None if rbw is None else float(rbw)}

    if role == "switching-regulator":
        for field in ("input_current_a", "input_voltage_v"):
            if field not in doc:
                raise DriverError(
                    f"a switching-regulator driver needs {field}, which the conducted "
                    f"prediction reads (§16.3)"
                )
            values[field] = _sourced(doc[field], field)

    if not values:
        raise DriverError("a driver document carries no numbers")

    return Driver(name=name.strip(), kind=kind, role=role, net=net,
                  values=values, payload=payload)
