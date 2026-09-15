# Length matching, impedance, and rule settings

**Status: implemented.** This is the design note it was built from; the audit in §1 describes
what the code looked like before. For the settings file itself see
[rules-file.md](rules-file.md). Three things are asked for here and they turn out to
share a spine:

1. **DDR length matching** — find lanes that are skewed against their clock or strobe,
   within a tolerance somebody can set.
2. **Rule settings** — a general way to turn checks on and off and to set their thresholds.
3. **Impedance** — compute it from the stackup and check it against a target.

The spine is that all three need something the analyzer does not currently have: a model of
*how long a signal takes*, not how much copper is on a net. Everything below builds from
that.

---

## 1. What exists today

An honest audit, because two of the three asks turn out to be partly answered already and
one of them is answered wrongly.

| | today | good enough for this? |
|---|---|---|
| Stackup parsing | per-layer type, thickness, material, `epsilon_r`, `loss_tangent`, each flagged `from_file` | **yes** — this is the foundation and it is sound |
| Stackup fallback | synthesises a symmetric FR-4 stack, εr 4.4, `from_file=False`, warns | yes, but see §5 on what "assumed" must mean downstream |
| Gerber path | thicknesses from the job file; **εr never available**, warned | no — impedance is not computable from Gerbers alone |
| Reference plane | `check_plane_gaps` finds the nearest plane by z-order, falling back to largest pour | **yes** — reusable as-is for impedance |
| Layer role | `plane_net` + `plane_coverage`, measured from pours | yes |
| Track width | parsed and kept per segment | kept, but **never used for anything electrical** |
| Net length | `length_mm` = **sum of all copper on the net** | **no — this is the wrong measurement**, see §2 |
| Velocity | one global constant, `velocity_factor = 0.5` | **no** — not per-layer, not from the stackup |
| Netclasses | **not parsed at all** | no |
| Differential pairs | **no concept of them** | no |
| Impedance | **nothing, anywhere** | no |
| Rule config | fixed list of 5 rules; one parameter (`max_frequency_hz`), not settable from the UI | no |

The five rules today are `plane-gap`, `return-via`, `via-stub`, `radiator`,
`edge-proximity`. They are hard-coded in a list, always all run, and their thresholds are
constants in the module.

### The one thing that is actively wrong

`length_mm` is the total copper on a net. For a point-to-point net that happens to equal the
path length. For anything else it does not, and DDR is the "anything else" case:

- A **fly-by** address/command net reaches four DRAMs through T-branches. Its total copper
  is the sum of the trunk and every stub, which is not the delay to any of them.
- A net with a via has the via's copper counted as zero, and its delay counted as zero.
- Two nets with identical total copper can differ by 25% in delay if one is routed on an
  outer layer and the other on an inner one (§3).

So the current number cannot be used for length matching, and quietly using it would produce
confident, wrong findings. This is the first thing to fix, and it is a prerequisite for
everything else in this document.

---

## 2. The measurement we are missing: pin-to-pin delay

### 2.1 Path, not total

We need, for a net, the **path from a driver pad to each receiver pad**, as an ordered list
of (layer, length) runs plus the vias crossed.

The data is already there — tracks with endpoints, vias with layer spans, pads with
positions — it has never been assembled into a graph. Sketch:

- Nodes: pad centres, via centres, and track endpoints, merged when within a tolerance
  (the same 0.08 mm the Gerber net reconstruction settled on).
- Edges: track segments (weight = length, tagged with layer), vias (weight = z-distance,
  tagged as a via).
- Shortest path between two pads gives the routed path; the graph also reveals the
  topology — a net whose pads are leaves off a trunk is fly-by, one with exactly two pads
  is point-to-point.

**Which pad is the driver** is not in the board file. Options, in order of preference:
(a) the user says so in settings, (b) infer from the component — the net's pad on the
controller/SoC rather than on the memory, identified by which reference designator carries
the most nets of the group, (c) fall back to the longest path in the net and say so.

### 2.2 Delay, not millimetres

Matching physical length across a layer change is wrong, and wrong by enough to matter.

Propagation delay depends on the effective permittivity the field sees:

```
t_pd = √(ε_eff) / c            c = 299.792 mm/ns
```

- **Stripline** (inner layer, plane above and below): ε_eff = εr.
  At εr 4.3 → 6.92 ps/mm (176 ps/inch).
- **Microstrip** (outer layer, plane on one side, air on the other): ε_eff is lower,
  roughly `0.475·εr + 0.67` → 2.71 at εr 4.3 → 5.49 ps/mm (139 ps/inch).

That is a **~25% difference**. Concretely: DDR4 at 3200 MT/s has a 312 ps unit interval and
an intra-byte skew budget of a few ps. A 20 mm section moved from an inner to an outer layer
changes that net's delay by ~28 ps — an order of magnitude more than the budget — while its
length in millimetres is unchanged. A length-matching check that reports millimetres would
call that board matched. It is not.

