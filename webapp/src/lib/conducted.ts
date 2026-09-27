/**
 * The browser's half of the conducted-emissions scan: turning what a user types into run
 * params, and a result into the few sentences the panel shows.
 *
 * The inputs are shown in the units a datasheet uses (kHz, A, %, ns) and sent in SI, the units
 * the worker and the server check (worker/emi_worker/conducted/sources.py, server/emi/conducted.go).
 */

import type {
  ConductedDoc, ConductedParamName, ConductedParams, ConductedRegulator, ConductedRegulatorParams, ConductedTopology,
  ConductedWorst,
} from './conductedTypes'

export interface ParamField {
  name: ConductedParamName
  label: string
  unit: string
  /** Multiply a displayed value by this to get SI. */
  scale: number
  min: number
  max: number
  decimals: number
}

/** The ranges are the worker's, in display units. */
export const PARAM_FIELDS: ParamField[] = [
  { name: 'frequency_hz', label: 'Frequency', unit: 'kHz', scale: 1e3, min: 10, max: 10000, decimals: 1 },
  { name: 'input_current_a', label: 'Input current', unit: 'A', scale: 1, min: 0.001, max: 100, decimals: 3 },
  { name: 'duty', label: 'Duty', unit: '%', scale: 0.01, min: 2, max: 98, decimals: 1 },
  { name: 'rise_s', label: 'Edge', unit: 'ns', scale: 1e-9, min: 0.1, max: 1000, decimals: 1 },
  { name: 'inductance_h', label: 'Inductor', unit: 'µH', scale: 1e-6, min: 0.01, max: 10000, decimals: 2 },
  { name: 'phase_deg', label: 'Phase', unit: '°', scale: 1, min: 0, max: 360, decimals: 0 },
]

export const TOPOLOGIES: { value: ConductedTopology; label: string }[] = [
  { value: 'buck', label: 'Buck' },
  { value: 'boost', label: 'Boost' },
  { value: 'buck-boost', label: 'Buck-boost' },
  { value: 'inverting', label: 'Inverting' },
]

/**
 * What the user has said, per regulator id: numbers in display units (a missing one stays
 * assumed), a changed topology, and whether it is removed or confirmed.
 */
export type Entered = Record<string, Partial<Record<ConductedParamName, number>> & {
  topology?: ConductedTopology
  removed?: boolean
  confirmed?: boolean
}>

/**
 * The fields a regulator's waveform depends on: a boost has no switching edge in its input
 * current but an inductor ripple, and the phase matters only between sources on one clock.
 */
export function fieldsFor(reg: ConductedRegulator, topology: ConductedTopology, shared: boolean): ParamField[] {
  return PARAM_FIELDS.filter((f) => {
    if (f.name === 'rise_s') return topology !== 'boost'
    if (f.name === 'inductance_h') return topology === 'boost'
    if (f.name === 'phase_deg') return shared || reg.params.phase_deg?.source === 'user'
    return true
  })
}

export function toDisplay(field: ParamField, si: number): number {
  const v = si / field.scale
  const f = 10 ** field.decimals
  return Math.round(v * f) / f
}

/** The entered values as run params, in SI, dropping anything out of range or empty. */
export function conductedParams(cls: 'A' | 'B', entered: Entered, entry?: string | null): ConductedParams {
  const regulators: Record<string, ConductedRegulatorParams> = {}
  for (const [ref, fields] of Object.entries(entered)) {
    const out: ConductedRegulatorParams = {}
    for (const field of PARAM_FIELDS) {
      const v = fields[field.name]
      if (typeof v !== 'number' || !Number.isFinite(v) || v < field.min || v > field.max) continue
      out[field.name] = v * field.scale
    }
    if (fields.topology) out.topology = fields.topology
    if (fields.removed) out.removed = true
    if (fields.confirmed) out.confirmed = true
    if (Object.keys(out).length > 0) regulators[ref] = out
  }
  const params: ConductedParams = { class: cls }
  if (entry) params.entry = entry
  if (Object.keys(regulators).length > 0) params.regulators = regulators
  return params
}

