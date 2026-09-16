# ESD and surge transient simulation

**Status: phase 1 and vendor-model upload implemented — §11 records where the build differs
from this plan.** Written before building, the way
[length-matching-and-impedance.md](length-matching-and-impedance.md) was. It answers one
question: can the analyzer say *how much* of a discharge reaches an IC pin, instead of only that
a clamp is missing or far away?

The short answer is yes, for the part that decides whether a board survives, by running ngspice
on the lines the `esd-protection` check already finds. The layout becomes transmission lines
using the stackup, impedance and topology code that already exists. The source reproduces the
standard's waveform, and parts get generic models built from datasheet numbers. The output is a
comparison — as laid out, and with the clamp moved to the connector — because the absolute volts
are an estimate and the difference between two layouts is not.

---

## 1. What exists today

| Need | Today | Good enough? |
|---|---|---|
| Which lines are exposed | `esd-protection` ([rules/emc.py](../worker/emi_worker/rules/emc.py)) finds edge connectors, the nets they carry off the board, clamps on those nets, and the first IC on each | **yes** — the simulation starts from exactly this set |
| Routed pad-to-pad length | `NetTopology.path(a, b)` gives per-layer runs and a via count | **mostly** — runs are summed per layer, not kept in physical order (§3.3) |
| Where the clamp branches off | not stored | derivable from pairwise path lengths (§3.2) |
| Delay per mm, per layer | `BoardElectrics.ps_per_mm(layer)` | yes |
| Trace impedance | `impedance.single_ended(width_mm, LayerElectrics)` → `Impedance.ohm` ± uncertainty | yes (closed-form microstrip/stripline) |
| Via inductance | nothing; only `via_ps` as a delay | **no** — add a closed form (§3.4) |
| Clamp ground path | `esd-protection` measures pad edge to the nearest ground via | yes as a length; needs converting to inductance |
| Part electrical models | none — parts are *recognised*, never *characterised* | **no** — §4 |
| Circuit simulator | none; the worker image has openEMS only | **no** — add ngspice (Debian bookworm ships 39.3, BSD-3-Clause) |
| Run kinds | server accepted `ingest` and `solve` when this was written (`server/emi/types.go`); workers advertise `kinds` (`worker/emi_worker/config.py`) | needed a third kind (§6); there are five now |

---

## 2. The standards' numbers

These are what the sources must reproduce, and what the tests in §7 pin.

### 2.1 ESD — IEC 61000-4-2:2008, contact discharge

From the standard's Tables 1 and 3:

| Level | Contact kV | First peak (±15 %) | Rise 10–90 % | At 30 ns (±30 %) | At 60 ns (±30 %) |
|---|---|---|---|---|---|
| 1 | 2 | 7.5 A | 0.8 ns ±25 % | 4 A | 2 A |
| 2 | 4 | 15 A | 0.8 ns ±25 % | 8 A | 4 A |
| 3 | 6 | 22.5 A | 0.8 ns ±25 % | 12 A | 6 A |
| 4 | 8 | 30 A | 0.8 ns ±25 % | 16 A | 8 A |

The 30 ns and 60 ns points are timed from the instant the current first reaches 10 % of its
first peak.

The standard gives an equation for the ideal waveform (with its Figure 2), which is what the
source implements:

```
I(t) = (I1/k1) · (t/τ1)^n / (1 + (t/τ1)^n) · exp(−t/τ2)
     + (I2/k2) · (t/τ3)^n / (1 + (t/τ3)^n) · exp(−t/τ4)

k1 = exp(−(τ1/τ2) · (n·τ2/τ1)^(1/n))
k2 = exp(−(τ3/τ4) · (n·τ4/τ3)^(1/n))

τ1 = 1.1 ns, τ2 = 2 ns, τ3 = 12 ns, τ4 = 37 ns, n = 1.8
I1 = 16.6 A, I2 = 9.3 A at 4 kV — scaled linearly with the level
```

Three things in the standard shape what we simulate:

- **It specifies the waveform, not the generator.** The simplified generator is 150 pF through
  330 Ω, but those are typical values and only the waveform is normative. So the source is the
  waveform (§5).
