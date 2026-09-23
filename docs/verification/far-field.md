# Verification: the board's far field

What was checked, how, the numbers, and what did not pass. Run in September 2026 in the worker
image (openEMS 0.0.35, nec2c), three solver threads per run. The scripts are in
[`worker/research/`](../../worker/research/) and write JSON next to what they print; the fast
checks are unit tests in [`worker/tests/test_scan.py`](../../worker/tests/test_scan.py), and one
slow one, [`test_far_field_solve.py`](../../worker/tests/test_far_field_solve.py), runs a real
solve when `EMI_SLOW_TESTS=1`.

**Summary.** The far field was wrong in two independent ways, both now fixed and both now
guarded. With them fixed it matches nec2c within 0.1 dB on short dipoles from 30 MHz to 850 MHz
and within about 1 dB on half-wave dipoles up to their resonance, and a small loop matches its
closed form within 0.2 dB. What is left is at the edges: the top of the solved band, above a
resonance, and the fact that a real board's level has no independent reference.

| Check | Pass criterion | Result |
|---|---|---|
| Short dipole over ground vs nec2c, scan 1-4 m at 3 m | within 1 dB, 30 MHz-1 GHz | ✅ 30-850 MHz, worst 0.22 dB. ❌ 1 GHz, -1.8 dB: the top of the excitation band (✅ 0.04 dB with the band moved up) |
| Half-wave dipole over ground vs nec2c | within 1 dB where both are valid | ⚠️ 30-350 MHz within 1.0 dB (worst -1.01 dB); 1.2-1.3 dB at 400 MHz, where the two solvers disagree on the antenna itself |
| Free-space directivity, short dipole | 1.76 dBi | ✅ 1.75-1.79 dBi, 30 MHz-1 GHz |
| Free-space directivity, half-wave dipole | 2.15 dBi | ✅ 2.15 dBi at resonance (280 MHz) |
| Ground plane 0.8 m below the box vs image theory | within 1 dB | ✅ exact images of an analytic dipole within 0.05 dB (unit test); nec2c over a perfect plane as above |
| Per-volt slope, small loop and short dipole | matches theory, 30 MHz-1 GHz | ✅ loop 36.3 dB/decade below 100 MHz (theory 36.3), 20.3 above 400 MHz (theory 21.6); dipole 40.3 (theory 40.5) |
| The fixture board's slope | at least 40 dB/decade, capacitive port | ✅ 39.1 dB/decade below 200 MHz, 41.4 overall (was 22.6) |
| A real board | runs; cost measured | ⚠️ runs: 7.9 M cells, 2 h 38 min, 1.8 GB, 47 KB artifact. ❌ the -40 dB record is incomplete at 57 of 60 frequencies; now detected and dropped (§5) |

---

## 1. Why the fixture board's far field rose 20 dB/decade

**Symptom.** On the fixture board (`tiny.kicad_pcb`, a 26 x 14 mm region, the port on the clock
pad over a floating ground pour), the far field per volt of source rose 22.6 dB/decade below
200 MHz while the port was capacitive (|Z_in| falling as 1/f). A structure that small fed
through a capacitance has current proportional to f and field proportional to f times current:
at least 40 dB/decade.

**Two faults, each enough on its own.** The same solve's dumps were re-read four ways
(`research/ff_fixture_decompose.py`), once from a solve with the old box and once from a solve
with the new one:

| Box | Read with | Slope below 200 MHz | E per volt at 30 MHz |
|---|---|---|---|
| open (old) | `nf2ff`, 3 m sphere, `nf2ff` PEC mirror (**production until now**) | 22.6 dB/decade | 1.31e-6 V/m |
| open (old) | `nf2ff`, no mirror | 31.0 | 2.98e-7 |
| closed | `nf2ff`, 3 m sphere, `nf2ff` PEC mirror | 24.8 | 1.14e-6 |
| closed | `nf2ff`, no mirror | 40.3 | 1.36e-7 |
| closed | scan positions, image currents (**production now**) | 39.1 | 1.85e-7 |

