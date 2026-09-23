"""Cable resonance: does a clock harmonic land where a cable radiates best? (§4, Tier A)

This is the cheapest useful thing in the whole plan. It needs no solve — a cable's resonances
depend on the cable, not the layout — and it answers the question that decides whether a board
passes: not "does this cable radiate" (they all do) but "does it radiate *at the frequencies
this board actually produces*".

A 1 m cable peaks near 117 MHz. A 25 MHz clock's fifth harmonic is 125 MHz. That is the entire
finding, and it costs about 70 ms.

The suggestion is never the assignment. A connector with no `cables:` entry is skipped rather
than guessed at, because a cable the user did not declare is an assumption that would change
the result without appearing in it.
"""

from __future__ import annotations

from typing import Iterator

from .model import Finding, RuleContext
from .planes import severity

#: How close a harmonic has to be to a peak to be worth mentioning, as a fraction. A cable
#: resonance is broad — the Q of a wire in free space is low — so a harmonic within a tenth of
#: a peak is landing on it for practical purposes.
NEAR_FRACTION = 0.10

#: Frequencies the budget is evaluated at: a log grid across the radiated range. Fine enough
#: to resolve peaks, coarse enough to stay in the tens of milliseconds.
def _grid(f_min: float = 30e6, f_max: float = 1.2e9, points: int = 40) -> list[float]:
    step = (f_max / f_min) ** (1.0 / (points - 1))
    return [f_min * step ** k for k in range(points)]


def _harmonics(fundamental_hz: float, up_to_hz: float, limit: int = 40) -> list[float]:
    out = []
    n = 1
    while fundamental_hz * n <= up_to_hz and len(out) < limit:
        out.append(fundamental_hz * n)
        n += 1
    return out


def check_cable_resonance(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled("cable-resonance"):
        return

    settings = getattr(ctx, "settings", None)
    assignments = getattr(settings, "cables", None) or {}
    if not assignments:
        # Nothing declared. §5: an unassigned connector is not modelled, and guessing one here
        # would put a finding on a cable the user never said existed.
        return

    from emi_worker.cables import CableError, get
    from emi_worker.cables.budget import solver_budget
    from emi_worker.cables.nec import available

    if not available():
        return

    fundamental = float(ctx.setting("cable-resonance", "clock_hz") or 0.0)
    grid = _grid()

    for ref, spec in sorted(assignments.items()):
        cable_id = spec.get("type") if isinstance(spec, dict) else spec
        if not cable_id or cable_id == "none":
            continue
        try:
            cable = get(cable_id)
            if isinstance(spec, dict) and spec.get("length_m"):
                cable = cable.with_length(float(spec["length_m"]))
        except CableError:
            continue

        try:
            budget = solver_budget(cable, grid)
        except Exception:
            # The solver is a subprocess; a failure here must not take the whole upload with
            # it. Every other rule still has something to say about this board.
            continue

        peaks = budget.radiation_peaks()
        if not peaks:
            # Nothing to say either way. A coarse grid cannot tell "no resonance" from "not
            # looked at closely enough", and a finding built on that distinction would be
            # guessing.
            continue
        tightest = budget.tightest()

        hits = []
        if fundamental > 0:
            for h in _harmonics(fundamental, grid[-1]):
                for peak in peaks:
                    if abs(h - peak) <= peak * NEAR_FRACTION:
                        hits.append((h, peak))
                        break

        peak_text = ", ".join(f"{p / 1e6:.0f} MHz" for p in peaks[:4])
        if hits:
            worst = hits[0]
            # Quote the budget AT the frequency that lands, not the tightest point overall.
            # They are usually different, and the one a designer can act on is this one.
            nearest = min(budget.points, key=lambda p: abs(p.frequency_hz - worst[1]))
            at_hit = nearest.max_current_dbua
            yield Finding(
                rule="cable-resonance",
                severity=severity(ctx, "cable-resonance", "warning"),
                title=(
                    f"{ref} — {cable.name} radiates best at {worst[1] / 1e6:.0f} MHz, where "
                    f"harmonic {round(worst[0] / fundamental)} lands"
                ),
                detail=(
                    f"A {cable.length_m:g} m {cable.name} on {ref} is at its best as an "
                    f"antenna near {peak_text}. Harmonic "
                    f"{round(worst[0] / fundamental)} of the {fundamental / 1e6:g} MHz clock "
                    f"is {worst[0] / 1e6:.0f} MHz, which lands on the {worst[1] / 1e6:.0f} MHz "
                    f"peak. There the cable may carry no more than {at_hit:.0f} dBµA of "
                    f"common-mode current before it reaches the {budget.standard_id} limit — "
                    f"against {tightest.max_current_dbua:.0f} dBµA at its tightest point "
                    f"overall. Changing the cable length moves the peak; a common-mode choke "
                    f"at the connector moves the current."
                ),
                net="", x=0.0, y=0.0,
            )
        else:
            yield Finding(
                rule="cable-resonance",
                severity=severity(ctx, "cable-resonance", "info"),
                title=f"{ref} — {cable.name} radiates best near {peaks[0] / 1e6:.0f} MHz",
                detail=(
                    f"A {cable.length_m:g} m {cable.name} on {ref} peaks at "
                    f"{peak_text}. Nothing declared on this board lands there, which is the "
                    f"outcome worth keeping: the budget at its tightest point allows "
                    f"{tightest.max_current_dbua:.0f} dBµA of common-mode current against the "
                    f"{budget.standard_id} limit."
                ),
                net="", x=0.0, y=0.0,
            )
