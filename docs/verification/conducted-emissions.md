# Verification: conducted emissions scan

September 2026. The conducted scan is experimental and behind its own feature, `conducted`, off
by default. This page says what was built, what each check compared against and what it gave,
and what is still to do. Real boards are private and named here only by letter.

## What was built

- **The LISN** (`worker/emi_worker/conducted/lisn.py`): CISPR 16-1-2's 50 Ω/50 µH V-network for
  0.15-30 MHz, one in the supply line and one in the return. The standard defines it by its
  impedance (2003 edition, 4.3 and Figure 1b: 50 Ω in parallel with 50 µH, ±20 % on the
  magnitude); the parts are the usual construction: 50 µH, 1 µF on the supply side, 0.1 µF into
  the receiver's 50 Ω with a 1 kΩ bleeder. The level reported is the voltage at the network's
  board terminal, which is what a receiver reads once the division factor is added back.
- **The input filter from the layout** (`conducted/rail.py`): the power entry the `input-filter`
  check finds, the rail behind it through ferrites, inductors, fuses, series diodes, low-value
  resistors and eFuses or load switches (an IC joining two nets named for the same voltage), up
  to three parts deep. Each net is a ladder ordered by routed distance; between rungs, the
  trace's loop inductance (Z0 times delay over its plane, a pour counted as up to 20 mm wide)
  and its vias. Capacitors use the component library's ESR and ESL plus one via and the track to
  it; a part the library does not know gets an assumed ESR and ESL and says so.
