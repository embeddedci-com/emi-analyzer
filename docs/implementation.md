# EMI Analyzer — how the model works, as built

This describes the tool that exists. It replaces `model-fidelity.md`, which was a design
document: where that argued for choices, this records what those choices became, and why the
code looks the way it does where the reason is not obvious from reading it. The parts of it that
were reference data rather than argument — the limits tables and the decisions log — are in
[`limits-and-decisions.md`](limits-and-decisions.md).

**What is still missing, wrong or unproven is not here.** It is in
[`known-issues.md`](known-issues.md), which is the companion to this file and the one to read
before relying on a number. Where this file says "M0 measured", the number comes from an early
round of spike measurements made before the model was built.

---

## 1. The shape of a run

Everything is a **run**: a row in `emi_runs`, a set of params, a worker that claims it, and a
set of artifacts in object storage. There are five kinds, and the split is about cost rather
than about topic.

| Kind | Cost | Needs | Produces |
|---|---|---|---|
| `ingest` | seconds | nothing | `board.json`, `geometry.bin`, rule findings |
| `solve` | hours | openEMS, real cores | `manifest.json`, field maps, `ports.json`, optionally `cable_ports.json`, `cable_antenna.json`, `farfield.json` |
| `transient` | seconds | ngspice | ESD waveforms |
| `cable` | ~½ second | nec2c | `cables.json` — the Tier A current budget |
| `compliance` | seconds | nothing | `compliance.json` — margin, confidence, contributions, recommendations |

**Why `compliance` is a run kind and not a stage of `solve`.** It is arithmetic on artifacts
other runs already produced. A user changing a cable length or swapping a driver wants the
answer immediately, and none of that needs fields recomputed — so every worker advertises it,
including the ingest-only one.

**Why a solve is relative by default.** openEMS solves a linear structure, so a Gaussian
excitation already contains the response to every source inside its band. Attaching a driver
afterwards is arithmetic on the recorded port spectra, not another run; the early
measurements found the re-weighting exact to 0.01 dB. That is why drivers are chosen *after* a run
and cable ports *before* one: a gap port changes the mesh, a driver does not.

---

## 2. Layout in → model

`kicad/` parses `.kicad_pcb` (and `gerber/` reads Gerber + IPC-D-356 where there is no KiCad
file). `openems/model.py` turns a board plus a `SolveParams` into a CSX document and a mesh.

### 2.1 The mesh, and the two things about it that surprise people

`openems/mesh.py` builds each axis from required lines — copper edges, layer heights, port
boundaries — then fills, grades and pads for the PML.

- **`dx_um` is a floor on cell size, not the spacing.** The mesher puts a line at every copper
  edge, and a routed board has edges far closer together than any preset. M0 measured the
  consequence: the in-plane axes come out 2.7–4.0× *denser* than a uniform grid over the same
  box, not sparser. The cost estimator's "fill factor" is therefore greater than one — 3.55 to
  8.75 at the coarse preset — and the UI passes the per-preset **minimum**, because the panel
  says "At least".
- **`merge_close` collapses lines closer than `dx_um / 4`**, which makes the smallest in-plane
  cell exactly `dx_um / 4`. Since the timestep is set by the smallest cell anywhere, the
  in-plane preset sets the timestep — *unless* `dz` is fine enough to win, which on a real
  stackup it usually is.

### 2.2 Excitation band

`excitation_band()` centres the Gaussian so requested frequencies sit inside it rather than on
the −20 dB edge. **This was a production bug M0 found:** a transfer function at 1 GHz moved
3–4 dB between two sources that agreed to 0.01 dB everywhere inside the band. `BAND_FILL = 0.8`
leaves about 7 dB of source amplitude at each requested end.

### 2.3 Run length

`required_timesteps()` takes the larger of three excitation lengths and three periods of the
lowest frequency. The second term is what makes radiated work expensive: 3 / 30 MHz is 100 ns
of record whatever the board is.

### 2.4 Instability is detected, not assumed away

**openEMS never tells you it diverged.** It prints energy in dB relative to its *running*
maximum, so a run whose fields are growing reports `- 0.0 dB` forever, exits zero and writes a
full set of results. `run.divergence_ratio` takes the largest rise from a running minimum,
counted only once the series has started to fall, and `run_openems` raises above 1000×. It has
already caught runs that would otherwise have published numbers.