- **Air discharge has no specified waveform.** Levels are 2, 4, 8 and 15 kV, and the arc makes
  every discharge different. It is not simulated.
- **Where discharges land (clause 8.3.2).** Contact discharges go to the metallic shell of a
  connector. The pins of an insulated connector get air discharges, and only when the product
  standard asks for them. So for a metal-shell connector, injecting at a pin is a worst case,
  not the test.

**Edition caveat:** edition 3.0 was published in March 2025, with an improved current
calibration procedure. Before a result is shown as "per IEC 61000-4-2", confirm that the ideal
waveform is unchanged. The tests pin the 2008 values.

### 2.2 Surge — IEC 61000-4-5:2014 (phase 3)

- **Levels:** line-to-line 0.5, 1 and 2 kV (levels 2–4); line-to-ground 0.5, 1, 2 and 4 kV
  (levels 1–4).
- **Combination wave generator:**
  - open-circuit voltage 1.2/50 µs (front ±30 %, duration ±20 %);
  - short-circuit current 8/20 µs (±20 %, ±20 %);
  - effective output impedance 2 Ω, so 0.5 kV gives 250 A and 4 kV gives 2 kA.
- **Coupling into AC/DC power ports:**
  - line-to-line: 18 µF;
  - line-to-ground: 9 µF + 10 Ω, which gives a short-circuit peak of 41.7 A at 0.5 kV up to
    333.3 A at 4 kV.
- **Generator element values are not specified,** only the waveforms. A generator circuit must
  be fitted until both waveforms and Tables 2, 3 and 6 are met, and the tests check that.
- **Out of scope:** the 10/700 µs generator for outdoor symmetrical telecom lines.

### 2.3 Burst — IEC 61000-4-4 (phase 2)

- **Pulse:** 5 ns rise and 50 ns width, into 50 Ω, from a 50 Ω generator.
- **Bursts:** 15 ms long at 5 kHz (or at 100 kHz), repeated every 300 ms.

**Not yet checked against the standard's text** — these come from test-equipment vendors.
Confirm them before implementing, including the capacitive coupling clamp used on signal
lines. The repetition does not change the peak, so a single pulse is what gets simulated.

---

## 3. From layout to circuit

### 3.1 What gets simulated

Each line the `esd-protection` check reports has:
- a connector pin **C**;
- the clamp pad **D**, if there is one;
- the first IC pin **U**.

```
 source ──► C ──[ trunk T-line ]──► B ──[ stub T-line ]──► D ─ clamp ─ L_gnd ─ L_via ─┐
 (contact            (TD, Z0)       │                                                 │
  waveform)                         └──[ stub T-line ]──► U ─ IC pin model ───────────┤
                                                                                      ⏚ plane (ideal)
```

For an R-clamp (a series resistor, then the clamp — the check already recognises this), the
circuit is T-line, then R, then T-line, split at the resistor's pad positions.

### 3.2 Finding the branch point

Topology stores pairwise paths, not the tree. On a tree, the distance from C to the point **B**
where the paths to D and U part is:

```
t = ( L(C,D) + L(C,U) − L(D,U) ) / 2
stub to clamp = L(C,D) − t
stub to IC    = L(C,U) − t
```

`topology.build` already computes all three pairs. If they do not form a tree — t or a stub
negative beyond a tolerance, as happens with a loop through a pour — fall back to straight-line
placement, and say so in the result.

### 3.3 Transmission lines, not lumped inductors

- **Why lines.** A 0.8 ns rise has a knee around 0.35/tr ≈ 440 MHz, with content above that. At
  about 6 ps/mm, a 40 mm trunk is 250 ps — a third of the rise. That is long enough for a lumped
  inductor to be wrong at the peak.
- **ngspice's lossless `T` element** takes delay and impedance directly:
  - **TD** = Σ(run length × `ps_per_mm(layer)`) + vias × `via_ps`;
  - **Z0** = `impedance.single_ended(width, layer).ohm`, using the dominant width on the run.
