/**
 * The `emi-driver` document (docs/emi-driver-format.md) — the browser's validator.
 *
 * The worker holds the other one (`worker/emi_worker/drivers/document.py`) and both assert
 * against `server/emi/testdata/driver_document_fixtures.json`. docs/emi-driver-format.md asks for
 * validation in both places for different reasons: this one tells someone their numbers do not make
 * sense while they are typing them, and the worker's refuses a document that arrived some other way
 * — BenchPod, a CI commit, a hand-edited file. If they disagree, a user saves something the worker
 * then rejects.
 *
 * **Every number carries where it came from.** The weakest source in a driver sets a term in
 * the confidence budget (docs/implementation.md §7.4), travels with the result, and is what the
 * Drivers panel puts on a chip. A document whose amplitude was measured and whose rise time was
 * guessed is not the same document as one measured throughout.
 */

import { DriverError, validateTrapezoid, type Trapezoid } from './driverSpectrum'

export const DRIVER_FORMAT = 'emi-driver'
export const DRIVER_VERSION = 1

/** The list in docs/emi-driver-format.md, worst last. */
export const SOURCES = ['scope', 'spectrum-analyzer', 'benchpod', 'datasheet', 'assumed'] as const
export type Source = (typeof SOURCES)[number]

/**
 * Provisional sigma per source, in dB, from docs/implementation.md §7.4. Engineering placeholders
 * that measured results replace with residuals from recorded lab results; the ordering is the part
 * that matters today.
 */
export const SOURCE_SIGMA_DB: Record<Source, number> = {
  scope: 1.0,
  'spectrum-analyzer': 1.0,
  benchpod: 1.5,
  datasheet: 3.0,
  assumed: 6.0,
}

export const KINDS = ['trapezoid', 'waveform', 'spectrum'] as const
export type Kind = (typeof KINDS)[number]

export const ROLES = ['signal', 'switching-regulator'] as const
export type Role = (typeof ROLES)[number]

/** Size caps (docs/emi-driver-format.md): a jsonb column, and a file parsed in a browser tab. */
export const MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
export const MAX_SAMPLES = 1_000_000

const TRAPEZOID_FIELDS = [
  'amplitude_v',
  'period_s',
  'pulse_width_s',
  'rise_s',
  'fall_s',
  'source_impedance_ohm',
] as const

export interface Sourced {
  value: number
  source: Source
  detail?: string
}

export interface Driver {
  name: string
  kind: Kind
  role: Role
  net?: string
  values: Record<string, Sourced>
  payload: Record<string, unknown>
}

const isPlainNumber = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v)

function sourced(raw: unknown, where: string): Sourced {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new DriverError(`${where} must be an object with a value and a source`)
  }
  const obj = raw as Record<string, unknown>
  if (!('value' in obj)) throw new DriverError(`${where} has no value`)
  if (!('source' in obj)) {
    throw new DriverError(
      `${where} does not say where it came from. Every number in a driver carries a ` +
        `source, one of: ${SOURCES.join(', ')}`,
    )
  }
  const source = obj.source
  if (typeof source !== 'string' || !(SOURCES as readonly string[]).includes(source)) {
    throw new DriverError(
      `${where}: unknown source ${JSON.stringify(source)}; use one of ${SOURCES.join(', ')}`,
    )
  }
  if (!isPlainNumber(obj.value)) {
    throw new DriverError(`${where}: value must be a number, got ${typeof obj.value}`)
  }
  if (obj.detail !== undefined && obj.detail !== null && typeof obj.detail !== 'string') {
    throw new DriverError(`${where}: detail must be text`)
  }
  return {
    value: obj.value,
    source: source as Source,
    detail: typeof obj.detail === 'string' ? obj.detail : undefined,
  }
}

/**
 * Validate an `emi-driver` document and return it in a usable shape.
 *
 * `documentBytes` is the encoded size when the caller has it (an upload), so the 2 MB cap
 * is enforced against what actually arrived rather than a re-serialisation of it.
 */
