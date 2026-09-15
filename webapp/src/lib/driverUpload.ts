/**
 * Turning an uploaded file into an `emi-driver` document (§9.1, §9.4).
 *
 * Browser-only, deliberately. BenchPod writes the document format directly, so the worker
 * never sees a CSV; this exists for the scope trace and the spreadsheet a user already has.
 * Whatever comes out of here goes through `parseDriverDocument`, the same validator the
 * worker runs, so a file that parses here still has to describe a possible waveform.
 *
 * The refusals carry most of the value. A capture that covers less than one period, or whose
 * samples are not evenly spaced, produces a transform that looks entirely plausible and is
 * wrong — so both are refused by name rather than coerced into something that runs.
 */

import { DriverError } from './driverSpectrum'
import { MAX_SAMPLES, type Source } from './driverDocument'

/** Sample spacing may vary by this much before the capture is called non-uniform. */
export const INTERVAL_TOLERANCE = 0.01

export interface UploadOptions {
  name: string
  /** Where the numbers came from; the upload itself cannot know. */
  source: Source
  sourceImpedanceOhm: number
  net?: string
  /** Waveform only: the capture bandwidth, above which the samples say nothing (§9.3). */
  bandwidthHz?: number
  /** Waveform only: a declared rise time lets the envelope continue above the bandwidth. */
  riseS?: number
  riseSource?: Source
}

interface Columns {
  header: string[]
  rows: number[][]
}

/** Parse CSV or TSV with a header row. Blank lines and `#` comments are skipped. */
export function parseDelimited(text: string): Columns {
  const lines = text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l !== '' && !l.startsWith('#'))
  if (lines.length < 2) {
    throw new DriverError('the file needs a header row and at least one row of numbers')
  }
  const delimiter = lines[0].includes('\t') ? '\t' : ','
  const header = lines[0].split(delimiter).map((h) => h.trim().toLowerCase())
  const rows: number[][] = []
  for (let i = 1; i < lines.length; i++) {
    const parts = lines[i].split(delimiter)
    if (parts.length < header.length) {
      throw new DriverError(
        `row ${i + 1} has ${parts.length} values where the header names ${header.length}`,
      )
    }
    const nums = parts.slice(0, header.length).map((p) => Number(p.trim()))
    const bad = nums.findIndex((n) => !Number.isFinite(n))
    if (bad >= 0) {
      throw new DriverError(
        `row ${i + 1}, column "${header[bad]}": ${JSON.stringify(parts[bad].trim())} is not a number`,
      )
    }
    rows.push(nums)
  }
  return { header, rows }
}

function columnIndex(header: string[], names: string[], what: string): number {
  for (const n of names) {
    const i = header.indexOf(n)
    if (i >= 0) return i
  }
  throw new DriverError(
    `no ${what} column: looked for ${names.map((n) => `"${n}"`).join(', ')}, found ` +
      `${header.map((h) => `"${h}"`).join(', ')}`,
  )
}

/**
 * The sample interval, having checked the samples are evenly spaced.
 *
 * The document format carries one interval, so uneven spacing cannot be represented. Taking
 * a mean and carrying on would misplace every harmonic while still producing a spectrum, so
 * this refuses and says where the file first disagrees with itself.
 */
export function uniformInterval(times: number[]): number {
  if (times.length < 2) throw new DriverError('a waveform needs at least two samples')
  const first = times[1] - times[0]
  if (!(first > 0)) {
    throw new DriverError('the time column must increase; the first two samples do not')
  }
  for (let i = 2; i < times.length; i++) {
    const step = times[i] - times[i - 1]
    if (!(step > 0)) {
      throw new DriverError(`the time column stops increasing at row ${i + 2}`)
    }
    if (Math.abs(step - first) > first * INTERVAL_TOLERANCE) {
      throw new DriverError(
        `the samples are not evenly spaced: row ${i + 2} steps by ${step.toPrecision(4)} s ` +
          `where the first step is ${first.toPrecision(4)} s. A driver document carries one ` +
          `sample interval, so resample the capture before uploading it`,
      )
    }
  }
  return first
}

