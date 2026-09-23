# EMI Analyzer — how the model works, as built

This describes the tool that exists. It replaces `model-fidelity.md`, which was a design
document: where that argued for choices, this records what those choices became, and why the
code looks the way it does where the reason is not obvious from reading it. The parts of it that
were reference data rather than argument — the limits tables and the decisions log — are in
[`limits-and-decisions.md`](limits-and-decisions.md).

**What is still missing, wrong or unproven is not here.** It is in
[`known-issues.md`](known-issues.md), which is the companion to this file and the one to read
before relying on a number. Where this file says "the spikes measured", the number comes from an
early round of measurements made before the model was built (the scripts in `worker/research/`).

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
  edge, and a routed board has edges far closer together than any preset. The spikes measured the
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
the −20 dB edge. **This was a production bug the spikes found:** a transfer function at 1 GHz moved
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
- **Every harmonic amplitude is RMS**: `√2·|Cₙ|`, the one-sided peak `2|Cₙ|` divided by √2
  (`RMS_PER_PEAK`, the same constant in Python and TypeScript). An analyzer reads the RMS of a
  steady sine on every detector, the limits are written in those units, and an uploaded analyzer
  spectrum already is one. Until September 2026 trapezoids and waveforms carried the peak, which
  put every level they produced 3 dB high against both. The near-field offsets in dBµA/m are RMS
  for the same reason.
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

The cable meshed in the FDTD grid. The spikes established it is not a product mode: placing a radiating
resonance needs about λ/90, not the λ/20 a near-field map needs, and the cable region is most of
the domain. It exists to certify Tier B on fixtures, and that certification is incomplete.

---

## 6. The board's far field

`openems/nf2ff.py`, `openems/scan.py`, `stages/solve.py`. Six E and six H frequency-domain face
dumps; their tangential fields are equivalent surface currents, integrated at the scan's antenna
positions with the full free-space Green's function, plus their image in the ground plane.
[verification/far-field.md](verification/far-field.md) has the measurements behind each point.

- **The level is read where a scan reads it, not on a far-field sphere.** The antenna stands 3 m
  from the smallest circle around the copper and rises 1-4 m (0.1 m steps, 24 azimuths) above a
  plane 0.8 m under the board; the reading is the better of vertical and horizontal, as the cable
  path reads nec2c. Until September 2026 the level came from `nf2ff` on a 3 m sphere, 30-90° from
  the zenith, with its own PEC `Mirror`. That was wrong in two ways that dipoles over ground
  against nec2c measured: the sphere is not the scan (the far-field limit does not hold at 3 m
  below 100 MHz, nor for a source and its image 1.6 m apart), which read up to 7.7 dB high on a
  vertical dipole; and **`nf2ff`'s PEC mirror images horizontal currents wrongly**, which read a
  horizontal dipole 19 dB high at 30 MHz. A board's currents are all horizontal.
- **The image is of the currents.** A PEC plane reverses the horizontal part of J and the
  vertical part of M. The board itself is solved in free space, so the plane's effect back on the
  board's currents is left out; nec2c puts that at up to 11 % of a half-wave dipole's input
  impedance at 0.8 m, and under 1 % of an electrically short one's.
- **`nf2ff` is kept as a guard.** Without a mirror, in the far-field limit, it integrates the same
  dumps as `scan.field`; the two agree to 0.001 dB, and a far field where they differ by more
  than 0.5 dB is refused rather than published.
- **The box must be closed, and is checked.** openEMS sub-samples a dump by striding from the
  box's first grid line, so the old every-4th-line faces lost their last lines wherever the count
  did not fit the stride: on the fixture board every face stopped 14 mm short of its neighbours.
  Faces are now thinned with `OptResolution` (5 mm, or λ/20 at the top of the band if finer),
  which keeps both ends, and `scan.read_surface` refuses a box whose faces do not meet. On a
  short dipole 5 mm agrees with the full grid to 0.03 dB; the 4th-line stride read 0.35 dB high.
