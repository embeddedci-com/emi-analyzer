# Verification: the full-wave solve core and the component models

September 2026. Six checks of the parts every full-wave result stands on: whether a run that
did not settle can still publish a number, whether the model gets a transmission line's
impedance right, whether the shipped solver can model a capacitor at all, whether a modelled
capacitor behaves like one, what a run at radiated record length costs, and whether the cost
estimator matches the mesher. Each has a criterion, a number against it, and a script that
reproduces it. Full-wave solving stays behind the `full-wave` experimental flag; nothing here
turns anything on.

Midway, whole-board solves at radiated record length were taken out of scope: full-wave is
for small regions cut from a net. The record-length check (§5) was stopped; what its first run
found about how openEMS ends a run applies to small regions too, and is kept.

Real boards are private and named here only by letter, layer count and size. Every openEMS run
was made in the worker image with at most three threads.

| # | Check | Criterion | Result |
|---|---|---|---|
| 1 | A run that did not settle | nothing derived from it is used | ✅ after three fixes, one of them to how a run ends |
| 2 | 50 ohm microstrip through the production path | Z0 within 5 % of Hammerstad-Jensen | ✅ -0.9 to +0.2 % on all presets, after three fixes |
| 3 | Lumped inductor in openEMS 0.0.35 | an L element is modelled | ❌ it is skipped; capacitors were open circuits. Fixed by refusing on 0.0.35 |
| 4 | One 0402 capacitor over a plane | SRF within 5 %, \|Z\| within 1 dB to 3x SRF | ⚠️ 100 pF on a current openEMS: SRF +1.6 % ✅, \|Z\| +1.37 dB at resonance ❌, and only with a -70 dB record |
| 5 | 100 ns record on a real board | decays to -40 dB; cost measured | dropped (out of scope); found openEMS ending runs inside the pulse |
| 6 | Cost estimator against the mesher | per-preset floors from real boards | ✅ recalibrated on 4-10 mm regions; the timestep term was also 2.0-2.7x low |

---

## 1. A run that did not settle

**What was wrong.** A run that reached its timestep cap was published with `converged: false`
in the manifest and one log line. Nothing read the flag: the far field, the cable transfer
functions and the port spectra were used as if the fields had settled, the compliance estimate
put a margin on them, and the hotspot panel showed a yellow "did not fully settle" note when
the energy had fallen past -20 dB.

**What it does now.**

- The solve stage writes `run.unusable_reason` beside `converged` and marks every derived number
  unusable: `usable: false` on each port in `ports.json`, every point of every cable transfer
  function and every far-field frequency.
- The compliance estimate refuses such a solve outright with a `solve-unconverged` gap, even when
  its artifacts predate the marking.
- The webapp treats `converged: false` as unusable whatever the energy: the map is hidden unless
  asked for, no driver can be attached, the cable chart is withheld, and the panel quotes the
  worker's reason.

**The divergence check could not see a run that grows from the start.** It armed only after the
energy had fallen 20 dB below its peak. The check now takes the step at which the source has
finished from openEMS's own log (the model's 2.86/fc is the fallback); after that nothing feeds a
passive structure, so the energy is held to a decay from the first sample past it, decayed or
not.

**The 20 dB gate was also unsafe while the source was on**, which only a real board showed. A
30 MHz-1 GHz pulse is barely one cycle of its carrier, and in a small region the energy leaves
about as fast as it arrives: on board D (below) it fell more than 20 dB between two lobes and
rose 2,070x on the next, at step 46,624 of a 66,806-step pulse. The gate called that a
divergence and refused a healthy run. When the source length is known it now replaces the gate.

**A run could also stop too early** (§5): openEMS checks its end criterion while its own pulse
is still running, and stopped a real board's region at "-53 dB" three quarters of the way
through it. The solve now gives openEMS an unreachable criterion and `run_openems` enforces
-40 dB itself once openEMS's own excitation has finished, by writing the `ABORT` file openEMS
polls for; openEMS then ends normally and writes every dump. Two details only running it
showed: openEMS 0.0.35 prints its timestep-cap warning on an abort too, and can leave the
`ABORT` file behind, which ended the next run in that directory at step 0. A run that stops
before its source has finished for any other reason is not converged, with its own reason.

**The docstrings claimed openEMS stops at -50 dB.** Every solve is given `end_criteria = 1e-4`,
an energy ratio, which is -40 dB, and that is what openEMS stops at (-41.1 dB on the fixture
run). Code and docs now say -40.

Tests: `test_run_stability.py` (growth from the start, growth during the source, ringing
after it, the parsed excitation length, a stop inside the source, the runner's own stop with a
fake openEMS that waits for `ABORT`), `test_post_ports.py`,
`test_nf2ff.py`, `test_assemble.py`, `solveQuality.test.ts`, `driverAttach.test.ts`.

