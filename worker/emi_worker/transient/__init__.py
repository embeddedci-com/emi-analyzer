"""Transient simulation: how much of a discharge reaches an IC pin.

The geometric checks say a clamp is missing or far away. This package puts a number on it by
building a circuit of each exposed line -- connector, trace, clamp, ground via, IC pin -- and
driving it with the standard's discharge current in ngspice. See
docs/esd-transient-simulation.md for the design and for what the numbers cannot say.
"""