export function parseDriverDocument(doc: unknown, documentBytes?: number): Driver {
  if (typeof doc !== 'object' || doc === null || Array.isArray(doc)) {
    throw new DriverError('a driver document must be a JSON object')
  }
  const d = doc as Record<string, unknown>

  if (documentBytes !== undefined && documentBytes > MAX_DOCUMENT_BYTES) {
    throw new DriverError(
      `driver document is ${(documentBytes / 1e6).toFixed(1)} MB, over the ` +
        `${(MAX_DOCUMENT_BYTES / 1e6).toFixed(0)} MB limit`,
    )
  }
  if (d.format !== DRIVER_FORMAT) {
    throw new DriverError(`not a driver document: format=${JSON.stringify(d.format)}`)
  }
  const version = d.version
  if (typeof version !== 'number' || !Number.isInteger(version)) {
    throw new DriverError(`version must be a whole number, got ${JSON.stringify(version)}`)
  }
  if (version > DRIVER_VERSION) {
    throw new DriverError(
      `this driver is version ${version} and this build understands version ` +
        `${DRIVER_VERSION}. Update the analyzer rather than letting it read the parts it ` +
        `recognises`,
    )
  }
  if (version < 1) throw new DriverError(`version must be at least 1, got ${version}`)

  const name = d.name
  if (typeof name !== 'string' || name.trim() === '') {
    throw new DriverError('a driver needs a name')
  }

  const kind = d.kind
  if (typeof kind !== 'string' || !(KINDS as readonly string[]).includes(kind)) {
    throw new DriverError(
      `unknown driver kind ${JSON.stringify(kind)}; use one of ${KINDS.join(', ')}`,
    )
  }
  const role = (d.role ?? 'signal') as string
  if (!(ROLES as readonly string[]).includes(role)) {
    throw new DriverError(`unknown role ${JSON.stringify(role)}; use one of ${ROLES.join(', ')}`)
  }
  if (d.net !== undefined && d.net !== null && typeof d.net !== 'string') {
    throw new DriverError('net must be text')
  }

  const values: Record<string, Sourced> = {}
  let payload: Record<string, unknown> = {}

  if (kind === 'trapezoid') {
    const body = d.trapezoid
    if (typeof body !== 'object' || body === null) {
      throw new DriverError('a trapezoid driver needs a trapezoid block')
    }
    const b = body as Record<string, unknown>
    for (const field of TRAPEZOID_FIELDS) {
      if (!(field in b)) throw new DriverError(`trapezoid.${field} is missing`)
      values[field] = sourced(b[field], `trapezoid.${field}`)
    }
    const trap: Trapezoid = {
      amplitude_v: values.amplitude_v.value,
      period_s: values.period_s.value,
      pulse_width_s: values.pulse_width_s.value,
      rise_s: values.rise_s.value,
      fall_s: values.fall_s.value,
    }
    validateTrapezoid(trap)
    if (values.source_impedance_ohm.value < 0) {
      throw new DriverError('source impedance must not be negative')
    }
  } else if (kind === 'waveform') {
    const body = d.waveform
    if (typeof body !== 'object' || body === null) {
      throw new DriverError('a waveform driver needs a waveform block')
    }
    const b = body as Record<string, unknown>
    const samples = b.samples_v
    if (!Array.isArray(samples) || samples.length < 2) {
      throw new DriverError('a waveform needs at least two samples')
    }
    if (samples.length > MAX_SAMPLES) {
      throw new DriverError(
        `${samples.length.toLocaleString('en-US')} samples is over the ` +
          `${MAX_SAMPLES.toLocaleString('en-US')} limit; decimate the capture or shorten it`,
      )
    }
    samples.forEach((s, i) => {
      if (!isPlainNumber(s)) throw new DriverError(`waveform.samples_v[${i}] is not a number`)
    })
    const interval = b.sample_interval_s
    if (!isPlainNumber(interval) || interval <= 0) {
      throw new DriverError('waveform.sample_interval_s must be a positive number')
    }
    for (const field of ['period_s', 'source_impedance_ohm'] as const) {
      if (!(field in b)) throw new DriverError(`waveform.${field} is missing`)
      values[field] = sourced(b[field], `waveform.${field}`)
    }
    const captured = (samples.length - 1) * interval
    const period = values.period_s.value
    if (captured < period) {
      throw new DriverError(
        `the capture is ${(captured * 1e9).toPrecision(4)} ns long and one period is ` +
          `${(period * 1e9).toPrecision(4)} ns. Transforming less than a full period would ` +
          `put every harmonic in the wrong place`,
      )
    }
    const bandwidth = b.bandwidth_hz
    if (bandwidth !== undefined && bandwidth !== null) {
      if (!isPlainNumber(bandwidth) || bandwidth <= 0) {
        throw new DriverError('waveform.bandwidth_hz must be a positive number')
      }
    }
    if ('rise_s' in b) values.rise_s = sourced(b.rise_s, 'waveform.rise_s')
    payload = {
      samples_v: samples as number[],
      sample_interval_s: interval,
      bandwidth_hz: isPlainNumber(bandwidth) ? bandwidth : null,
    }
  } else {
    const body = d.spectrum
    if (typeof body !== 'object' || body === null) {
      throw new DriverError('a spectrum driver needs a spectrum block')
    }
    const b = body as Record<string, unknown>
    const points = b.points
    if (!Array.isArray(points) || points.length < 2) {
      throw new DriverError('a spectrum needs at least two points')
    }
    if (points.length > MAX_SAMPLES) {
      throw new DriverError(
        `${points.length.toLocaleString('en-US')} points is over the ` +
          `${MAX_SAMPLES.toLocaleString('en-US')} limit`,
      )
    }
    const parsed: [number, number][] = points.map((p, i) => {
      if (typeof p !== 'object' || p === null) {
        throw new DriverError(`spectrum.points[${i}] needs a frequency_hz and a level_dbuv`)
      }
      const pt = p as Record<string, unknown>
      if (!('frequency_hz' in pt) || !('level_dbuv' in pt)) {
        throw new DriverError(`spectrum.points[${i}] needs a frequency_hz and a level_dbuv`)
      }
      if (!isPlainNumber(pt.frequency_hz) || pt.frequency_hz <= 0) {
        throw new DriverError(`spectrum.points[${i}].frequency_hz must be positive`)
      }
      if (!isPlainNumber(pt.level_dbuv)) {
        throw new DriverError(`spectrum.points[${i}].level_dbuv must be a number`)
      }
      return [pt.frequency_hz, pt.level_dbuv]
    })
    for (let i = 1; i < parsed.length; i++) {
      if (parsed[i][0] <= parsed[i - 1][0]) {
        throw new DriverError('spectrum points must be in increasing frequency order')
      }
    }
    if (!('source_impedance_ohm' in b)) {
      throw new DriverError('spectrum.source_impedance_ohm is missing')
    }
    values.source_impedance_ohm = sourced(b.source_impedance_ohm, 'spectrum.source_impedance_ohm')
    const rbw = b.rbw_hz
    if (rbw !== undefined && rbw !== null && (!isPlainNumber(rbw) || rbw <= 0)) {
      throw new DriverError('spectrum.rbw_hz must be a positive number')
    }
    payload = { points: parsed, rbw_hz: isPlainNumber(rbw) ? rbw : null }
  }

  if (role === 'switching-regulator') {
    for (const field of ['input_current_a', 'input_voltage_v'] as const) {
      if (!(field in d)) {
        throw new DriverError(
          `a switching-regulator driver needs ${field}, which the conducted prediction ` +
            `reads`,
        )
      }
      values[field] = sourced(d[field], field)
    }
  }

  if (Object.keys(values).length === 0) {
    throw new DriverError('a driver document carries no numbers')
  }

  return {
    name: name.trim(),
    kind: kind as Kind,
    role: role as Role,
    net: typeof d.net === 'string' ? d.net : undefined,
    values,
    payload,
  }
}

