# Known issues and status

What works, what is experimental, and what is not built yet. Read this before relying on a
number. [`implementation.md`](implementation.md) describes how the parts that exist work.

**Status words.** *Built* means the code exists and its tests pass. *Verified* means the
feature has been checked against an independent reference — a closed-form result, a second
solver or a measurement — and passed. The gap between the two is most of this page.

---

## 1. Overview

| Feature | Built | Verified | In the app |
|---|---|---|---|
| Board ingest (KiCad, Gerber + IPC-D-356), viewer | ✅ | ✅ | on |
| Geometric EMI/EMC rule checks and findings | ✅ | ✅ | on |
| Run-cost estimator | ✅ | ✅ | on |
| ESD transient simulation (ngspice) | ✅ | ⚠️ source, line and clamp models unit-checked; no bench comparison | on |
| Cable budget, Tier A (`cable` run, nec2c) | ✅ | ✅ against openEMS on the product setup: 7 of 9 configurations within 1 dB below resonance, all within 2 dB at the peaks; the wire radius, not the solver, is the larger uncertainty — §3 | on |
| Limits library and Limits page | ✅ | ⚠️ FCC Part 15 only; there is no CISPR 32 table | on |
| **Full-wave solve (openEMS)** | ✅ | ⚠️ **a 50 ohm microstrip within 1 % of theory on every preset; solves end to end on the fixture board; long records on whole boards are out of scope — §2** | **off** (`full-wave`) |
| Small-part solve (one net cut out over its planes, `small-part-solve`) | ✅ | ⚠️ microstrip, stripline and via within their closed forms (coarse via 0.1 % over); a synthetic coupon converges; one real coupon of three checked, and its presets disagree on where the hotspot is ([verification/small-part-solve.md](verification/small-part-solve.md)) | **off** (`small-part-solve`, or with full-wave) |
| Drivers (re-weighting a solve) | ✅ | ⚠️ every check in §3 passes; nothing against a measured source | off, with full-wave |
| Components (MLCC models in a solve) | ✅ | ❌ the shipped openEMS 0.0.35 cannot model an inductor, so no capacitor is placed; on a current openEMS build a 100 pF 0402 resonates within 1.6 % but only with a -70 dB record (§3) | off, with full-wave |
| Board far field (NF2FF) | ✅ | ⚠️ matches nec2c on dipoles over the ground plane as the product runs it, 30 MHz up (§3); no board checked against a measurement | off, with full-wave |
| Cable emissions, Tier B | ✅ | ❌ fails its real-board gate: 7-9 dB low on average on two boards below resonance; the third gave no result — §3 | off, with full-wave |
| Compliance estimate | ✅ | ❌ runs end to end on the fixture board; never checked against a lab or a second solver (§3) | off, with full-wave |
| Conducted emissions scan | ❌ | ❌ | — |
| Report export | ❌ | ❌ | — |

Everything marked **off** is behind the `full-wave` experimental feature, except small-part
solves, which also have their own, `small-part-solve`. It is refused by the server, not merely
hidden. It is off because none of it has been verified on a real board, not
because it is known to be broken — see §2. To try it:

```bash
emi-local -experimental full-wave
```

or set `EMI_EXPERIMENTAL=full-wave` in the environment the desktop app is started from.

---

## 2. Full-wave solving: what was wrong, and what is still unknown

Every long FDTD run was refused with "the simulation went unstable". That is what full-wave
solving is gated on, and it turned out to be two separate defects, one of which was the check
itself.

**The refusals were a false positive.** The worker watches the energy openEMS reports and fails
a run whose energy climbs back after the excitation has passed. It decided the excitation had
passed at the first sample lower than the one before it. Measured on a real run, the energy
oscillates 3-5 dB the whole way up the excitation ramp: it dips at the sixth sample while still
seven orders of magnitude below the peak. The floor was therefore set at 1.07e-19, and the
legitimate climb to the 1.14e-13 peak was reported as a divergence "by a factor of 1.07e6" --
the exact figure users were shown, on every long run, whatever the mesh. The excitation now
counts as passed only once the energy has fallen 20 dB below its own peak, which is far more
than the ripple and far less than the 40 dB decay a finished run ends with.

With that corrected, the fixture board solves end to end: openEMS stops on its own
end-criterion after 84,987 of a possible 358,695 timesteps, converged to -41.1 dB, and the
field maps come back through the API. A run of this shape used to fail at about step 79,000.

