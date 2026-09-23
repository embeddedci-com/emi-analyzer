/**
 * FDTD cost model — the browser's copy.
 *
 * This is the third implementation of one computation. The Go server uses it for admission
 * control, the Python worker recomputes it authoritatively at the mesh stage, and this one
 * runs live as the user drags the region of interest. All three are checked against
 * `server/emi/testdata/estimate_fixtures.json`, because a client that quotes two hours for
 * a run the worker then refuses is worse than no estimate at all.
 *
 * Keep it dumb and keep it matching. A user told "1.9 billion cells" should be able to
 * reproduce that on paper.
 */

export const SPEED_OF_LIGHT = 299_792_458.0

/**
 * openEMS's cost per Yee cell: six arrays (volt, curr, and the four operator arrays
 * vv, vi, iv, ii), three components each, float32. 6 * 3 * 4 = 72.
 */
export const BYTES_PER_CELL = 72

/** T_sim = periods / f_min */
export const DEFAULT_PERIODS = 3.0

/** Measured openEMS throughput is roughly 150–250 MCells/s; it is memory-bandwidth bound. */
export const DEFAULT_THROUGHPUT_MCELLS_PER_S = 200.0

/**
 * An upper bound on `fill_factor`, to catch a caller passing a cell count where a ratio
 * belongs. The largest value measured on a real board is 8.75.
 */
export const MAX_FILL_FACTOR = 50.0

/**
 * Per-preset lower bounds on the mesh multiplier, measured on four real boards by
 * `worker/scripts/measure_fill_factor.py` (September 2026, after the mesher's grading fix
 * and the thirds rule).
 *
 * These are usually **greater than 1**. `dx` and friends are a floor on cell size, not the
 * spacing: the mesher puts a line at every copper edge, and a routed board has edges far
 * closer than any preset. In-plane the mesh comes out 2.0–4.0× denser than uniform; the
 * vertical axis, whose air is graded coarsely, comes out 0.26–0.55×. In-plane wins.
 *
 *     coarse  min 4.67  median 7.53  max 8.65
 *     normal  min 2.31  median 4.66  max 5.08
 *     fine    min 1.14  median 2.90  max 3.22
 *
 * The *minimum* is used rather than the median because the panel says "At least". A median
 * would be wrong for half of all boards, in the direction that costs the user a day.
 */
export const MESH_MULTIPLIER_FLOOR: Record<string, number> = {
  coarse: 4.6,
  normal: 2.3,
  fine: 1.1,
}

/**
 * The smallest in-plane cell as a fraction of the requested one. The mesher merges lines
 * closer than a quarter of dx, and copper puts lines that close everywhere on a routed board,
 * so the cell that sets the timestep is dx / 4, not dx. Taking dx put the step count 2.0–4.5×
 * low on four real boards. Same constant in `estimate.py` and `estimate.go`.
 */
export const IN_PLANE_MIN_CELL_FRACTION = 0.25

export interface EstimateInput {
  /** Region of interest including the air box, in mm. */
  roi_x_mm: number
  roi_y_mm: number
  roi_z_mm: number
  /**
   * Requested cell size per axis, in um: the mesh preset.
   *
   * The Courant limit keys off the smallest cell in *any* axis, and in plane that is a
   * quarter of the request (IN_PLANE_MIN_CELL_FRACTION), because copper edges put grid lines
   * that close on any routed board. So it is usually dx, not dz, that sets the timestep.
   */
  dx_um: number
  dy_um: number
  dz_um: number
  /** Lowest frequency the run has to resolve, in Hz. */
  f_min_hz: number
  /** openEMS runs one full pass per excited port. */
  ports?: number
  /**
   * Fraction of the uniform bounding-box cell count a graded mesh actually produces.
   * 1.0 assumes uniform, which is the conservative answer; real graded PCB meshes land
   * around 0.10–0.20. Grading changes the cell count but *not* the timestep.
   */
  /**
   * The real mesh's cell count as a multiple of the uniform bounding-box count. Usually
   * GREATER than 1 — see MESH_MULTIPLIER_FLOOR.
   */
  fill_factor?: number
  periods?: number
  throughput_mcells_per_s?: number
}

export interface Estimate {
  cells: number
  ram_bytes: number
  timesteps: number
  dt_seconds: number
  sim_time_seconds: number
  eta_seconds: number
}

export class EstimateError extends Error {}

export function estimate(input: EstimateInput): Estimate {
  const {
    roi_x_mm, roi_y_mm, roi_z_mm,
    dx_um, dy_um, dz_um,
    f_min_hz,
    ports = 1,
    fill_factor = 1.0,
    periods = DEFAULT_PERIODS,
    throughput_mcells_per_s = DEFAULT_THROUGHPUT_MCELLS_PER_S,
  } = input

  if (Math.min(roi_x_mm, roi_y_mm, roi_z_mm) <= 0) {
    throw new EstimateError('region extents must be positive')
  }
  if (Math.min(dx_um, dy_um, dz_um) <= 0) {
    throw new EstimateError('mesh resolutions must be positive')
  }
  if (!(f_min_hz > 0)) throw new EstimateError('f_min must be positive')
  if (!(ports > 0)) throw new EstimateError('ports must be positive')
  if (!(fill_factor > 0 && fill_factor <= MAX_FILL_FACTOR)) {
    throw new EstimateError(`fill_factor must be in (0, ${MAX_FILL_FACTOR}]`)
  }

  const p = periods > 0 ? periods : DEFAULT_PERIODS
  const tput = throughput_mcells_per_s > 0
    ? throughput_mcells_per_s
    : DEFAULT_THROUGHPUT_MCELLS_PER_S

  // Extents are mm, resolutions um, so extent * 1000 / res is dimensionless.
  const nx = Math.ceil((roi_x_mm * 1000.0) / dx_um)
  const ny = Math.ceil((roi_y_mm * 1000.0) / dy_um)
  const nz = Math.ceil((roi_z_mm * 1000.0) / dz_um)
  const cells = Math.ceil(nx * ny * nz * fill_factor)

  // Courant limit, from the smallest cell in any axis, converted um -> m. In plane that is a
  // quarter of the requested cell: see IN_PLANE_MIN_CELL_FRACTION.
  const dminM = Math.min(
    dx_um * IN_PLANE_MIN_CELL_FRACTION,
    dy_um * IN_PLANE_MIN_CELL_FRACTION,
    dz_um,
  ) * 1e-6
  const dt = dminM / (SPEED_OF_LIGHT * Math.sqrt(3))

  const simTime = p / f_min_hz
  const steps = Math.ceil(simTime / dt)

  const cellUpdates = cells * steps * ports
  const eta = cellUpdates / (tput * 1e6)

  return {
    cells,
    ram_bytes: cells * BYTES_PER_CELL,
    timesteps: steps,
    dt_seconds: dt,
    sim_time_seconds: simTime,
    eta_seconds: eta,
  }
}

/** "3 h 20 m", "45 m", "2 d 4 h" — for a number the user is deciding to spend. */
export function formatDuration(seconds: number): string {
  if (!isFinite(seconds) || seconds <= 0) return '—'
  if (seconds < 60) return `${Math.round(seconds)} s`
  const m = Math.floor(seconds / 60) % 60
  const h = Math.floor(seconds / 3600) % 24
  const d = Math.floor(seconds / 86400)
  if (d > 0) return `${d} d ${h} h`
  if (h > 0) return `${h} h ${m} m`
  return `${m} m`
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`
  return `${(bytes / 1e3).toFixed(0)} kB`
}

export function formatCount(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} G`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} k`
  return `${n}`
}