/** The values a previous run was given, back in display units, so an edit starts from them. */
export function enteredFrom(params: ConductedParams | null | undefined): Entered {
  const out: Entered = {}
  for (const [ref, given] of Object.entries(params?.regulators ?? {})) {
    const fields: Entered[string] = {}
    for (const field of PARAM_FIELDS) {
      const v = given[field.name]
      if (typeof v === 'number') fields[field.name] = toDisplay(field, v)
    }
    if (given.topology) fields.topology = given.topology
    if (given.removed) fields.removed = true
    if (given.confirmed) fields.confirmed = true
    out[ref] = fields
  }
  return out
}

/**
 * A result written before regulators had ids and topologies (format 1): every regulator was a
 * buck found by its switch node, one source per part.
 */
export function upgradeDoc(doc: ConductedDoc): ConductedDoc {
  if (doc.format >= 2) return doc
  return {
    ...doc,
    regulators: doc.regulators.map((r) => ({
      ...r,
      id: r.id ?? r.ref,
      topology: r.topology ?? 'buck',
      topology_from: r.topology_from ?? 'layout',
      found_as: r.found_as ?? 'buck',
      found_by: r.found_by ? `switch node ${r.switch_net} (${r.found_by})` : `switch node ${r.switch_net}`,
      confidence: r.confidence ?? 'medium',
      confirmed: r.confirmed ?? false,
      inductor: r.inductor ?? '',
      group: r.group ?? r.ref,
    })),
  }
}

export function fmtHz(hz: number): string {
  if (hz >= 1e6) return `${(hz / 1e6).toFixed(hz >= 10e6 ? 1 : 2)} MHz`
  return `${(hz / 1e3).toFixed(0)} kHz`
}

/** One line about the worst harmonic: whether it passes, by how much, and where. */
export function verdict(worst: ConductedWorst | null): string {
  if (!worst) return 'No harmonic falls between 150 kHz and 30 MHz.'
  const where = `at ${fmtHz(worst.f_hz)} (${worst.detector} limit)`
  const by = Math.abs(worst.margin_db).toFixed(1)
  return worst.margin_db < 0 ? `Over the limit by ${by} dB ${where}.` : `Under the limit by ${by} dB ${where}.`
}

/** Regulators with at least one assumed setting, and which: "U3 (frequency, current)". */
export function assumedSummary(doc: ConductedDoc): string | null {
  const short: Record<ConductedParamName, string> = {
    frequency_hz: 'frequency', input_current_a: 'current', duty: 'duty', rise_s: 'edge',
    inductance_h: 'inductor', phase_deg: 'phase',
  }
  const parts = doc.regulators
    .filter((r) => r.assumed.length > 0)
    .map((r) => `${r.id ?? r.ref} (${r.assumed.map((n) => short[n]).join(', ')})`)
  return parts.length > 0 ? parts.join(', ') : null
}

/** A dB change as a signed phrase: "+12.3 dB" for more margin. */
export function fmtChange(db: number | null): string {
  if (db === null) return '—'
  return `${db >= 0 ? '+' : ''}${db.toFixed(1)} dB`
}

/** A fraction as a percentage a reader can compare: "0.012 %", not "0.0 %". */
export function fmtShare(share: number): string {
  const pct = share * 100
  if (pct >= 10) return `${pct.toFixed(0)} %`
  if (pct >= 0.001) return `${Number(pct.toPrecision(2))} %`
  return 'less than 0.001 %'
}

/** Log-frequency x for the chart, 150 kHz to 30 MHz across [x0, x1]. */
export function xOf(hz: number, x0: number, x1: number): number {
  const lo = Math.log10(150e3)
  const hi = Math.log10(30e6)
  return x0 + ((Math.log10(hz) - lo) / (hi - lo)) * (x1 - x0)
}
