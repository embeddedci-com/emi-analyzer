# Verification: small-part solves

September 2026. A small-part solve is the bounded kind of full-wave solve: one net (or a pair)
cut out of the board over its planes, solved in minutes with no far field. This page says what
was built, which checks were run and what they gave. Every check passed in the third pass, and
small-part solving is now **on** by default.

Real boards are private and named here only by letter (this page's own), layer count and coupon
size. Every openEMS run was made in the worker image with three threads.

## What was built

- **The coupon** (`worker/emi_worker/openems/coupon.py`): the net's own copper, every pour under
  it (whatever its net), the ground copper beside it, and the board's stackup. Neighboring
  signals are left out, and the result says how many. The margin is the larger of 3 mm and five
  heights of the outer dielectric; a coupon side over 60 mm is refused.
- **Ports** at the net's two furthest pads, 50 ohm each. The pad on the part with the most pins
  is driven. A net with one pad gets a port there and a 50 ohm load at its far track end.
- **The solve** (`worker/emi_worker/stages/small_part.py`): 100 MHz to 2 GHz by default (50 MHz
  to 6 GHz allowed), no far-field box, a -50 dB end criterion, and a budget of 3 M cells and
  1.5e11 cell-steps checked against the real mesh before the run starts. In-plane copper lines
  closer than half a cell are merged, so one pad corner does not set the timestep.
- **Outputs**: the hotspot map and `network.json` with Z_in, S11 and S21 per port. A frequency
  that reads as a negative resistance is dropped and listed in `truncated_hz`; a run that did
  not settle publishes no number.
- **The gate** (`server/emi/features.go`): feature id `small-part-solve`, on by default
  (`SmallPartSolveByDefault = true`). It lets through only a solve whose params say
  `mode: small_part`; asking for a far field, cable ports or component models is a 400.
  `full-wave` implies it.
- **The app**: a Part solve tab (pick a net or its pair, or draw a region; band, mesh preset,
  size, ports and cost estimate before it runs), progress by energy decay, and a result view
  with the hotspot map, port impedance and S11/S21. The browser plans the coupon the way the
  worker does; `server/emi/testdata/small_part_fixtures.json` pins the shared constants.

## What was fixed in the second pass

The second pass (September 2026) found four defects in the model, not the solver. Each showed
on every preset while every run still settled, so nothing flagged them.

- **Vias shorted diagonal traces.** A via was one PEC box as wide as its ring. A square reaches
  41 % further along its diagonal than the ring does: on board C a ground via's corner came
  within 24 um of a 45-degree differential trace, the grid joined them, and the "through line"
  read S21 of -57 dB with |Z_in| an inductor. A via is now its drilled barrel plus a round ring
  on each layer it spans. The same coupon reads S21 of -0.05 dB at 100 MHz.
- **Diagonal traces were cut in two.** Grid lines sat at a diagonal segment's ends only, so the
  mesh graded out to 0.7 mm under a 0.3 mm trace at 45 degrees and whole columns of cells missed
  it. The synthetic clock coupon's far port read -79 dB. A diagonal now gets lines along both
  axes it spans, no further apart than 0.7 of its width and no closer than half its width or
  the preset's cell.
- **The map was read at a different height on each preset.** It was read on the first grid
  line above the copper: 72 um up on coarse, 51 um on normal. The field at a trace edge falls
  as one over the root of the height, and board C's hotspot read 1.7 dB louder on normal. A
  small-part map is now read 0.1 mm above its copper on every preset, interpolated between the
  two grid lines either side.
- **The coarsest cell was in the absorbing layer.** The PML padding grew 1.2x a line and took
  board C's coarsest cell to 5.3 mm against the 3.49 mm the band allows, on both presets. A
  small-part mesh now holds the padding to the band's cell: 3.49 mm on both. The interior never
  went past 1.5 mm.

Also added: a coupon now says how many pieces of other copper sit within one cell of the net
on the same layer, and a small-part result no longer shows the band-edge note (every output is
a ratio to the source) or "no components were modelled". The app starts a part on the coarse
mesh when normal is over budget, since the sample board's first net was refused on normal.

## What was fixed in the third pass

The second pass left three failures: board B's hotspot level differed by 3.3 dB between presets,
the end criterion could stop on one low sample, and the sample board gave no numbers. The third
pass (September 2026) fixed them and what the reruns found.

- **A hotspot's level is a probe-sized average** (the user's decision). The map is averaged over
  a disc of 0.25 mm radius before its loudest point is taken, in the study and in the product
  (`openems/hotspots.py`), so the listed level is the same number. A single grid point beside a
  pad edge did not settle with the mesh.