- **The report quotes per-mm values** derived from those: L′ = Z0 · t_pd and C′ = t_pd / Z0.

**Gap:** `topology._trace` sums length per layer, in first-seen order, walking back from the far
pad. Physical order is lost.
- **Phase 1:** each layer's run becomes one T section. Series order does not change the total
  delay, only the reflections at impedance steps.
- **Phase 2:** keep ordered runs, with widths, in `_trace`. That is a small change.

### 3.4 Vias and the clamp's ground

- **Via inductance.** Use the common closed form L ≈ 0.2 · h · (ln(4h/d) + 1) nH, with h (the
  via length to the plane) and d (the drill) in mm. Treat it as ±30 %.
- **Clamp ground.** This is the pad-edge-to-via length `esd-protection` already measures (as a
  short T-line), plus that via's inductance down to the plane. It is the step that turns "no
  ground via within 2 mm" into volts.
- **Plane.** Ideal. No ground bounce across the board (§8).

### 3.5 What-ifs

Every line is simulated more than once.

**A protected line:**
1. As laid out.
2. Clamp at the connector: trunk to branch 0 mm, clamp stub 1 mm, same clamp and same ground.
3. Clamp ground ideal: a via at the pad.

**An unprotected line:** as laid out, against a reference generic 5 V TVS at the connector. The
"no ESD protection" finding then gets a number too.

The comparison is the product. Absolute values are estimates (§8).

---

## 4. Part models

### 4.1 Clamps: generic, from datasheet numbers

Vendor SPICE models vary in quality and in licence, and shipping them in the image means
checking each vendor's terms. So the default is a generic model built from numbers every TVS
datasheet gives:

| Parameter | Datasheet field | Model element |
|---|---|---|
| V_RWM | reverse standoff voltage | reported only |
| V_BR | breakdown voltage at I_T | diode `BV` |
| V_C at I_PP (8/20 µs), or TLP clamping voltage at a stated current | clamping voltage | `RS = (V_C − V_BR) / I` |
| C_J | line capacitance | `CJO` |
| topology | how the part is built | subcircuit shape, below |

**Topologies:**
- **Unidirectional TVS:** line to ground, breakdown one way and forward conduction the other.
- **Bidirectional TVS:** two diodes in anti-series.
- **Steering array** (USBLC6, SRV05, TPD4Exx):
  - each line has a diode to an internal rail and a diode from ground;
  - a zener runs from the rail to ground;
  - the rail pin connects to the board supply when it is wired (USBLC6 pin 5).
- **Rail clamp** (BAT54S): a diode to a named supply and one from ground. The clamp current then
  flows into that supply, modelled as its nearest decoupling capacitor and that capacitor's
  ground via.

**A built-in table** covers the parts found on the real boards: USBLC6-2SC6 and -4SC6,
PESD5V0S1BA, PESD3V3L4UG, SRV05-4, SMAJ5.0A and SMBJ5.0A, SMF13CA, SMAJ36CA, and BAT54S.
- **Every value is filled from that part's datasheet, with the datasheet revision cited in the
  entry** — not from memory.
- A recognised part that is not in the table is modelled as a generic 5 V TVS, and the result
  says so.

### 4.2 Vendor models (phase 2)

The user supplies a `.lib` or `.cir` file in the project zip, mapped by part value in
`emi.rules.yaml`:

```yaml
transient:
  parts:
    USBLC6-2SC6:
      model: models/usblc6.lib
      subckt: USBLC6_2SC6
      pins: [IO1, GND, IO2, IO3, VBUS, IO4]   # footprint pad order -> subcircuit pins
```

A vendor model is used only if it reproduces the datasheet's V_C at its rated I_PP within 20 %.
Otherwise the result falls back to the generic model and says why.

### 4.3 IC pins

This is the largest uncertainty, and the reason §8 exists.

**Phase 1: a generic CMOS input.**
- **Pin:** 5 pF of capacitance, a diode to the net on its supply pin, a diode from ground, and
  1 Ω in series.
