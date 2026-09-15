"""Composing a solve and a cable into a field at three metres (§7).

    I_cm(f) = H_cm(f) · V_src(f) / Z_ant(f)
    E(f)    = I_cm(f) · E_per_amp(f)

Three separate things meet here, and each knows something the others do not:

* **H_cm** comes from the solve. It is what *this layout* puts across the connector per volt
  of source — the only term that changes when somebody moves a trace.
* **Z_ant** and **E_per_amp** come from the antenna solver. They are properties of the cable
  and the room, and would be the same on any board.
* **V_src** comes from the driver. Without one there is no absolute answer, only a shape.

The composition is exact for a linear system **provided the board and the cable interact only
through the gap**. M0 measured what that provisionally costs: 1.2 dB median and 2.2 dB worst
on a synthetic board, across a 40 dB swing in Z_ant. That residual is real — it is coupling
that does not pass through the gap — and it is the term §17.2's cable budget should eventually
be built from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EmissionPoint:
    frequency_hz: float
    #: Common-mode current the layout drives into this cable, amps.
    current_a: float
    #: Field at the standard's distance, V/m.
    field_v_per_m: float
    #: The limit there, in dBµV/m, and the margin against it.
    limit_dbuv_per_m: float

    @property
    def field_dbuv_per_m(self) -> float:
        if self.field_v_per_m <= 0:
            return float("-inf")
        return 20.0 * math.log10(self.field_v_per_m / 1e-6)

    @property
    def current_dbua(self) -> float:
        if self.current_a <= 0:
            return float("-inf")
        return 20.0 * math.log10(self.current_a / 1e-6)

    @property
    def margin_db(self) -> float:
        """Positive means under the limit. Negative is a predicted failure."""
        return self.limit_dbuv_per_m - self.field_dbuv_per_m


@dataclass
class Emission:
    ref: str
    cable_id: str
    standard_id: str
    points: list[EmissionPoint]
    #: Frequencies the driver does not drive, and why — carried through from §9.
    undriven: dict

    def worst(self) -> EmissionPoint | None:
        """The point with the least margin: the one a report leads with."""
        return min(self.points, key=lambda p: p.margin_db) if self.points else None


def compose(
    ref: str,
    cable_id: str,
    transfer: dict,
    source_volts: dict,
    antenna: dict,
    standard_id: str = "fcc-15b-radiated-3m",
) -> Emission:
    """Put the three halves together.

    ``transfer`` is one entry from ``cable_ports.json``; ``source_volts`` maps frequency to the
    driver's complex open-circuit voltage; ``antenna`` maps frequency to ``(z_ant, e_per_amp)``
    from the antenna solver.

    Only frequencies that all three know about produce a point. A frequency the driver does
    not drive is carried through as undriven rather than dropped, because §17.3's completeness
    gate reads that list — a quietly shorter list of points would look like a cleaner result.
    """
    from emi_worker.compliance.limits import limit_at

    freqs = transfer.get("frequencies_hz") or []
    h_real = transfer.get("h_real") or []
    h_imag = transfer.get("h_imag") or []
    usable = transfer.get("usable") or [True] * len(freqs)

    points: list[EmissionPoint] = []
    undriven: dict = {}
    for i, f in enumerate(freqs):
        if not usable[i]:
            undriven[f] = (
                f"the solve delivered no source energy at {f / 1e6:g} MHz, so the transfer "
                f"function there is a ratio of two small numbers rather than a measurement"
            )
            continue
        v_src = source_volts.get(f)
        if v_src is None:
            undriven[f] = f"the driver says nothing at {f / 1e6:g} MHz"
            continue
        pair = antenna.get(f)
        if pair is None:
            undriven[f] = f"the antenna solver was not run at {f / 1e6:g} MHz"
            continue
        z_ant, e_per_amp = pair
        if z_ant == 0:
            undriven[f] = f"the cable has zero input impedance at {f / 1e6:g} MHz"
            continue

        h = complex(h_real[i], h_imag[i])
        i_cm = abs(h * v_src / z_ant)
        points.append(EmissionPoint(
            frequency_hz=f,
            current_a=i_cm,
            field_v_per_m=i_cm * e_per_amp,
            limit_dbuv_per_m=limit_at(standard_id, f),
        ))

    return Emission(ref=ref, cable_id=cable_id, standard_id=standard_id,
                    points=points, undriven=undriven)


def antenna_terms(
    cable_id: str,
    length_m: float,
    frequencies_hz: list[float],
    *,
    height_m: float = 1.0,
    board_span_m: float = 0.1,
    distance_m: float = 3.0,
) -> dict:
    """Z_ant and E_per_amp for one cable, on the grid a solve already uses.

    §7 needs both, and only the antenna solver has them. Running it here — beside the solve
    that fitted the gap port — rather than in the browser is what lets a driver be attached
    later without a second trip to a worker: the result then carries every term except the
    driver's own voltage, which is the one thing §10 says must stay changeable afterwards.

    Costs about 2.5 ms a frequency. On the dense grid a solve writes, that is under a second.
    """
    from emi_worker.cables import nec
    from emi_worker.cables.library import get

    cable = get(cable_id)
    ring = nec.ObservationRing(distance_m=distance_m)
    z_real: list[float] = []
    z_imag: list[float] = []
    e_per_amp: list[float] = []
    for f in frequencies_hz:
        r = nec.run(nec.Deck(
            length_m=length_m, frequency_hz=float(f), height_m=height_m,
            board_span_m=board_span_m, far_end=cable.far_end, ring=ring,
        ))
        z_real.append(r.z_in.real)
        z_imag.append(r.z_in.imag)
        e_per_amp.append(r.e_per_amp())

    return {
        "cable_id": cable_id,
        "length_m": length_m,
        "height_m": height_m,
        "board_span_m": board_span_m,
        "distance_m": distance_m,
        "frequencies_hz": [float(f) for f in frequencies_hz],
        "z_real": z_real,
        "z_imag": z_imag,
        "e_per_amp": e_per_amp,
    }
