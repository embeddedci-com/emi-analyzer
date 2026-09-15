"""The fast rules tier.

Geometry-only EMI checks that run in seconds on any worker, with no solver. This is the
part of the product people will actually open every day: a full-wave solve answers "how
bad", but these answer "where to look", and they answer it before the user has finished
reading the page.

Every check here is a *known mechanism*, not a heuristic someone liked:

* A signal that changes layer needs its return current to change layer too. If there is no
  nearby via tying the two reference planes together, the return takes a long detour and
  the loop it encloses becomes the antenna.
* A through via serving a top-to-inner connection leaves an unterminated stub, which is a
  quarter-wave resonator at a frequency you did not choose.
* A trace crossing a gap in its reference plane forces the return current around the gap.
  This is the single most common cause of a board failing radiated emissions.
* A trace longer than a twentieth of a wavelength radiates efficiently enough to matter.
* Copper near the board edge radiates from the edge rather than coupling back to the plane.

Each finding carries board-space coordinates so the UI can zoom to it. A finding the user
cannot locate is a complaint, not a diagnosis.
"""

from __future__ import annotations

from .checks import RULES, run_rules
from .model import Finding, RulesResult

__all__ = ["Finding", "RULES", "RulesResult", "run_rules"]
