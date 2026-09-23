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

## Checks

Scripts are in `worker/research/sp_*.py` and run in the worker image (the docstring of each has
the command). `worker/tests/test_small_part_solve.py` runs the coarse cases with
`EMI_SLOW_TESTS=1`; it has **not** been run yet.

| # | Check | Criterion | Result |
|---|---|---|---|
| 1 | 50 ohm microstrip, Z0 and delay | within 5 % of Hammerstad-Jensen | ✅ coarse and normal presets |
| 2 | Same microstrip, matched S21 | within 0.5 dB of the closed form to 1 GHz | ✅ 0.06 dB worst |
| 3 | 50 ohm stripline, Z0 and delay | within 5 % of Cohn | ❌ not run validly (script bug, fixed, not re-run) |
| 4 | Via inductance | within 10 % of a closed form | ❌ old setup 57-59 % high; new setup not run |
| 5 | Convergence over 3 coupon margins and 2 presets | hotspot, \|Z\| and S21 agree | not run |
| 6 | Coupons from real boards | converge, nothing dropped, presets agree | ❌ one of three compared, and it failed |

### 1-2. Microstrip (`sp_verify_lines.py`)

A 382.8 um line on 200 um FR-4 (er 4.4, tan d 0.02), 25 mm long, drawn as a KiCad board and put
through the product path: coupon cut, ports, default band and end criterion. Hammerstad-Jensen
gives 50.00 ohm and eps_eff 3.331 (IPC-2141: 49.2 ohm, printed as a sanity bound).

| Preset | Cells | Steps | Wall time | End | Z0 error, 0.5-2 GHz | Delay error | S21, worst to 1 GHz |
|---|---|---|---|---|---|---|---|
| coarse | 59,829 | 13,950 | 13 s | -55.2 dB | -0.30 to -0.43 % | -0.35 to -0.48 % | 0.06 dB |
| normal | 82,173 | 22,644 | 22 s | -53.3 dB | +0.04 to +0.25 % | -0.55 to -0.71 % | 0.06 dB |

No frequency was dropped. The delay is short on both presets, so eps_eff reads about 0.7 to
1.4 % low. That is inside the criterion, but it is consistent, not noise.

An earlier run of the same script on an uncommitted state of the branch read Z0 10.6 to 12.4 %
low, with the delay within 1.1 %. The committed code gives the numbers above; the log does not record
what changed between the two.

### 3. Stripline

The one run used a 10 mm wide strip: the harness's Cohn formula had its two elliptic integrals
swapped, so the width search ran to its upper bound. That run also did not settle (-30.6 dB
after 217,161 steps, 249 s on coarse) and was stopped. The formula is fixed (a 50 ohm strip
between planes 415 um apart is now 191 um wide); the check has not been re-run.

### 4. Via inductance (`sp_verify_via.py`)

The first form of the check drove a pad joined by a strap to a via to the plane, and compared
the difference between a 0.3 mm and a 1.0 mm via with Goldfarb and Pucel's via-to-ground
formula (0.279 nH). The solve read 0.438 to 0.441 nH on coarse (+57 to +58 %) and 0.443 to
0.445 nH on normal (+59 %). Both presets agreed with each other, so this is the setup (a strap
junction and a loop the formula does not model), not the mesh.

The script was rewritten to a via between two planes beside the port, against the exact
two-post parallel-plate formula, with a 10 % criterion. That version has not been run.

### 5. Convergence (`sp_convergence.py`)

Written, not run. It cuts the same net at margins of 2, 4.5 and 7 mm on coarse and normal, and
compares each with the largest: hotspot within two cells or 1 dB, level within 1 dB, |Z_in|
within 5 % (median), S21 within 0.5 dB. It uses a public synthetic four-layer clock board.

### 6. Real-board coupons (`sp_real_coupons.py`)

| Coupon | Size | Ports | Preset | Cells | Steps (of cap) | Wall | Peak RAM | End | Dropped |
|---|---|---|---|---|---|---|---|---|---|
| Board C, a differential pair, 4 layers | 32.3 x 8.5 mm | 4 | coarse | 435,968 | 21,824 of 204,480 | 111 s | 130 MB | -50.1 dB | none |
| | | | normal | 869,466 | 44,682 of 389,442 | 302 s | 216 MB | -50.1 dB | none |
| Board D, a supply net, 4 layers | 17.8 x 8.5 mm | 2 | coarse | 319,488 | 15,752 of 207,703 | 44 s | n/a | -58.3 dB | none |
| Board B, a clock net | | | coarse | no result | | | | | |

Peak RAM is the largest openEMS process so far in the script, so board D's reading was board
C's and is left out. Cost is where it was meant to be: minutes, a few hundred MB, and every run
that finished settled well inside its timestep cap. The answers are not yet trustworthy: on board C the two
presets disagree. The hotspot moved 145 cells and 4.6 dB, |Z_in| differs by 5.2 % median and
188 % at worst, and S21 by 2.4 dB. The board D normal run and the board B run did not finish.
Board C's coarse run also warned that its coarsest cell (5.4 mm) was over the 3.5 mm the top
of the band needs, which the band was chosen to avoid.

## Still to do

Before `small-part-solve` can be turned on, all of these must be done and pass:

- **Stripline** (check 3): re-run with the fixed formula on both presets.
- **Via** (check 4): run the parallel-plate version.
- **Convergence** (check 5): run it on the synthetic board, then on real coupons.
- **Real boards** (check 6): find why board C's presets disagree (likely the under-resolved
  coarse cells above, or the hotspot sitting on a flat line), then finish boards B and D and
  add more nets. At least three real coupons should pass.
- **Slow tests**: run `test_small_part_solve.py` in the worker image, and put it in CI or in
  the release checklist.
- **The coarsest cell**: make the mesher hold the band's cell limit on a real coupon, or refuse
  the band.
- **Microstrip eps_eff**: find why it reads 0.7 to 1.4 % low on every preset.
- **In the app**: no small-part solve has been started from the Part solve tab end to end; that
  needs a run on the desktop app and in the browser. A drawn region needs its ports placed by
  hand, and the cost estimate has only been checked in unit tests.

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