**The mesher really did violate its own grading bound**, which is what the divergence was
attributed to. Its stated maximum step between neighbouring cells is 1.4; on a real board it
produced 2.0, 2.6 and 4.5 at the three presets. Three rules were at fault: two were written in
terms of the preset's minimum cell size, and copper puts grid lines far closer together than
any preset, so both misfired where the mesh is finest; the third stretched a graded series to
fit its gap, inflating the first cell by up to 2.7x. The same board now grades to 1.73, 1.56
and 1.46. What remains is geometric: where two copper edges sit closer together than the cell
beside them, closing the step would need a cell smaller than the mesh's smallest, which would
cost timesteps for the whole run. Every mesh now reports `max_cell_ratio` in its summary.

Grading properly costs cells: the fixture's mesh grew 35-50 % at the same presets. The cost
estimator was recalibrated in September 2026 on 4-10 mm regions of four real boards (floors
4.8 / 2.3 / 1.2), and its timestep now comes from the mesher's real smallest cell, a quarter of
dx, which had put step counts 2-2.7x low. The worker still recomputes the cost authoritatively
before it solves. [verification/solver-and-components.md](verification/solver-and-components.md)
has the numbers.

**Three model defects a microstrip found** (September 2026, same page): a ground plane drawn
over the whole board was dropped from any region that did not contain one of its corners;
every dielectric had half a copper thickness of air either side of it; and a grid line on a
trace's edge made the trace act half a cell wider. All fixed; a 50 ohm line now measures
49.6-50.1 ohm on every preset. They changed every solve's geometry, so the fixture figures
above are from the model before them.

**openEMS stopped runs inside their own pulse.** It checks its end criterion while the source is
still on, and a wide-band pulse dips far below -40 dB between lobes: a 6 mm region of a real
board stopped 51k steps into a 67k-step pulse and reported "converged". The worker now ends a
run itself, after the source, and refuses one that stopped inside it.

**What is still unknown, and why the feature stays off by default.** Whole-board solves at the
record length a 30 MHz result needs (100 ns, over a million timesteps) will not be validated or
shipped; full-wave is aimed at small regions cut from a net. What those need, and have not had,
is a check of a real coupon against a second solver or a measurement. Neither has the far field
on a real board, nor cable emissions; a compliance estimate has run end to end on the fixture
board only (§4).

