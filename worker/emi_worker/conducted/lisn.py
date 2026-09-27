"""The line impedance stabilisation network, one per supply line.

CISPR 16-1-2 (2003, 4.3) defines the 50 Ω/50 µH V-network for 0.15-30 MHz by its impedance
(Figure 1b: 50 Ω in parallel with 50 µH) with a ±20 % tolerance on the magnitude, not by its
parts. The parts here are the usual construction of that network: 50 µH from the supply side to
the EUT terminal, 1 µF from the supply side to ground, and 0.1 µF from the EUT terminal to the
receiver's 50 Ω with a 1 kΩ bleeder across it. The coupling capacitor moves the impedance off the
ideal curve at the bottom of the band; test_conducted_sim checks it stays inside the tolerance.

What a receiver reads, corrected by the test house for the network's voltage division factor,
is the voltage at the EUT terminal. That is the node reported, so no division factor is needed.
"""

from __future__ import annotations

import math

L_H = 50e-6
R_RECEIVER_OHM = 50.0
R_BLEED_OHM = 1000.0
C_COUPLING_F = 0.1e-6
C_SUPPLY_F = 1e-6
#: Magnitude tolerance of the impedance, CISPR 16-1-2 4.3.
TOLERANCE = 0.20
#: Phase tolerance later editions of CISPR 16-1-2 add. Checked too, but not taken from the text
#: of the edition that was read (2003), so it is reported rather than relied on.
PHASE_TOLERANCE_DEG = 11.5
#: The band CISPR 16-1-2 4.3 and FCC 15.107 cover.
F_LO_HZ = 150e3
F_HI_HZ = 30e6


def ideal_impedance(frequency_hz: float) -> complex:
    """The CISPR 16-1-2 Figure 1b curve: 50 Ω in parallel with 50 µH."""
    zl = 1j * 2 * math.pi * frequency_hz * L_H
    return R_RECEIVER_OHM * zl / (R_RECEIVER_OHM + zl)


def network_impedance(frequency_hz: float) -> complex:
    """The impedance of the construction above at its EUT terminal, supply side open.

    The same circuit lines() writes, in closed form, so a test can compare the two.
    """
    w = 2 * math.pi * frequency_hz
    port = R_RECEIVER_OHM * R_BLEED_OHM / (R_RECEIVER_OHM + R_BLEED_OHM)
    z_rx = port + 1 / (1j * w * C_COUPLING_F)
    z_supply = 1j * w * L_H + 1 / (1j * w * C_SUPPLY_F)
    return z_rx * z_supply / (z_rx + z_supply)


def lines(key: str, side: str, eut: str) -> list[str]:
    """One network from ``eut`` (the board's terminal) to ground; ``side`` is "p" or "n".

    The supply side has only its capacitor to ground: an ideal supply behind it would short the
    same node, and the network exists so that whatever is there does not matter. A 10 MΩ gives
    the node the DC path ngspice's operating point needs.
    """
    s = f"{key}_ls{side}"
    rx = f"{key}_lr{side}"
    return [
        f"L{key}_l{side} {s} {eut} {L_H:g}",
        f"C{key}_s{side} {s} 0 {C_SUPPLY_F:g}",
        f"R{key}_d{side} {s} 0 10meg",
        f"C{key}_c{side} {eut} {rx} {C_COUPLING_F:g}",
        f"R{key}_r{side} {rx} 0 {R_RECEIVER_OHM:g}",
        f"R{key}_b{side} {rx} 0 {R_BLEED_OHM:g}",
    ]


def dbuv(volts_rms: float) -> float:
    return 20.0 * math.log10(max(volts_rms, 1e-15) / 1e-6)
