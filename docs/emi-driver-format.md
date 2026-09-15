# The `emi-driver` format

**Version 1** · specification for producers, BenchPod included · 12 Sep 2026

A driver document says what a net actually carries. The EMI Analyzer solves a board with an
arbitrary excitation, which makes its results *relative*: one layout is 8 dB quieter than
another. Attaching a driver turns that into absolute units — dBµA/m on a near-field map,
dBµV/m at three metres — without re-solving anything, because openEMS solves a linear
structure and the response to any source inside the excitation band is already in the result.

This is the interchange format. It is what **Export JSON** writes, what the analyzer's upload
accepts, and what BenchPod should produce directly.

**Canonical implementations.** [`worker/emi_worker/drivers/document.py`](../worker/emi_worker/drivers/document.py)
and [`webapp/src/lib/driverDocument.ts`](../webapp/src/lib/driverDocument.ts), both checked
against [`server/emi/testdata/driver_document_fixtures.json`](../server/emi/testdata/driver_document_fixtures.json).
**Use the fixtures as the conformance suite**: 5 documents that must parse and 17 that must be
refused. A producer that agrees with them agrees with the analyzer.

---

## 1. The envelope

```json
{
  "format": "emi-driver",
  "version": 1,
  "name": "U3 SPI clock",
  "net": "/SPI_SCK",
  "kind": "trapezoid",
  "role": "signal",
  "captured_by": { "instrument": "BenchPod v2", "at": "2026-09-11T10:02:00Z" }
}
```

| Field | Required | Notes |
|---|---|---|
| `format` | yes | Always `emi-driver`. |
| `version` | yes | Integer. A reader refuses anything **newer** than it understands rather than reading the parts it recognises. |
| `name` | yes | Non-blank after trimming. |
| `kind` | yes | `trapezoid` · `waveform` · `spectrum`. |
| `role` | no | `signal` (default) or `switching-regulator`. |
| `net` | no | Net name as it appears in the board file. |
| `captured_by` | no | Free-form provenance for humans. |

A document is at most **2 MB**, and a `waveform` or `spectrum` carries at most **1,000,000**
points.

## 2. Every number carries its source

This is the part that distinguishes this format from a list of numbers. A driver value is
never a bare number:

```json
"rise_s": { "value": 1.2e-9, "source": "datasheet", "detail": "GPIO speed setting, 10 pF" }
```

`source` is one of, best first:

| `source` | Means | Provisional σ |
|---|---|---|
| `scope` | measured on an oscilloscope | 1.0 dB |
| `spectrum-analyzer` | measured on a spectrum analyser | 1.0 dB |
| `benchpod` | measured by a BenchPod | 1.5 dB |
| `datasheet` | taken from a part's datasheet | 3.0 dB |
| `assumed` | a reasonable guess | 6.0 dB |

**The weakest source in a document sets the driver's contribution to the uncertainty budget**
— worst, never an average. One guessed number among four measured ones still makes the driver
a guess, and averaging would hide exactly the case the budget exists to price. The σ values
are engineering placeholders that get replaced with residuals from recorded lab results; the
*ordering* is the part that is settled.

`detail` is optional free text and is shown to the user next to the value.

## 3. `kind: trapezoid`

```json
"trapezoid": {
  "amplitude_v":          { "value": 3.3,    "source": "benchpod", "detail": "high level, ADC mean" },
  "period_s":             { "value": 4.0e-8, "source": "benchpod", "detail": "LA, 2048 periods" },
  "pulse_width_s":        { "value": 2.0e-8, "source": "benchpod" },
  "rise_s":               { "value": 1.2e-9, "source": "datasheet" },
  "fall_s":               { "value": 1.2e-9, "source": "datasheet" },
  "source_impedance_ohm": { "value": 40,     "source": "assumed" }
}
```

All six are required.

- **`pulse_width_s` is the width at 50 % amplitude**, which is what an instrument reports and
  what the textbook closed form means by τ. The flat top is therefore
  `pulse_width_s − (rise_s + fall_s) / 2`.
