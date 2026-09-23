# Verification: cables and drivers

What was checked for the cable models (Tier A, Tier B) and the drivers, against what reference,
with the numbers. The status rows in [`known-issues.md`](../known-issues.md) §3 point here.

Everything that needs a solver is a script in [`worker/research/`](../../worker/research/) and
runs in the worker image; everything else is in the test suite. Real boards are private and
are called board A, B and C below.

| Check | Criterion | Result | Verdict |
|---|---|---|---|
| Tier A: nec2c against openEMS, product deck | e_per_amp within 1 dB below the first resonance; 2 dB and 3 % at peaks | 7 of 9 within 1 dB (worst 0.96); 1.14 dB and 2.48 dB for the other two; all 9 within 2 dB and one grid step at peaks | ✅ with two stated exceptions |
| Tier A: the integrator that reads openEMS's current | reproduces nec2c's own field from nec2c's current | 0.15 dB worst | ✅ |
| Bond | moves the first resonance where a line over the plane resonates, both ways | within 3.3 % (1 m), 6.7 % (2 m) | ✅ (old claim was wrong) |
| Choke | R + jX from a datasheet curve | built and tested; no library cable has one | ✅ model, not validated against a measured choke |
| Tier B against a fully coupled solve, three real boards | ±6 dB below the first resonance | board A: mean -8.9 dB, worst 17 dB; board C: mean -7.5 dB, worst 21 dB; board B: no result | ❌ |
| Cables tab chart | same composition as the estimate | one function, shared fixtures | ✅ |
| Uploaded waveform joins its envelope | within 1 dB | 0.22 dB worst | ✅ |
| Assumed driver shown as assumed, including σ | end to end | worker result, σ, 80 % range, panel, pickers | ✅ |
| Older result refuses a driver with a re-run message | near field, estimate, Cables chart | all three | ✅ |

---

## 1. Tier A against a second solver

**What is compared.** The product deck, `nec.Deck(feed="board")`: a cable along +x, 0.8 m
over a perfect ground, fed on its first segment against a 0.1 m board arm, read on the scan ring
3 m outside the arrangement at 1-4 m height, keeping the better polarisation at each point and
the worst point on the ring. Three lengths (0.3, 1, 2 m) and the three far ends the library uses
(open; grounded by a drop wire; equipment, the drop carrying 150 ohm). 121 log-spaced points,
30 MHz to 1.2 GHz. Script: `worker/research/verify_cable_tier_a.py`.

**The second solver.** openEMS, with the same wire as a square PEC column one 5 mm cell across,
the ground as a PEC wall at z = 0 (a boundary, not meshed copper), 600 mm of air to the absorbing
boundary on the other five sides, and a 50 ohm lumped port in a one-cell gap where the deck puts
its source. nec2c is run at the column's equivalent radius (0.59 a = 2.95 mm) so both solve one
wire, and again at the product's own 0.5 mm to say how much the radius itself matters.

openEMS cannot hold the ring: it is 3 m and more out and up to 4 m high, and a grid that reached
it at λ/20 of 1.2 GHz would be ~200 M cells. So a current probe sits on every cell of the wire,
and the current is carried to the ring by the exact field of a current element over a perfect
ground (every 1/r, 1/r² and 1/r³ term, plus the image). What is compared is what each solver is
responsible for, the input impedance and the current distribution, read the way the product
reads it.

- **The integrator is not the difference.** Applied to nec2c's own currents it reproduces
  nec2c's own near field within **0.15 dB** in every case (0.05-0.15 dB).
- **The records are long enough.** Each openEMS transform is repeated on the first 80 % of its
  record: within 0.07 dB for seven cases. The two grounded cases that ring longest, 1 m and
  2 m, moved by 0.53 and 0.83 dB, so their openEMS side is short at the lowest frequencies
  (a 40,000-step cap, 385 ns).
- 2.8-5.5 M cells, 18,500-40,000 timesteps, 1-15 minutes each at 3 threads.