- **Both faults together made the fixture board's far field rise 20 dB/decade per volt** where an
  electrically small, capacitively fed structure must rise 40. Each alone breaks it: the closed
  box through the old mirror gives 25 dB/decade, the open box without a mirror 31. Fixed, the
  fixture reads 39.
- **The box sits a stated distance from the copper**: `max(25 mm, λ/10 at the top of the solved
  band)`, snapped outward to grid lines, with at least ten grid lines between each face and the
  edge of the grid so the PML_8 absorber stays clear. The grid grows by `clearance + 12 cells` on
  every side when the far field is on, and the solve's notes state what that costs (2.6x the cells
  on the fixture board). Below the frequency where the clearance is a tenth of a wavelength the
  box is electrically closer; on dipoles and a loop it measured right down to 30 MHz (0.003 λ).
- **The far-field grid covers the solved band only**: 60 log points from `max(30 MHz, lowest
  solved frequency)` to the solve's `f_max`. Frequencies asked for outside the band are dropped
  with a note.
- **`farfield.json` (format 3) is a transfer function per volt of source**, the maximum over the
  scan and per antenna height. It is divided by the solve's Thévenin source `V_port + I_port·Z_s`
  at each frequency, with the port's `Z_in` beside it so a driver can be attached later.
  **openEMS writes FD dumps single-sided**, `2·Σx·e^(−jωt)·Δt`; `OPENEMS_FD_SCALE = 2` brings the
  source onto that convention before dividing. Format 2 results are refused by the compliance
  estimate with a re-run message.
- **A port reading negative resistance means the record was cut short.** A passive port cannot
  have one; when `Re(Z_in) < -5 % |Z_in|` the run stopped on its end criterion while the board
  was still ringing, and the frequency is marked unusable and listed in `truncated_hz`. A real
  4-layer board at the default -40 dB lost 57 of 60 frequencies this way.
- **Radians and metres, checked**, for the `nf2ff` guard job: it echoes its input angles into
  `/Mesh/theta`, so a job written in degrees produces an output file that agrees with itself.
- **A missing face is refused.** Surface equivalence needs the surface closed.

---

## 7. Compliance

`compliance/` plus `stages/compliance.py`. Behind `full-wave`, labelled experimental, and
uncalibrated.

### 7.1 Where the inputs come from

The run names a solve and a driver; **everything that decides the answer is read by the worker**.
The server's run input (`server/emi/compliance.go`) carries the original upload, presigned URLs
for five JSON artifacts of the named solve, and the driver's document. The solve must be a
finished solve of the same project and the same board; anything else comes back as a message the
worker reports as a gap, and a run in another project reads exactly like a missing one.

`compliance/assemble.py` builds the paths:

```
board:  E(f) = e_per_volt(f) · |Z_s + Z_in(f)| / |Z_d + Z_in(f)| · |V_d(f)|
cable:  E(f) = |H_cm(f)| · |Z_s + Z_in(f)| / |Z_d + Z_in(f)| · |V_d(f)| / |Z_ant(f)| · E_per_amp(f)
```

