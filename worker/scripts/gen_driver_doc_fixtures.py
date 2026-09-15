#!/usr/bin/env python3
"""Regenerate the shared driver-document fixtures.

Asserted against by worker/tests/test_driver_document.py and
webapp/src/lib/driverDocument.test.ts. §9.2 asks for validation in the browser *and* on the
server; these fixtures are what stops the two from drifting apart, which would show up as a
document a user was allowed to save and the worker then refused.

Invalid cases carry `must_mention` rather than a full message. Matching exact wording across
two languages would be brittle for no benefit; what matters is that both refuse, and that
both say which field and why.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emi_worker.drivers.document import parse  # noqa: E402
from emi_worker.drivers.apply import PortSpectrum, apply_driver  # noqa: E402
from emi_worker.drivers.resolve import resolve  # noqa: E402
from emi_worker.drivers.spectrum import DriverError  # noqa: E402

OUT = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
       / "driver_document_fixtures.json")


def trapezoid(**over):
    body = {
        "amplitude_v": {"value": 3.3, "source": "benchpod", "detail": "high level, ADC mean"},
        "period_s": {"value": 4.0e-8, "source": "benchpod", "detail": "LA, 2048 periods"},
        "pulse_width_s": {"value": 2.0e-8, "source": "benchpod"},
        "rise_s": {"value": 1.2e-9, "source": "datasheet"},
        "fall_s": {"value": 1.2e-9, "source": "datasheet"},
        "source_impedance_ohm": {"value": 40, "source": "assumed"},
    }
    body.update(over)
    return {"format": "emi-driver", "version": 1, "name": "U3 SPI clock", "net": "/SPI_SCK",
            "kind": "trapezoid", "role": "signal", "trapezoid": body}


WAVEFORM = {
    "format": "emi-driver", "version": 1, "name": "captured edge", "kind": "waveform",
    "role": "signal",
    "waveform": {
        "sample_interval_s": 1e-10,
        "samples_v": [0.0, 1.1, 3.3, 3.3, 3.3, 1.1, 0.0, 0.0, 0.0, 0.0, 0.0],
        "bandwidth_hz": 2.0e9,
        "period_s": {"value": 1.0e-9, "source": "benchpod"},
        "source_impedance_ohm": {"value": 50, "source": "assumed"},
        "rise_s": {"value": 2e-10, "source": "scope"},
    },
}

SPECTRUM = {
    "format": "emi-driver", "version": 1, "name": "analyser trace", "kind": "spectrum",
    "role": "signal",
    "spectrum": {
        "rbw_hz": 9000,
        "points": [{"frequency_hz": 25e6, "level_dbuv": 96.4},
                   {"frequency_hz": 75e6, "level_dbuv": 86.8},
                   {"frequency_hz": 125e6, "level_dbuv": 81.2}],
        "source_impedance_ohm": {"value": 50, "source": "assumed"},
    },
}

REGULATOR = {
    **trapezoid(), "name": "buck switch node", "role": "switching-regulator",
    "input_current_a": {"value": 0.42, "source": "benchpod"},
    "input_voltage_v": {"value": 12.0, "source": "datasheet"},
}

VALID = [
    ("trapezoid-spi-clock", trapezoid()),
    ("waveform-captured-edge", WAVEFORM),
    ("spectrum-analyser-trace", SPECTRUM),
    ("switching-regulator", REGULATOR),
    ("all-measured", trapezoid(
        rise_s={"value": 1.2e-9, "source": "scope"},
        fall_s={"value": 1.2e-9, "source": "scope"},
        source_impedance_ohm={"value": 40, "source": "scope"})),
]

INVALID = [
    ("wrong-format", {**trapezoid(), "format": "emi-cable"}, "format"),
    ("future-version", {**trapezoid(), "version": 2}, "version 2"),
    ("no-name", {**trapezoid(), "name": "  "}, "name"),
    ("unknown-kind", {**trapezoid(), "kind": "sawtooth"}, "kind"),
    ("unknown-role", {**trapezoid(), "role": "antenna"}, "role"),
    ("missing-field", {**trapezoid(), "trapezoid": {
        k: v for k, v in trapezoid()["trapezoid"].items() if k != "rise_s"}}, "rise_s"),
    ("no-source-on-a-number", trapezoid(amplitude_v={"value": 3.3}), "source"),
    ("unknown-source", trapezoid(amplitude_v={"value": 3.3, "source": "vibes"}), "source"),
    ("non-numeric-value", trapezoid(amplitude_v={"value": "3.3", "source": "scope"}), "number"),
    ("edges-do-not-fit", trapezoid(
        pulse_width_s={"value": 1e-9, "source": "benchpod"},
        rise_s={"value": 4e-9, "source": "scope"},
        fall_s={"value": 4e-9, "source": "scope"}), "do not fit"),
    ("pulse-longer-than-period", trapezoid(
        period_s={"value": 4e-9, "source": "benchpod"},
        pulse_width_s={"value": 3.5e-9, "source": "benchpod"},
        rise_s={"value": 2e-9, "source": "scope"},
        fall_s={"value": 2e-9, "source": "scope"}), "longer than the period"),
    ("negative-source-impedance", trapezoid(
        source_impedance_ohm={"value": -10, "source": "assumed"}), "negative"),
    ("waveform-shorter-than-a-period", {
        **WAVEFORM,
        "waveform": {**WAVEFORM["waveform"], "period_s": {"value": 5e-9, "source": "benchpod"}},
    }, "full period"),
    ("waveform-too-few-samples", {
        **WAVEFORM, "waveform": {**WAVEFORM["waveform"], "samples_v": [1.0]},
    }, "two samples"),
    ("waveform-bad-interval", {
        **WAVEFORM, "waveform": {**WAVEFORM["waveform"], "sample_interval_s": 0},
    }, "sample_interval_s"),
    ("spectrum-out-of-order", {
        **SPECTRUM,
        "spectrum": {**SPECTRUM["spectrum"], "points": [
            {"frequency_hz": 75e6, "level_dbuv": 86.8},
            {"frequency_hz": 25e6, "level_dbuv": 96.4}]},
    }, "increasing frequency"),
    ("regulator-without-supply-numbers", {**trapezoid(), "role": "switching-regulator"},
     "input_current_a"),
]


#: Frequencies each valid document is resolved at, chosen to exercise the ways a driver can
#: have nothing to say: between harmonics, above a capture bandwidth, outside an upload.
RESOLVE_AT = {
    "trapezoid-spi-clock": [25e6, 40e6, 50e6, 75e6, 125e6],
    "waveform-captured-edge": [1e9, 2e9, 3e9, 4e9],
    "spectrum-analyser-trace": [10e6, 25e6, 43.301270189221924e6, 75e6, 500e6],
    "switching-regulator": [25e6, 75e6],
    "all-measured": [25e6, 75e6],
}


#: A synthetic port spectrum to apply drivers against: Z_in is 100 ohm at every frequency,
#: so the expected offsets can be checked on paper.
PORT = {
    "frequencies_hz": [25e6, 40e6, 50e6, 75e6, 125e6, 1e9, 2e9, 3e9, 4e9],
    "v_real": [2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    "v_imag": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "i_real": [0.02, 0.02, 0.02, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01],
    "i_imag": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
}
REFERENCE_MAGNITUDE = 0.5


def main() -> int:
    valid = []
    for name, doc in VALID:
        d = parse(doc)
        entry = {
            "name": name,
            "document": doc,
            "expected": {
                "kind": d.kind, "role": d.role, "driver_name": d.name,
                "weakest_source": d.weakest_source(), "sigma_db": d.sigma_db(),
            },
        }
        if name in RESOLVE_AT:
            freqs = RESOLVE_AT[name]
            r = resolve(d, freqs)
            entry["resolve"] = {
                "frequencies_hz": freqs,
                "volts_abs": [None if v is None else abs(v) for v in r.volts],
                "undriven_hz": sorted(r.undriven),
                "magnitude_only": r.magnitude_only,
            }
            usable = [f for f in freqs if f in PORT["frequencies_hz"]]
            if usable:
                applied = apply_driver(
                    d, PortSpectrum.from_json(PORT), REFERENCE_MAGNITUDE, usable)
                entry["apply"] = {
                    "frequencies_hz": usable,
                    "offset_db": applied.offset_db,
                    "complete": applied.is_complete(),
                }
        valid.append(entry)

    invalid = []
    for name, doc, mention in INVALID:
        try:
            parse(doc)
        except DriverError as exc:
            message = str(exc)
        else:
            raise SystemExit(f"fixture {name!r} was supposed to be rejected and was not")
        if mention not in message:
            raise SystemExit(f"fixture {name!r}: {message!r} does not mention {mention!r}")
        invalid.append({"name": name, "document": doc, "must_mention": mention,
                        "python_message": message})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "_comment": (
            "Shared driver-document fixtures. The Python and TypeScript validators must "
            "agree on which documents are acceptable. Invalid cases carry `must_mention` "
            "rather than a full message: matching wording across two languages is brittle, "
            "but both must refuse and both must say which field and why. "
            "`python_message` is recorded for reference only. Regenerate with "
            "`make driver-fixtures`, never by hand."
        ),
        "port_spectrum": PORT,
        "reference_magnitude": REFERENCE_MAGNITUDE,
        "valid": valid,
        "invalid": invalid,
    }, indent=2) + "\n")
    print(f"wrote {OUT} ({len(valid)} valid, {len(invalid)} invalid)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
