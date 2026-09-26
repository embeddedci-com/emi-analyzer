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
- **The sources** (`conducted/sources.py`): a buck's input current as a trapezoid, I_in / D
  high for D of each period, the given edge, a 30 % ripple on the top. Frequency, input current,
  duty and edge come from the user; any not given is an assumed default (500 kHz, 0.5 A, duty
  from the rail names or 50 %, 10 ns) and is marked assumed in the result and the UI.
- **The scan** (`conducted/scan.py`): one ngspice AC analysis per regulator, `.ac lin` from its
  first harmonic in the band to its last with one point per harmonic, so nothing is
  interpolated; the layout and every what-if are isolated circuits in the same deck. Lines of
  two regulators closer than the 9 kHz receiver bandwidth are added. The deck goes through the
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
| The ranking on a two-capacitor network: the bulk capacitor carries the ripple, the LC what-if helps most, removing the bulk capacitor costs more than removing the 100 nF | as stated | ✅ |

Unit tests (`tests/test_conducted.py`, no simulator) cover the trapezoid's harmonics against the
sinc-sinc closed form (0.1 %), the ripple filling a flat top's nulls (and moving the first
harmonic by under 0.3 dB), parameter ranges, discovery on synthetic boards (a ferrite, an eFuse,
a boost, a rail that is the regulator's output) and on the committed fixture
`worker/tests/fixtures/buck.kicad_pcb`.

## End to end

The fixture board (a 12 V barrel jack, 10 µF, a 600 Ω ferrite, 4.7 µF and 100 nF, a buck to
3.3 V) through the app with `-experimental conducted`: the scan finds J1:+12V, FB1 and U1 (duty
0.275 from the rail names) and finishes in a few seconds. On the assumed defaults it is 0.9 dB under the
class B average limit at 500 kHz; entered as 1 MHz and 1 A, 16.8 dB under at 1 MHz. Removing C1,
the capacitor in front of the ferrite, costs 55 dB: with FB1 it is the filter.

Four private boards, read only:

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

## Still to do

- **A measurement.** No board has been compared with a LISN reading. Until one is, the level is
  an estimate and the what-ifs are the useful part.
- **Common mode.** Not modelled: it flows through the capacitance from the switch node and the
  board to the test bench's reference plane, which depends on the setup and the enclosure.
- **More regulators recognised**: controllers with external FETs, bucks the switch-node check
  misses, one source per switch node of a PMIC, boost and buck-boost input currents.
- **Part models**: inductor and ferrite self-resonance and a bead's resistive peak; DC-bias
  derating of ceramics (a 10 µF 0805 at its rated voltage has well below 10 µF).
- **Detectors**: spread spectrum, burst and pulse-skipping modes, which lower quasi-peak and
  average readings, are not modelled; every line is a steady tone.