**So the unit of the check is picoseconds.** Millimetres can be shown alongside as a
convenience, and the tolerance can be *entered* in mm or mil for people who think that way,
but the comparison is in time.

Also to account for:

- **Via delay.** Small but not zero — roughly 1–3 ps for a through via in a 1.6 mm board,
  plus the layer transition. A net with three more vias than its neighbour is skewed.
- **Covered microstrip.** Solder mask over an outer trace raises ε_eff by ~0.1–0.3, worth a
  few ps over a long run. Second-order; note it and move on.
- **Fibre weave.** Real, direction-dependent, and not computable from what we have. Out of
  scope, but worth stating in the limitations page so nobody reads ±2 ps as truth.

---

## 3. DDR length matching

### 3.1 What actually has to match what

This is the part most easily got wrong, because "match everything to the clock" is not how
DDR works:

| group | matched to | why |
|---|---|---|
| DQ[0..7], DM within a byte lane | **their own DQS pair**, not CK | data is captured on the strobe that travels with it |
| DQS± | its own complement (intra-pair) | differential; tightest tolerance of all |
| Address, command, control, CKE, ODT, CS | **CK±** | captured on the clock at each DRAM |
| CK± | its own complement | as DQS |
| byte lane ↔ byte lane | *nothing* | lanes are independent; write levelling absorbs it |
| DQS ↔ CK | loosely, or not at all on fly-by | write levelling handles it by design |

Getting this wrong in either direction is costly: matching lanes to each other produces a
wall of findings nobody can act on, and matching DQ to CK instead of DQS misses the error
that matters.

### 3.2 Identifying the nets

In order of confidence:

1. **Netclasses**, if the board file has them. KiCad stores netclass membership and
   diff-pair rules; we do not parse either today, and we should. A netclass called
   `DDR_DQ0` is the designer telling us the answer.
2. **Component pins.** We have pad `ref` and `number`. The DQ nets landing on one memory
   device *are* that device's lanes; grouping by component is robust against unusual net
   names and is data we already have.
3. **Name patterns**, as a fallback: `DQ\d+`, `DQS\d*[_-]?[PN]`, `DM\d|DQM`, `A\d+`, `BA\d`,
   `CK[_-]?[PN]`, `CKE`, `CS`, `ODT`, `RAS|CAS|WE`, `LDQS|UDQS`, `LDM|UDM`.

Bit swapping within a byte lane and byte-lane swapping are both legal and common, so lane
membership must come from the grouping, never from assuming DQ0–7 is lane 0.

Whichever route identified a group must be **stated in the finding** — "grouped by netclass
`DDR_DQ0`" versus "grouped by name pattern" is the difference between a user trusting the
result and dismissing it.

### 3.3 The check

For each group: compute each member's driver→receiver delay, take the group's reference
(its DQS or CK), and report members outside tolerance.

A finding needs to say: which group, which net, how far out, in ps *and* mm, against which
reference, and where to look. "DQ5 is 34 ps longer than DQS0 (tolerance ±10 ps); 4.9 mm of
that is the extra inner-layer run at (72.1, 40.3)" is actionable. "Net too long" is not.

Fly-by nets have several receivers, so the check runs per receiver and reports the worst.

### 3.4 Tolerances — the settings this needs

Defaults have to be *defaults*, not gospel; every board has its own budget. As a starting
point, expressed in time:

| what | default | typical mm equivalent (inner layer) |
|---|---|---|
| intra-pair (DQS±, CK±) | ±2 ps | ±0.3 mm |
| DQ/DM within a byte lane, to DQS | ±10 ps | ±1.4 mm |
| address/command/control, to CK | ±25 ps | ±3.6 mm |
| byte lane to byte lane | disabled | — |

Per-group overrides are essential — a 1600 MT/s design and a 3200 MT/s design do not share
a budget, and someone will always have a reason to loosen one lane.

---

## 4. Impedance

### 4.1 What we would need, and what we have

To compute the characteristic impedance of a trace:

| input | have it? |
|---|---|
| trace width | **yes**, per segment |
| copper thickness | **yes**, from the stackup |
| height to the reference plane | **yes** — stackup thicknesses plus the existing nearest-plane logic |
| which plane is the reference | **yes** — `check_plane_gaps` already resolves this |
| dielectric εr | **sometimes** — from the file, or assumed 4.4 and flagged |
| whether it is microstrip or stripline | derivable: is there a plane on both sides? |
| differential spacing | **no** — needs pair identification and a per-segment gap measurement |
| solder mask coverage | partially — mask layers are in the stackup |

So single-ended impedance is **mostly computable today** and simply is not computed.
Differential needs pair awareness first.

### 4.2 How to compute it

Closed-form first: IPC-2141 / Hammerstad for microstrip, Cohn for stripline, with their
coupled variants for differential pairs. These are accurate to roughly ±5–10% against a
field solver for ordinary geometries, and worse at the edges of their validity (very wide or
very thin traces, w/h outside ~0.1–3).