- **Supply:** modelled as its nearest decoupling capacitor and that capacitor's ground via. The
  decoupling check already knows both.
- **Results:** peak current into the pin's diodes and the energy dissipated there, not a pass or
  fail.

**Phase 2: IBIS models.**
- **Inputs:** `C_comp` and the `[GND Clamp]` / `[POWER Clamp]` tables.
- **Limitation:** IBIS tables stop near the rails, so any current at ESD levels is extrapolated,
  and the result has to say that.

---

## 5. Running ngspice

- **Image.** Add `ngspice` to the base stage of `worker/Dockerfile`, so a worker built without a
  solver has it too. A worker advertises the `transient` kind only
  when the binary is present, the same way `solve` is gated on openEMS in `config.py` today.
- **Invocation.** `ngspice -b` on a generated netlist, with results written through `wrdata`
  and parsed. One process per run, with each line as an independent subcircuit so startup is
  paid once, under a hard timeout.
- **Time base.** `.tran` over 0–100 ns for ESD. The step is bounded by a tenth of the shortest
  T-line delay.
- **Source.** A behavioural current source (`B` element) evaluating the §2.1 equation in `time`,
  injected at C.
  - **Why a current source:** the generator's 330 Ω dwarfs the impedance of a clamped line, so
    the current barely depends on the board.
  - **Where that breaks:** on an unclamped line. When node C rises beyond a set fraction of the
    charge voltage, the result reports "unclamped: the pin takes the discharge" instead of a
    number.
  - **Later:** the generator's RC network, with parasitics fitted to Table 3.
- **Cost.** A line is about ten elements. The four real boards have up to about 20 exposed lines
  each. Expect well under a second per line; phase 1 measures it.

---

## 6. Where it lives

- **Run kind.** Add `transient`: `RunKind` in `server/emi/types.go`, validated in
  `handleCreateRun`, and a worker `kinds` entry.
  - **On demand, not on every upload.** Part models are generic until someone confirms them, and
    an automatic number on every board would be read as a prediction.
  - **No cost gate.** It takes seconds, so it needs no cell estimate.
- **Parameters:**
  ```json
  {"standard": "61000-4-2", "level": 4, "polarity": "both", "lines": ["/USB_D+"], "what_if": true}
  ```
  `lines` may be omitted to mean every line `esd-protection` found. Suppressed nets are skipped.
- **Input.** The run re-parses the upload with the same loader ingest uses. Every run already
  gets `input_url`, and parsing plus topology takes well under a second on the real boards.
- **Output artifact `transient.json`, per line:**
  - the nets and the parts matched;
  - which model each part used (table, generic or vendor) and where its numbers came from;
  - the parasitics: lengths, Z0, TD and via inductance;
  - per what-if: peak pin voltage, peak pin current, pin energy, peak clamp current and clamp
    voltage;
  - downsampled waveforms, at most 400 points each, for plots.

  Plus a run-level list of assumptions.
- **UI.**
  - **Buttons:** "Simulate discharge" on each `esd-protection` finding, and on the Board tab for
    every exposed line.
  - **Result panel:** the pin-voltage waveform for each what-if, the numbers, the assumptions,
    and the §8 caveats.
  - **Findings stay as they are;** the simulation annotates them.
- **Settings.** A `transient:` block in `emi.rules.yaml` for the level, part overrides (§4.2) and
  IC pin overrides.

---

## 7. Verification

These tests must exist before a number is shown to anyone:

1. **Source.** Sample the equation at every level and check it against §2.1: first peak within
   ±15 %, 10–90 % rise within 0.8 ns ±25 %, and the 30/60 ns points within ±30 %, timed from the
   10 % crossing. Then run the same source through ngspice into 1 Ω, which checks the netlist and
   not only the Python.
2. **Inductor.** A known current into a known inductance gives V = L · di/dt at the steepest
   point.
3. **Transmission line.** A step on a matched line arrives after TD with no reflection. An open
   stub doubles it.
4. **Clamp models.** For every built-in table entry, an 8/20 µs pulse at rated I_PP reproduces
   the datasheet V_C within tolerance.
