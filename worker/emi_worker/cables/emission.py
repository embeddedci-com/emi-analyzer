"""The antenna terms a solve carries so a driver can be attached afterwards (§7, §10).

    I_cm(f) = |H_cm(f)| · |Z_s + Z_in| / |Z_d + Z_in| · V_d(f) / |Z_ant(f)|
    E(f)    = I_cm(f) · E_per_amp(f)

Three separate things meet in that composition, and each knows something the others do not:

* **H_cm** comes from the solve. It is what *this layout* puts across the connector per volt
  of source — the only term that changes when somebody moves a trace.
* **Z_ant** and **E_per_amp** come from the antenna solver, here. They are properties of the
  cable and the room, and would be the same on any board.
* **V_d** and **Z_d** come from the driver. Without one there is no absolute answer.

The composition itself is ``compliance.assemble.compose_cable``, which the estimate and the
Cables tab's chart share. It is exact for a linear system **provided the board and the cable
interact only through the gap**. M0 measured what that provisionally costs: 1.2 dB median and
2.2 dB worst on a synthetic board; cable test 4 measures it on real boards
(docs/verification/cables-and-drivers.md).
"""

from __future__ import annotations


def antenna_terms(
    cable_id: str,
    length_m: float,
    frequencies_hz: list[float],
    *,
    height_m: float | None = None,
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
    if height_m is None:
        height_m = nec.TABLE_HEIGHT_M
    ring = nec.ObservationRing(distance_m=distance_m)
    z_real: list[float] = []
    z_imag: list[float] = []
    e_per_amp: list[float] = []
    for f in frequencies_hz:
        # The choke too, as the budget has it. Leaving it out made a choked cable's Tier B
        # emission identical to an unchoked one's.
        r = nec.run(nec.Deck(
            length_m=length_m, frequency_hz=float(f), height_m=height_m,
            board_span_m=board_span_m, far_end=cable.far_end, ring=ring,
            choke_z=cable.cm_choke.z_at(float(f)) if cable.cm_choke else None,
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
