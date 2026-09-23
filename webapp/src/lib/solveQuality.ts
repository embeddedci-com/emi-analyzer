/**
 * Is this field map worth drawing, and how?
 *
 * Measured on real solves, the maps themselves are smooth — there is no checkerboard
 * numerical noise to hide. The ways a map misleads are elsewhere:
 *
 *  - A layer shielded by a plane carries essentially no field. Its whole grid sits at the
 *    dynamic-range floor and its "peak" is numerical residue at the region boundary, a few
 *    dB above the floor. Drawn with a low gate it looks like hotspots at the edge.
 *  - A small region around a driven trace is near-field everywhere, so a gate far below the
 *    peak paints the entire rectangle and nothing stands out.
 *  - Traces are cut where they leave the region. Strong field at the boundary means values
 *    there are partly an artefact of that cut.
 *  - A run that stopped before its energy decayed is a transient snapshot, not a
 *    steady-state map, however smooth it looks.
 *
 * These functions put numbers on each, so the results panel can say so instead of drawing
 * everything the same way.
 */

/** A layer whose peak is within this much of the floor has no field worth drawing. */
export const NOISE_MARGIN_DB = 10
/** Strong field reaching the boundary: edge within this much of the grid's peak. */
export const TRUNCATION_MARGIN_DB = 10
/** Cells from the boundary counted as "the edge". */
export const EDGE_CELLS = 2
/** Energy that has not fallen at least this far means the map is not a steady state. */
export const UNUSABLE_ENERGY_DB = -20
/** Energy at or below this counts as settled even if the run carries no explicit flag. */
export const SETTLED_ENERGY_DB = -30

export interface GridQuality {
  peakDb: number
  belowNoise: boolean
  edgeDb: number
  truncated: boolean
  /** Share of cells at or above the gate, 0..1. */
  visibleShare: number
}

export function assessGrid(
  values: Float32Array,
  width: number,
  height: number,
  floorDb: number,
  gateDb: number,
): GridQuality {
  let peak = -Infinity
  let edge = -Infinity
  let visible = 0
  const n = Math.min(values.length, width * height)
  for (let i = 0; i < n; i++) {
    const v = values[i]
    if (v > peak) peak = v
    if (v >= gateDb) visible++
    const r = Math.floor(i / width)
    const c = i % width
    if (r < EDGE_CELLS || c < EDGE_CELLS || r >= height - EDGE_CELLS || c >= width - EDGE_CELLS) {
      if (v > edge) edge = v
    }
  }
  const belowNoise = peak <= floorDb + NOISE_MARGIN_DB
  return {
    peakDb: peak,
    belowNoise,
    edgeDb: edge,
    // A shielded layer's peak is at the edge too, but that is residue, not a cut trace.
    truncated: !belowNoise && edge >= peak - TRUNCATION_MARGIN_DB,
    visibleShare: n ? visible / n : 0,
  }
}

/**
 * A gate that shows the loudest part of the map rather than all of it: roughly the top fifth
 * of the cells that carry any field, rounded down to 5 dB and kept between -40 and -10.
 */
export function suggestedGate(values: Float32Array, floorDb: number): number {
  const live: number[] = []
  for (const v of values) if (v > floorDb + NOISE_MARGIN_DB) live.push(v)
  if (live.length === 0) return -20
  live.sort((a, b) => a - b)
  const at = live[Math.floor(live.length * 0.8)]
  return Math.max(-40, Math.min(-10, Math.floor(at / 5) * 5))
}

export type Convergence = 'converged' | 'partial' | 'unusable' | 'unknown'

/**
 * Decided from the energy itself as well as the run's flag. The flag alone called a run that
 * stopped at -4.5 dB "not fully converged — still informative", when its fields were still
 * ringing and the map was a snapshot of a transient.
 *
 * A run whose flag says it hit its timestep limit is unusable whatever its energy: the worker
 * marks every level, impedance and transfer function from it unusable and the compliance
 * estimate refuses it, so the map must not be the one place it still looks like a result.
 */
export function convergence(finalEnergyDb?: number | null, converged?: boolean | null): Convergence {
  if (converged === false) return 'unusable'
  if (finalEnergyDb === undefined || finalEnergyDb === null || !Number.isFinite(finalEnergyDb)) {
    return converged ? 'converged' : 'unknown'
  }
  if (finalEnergyDb > UNUSABLE_ENERGY_DB) return 'unusable'
  if (finalEnergyDb > SETTLED_ENERGY_DB) return 'partial'
  return 'converged'
}
