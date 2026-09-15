"""Driver spectra: what a real source pushes into a port, and how to re-weight a solve by it."""

from emi_worker.drivers.spectrum import (
    Trapezoid,
    corner_frequencies,
    envelope_v,
    piecewise_linear_series,
    reweight,
    trapezoid_series,
)

__all__ = [
    "Trapezoid",
    "corner_frequencies",
    "envelope_v",
    "piecewise_linear_series",
    "reweight",
    "trapezoid_series",
]
