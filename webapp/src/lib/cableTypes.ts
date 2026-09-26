/**
 * What a `cable` run writes (`cables.json`).
 *
 * Mirrors `worker/emi_worker/stages/cable.py`. The shape worth noticing is that a connector
 * appears in exactly one of three places, and they mean different things:
 *
 *   - in `cables` with a `cable_id` — modelled, and there is a budget for it;
 *   - in `cables` with `declared_none` — the user said it is never cabled, which is an answer;
 *   - in `unassigned` — nothing was declared, so it is **not modelled at all** and compliance
 *     treats it as incomplete rather than as zero.
 *
 * A UI that collapsed the last two would tell someone their board is covered when it is not.
 */

export interface CableBudgetPoint {
  frequency_hz: number
  limit_dbuv_per_m: number
  max_current_dbua: number
  e_per_amp: number
  radiation_peak: boolean
}

export interface CableResult {
  ref: string
  cable_id: string | null
  cable_name?: string
  length_m?: number
  far_end?: string
  shield?: string
  declared_none: boolean
  standard_id?: string
  distance_m?: number
  radiation_peaks_hz?: number[]
  /** True when the frequency grid cannot resolve a peak, so an empty list says nothing. */
  grid_too_coarse?: boolean
  tightest?: {
    frequency_hz: number
    max_current_dbua: number
    limit_dbuv_per_m: number
  } | null
  points?: CableBudgetPoint[]
}

export interface UnassignedConnector {
  ref: string
  footprint: string
  suggested: string | null
  reason: string
}

export interface CablesDoc {
  format: string
  format_version: number
  solver: string
  standard_id: string
  assumptions: string[]
  /**
   * How close a connector's nearest pad must come to the board edge for a solve to attach its
   * cable (the worker's `EDGE_TOLERANCE_MM`). Absent on runs from before it was written.
   */
  edge_tolerance_mm?: number
  cables: CableResult[]
  unassigned: UnassignedConnector[]
  notes: string[]
}