Both halves of that definition were necessary: counting from the first sample calls every run
unstable, because the rise into the excitation peak is enormous by construction; comparing
against the global peak finds nothing, because a diverging run's largest energy is its last.

---

## 3. Components

`components/` matches footprints against a library, resolves a series R-L-C, and places it.

- **`mlcc_family`** rather than enumerated parts. ESL is a property of the package; ESR is
  interpolated log-log over C. This took library coverage on the four real boards from 46 % to
  99 %.
- **Three single-value elements in adjacent cells, not one R-L-C.** CSXCAD 0.6.2 has no
  `LEtype`, so a lumped element's R, C and L are in **parallel**. A series model has to be built
  from three cells.
- **Off by default.** With `model_components` unset a solve is bit-for-bit what it was before
  component models existed.
- Every run carries **resolved copies** of the components it used, so editing one later never
  changes a result already reported.

---

## 4. Drivers

`drivers/` turns a declared or measured source into a spectrum, and `driverApply` re-weights a
finished solve with it.

- **A trapezoid is four breakpoints of a piecewise-linear waveform.** The textbook sinc×sinc
  falls out of the general case, asymmetric edges are exact rather than approximated, and an
  uploaded waveform reuses the same code.
- **A square wave's harmonics are `2A/(nπ)`, not `4A/(nπ)`.** The textbook figure is for a ±A
  swing; a driver swings 0→A. Getting this wrong would have made every absolute level 6 dB high.
- **Provenance is carried, never averaged.** `SOURCE_SIGMA_DB` runs scope 1.0, spectrum analyzer
  1.0, BenchPod 1.5, datasheet 3.0, assumed 6.0, and `weakest_source()` takes the worst value in
  a document rather than a blend.
