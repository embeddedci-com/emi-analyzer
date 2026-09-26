# Verification: the decoupling view

September 2026. The decoupling view is a lumped estimate of each IC's supply impedance, built
at ingest from the layout (`worker/emi_worker/rules/pdn.py`, `rules/decoupling_view.py`). This
page says what it models, which checks were run against which reference, and what is still to
do. Every number here is asserted by `worker/tests/test_decoupling_view.py` or
`webapp/src/lib/decoupling.test.ts`; a test that fails moved a number on this page.

## The model

Per supply rail and per IC on it (parts named U or IC, nets that classify as power):

- **Each capacitor is a series R-L-C.** C, ESR and ESL come from the component library
  (`components/library/mlcc.json`, generic figures, labelled so). A part the library cannot
  resolve gets its value from its Value field (100 nF when that does not parse), an ESL of
  0.5 nH and the family's ESR for its value, and is shown as assumed.
- **The layout adds a connection loop** in series with the part. The loop runs from the IC's
  supply pin (no power plane) or the capacitor's supply via (power plane) through the part to
  its ground via, at the height of the mounting layer above the nearest plane. It is half a
  rectangular loop of round wire twice that tall (image theory), with the via's drilled radius:
  Grover (1946), as restated in Paul, *Inductance: Loop and Partial* (2009), eq. 5.20. More
  ground vias within `max_ground_via_mm` parallel one of the loop's two vertical legs:
  n vias scale it by (1 + n) / 2n.
- **A plane pair adds spreading inductance and its own capacitance.** When the rail is poured
  on a layer (30 % coverage or more) with a ground plane in the stack, each capacitor also sees
  (mu0 t / pi) ln(D / r) between its vias and the IC's, t the cavity and D the distance: two
  posts between parallel plates. The IC's own connection to the pair is a series inductance
  common to every capacitor. The pair is one more branch: C = eps0 eps_r A / t, L the radial
  inductance from a via to the plane's equivalent radius, R from the loss tangent at its own
  resonance. Its first cavity resonance, c / (2 a sqrt(eps_r)), is quoted and not modelled.
- **Target impedance** = rail volts x ripple / current step. Volts from the net name (3V3, 1V8,
  +5V; 3.3 V labelled assumed otherwise), ripple 5 %, step 0.5 A. All three are settings
  (`ripple_pct`, `step_current_a` per net group) and all are editable in the app. The target
  is judged from 100 kHz up to `board_max_hz` (100 MHz): above that an IC's package and die
  capacitance decouple it, and 1 nH is already 0.6 ohm at 100 MHz.
- **What-ifs** are computed with the same model, each as one admittance swapped in the
  precomputed sum: move a capacitor to `max_distance_mm` from its pin, add a ground via (or a
  second one), add one part (every E3 value from 1 nF to 22 uF in 0402, 0603 and 0805, placed
  at `max_distance_mm` with a via 0.5 mm from each pad; the one that closes the most of the
  worst gap), and remove each part. Ranked by how much of the worst gap they close, then by dB at
  the worst frequency, then by how much of any gap they fix.