/** Build a waveform driver document from a CSV/TSV capture of time and voltage. */
export function waveformFromDelimited(text: string, opts: UploadOptions): Record<string, unknown> {
  const { header, rows } = parseDelimited(text)
  const t = columnIndex(header, ['time', 't', 'time_s', 'seconds'], 'time')
  const v = columnIndex(header, ['voltage', 'v', 'volts', 'value'], 'voltage')
  if (rows.length > MAX_SAMPLES) {
    throw new DriverError(
      `${rows.length.toLocaleString('en-US')} samples is over the ` +
        `${MAX_SAMPLES.toLocaleString('en-US')} limit; decimate the capture or shorten it`,
    )
  }
  const times = rows.map((r) => r[t])
  const volts = rows.map((r) => r[v])
  const interval = uniformInterval(times)

  return {
    format: 'emi-driver',
    version: 1,
    name: opts.name.trim() || 'Uploaded waveform',
    ...(opts.net ? { net: opts.net } : {}),
    kind: 'waveform',
    role: 'signal',
    waveform: {
      sample_interval_s: interval,
      samples_v: volts,
      // The whole capture is one period unless the caller knows better. §9.2 refuses a
      // capture shorter than a period, which is the check that matters here.
      period_s: { value: (rows.length - 1) * interval, source: opts.source },
      source_impedance_ohm: { value: opts.sourceImpedanceOhm, source: opts.source },
      ...(opts.bandwidthHz ? { bandwidth_hz: opts.bandwidthHz } : {}),
      ...(opts.riseS
        ? { rise_s: { value: opts.riseS, source: opts.riseSource ?? opts.source } }
        : {}),
    },
  }
}

/** Build a spectrum driver document from a CSV/TSV of frequency and level. */
export function spectrumFromDelimited(text: string, opts: UploadOptions): Record<string, unknown> {
  const { header, rows } = parseDelimited(text)
  const f = columnIndex(header, ['frequency', 'frequency_hz', 'f', 'hz'], 'frequency')
  const l = columnIndex(header, ['level', 'level_dbuv', 'dbuv', 'amplitude'], 'level')
  if (rows.length > MAX_SAMPLES) {
    throw new DriverError(`${rows.length.toLocaleString('en-US')} points is over the limit`)
  }
  const points = rows.map((r) => ({ frequency_hz: r[f], level_dbuv: r[l] }))
  return {
    format: 'emi-driver',
    version: 1,
    name: opts.name.trim() || 'Uploaded spectrum',
    ...(opts.net ? { net: opts.net } : {}),
    kind: 'spectrum',
    role: 'signal',
    spectrum: {
      points,
      source_impedance_ohm: { value: opts.sourceImpedanceOhm, source: opts.source },
    },
  }
}

/**
 * Read whatever a user dropped on the form.
 *
 * A `.json` file is expected to already be an emi-driver document — that is what BenchPod
 * writes and what Export JSON produces — so it is passed straight through to the validator
 * rather than being reinterpreted.
 */
export function documentFromFile(
  filename: string,
  text: string,
  kind: 'waveform' | 'spectrum',
  opts: UploadOptions,
): Record<string, unknown> {
  const trimmed = text.trim()
  if (filename.toLowerCase().endsWith('.json') || trimmed.startsWith('{')) {
    let parsed: unknown
    try {
      parsed = JSON.parse(trimmed)
    } catch (e) {
      throw new DriverError(`${filename} is not valid JSON: ${(e as Error).message}`)
    }
    if (
      typeof parsed !== 'object' || parsed === null ||
      (parsed as { format?: unknown }).format !== 'emi-driver'
    ) {
      throw new DriverError(
        `${filename} is JSON but not an emi-driver document. Upload a CSV of the raw ` +
          `samples, or a document written by BenchPod or by Export JSON`,
      )
    }
    return parsed as Record<string, unknown>
  }
  return kind === 'waveform'
    ? waveformFromDelimited(text, opts)
    : spectrumFromDelimited(text, opts)
}
