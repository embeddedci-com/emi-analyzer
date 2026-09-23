/**
 * The browser's half of the component contract.
 *
 * Same fixtures as `worker/tests/test_component_document.py`. The browser previews a
 * component's impedance while it is typed and the worker places that same component in a
 * solve; a disagreement is a user shown a self-resonance the solve does not model.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/component_fixtures.json'
import {
  ComponentError,
  cites,
  describeProvenance,
  esrFor,
  impedanceAt,
  isComplete,
  isGeneric,
  mlccFamily,
  parseComponent,
  selfResonanceHz,
  seriesRLC,
} from './componentDocument'

interface ValidCase {
  name: string
  document: unknown
  expected: {
    id: string
    provenance: string
    generic: boolean
    model_type: string
    describe_provenance: string
    self_resonance_hz?: number | null
    complete?: boolean
    impedance?: { frequency_hz: number; real: number; imag: number }[]
    family?: {
      esl_by_package: Record<string, number>
      esr_at: { c_f: number; esr_ohm: number }[]
    }
  }
}
interface InvalidCase { name: string; document: unknown; must_mention: string }

const data = fixtures as unknown as { valid: ValidCase[]; invalid: InvalidCase[] }
const byName = Object.fromEntries(data.valid.map((c) => [c.name, c]))

describe('shared fixtures', () => {
  it.each(data.valid)('reproduces $name', (c) => {
    const got = parseComponent(c.document)
    expect(got.id).toBe(c.expected.id)
    expect(got.provenance).toBe(c.expected.provenance)
    expect(isGeneric(got)).toBe(c.expected.generic)
    expect(got.modelType).toBe(c.expected.model_type)
    expect(describeProvenance(got)).toBe(c.expected.describe_provenance)

    if (c.expected.model_type === 'series_rlc') {
      const rlc = seriesRLC(got)
      expect(isComplete(rlc)).toBe(c.expected.complete)
      const srf = selfResonanceHz(rlc)
      if (c.expected.self_resonance_hz === null) {
        expect(srf).toBeNull()
      } else {
        expect(srf! / c.expected.self_resonance_hz! - 1).toBeCloseTo(0, 12)
      }
      for (const point of c.expected.impedance ?? []) {
        const z = impedanceAt(rlc, point.frequency_hz)
        expect(z.re).toBeCloseTo(point.real, 12)
        // Reactance spans decades, so compare relative to its own magnitude.
        expect(Math.abs(z.im - point.imag) / Math.max(Math.abs(point.imag), 1e-12))
          .toBeLessThan(1e-12)
      }
    } else {
      const fam = mlccFamily(got)
      expect(fam.eslByPackage).toEqual(c.expected.family!.esl_by_package)
      for (const point of c.expected.family!.esr_at) {
        expect(esrFor(fam, point.c_f)! / point.esr_ohm - 1).toBeCloseTo(0, 12)
      }
    }
  })

  it.each(data.invalid)('refuses $name', (c) => {
    let message = ''
    expect(() => {
      try {
        parseComponent(c.document)
      } catch (e) {
        message = (e as Error).message
        throw e
      }
    }).toThrow(ComponentError)
    expect(message).toContain(c.must_mention)
  })
})

describe('the citation rule', () => {
  it('refuses a number with nothing said about where it came from', () => {
    expect(() => parseComponent(byName['vendor-series-rlc'].document))
      .not.toThrow()
    const uncited = JSON.parse(JSON.stringify(byName['vendor-series-rlc'].document))
    uncited.sources = []
    expect(() => parseComponent(uncited)).toThrow(/says nothing about where it came from/)
  })

  it('lets a null value pass without one', () => {
    const c = parseComponent(byName['generic-no-numbers'].document)
    expect(isComplete(seriesRLC(c))).toBe(false)
    expect(c.sources).toHaveLength(0)
  })

  it('finds the source that covers a value', () => {
    const c = parseComponent(byName['vendor-series-rlc'].document)
    expect(cites(c, 'ESL')).not.toBeNull()
    expect(cites(c, 'ESR')!.doc).toBe('part datasheet')
  })
})

describe('the circuit', () => {
  const rlc = { c_f: 1e-7, esl_h: 6e-10, esr_ohm: 0.02, esl_includes_mount: false }

  it('is capacitive below resonance and inductive above', () => {
    const srf = selfResonanceHz(rlc)!
    expect(impedanceAt(rlc, srf / 10).im).toBeLessThan(0)
    expect(impedanceAt(rlc, srf * 10).im).toBeGreaterThan(0)
    expect(impedanceAt(rlc, srf).im).toBeCloseTo(0, 6)
    expect(impedanceAt(rlc, srf).re).toBeCloseTo(0.02, 12)
  })

  it('has no self-resonance without an ESL', () => {
    expect(selfResonanceHz({ ...rlc, esl_h: null })).toBeNull()
  })

  it('refuses an impedance it cannot compute rather than guessing', () => {
    expect(() => impedanceAt({ ...rlc, esl_h: null }, 1e8))
      .toThrow(/needs both an ESL and an ESR/)
  })
})

describe('the family', () => {
  const fam = () => mlccFamily(parseComponent(byName['generic-family'].document))

  it('clamps ESR at both ends rather than extrapolating', () => {
    const f = fam()
    // Beyond the table the trend is not something this family knows, and a straight line
    // off the end would invent a number with no basis.
    expect(esrFor(f, 1e-15)).toBe(f.esrTable[0][1])
    expect(esrFor(f, 1e-2)).toBe(f.esrTable[f.esrTable.length - 1][1])
  })

  it('knows an ESL per package and nothing about packages it has not got', () => {
    expect(fam().eslByPackage['0402']).toBeCloseTo(4.5e-10, 15)
    expect(fam().eslByPackage['1812']).toBeUndefined()
  })
})

describe('axis formatting', () => {
  it('never renders exponential notation', async () => {
    // toPrecision(2) returns "1.0e+2" for 100, so a 0.1 Ω gridline rendered as "1.0e+2 mΩ"
    // and clipped on the axis to "0e+2 mΩ" — a label that means nothing. Caught by looking
    // at the chart, not by a test, so here is the test.
    const { fmtOhm } = await import('../components/ComponentImpedancePreview')
    for (const z of [1e-6, 1e-5, 1e-4, 1e-3, 0.01, 0.02, 0.1, 1, 10, 100]) {
      expect(fmtOhm(z)).not.toMatch(/e[+-]/)
    }
    expect(fmtOhm(0.1)).toBe('100 mΩ')
    expect(fmtOhm(0.02)).toBe('20 mΩ')
    expect(fmtOhm(1)).toBe('1.0 Ω')
    expect(fmtOhm(10)).toBe('10 Ω')
  })
})

describe('the typo the preview exists to catch', () => {
  it('moves the self-resonance visibly for a factor of ten in ESL', () => {
    const base = { c_f: 100e-9, esr_ohm: 0.02, esl_includes_mount: false }
    const right = selfResonanceHz({ ...base, esl_h: 0.4e-9 })!
    const typo = selfResonanceHz({ ...base, esl_h: 4e-9 })!
    expect(right / 1e6).toBeCloseTo(25.2, 1)
    expect(typo / 1e6).toBeCloseTo(7.96, 1)
    // Roughly the square root of ten, which is plainly different on a log axis.
    expect(right / typo).toBeCloseTo(Math.sqrt(10), 3)
  })
})