- **The regulators** (`conducted/regulators.py`): the `switch-node` check's nodes, plus an
  inverting stage's (an inductor from an IC pin to ground) and modules known by part number
  (inductor inside, no switch node on the board). An inductor is known by an L reference or an
  inductor footprint (a real board's was U3). The side of the inductor on the input rail decides
  the topology: switch node to rail is a boost fed from it, unless the part also draws from
  another, higher power input (then the rail is its output); rail to switch pin is a buck; an
  inductor to ground is inverting; between two switch nodes, a four-switch buck-boost. With
  external FETs the input is the high-side drain on the rail, not the controller's own VIN pin.
  Each switch node with its own inductor is its own source, so a PMIC's bucks are separate,
  named after the pin ("U1/VLX1"). A small table of part numbers (bucks, boosts, buck-boosts,
  PMICs, modules) is a second cue. Every regulator carries the cues it was found by and a
  confidence: high when the layout and a second cue (the node's name or the part number) agree,
  medium on one cue, low when they disagree or the mode is a default. The tab offers to confirm
  it, change its type or remove it.
- **The rail through a pass element**: a small IC (16 pads or fewer, not a regulator) joining the
  rail to a net a regulator draws from, never up to a higher named voltage: on board A a boost ran
  from a linear charger's system output. It is a 50 mΩ switch, the worst case, and says so.
- **The sources** (`conducted/sources.py`): a buck's input current as a trapezoid, I_in / D
  high for D of each period, the given edge, a 30 % ripple on the top; an inverting stage and a
  four-switch buck-boost (in buck mode) draw the same shape. A boost's input current is its
  inductor current: I_in with a triangle on top, rising for D, peak to peak
  ΔI = V_in·D / (f·L) with D = 1 - V_in/V_out (TI SLVA372C, "Basic Calculation of a Boost
  Converter's Power Stage"), from the inductor's value and the input rail's name, or 30 % of
  I_in when either is missing; capped at 2·I_in, the edge of discontinuous mode, with a note.
  Its harmonics are ΔI·|sin(πnD)|/(π²n²D(1-D)) peak, falling as 1/n² where a buck's fall as 1/n.
  Frequency, input current, duty, edge, inductance and phase come from the user; any not given
  is an assumed default (500 kHz, 0.5 A, duty from the rail names or 50 %, 10 ns, 0°) and is
  marked assumed in the result and the UI. The phase is asked for only where it matters: sources
  that share a part's clock.
- **The scan** (`conducted/scan.py`): one ngspice AC analysis per regulator, `.ac lin` from its
  first harmonic in the band to its last with one point per harmonic, so nothing is
  interpolated; the layout and every what-if are isolated circuits in the same deck. Lines at one
  frequency from sources whose phase the user gave add as phasors (a delay of φ multiplies
  harmonic n by e^(-jnφ)); any other lines closer than the 9 kHz receiver bandwidth add in
  magnitude, the worst case, since an unknown phase drifts. The deck goes through the
  ESD simulation's sanitized title, deck check and sandbox (`transient/ngspice.py`).
- **The gate** (`server/emi/features.go`): `conducted`, independent of `full-wave`. Off, the kind
  is refused on create and retry, left out of the worker list and the socket push, and refused
  at token mint. Params are checked against the worker's ranges when the run is created.

## Checks

Run in the worker image (`tests/test_conducted_sim.py`, ngspice 39); the closed forms are worked
out in the test, not by the code under test.

| Check | Criterion | Result |
|---|---|---|
| LISN impedance against the CISPR 16-1-2 curve, 150 kHz-30 MHz, 50 points per decade | \|Z\| within ±20 % (2003, 4.3); phase within ±11.5° (the tolerance later editions add; not taken from the text that was read, so checked but not relied on) | ✅ \|Z\| within 10.4 % (worst at 150 kHz, where the 0.1 µF coupling capacitor matters), phase within 6.6° |
| The same impedance from ngspice against the network's closed form | 0.1 % | ✅ below 0.001 % |
| A buck's input ripple (1 A, D = 0.3, 500 kHz, 10 µF + 1 mΩ, both LISNs), simulated in time, against I·D(1-D)/(f·C) + I·ESR | 1 dB | ✅ 141.7 mV p-p against 143.3 mV: -0.10 dB |
| Its first harmonic at the LISN: the scan against the closed-form divider I1·Zc/(Zc + 2·Z_LISN)·Z_LISN | 1 dB | ✅ 85.72 against 85.72 dBµV |
| The same harmonic from a Fourier transform of the transient run (last 50 periods, Hann window) | 1 dB | ✅ 85.73 dBµV |
| An ideal LC filter (10 µH, 10 µF) ahead of the same input capacitor, attenuation against the closed form, 150 kHz-30 MHz | 0.1 dB | ✅ within 0.012 dB; 70.1 dB at 900 kHz, where ω²LC - 1 is 70.1 dB |
| A boost's input ripple (1 A, D = 0.4, the assumed 0.3 A p-p triangle, 500 kHz, 10 µF + 1 mΩ, both LISNs), simulated in time, against ΔI/(8·f·C) + ΔI·ESR | 1 dB | ✅ 7.503 mV p-p against 7.800 mV: -0.34 dB (the ESR term is an upper bound; the capacitor's part alone is 7.500 mV) |
| Its first harmonic at the LISN: the scan against ΔI·sin(πD)/(π²D(1-D)) through the same divider, and a transient FFT | 1 dB | ✅ 62.65, 62.65 and 62.65 dBµV; a buck of the same current reads 85.74, 23 dB more |
| Two identical bucks on one node, against one: phases 0/0, 0/90, 0/180 given, and not given, against \|1 + e^(-jnφ)\| | 0.02 dB | ✅ in phase +6.02 dB at n = 1 and 2; 90° +3.01 dB at n = 1 and -255 dB at n = 2; 180° -266 dB at n = 1 and +6.02 dB at n = 2; not given +6.02 dB |
| The public fixture `regulators.kicad_pcb` (a two-buck PMIC, a boost, a buck module) through the stage: the PMIC's bucks at 1 MHz given 180° apart against in phase | cancel at odd harmonics, equal at even | ✅ 50.1 against 73.0 dBµV at 1 MHz (the two input pins are 2 mm apart, so not to nothing); equal at 2 MHz |
| The ranking on a two-capacitor network: the bulk capacitor carries the ripple, the LC what-if helps most, removing the bulk capacitor costs more than removing the 100 nF | as stated | ✅ |

Unit tests (`tests/test_conducted.py`, no simulator) cover the trapezoid's harmonics against the
sinc-sinc closed form (0.1 %), the ripple filling a flat top's nulls (and moving the first
harmonic by under 0.3 dB), a boost's triangle against its closed form (1e-6), its ripple from the
inductor and the cap at discontinuous mode, a phase's e^(-jnφ), phasor and magnitude addition,
parameter ranges, discovery on synthetic boards (a ferrite, an eFuse, a boost, a rail that is the
regulator's output, a controller with external FETs and an inductor with a U reference, a PMIC's
two bucks, a buck module, a boost behind a charger, an inverting stage, a pin named like a switch
node with no inductor, a screw terminal away from the edge) and on the committed fixtures
`worker/tests/fixtures/buck.kicad_pcb` and `regulators.kicad_pcb`.

## End to end

The fixture board (a 12 V barrel jack, 10 µF, a 600 Ω ferrite, 4.7 µF and 100 nF, a buck to
3.3 V) through the app with `-experimental conducted`: the scan finds J1:+12V, FB1 and U1 (duty
0.275 from the rail names) and finishes in a few seconds. On the assumed defaults it is 0.9 dB under the
class B average limit at 500 kHz; entered as 1 MHz and 1 A, 16.8 dB under at 1 MHz. Removing C1,
the capacitor in front of the ferrite, costs 55 dB: with FB1 it is the filter.

Four private boards, read only. First run:

| Board | Found | Why not more |
|---|---|---|
| A | the USB input and a series diode | its one regulator is a boost, which is not modelled |
| B | the USB input, two eFuses, a PMIC behind them, 23 capacitors; scanned in 3.8 s | the PMIC's several bucks are one source |
| C | the USB input, a diode, four ferrites, 29 capacitors | its buck's switch node is not one the `switch-node` check recognises; a dual-output converter is skipped as not a load |
| D | the input and 3 capacitors | a four-switch buck-boost controller with external FETs is not recognised as on the rail |

Before those runs the default input was the first connector by reference, a +3V3 pin carrying
the board's own supply out, and the buck making it was read as a load on it; and every regulator
on board B sat behind an eFuse the rail did not cross. Both are fixed: the default is the
highest named voltage, eFuses and load switches pass the supply through, and a part whose
switch-node inductor lands on the rail is skipped with the reason.

Why the others were missed, and after the fixes (September 2026; discovery and a scan on assumed
settings, each board in under 1.5 s):

| Board | Why it was missed | Found now |
|---|---|---|
| A | the boost ran from a linear charger's system output, and the rail stopped at the charger | 1 boost (high confidence), through the charger as a pass element |
| B | seven switch nodes of one PMIC read as one source | 7 bucks (all high), one per switch node, 23 capacitors |
| C | "the buck" is a buck module with its inductor inside: no switch node on the board. The dual-output part is a boost and an inverting stage | 1 boost and 1 inverting (high), 1 buck module (medium, by part number) |
| D | the 24 V input is a screw terminal whose pads sit 7.6 mm from the edge, so only a 5 V header was an input; the inductor has a U reference; the input is the high-side FET's drain behind a 4 mΩ sense resistor | 1 buck-boost controller (medium: the mode is a default) |

Still not found: board A's second input, a screw terminal with a U reference on an unnamed net,
feeding a buck module; that module is found and reported as not fed from the USB input. Board
C's MCU pins PH0 and PH1 matched the switch-node name pattern; with no inductor on them they are
no longer candidates. A charge pump on board C is not modelled.

## Still to do

- **A measurement.** No board has been compared with a LISN reading. Until one is, the level is
  an estimate and the what-ifs are the useful part.
- **Common mode.** Not modelled: it flows through the capacitance from the switch node and the
  board to the test bench's reference plane, which depends on the setup and the enclosure.
- **More regulators and modes**: flyback, SEPIC and charge-pump input currents; a buck-boost in
  boost mode (it is modelled in buck mode, the louder one); discontinuous conduction; power
  inputs on connectors with U references or unnamed nets. The part-number table is short.
- **Part models**: inductor and ferrite self-resonance and a bead's resistive peak; DC-bias
  derating of ceramics (a 10 µF 0805 at its rated voltage has well below 10 µF).
- **Detectors**: spread spectrum, burst and pulse-skipping modes, which lower quasi-peak and
  average readings, are not modelled; every line is a steady tone.