`Z_s` is the port's own resistance (recorded in the manifest since September 2026), `Z_d` the
driver's source impedance. Everything in the structure is proportional to the port current, so a
driver that is not the solve's source changes every field by that ratio; leaving it out is exact
only when `Z_d = Z_s`. (The Cables tab's browser composition still leaves it out.)

**A clock is evaluated at its own harmonics**, not on the solve's grid. Composing on the 60-point
log grid drove a harmonic only where one landed within 100 ppm of a grid point: two of sixty for
a 25 MHz clock. The transfer functions are interpolated to every harmonic instead, magnitudes in
dB against log frequency and impedances by real and imaginary part, inside the band each one
covers and never beyond it. An uploaded analyzer spectrum is continuous and is read at the
transfer functions' own points inside its range.

Before this, the request carried the paths and every gate input, the browser sent none, and every
result was "nothing radiating"; an API client could send anything and get `complete: true`.

### 7.2 Combination

```
E_d(f) = Σ_paths |E_{d,path}(f)|     one driver's paths add in AMPLITUDE
E(f)   = sqrt( Σ_d E_d(f)² )         different drivers add in POWER
```

**Paths meet on a common grid**: every frequency any path has, inside the standard's scan,
merged to 100 ppm. A line spectrum is zero between its lines, which is the physics; a continuous
path is interpolated in dB inside its band. Outside a path's band the answer is "not covered",
recorded on the point, never zero. `Path.at` used to match by exact float, and the far-field and
cable grids share no point, so two paths of one driver were never added.

Amplitude for one driver is the worst case over a relative phase the model does not know. When a
driver contributes through more than one path, the per-path **shares are marked indicative**.

**Levels are RMS** (§4), and so are the limits for a steady harmonic: quasi-peak below 1 GHz,
average above it (§15.35), with the 20 dB higher peak limit recorded but never deciding a margin
for a steady source.

### 7.3 The budget

σ is root-sum-square over the terms that apply, **each path's terms weighted by its power
share**.

| Term | Applies to | σ (dB) |
|---|---|---|
| Cable idealisation | cable paths | 4.5 |
| Transfer-function interpolation | cable paths | 1 |
| Driver provenance | every path | 1 / 1.5 / 3 / 6 by source |
| Mesh preset | board path | fine 1 · normal 2 · coarse 4 (coarse when not recorded) |
| Permittivity (not confirmed) | board path | 1.5 (always: the solve does not record whether the stackup was assumed) |
| Far-field interpolation | board path | 1 |

These are **engineering placeholders**, conservative by choice, shown in the result, and
replaced by residuals as the verification tests produce them — not tuned. Component coverage is
not in the budget: the solve does not yet record how many parts on the path matched nothing.

### 7.4 Confidence

`Φ(margin / σ)`: the probability the true margin is positive *under the model's own budget*. Not
a pass probability. It exists in the payload, the run summary, the TypeScript types and the
fixtures **only** as `confidence_uncalibrated`; a plain `confidence` key used to travel beside it.
Evaluated at the worst frequency alone, which makes it **optimistic when several sit close** —
hence the near-miss list.

### 7.5 The completeness gate

An incomplete estimate carries **no** `margin_db` and no confidence key at all. The spectrum is
still drawn, greyed. Every gap names the tab that fixes it.

**The gate's inputs are derived, not sent**: the band each path covers (from the artifacts), the
scan it must cover (30 MHz to `scan_to_hz` of the driver's fundamental, §15.33(b)), the solve's
excited ports and which have a far field, the board's connectors (found in the upload, as the
cable run finds them), which cables the solve modelled, and whether power enters through a
connector. What the user may still say is what only they know, and each can only add a
requirement or record a decision the result names: a connector that carries no cable, the nets
that are sources, a higher frequency in the product, the enclosure and the power. The result's
`inputs` block lists all of it.

**Standards the estimate cannot reproduce are refused**, not scaled: the fields are computed at
3 m for a 1-4 m height scan, and a 10 m standard sweeps different elevation angles, so 1/r scaling
of the 3 m maximum is not the 10 m maximum; a conducted scan is a different quantity. Frequencies
outside the scan are not scored (asking for a limit there used to raise an uncaught error).

### 7.6 Recommendations

Gathered from rule findings, **never invented**. A finding qualifies by rule *and* (net *or*
proximity ≤ 25 mm). A board path uses the driver's net; a cable path uses the connector's pad
nets and position from the board. With nothing specific found, general guidance, labelled
general. The findings come with the request and only ever change this list.

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
whether the value is right. The spikes met four tools that completed successfully and computed nothing:

- a NEC deck with no execution card (no output, exit 0);
- a `DUMP_J_FREQ` map of an all-zero conduction current on PEC;
- an `nf2ff` job whose angles were read as radians — it echoed them back, so the output agreed
  with the bad input;
- an openEMS run diverging by twenty orders of magnitude, reporting `- 0.0 dB` throughout.

None produced an error and three produced plausible numbers. A test that only checks the process
exited zero passes on all four.
