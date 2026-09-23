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
| Cable budget, Tier A (`cable` run, nec2c) | ✅ | ⚠️ the solver matches transmission-line theory; the product setup (fed against the board, scanned 3 m out) has no second-solver comparison — §3 | on |
| Limits library and Limits page | ✅ | ⚠️ FCC Part 15 only; there is no CISPR 32 table | on |
| **Full-wave solve (openEMS)** | ✅ | ⚠️ **solves end to end on the fixture board; nothing verified at radiated record length — §2** | **off** (`full-wave`) |
| Drivers (re-weighting a solve) | ✅ | ⚠️ partly | off, with full-wave |
| Components (MLCC models in a solve) | ✅ | ⚠️ partly | off, with full-wave |
| Board far field (NF2FF) | ✅ | ⚠️ on a dipole fixture only | off, with full-wave |
| Cable emissions, Tier B | ✅ | ⚠️ synthetic board only | off, with full-wave |
| Compliance estimate | ✅ | ❌ never run on real inputs | off, with full-wave |
| Conducted emissions scan | ❌ | ❌ | — |
| Report export | ❌ | ❌ | — |

Everything marked **off** is behind the `full-wave` experimental feature. It is refused by the
server, not merely hidden. It is off because none of it has been verified on a real board, not
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
than the ripple and far less than the 50 dB decay a finished run ends with.

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

Grading properly costs cells: the fixture's mesh grew 35-50 % at the same presets, and the
cost estimator's per-preset fill factors were calibrated against the old mesher, so they now
under-predict by about that much. Recalibrating is quick --
`worker/scripts/measure_fill_factor.py` with `EMI_TEST_BOARDS` -- and has not been done. The
worker recomputes the cost authoritatively before it solves, so an underestimate delays a
refusal rather than hiding it.

**What is still unknown, and why the feature stays off by default.** One board, one region, one
frequency, on a fixture designed to be small. None of this has been run at the record length a
radiated result needs: 30 MHz means 100 ns of simulated time whatever the board is, which is
millions of timesteps, and no run of that length has been measured since the fix. Neither has
the far field on a real board, nor cable emissions, nor a compliance estimate from real inputs
-- the three gates below that were blocked by the refusals and are now worth re-running.

**The detector's own limit**, stated because it is now the only thing standing between a bad
mesh and a plausible-looking number: it judges a rise only after the energy has fallen 20 dB,
so a grid that blew up before decaying at all would not be reported. Every divergence on
record has the other shape.

---

## 3. Verification still to do

### Cables

| Check | Status |
|---|---|
| Closed form vs solver | ✅ |
| Wire over ground resonates within 5 % of transmission-line theory | ✅ open and shorted |
| A choke never raises common-mode current; a bond never lengthens the first resonance | ✅ |
| **Tier B against a fully coupled simulation on three real boards, ±6 dB below resonance** | ❌ was blocked by §2; unblocked and not yet run |
| No diode package is mistaken for a connector | ✅ |
| `nec2c` and a second antenna solver agree within 1 dB | ❌ there is no second solver |

Tier B agreed to 1.2 dB typical and 2.2 dB worst on a synthetic board, and once on a real board
at a cheap mesh preset. Neither is the full check.

### Drivers

| Check | Status |
|---|---|
| Trapezoid harmonics within 0.1 dB | ✅ |
| Re-weighting a result matches re-solving within 0.5 dB | ✅ |
| An uploaded waveform joins the spectrum envelope within 1 dB | ❌ |
| An *assumed* driver shows as assumed everywhere, including the uncertainty | ❌ end to end |
| A result from an older format refuses a driver with a re-run message | ❌ |

### Components

| Check | Status |
|---|---|
| **One 0402 capacitor over a plane: SRF within 5 %, \|Z\| within 1 dB to 3× SRF** | ❌ **never run** |
| No matched parts gives results identical to before | ✅ |
| Every standard KiCad capacitor footprint on four real boards resolves | ✅ (46 % → 99 %) |
| A decoupling finding quotes the library's SRF and source | ❌ |

The first of these is the only check that the series R-L-C construction behaves like a
capacitor.

### Compliance

| Check | Status |
|---|---|
| Every FCC limit segment matches the published CFR text | ✅ |
| Conducted-limit interpolation | ✅ |
| Combination of paths, totals and shares | ✅ |
| Confidence arithmetic (Python and TypeScript agree) | ✅ |
| Incomplete inputs remove the margin rather than guess | ✅ |
| Far field vs theory (dipole 2.13 dBi vs 2.15; ground reflection within 0.08 dB) | ✅ on a fixture |
| Disclaimer on reports and exports | ❌ no export exists |
| LISN network for conducted emissions | ❌ not started |
| One real board against a real lab result | ❌ |

---

## 4. Gaps in what is built

- **Mesh grading is enforced as far as geometry allows** (§2). Where two copper edges sit
  closer together than the cell beside them, the step between those two cells stays: closing it
  would mean a cell smaller than the mesh's smallest, which costs timesteps everywhere.
- **The cost estimator is calibrated against the old mesher** and now under-predicts (§2).
- **No solve has been run at radiated record length** since the divergence check was fixed
  (§2). That is the run the far-field and compliance paths need.
- **The far field has never run on a real board.** Box placement, face sub-sampling and artifact
  size are untested outside the fixture.
- **The compliance chain has never been given real inputs.** Nothing yet assembles a real solve's
  far field and cable transfer functions into the paths the compliance run combines.
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
