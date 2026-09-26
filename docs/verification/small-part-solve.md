# Verification: small-part solves

September 2026. A small-part solve is the bounded kind of full-wave solve: one net (or a pair)
cut out of the board over its planes, solved in minutes with no far field. This page says what
was built, which checks were run and what they gave, and what is still to do before the
`small-part-solve` experimental feature can be turned on. It stays **off**.

Real boards are private and named here only by letter (this page's own), layer count and coupon
size. Every openEMS run was made in the worker image with three threads.

## What was built

- **The coupon** (`worker/emi_worker/openems/coupon.py`): the net's own copper, every pour under
  it (whatever its net), the ground copper beside it, and the board's stackup. Neighboring
  signals are left out, and the result says how many. The margin is the larger of 3 mm and five
  heights of the outer dielectric; a coupon side over 60 mm is refused.
- **Ports** at the net's two furthest pads, 50 ohm each. The pad on the part with the most pins
  is driven. A net with one pad gets one port and an open far end.
- **The solve** (`worker/emi_worker/stages/small_part.py`): 100 MHz to 2 GHz by default (50 MHz
  to 6 GHz allowed), no far-field box, a -50 dB end criterion, and a budget of 3 M cells and
  1.5e11 cell-steps checked against the real mesh before the run starts. In-plane copper lines
  closer than half a cell are merged, so one pad corner does not set the timestep.
- **Outputs**: the hotspot map and `network.json` with Z_in, S11 and S21 per port. A frequency
  that reads as a negative resistance is dropped and listed in `truncated_hz`; a run that did
  not settle publishes no number.
- **The gate** (`server/emi/features.go`): feature id `small-part-solve`, off by default
  (`SmallPartSolveByDefault = false`). It lets through only a solve whose params say
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

## Checks

Scripts are in `worker/research/sp_*.py` and run in the worker image with the worktree mounted
(the docstring of each has the command), three threads. Real boards are private and named here
by letter only.

| # | Check | Criterion | Result |
|---|---|---|---|
| 1 | 50 ohm microstrip, Z0 and delay | within 5 % of Hammerstad-Jensen | ✅ all three presets, Z0 within 0.45 %, delay within 0.71 % |
| 2 | Same microstrip, matched S21 | within 0.5 dB to 1 GHz | ✅ 0.06 dB on all three presets |
| 3 | 50 ohm stripline, Z0 and delay | within 5 % of Cohn | ✅ Z0 within 1.0 %, delay +1.0 to +1.2 % |
| 4 | Via inductance, parallel-plate | within 10 % of two posts | normal ✅ (worst +7.7 %); coarse ❌ (+10.1 % at one point). The limit stays; a coarse part with vias says so (below) |
| 5 | Synthetic coupon, 3 margins and 2 presets | hotspot, level 1 dB, \|Z\| 5 %, S21 0.5 dB | ✅ margins and presets |
| 6 | Real coupons, 3 margins and 2 presets | the same | ❌ C ✅ and D ✅; B ✅ on margins, ❌ on level between presets (3.3 dB) |
| 7 | Slow tests (`EMI_SLOW_TESTS=1`) | pass in the image | ✅ the 3 small-part solves, on openEMS 0.0.35 and on the from-source openEMS the released image now builds on |
| 8 | A small-part solve from the Part solve tab | runs end to end | ⚠️ the synthetic board ✅; the sample board's CLK net ran and gave no numbers (it did not settle) |

The hotspot criterion changed in the third pass (September 2026), by the user's decision: a
hotspot is found if it did not move, or if either run's hotspot is within 3 dB of the other
run's peak. Two near-equal spots can trade places between meshes, and either is a spot to fix.
The level criterion still applies. To go with it, a result now lists every separate spot within
3 dB of each map's loudest point away from the ports (`worker/emi_worker/openems/hotspots.py`),
with the nearest net and part, numbered in the result panel and marked on the board.

### 1-2. Microstrip (`sp_verify_lines.py`)

A 382.8 um line on 200 um FR-4 (er 4.4, tan d 0.02), 25 mm long, through the product path.
Hammerstad-Jensen gives 50.00 ohm and eps_eff 3.331. Errors over 0.5-2 GHz:

| Preset | Cells | Wall | End | Z0 | Delay, port to port | Delay, by difference | S21 to 1 GHz |
|---|---|---|---|---|---|---|---|
| coarse | 59,829 | 13 s | -55.1 dB | -0.43 to -0.30 % | -0.48 to -0.34 % | +0.08 to +0.36 % | 0.06 dB |
| normal | 82,173 | 22 s | -52.8 dB | +0.04 to +0.25 % | -0.71 to -0.55 % | -0.08 to +0.18 % | 0.06 dB |
| fine | 102,459 | 47 s | -54.9 dB | -0.01 to +0.45 % | -0.42 to -0.26 % | -0.13 to +0.17 % | 0.06 dB |

**Why the delay read low.** The port-to-port delay assumes the line starts at the port's
center. The same line at 25 and 50 mm, with the delay taken from the phase the two differ by,
removes whatever the ends add: eps_eff is then within 0.4 % on every preset. The ends add -0.14
to -0.18 mm of line (coarse, normal) and -0.07 to -0.11 mm (fine), which is the 0.3-0.7 %. So
the reference plane of a 0.4 mm lumped port sits inside the pad center; the line itself is
right. Dispersion is not it: at 2 GHz on 0.2 mm FR-4, f times h is 0.4 GHz mm, where
Kirschning-Jansen moves eps_eff well under 0.1 %.

### 3. Stripline

191.5 um between planes 415.2 um apart (the strip at 0.48 of the gap), Cohn 50.00 ohm, eps_eff
4.4. Every run settled (-52.1, -50.9 and -50.2 dB in 22, 35 and 79 s). Z0: coarse -1.02 to
-0.25 %, normal -0.19 to +0.44 %, fine -0.14 to +0.41 %. Delay +1.0 to +1.2 % on all three,
which does not move with the mesh, so it is likely the ends again (the strip runs past each
port by half its width). Not checked by difference.

### 4. Via inductance (`sp_verify_via.py`)

A via between two planes 1.5 mm apart beside the port, against two posts between parallel
plates, read at 100-300 MHz. The via is drawn as its drilled barrel, so the closed form uses
the drill.

| Via drill, spacing | Closed form | Coarse | Normal |
|---|---|---|---|
| 0.3 mm, 1.0 mm | 0.925 nH | +7.0 to +10.1 % | -2.5 to +7.7 % |
| 0.3 mm, 2.0 mm | 1.362 nH | +5.8 to +7.4 % | +2.6 to +4.9 % |
| 0.8 mm, 2.0 mm | 1.052 nH | -1.0 to +1.7 % | -0.2 to +1.5 % |

Coarse misses by 0.1 % at 100 MHz on the smallest via at the closest spacing, where the 0.3 mm
barrel is two coarse cells across. So coarse fails this check, and the 10 % limit stays (the
user's decision). What the product does about it: the app starts a part on normal whenever
normal fits the budget, and a part that runs coarse with a via on its nets says, in the set-up
and in the result, "coarse mesh: via inductance can read up to about 10% high"
(`stages/small_part.py`, `COARSE_VIA_NOTE`).

### 5. Convergence, synthetic clock board (`sp_convergence.py`)

Margins of 3, 5 and 7 mm on coarse and normal. Against the 7 mm cut on the same preset: hotspot
0 cells and at most 0.01 dB, |Z_in| 0.2 % worst, S21 0.002 dB. Coarse against normal at each
margin: level 0.53 dB, |Z_in| 0.9-1.0 % median and 3.7 % worst, S21 0.022 dB; the peak moved
along the line, and coarse's hotspot is 0.89 dB below normal's peak there. Pass.

### 6. Real coupons

Three coupons, each at 3, 5 and 7 mm margins on coarse and normal, with the study's budget lifted
(`SP_BUDGET=2e11`; a normal run at 7 mm is over the product's). Each margin is compared with
7 mm on the same preset (the cut), and the presets with each other at each margin (the mesh).
"Swap" is how far the nearer of the two hotspots sits below the other run's peak.

| Coupon | Size at 3 mm | Cells, coarse / normal | Wall, coarse / normal |
|---|---|---|---|
| C, differential pair, 4 layers, 4 ports | 32.3 x 8.5 mm | 517-611 k / 1.01-1.16 M | 45-185 s / 153-325 s |
| D, supply net, 4 layers | 17.8 x 8.5 mm | 432-527 k / 0.92-1.03 M | 52-62 s / 121-237 s |
| B, clock net, 6 layers | 7.0 x 10.2 mm | 183-225 k / 498-607 k | 28-29 s / 73-87 s |

Every run settled (by the runner's criterion; see the end criterion below). Peak memory was
134-273 MB.

| Coupon | The cut, worst of 4 | Presets: hotspot | Level | \|Z_in\| median / worst | S21 | Verdict |
|---|---|---|---|---|---|---|
| C | 0 cells, 0.00 dB, \|Z\| 0.4 %, S21 0.013 dB | 128 cells at 300 MHz, swap 0.1 dB | 0.78 dB | 2.4 / 8.9 % | 0.09 dB | ✅ |
| D | 0 cells, 0.00 dB, \|Z\| 0.1 %, S21 0.004 dB | 1.7 cells | 0.64 dB | 0.6 / 3.1 % | 0.03 dB | ✅ |
| B | 0.2 cells, 0.00 dB, \|Z\| 0.1 %, S21 0.001 dB | 0.45 cells | **3.3 dB** | 0.1 / 2.4 % | 0.006 dB | ❌ |

- **Board C** passes with the new hotspot criterion. At 300 MHz the two ends of the pair are
  0.1 dB apart on normal, so which is the loudest is a coin toss; the result lists both.
- **Board B fails on the level.** The presets agree on where the hotspot is (under half a cell)
  and on the port, but coarse reads the hotspot 3.3 dB below normal. A fine run at 3 mm (1.0 M
  cells, 224 s, budget lifted to 6e11) reads 2.8 dB below normal, so normal is the outlier, not
  coarse: coarse is 0.5 dB below fine. The net is a 0.1 mm trace on 76 um of dielectric and the
  hotspot is 1.8 mm from the driven port, just outside the 1.5 mm the study leaves out around a
  port. A single grid point beside a pad edge, where the field is sharpest, does not settle with
  the mesh. Averaged over a disc of 0.25 mm radius around the hotspot, the three presets read
  19.8, 20.7 and 20.5 dB (coarse, normal, fine): within 0.9 dB. Over 0.5 mm, 16.7, 16.1 and 17.2
  dB: within 1.1 dB.
- **The end criterion can stop on a dip.** Board D's normal 7 mm run said it settled with its
  last energy reading at -41 dB. Re-run with the solver log kept, its energy swings by 10-20 dB
  between progress reports (-49.6, -38.6, then -57.0 dB) and the runner stopped on the -57 dB
  sample while the swing still reached about -39 dB. Its numbers still match the 3 and 5 mm
  runs (\|Z\| 0.0 %), so nothing in this table moves, but "settled at -50 dB" means one sample
  below -50 dB, not the ringing's envelope.

### 8. In the app

`emi-local -experimental small-part-solve`, worker image built from this branch, three threads,
in a browser:

- **The public synthetic clock board** (`clock_board()` in `sp_convergence.py`, uploaded as a
  `.kicad_pcb`): the Part solve tab picked CLK's two ends and chose coarse ("over budget on
  normal"). 0.40 M cells, 32 s, settled at -50.1 dB. The result showed the map, the port table
  (S11 -35.9 to -14.3 dB, S21 -0.09 to -0.27 dB) and, on F.Cu at 100 MHz, three loudest spots
  within 0.4 dB ("The loudest 3 spots are within 3 dB of each other, so treat them together"),
  marked on the board; a row click moves the view there.
- **The sample board** ("Try the sample board"): both nets have one pad, so their far ends are
  open. CLK, coarse (normal is over budget), 1.36 M cells: the set-up estimated "about 2 m, at
  most 16 m"; it ran for 10 minutes, reached its 109,942-step cap with the energy at -48.2 dB,
  and published no numbers. The result said so plainly and showed the coarse-via note. An
  unterminated net rings far longer than the terminated coupons the estimate was fitted to.

Two things were fixed from this pass in the app: the loudest spots were never marked on the
board after a solve (the set-up ports still on screen hid them), and orange markers vanished on
orange copper.

### Which openEMS

Checks 6 and 8 ran on the Debian openEMS 0.0.35. The released image moved to an openEMS built
from source while this pass ran; on it the three small-part slow tests pass (1030 of 1031 in
the suite; the one failure is a far-field test, whose reader does not yet know the new build's
HDF5 layout). The real coupons have not been re-run on it.

## Verdict

**Stays off.** Checks 1-3, 5 and 7 pass; 4 fails on coarse by the rule the user kept, and the
product now says so. What fails:

1. Board B's hotspot level differs by 3.3 dB between coarse and normal (limit 1 dB). Fine shows
   normal is the outlier.
2. The sample board, the first thing a new user solves, gives no numbers: its nets are open at
   one end and ring past the budget.
3. Not a failed criterion but a defect found on the way: the end criterion stops on one sample
   below -50 dB, and on board D that sample sat in a 20 dB swing.

## Still to do

1. **Decide how a hotspot's level is judged** (user decision). Options: (a) the map averaged over
   a probe-sized disc (0.25 mm radius: B's presets within 0.9 dB), which is also closer to what a
   near-field probe reads; (b) keep the single grid point and leave out more around a port (B's
   hotspot is 1.8 mm from it). Then change `sp_metrics.hotspot` (and `openems/hotspots.py`, so
   the listed level is the same number) and re-run `sp_convergence.py` on the three coupons.
2. **End criterion on the envelope.** Stop only when the energy has stayed under the criterion
   for several progress reports (or over a period of the band's lowest frequency), in
   `openems/run.py`. This touches every solve, so re-run checks 1-6 after it.
3. **The sample board.** Either give an open-ended net's far end a 50 ohm port by default (the
   run then rings down like the coupons), or have the set-up warn that an open end rings long and
   estimate it from the open-end case. Then re-run check 8 on it.
4. The browser estimate chose coarse for the synthetic board ("over budget on normal"); check
   `CELL_FIT` against the real normal mesh, since the rule is normal whenever it fits.
5. Optional: the stripline delay by difference, to confirm the +1.2 % is the ends.
6. When 1-3 pass, on the openEMS the released image ships: set `SmallPartSolveByDefault = true` and update the README's experimental
   table, known-issues, `EXPERIMENTAL.smallPart` and the limitations page.

Known limitations that stay after that:

- Neighboring nets are left out of the cut, so coupling into them is not shown.
- No far field, cable emissions or compliance estimate; those need `full-wave`.
- No component models in a small part. Capacitors need an openEMS newer than
  0.0.35 ([solver-and-components.md](solver-and-components.md) §3).
- Coarse mesh: via inductance can read up to about 10 % high.
- Below 100 MHz a part a few centimeters across is quasi-static; a circuit tool gives the
  lumped L or C more cheaply.
- Nothing has been compared with a measurement.

Once the list above is done, flipping `SmallPartSolveByDefault` in `server/emi/features.go` is
the whole change needed to ship it.
