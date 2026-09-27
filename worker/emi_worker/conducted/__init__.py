"""Conducted emissions: what a board's switching regulators put back onto its power input.

Experimental, and differential mode only. The board's power input is connected to two
CISPR 16-1-2 line impedance stabilisation networks (one per line, as a test house does), the
input filter is built from the layout -- the capacitors on the input rail with their library
ESR and ESL, the trace and via inductance between them, any ferrite or inductor in series --
and each switching regulator is a trapezoidal current source at its input pin. ngspice solves
the transfer from every source to the LISN; the source's harmonics times that transfer is the
voltage a receiver would read, compared with FCC 15.107.

What it is not: a common-mode model (that current flows through the capacitance from the
switch node and the board to the test bench's reference plane, which depends on the setup and
the enclosure), a model of the regulator's own behaviour (spread spectrum, burst mode,
light-load skipping) or of the external power supply's filter. See docs/known-issues.md.
"""