/**
 * The source that sets this driver's confidence term (docs/implementation.md §7.4).
 *
 * Worst, not average: an averaged provenance would hide a guessed rise time behind four
 * measured values, which is exactly the case the confidence term exists to price.
 */
export function weakestSource(driver: Driver): Source {
  return Object.values(driver.values).reduce((worst, s) =>
    SOURCE_SIGMA_DB[s.source] > SOURCE_SIGMA_DB[worst.source] ? s : worst,
  ).source
}

export function sigmaDb(driver: Driver): number {
  return SOURCE_SIGMA_DB[weakestSource(driver)]
}

/**
 * A driver picker's label: its name, and "assumed" when any value in it is. Every place a
 * driver is chosen says so before it is chosen, not only once a result is drawn with it.
 */
export function driverOptionLabel(name: string, document: unknown): string {
  try {
    return weakestSource(parseDriverDocument(document)) === 'assumed' ? `${name} · assumed` : name
  } catch {
    return name
  }
}

/** The trapezoid parameters, for a driver that has them. */
export function driverTrapezoid(driver: Driver): Trapezoid {
  if (driver.kind !== 'trapezoid') {
    throw new DriverError(`a ${driver.kind} driver has no trapezoid parameters`)
  }
  return {
    amplitude_v: driver.values.amplitude_v.value,
    period_s: driver.values.period_s.value,
    pulse_width_s: driver.values.pulse_width_s.value,
    rise_s: driver.values.rise_s.value,
    fall_s: driver.values.fall_s.value,
  }
}
