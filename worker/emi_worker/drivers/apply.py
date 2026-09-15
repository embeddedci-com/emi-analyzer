"""Applying a driver to a finished solve (§8, §10).

The solve recorded fields in whatever units its Gaussian excitation happened to produce,
stored as dB relative to one shared peak. A driver turns those into absolute units, and §10
asks for that to arrive as **one dB offset per frequency** so the near-field shader can add a
uniform rather than rewriting a texture.

    displayed dBuA/m  =  stored dB (relative to peak)  +  offset_db(f)

with the offset carrying both halves of the conversion:

    offset_db(f) = 20*log10(reference_magnitude / 1e-6)      relative -> absolute uA/m
                 + 20*log10(|I_new(f) / I_solve(f)|)         the driver's current

Keeping them in one number is deliberate. Two numbers invite a caller to apply one and forget
the other, and forgetting the micro prefix is a 120 dB error that still looks like a plausible
emissions figure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from emi_worker.drivers.document import Driver
from emi_worker.drivers.resolve import Resolved, resolve
from emi_worker.drivers.spectrum import DriverError, reweight

#: One microamp per metre, the reference for dBuA/m.
MICRO = 1e-6

#: Below this fraction of the driver's own peak, a harmonic is a spectral null rather than a
#: small number. A 50 % duty clock has no even harmonics at all; what the transform returns
#: there is a rounding residue around 1e-16, and taking its logarithm turns a few ulp into
#: tens of dB. Reporting "-204 dBuA/m" would be noise presented as a measurement, and two
#: implementations would not even agree on the noise.
NULL_FLOOR = 1e-9


@dataclass(frozen=True)
class PortSpectrum:
    """What ``ports.json`` records for one port (see ``openems/post.py``)."""

    frequencies_hz: list[float]
    v: list[complex]
    i: list[complex]

    @classmethod
    def from_json(cls, block: dict) -> "PortSpectrum":
        f = [float(x) for x in block["frequencies_hz"]]
        v = [complex(re, im) for re, im in zip(block["v_real"], block["v_imag"])]
        i = [complex(re, im) for re, im in zip(block["i_real"], block["i_imag"])]
        if not (len(f) == len(v) == len(i)):
            raise DriverError("port spectrum arrays have different lengths")
        return cls(f, v, i)

    def at(self, frequency_hz: float, tolerance_ppm: float = 100.0) -> tuple[complex, complex]:
        for k, f in enumerate(self.frequencies_hz):
            if f == frequency_hz or abs(f - frequency_hz) <= frequency_hz * tolerance_ppm / 1e6:
                return self.v[k], self.i[k]
        raise DriverError(
            f"the solve recorded no port spectrum at {frequency_hz / 1e6:g} MHz; it has "
            f"{', '.join(f'{x / 1e6:g}' for x in self.frequencies_hz[:8])} MHz"
        )


@dataclass
class Applied:
    """One offset per frequency, and why any of them are missing."""

    frequencies_hz: list[float]
    #: dB to add to a stored value to read it in dBuA/m. ``None`` where not driven.
    offset_db: list[float | None]
    undriven: dict[float, str]

    def is_complete(self) -> bool:
        return not self.undriven

    def as_json(self) -> dict:
        return {
            "unit": "dBuA/m",
            "frequencies_hz": self.frequencies_hz,
            "offset_db": self.offset_db,
            "complete": self.is_complete(),
            "undriven": {str(f): why for f, why in sorted(self.undriven.items())},
        }


def reference_offset_db(reference_magnitude: float) -> float:
    """The relative-to-absolute half of the offset, on its own.

    ``reference_magnitude`` is the peak |H| in A/m that every stored dB value is relative to
    (``post.build_artifacts``). This is the only place the micro prefix is applied.
    """
    if reference_magnitude <= 0:
        raise DriverError(
            "the solve reports a reference magnitude of zero, so it produced no field and "
            "there is nothing to put a unit on"
        )
    return 20.0 * math.log10(reference_magnitude / MICRO)


def apply_driver(
    driver: Driver,
    port: PortSpectrum,
    reference_magnitude: float,
    frequencies: list[float] | None = None,
    *,
    resolved: Resolved | None = None,
) -> Applied:
    """Offsets that turn this solve's stored dB into dBuA/m under ``driver``."""
    freqs = list(frequencies) if frequencies is not None else list(port.frequencies_hz)
    if not freqs:
        raise DriverError("ask for at least one frequency")
    source = resolved if resolved is not None else resolve(driver, freqs)
    if source.frequencies_hz != freqs:
        raise DriverError("the resolved driver does not cover the requested frequencies")

    reference = reference_offset_db(reference_magnitude)
    z_s = driver.values["source_impedance_ohm"].value
    # The driver's own scale, not the peak of whatever was asked for: a caller asking only
    # about 50 MHz must still be told that 50 MHz is a null.
    scale = source.scale_v

    offsets: list[float | None] = []
    undriven = dict(source.undriven)
    for k, f in enumerate(freqs):
        v_source = source.volts[k]
        if v_source is None:
            offsets.append(None)
            continue
        if scale > 0 and abs(v_source) < scale * NULL_FLOOR:
            # Driven, but on a null of the driver's own spectrum -- a real answer of zero,
            # which has no logarithm and nothing to show on a map.
            offsets.append(None)
            undriven[f] = (
                f"{f / 1e6:g} MHz falls on a null of this driver's spectrum, so it drives "
                f"no current there"
            )
            continue
        v_port, i_port = port.at(f)
        try:
            factor = abs(reweight(v_source, z_s, v_port, i_port))
        except DriverError as exc:
            offsets.append(None)
            undriven[f] = str(exc)
            continue
        if factor <= 0:
            offsets.append(None)
            undriven[f] = (
                f"{f / 1e6:g} MHz falls on a null of this driver's spectrum, so it drives "
                f"no current there"
            )
            continue
        offsets.append(reference + 20.0 * math.log10(factor))

    return Applied(freqs, offsets, undriven)