1. **`nf2ff`'s PEC mirror images horizontal currents wrongly.** Measured on a horizontal 40 mm
   dipole 0.8 m over the plane: the transform with its mirror read 18.7 dB above nec2c at
   30 MHz, and 16.8 dB above the same dipole with no plane at all, which no image can do (an
   image at most doubles the field). A vertical dipole it gets right (0.2 dB), which is the only
   case the early spikes checked. A board's currents are all horizontal. The mechanism shows in
   the slope: the quasi-static field near a small capacitive source is proportional to the
   voltage and does not depend on frequency; a surface integral in which its electric and
   magnetic parts do not cancel radiates it at one power of f, which is the 20 dB/decade seen.
2. **The box was open.** Faces were recorded every 4th grid line. openEMS strides from a dump
   box's first line and drops whatever does not fit the stride at the far end, so on the fixture
   every face stopped 13.6-13.8 mm short of its neighbours: a slot round every edge of the box.
   The same leak, the same f^1 term. Nothing checked what the dumps actually sampled.

The record length is not a cause: running the fixture to -61 dB instead of the production -46 dB
moved the old slope from 22.6 to 23.9 dB/decade.

**Fixes.**

- The level is now computed in [`openems/scan.py`](../../worker/emi_worker/openems/scan.py): the
  box's equivalent currents, integrated with the full free-space Green's function at the scan's
  antenna positions, plus their image (horizontal J and vertical M reversed). `nf2ff` still runs,
  without a mirror, as a guard: in the far-field limit the two integrate the same dumps and agree
  to within 0.001 dB on the dipoles and 0.0002 dB on the fixture board, and a far field where
  they differ by more than 0.5 dB is refused.
- Faces are thinned with openEMS's `OptResolution` (5 mm, or λ/20 if finer), which keeps both
  ends, and snapped outward to grid lines; `scan.read_surface` refuses a box whose faces do not
  meet. On a short dipole 5 mm agrees with the full grid to 0.03 dB; the 4th-line stride, even
  when it happened to close, read 0.35 dB high.
- `farfield.json` is format 3; the compliance estimate refuses format 2 with a re-run message.

---

## 2. Dipoles over ground against nec2c

**Method** (`research/ff_verify_dipole.py`). A centre-fed dipole of square section, built with
the production port (a lumped R with a soft source, the same voltage and current probes), the
production excitation band and record length, box placement and face sampling, solved by openEMS
in free space. Read with `scan.field` at the scan's positions: 24 azimuths, 1-4 m in 0.1 m steps,
3 m outside the dipole's plan boundary, ground 0.8 m below the dipole. The same wire (equivalent
radius 0.59 x side) 0.8 m over a perfect plane (`GN 1`) in nec2c, with a near-field card at every
one of those points. Compared as the scan's maximum reading per amp of feed current: the FDTD
dipole has no plane under it and nec2c's does, so their feed impedances differ by the plane's
coupling, and per amp removes that.

**Short dipole, 40 mm, 1 mm square** (0.13 λ at 1 GHz). Scan maximum, FDTD minus nec2c:

| MHz | 30 | 60 | 100 | 200 | 300 | 500 | 700 | 850 | 1000 |
|---|---|---|---|---|---|---|---|---|---|
| vertical | +0.03 | +0.03 | +0.04 | +0.06 | +0.07 | +0.09 | +0.07 | -0.22 | -1.75 |
| horizontal | +0.19 | +0.17 | +0.13 | +0.06 | +0.05 | +0.09 | +0.07 | -0.21 | -1.77 |
| vertical, band to 1.5 GHz | +0.04 | +0.03 | +0.02 | -0.02 | -0.04 | -0.04 | -0.01 | -0.01 | -0.04 |

The worst single antenna height is within 0.06 dB of these. The 1 GHz miss is the top
of the solved band, not the transform: moving the excitation band and the mesh up to 1.5 GHz
(`F_HI=1.5e9`) takes it from -1.75 to -0.04 dB. A solve whose top frequency is the top of the
scan sits exactly there; the solve already warns that its band edges carry extra uncertainty.

**Half-wave dipole, 500 mm, 5 mm square** (resonant near 280 MHz; compared to 450 MHz, below the
full-wave antiresonance where the feed current collapses):

