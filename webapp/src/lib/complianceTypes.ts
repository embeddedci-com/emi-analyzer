/**
 * What a `compliance` run writes (`compliance.json`). Mirrors
 * `worker/emi_worker/stages/compliance.py`.
 *
 * The shape to notice is that **`margin_db` and `confidence_uncalibrated` are absent, not null,
 * when the inputs are incomplete** (§17.3). That is the contract: a warning beside a number is
 * something a reader can skip, and an export or an API client will happily print the number
 * without the caveat. So the field simply is not there, and every consumer has to decide what
 * to show in its place — which is the `gaps` list, doubling as the to-do list.
 */

export interface ComplianceGap {
  key: string
  message: string
  /** Which tab fixes it: "drivers" | "cables" | "solve" | "product". */
  fixed_on: string
}

export interface SpectrumPoint {
  frequency_hz: number
  /** RMS level. Null where nothing radiates: between a clock's harmonics, or on a null. */
  field_dbuv_per_m: number | null
  limit_dbuv_per_m: number
  /** Which detector the limit here is read with (47 CFR 15.35). */
  detector?: string
  /** Paths that say nothing at this frequency, so the level here is a lower bound. */
  uncovered?: string[]
}

/** What the gate was given, derived by the worker from the board and the solve. */
export interface ComplianceInputs {
  solve_run_id?: string | null
  driver: { id: string; name: string; kind: string; net: string | null } | null
  connectors: string[]
  modelled_cables: Record<string, { cable_id: string; length_m: number }>
  excited_ports: string[]
  far_field_ports: string[]
  covered_hz: Record<string, [number, number]>
  required_hz: [number, number] | null
  declared: Record<string, unknown>
}

export interface Contribution {
  label: string
  kind: string
  driver_id: string
  field_dbuv_per_m: number
  /** Share of the total power at the worst frequency, 0..1. */
  share: number
}

export interface RecommendationItem {
  finding_id: string
  rule: string
  severity: string
  title: string
  detail: string
  net: string
  distance_mm: number | null
}

export interface ComplianceDoc {
  format: string
  format_version: number
  standard_id: string
  standard: string
  distance_m: number | null
  scan: string
  complete: boolean
  gaps: ComplianceGap[]
  spectrum: SpectrumPoint[]
  paths: {
    kind: string
    label: string
    driver_id: string
    sigma_terms: Record<string, number>
    points?: number
    line_spectrum?: boolean
  }[]
  /** Always true today: the estimate is experimental and uncalibrated. */
  experimental?: boolean
  inputs?: ComplianceInputs
  /** What the result says about itself that is not a gap. */
  notes?: string[]
  uncertainty_note: string
  /** Present only when nothing radiating has been modelled at all. */
  no_paths?: string

  // ---- present only when `complete` ----
  worst?: { frequency_hz: number; field_dbuv_per_m: number; limit_dbuv_per_m: number }
  margin_db?: number
  sigma_db?: number
  sigma_terms?: Record<string, number>
  /**
   * P(true margin > 0) under the model's own budget. Not a pass probability, and uncalibrated
   * until lab results are recorded against it -- which is why this is its only name.
   */
  confidence_uncalibrated?: number
  range_80_db?: [number, number]
  contributions?: Contribution[]
  /** True when a driver contributed through more than one path, so shares are indicative. */
  shares_indicative?: boolean
  near_misses?: { frequency_hz: number; margin_db: number; field_dbuv_per_m: number }[]
  recommendations?: {
    path_kind: string
    path_label: string
    general_only: boolean
    items: RecommendationItem[]
    general: string[]
  }
}

/** True when this document is allowed to show a number at all. */
export const hasMargin = (d: ComplianceDoc): boolean =>
  d.complete && typeof d.margin_db === 'number'

export const fmtHz = (f: number): string =>
  f >= 1e9 ? `${(f / 1e9).toFixed(2)} GHz` : `${(f / 1e6).toFixed(f / 1e6 >= 10 ? 0 : 1)} MHz`
