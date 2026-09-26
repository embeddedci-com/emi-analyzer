/**
 * The shape of `conducted.json`, written by the worker's conducted-emissions scan
 * (worker/emi_worker/stages/conducted.py), and the parameters a conducted run accepts
 * (server/emi/conducted.go).
 */

/** What the user says about one regulator. Anything left out is an assumed default. */
export interface ConductedRegulatorParams {
  frequency_hz?: number
  input_current_a?: number
  duty?: number
  rise_s?: number
}

export type ConductedParamName = keyof ConductedRegulatorParams

export interface ConductedParams {
  class?: 'A' | 'B'
  /** "J1:+12V": which power input, when a board has more than one. */
  entry?: string
  regulators?: Record<string, ConductedRegulatorParams>
}

export interface ConductedParamValue {
  value: number
  /** "user", "assumed", or "rail names" for a duty worked out from the net names. */
  source: 'user' | 'assumed' | 'rail names'
  assumed: boolean
}

export interface ConductedRegulator {
  ref: string
  switch_net: string
  found_by: string
  input_net: string
  output_net: string
  x?: number
  y?: number
  params: Record<ConductedParamName, ConductedParamValue>
  /** The parameter names still at an assumed value. */
  assumed: ConductedParamName[]
}

export interface ConductedSource {
  ref: string
  harmonic: number
  dbuv: number
}

export interface ConductedLine {
  f_hz: number
  dbuv: number
  sources: ConductedSource[]
  qp_limit: number
  avg_limit: number
  margin_qp_db: number
  margin_avg_db: number
}

export interface ConductedWorst {
  f_hz: number
  /** Limit minus level: negative fails. */
  margin_db: number
  dbuv: number
  detector: 'average' | 'quasi-peak'
  sources: ConductedSource[]
}

export type ConductedVariantId = 'as_laid_out' | 'cap_at_connector' | 'cap_at_regulator' | 'lc_filter'

export interface ConductedVariant {
  id: ConductedVariantId
  label: string
  lines: ConductedLine[]
  worst: ConductedWorst | null
}

export interface ConductedCap {
  ref: string
  value: string
  c_f: number
  esr_ohm: number
  esl_nh: number
  mount_nh: number
  distance_mm: number
  model: string
  assumed: boolean
  x?: number
  y?: number
}

export interface ConductedDoc {
  format: number
  entry: { id: string; connector: string; net: string; ground_net: string; x?: number; y?: number } | null
  entries: string[]
  rail_nets: string[]
  regulators: ConductedRegulator[]
  skipped: { ref: string; why: string }[]
  network: {
    caps: ConductedCap[]
    series: { ref: string; kind: string; value: string; l_nh: number; r_ohm: number; assumed: boolean }[]
    trace_nh: number
    routed: boolean
  }
  standard: { class: 'A' | 'B'; quasi_peak: string; average: string }
  variants: ConductedVariant[]
  worst: ConductedWorst | null
  dominant: {
    f_hz: number
    regulator: string
    harmonic: number
    /** Fraction of the regulator's current at that harmonic in each capacitor, and into the LISN. */
    shares: { ref: string; share: number }[]
  } | null
  suggestions: { id: ConductedVariantId; label: string; worst: ConductedWorst | null; change_db: number | null }[]
  /** How much the worst margin changes without each capacitor. Negative: the capacitor helps. */
  components: { ref: string; without_change_db: number | null }[]
  assumptions: string[]
  not_modelled: string[]
  notes: string[]
}
