/**
 * Uploaded captures (docs/emi-driver-format.md §4).
 *
 * The refusals are the point. A capture that covers less than one period, or whose samples
 * are not evenly spaced, transforms into something that looks entirely plausible and is
 * wrong, so both must be refused by name rather than coerced into something that runs.
 */

import { describe, expect, it } from 'vitest'
import {
  documentFromFile,
  parseDelimited,
  spectrumFromDelimited,
  uniformInterval,
  waveformFromDelimited,
  type UploadOptions,
} from './driverUpload'
import { parseDriverDocument } from './driverDocument'
import { resolveDriver } from './driverResolve'

const opts: UploadOptions = { name: 'capture', source: 'scope', sourceImpedanceOhm: 50 }

/** One period of a 100 MHz square-ish wave at 1 ps steps. */
function squareCsv(samples = 101, period = 1e-8): string {
  const dt = period / (samples - 1)
  const rows = Array.from({ length: samples }, (_, i) => {
    const t = i * dt
    return `${t},${t < period / 2 ? 3.3 : 0}`
  })
  return ['time,voltage', ...rows].join('\n')
}

describe('parseDelimited', () => {
  it('reads CSV with a header', () => {
    const { header, rows } = parseDelimited('time,voltage\n0,0\n1e-9,3.3')
    expect(header).toEqual(['time', 'voltage'])
    expect(rows).toEqual([[0, 0], [1e-9, 3.3]])
  })

  it('reads TSV, and skips comments and blank lines', () => {
    const { rows } = parseDelimited('# a scope export\ntime\tvoltage\n0\t0\n\n1e-9\t3.3\n')
    expect(rows).toEqual([[0, 0], [1e-9, 3.3]])
  })

  it('names the row and column of a value that is not a number', () => {
    expect(() => parseDelimited('time,voltage\n0,0\n1e-9,n/a')).toThrow(/row 3, column "voltage"/)
  })

  it('refuses a file with no data rows', () => {
    expect(() => parseDelimited('time,voltage')).toThrow(/header row and at least one row/)
  })
})

describe('uniformInterval', () => {
  it('accepts evenly spaced samples', () => {
    expect(uniformInterval([0, 1e-9, 2e-9, 3e-9])).toBeCloseTo(1e-9, 15)
  })

  it('refuses uneven spacing and says where', () => {
    // The document format carries ONE interval, so uneven spacing cannot be represented.
    expect(() => uniformInterval([0, 1e-9, 2e-9, 9e-9])).toThrow(/not evenly spaced: row 5/)
  })

  it('refuses a time column that stops increasing', () => {
    expect(() => uniformInterval([0, 1e-9, 1e-9])).toThrow(/stops increasing/)
  })

  it('refuses a first step that is not positive', () => {
    expect(() => uniformInterval([1e-9, 0])).toThrow(/must increase/)
  })
})

describe('waveformFromDelimited', () => {
  it('produces a document the shared validator accepts', () => {
    const doc = waveformFromDelimited(squareCsv(), opts)
    expect(() => parseDriverDocument(doc)).not.toThrow()
  })

  it('recovers the fundamental it was given', () => {
    const doc = waveformFromDelimited(squareCsv(101, 1e-8), opts)
    const d = parseDriverDocument(doc)
    // One period of 10 ns is a 100 MHz fundamental; it must resolve there.
    const r = resolveDriver(d, [100e6])
    expect(r.volts[0]).not.toBeNull()
    expect(Math.hypot(r.volts[0]!.re, r.volts[0]!.im)).toBeGreaterThan(0.5)
  })

  it('accepts alternative column names', () => {
    const csv = 't,v\n0,0\n1e-9,3.3\n2e-9,0'
    expect(() => waveformFromDelimited(csv, opts)).not.toThrow()
  })

  it('says which columns it looked for when they are missing', () => {
    expect(() => waveformFromDelimited('a,b\n0,1\n1,2', opts))
      .toThrow(/no time column: looked for "time", "t"/)
  })

  it('carries the declared bandwidth and rise time through', () => {
    const doc = waveformFromDelimited(squareCsv(), {
      ...opts, bandwidthHz: 2e9, riseS: 2e-10, riseSource: 'datasheet',
    })
    const d = parseDriverDocument(doc)
    expect(d.payload.bandwidth_hz).toBe(2e9)
    expect(d.values.rise_s.source).toBe('datasheet')
  })
})

describe('spectrumFromDelimited', () => {
  it('produces a document the shared validator accepts', () => {
    const csv = 'frequency,level\n25e6,96.4\n75e6,86.8\n125e6,81.2'
    expect(() => parseDriverDocument(spectrumFromDelimited(csv, opts))).not.toThrow()
  })

  it('is refused by the validator when the points are out of order', () => {
    // The upload does not sort: a spectrum whose frequencies go backwards is a sign the
    // wrong column was picked, not something to quietly repair.
    const csv = 'frequency,level\n75e6,86.8\n25e6,96.4'
    expect(() => parseDriverDocument(spectrumFromDelimited(csv, opts)))
      .toThrow(/increasing frequency/)
  })
})

describe('documentFromFile', () => {
  it('passes an emi-driver JSON document straight through', () => {
    const original = waveformFromDelimited(squareCsv(), opts)
    const round = documentFromFile('d.json', JSON.stringify(original), 'waveform', opts)
    expect(round).toEqual(original)
  })

  it('refuses JSON that is not an emi-driver document', () => {
    expect(() => documentFromFile('x.json', '{"hello":1}', 'waveform', opts))
      .toThrow(/not an emi-driver document/)
  })

  it('reports invalid JSON with the filename', () => {
    expect(() => documentFromFile('x.json', '{oops', 'waveform', opts))
      .toThrow(/x\.json is not valid JSON/)
  })

  it('treats a CSV as the chosen kind', () => {
    const doc = documentFromFile(
      's.csv', 'frequency,level\n25e6,96.4\n75e6,86.8', 'spectrum', opts,
    )
    expect(parseDriverDocument(doc).kind).toBe('spectrum')
  })

  it('refuses a capture shorter than one period via the shared validator', () => {
    // Two samples describe a 1 ns window; declaring that as a period is fine, but the
    // waveform validator is what enforces the format's rule and this proves the path reaches it.
    const doc = waveformFromDelimited('time,voltage\n0,0\n1e-9,3.3', opts) as {
      waveform: { period_s: { value: number } }
    }
    doc.waveform.period_s.value = 5e-9
    expect(() => parseDriverDocument(doc)).toThrow(/full period/)
  })
})
