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
| Cable budget, Tier A (`cable` run, nec2c) | ✅ | ✅ | on |
| Limits library and Limits page | ✅ | ⚠️ FCC only; CISPR 32 from secondary sources | on |
| **Full-wave solve (openEMS)** | ✅ | ❌ **long solves diverge — §2** | **off** (`full-wave`) |
| Drivers (re-weighting a solve) | ✅ | ⚠️ partly | off, with full-wave |
| Components (MLCC models in a solve) | ✅ | ⚠️ partly | off, with full-wave |
| Board far field (NF2FF) | ✅ | ⚠️ on a dipole fixture only | off, with full-wave |
| Cable emissions, Tier B | ✅ | ⚠️ synthetic board only | off, with full-wave |
| Compliance estimate | ✅ | ❌ never run on real inputs | off, with full-wave |
| Conducted emissions scan | ❌ | ❌ | — |
| Report export | ❌ | ❌ | — |

Everything marked **off** is behind the `full-wave` experimental feature. It is refused by the
server, not merely hidden. To try it anyway:

```bash
emi-local -experimental full-wave
```

or set `EMI_EXPERIMENTAL=full-wave` in the environment the desktop app is started from.

---

## 2. Long full-wave solves diverge

This is why full-wave solving is off by default.

Every FDTD run measured over a long record (150,000 timesteps or more) has diverged: the energy
in the simulation falls as the excitation passes, then turns around and grows without bound.
It happened with and without a cable port, at every mesh preset tried, on compact regions as
well as large ones. A short run is not evidence of a stable long one: configurations that were
still stable at 25,000 steps blew up later.

**What the worker does about it.** It watches the energy and, when it climbs back above its own
low point by more than a fixed factor, fails the run with "the simulation went unstable" instead
of reporting its numbers. So a diverged solve fails; it does not return noise. But it can take
many minutes to get there.

**Why it matters beyond one run.** A far-field or radiated-compliance run has to cover 30 MHz,
and resolving 30 MHz needs `3 / f_min` = 100 ns of simulated time whatever the board is. Those
runs are always long, so they are the ones this affects most.

**What is known.**

- It is not caused by the cable's grid extension: a region with no cable diverges too.
- It is not copper layers merging onto one grid plane.
- **The leading suspect** is mesh grading: the mesher's stated maximum ratio between
  neighbouring cells is 1.4, and every preset violates it, by as much as 5× next to fine copper
  features. This has not yet been confirmed as the cause.

**What would close it:** enforce the grading bound in the mesher, then show long runs stay
stable at every preset on several real boards.

---

## 3. Verification still to do

### Cables

| Check | Status |
|---|---|
| Closed form vs solver | ✅ |
| Wire over ground resonates within 5 % of transmission-line theory | ✅ open and shorted |
| A choke never raises common-mode current; a bond never lengthens the first resonance | ✅ |
| **Tier B against a fully coupled simulation on three real boards, ±6 dB below resonance** | ❌ **blocked by §2** |
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

- **Mesh grading is not enforced** (§2).
- **The far field has never run on a real board.** Box placement, face sub-sampling and artifact
  size are untested outside the fixture.
- **The compliance chain has never been given real inputs.** Nothing yet assembles a real solve's
  far field and cable transfer functions into the paths the compliance run combines.
- **CISPR 32 limits come from cross-checked secondary sources**, not the standard itself.
- **Capacitance is nominal**: no DC-bias or temperature derating.
- **Shielding and enclosures are not modelled.** A shielded and an unshielded cable are the same
  antenna.
- **Spread-spectrum clocking is ignored**, which errs on the conservative side.
- **Runs do not survive closing the app.** A run that was in progress is marked failed on the
  next start; retry it.

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