**First run: a real disagreement, and it was nec2c's deck.** Below the first resonance the two
agreed within 1 dB for 0.3 m but not beyond: 1.67 dB for 1 m open, 2.59 dB for 1 m equipment,
2.1 to 9.6 dB for 2 m. The disagreement was largest at the low end, and it was the segmentation:
`segments_for` used λ/20 alone, so at 30 MHz a 1 m cable was nine 111 mm segments fed against a
board arm of 11 mm ones, a 10:1 step at the source. Capping every segment at 12.5 mm
(`nec.MAX_SEGMENT_M`, λ/20 at 1.2 GHz) closed it; halving the cap again moved nec2c by less than
0.5 dB more, so the capped deck is converged and openEMS was right. The product now uses it.

**Result, with the capped deck** (e_per_amp, nec2c against openEMS; "below" is below 95 % of the
first resonance, the lower of the first reactance zero and the first radiation peak):

| Cable | Far end | First resonance | Below: worst | Below: \|Z\| worst | Whole band: median / worst | Peaks: level, frequency |
|---|---|---|---|---|---|---|
| 0.3 m | open | 360 MHz | 0.39 dB | 1.10 dB | 0.15 / 1.60 dB | +0.2 to +0.9 dB, one grid step |
| 0.3 m | ground | 67 MHz | 0.08 dB | 0.79 dB | 0.11 / 1.09 dB | +0.4 to +0.7 dB, one grid step |
| 0.3 m | equipment | 67 MHz | 0.81 dB | 0.72 dB | 0.27 / 1.14 dB | +0.2 to +1.0 dB, same point |
| 1 m | open | 140 MHz | 0.23 dB | 0.86 dB | 0.16 / 1.70 dB | +0.5 to +1.1 dB, one grid step |
| 1 m | ground | 42 MHz | 0.40 dB | 0.83 dB | 0.17 / 1.35 dB | +0.4 to +1.3 dB, one grid step |
| 1 m | equipment | 42 MHz | **1.14 dB** (at 40 MHz, on the peak's shoulder) | 0.69 dB | 0.28 / 1.34 dB | +0.5 to +1.2 dB, one grid step |
| 2 m | open | 69 MHz | 0.22 dB | 0.85 dB | 0.17 / 1.35 dB | -0.1 to +0.9 dB, one grid step |
| 2 m | ground | 80 MHz | **2.48 dB** (at 31 MHz; record-limited, below) | 1.24 dB | 0.26 / 2.48 dB | +0.3 to +0.6 dB, same point |
| 2 m | equipment | 80 MHz | 0.96 dB | 0.92 dB | 0.24 / 1.29 dB | +0.2 to +1.2 dB, one grid step |

**Around resonances.** The peaks are the same peaks: each one openEMS finds, nec2c finds at the
same grid point or the next (the grid is 3.1 % per step), and nec2c reads them 0.3-1.3 dB
higher. Twice, for 2 m, openEMS marks a peak that nec2c shows only as a shoulder under the 1 dB
detection threshold. The stated tolerance around resonances is therefore **2 dB in level and
3 % in frequency**, and every case is inside it. The whole-band worst (1.1-1.7 dB) is always on the shoulder of a peak, where a 3 % shift
is worth a dB.

**The radius matters more than the solver.** Between 0.5 mm (the product's) and 2.95 mm, nec2c's
own e_per_amp below resonance moves 0.5-3.0 dB, most for the equipment far end. A real USB or
Ethernet cable is 4-6 mm across, so the product's 0.5 mm is thin; that is a modelling choice to
revisit, not a solver error, and it is now measured.

**The closed form is not a bound for a grounded cable.** Inside its window (cable under λ/10 and
the ring in the far field; 32-99 MHz for 0.3 m, nothing for 1 or 2 m) the closed form with its
6 dB ground reflection is 7.8-14 dB *above* the product deck for an open far end, as documented,
but 4-26 dB *below* it for a grounded or equipment far end: the drop wire is vertical, carries
the full current, and radiates as a monopole over the plane, which a horizontal uniform-current
formula does not contain. Tier A does not use the closed form; `budget.budget()` should not be
read as conservative for those far ends.

**Verdict.** Seven of nine configurations meet the 1 dB gate below the first resonance, and all
nine meet 2 dB and one grid step around the peaks. The two exceptions are understood well enough
to say where the doubt is: 1 m equipment misses by 0.14 dB at 40 MHz, the last point before its
42 MHz peak, where a one-step peak shift is worth that much; 2 m grounded misses at 31 MHz, the
lowest point, in the case whose openEMS record is demonstrably short there (0.83 dB between
80 % and 100 % of it). A longer record for that case was not run. Tier A can be
called **verified against a second solver for this geometry**, with two limits stated: the
comparison is at a 2.95 mm wire, and the radius itself moves the answer by up to 3 dB (above).


---

## 2. The bond test could not fail

`nec.Deck.bond_nh` wrote `LD 4 2 1 1 0 X`: a series reactance on segment 1 of tag 2, the board
arm. Segment 1 of the board arm is its open far end, where the current is zero, so the element
moved nothing. The test "a bond never lengthens the first resonance" compared two identical
structures and passed. Measured with the old deck, 1 m cable: 136.4 MHz unbonded, 136.4 MHz
with a 10 nH bond; open or grounded far end alike.

**The model now.** A bond is a wire from the junction (the board side of the feed) down to the
ground plane, with the inductance in its top segment. The feed stays on segment 1 of the cable,
so the source drives the cable against a grounded board.

**What that shows.** The old claim was wrong for half the library. A bond turns the dipole
(cable against a floating board) into a transmission line over the plane, with the strap as its
feed drop, and the first series resonance moves to where cable test 2's transmission-line
formula puts it:

| Cable | Far end | Floating | Bonded (0 nH) | Line theory | Error |
|---|---|---|---|---|---|
| 1 m | open | 139.8 MHz | 41.9 MHz | 41.6 MHz, c/4(L+H) | 0.7 % |
| 1 m | ground | 39.8 MHz | 59.6 MHz | 57.7 MHz, c/2(L+2H) | 3.3 % |
| 2 m | open | 67.9 MHz | 27.4 MHz | 26.8 MHz | 2.2 % |
| 2 m | ground | 26.0 MHz | 44.4 MHz | 41.6 MHz | 6.7 % |

So a bond lowers an open cable's first resonance by 2-3x and raises a grounded one's by about
1.5x. Adding inductance to the strap lowers the resonance monotonically (1 m grounded: 59.6,
58.9, 53.8 MHz at 0, 100, 1000 nH). At 0.3 m the table height (0.8 m) is longer than the cable
and the line model does not hold; the test uses 1 m and 2 m.

The strap is the table height long, about a microhenry by itself, so a bond inductance of tens
of nanohenries changes little. That is what a bond to the floor plane can do; a bond to a
chassis beside the board is not modelled.

`test_cable_3_a_bond_turns_the_cable_into_a_line_over_the_plane` asserts the direction and
size of the move (more than 25 %) and the line-theory value (8 %) for both far ends; the old
deck fails it. `bond_nh` is not used by any product path; no library cable declares a bond.

## 3. The choke

It was a pure resistance, `z_ohm_at_100mhz · f / 100 MHz`. A ferrite is inductive below its
peak, resistive at it and capacitive above, so that is roughly right on the rising side and wrong
above the peak, where it keeps climbing (10x the 100 MHz figure at 1 GHz).

A cable document can now give the choke's datasheet curve, `cm_choke.impedance`: points of
frequency, R and X. The deck loads the cable's first segment with R + jX interpolated linearly
in log frequency, held at the end values outside the curve. The one-number form stays, as a
documented simplification, and a cable run that uses one says so in its notes. Tier B's antenna
terms now include the choke too; they ignored it before, so a choked cable's emission was the
same as an unchoked one's.

Not validated: no library cable has a choke, and nothing has been compared with a measured one.
"A choke never raises the current" is tested for resistive chokes only. A reactive one can raise
it where it cancels the cable's own reactance, which is real.

## 4. Tier B against a fully coupled solve on real boards

**What is compared.** `worker/research/spike_m3_cable_test4.py`, two solves on one shared grid
per board: Tier B (the product model: a 10 mm stub, a 1 MΩ gap, H_cm = V_oc / V_src, composed
with nec2c's Z_ant) and Tier C (the gap bonded and a PEC cable in the grid, its current probed
at the root). The gate: Tier B's cable current within ±6 dB of Tier C's below the first
resonance. Three private boards, each with an edge connector: board A (4 layers, USB-C),
board B (6 layers, USB-C), board C (4 layers, RJ45); a 0.3 m cable; 30-600 MHz, 31 points; the
2000/300 µm study preset; 80 mm of air around everything.

**Three things had to be fixed before it could run at all**, each found by running it:

- **The cable ran next to the absorbing boundary.** The grid stopped 5 mm above and below the
  copper and at the edge of a 30 mm strip, so Tier C's cable was a wire 5-15 mm from a PML for
  its whole length while nec2c's Z_ant is a wire in free space. `AIR_MM` now pads every side.
- **The grid broke its own grading along the cable.** The mesher filled the cable's air at
  2.4 mm cells and stepped to 11.5 mm at the padding, 4.8:1 on the cable's own axis (6.8:1 at
  the 1000 µm preset); the first run diverged. The study re-grades that axis past the stub
  (1.3x per cell to λ/20). The mesher bug itself is filed separately.
- **The divergence check fired inside the excitation.** A 30-600 MHz pulse is several lobes
  and about 9 ns long; a board strip with a stub empties between lobes, the energy fell 20 dB,
  the check decided the excitation had passed, and the next lobe "climbed back 2,950x". openEMS's
  own end criterion stopped the run at the same moment. The study now runs a fixed 40 ns record
  and judges stability from the energy instead (a gap probe alone can end 10 dB below its peak
  in a run whose energy is 60 dB down). The worker's check now waits for the excitation to end.

**Results.**

| Board | Below the first resonance | Whole band | Record check | Verdict |
|---|---|---|---|---|
| A | median 9.9 dB, worst 27.4 dB; 45 MHz up to the 403 MHz resonance: median 9.6, worst 17.4, **mean -8.9 dB** | median 9.5 dB | 17 dB (below 45 MHz) | ❌ |
| B | not run to a result: its Tier B energy had fallen only 37 dB in 40 ns (6 layers, 2.3 M cells, 43 minutes a run), and it was stopped there | | | ❌ |
| C | 45 MHz up to the 356 MHz resonance: median 5.6, worst 21.2, **mean -7.5 dB**; from 80 MHz: median 2.7, worst 12.0 | median 6.4 dB | 11 dB (below 80 MHz) | ❌ |

**What boards A and C say.** The error is not noise around zero. On board A, from 45 MHz to 330 MHz Tier B is
6-15 dB **below** Tier C at every point, and at the resonance (402 MHz) the two agree to 1.2 dB.
Board C has the same shape: 2-11 dB low from 81 to 270 MHz, 0.65 dB at 330 MHz next to its
356 MHz resonance.
That is the signature of a wrong antenna impedance rather than of coupling outside the gap:
where |Z_ant| is large and capacitive (1-6 kΩ) the composition divides by too much, and where it
is small (250 Ω at resonance) the difference vanishes. The likeliest cause is the other arm:
nec2c models the board as a thin wire as long as the board, while in the grid it is the board
itself, a strip of copper planes with far more capacitance. A thick-wire board arm in nec2c did
not reproduce it (nec2c is unreliable where wires of different radius meet), so the cause is a
hypothesis, not a finding. Below 45 MHz (board A) and 80 MHz (board C) the 40 ns record is too
short (11-17 dB between 80 % and 100 % of it), so those points say nothing either way. The gap
itself is part of why: its 1 MΩ holds the charge the pulse's DC content leaves on it and bleeds
it off over about 100 ns.

The direction is the dangerous one: Tier B under-predicts the current, so an emission it shows
under the limit can be over it.

**Verdict.** The gate is not met. Tier B stays experimental. What would move it: measure the
Tier C antenna directly (Z at the bonded gap, from the same grid) against nec2c's Z_ant, which
separates "wrong antenna" from "coupling outside the gap"; if it is the antenna, model the board
arm from the outline (a plate or wire grid) in the nec2c deck; then rerun at 60-100 ns records
on all three boards, and at 1 m.


## 5. The Cables tab chart

It composed on the solve's own frequency grid, so a clock drove only the harmonics that
happened to land on a grid point, and it took the driver's source impedance to be the port's.
(It also read `cable_ports.json` in the wrong shape and so drew nothing at all; that was fixed
separately, and `parseCablePorts` now keeps the `driven_by` the composition needs.)

There is now one composition, `compose_cable` in `worker/emi_worker/compliance/assemble.py`:
every harmonic in the band, transfer functions interpolated between grid points (magnitude in dB
against log frequency, impedances linearly), and the `|Z_s + Z_in| / |Z_d + Z_in|` factor for
the driver's own source impedance. The compliance estimate uses it, the chart uses its
TypeScript copy (`composeCable` in `webapp/src/lib/cableEmission.ts`), and
`server/emi/testdata/cable_emission_fixtures.json` pins both to 1e-12. The grid composition and
its fixtures are gone.

A solve without the port records a driver needs refuses one with "Re-run this solve to see
cable emissions with a driver".

Not checked in a browser against a live solve with a cable port: that needs the `full-wave`
experimental feature and a real run.

## 6. Drivers

**An uploaded waveform joins its envelope within 1 dB.** Above its bandwidth a capture continues
along the envelope implied by its rise time, scaled to meet the capture. The scale came from the
single highest *requested* harmonic below the join. With the join just above an even harmonic of
a square wave, that harmonic is a null: the scale was 7.5e-16, and every harmonic above the
bandwidth resolved to nothing. It also depended on which frequencies the caller asked for, so
the chart and the estimate could disagree. The scale is now the largest line-to-envelope ratio
over the capture's top octave, read from the capture itself. Against a 25 MHz, 3.3 V trapezoid
with a 1 ns edge, sampled at 50 ps and uploaded as a waveform, every harmonic above the join is
within 1 dB of the trapezoid's envelope:

| Duty | Bandwidth | Worst above the join | Old scale |
|---|---|---|---|
| 50 % | 210 MHz (join on a null) | 0.22 dB | -302 dB |
| 50 % | 180 MHz | 0.22 dB | -0.4 dB |
| 30 % | 210 MHz | 0.22 dB | -1.0 dB |
| 50 % | 600 MHz (past the rise corner) | 0.03 dB | -281 dB |

`test_an_uploaded_waveform_joins_the_envelope_within_1_db`, plus a shared fixture
(`waveform-join-on-a-null`) that Python and TypeScript both resolve.

**An assumed driver is assumed everywhere.** The compliance result's inputs now carry the
driver's weakest source, its σ and which values were assumed, whether or not the estimate got
as far as a margin. Its σ term (`driver provenance (assumed)`, 6 dB) is in the budget and so in
the 80 % range. The Compliance panel shows it on a badge; every driver picker (Compliance, the
near-field attach, the Cables chart) marks an assumed driver before it is chosen; the Drivers tab
and the attach badges already did. Tested end to end through the compliance stage, including
that a measured driver carries its own smaller term.

**An older result refuses a driver with a re-run message.** Near field: a version-1 solve
("Re-run this solve to attach a driver"). Estimate: no port resistances ("Run the solve
again"), or a far field in pulse units. Cables chart: no port records (§5). Each is tested.

## Reproducing

```sh
# Tier A, nine configurations (about two hours at 3 threads)
docker run --rm --cpus 3 -m 6g -v "$PWD/worker:/spike" -v "$PWD/out:/out" -e OUT=/out \
    -w /spike -e PYTHONPATH=/spike --entrypoint python3 emi-worker:phase1 \
    research/verify_cable_tier_a.py

# Tier B, your own boards
docker run --rm --cpus 3 -m 6g -v "$PWD/worker:/spike" -v "$BOARDS:/boards:ro" \
    -v "$PWD/out:/spike/spike_out" -e SETUPS=<folder:ref:cable,...> -e LENGTHS=0.3 \
    -e DX_UM=2000 -e DZ_UM=300 -e AIR_MM=80 -w /spike -e PYTHONPATH=/spike \
    --entrypoint python3 emi-worker:phase1 research/spike_m3_cable_test4.py
```