- **The mesher draws a narrow trace the same on every preset.** A trace narrower than two cells
  gets the thirds rule at half its width, and kept lines closer than half their rule's cell are
  one (a routed trace's jogs of a few micrometers each had lines of their own).
- **The end criterion holds.** The energy must read under -50 dB for three reports in a row,
  over at least 1 ns of simulated time, and the last report must be no higher than the first.
  Board C's pair swung 12 dB with a period of 0.36 ns, and three reports fell in one trough.
  A via between two planes (check 4) fell 19 dB in one report and climbed back over the next
  nanosecond; stopped there, its inductance read 27 % high at 100 MHz.
- **A one-pad net's far end gets a 50 ohm load.** Left open, the old sample board's clock rang
  past its whole budget.
- **Less of the pulse falls below the band.** At the default 20:1 band the Gaussian's half-width
  equalled its center, so it was only 20 dB down at DC. It is now held to 0.71 of the center,
  and a slow mode below the band is driven 38 dB down instead of 19. Every output is a ratio to
  the source, so the band's weaker edges move nothing. The timestep cap is computed from the
  narrower pulse.
- **The estimate counts the mesh.** The browser lays out grid lines the way the worker does
  (`webapp/src/lib/smallPartMesh.ts`), including the lines along a diagonal trace, which it reads
  off the outline as pairs of long parallel edges. Without them the new sample board's SPI_CLK
  counted 1.0 M cells on normal where the worker meshed 2.4 M, and the app offered a normal
  mesh the worker refused. On the sample board's ten nets and the four study coupons the
  estimate is now 0.72x to 1.8x the real mesh.
- A coarse part on a dielectric one cell thick says so.

## Checks

Scripts are in `worker/research/sp_*.py` and run in the worker image with the worktree mounted
(the docstring of each has the command), three threads, on the released image's openEMS, built
from source (65f8771). `REUSE` reads runs that already finished. Real boards are private and
named here by letter only.

| # | Check | Criterion | Result |
|---|---|---|---|
| 1 | 50 ohm microstrip, Z0 and delay | within 5 % of Hammerstad-Jensen | ✅ all three presets, Z0 within 0.45 %, delay within 0.69 % |
| 1b | A narrow microstrip, 0.1 mm on 76 um (board B's) | within 5 % | normal -1.8 % and fine +2.0 % ✅; coarse -10.3 %, so a coarse part on one cell of dielectric says so |
| 2 | Same microstrip, matched S21 | within 0.5 dB to 1 GHz | ✅ 0.06 dB on all three presets |
| 3 | 50 ohm stripline, Z0 and delay | within 5 % of Cohn | ✅ Z0 within 3.1 % (coarse), 0.44 % (normal, fine); delay +1.1 to +1.2 % |
| 4 | Via inductance, parallel-plate | within 10 % of two posts at normal | ✅ normal worst +7.2 %; coarse worst +9.0 % |
| 5 | Synthetic coupon, 3 margins and 2 presets | hotspot, level 1 dB, \|Z\| 5 %, S21 0.5 dB | ✅ |
| 6 | Real coupons B, C and D, 3 margins and 2 presets | the same | ✅ all three |
| 7 | Slow tests (`EMI_SLOW_TESTS=1`) | pass in the image | ✅ 1127 passed, 10 skipped |
| 8 | The sample board from the Part solve tab | numbers in minutes, estimate within about 2x | ✅ 240 s against "about 3 m"; 167 s against "about 4 m" |

The hotspot is found if it did not move by more than two cells, or if either run's hotspot is
within 3 dB of the other run's peak (the user's decision in the third pass). Two near-equal
spots can trade places between meshes, and either is a spot to fix. A result lists every
separate spot within 3 dB of each map's loudest point away from the ports
(`worker/emi_worker/openems/hotspots.py`), with the nearest net and part, numbered in the result
panel and marked on the board.

### 1-2. Microstrip (`sp_verify_lines.py`)

A 382.8 um line on 200 um FR-4 (er 4.4, tan d 0.02), 25 mm long, through the product path.
Hammerstad-Jensen gives 50.00 ohm and eps_eff 3.331. Errors over 0.5-2 GHz:

| Preset | Cells | Wall | End | Z0 | Delay, port to port | Delay, by difference | S21 to 1 GHz |
|---|---|---|---|---|---|---|---|
| coarse | 59,829 | 25 s | -75.5 dB | -0.43 to -0.30 % | -0.48 to -0.32 % | +0.09 to +0.36 % | 0.06 dB |
| normal | 82,173 | 39 s | -72.2 dB | +0.04 to +0.25 % | -0.69 to -0.54 % | -0.09 to +0.19 % | 0.06 dB |
| fine | 102,459 | 81 s | -73.9 dB | 0.00 to +0.45 % | -0.40 to -0.25 % | -0.12 to +0.17 % | 0.06 dB |

**Why the delay reads low.** The port-to-port delay assumes the line starts at the port's
center. The same line at 25 and 50 mm, with the delay taken from the phase the two differ by,
removes whatever the ends add: eps_eff is then within 0.4 % on every preset. The ends add -0.14
to -0.18 mm of line (coarse, normal) and -0.07 to -0.10 mm (fine), which is the 0.3-0.7 %. So
the reference plane of a 0.4 mm lumped port sits inside the pad center; the line itself is
right. Dispersion is not it: at 2 GHz on 0.2 mm FR-4, f times h is 0.4 GHz mm, where
Kirschning-Jansen moves eps_eff well under 0.1 %.

### 1b. A narrow microstrip

Board B's clock is a 0.1 mm trace on 76 um of FR-4. The same line, 25 mm long
(`MS_H=0.0764 MS_W=0.1 LINES=microstrip`), against Hammerstad-Jensen's 61.98 ohm: coarse (102,960
cells, 47 s) reads Z0 -9.3 to -10.3 %, normal (140,712 cells, 67 s) -1.5 to -1.8 %, fine
(164,406 cells, 136 s) +1.6 to +2.0 %. S21 within 0.12 dB to 1 GHz on all three. On coarse the
dielectric is one 100 um cell through, and the product says so in the set-up and the result
(`COARSE_THIN_NOTE`): "its impedance can read about 10% low". Board B's convergence (below) is
unaffected: the level and the port agree between coarse and normal.