5. **Branch point.** Synthetic trees with known geometry. A loop falls back to straight-line
   placement and says so.
6. **What-ifs.** On synthetic cases, moving the clamp to the connector never raises the pin
   voltage.
7. **Real boards.** Every exposed line on a set of real boards gives plausible, stable results,
   with timings recorded.
8. **Cross-check (phase 4).** In a linear case (no clamp, 50 Ω termination), ngspice's T-line
   response matches openEMS S-parameters of the same trace.
9. **Bench, when equipment exists.** An ESD gun on a test coupon with the clamp near and far.
   Compare the trend, not the volts.

---

## 8. What it cannot say

- **Contact discharge only.** Air discharge is an arc and is not modelled.
- **IC internal protection is unknown,** so absolute volts and energy are estimates. What to
  trust is the comparison between layouts.
- **No failure prediction.** It predicts neither damage nor soft failures (resets, lock-ups), and
  an IC's HBM/CDM rating is a different test that cannot be compared with it.
- **The plane is ideal.** It models no ground bounce, no coupling onto neighbouring traces, no
  radiated field from the discharge, and no discharge to the enclosure, cables or coupling
  planes. Phase 4 adds coupling and ground bounce through openEMS.
- **Parts are recognised by value and footprint,** and a clamp the tool does not recognise is
  simulated as absent — the same rule the checks use.
- **Metal-shell connectors are tested at the shell** (§2.1). Injecting at a pin is a worst case.
- **Not a compliance prediction,** the same position the limitations page takes for emissions.

---

## 9. Phases

| Phase | Scope | Done when |
|---|---|---|
| 1 | ESD contact discharge on the lines `esd-protection` finds; generic and table clamp models; generic IC pin; T-lines from the stackup; what-ifs; `transient` run kind, `transient.json`, result panel | §7 tests 1–7 pass and the real boards run |
| 2 | Vendor models from the project zip; IBIS pin capacitance; ordered runs in topology; burst as a single 5/50 ns pulse, after checking the 61000-4-4 text | vendor models validate against datasheets |
| 3 | Surge 1.2/50–8/20 µs on the lines `input-filter` finds; coupling 18 µF and 9 µF + 10 Ω; TVS energy against its rated pulse power | the generator reproduces the waveforms and Tables 2, 3 and 6 |
| 4 | openEMS extraction, scikit-rf vector fit to a SPICE subcircuit, then ground bounce and coupling to neighbours | §7 test 8 |

Physical injection on the bench, with BenchPod monitoring the board, is a separate plan.

## 10. Decisions before building

1. **On-demand only** (recommended), or also automatic on every upload once the part table has
   proven itself?
2. **The built-in part table** is real work: each entry is read from a datasheet and cited. Is
   the list in §4.1 the right starting set?
3. **Phase 2 vendor-model upload:** worth building, or stay with generic models plus the table?

Decided: on demand only; the §4.1 table (plus SMAJ22A, found on a real board); vendor models
uploadable, and validated by the worker before it uses them.

---

## 11. As built

Phase 1 and vendor-model upload are implemented. Where the build differs from the plan above,
this section is the one that is true.

### 11.1 Changes from the plan, and why

- **Traces are lumped LC ladders, not ngspice's lossless `T` element.** The `T` element is exact
  but schedules a breakpoint for every reflection; with a clamp stub and a nonlinear pin they
  multiplied until one line ran past two minutes. Pi sections of at most 12 ps (about 2 mm) put
  the cutoff above 20 GHz, far past a 0.8 ns rise, and a line takes about half a second.
- **Every modelled node has the capacitance it physically has.** A supply node between the
  regulator's inductance and the decoupling capacitor's ESL, with nothing else on it, collapses
  the timestep in the first femtoseconds. The IC supply carries 100 pF of on-die capacitance, and
  pin-internal and clamp-ground nodes 0.5 pF.
- **The source is a Norton equivalent:** the Table 3 current in parallel with the generator's
  330 Ω discharge resistor. Into a clamped line almost all the current still flows to the line,
  and the source still meets Table 3. A pure current source forced 30 A through the series
  resistor of every R-clamp on a real board.