| MHz | 30 | 40 | 60 | 100 | 150 | 200 | 250 | 280 | 300 | 350 | 400 | 450 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| horizontal | -0.89 | -1.01 | -0.81 | -0.83 | -0.62 | -0.35 | -0.09 | +0.08 | +0.29 | +0.99 | +1.27 | +0.32 |
| vertical | -0.74 | -0.71 | -0.94 | -0.75 | -0.59 | -0.38 | -0.06 | +0.17 | +0.36 | +0.80 | +1.16 | +0.96 |

This is the antenna model, not the far field. Halving every cell (`REFINE=2`) moves no point by
more than 0.2 dB, running to -60 dB instead of -43 dB moves none by more than 0.15 dB, and the
same code reads the short dipole to 0.1 dB. The two solvers simply disagree about this wire: the
FDTD one's reactance is 15 % lower at 30 MHz (a 5 mm square staircase against a thin-wire
cylinder) and its resistance 32 % higher at 400 MHz. Per amp of a current both solvers put in a
different place, they differ by up to 1.3 dB.

**Old path against the same nec2c reference.** The v2 far field (3 m sphere, `nf2ff` mirror) read
the short vertical dipole up to 7.1 dB high (250 MHz) and the half-wave vertical up to 7.7 dB
high; the short horizontal 18.7 dB high at 30 MHz and 2.9 dB low at 1 GHz.

**Directivity**, from a full-sphere free-space `nf2ff` transform of the same dumps: the short
dipole 1.76 dBi from 30 to 300 MHz, 1.77-1.79 up to 850 MHz, 1.75 at 1 GHz (theory 1.76); the
half-wave dipole 2.15 dBi (horizontal) and 2.14 (vertical) at its 280 MHz resonance (theory 2.15).

**The plane's coupling back onto the antenna**, which the product leaves out (the board is solved
in free space): nec2c's feed impedance over the plane against free space differs by under 0.1 Ω
on the short dipole, and by up to 11 % on the half-wave one (450 MHz).

---

## 3. Slope through the production port

**Method** (`research/ff_verify_slope.py`). Both structures in free space, read broadside at 3 m
with `scan.field`, per volt of the port's Thévenin source.

**A 20 mm square loop**, 1 mm wire: the field of a magnetic dipole, E = η k² I A / (4π r)
|1 + 1/(jkr)|, and a current of V / (50 + jωL), with L = 44.0 nH from Grover's closed form for a
square loop. Nothing is taken from the solve.

| MHz | 30 | 60 | 100 | 150 | 200 | 300 | 400 | 500 | 700 | 1000 |
|---|---|---|---|---|---|---|---|---|---|---|
| per volt vs closed form, dB | -0.16 | -0.15 | -0.12 | -0.04 | +0.04 | +0.21 | +0.35 | +0.47 | +0.73 | -0.26 |
| per amp vs closed form, dB | -0.20 | -0.21 | -0.22 | -0.20 | -0.17 | -0.02 | +0.23 | +0.54 | +1.36 | +1.78 |

The solve's own inductance is 42.0 nH at low frequency against 44.0 closed form. Above 500 MHz the
loop's 80 mm perimeter is no longer small (0.27 λ at 1 GHz) and the small-loop formula stops
applying. Slope per volt below 100 MHz: 36.3 dB/decade, theory 36.3 (40 from the magnetic dipole,
less the 50 Ω/jωL roll-off and the near-field term); above 400 MHz 20.3, theory 21.6.

**A 40 mm dipole**, capacitive throughout: per-volt slope 40.3 dB/decade against 40.5 for K·f²
with the dipole's near-field factor; residual from that curve within 0.42 dB up to 700 MHz, -0.93
at 1 GHz (the band edge again).

---

## 4. The ground plane 0.8 m below the box

Unit tests (`test_scan.py`) write the twelve dumps openEMS would write for a Hertzian dipole's
exact fields, read them back through `scan.read_surface`, and compare with the closed form: at
3 m, with no plane, within 0.05 dB at 30 MHz, 300 MHz and 1 GHz for vertical and horizontal
dipoles; over a plane 0.8 m below, against the exact two-dipole image sum at scan positions
1-4 m up, within 0.05 dB for both orientations. An open box, a frequency the dumps did not
record, and a plane inside the box are refused. §2 is the same check on real solves against an
independent solver.