- **Noise frequencies**: a crystal or oscillator (Y, X, XTAL, OSC) whose Value has an explicit
  Hz, on a net the IC shares; a net name with an explicit Hz; the board clock setting
  (`clock_hz`, or cable-resonance's); a regulator's `switching_hz` per rail. Harmonics up to the
  fifteenth are marked, and "not filtered" where the total is above the target in the band.

## Checks

| # | Check | Reference | Criterion | Result |
|---|---|---|---|---|
| 1 | Two-cap anti-resonance | closed form: L1 + L2 with C1 in series with C2; \|Z\| = \|Z1\|\|Z2\| / (R1 + R2) | \|Z\| within 0.5 dB, f within 2 % | ✅ +0.08 dB, -1.4 % |
| 2 | N identical parts | ESR / N at their resonance | exact | ✅ to 1e-6 |
| 3 | Textbook PDN: 10 uF, 4 x 100 nF, 10 nF, 50 x 50 mm plane pair | closed-form landmarks | see below | ✅ |
| 4 | Connection inductance | Archambeault, Connor, Steffka, In Compliance Magazine, Tables 1 and 2 (40 entries) | within 25 %; within 10 % at 20-40 mil | ✅ -17.7 to +24.2 %; -6.7 to +6.2 % |
| 5 | Plane capacitance | Bogatin's rule of thumb, C[pF] = 0.225 eps_r A[in^2] / h[in] | 0.5 % | ✅ 225 pF |
| 6 | Browser against worker | the worker's own output (`decouplingFixture.json`) | gaps, worst point, ranking | ✅ edges to 0.1 %, worst within 0.01 dB, same order, dB within 0.05 |
| 7 | 0402 via-in-pad mount against openEMS | the measured short in solver-and-components.md §4: 0.207 nH | reported | ⚠️ 0.308 nH, +49 % (see below) |
| 8 | Run time | a 71-IC board, 25 rails | well under a second | ✅ 140 ms, 357 KiB in rules.json |

**1. Two-cap anti-resonance.** 10 uF (2 nH, 10 mohm) with 100 nF (1.5 nH, 30 mohm): closed form
8.55 MHz and 0.291 ohm; the model's peak, found on the 40-a-decade grid and refined on two
nested 64-point grids, 8.43 MHz and 0.294 ohm. The closed form puts the peak where the
reactances cancel; with ESR the maximum of |Z| sits a little lower, which is the 1.4 %.

**3. Textbook PDN.** Not transcribed from a book: no published curve was at hand with every
part value, loop and plane stated. It is instead the arrangement textbook examples use (one
bulk, a bank of 100 nF, one 10 nF, a plane pair of 0.1 mm FR-4), checked against the
closed-form landmarks such a curve is built from:

| Landmark | Closed form | Model | Tolerance |
|---|---|---|---|
| Plane capacitance, 2500 mm^2 / 0.1 mm, eps_r 4.4 | 0.974 nF | 0.974 nF | 0.5 % |
| \|Z\| at 100 kHz, all capacitance in parallel | 0.1529 ohm | 0.1518 ohm (-0.7 %) | 1 % |
| The 100 nF bank's dip at its SRF, 13.0 MHz | ESR / 4 = 13.8 mohm | 13.7 mohm (-0.05 dB) | 1 dB |
| Bulk against the bank, as two branches | 4.42 MHz, 0.228 ohm | 4.36 MHz, 0.228 ohm (-0.01 dB) | 0.5 dB, 2 % |

**4. Connection inductance.** The table gives the inductance above the planes for minimum
0402, 0603 and 0805 mounts (106, 128 and 148 mil between via barrels, 10 mil barrels, 20 mil
traces) and for an 0402 with 50 mil from pad to via (166 mil), at 10 to 100 mil from the board
surface to the planes. The rectangle formula with the barrel radius reads low at 10 mil (the
flat pads and trace matter more than the wire model allows) and high at 100 mil (the table's
formula grows more slowly with depth). At 10 mil it gives 0.75 nH for the 0402 against 0.9 nH.
At the 20 to 40 mil where four- and six-layer boards put their first plane, every entry is
within 6.7 %.

**7. 0402 via-in-pad against openEMS.** `research/verify_0402.py` measured the port, the two
0402 pads 0.96 mm apart and a 0.2 mm via in the second, over a plane 0.2 mm below, as
0.207 nH (flat 234 MHz to 2.6 GHz). The same geometry through `connection_nh` (0.96 mm loop,
0.235 mm between copper centres, 0.1 mm via radius) gives 0.308 nH, 49 % high; with 0.2 mm
height, 0.264 nH (+28 %). The loop is shorter than it is wide: two 0.54 mm pads and a wide
lumped port, against a formula for thin round wire. The model is pessimistic for very short,
wide loops, which moves a part's useful-up-to down by up to 18 % (the square root of 1.49 on
the mounting term alone, less once ESL is added). It is quoted as found and not tuned.

## Follow-up (not run: the CPU was busy)

1. **Rerun check 7 on the current image**, and add a trace-fed case, so the formula is checked
   against a solve at the loop lengths the view reports most (2 to 10 mm), not only at 1 mm:

       docker run --rm --cpus 3 --memory 6g -v "$PWD/worker:/spike" \
           -v "$PWD/spike_out:/spike/spike_out" -w /spike -e PYTHONPATH=/spike \
           -e VALUE=100p -e END_CRITERIA=1e-7 \
           --entrypoint python3 emi-worker:decoupling research/verify_0402.py

   About 5 minutes on three threads (solver-and-components.md §4). Compare the short's
   `L_mount` with `pdn.connection_nh(0.96, 0.235, 0.1)`.
2. **Spreading inductance** is the two-post formula the small-part solve already checked
   (`research/sp_verify_via.py`, small-part-solve.md: within 7.7 % on the normal preset). The
   view uses the via's radius for both posts; with the check's port and via radii it reads 1.00 and
   1.42 nH against that page's 0.925 and 1.362 nH (+8 %, +4 %). No separate run is needed
   unless the formula changes.
3. **A published PDN curve** with every value stated, to replace check 3's closed-form
   landmarks with a transcribed reference.
