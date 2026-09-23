/**
 * The browser's half of driver resolution.
 *
 * Same fixtures as `worker/tests/test_driver_resolve.py`. The preview a user sees while
 * typing has to be the spectrum the worker then uses.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/driver_document_fixtures.json'
import { parseDriverDocument } from './driverDocument'
import {
  describeCorners,
  drivenCount,
  isComplete,
  resolveDriver,
  voltsToDbuv,
} from './driverResolve'
import { DriverError, trapezoidSeries } from './driverSpectrum'
import { driverTrapezoid } from './driverDocument'

interface ValidCase {
  name: string
  document: unknown
  resolve?: {
    frequencies_hz: number[]
    volts_abs: (number | null)[]
    undriven_hz: number[]
    magnitude_only: boolean
  }
}

const cases = (fixtures as unknown as { valid: ValidCase[] }).valid
const byName = Object.fromEntries(cases.map((c) => [c.name, c]))
const docOf = (name: string) => parseDriverDocument(byName[name].document)
const abs = (c: { re: number; im: number }) => Math.hypot(c.re, c.im)

describe('shared fixtures', () => {
  const withResolve = cases.filter((c) => c.resolve)

  it('has resolution cases', () => {
    expect(withResolve.length).toBeGreaterThan(0)
  })

  it.each(withResolve)('resolves $name as the worker does', (c) => {
    const r = resolveDriver(parseDriverDocument(c.document), c.resolve!.frequencies_hz)
    expect(r.magnitudeOnly).toBe(c.resolve!.magnitude_only)
    expect([...r.undriven.keys()].sort((a, b) => a - b)).toEqual(c.resolve!.undriven_hz)
    const peak = Math.max(
      ...c.resolve!.volts_abs.filter((v): v is number => v !== null),
    )
    r.volts.forEach((v, i) => {
      const want = c.resolve!.volts_abs[i]
      if (want === null) {
        expect(v).toBeNull()
        return
      }
      expect(v).not.toBeNull()
      // Scaled by the peak: a spectral null is float noise against its own magnitude.
      expect(Math.abs(abs(v!) - want) / peak).toBeLessThan(1e-12)
    })
  })
})

describe('line spectra', () => {
  it('drives its own harmonics', () => {
    const d = docOf('trapezoid-spi-clock')
    const r = resolveDriver(d, [25e6, 75e6, 125e6])
    expect(isComplete(r)).toBe(true)
    const series = trapezoidSeries(driverTrapezoid(d), 5)
    expect(abs(r.volts[0]!) / series[0].amplitude_v).toBeCloseTo(1, 9)
    expect(abs(r.volts[1]!) / series[2].amplitude_v).toBeCloseTo(1, 9)
  })

  it('reports a frequency between harmonics as undriven, not small', () => {
    const r = resolveDriver(docOf('trapezoid-spi-clock'), [25e6, 40e6])
    expect(r.volts[1]).toBeNull()
    expect(isComplete(r)).toBe(false)
    expect(r.undriven.get(40e6)).toMatch(/not a harmonic/)
    expect(drivenCount(r)).toBe(1)
  })

  it('distinguishes a spectral null from an undriven frequency', () => {
    // 50 MHz IS a harmonic of a 25 MHz clock; it is driven, with zero amplitude.
    const r = resolveDriver(docOf('trapezoid-spi-clock'), [50e6])
    expect(r.volts[0]).not.toBeNull()
    expect(abs(r.volts[0]!)).toBeLessThan(1e-9)
    expect(isComplete(r)).toBe(true)
  })

  it('tolerates float arithmetic but not disagreement', () => {
    const d = docOf('trapezoid-spi-clock')
    expect(resolveDriver(d, [25e6 * (1 + 1e-9)]).volts[0]).not.toBeNull()
    expect(resolveDriver(d, [25e6 * 1.01]).volts[0]).toBeNull()
  })
})

describe('uploaded spectrum', () => {
  it('interpolates in log frequency', () => {
    const d = docOf('spectrum-analyser-trace')
    const mid = Math.sqrt(25e6 * 75e6)
    const r = resolveDriver(d, [25e6, mid, 75e6])
    expect(voltsToDbuv(abs(r.volts[0]!))).toBeCloseTo(96.4, 6)
    expect(voltsToDbuv(abs(r.volts[2]!))).toBeCloseTo(86.8, 6)
    expect(voltsToDbuv(abs(r.volts[1]!))).toBeCloseTo((96.4 + 86.8) / 2, 6)
  })

  it('is undriven outside its range', () => {
    const r = resolveDriver(docOf('spectrum-analyser-trace'), [10e6, 25e6, 500e6])
    expect(r.volts[0]).toBeNull()
    expect(r.volts[2]).toBeNull()
    expect(r.undriven.get(500e6)).toMatch(/says nothing at/)
  })
})

describe('a capture above its bandwidth', () => {
  it('joins the envelope from the top octave, not from a null at the join', () => {
    // 400 MHz is the 4th harmonic of a 100 MHz square wave, a null, and the last one below
    // the 410 MHz bandwidth. Scaled to it, everything above would resolve to nothing.
    const r = resolveDriver(docOf('waveform-join-on-a-null'), [5e8, 7e8])
    expect(abs(r.volts[0]!)).toBeGreaterThan(0.1)
    expect(abs(r.volts[1]!)).toBeGreaterThan(0.05)
  })

  it('gives the same level whatever else was asked for', () => {
    const d = docOf('waveform-join-on-a-null')
    const alone = resolveDriver(d, [7e8]).volts[0]!
    const withRest = resolveDriver(d, [1e8, 2e8, 3e8, 4e8, 7e8]).volts[4]!
    expect(abs(alone) / abs(withRest)).toBeCloseTo(1, 12)
  })
})

describe('the preview', () => {
  it('gets both corners for a trapezoid and none for a waveform', () => {
    const corners = describeCorners(docOf('trapezoid-spi-clock'))
    expect(corners).not.toBeNull()
    expect(corners!.f1).toBeCloseTo(1 / (Math.PI * 2e-8), 0)
    expect(describeCorners(docOf('waveform-captured-edge'))).toBeNull()
  })
})

describe('refusals', () => {
  it('refuses an empty frequency list', () => {
    expect(() => resolveDriver(docOf('trapezoid-spi-clock'), [])).toThrow(DriverError)
  })

  it('refuses a non-positive frequency', () => {
    expect(() => resolveDriver(docOf('trapezoid-spi-clock'), [0])).toThrow(/must be positive/)
  })

  it('has no dBuV for zero volts', () => {
    expect(() => voltsToDbuv(0)).toThrow(/no dBuV/)
  })
})