- **"Unclamped" is judged at the connector:** the node reaching more than 90 % of the generator's
  open-circuit voltage (first peak × 330 Ω). A clamp-current threshold flagged R-clamps, whose
  resistor limits the current by design.
- **Capacitors to ground on the IC's net are lumped at the pin**, each with 1 nH. Without them a
  supply pin behind 10 µF of decoupling read like a bare logic input, at around a hundred volts.
- **The clamp slope is fitted through the datasheet's rated point**, after subtracting the
  breakdown knee and, for steering arrays and bidirectional parts, one forward junction.
- **Element names never collide.** A clamp directly on the line gave its zero-length stub the
  same name as another resistor, and ngspice refused every such netlist.
- **Models are stored with the run, not in a table.** A file goes through the ordinary
  content-addressed upload; the run's params name its key, subcircuit and pad-to-pin map, and
  the UI rebuilds the attached models from the latest transient run. The server accepts only
  keys under the project's own `uploads/` prefix, checks that the object exists and its size,
  and checks the prefix again when it presigns the file for the worker.

### 11.2 Vendor model validation

A file is used only when every step passes, and every result is shown with the simulation:

1. **Structure** (`transient/spice_model.py`): text under 2 MB, and an allowlist — `.subckt`,
   `.ends`, `.model`, `.param`, `.func`, and R C L K D Q M J V I E F G H B X T S W elements inside a
   subcircuit. Refused, with the reason: `.control` (runs commands, including `shell`),
   `.include` and `.lib` (read files), `.options`, `.global`, `.temp`, analysis and output
   commands, XSPICE `A` and OSDI `N` devices, `file=` arguments, elements outside a subcircuit,
   references to undefined subcircuits or diode models, and names starting `EMI_`.
2. **Pins**: the chosen subcircuit exists, and each of its pins maps to exactly one pad of that
   part on the board.
3. **It clamps**: a 2 kV contact discharge into a line pin, both polarities, converges and stays
   below 200 V.
4. **It matches the datasheet**, when the part is in the table: the clamping voltage at the rated
   current within 25 %.

ngspice runs sandboxed: a fresh temporary directory that is also HOME, so the only `.spiceinit`
it reads is ours (which sets PSpice compatibility); batch mode, stdin closed, an emptied
environment, a wall-clock timeout, and CPU, file-size and memory limits.

### 11.3 The datasheet table

Read on 2026-09-11. Several manufacturer sites refused the download, so their own PDFs were read
from distributor copies, and newer revisions may exist. SMAJ, SMBJ and SMF clamping voltages are
on the 10/1000 µs basis, not 8/20 µs. SMF13CA uses Diotec's datasheet, whose SMF13A row matches
Littelfuse's exactly. Each entry in `parts_table.py` records its source and caveats.

### 11.4 On real boards

On the four calibration boards (58 exposed lines in all) simulation took 0.5–0.75 s a line in the
worker image on one laptop core; the 22-line board took 14 s.

### 11.5 Where it lives

- Worker: `emi_worker/transient/` — `waveforms`, `lines`, `parts`, `parts_table`, `netlist`,
  `ngspice`, `spice_model`, `models`, `simulate` — and `stages/transient.py`. A worker advertises
  the `transient` kind only when `ngspice` resolves; the worker image installs it in the base
  stage, so an image built without a solver has it too.
- Server: `emi/transient.go` (params, model-key checks), the `transient` run kind in `types.go`,
  and model URLs in `handleRunInputURL`.
- Webapp: `components/EsdSimulation.tsx` (the ESD tab), `TransientChart.tsx`,
  `lib/transientTypes.ts`, `lib/spiceHeader.ts`, and "Simulate discharge" on esd-protection
  findings.
- Tests: `worker/tests/test_transient_models.py` (no ngspice), `worker/tests/test_transient_sim.py`
  (needs ngspice; run it in the worker image), `server/emi/transient_test.go`,
  `webapp/src/lib/spiceHeader.test.ts`.