---

## 2. Microstrip impedance

`worker/research/verify_microstrip.py`. A 20 mm, 382.8 um wide strip on 0.2 mm of eps_r 4.4,
lossless, over a plane: 50.00 ohm and eps_eff 3.331 by Hammerstad-Jensen for zero thickness
(which is what the model draws), 49.18 ohm by IPC-2141. It is written as a KiCad board and goes
through the parser, `build_model`, the mesher and two production lumped ports; one solve gives
V and I at both ends, a symmetric reciprocal two-port has two unknowns, and
Z0 = sqrt(Z11^2 - Z12^2), cosh(gamma l) = Z11/Z12. 0.5-3 GHz, openEMS 0.0.35.

The first run found three defects, in order:

| Defect | Symptom | Fix |
|---|---|---|
| A shape was kept only if one of its vertices lay in the region | the plane under the line, drawn as one rectangle over the board, was dropped: Z0 = -j9000 ohm | kept by bounding-box overlap |
| Copper sheets sat mid-copper while each dielectric kept its own thickness | half a copper thickness of air either side of every dielectric; at fine: 56.3 ohm, eps_eff 2.18 | sheets sit on the dielectric face (the thinner neighbour for an inner layer); slabs reach them |
| A grid line on a zero-thickness strip's edge | the strip acts about half a cell wider per side: 40.0-40.6 ohm on every preset, because the cell beside a lone trace is set by the copper, not the preset | thirds rule on straight trace edges: a line a third of a cell inside, two thirds outside, none on the edge |

After them, and after the second mesh-grading fix, with runs ended by the runner (§1):

| Preset | Cells | Cells across strip | Z0 error | eps_eff error | Steps | Wall (3 threads) |
|---|---|---|---|---|---|---|
| coarse | 58,608 | 2 | -0.56 to -0.91 % | -1.4 to -1.8 % | 7,280 | 17 s |
| normal | 80,496 | 4 | +0.13 to -0.39 % | -1.7 to -2.1 % | 14,433 | 27 s |
| fine | 101,520 | 4 | +0.09 to +0.23 % | -1.4 to -1.8 % | 28,203 | 47 s |

**Pass**: within 1 % of Hammerstad-Jensen on every preset against a 5 % criterion, and within
2 % of IPC-2141, which is itself 1.6 % off Hammerstad-Jensen here. The 2 % low eps_eff is
consistent across presets and not explained; it is within what the ports' own vertical current
path and a 20 mm line can account for, and it moves a resonance by 1 %.

The second and third fixes change every solve's geometry, so the fixture-board figures quoted
in `known-issues.md` §2 before this date (steps to converge, energy) are from the old model.

---

## 3. Lumped inductors in openEMS 0.0.35

`worker/research/verify_lumped_rlc.py`. A 50 ohm port drives a 4 mm PEC strip 1 mm over a
plate; a z-directed element closes the loop. The same loop with the element replaced by PEC is
subtracted (1.60 nH of loop), leaving the element.

| Element | openEMS 0.0.35 (the image) | openEMS build (`OPENEMS_SOURCE=build`) |
|---|---|---|
| R 20 ohm | 0.00-0.08 dB | 0.00-0.08 dB |
| C 10 pF | -0.12 to -0.38 dB | -0.13 to -0.39 dB |
| L 10 nH | **skipped**: "R or C not specified! skipping", -j9921 ohm at 100 MHz (a 0.16 pF gap) against +j6.3 | 0.09 dB at 100 MHz to 0.73 dB at 1 GHz |
| series 1 ohm + 10 nH + 10 pF (`LEtype=1`) | not solved: with R and C set, 0.0.35 ignores `LEtype` and `L` without a warning and builds R parallel to C | 0.06-0.52 dB, 2.5 dB at 500 MHz where \|Z\| is 1 ohm at resonance |
| series 10 nH alone (`LEtype=1`) | | refused as unstable (4.5e3) by the old 20 dB gate, the false positive §1 describes; not rerun |

**The reviewer was right.** openEMS 0.0.35's `Operator::Calc_LumpedElements` models R and C and
drops an element with neither; the warning was not in `_SIGNIFICANT_WARNINGS`. The capacitor
model was three single-value elements in adjacent cells, so the L cell was skipped and every
modelled capacitor was R, an open gap and C: an open circuit. Every solve with
`model_components` on has modelled its decoupling capacitors as missing.

**Fix.**