That error bar is not a footnote. A 40 Ω DDR target with a ±10% tolerance and a ±10% model
error means the check cannot honestly flag a 44 Ω trace. Two consequences:

- The check should report **impedance with an uncertainty**, and only flag what is outside
  tolerance *including* that uncertainty.
- When εr was assumed rather than read from the file, uncertainty rises sharply — εr 4.2 vs
  4.6 moves impedance by several ohms — and the finding must say the number rests on an
  assumption.

A 2D quasi-static field solver would remove most of the model error and is the natural
second stage. Note that openEMS does not help here: it is a 3D FDTD solver for radiated
fields, and a cross-section impedance problem wants a different tool.

### 4.3 What to check

- **Target vs computed**, per net or netclass: DDR3/4 single-ended commonly 40 Ω (sometimes
  50 Ω), DQS and CK differential 80 Ω. Targets come from settings, ideally from netclass.
- **Impedance discontinuities along a net**: a trace that changes width, or changes layer
  into a different dielectric height, changes impedance mid-flight. This is often more
  actionable than the absolute value, and it needs no target to be set.
- **Reference plane changes**: already half-covered by `plane-gap`; a layer change that also
  changes reference plane is both a return-path problem and an impedance discontinuity.

### 4.4 Improvements needed to the stackup work itself

The parsing is sound. What is missing sits around it:

1. **εr is a single number.** It is frequency-dependent; at DDR rates the value at 1 GHz is
   the relevant one and a datasheet 1 MHz figure will be optimistic. Settings should allow
   an εr at a stated frequency, and the assumed default should say which frequency it means.
2. **No copper roughness.** Affects loss, not impedance much. Matters for the solve, not for
   these checks. Worth a settings field for completeness.
3. **The Gerber path has no εr at all.** Impedance checks must be disabled, not guessed, on
   a Gerber-only board — with a message saying why and what to supply.
4. **Prepreg vs core is not distinguished** where the file gives it. Different εr, and it
   changes which dielectric height applies to which layer pair.

---

## 5. Rule settings

Everything above needs configuration, and the current arrangement has none: five rules
always run, thresholds are module constants, and the one parameter that exists
(`max_frequency_hz`) cannot be set from the UI.

### 5.1 What has to be configurable

- **Per rule**: enabled, severity override, and its own thresholds.
- **Per group or netclass**: DDR tolerances, impedance targets. A board has several
  different budgets on it at once.
- **Board-level**: max frequency, εr override and the frequency it applies at, default
  velocity, driver-pad hints.
- **Suppressions**: "this finding, on this net, is understood and accepted", with a reason.
  Without this, the second run of a real board is a list nobody reads. This is the
  difference between a linter people keep and one they turn off.

### 5.2 Where settings live

Two homes, because they serve different needs:

- **Project settings**, in the database, edited in the UI. Convenient, immediate.
- **A file committed with the board** — `emi.rules.yaml` beside the `.kicad_pcb`. This is
  what makes the tool usable in CI: the rules travel with the design, change under review,
  and a regression is a diff. Given there is already an EMI CI path being built, this
  matters more than the UI copy.

Precedence, most specific last: built-in defaults → project settings → committed file → run
parameters. Every effective value should carry **where it came from**, and findings should
be able to say "threshold 25 ps, from emi.rules.yaml" — otherwise a surprising result costs
an hour of hunting.

### 5.3 Shape

Rules need **stable ids** (`plane-gap`, `ddr-skew`, `impedance-target`) that settings key
off, decoupled from their titles — the existing rule names are already suitable. A rule
catalogue endpoint listing id, title, what it checks, its parameters and their defaults lets
the UI build a settings page without hard-coding any of it, and gives the docs one source.

The settings document needs a schema version from day one; thresholds will change meaning
as the checks improve, and a silently reinterpreted threshold is worse than a rejected file.

---

## 6. Order of work

Each step is useful on its own, and each is a prerequisite for the next.

1. **Connectivity graph and pin-to-pin path length.** Fixes the wrong measurement, and
   `length_mm` becomes honest. Nothing else here is possible without it. *Also improves what
   is already shipped: the net list's length column currently means something other than what
   users assume.*
2. **Per-layer delay from the stackup.** Microstrip vs stripline, ε_eff, via delay. Replaces
   the global `velocity_factor = 0.5` — which also silently affects the existing
   wavelength-based `radiator` check, so this makes a shipped rule more accurate too.
3. **Settings framework.** Needed before any rule with a tolerance ships, or the tolerances
   become constants that have to be dug out later.
4. **Netclass and differential-pair parsing.** Small, unlocks both remaining features.
5. **DDR grouping and the skew check.**
6. **Single-ended impedance**, with uncertainty and assumption flagging.
7. **Differential impedance and discontinuity checks.**

Steps 1 and 2 are the ones that pay for themselves regardless of whether DDR checking ever
ships, because they correct numbers the tool already reports.