### 3. Stripline

191.5 um between planes 415.2 um apart (the strip at 0.48 of the gap), Cohn 50.00 ohm, eps_eff
4.4. Every run settled (-69.5, -68.2 and -67.4 dB in 38, 69 and 151 s). Z0: coarse -3.1 to
-2.5 %, normal -0.20 to +0.44 %, fine -0.15 to +0.41 %. Coarse reads lower than in the second
pass (-1.0 %) since the strip, narrower than two coarse cells, is ruled at half its width. Delay
+1.1 to +1.2 % on all three, which does not move with the mesh, so it is likely the ends again
(the strip runs past each port by half its width). Not checked by difference.

### 4. Via inductance (`sp_verify_via.py`)

A via between two planes 1.5 mm apart beside the port, against two posts between parallel
plates, read at 100-300 MHz. The via is drawn as its drilled barrel, so the closed form uses
the drill.

| Via drill, spacing | Closed form | Coarse | Normal |
|---|---|---|---|
| 0.3 mm, 1.0 mm | 0.925 nH | +7.3 to +9.0 % | +2.2 to +7.2 % |
| 0.3 mm, 2.0 mm | 1.362 nH | +0.2 to +8.0 % | -2.7 to +5.6 % |
| 0.8 mm, 2.0 mm | 1.052 nH | -3.1 to +2.5 % | -3.4 to +0.9 % |

The criterion is at normal; coarse may exceed it with the note shown. Coarse now stays within
10 % too, but only just, and it read +10.1 % at one point in the second pass. So a part that runs
coarse with a via on its nets still says, in the set-up and in the result, "coarse mesh: via
inductance can read up to about 10% high" (`stages/small_part.py`, `COARSE_VIA_NOTE`). The app
starts a part on normal whenever normal fits the budget.

The first normal run of the 0.8 mm via read +27 % at 100 MHz: it stopped in a trough of its
energy (above). With the end criterion fixed, the three normal runs were made again; the numbers
above are those.

### 5-6. Convergence (`sp_convergence.py`)

Each part at 3, 5 and 7 mm margins on coarse and normal, with the study's budget lifted
(`SP_BUDGET=2e11`; board C's normal run at 7 mm needs 1.9e11 cell-steps, over the product's
1.5e11). Each margin is compared with 7 mm on the same preset (the cut), and the presets with
each other at each margin (the mesh). "Swap" is how far the nearer of the two hotspots sits
below the other run's peak. Levels are the probe-sized average.

| Part | Size at 3 mm | Cells, coarse / normal | Wall, coarse / normal |
|---|---|---|---|
| Synthetic clock, 4 layers | 28.9 x 14.4 mm | 397-435 k / 0.89-0.99 M | 59-66 s / 193-222 s |
| B, clock net, 6 layers | 7.0 x 10.2 mm | 189-232 k / 491-599 k | 58-63 s / 133-158 s |
| C, differential pair, 4 layers, 4 ports | 32.3 x 8.5 mm | 493-586 k / 1.01-1.16 M | 72-253 s / 291-880 s |
| D, supply net, 4 layers | 17.8 x 8.5 mm | 351-406 k / 0.92-1.03 M | 61-80 s / 248-335 s |

Every run settled.

