/**
 * The browser's half of §9.2.
 *
 * Same fixtures as `worker/tests/test_driver_document.py`. A document one validator accepts
 * and the other refuses is a user allowed to save something the worker then rejects.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/driver_document_fixtures.json'
import { DriverError, trapezoidSeries } from './driverSpectrum'
import {
  driverTrapezoid,
  MAX_DOCUMENT_BYTES,
  MAX_SAMPLES,
  parseDriverDocument,
  sigmaDb,
  SOURCE_SIGMA_DB,
  SOURCES,
  weakestSource,
} from './driverDocument'

interface ValidCase {
  name: string
  document: unknown
  expected: {
    kind: string
    role: string
    driver_name: string
    weakest_source: string
    sigma_db: number
  }
}
interface InvalidCase {
  name: string
  document: unknown
  must_mention: string
}

const data = fixtures as unknown as { valid: ValidCase[]; invalid: InvalidCase[] }
const byName = Object.fromEntries(data.valid.map((c) => [c.name, c]))

describe('valid documents', () => {
  it.each(data.valid)('parses $name', (c) => {
    const d = parseDriverDocument(c.document)
    expect(d.kind).toBe(c.expected.kind)
    expect(d.role).toBe(c.expected.role)
    expect(d.name).toBe(c.expected.driver_name)
    expect(weakestSource(d)).toBe(c.expected.weakest_source)
    expect(sigmaDb(d)).toBeCloseTo(c.expected.sigma_db, 9)
  })
})

describe('invalid documents', () => {
  it.each(data.invalid)('refuses $name with a reason', (c) => {
    let message = ''
    expect(() => {
      try {
        parseDriverDocument(c.document)
      } catch (e) {
        message = (e as Error).message
        throw e
      }
    }).toThrow(DriverError)
    expect(message).toContain(c.must_mention)
  })
})

describe('provenance', () => {
  it('takes the weakest source, not the average', () => {
    const d = parseDriverDocument(byName['trapezoid-spi-clock'].document)
    expect(weakestSource(d)).toBe('assumed')
    expect(sigmaDb(d)).toBe(SOURCE_SIGMA_DB.assumed)
  })

  it('makes an all-measured driver the most confident', () => {
    const measured = parseDriverDocument(byName['all-measured'].document)
    const mixed = parseDriverDocument(byName['trapezoid-spi-clock'].document)
    expect(sigmaDb(measured)).toBeLessThan(sigmaDb(mixed))
  })

  it('has a sigma for every source, in order', () => {
    expect(new Set(Object.keys(SOURCE_SIGMA_DB))).toEqual(new Set(SOURCES))
    expect(SOURCE_SIGMA_DB.scope).toBeLessThanOrEqual(SOURCE_SIGMA_DB.benchpod)
    expect(SOURCE_SIGMA_DB.benchpod).toBeLessThanOrEqual(SOURCE_SIGMA_DB.datasheet)
    expect(SOURCE_SIGMA_DB.datasheet).toBeLessThan(SOURCE_SIGMA_DB.assumed)
  })
})

describe('caps', () => {
  it('refuses an oversized document by its real size', () => {
    const doc = byName['trapezoid-spi-clock'].document
    expect(() => parseDriverDocument(doc, MAX_DOCUMENT_BYTES + 1)).toThrow(/over the/)
    expect(() => parseDriverDocument(doc, MAX_DOCUMENT_BYTES)).not.toThrow()
  })

  it('refuses too many samples', () => {
    const doc = {
      format: 'emi-driver',
      version: 1,
      name: 'huge',
      kind: 'waveform',
      role: 'signal',
      waveform: {
        sample_interval_s: 1e-12,
        samples_v: new Array(MAX_SAMPLES + 1).fill(0),
        period_s: { value: 1e-9, source: 'benchpod' },
        source_impedance_ohm: { value: 50, source: 'assumed' },
      },
    }
    expect(() => parseDriverDocument(doc)).toThrow(/over the/)
  })
})

describe('handoff to the spectrum module', () => {
  it('feeds a parsed trapezoid straight in', () => {
    const d = parseDriverDocument(byName['trapezoid-spi-clock'].document)
    const series = trapezoidSeries(driverTrapezoid(d), 5)
    expect(series[0].frequency_hz).toBeCloseTo(25e6, 0)
    expect(d.values.source_impedance_ohm.value).toBe(40)
  })

  it('refuses to give trapezoid parameters for a waveform', () => {
    const d = parseDriverDocument(byName['waveform-captured-edge'].document)
    expect(() => driverTrapezoid(d)).toThrow(/no trapezoid parameters/)
  })
})