- A capacitor is now one series element (`LEtype="1"`, R, L and C) across the pad gap.
- The solve asks the binary before placing one (`run.solver_has_series_rlc`: one cell of
  inductance, one timestep, and a look for the warning; 0.05 s). Asked with R and C set too,
  0.0.35 answers yes and builds R parallel to C, so it is asked with L alone. The image's 0.0.35
  answers no, the build answers yes.
- On a solver that says no, no capacitor is placed and the result says so: bare copper is
  visibly incomplete, a model that looks like decoupling and is not is not.
- "R or C not specified" and "capacity is too small for its size" are significant warnings, so
  any skipped element reaches the result.

Components can therefore only be modelled on a worker built with `OPENEMS_SOURCE=build`. The
released image is 0.0.35; switching it is a release decision, not made here.

---

## 4. One 0402 capacitor over a plane

`worker/research/verify_0402.py`. A two-layer board, 0.2 mm of eps_r 4.4 over a plane, one
0402 capacitor (KiCad's `C_0402_1005Metric`), a production port on its first pad and a via in
its second. The part is matched to the built-in generic library and placed by the production
path with `model_components` on. A second run bridges the pad gap with metal; it is the port,
pads, via and plane alone, and subtracting it leaves the part. The analytic reference is the
library's series R-L-C: its ESL excludes the mounting loop, which the solve models itself, so
the mounted resonance is compared with 1/(2 pi sqrt((ESL + L_mount) C)), L_mount read from the
short. Coarse preset. It needs a solver with the series element, so it ran on the
`OPENEMS_SOURCE=build` image; the shipped 0.0.35 image now refuses to place the part (§3).

**100 pF** (library: C 100 pF, ESL 0.45 nH, ESR 0.30 ohm), after the second mesh-grading fix, runs
ended at -70 dB (`END_CRITERIA=1e-7`): 91,080 cells, 69,160 + 173,409 steps, about 5 minutes.

| | Analytic | Measured | Error | Criterion |
|---|---|---|---|---|
| Mounting inductance (the short) | | 0.207-0.208 nH, flat 234 MHz-2.6 GHz | | |
| Part SRF (Z_part crosses zero) | 750.3 MHz | 762.3 MHz | **+1.6 %** | 5 % ✅ |
| Mounted SRF (Z_in crosses zero) | 620.5 MHz | 629.5 MHz | **+1.5 %** | 5 % ✅ |
| \|Z_part\|, 234 MHz-2.6 GHz (SRF/3.2 to 3.5x SRF) | | | -1.03 to **+1.37 dB** | 1 dB ❌ |

The two points past 1 dB are at 710 and 868 MHz, either side of the resonance, where |Z| is a
third of an ohm and the error is in the real part: the measured resistance scatters from 0.02 to
0.49 ohm below the SRF against the library's 0.30. Away from resonance the part is within 0.9 dB
below it and 0.2 dB above twice it. **The SRF passes; |Z| fails by 0.37 dB, at resonance only.**

What the other runs showed:

- **At the solve's own -40 dB the transform ripples by +-6 dB.** The same model ended by the
  runner at -40 dB (46,436 steps) measured the mounted SRF 4 % low and |Z| between -6.6 and
  +5.9 dB. The capacitor's loop rings down slowly, and -40 dB leaves enough of it untransformed
  to matter. A solve with a modelled capacitor needs a tighter end criterion than one without;
  that is not built.
- **Before the second mesh-grading fix** the same part measured C at 0.71x nominal and the SRF 17 %
  high, with a record ended at -65 dB on a 131,208-cell mesh. The fix changed the mesh around
  the pad gap, and with it the answer, which says the element is sensitive to the cells it spans.
- **1 nF** (ESL 0.45 nH, ESR 0.25 ohm), on the pre-fix mesh: the part run never settled. From
  8 ns to its 50 ns cap the port held a steady 4.5 uV and -90 nA, exactly -50 ohm apart, a DC
  current through an element that should block it. Its transform put the SRF at 67 MHz against
  196 MHz. Not rerun after the fix, since a run to settle it is hours.

**Result.** On a current openEMS, a 100 pF 0402 resonates where the library says, within 1.6 %,
and its impedance is within 1 dB of the series R-L-C except at resonance, where it is within
1.4 dB, **provided the run goes to -70 dB**. It is not verified at the solve's -40 dB, not on
the shipped solver, and not at 1 nF or above.

---

## 5. Record length on a real board

**Dropped**: whole-board solves at radiated record length will not be validated or shipped.
One run was made before that decision and a second was stopped part-way; they are reported
because they found how openEMS ends a run, which matters at any size. The script is not kept.

Board D (4 layers, 60 x 40 mm), a 6 mm region at the coarse preset, 30 MHz-1 GHz, the production
solve stage, openEMS 0.0.35, three threads:

| | |
|---|---|
| Mesh | 1,299,456 cells, smallest 37.5 um |
| Timesteps a 100 ns record needs | 1,383,981 (openEMS's own dt: about 1.2 M) |
| openEMS's excitation | 66,806 timesteps, 5.6 ns |
| First run, openEMS's -40 dB criterion | stopped at 50,995 steps (4.2 ns) at "-53 dB", **inside the pulse**; 507 s, 131 MCells/s, 297 MB peak |
| Same run on the openEMS build | stopped at the same 50,995 steps |
| Second run, criterion unreachable, stopped by instruction at 40 % | port voltage 118 dB below its peak by 5.6 ns and at the -180 dB noise floor from 7 ns on |

The first run was accepted as converged. openEMS compares energy to its running maximum at
every report, source or not, and a 33:1 band is one cycle of its carrier: between two lobes the
6 mm region, which empties almost as fast as it fills, fell 53 dB. That is fixed in §1.

The second shows what the rest of a 100 ns record buys on a region this small: nothing. The
fields had gone by the end of the pulse; the other 94 ns would have cost about 2.6 hours at the
measured throughput. For a small region the record is set by when the energy has gone, not by
three periods of 30 MHz, which is what ending the run on its energy after the source does.

On the build, post-processing then failed on "object 'f0_real' doesn't exist": a current openEMS
writes each frequency-domain dump as one complex (3, x, y, z) dataset with `d_order`. The
near-field reader now reads both layouts (`test_post_ports.py`); nf2ff's output on the build is
untested.

---

## 6. Cost estimator

`worker/scripts/measure_fill_factor.py` with `EMI_TEST_BOARDS`, meshing only: four real boards
(A: 6 layers, 120 x 100 mm; B: 4 layers, 100 x 80; C: 4 layers, 100 x 60; D: 4 layers, 60 x 40),
square regions of 4, 6 and 10 mm around the middle of the routing, three presets, 36 meshes,
after the second mesh-grading fix and the thirds rule. Mesh multiplier = cells / uniform cells.
Regions this small because that is what full-wave is for; they are denser than large ones, and
at 40 mm the floors came out 10-25 % lower.

| Preset | First calibration (old mesher, 10-40 mm) min / median / max | Now (4-10 mm) min / median / max | Floor |
|---|---|---|---|
| coarse | 3.55 / 6.11 / 8.75 | 4.85 / 6.93 / 8.74 | 3.5 → **4.8** |
| normal | 1.49 / 2.99 / 4.79 | 2.33 / 4.11 / 4.63 | 1.4 → **2.3** |
| fine | 0.70 / 1.64 / 2.89 | 1.23 / 2.65 / 3.03 | 0.7 → **1.2** |

The true minima were 39-76 % above the old floors. The measurement found a larger error in the other
term, **the timestep**. The estimator took the Courant step from min(dx, dy, dz), but the mesher
merges lines only closer than dx/4, and copper puts lines that close everywhere: the smallest
cell was within 0.4 % of dx/4 in all 36 meshes (37.5, 18.75 and 12.5 um). Step counts were 2.7x
low at coarse and normal and 2.0x at fine. The estimator now uses min(dx/4, dy/4, dz)
(`IN_PLANE_MIN_CELL_FRACTION`, equal to the mesher's `MERGE_FRACTION`, and tested to be).
Together a coarse estimate now reads about 3.7x longer than before, which is what the worker's
own figure at the mesh stage had been saying.

The floors and the fraction are pinned in `server/emi/testdata/estimate_fixtures.json` and
asserted by Python, Go and TypeScript.

---

## What still blocks full-wave coming out of experimental

In order of what would have to change first:

1. **Components.** The released image's openEMS 0.0.35 cannot model an inductor, so it places no
   capacitor. On a current openEMS build a 100 pF 0402 passes on SRF and misses |Z| by 0.4 dB at
   resonance, but only when the run goes to -70 dB; at the solve's -40 dB it ripples +-6 dB, and a
   1 nF part (pre-fix mesh) passed a DC current and never settled. Moving the image to the build
   is a release decision, and the build has not been through the rest of this page (its nf2ff
   output is untested).
2. **A real coupon against an independent reference.** The microstrip is the only full-wave
   result checked against theory, and it is a line on a plane. A coupon cut from a real net
   (vias, a split plane, a connector) has not been compared with a second solver or a
   measurement.
3. **The far field and compliance on a real region**, and Tier B cable emissions: as in
   `known-issues.md` §3, unchanged here.
4. **The 2 % low effective permittivity** of the microstrip is unexplained. It is small, but it
   is the same sign on every preset.

What is no longer a blocker: unconverged runs publish no numbers; a run cannot stop inside its
own pulse; the estimator matches the mesher on small regions; a diverging run that grows from
the start is caught.
