/**
 * The shape of `transient.json`, written by the worker's ESD simulation
 * (worker/emi_worker/transient/simulate.py), and the parameters a transient run accepts
 * (server/emi/transient.go). Kept in its own file so the viewer's board types stay about
 * the board.
 */

/** A vendor SPICE model attached to one part value for a run. */
export interface TransientModelRef {
  part: string
  /** Object key from the upload endpoint; the server only accepts this organisation's uploads. */
  key: string
  filename: string
  subckt?: string
  /** Pad number on the footprint -> subcircuit pin name. */
  pins?: Record<string, string>
}

export interface TransientParams {
  standard?: string
  /** IEC 61000-4-2 contact discharge level: 1–4 is 2, 4, 6 or 8 kV. */
  level: number
  polarity?: 'both' | 'positive' | 'negative'
  lines?: string[]
  models?: TransientModelRef[]
}

export interface TransientSegment {
  length_mm: number
  td_ps: number
  z0_ohm: number
  vias: number
}

export type TransientVariantId =
  | 'as_laid_out'
  | 'clamp_at_connector'
  | 'ideal_ground'
  | 'reference_clamp'

export interface TransientVariant {
  id: TransientVariantId
  label: string
  worst_polarity: 'positive' | 'negative'
  v_pin_peak_v?: number
  i_pin_peak_a?: number
  e_pin_uj?: number
  v_clamp_peak_v?: number
  i_clamp_peak_a?: number
  v_connector_peak_v: number
  /** Nothing took the current: the node rose past a tenth of the charge voltage. */
  unclamped: boolean
  /** Sampled on the line's `t_ns` grid. */
  v_pin?: number[]
  i_clamp?: number[]
}

export type ClampModelKind = 'vendor' | 'table' | 'illustrative'

export interface TransientLine {
  net: string
  connector: { ref: string; pad: string }
  x: number
  y: number
  clamp: null | {
    ref: string
    pad: string
    part: string
    model: ClampModelKind
    source: string
    assumptions: string[]
  }
  series_resistor: null | { ref: string; ohm: number }
  through: null | { ref: string; pad: string }
  ic: null | { ref: string; pad: string; supply: string; supply_v: number }
  geometry: {
    routed: boolean
    trunk: TransientSegment
    clamp_stub: TransientSegment | null
    ic_stub: TransientSegment | null
    lead: TransientSegment | null
    clamp_ground_mm: number
    clamp_ground_nh: number
    via_nh: number
    /** Capacitance to ground on the IC's net, lumped at the pin. */
    net_c_nf?: number
  }
  notes: string[]
  variants?: TransientVariant[]
  t_ns?: number[]
  /** The simulation of this line failed; the message is ngspice's. */
  error?: string
}

export interface TransientModelCheck {
  name: string
  ok: boolean
  detail: string
}

export interface TransientModelResult {
  part: string
  file: string
  subckt: string
  status: 'accepted' | 'rejected'
  reasons: string[]
  checks: TransientModelCheck[]
}

export interface TransientDoc {
  format_version: number
  standard: string
  discharge: 'contact'
  level: number
  kv: number
  polarities: string[]
  /** Where the simulated source misses the standard's table. Empty when it conforms. */
  source_check: string[]
  reference_clamp: { part: string; model: string; assumptions: string[] }
  lines: TransientLine[]
  models: TransientModelResult[]
  assumptions: string[]
  notes: string[]
}