**The detector's limit.** Once the source has finished it holds the energy to a decay from the
first sample, so a run that grows from the start is caught. Without the source length (a log
that lacks openEMS's line and a caller that gave none) it falls back to judging a rise only
after the energy has fallen 20 dB.

---

## 3. Verification still to do

### Cables

| Check | Status |
|---|---|
| Closed form vs solver | ✅ |
| Wire over ground resonates within 5 % of transmission-line theory | ✅ open and shorted |
| A resistive choke never raises common-mode current | ✅ |
| A choke from a datasheet curve is R + jX | ✅ built and tested; no library cable has one, none compared with a measured choke |
| A bond moves the first resonance where a line over the plane resonates | ✅ within 3.3 % (1 m), 6.7 % (2 m). The old check ("never lengthens") could not fail and was wrong for an open far end |
| **Tier B against a fully coupled simulation on three real boards, ±6 dB below resonance** | ❌ board A: Tier B 6-15 dB low from 45 MHz to resonance (mean -8.9 dB); board B: did not decay in its record; board C: 2-11 dB low from 80 MHz to resonance (mean -7.5 dB from 45 MHz). Same shape on both: low where the cable's impedance is large, agreeing at resonance |
| No diode package is mistaken for a connector | ✅ |
| `nec2c` and a second antenna solver agree within 1 dB | ✅ openEMS, 9 configurations: 7 within 1 dB below resonance, 1.14 and 2.48 dB for the other two (the second record-limited) |
| The closed form is a conservative bound | ⚠️ for an open far end only; 4-26 dB low for grounded and equipment far ends |

Numbers, method and what is still open: [`verification/cables-and-drivers.md`](verification/cables-and-drivers.md).

### Small-part solves

| Check | Status |
|---|---|
| 50 ohm microstrip Z0 and delay within 5 % of Hammerstad-Jensen | ✅ Z0 within 0.45 %, delay within 0.71 % on three presets; the low delay is the port's reference plane, and by difference of two lengths it is within 0.4 % |
| Matched microstrip S21 within 0.5 dB to 1 GHz | ✅ 0.06 dB |
| 50 ohm stripline within 5 % of Cohn | ✅ Z0 within 1.0 %, delay +1.0 to +1.2 % |
| Via inductance within 10 % of two posts between planes | ⚠️ normal worst +7.7 %; coarse +10.1 % at one point |
| Convergence over 3 coupon margins and 2 presets, synthetic board | ✅ |
| Real-board coupons converge and agree across presets | ⚠️ board C: margins agree, presets agree on level, \|Z\| and S21 but not on which end is loudest; boards D and B not run |

Details and the full list: [`verification/small-part-solve.md`](verification/small-part-solve.md).

### Drivers

| Check | Status |
|---|---|
| Trapezoid harmonics within 0.1 dB | ✅ |
| Re-weighting a result matches re-solving within 0.5 dB | ✅ |
| An uploaded waveform joins the spectrum envelope within 1 dB | ✅ 0.22 dB worst; the join used to scale to a null and drop every harmonic above it |
| An *assumed* driver shows as assumed everywhere, including the uncertainty | ✅ result, σ, 80 % range, Compliance panel and every driver picker |
| A result from an older format refuses a driver with a re-run message | ✅ near field, estimate and Cables chart |

Details and numbers: [`verification/cables-and-drivers.md`](verification/cables-and-drivers.md).

### Components

| Check | Status |
|---|---|
| **One 0402 capacitor over a plane: SRF within 5 %, \|Z\| within 1 dB to 3× SRF** | ⚠️ **100 pF on a current openEMS build, run to -70 dB: SRF +1.6 % ✅, \|Z\| within 1.37 dB (1 dB missed at resonance only) ❌. At the solve's -40 dB it ripples ±6 dB; a 1 nF part never settled. The shipped 0.0.35 cannot run it at all** |
| No matched parts gives results identical to before | ✅ |
| Every standard KiCad capacitor footprint on four real boards resolves | ✅ (46 % → 99 %) |
| A decoupling finding quotes the library's SRF and source | ❌ |

The first of these is the only check that the series R-L-C construction behaves like a
capacitor. It found that the construction used until September 2026 (R, L and C elements in three
adjacent cells) was an open circuit on the shipped openEMS 0.0.35, which skips an element with
only L; every solve with components on had modelled its capacitors as missing. Capacitors are
now one series element, placed only on a solver that models an inductor.

### Compliance

| Check | Status |
|---|---|
| Every FCC limit segment matches the published CFR text | ✅ |
| Conducted-limit interpolation | ✅ |
| Combination of paths, totals and shares | ✅ |
| Confidence arithmetic (Python and TypeScript agree) | ✅ |
| Incomplete inputs remove the margin rather than guess | ✅ |
| Far field vs theory: directivity 1.76 dBi short dipole, 2.15 dBi half-wave | ✅ 1.75-1.79 and 2.15 dBi ([verification/far-field.md](verification/far-field.md)) |
| Far field with the ground plane 0.8 m below, scanned 1-4 m at 3 m, vs nec2c | ✅ short dipoles within 0.22 dB to 850 MHz; ⚠️ -1.8 dB at the top of the solved band (0.04 dB when solved past it); half-wave dipoles within about 1 dB to resonance, 1.3 dB above |
| Far field below the frequency where the box is a tenth of a wavelength out | ✅ on dipoles and a loop down to 30 MHz (0.003 λ); not on a board |
| Far field per volt rises as theory says (loop, short dipole, fixture board) | ✅ loop within 0.2 dB of closed form below 300 MHz; dipole 40.3 dB/decade; fixture 39 (was 22.6, see §4) |
| Far field divided by the source in openEMS's own convention (the FD factor of 2) | ✅ against openEMS: an E dump integrated along the port's probe is 1.99-2.01 times `post._dft` of the same voltage, fixture board |
| A real solve, a driver and the board assembled into a margin | ✅ runs on the fixture board (`scripts/e2e_compliance_fixture.py`); the level is not checked against anything |
| Transfer functions interpolated between grid points | ❌ the 1 dB σ term is a placeholder, not a residual |
| Disclaimer on reports and exports | ❌ no export exists |
| LISN network for conducted emissions | ❌ not started |
| One real board against a real lab result | ❌ |

---

## 4. Gaps in what is built

- **Mesh grading is enforced as far as geometry allows** (§2). Where two copper edges sit
  closer together than the cell beside them, the step between those two cells stays: closing it
  would mean a cell smaller than the mesh's smallest, which costs timesteps everywhere.
- **Components need an openEMS build** (`OPENEMS_SOURCE=build`); the released image is 0.0.35
  and places none. On the build they need a run to -70 dB to be right (§3), which the solve
  does not ask for, and a current openEMS writes field dumps in a layout only the near-field
  reader has been taught; nf2ff output from it is untested.
- **Whole-board solves at radiated record length are out of scope** (§2).
- **The far field on a real board runs, but its record is too short.** Board A (4 layers,
  100 x 80 mm, a 15 mm region at 300 µm): 7.9 M cells, 2 h 38 min on three threads, 1.8 GB, an
  83 x 83 x 71 mm box, a 47 KB `farfield.json`. It stopped on the -40 dB end criterion at 165,536
  steps with the board still ringing, and the port read a negative resistance at 57 of 60
  frequencies. Those are now marked unusable (`truncated_hz`) and the estimate says a longer run
  would recover them. What end criterion a real board needs, and what it costs, is not measured.
  At the 600 µm preset the same run was refused as unstable. See
  [verification/far-field.md](verification/far-field.md) §5.
- **The compliance chain has only run on the fixture board.** The worker now assembles a solve's
  far field and cable transfer functions into paths (September 2026), and
  `worker/scripts/e2e_compliance_fixture.py` runs it on `tiny.kicad_pcb` in the worker image: a
  30 MHz-1 GHz solve with the far field (1.89 M cells, converged at 48,930 steps, 5 minutes),
  a 25 MHz 3.3 V clock attached, J1 declared as carrying no cable. It comes back complete,
  one board path, 19 driven harmonics, +35.9 dB at 75 MHz, σ 7.4 dB. That shows the chain
  runs; it does not show the level is right. That run's far field rose 20 dB/decade per volt
  where a capacitive port needs 40. The cause was two faults, both fixed (September 2026):
  `nf2ff`'s PEC mirror images horizontal currents wrongly, and the stride-sampled faces left a
  14 mm slot round the box ([verification/far-field.md](verification/far-field.md) §1). The
  far field is now read at the scan's positions with image currents, format 3; the same run
  now gives 39 dB/decade and a margin of +41.5 dB at 325 MHz (it was +35.9 dB at 75 MHz, with
  most of the low band inflated). The first real run also found the far field had never worked on a derived grid: the
  job wrote frequencies with more digits than the dumps recorded, and `nf2ff` refused every
  plane. Fixed; the check is in `test_nf2ff.py`.
- **One driver per solve.** A solve has one excited port and the estimate attaches one driver to
  it. A board with two clocks needs two solves, and combining two solves in one estimate is not
  built. More than one excited port is a gap.
- **Undeclared sources are invisible.** The gate knows the nets the user names as sources, not
  every clock on the board; a clock nobody declared is missing, not zero.
- **Harmonics of two drivers are combined only when they coincide to 100 ppm.** A receiver sees
  everything inside its 120 kHz (or 1 MHz) bandwidth together; lines closer than that but not
  equal are shown separately.
- **The far field makes a solve much larger.** The box is kept 25 mm or λ/10 from the copper and
  the grid grows to hold it: 2.6x the cells on the fixture board. The browser's cost estimate
  does not know this; the worker republishes the real figure at the mesh stage.
- **The budget always carries the permittivity term**, because the solve does not record whether
  the stackup was assumed, and never carries the component-coverage term, because it does not
  record which parts matched nothing.
- **10 m and conducted standards are refused** by the compliance estimate rather than scaled.
- **There is no CISPR 32 table.** Only FCC Part 15 limits exist. When CISPR 32 is added its values
  will come from cross-checked secondary sources, not the standard itself.
- **Capacitance is nominal**: no DC-bias or temperature derating.
- **Shielding and enclosures are not modelled.** A shielded and an unshielded cable are the same
  antenna.
- **Spread-spectrum clocking is ignored**, which errs on the conservative side.
- **Runs do not survive closing the app.** A run that was in progress is marked failed on the
  next start; retry it from the Runs menu in the board's header.

---

## 5. Not started

| Item | Notes |
|---|---|
| Conducted emissions scan | ngspice LISN, switching-regulator drivers, common-mode term. The largest piece left. |
| Record a lab test result | Needed before confidence can be called calibrated. |
| Report export | |
| Built-in antenna solver | nec2c is the only one. |
| Cheap cable what-ifs | Re-run only the antenna model when a choke, length or far end changes. |
| DC-bias / temperature derating | |
| Metal enclosures; MPN matching; Touchstone, ferrite, regulator and IBIS models | |