- **`rise_s` and `fall_s` are 0–100 %.** They need not be equal; the analyzer transforms an
  asymmetric edge exactly rather than averaging the two.
- **Refused:** edges that do not fit inside the pulse (a negative flat top), and one pulse
  longer than the period.

## 4. `kind: waveform`

```json
"waveform": {
  "sample_interval_s": 1.0e-10,
  "samples_v": [0.0, 1.1, 3.3, "…"],
  "bandwidth_hz": 2.0e9,
  "period_s":             { "value": 1.0e-8, "source": "benchpod" },
  "source_impedance_ohm": { "value": 50,     "source": "assumed" },
  "rise_s":               { "value": 2.0e-10, "source": "scope" }
}
```

- **Samples must be evenly spaced.** The format carries one interval and cannot express
  anything else; a producer with uneven sampling must resample before writing.
- **The capture must cover at least one full period.** Transforming a fragment puts every
  harmonic in the wrong place while producing something that looks like a spectrum, so this
  is refused rather than accepted with a warning.
- **`bandwidth_hz` is where the samples stop meaning anything.** Above it the analyzer
  continues with the envelope implied by `rise_s`, scaled to meet the transform at the join.
  **With no `rise_s` declared, those harmonics are not driven at all** and the result is
  reported as incomplete there — which is honest, and is why declaring a rise time from a
  datasheet is worth doing even when the capture cannot see it.

## 5. `kind: spectrum`

```json
"spectrum": {
  "rbw_hz": 9000,
  "points": [
    { "frequency_hz": 25e6, "level_dbuv": 96.4 },
    { "frequency_hz": 75e6, "level_dbuv": 86.8 }
  ],
  "source_impedance_ohm": { "value": 50, "source": "assumed" }
}
```

Points must be in **strictly increasing** frequency order — out-of-order points usually mean
the wrong column was read, so they are refused rather than sorted. Levels are interpolated in
dB against log frequency. Outside the given range the driver drives nothing, and says so.

An uploaded spectrum carries **no phase**, and the analyzer records that: downstream
combination is worst-case phase anyway, but the result has to say which it is.

## 6. `role: switching-regulator`

Adds two top-level sourced values, which the conducted prediction reads:

```json
"input_current_a": { "value": 0.42, "source": "benchpod" },
"input_voltage_v": { "value": 12.0, "source": "datasheet" }
```

Both are required when the role is set. A BenchPod can measure supply current directly.

## 7. What a BenchPod can honestly fill in

The format has to be straight about the instrument's reach, because a number recorded with
the wrong `source` is worse than a missing one.

| Quantity | From | Realistic |
|---|---|---|
| `period_s`, `pulse_width_s` | logic analyzer, ~12 MS/s | **Yes.** Sets the first envelope corner, `1/(πτ)`. |
| `amplitude_v` | ADC | **Yes.** |
| `input_current_a`, `input_voltage_v` | ADC | **Yes.** |
| `rise_s`, `fall_s` | — | **No** below about 100 ns: outside the capture bandwidth. |

**The rise time is the one that matters most and the one the pod cannot see.** It sets the
second corner, `1/(π·t_r)`, and therefore every harmonic above a few hundred megahertz —
which is most of what a radiated scan cares about. A pod-written document should mark it
`datasheet` when it comes from a part's specification, or `assumed` when it is a guess, and
never `benchpod`.

A pod-written `waveform` should set `bandwidth_hz` to the real capture bandwidth. Claiming
more is how a spectrum acquires detail nobody measured.

## 8. Conformance

Run a producer against
[`driver_document_fixtures.json`](../server/emi/testdata/driver_document_fixtures.json):

- every document under `valid` must be accepted, and `weakest_source` must match;
- every document under `invalid` must be refused, and the message should mention the field
  named in `must_mention`.

The fixtures are generated, never hand-edited —
`worker/scripts/gen_driver_doc_fixtures.py`, via `make driver-fixtures`.