- **A null is not a small number.** Below `NULL_FLOOR` (1e-9 of the driver's own scale) a
  harmonic is reported as undriven, because the transform's rounding residue there is ~1e-16 and
  its logarithm is a plausible-looking level.

---

## 5. Cables

### 5.1 Tier A — the budget (`cable` run)

`cables/budget.py` asks nec2c how much common-mode current this cable may carry before it
reaches the limit, at 48 log-spaced frequencies. **The closed form is not the engine:** it is
valid below λ/10, which for a 1 m cable is 30 MHz — the bottom of the radiated range — so a
closed-form budget would be an extrapolation everywhere it is used. It survives as a sanity
bound only.

**The geometry.** The cable lies straight along +x, 0.8 m above a perfect ground plane (the
tabletop height of ANSI C63.4 and CISPR 16-2-3, and the same height the far field uses). The
board is a 0.1 m wire on the other side of the feed. The receiving antenna sweeps a ring 3 m
outside the smallest circle around board and cable, as a turntable scan does, at 1-4 m. Cables
are limited to 10 m and segmented at λ/20 up to 800 segments, so the longest is still finer
than λ/10 at 1.2 GHz. Before September 2026 the ring was centred on the feed with a 3 m radius
and the table was 1 m high: the antenna sat 1 m from the end of a 2 m cable and on the wire of a
3 m one. The closed-form comparison below was measured with that geometry and has not been
repeated.

**How far the closed form is from the solver, measured.** Inside the window where both are
entitled to an opinion — the wire electrically short *and* the observation point in the far
field, which for a 1 m cable at 3 m is 16–30 MHz — the closed form sits **3.5–4 dB above** a
solved fed wire, with a constant offset across the band. That gap is the current distribution: a
fed wire tapers towards its open end, and a perfectly triangular taper would be 6 dB. The
direction is the useful one — the closed form predicts more field per amp, so the budget it
computes is tighter. Below the far-field boundary the two diverge without limit (25 dB at 5 MHz),
because 1/r² and 1/r³ terms dominate there and no far-field formula contains them.

Radiation peaks are marked by comparing each point against a **window** — ±25 % of its own
frequency, capped at ⅓ of `c/2L`. Both bounds were measured into existence: neighbour-based
detection made a *finer* grid find *fewer* peaks, and a purely fractional window let the top
peak wander between 632 and 796 MHz.

### 5.2 Tier B — what this layout drives (`solve` + `compliance`)

A 10 mm PEC stub leaves the board along the connector's exit normal; a one-cell gap with a 1 MΩ
element across it measures the open-circuit voltage. Then

```
H_cm(f) = V_oc / V_src          from the solve, dimensionless
I_cm(f) = H_cm · V_src / Z_ant  V_src from the driver, Z_ant from nec2c
E(f)    = I_cm · E_per_amp
```

- **`V_src` is the solve's Thévenin source**, `V_port + I_port·Z_s` — not the port voltage,
  which would fold the port's own input impedance into a number meant to be independent of it.
- **The grid is extended only on the sides a stub leaves from.** Growing all four costs +46 %
  cells on one real board; per-side is +0.6 %.
- **The dielectric is clipped to the outline's bounding box**, or the stub would sit on FR-4
  instead of in air. Exact for a rectangular board, which all four fixtures are.
- **An edge connector is one whose nearest *pad* is within 12 mm of the outline**, not its
  centroid: an RJ45 on one real board is 14 mm out by centroid and 2 mm by pad.
- **A gap port is refused, never approximated** — for a mid-board connector, a diagonal exit, or
  one outside the region. Each says which.
- **The antenna terms are computed beside the solve**, not in the browser, so a driver can still
  be attached afterwards: the result carries everything except the driver's own voltage.

**This path is marked experimental, and is off unless `full-wave` is enabled.** See [`known-issues.md`](known-issues.md).

### 5.3 Tier C — the reference

The cable meshed in the FDTD grid. M0 established it is not a product mode: placing a radiating
resonance needs about λ/90, not the λ/20 a near-field map needs, and the cable region is most of
the domain. It exists to certify Tier B on fixtures, and that certification is incomplete.

---

## 6. The board's far field

`openems/nf2ff.py`. Six E and six H frequency-domain face dumps, then the shipped `nf2ff` CLI.

- **A PEC `Mirror` at table height puts the ground reflection inside the transform.** M0
  measured it against image theory at 0.08 dB median, which retired the hand-computed two-ray
  sum and the flat +6 dB fallback the design originally planned. When the mirror is present the
  lower face is dropped — the image *is* the lower hemisphere, and keeping both counts twice.
- **`Radius` is where E is evaluated, and E really does scale as 1/r** — measured at 1, 3 and
  10 m as 1.037e-11, 3.456e-12, 1.037e-12 V/m. So the job asks for the standard's own distance
  and the artifact is directly comparable with a limit. A 1 m field against a 3 m limit is
  9.5 dB optimistic and nothing in the output would say so.
- **Faces are sub-sampled 4:1.** The surface must resolve the *wavelength*, not the copper:
  300 mm at 1 GHz against 200 µm for a quarter of a 50 µm mesh. M0 sized the artifact at
  1.1–4.0 GB full and 0.07–0.25 GB sub-sampled.
- **Radians and metres, checked.** `nf2ff` echoes its input angles into `/Mesh/theta`, so a job
  written in degrees produces an output file that agrees with itself and confirms nothing. The
  reader asserts the echo matches what was sent.
- **A missing face is refused.** Surface equivalence needs the surface closed; five faces is not
  a smaller answer.
- **The box is placed from the finished mesh**, at 0.8 of the way to each boundary — so it
  follows a cable port's one-sided extension instead of sitting in the reactive near field on one
  side and outside the grid on the other. M0's first attempt put the faces 0.064 λ out and got a
  pattern oscillating on a 7° scale, which no box that size can produce.

---

## 7. Compliance

`compliance/` plus `stages/compliance.py`.

### 7.1 Combination

```
E_d(f) = Σ_paths |E_{d,path}(f)|     one driver's paths add in AMPLITUDE
E(f)   = sqrt( Σ_d E_d(f)² )         different drivers add in POWER
```

Amplitude for one driver is the worst case over a relative phase the model does not know. Two
equal paths are 6 dB up this way and 3 dB in power — getting it backwards is optimistic exactly
where two paths matter. When a driver does contribute through more than one path, the per-path
**shares are marked indicative**, because an amplitude sum is not a power sum and the parts stop
adding to the whole.

### 7.2 The budget

σ is root-sum-square over the terms that apply, **each path's terms weighted by its power
share** — a cable contributing 1 % of the power brings 0.45 dB of its 4.5, not the whole thing,
so the confidence figure describes the prediction actually on screen.

| Term | Applies to | σ (dB) |
|---|---|---|
| Cable idealisation | radiated, cable paths | 4.5 |
| Driver provenance | the field that sets the level | 1 / 1.5 / 3 / 6 by source |
| Mesh preset | radiated, board path | fine 1 · normal 2 · coarse 4 |
| Assumed permittivity | radiated, board path | 1.5 |
| Far-field interpolation | radiated, board path | 1 |
| Component coverage | both | 3 × unmatched fraction |

These are **engineering placeholders**, conservative by choice, shown in the result, and
replaced by residuals as the verification tests produce them — not tuned.

### 7.3 Confidence

`Φ(margin / σ)`: the probability the true margin is positive *under the model's own budget*. Not
a pass probability. Carried in the payload as `confidence_uncalibrated` as well as `confidence`,
so no client can present it as one by accident. Evaluated at the worst frequency alone, which
makes it **optimistic when several sit close** — hence the near-miss list, every frequency within
one σ of the worst.

### 7.4 The completeness gate

An incomplete estimate carries **no** `margin_db` and no `confidence` key at all — not a margin
with a warning attached, because a warning is what gets dropped when someone screenshots the
number. The spectrum is still drawn, greyed. Every gap names the tab that fixes it, so the list
doubles as the to-do list.

A board with three connectors where one has no declared cable produces a perfectly reasonable
margin **about a different product**, and the arithmetic cannot tell the difference. That is the
failure this prevents.

### 7.5 Recommendations

Gathered from rule findings, **never invented**. A finding qualifies by rule *and* (net *or*
proximity ≤ 25 mm): rule alone pulls in every plane gap on the board, net alone misses a
stitching finding that has no net but sits 2 mm from the connector exit. With nothing specific
found, general guidance for that path type, labelled general.

---

## 8. Marking a feature experimental

Not everything passes its gate before it is useful. `<Experimental why={...} />` renders a badge
whose tooltip is the explanation, and **`why` is required**: a bare "experimental" warns without
saying what about, which is the wrong hedge for a tool whose claim is that numbers arrive with an
account of how far to trust them.

The reason is written for someone deciding whether to act on the number — it names the
measurement that would settle the question and says which way the error runs when that is known.
Reasons live together in `EXPERIMENTAL` so the set of things the tool is unsure about can be read
in one go.

**This is not an uncertainty figure.** σ says how far a number might be off *given that the model
is right*; experimental says the model has not been checked against the case in front of you. A
feature can be experimental and precise, or validated and vague.

---

## 9. Shared contracts

Anything computed in more than one language is pinned by a fixture in `server/emi/testdata/`,
asserted by every implementation:

| Fixture | Asserted by |
|---|---|
| `estimate_fixtures.json` | Python, Go |
| `driver_fixtures.json`, `driver_document_fixtures.json` | Python, TypeScript |
| `component_fixtures.json` | Python, TypeScript |
| `cable_emission_fixtures.json` | Python, TypeScript |
| `compliance_fixtures.json` | Python, TypeScript |

The rule that produced them: a user's live estimate in the browser disagreeing with what the
worker does means a run is rejected at the mesh stage after they already committed to it.

---

## 10. Verification, and the failure mode it is built around

**Every solver step asserts twice** — that what came back is what was asked for, separately from
whether the value is right. M0 met four tools that completed successfully and computed nothing:

- a NEC deck with no execution card (no output, exit 0);
- a `DUMP_J_FREQ` map of an all-zero conduction current on PEC;
- an `nf2ff` job whose angles were read as radians — it echoed them back, so the output agreed
  with the bad input;
- an openEMS run diverging by twenty orders of magnitude, reporting `- 0.0 dB` throughout.

None produced an error and three produced plausible numbers. A test that only checks the process
exited zero passes on all four.
