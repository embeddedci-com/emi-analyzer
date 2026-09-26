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
| 4 | Via inductance, parallel-plate | within 10 % of two posts | ⚠️ normal ✅ (worst +7.7 %); coarse ❌ by 0.1 % (+10.1 % at one point) |
| 5 | Synthetic coupon, 3 margins and 2 presets | hotspot, level 1 dB, \|Z\| 5 %, S21 0.5 dB | ✅ margins and presets |
| 6 | Real coupons, 3 margins and 2 presets | the same | ⚠️ board C: margins ✅, presets ❌ on hotspot location only; boards D and B not run |
| 7 | Slow tests (`EMI_SLOW_TESTS=1`) | pass in the image | not run |
| 8 | A small-part solve from the Part solve tab | runs end to end | ✅ once, before the diagonal fix; not re-run |

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
barrel is two coarse cells across.

### 5. Convergence, synthetic clock board (`sp_convergence.py`)

Margins of 3, 5 and 7 mm on coarse and normal. Against the 7 mm cut on the same preset: hotspot
0 cells and at most 0.01 dB, |Z_in| 0.2 % worst, S21 0.002 dB. Coarse against normal at each
margin: level 0.53 dB, |Z_in| 0.9-1.0 % median and 3.7 % worst, S21 0.022 dB; the peak moved
along the line, and coarse's hotspot is 0.89 dB below normal's peak there. Pass.

### 6. Real coupons

Board C, a differential pair, 4 layers, 32.3 x 8.5 mm at the default 3 mm margin, 4 ports.
Before the fixes the presets disagreed by 2.2 dB on level, 5.6 % median and 1534 % worst on
|Z_in| and 1.4 dB on S21, because neither was a through line (the via short above). After:

| Run | Cells | Steps | Wall | End |
|---|---|---|---|---|
| coarse, 3 / 5 / 7 mm | 516,780 / 582,768 / 611,010 | 19,074 / 24,684 / 68,816 | 45 / 65 / 183 s | -51.4 / -50.2 / -50.1 dB |
| normal, 3 / 5 mm | 1,013,880 / 1,105,896 | 37,730 / 35,698 | 145 / 154 s | -50.1 / -51.7 dB |

- The cut: 3 and 5 mm against 7 mm on coarse, and 3 against 5 mm on normal, all pass (0 cells,
  0.00 dB, |Z_in| 0.3 % worst, S21 0.011 dB).
- The presets, at 3 and at 5 mm: level 0.78 dB, |Z_in| 2.4 % median and 8.9 % worst, S21
  0.09 dB, all inside the criteria. The hotspot location fails: at 300 MHz normal's loudest
  point is at the driven end and coarse's at the far end, and coarse's is 2.7 dB below
  normal's peak. Both are the jogs beside the ports, within a few dB of each other.

The normal 7 mm run was stopped when this pass ended. Its budget is over the product's (1.16 M
cells buys 9.7 ns, under the 10 ns minimum), so the study lifted it (`SP_BUDGET=2e11`); no
product run is affected. Boards D (a supply net, 17.8 x 8.5 mm) and B (a clock net, 7.0 x
10.2 mm, 6 layers) were not run on the final code. The earlier board D and B numbers were made
before the via and diagonal fixes and are withdrawn.

### 8. In the app

`emi-local -experimental small-part-solve` with the worker image built from this branch (vias,
PML and map height fixed, the diagonal fix not yet in): the sample board's CLK net, coarse,
0.68 M cells, finished in about two minutes with its map and port table. Two things were fixed
from it: the band-edge and "no components" notes, and the normal preset being refused on the
sample board's first net. It has not been re-run since the diagonal fix.

## Still to do

Where this pass stopped: the convergence study was on board C's normal 7 mm run. To finish:

1. Re-run the study on the three real coupons with the final code:
   `MARGINS=3,5,7 PRESETS=coarse,normal SP_BUDGET=2e11 SETUPS=<C pair>,<D supply>,<B clock>`
   (`sp_convergence.py`, about an hour on three threads).
2. **Board C's hotspot location.** Decide whether a peak that swaps between two near-equal
   spots at the two ends is a failure. If the criterion stays, look at the jogs beside the ports
   on coarse; if it changes, change it in `sp_convergence.passes` and say why here.
3. **Coarse via** (+10.1 % at one point): either accept a coarse tolerance or put a finer cell
   under small vias.
4. Run `EMI_SLOW_TESTS=1 pytest tests/test_small_part_solve.py` in the image, as
   `.github/workflows/test.yml` runs the suite, and add it to the release checklist.
5. Rebuild the worker image and run a small-part solve from the Part solve tab again, on coarse
   and on normal where it fits.
6. Optional: the stripline delay by difference, to confirm the +1.2 % is the ends.
7. Only when 1-5 pass: set `SmallPartSolveByDefault = true` and update the README's experimental
   table, known-issues and `EXPERIMENTAL.smallPart`.

Known limitations that stay after that:

- Neighboring nets are left out of the cut, so coupling into them is not shown.
- No far field, cable emissions or compliance estimate; those need `full-wave`.
- No component models in a small part. Capacitors need an openEMS build newer than the shipped
  0.0.35 ([solver-and-components.md](solver-and-components.md) §3).
- Below 100 MHz a part a few centimeters across is quasi-static; a circuit tool gives the
  lumped L or C more cheaply.
- Nothing has been compared with a measurement.

Once the list above is done, flipping `SmallPartSolveByDefault` in `server/emi/features.go` is
the whole change needed to ship it.