---

## 5. A real board

**Method** (`research/ff_real_board.py`). The production solve stage, far field on, 30 MHz-1 GHz,
on board A (4 layers, 100 x 80 mm, from the private boards checkout, mounted read-only): a 15 mm
square region around a 50 MHz reference-clock pad, the port on that pad. Three solver threads, in
a container capped at 6 GiB, with two other agents' solves on the same machine.

**Preset 300 µm / 150 µm (dx / dz).**

| | |
|---|---|
| Region, mesh | 15 x 15 mm, 300 µm / 150 µm |
| Box | 83 x 83 x 71 mm, 30 mm clearance, faces at 5 mm |
| Cells | 7.91 M (2.30 M without the far field: 3.4x) |
| Timesteps | 165,536 of 692,342 allowed; stopped on the -40 dB end criterion (-40.3 dB) |
| Runtime | 2 h 38 min on three threads, shared machine (100-230 MCells/s) |
| Peak memory | 1.79 GB |
| Face dumps on disk | 8.2 MB (12 files, 60 frequencies) |
| Uploaded artifacts | 2.5 MB, of which `farfield.json` 47 KB |
| `nf2ff` guard | passed |
| Box closed | yes |

It runs, and at a cost a desktop can carry. **The level is not usable.** The port read a
negative resistance at 57 of the 60 far-field frequencies, -25.5 kΩ against |Z_in| = 25.5 kΩ at
30 MHz and -545 Ω against 3.85 kΩ at 520 MHz. A passive port cannot do that: the record stopped
on the -40 dB end criterion while the board was still ringing (a 4-layer board's plane pairs
are resonators with little loss), and what the transform holds at those frequencies is the
truncation. The fixture board and every dipole decay quickly enough that -40 dB was a complete
record (the worst of them read -0.5 % of |Z_in|, a 5 mm half-wave dipole -6 %). The per-volt
slope below 200 MHz came out 28 dB/decade, which means nothing for the same reason.

That is now caught: `farfield.json` marks a frequency unusable, and lists it in `truncated_hz`,
when the port's resistance is below -5 % of |Z_in|, and the compliance estimate says a longer run
would recover it rather than that the source missed it. On this run that leaves 3 usable
frequencies, all above 800 MHz. The next run to make is the same one with a lower end criterion
(1e-6), which on this board may take the full 100 ns (about 7 hours at these speeds).

**Preset 600 µm / 300 µm.** Refused as unstable at step ~30,000: the energy climbed back by a
factor of 1,140 after the excitation had passed. The divergence check did its job; the far field
never ran. The same region without the far field was not tried at this preset; the far-field box
grows the grid into coarse air cells, which is where the mesher's remaining grading steps are.

**Sizes, from the mesh alone** (`build_model`, no solve), to show what the far field costs on this
board: at 300 µm a 15 mm region is 2.3 M cells without the far field and 7.9 M with it (3.4x); a
40 mm region 14.1 M and 29.4 M (2.1x); at 150 µm a 40 mm region is 108.6 M cells with the far field,
which is beyond a desktop. The whole of a second board (4 layers, 60 x 40 mm) is 26.6 M cells at
300 µm with the far field. The timestep is set by the copper, not the box: 692,342 steps at 300 µm
for the 100 ns a 30 MHz lower edge needs.

---

## What is still missing

- **A real board's run stops before its planes stop ringing.** At the production end criterion
  (-40 dB) board A's record was incomplete at 57 of 60 frequencies (§5). The check now drops them;
  nothing has yet shown what end criterion a real board needs, or what that run costs.
- **No real board's level has been checked** against a measurement or a second full-wave
  solver. Everything above checks the far field of structures whose answer is known.
- **The top of the band.** The last 15 % below a solve's top frequency reads up to 1.8 dB low on
  a short dipole. Solving to 1.25-1.5 times the highest frequency of interest removes it, at the
  price of a finer mesh.
- **Above a resonance** the far field is only as good as FDTD's model of the structure; §2's
  half-wave dipole differs from nec2c by up to 1.3 dB there.
- **The plane does not act back on the board.** Up to 11 % of a half-wave dipole's feed
  impedance at 0.8 m; far less for anything electrically small.