| Part | The cut, worst of 4 | Presets: hotspot | Level | \|Z_in\| median / worst | S21 | Verdict |
|---|---|---|---|---|---|---|
| Synthetic | 0 cells, 0.01 dB, \|Z\| 0.2 %, S21 0.002 dB | 0 cells at 1 GHz; at 300 MHz swap 0.05 dB | 0.07 dB | 0.7 / 6.9 % | 0.03 dB | ✅ |
| B | 0.11 cells, 0.00 dB, \|Z\| 0.1 %, S21 0.001 dB | 0.11 cells | 0.86 dB | 0.1 / 2.1 % | 0.002 dB | ✅ |
| C | 0 cells, 0.00 dB, \|Z\| 0.3 %, S21 0.019 dB | 1.8 cells at 1 GHz; at 300 MHz swap 0.05 dB | 0.58 dB | 0.0 / 0.6 % | 0.023 dB | ✅ |
| D | 0 cells, 0.00 dB, \|Z\| 0.0 %, S21 0.002 dB | 0.4 cells | 0.45 dB | 0.9 / 5.4 % | 0.079 dB | ✅ |

- **Board B** read 3.3 dB apart between presets at a single grid point in the second pass. With
  the probe-sized average and the narrow-trace rule it reads 0.86 dB.
- **Board C** and the synthetic clock each have two near-equal spots at 300 MHz (the two ends
  of the line, 0.05 dB apart), and the presets pick different ones. The result lists both.
- The worst |Z_in| points (5.4 % on D, 6.9 % on the synthetic board) sit on a resonance, where a
  small shift in frequency is a large change in impedance. The medians are under 1 %.
- Of the runs reused from the stopped study, three would have stopped later under the fixed end
  criterion (B coarse at 5 mm, D coarse at 5 mm, synthetic coarse at 7 mm); those were run again.
  The earlier study's D normal run at 5 mm ran for over an hour and was stopped. Run again, it
  took 320 s. The cause was not found.

### 7. Slow tests

`EMI_SLOW_TESTS=1` in the image, as `.github/workflows/test.yml` runs it: 1127 passed and 10
skipped, in 3 minutes. The three small-part solves took 60, 46 and 26 s. The far-field test that
failed in the second pass (its reader did not know the from-source build's HDF5 layout) passes.

### 8. In the app

`emi-local` built from this branch, `-experimental small-part-solve` (it was still off), with a
worker from the same image capped at three cores, in a browser. The sample board ("Try the sample
board") is now a 70 x 45 mm, 4-layer board whose nets all have two ends.

| Net | Mesh the app chose | Estimate | Real mesh | Wall | Result |
|---|---|---|---|---|---|
| /SPI_CLK (crosses a plane slot) | coarse ("over budget on normal") | 0.78 M cells, "about 3 m, at most 16 m" | 677 k | 240 s | map, loudest spot, ports; S21 -0.06 to -2.65 dB |
| /USB_DP with /USB_DN | normal | 0.57 M cells, "about 4 m, at most 16 m" | 425 k | 167 s | map, loudest spot, ports; S21 -0.07 to -0.71 dB |

Before the estimate counted a diagonal's lines, /SPI_CLK was estimated at 1.04 M cells on normal
("about 7 m"); the worker meshed 2.42 M and refused it before running.

## Verdict

**On.** Every criterion passes on the openEMS the released image ships: the lines and vias
against their closed forms, the synthetic part and three real parts over the cut and the mesh,
the slow tests, and the sample board from the app in minutes, with the runtime within 1.4x of
the estimate. `SmallPartSolveByDefault` is `true`.

## Limits that stay

- Neighboring nets are left out of the cut, so coupling into them is not shown.
- No far field, cable emissions or compliance estimate; those need `full-wave`.
- No component models in a small part. Elsewhere they need the openEMS built from source that
  the worker image ships; 0.0.35 cannot model an inductor
  ([solver-and-components.md](solver-and-components.md) §3).
- Coarse mesh: via inductance can read up to about 10 % high.
- A net with one pad gets a 50 ohm load at its far end, which the real board may not have.
- Below 100 MHz a part a few centimeters across is quasi-static; a circuit tool gives the
  lumped L or C more cheaply.
- Nothing has been compared with a measurement.

## Next steps

1. Compare a part's map and port with a measurement: a near-field probe scan and a VNA on one
   of the real boards.
2. The report still lists small-part solves under its experimental section
   (`webapp/src/lib/report/model.ts`); give them a section of their own.
3. The estimate still reads up to 1.8x high on short nets with few edges (the sample board's
   /NRST), where the worker's jog merge and thirds rule remove lines the browser keeps.
4. Optional: the stripline delay by difference, to confirm the +1.2 % is the ends.
