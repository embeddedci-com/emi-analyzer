# Limits library, and the decisions behind the model

Two things from the original design document that are reference data rather than design
argument, and so outlive it:

- **the limits library** — what a standard's table looks like as the tool stores it, and where
  each number came from;
- **the decisions log** — what was settled, by whom, and what is still open.

How the model works is in [`implementation.md`](implementation.md); what is missing is in
[`known-issues.md`](known-issues.md).

---

## 15. The limits library

### 15.1 A table, as published

```json
{
  "format": "emi-limits", "version": 1,
  "id": "fcc-15-109-class-b",
  "title": "FCC Part 15 Class B — radiated",
  "document": "47 CFR §15.109(a)",
  "revision": "80 FR 33447, 12 Jun 2015",
  "port": "radiated",
  "distance_m": 3,
  "detector": "quasi-peak",
  "band_edges": "tighter",
  "segments": [
    {"f_hz": [30e6,  88e6],  "limit_uv_m": 100},
    {"f_hz": [88e6,  216e6], "limit_uv_m": 150},
    {"f_hz": [216e6, 960e6], "limit_uv_m": 200},
    {"f_hz": [960e6, null],  "limit_uv_m": 500}
  ],
  "scan_range": "47 CFR §15.33(b)",
  "url": "https://www.ecfr.gov/current/title-47/part-15/section-15.109",
  "verified": "2026-09-11"
}
```

- **`band_edges: tighter`** encodes §15.109's own rule: *"the tighter limit applies at the band
  edges"*. At exactly 88 MHz the limit is 100 µV/m.
- **Conducted segments interpolate.** §15.107 Class B falls from 66 to 56 dBµV quasi-peak, and 56
  to 46 average, over 0.15–0.5 MHz, *"with the logarithm of the frequency"*. Those segments carry
  `"interp": "log-f"`.
- **Detectors are separate fields** (`quasi-peak`, `average`, `peak`). Above 1 GHz, §15.35 sets
  average limits with a peak limit above them, cited by clause when that table is built.

### 15.2 Starting set

| Table | Values (verified 11 Sep 2026 against the CFR text) |
|---|---|
| §15.109(a) Class B radiated, 3 m | 30–88 MHz 100 µV/m (40.0 dBµV/m) · 88–216 150 (43.5) · 216–960 200 (46.0) · above 960 500 (54.0) |
| §15.109(b) Class A radiated, 10 m | 30–88 MHz 90 µV/m (39.1) · 88–216 150 (43.5) · 216–960 210 (46.4) · above 960 300 (49.5) |
| §15.107(a) Class B conducted | 0.15–0.5 MHz 66→56 QP / 56→46 avg · 0.5–5 MHz 56 / 46 · 5–30 MHz 60 / 50 dBµV |
| §15.107(b) Class A conducted | 0.15–0.5 MHz 79 QP / 66 avg · 0.5–30 MHz 73 / 60 dBµV |
| §15.33(b) scan range | highest frequency < 1.705 MHz → 30 MHz · 1.705–108 → 1 GHz · 108–500 → 2 GHz · 500–1000 → 5 GHz · above → 5th harmonic or 40 GHz |

- **CISPR 32 / EN 55032** (Class A and B, radiated at 10 m and 3 m, conducted).
  - **Not freely published.** IEC and the national standards bodies sell it, unlike the FCC
    rules, which are US law and free online.
  - **Its limit values are facts and widely reproduced.** The table ships from at least two
    independent public sources that agree, both cited, with `"verified": "secondary sources"`.
    **This is the decision, not a placeholder**: the tables ship this way.
  - **Upgrade path.** The label becomes `"standard text"` if a copy is ever bought.
  - **The one thing to avoid** is a limit typed from memory.
- **Intentional radiators** (Part 15 Subpart C: Wi-Fi, BLE, the ESP32-C3 on BenchPod) are out of
  scope. The digital circuitry around them is not.

### 15.3 Reading the guidelines

A **Limits** page (`/tools/emi/limits`), prerendered like the limitations page, shows:

- each table as published;
- the unit conversions;
- the detector and distance;
- the scan-range rule;
- a link to the official text, with the revision and the date it was verified.

For CISPR, it shows the limit values and clause numbers, and does not reproduce the standard's
text.

---

## 22. Decisions

### Made

See the table at the top:

- hybrid cables, with the user's choice of antenna solver;
- defaults as chosen here;
- drivers saved with the project;
- components saved in a library (in the local app, on this computer);
- drivers → capacitors → cables, then compliance;
- board data never leaves the machine that stores it;
- the conducted common-mode term included;
- relaxed wording, with a clear "not a full pre-compliance test";
- **CISPR 32 / EN 55032 limits come from public secondary sources,** cross-checked between at
  least two that agree and labelled `"verified": "secondary sources"` (§15.2). Buying a copy is
  not a prerequisite for shipping the tables; the label becomes `"standard text"` if one is ever
  bought, and no limit is ever typed from memory.

### To confirm

*(nothing open)*

---

## Sources

- 47 CFR §15.107, §15.109 (as amended 80 FR 33447, 12 Jun 2015), §15.33 — read 11 Sep 2026 via the
  LII mirror of the CFR (law.cornell.edu/cfr/text/47/15.109, …/15.107, …/15.33).
- Debian bookworm `nec2c` 1.3-4; PyNEC / necpp, GPL-2.0.
- T. Hockanson et al., "Investigation of fundamental EMI source mechanisms driving common-mode
  radiation from printed circuit boards with attached cables", IEEE Trans. EMC 38(4), 1996.
- C. R. Paul, *Introduction to Electromagnetic Compatibility*, 2nd ed. — common-mode radiation from
  short cables; spectra of trapezoidal waveforms.
- CISPR 16-1-2, CISPR 32, ANSI C63.4 — sold by IEC and the standards bodies. Values are cited from
  public secondary sources until a copy is at hand.
- 47 CFR §15.35 — to be read and cited before the above-1 GHz table ships.
- IBIS specification — `[Package]` and `[Pin]` parasitics.
