/**
 * The shape of `conducted.json`, written by the worker's conducted-emissions scan
 * (worker/emi_worker/stages/conducted.py), and the parameters a conducted run accepts
 * (server/emi/conducted.go).
 */

export type ConductedTopology = 'buck' | 'boost' | 'buck-boost' | 'inverting'

/** The numbers a user can give for one regulator. Anything left out is an assumed default. */
export interface ConductedRegulatorValues {
  frequency_hz?: number
  input_current_a?: number
  duty?: number
  rise_s?: number
  phase_deg?: number
  inductance_h?: number
}

export type ConductedParamName = keyof ConductedRegulatorValues

/** What the user says about one regulator, keyed by its id. */
export interface ConductedRegulatorParams extends ConductedRegulatorValues {
  /** Replaces the topology the scan recognised. */
  topology?: ConductedTopology
  /** Leaves the regulator out of the scan. */
  removed?: boolean
  /** The user checked what the scan recognised. */
  confirmed?: boolean
}

export interface ConductedParams {
  class?: 'A' | 'B'
  /** "J1:+12V": which power input, when a board has more than one. */
  entry?: string
  regulators?: Record<string, ConductedRegulatorParams>
}

export interface ConductedParamValue {
  value: number
  /**
   * "user", "assumed", "rail names" for a duty worked out from the net names, or "board" for an
   * inductance read from the part's value.
   */
  source: 'user' | 'assumed' | 'rail names' | 'board'
  assumed: boolean
}

export interface ConductedRegulator {
  /** The part's reference, or "U1/VLX1" for one output of a part with several. */
  id: string
  ref: string
  topology: ConductedTopology
  /** Where the topology came from: the layout, the part number, the user, or a default. */
  topology_from: 'layout' | 'part number' | 'user' | 'assumed'
  /** The topology the scan recognised, before any change by the user. */
  found_as: ConductedTopology
  /** How it was recognised, cue by cue. */
  found_by: string
  confidence: 'high' | 'medium' | 'low'
  confirmed: boolean
  switch_net: string
  input_net: string
  output_net: string
  inductor: string
  /** Sources of one part share its clock. */
  group: string
  x?: number
  y?: number
  /** Phase and inductance are absent from results written before they existed, and inductance
   * from any regulator that has no inductor value to read. */
  params: Record<Exclude<ConductedParamName, 'inductance_h' | 'phase_deg'>, ConductedParamValue> &
    Partial<Record<'inductance_h' | 'phase_deg', ConductedParamValue>>
  /** The parameter names still at an assumed value. */
  assumed: ConductedParamName[]
  /** A boost's input ripple, peak to peak. */
  input_ripple_a?: { value: number; source: string }
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
  /** Regulators the user removed: found, and left out of the scan. */
  removed?: { id: string; ref: string; topology: ConductedTopology; found_by: string; confidence: string; x?: number; y?: number }[]
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
