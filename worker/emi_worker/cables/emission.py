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
Cables tab's chart share. It is Thévenin's theorem at the gap, and it holds on two conditions:

* **Z_ant is the impedance at the gap of the board and cable together.** With the board
  as a plate that is what nec2c computes, so the board's side of the source impedance is
  inside Z_ant and nothing is added to it. The board as a thin wire was the wrong antenna:
  5-12 dB too much impedance below the first resonance on real board outlines.
* **V_oc does not change when the cable is attached.** It does: a 10 mm stub sits in the
  board's near field and floats at part of the board's potential, a cable does not. In
  nec2c models the difference is 0-2 dB for a source next to the connector and up to 7 dB
  for one across the board, always in the direction of too little current. The solve
  cannot see it, because the solve has no cable.

The numbers are in docs/verification/cables-and-drivers.md §4.
"""

from __future__ import annotations


def antenna_terms(
    cable_id: str,
    length_m: float,
    frequencies_hz: list[float],
    *,
    height_m: float | None = None,
    board_span_m: float = 0.1,
    board_width_m: float | None = None,
    board_offset_m: float | None = None,
    distance_m: float = 3.0,
) -> dict:
    """Z_ant and E_per_amp for one cable, on the grid a solve already uses.

    §7 needs both, and only the antenna solver has them. Running it here — beside the solve
    that fitted the gap port — rather than in the browser is what lets a driver be attached
    later without a second trip to a worker: the result then carries every term except the
    driver's own voltage, which is the one thing §10 says must stay changeable afterwards.

    The board is a plate when ``board_width_m`` is given (``board_arm`` reads it from the
    outline), and a thin wire otherwise. A plate costs about 0.3 s a frequency over the ground
    plane, about 12 s for a 100 x 80 mm board on a solve's 60-point grid at two cores; the wire
    cost 2.5 ms.
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
    # The choke too, as the budget has it. Leaving it out made a choked cable's Tier B
    # emission identical to an unchoked one's.
    decks = [nec.Deck(
        length_m=length_m, frequency_hz=float(f), height_m=height_m,
        board_span_m=board_span_m, board_width_m=board_width_m,
        board_offset_m=board_offset_m, far_end=cable.far_end, ring=ring,
        choke_z=cable.cm_choke.z_at(float(f)) if cable.cm_choke else None,
    ) for f in frequencies_hz]
    for r in nec.run_many(decks):
        z_real.append(r.z_in.real)
        z_imag.append(r.z_in.imag)
        e_per_amp.append(r.e_per_amp())

    return {
        "cable_id": cable_id,
        "length_m": length_m,
        "height_m": height_m,
        "board_span_m": board_span_m,
        "board_width_m": board_width_m,
        "board_offset_m": board_offset_m,
        "distance_m": distance_m,
        "frequencies_hz": [float(f) for f in frequencies_hz],
        "z_real": z_real,
        "z_imag": z_imag,
        "e_per_amp": e_per_amp,
    }


def board_arm(exit_normal, anchor_mm, extent_mm) -> dict:
    """The board as the antenna's other arm, from its outline's bounding box, in metres.

    ``board_span_m`` runs along the cable's exit, ``board_width_m`` across it, and
    ``board_offset_m`` is where the cable leaves, measured across from the low edge.
    ``extent_mm`` is (x0, y0, x1, y1) in the same frame as ``anchor_mm``.
    """
    x0, y0, x1, y1 = extent_mm
    along_x = abs(exit_normal[0]) >= abs(exit_normal[1])
    span, width = ((x1 - x0), (y1 - y0)) if along_x else ((y1 - y0), (x1 - x0))
    across = (anchor_mm[1] - y0) if along_x else (anchor_mm[0] - x0)
    return {
        "board_span_m": max(0.01, span / 1000.0),
        "board_width_m": max(0.01, width / 1000.0),
        "board_offset_m": min(max(across, 0.0), width) / 1000.0,
    }
