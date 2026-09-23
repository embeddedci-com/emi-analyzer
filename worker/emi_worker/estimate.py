"""FDTD cost model.

This is the Python half of a computation that also lives in Go
(``server/emi/estimate.go``) and, later, in TypeScript in the webapp. All of them are
checked against ``server/emi/testdata/estimate_fixtures.json`` so they cannot drift.

The model is intentionally not clever. A user told "1.9 billion cells" should be able to
reproduce that on paper, because the alternative is a black box telling them their board
will take seventy days.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict

#: Speed of light, m/s.
SPEED_OF_LIGHT = 299_792_458.0

#: What openEMS's engine costs per Yee cell: six arrays (volt, curr, and the four operator
#: arrays vv, vi, iv, ii), three components each, float32.  6 * 3 * 4 = 72
BYTES_PER_CELL = 72

#: T_sim = periods / f_min
DEFAULT_PERIODS = 3.0

#: Measured openEMS throughput is roughly 150-250 MCells/s; it is memory-bandwidth bound.
DEFAULT_THROUGHPUT_MCELLS_PER_S = 200.0

#: An upper bound on fill_factor, to catch a caller passing a cell count by mistake rather
#: than a ratio. The largest value measured on a real board is 8.75.
MAX_FILL_FACTOR = 50.0

#: Per-preset lower bounds on the mesh multiplier, from the measurements above, rounded down.
#: These are the *minimum* observed rather than the median on purpose: the panel that shows
#: this says "At least", and a median would be wrong for half of all boards.
MESH_MULTIPLIER_FLOOR = {"coarse": 4.6, "normal": 2.3, "fine": 1.1}

#: The smallest in-plane cell as a fraction of the requested one. The mesher merges lines
#: closer than this (``mesh.MERGE_FRACTION``), and on a routed board copper edges put lines
#: that close everywhere, so the smallest cell -- which sets the timestep -- is a quarter of
#: dx, not dx. Measured on four real boards, 36 regions: the smallest cell was 37.5 um in 11
#: of 12 coarse regions (dx 150; 29.8 in the other), 18.75 um in every normal one (dx 75) and
#: 8.7-12.5 um at fine (dx 50). Taking dx itself, as this did, put the step count 2.0-3.4x
#: low; a quarter of dx leaves it at most 1.4x low, and never high.
IN_PLANE_MIN_CELL_FRACTION = 0.25


@dataclass(frozen=True)
class EstimateInput:
    """Everything the cost model needs.

    Extents are in millimetres and include the air box above and below the board.
    Resolutions are in micrometres.

    The Courant limit keys off the smallest cell in *any* axis, and in plane that is a
    quarter of the requested resolution (``IN_PLANE_MIN_CELL_FRACTION``), because copper edges
    put grid lines that close on any routed board. So it is usually ``dx_um``, not ``dz_um``,
    that sets the timestep: 12.5 um in plane against 25 um vertically at the fine preset.
    """

    roi_x_mm: float
    roi_y_mm: float
    roi_z_mm: float
    dx_um: float
    dy_um: float
    dz_um: float
    f_min_hz: float
    ports: int = 1

    #: The real mesh's cell count as a multiple of the uniform bounding-box count. 1.0 means
    #: uniform. Grading changes the cell count but NOT the timestep -- dt is pinned by the
    #: smallest cell, and grading is precisely the technique of keeping some cells small.
    #:
    #: **This is usually greater than 1**, which is the opposite of what the name suggests
    #: and of what this model originally assumed. ``dx_um`` is a *floor* on cell size, not
    #: the spacing: the mesher puts a line at every copper edge, and a routed board has
    #: edges far closer together than any preset. Measured on four real boards
    #: (``scripts/measure_fill_factor.py``, September 2026, after the mesher's grading fix and
    #: the thirds rule), the in-plane axes come out 2.0-4.0x denser than uniform while the
    #: vertical axis, whose air is graded coarsely, comes out 0.26-0.55x. In-plane wins:
    #:
    #:     coarse  min 4.67  median 7.53  max 8.65
    #:     normal  min 2.31  median 4.66  max 5.08
    #:     fine    min 1.14  median 2.90  max 3.22
    #:
    #: The first calibration (3.55 / 1.49 / 0.70 at the minimum) was made against the mesher
    #: before it enforced its grading bound, which cost 30-60 % more cells.
    #:
    #: The preset dependence is not noise. Feature spacing is set by the board, so the
    #: coarser the request, the more the copper dominates -- which is why one constant
    #: cannot be right for all three. See MESH_MULTIPLIER_FLOOR.
    #:
    #: The name is kept because it is a wire field; the meaning is what is written here.
    fill_factor: float = 1.0

    periods: float = DEFAULT_PERIODS
    throughput_mcells_per_s: float = DEFAULT_THROUGHPUT_MCELLS_PER_S


@dataclass(frozen=True)
class Estimate:
    cells: int
    ram_bytes: int
    timesteps: int
    dt_seconds: float
    sim_time_seconds: float
    eta_seconds: float

    def as_dict(self) -> dict:
        return asdict(self)


class EstimateError(ValueError):
    """Raised when the input cannot describe a real simulation."""


def estimate(inp: EstimateInput) -> Estimate:
    """Compute the cost of a solve run.

    Every number follows from four facts and nothing else::

        dmin   = min(dx/4, dy/4, dz)    the smallest cell the mesher makes (see below)
        dt     = dmin / (c * sqrt(3))   Courant limit, set by the SMALLEST cell, any axis
        T_sim  = periods / f_min        long enough to resolve the lowest frequency
        steps  = T_sim / dt
        cells  = fill * product of ceil(extent / resolution) over the three axes
    """
    if min(inp.roi_x_mm, inp.roi_y_mm, inp.roi_z_mm) <= 0:
        raise EstimateError("region extents must be positive")
    if min(inp.dx_um, inp.dy_um, inp.dz_um) <= 0:
        raise EstimateError("mesh resolutions must be positive")
    if inp.f_min_hz <= 0:
        raise EstimateError("f_min must be positive")
    if inp.ports <= 0:
        raise EstimateError("ports must be positive")
    if not 0 < inp.fill_factor <= MAX_FILL_FACTOR:
        raise EstimateError(f"fill_factor must be in (0, {MAX_FILL_FACTOR:g}]")

    periods = inp.periods if inp.periods > 0 else DEFAULT_PERIODS
    throughput = (
        inp.throughput_mcells_per_s
        if inp.throughput_mcells_per_s > 0
        else DEFAULT_THROUGHPUT_MCELLS_PER_S
    )

    # Extents are mm, resolutions um, so extent * 1000 / res is dimensionless.
    nx = math.ceil(inp.roi_x_mm * 1000.0 / inp.dx_um)
    ny = math.ceil(inp.roi_y_mm * 1000.0 / inp.dy_um)
    nz = math.ceil(inp.roi_z_mm * 1000.0 / inp.dz_um)
    cells = math.ceil(nx * ny * nz * inp.fill_factor)

    # Courant limit, from the smallest cell in any axis, converted um -> m. In plane that is a
    # quarter of the requested cell, not the cell itself: see IN_PLANE_MIN_CELL_FRACTION.
    dmin_m = min(inp.dx_um * IN_PLANE_MIN_CELL_FRACTION,
                 inp.dy_um * IN_PLANE_MIN_CELL_FRACTION, inp.dz_um) * 1e-6
    dt = dmin_m / (SPEED_OF_LIGHT * math.sqrt(3))

    sim_time = periods / inp.f_min_hz
    steps = math.ceil(sim_time / dt)

    # Wall clock. openEMS runs one full pass per excited port.
    cell_updates = cells * steps * inp.ports
    eta = cell_updates / (throughput * 1e6)

    return Estimate(
        cells=cells,
        ram_bytes=cells * BYTES_PER_CELL,
        timesteps=steps,
        dt_seconds=dt,
        sim_time_seconds=sim_time,
        eta_seconds=eta,
    )
