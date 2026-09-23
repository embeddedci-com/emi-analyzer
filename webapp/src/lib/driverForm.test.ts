/**
 * The driver form's document builder.
 *
 * The form works in nanoseconds, which is what a datasheet quotes; the driver document is in
 * seconds. A missing or doubled 1e-9 would move every harmonic by nine decades while still
 * producing a document that validates and a spectrum that looks like a spectrum, so the
 * conversion is pinned against a hand-computed fundamental rather than against itself.
 */

import { describe, expect, it } from 'vitest'
import {
  buildTrapezoidDocument,
  DRIVER_FORM_DEFAULTS,
  type Field,
} from '../components/DriversPanel'
import { parseDriverDocument, driverTrapezoid, weakestSource, sigmaDb } from './driverDocument'
import { resolveDriver } from './driverResolve'
import { DriverError } from './driverSpectrum'

const fields = (over: Record<string, Partial<Field>> = {}) => {
  const out: Record<string, Field> = { ...DRIVER_FORM_DEFAULTS }
  for (const [k, v] of Object.entries(over)) out[k] = { ...out[k], ...v }
  return out
}

describe('buildTrapezoidDocument', () => {
  it('converts nanoseconds to seconds exactly once', () => {
    const doc = buildTrapezoidDocument(fields({ period_ns: { value: 40 } }), 'clk', null)
    const trap = driverTrapezoid(parseDriverDocument(doc))
    expect(trap.period_s).toBe(40e-9)
    // 40 ns is a 25 MHz clock. If the exponent were wrong this would be 25 mHz or 25 PHz,
    // both of which still "work".
    const r = resolveDriver(parseDriverDocument(doc), [25e6])
    expect(r.volts[0]).not.toBeNull()
  })

  it('leaves amplitude and impedance in their own units', () => {
    const doc = buildTrapezoidDocument(
      fields({ amplitude_v: { value: 1.8 }, source_impedance_ohm: { value: 33 } }),
      'clk', null,
    )
    const d = parseDriverDocument(doc)
    expect(d.values.amplitude_v.value).toBe(1.8)
    expect(d.values.source_impedance_ohm.value).toBe(33)
  })

  it('produces a document the shared validator accepts', () => {
    expect(() => parseDriverDocument(buildTrapezoidDocument(fields(), 'clk', null))).not.toThrow()
  })

  it('carries each field\'s own source through', () => {
    const doc = buildTrapezoidDocument(
      fields({
        amplitude_v: { source: 'scope' },
        period_ns: { source: 'benchpod' },
        pulse_width_ns: { source: 'benchpod' },
        rise_ns: { source: 'scope' },
        fall_ns: { source: 'scope' },
        source_impedance_ohm: { source: 'datasheet' },
      }),
      'clk', null,
    )
    const d = parseDriverDocument(doc)
    expect(d.values.period_s.source).toBe('benchpod')
    // datasheet is the weakest of those, so it sets the budget term.
    expect(weakestSource(d)).toBe('datasheet')
    expect(sigmaDb(d)).toBe(3)
  })

  it('omits the net when none is chosen, rather than sending null', () => {
    expect(buildTrapezoidDocument(fields(), 'clk', null)).not.toHaveProperty('net')
    expect(buildTrapezoidDocument(fields(), 'clk', '/SPI_SCK')).toHaveProperty('net', '/SPI_SCK')
  })

  it('names an untitled driver rather than failing validation on a blank', () => {
    const d = parseDriverDocument(buildTrapezoidDocument(fields(), '   ', null))
    expect(d.name).toBe('Untitled driver')
  })

  it('still builds an impossible waveform, so the form can explain why', () => {
    // The builder does not validate; the form parses and shows the message. Building a
    // document that cannot be a waveform is how the user gets told what is wrong.
    const doc = buildTrapezoidDocument(
      fields({ pulse_width_ns: { value: 1 }, rise_ns: { value: 4 }, fall_ns: { value: 4 } }),
      'clk', null,
    )
    expect(() => parseDriverDocument(doc)).toThrow(DriverError)
    expect(() => parseDriverDocument(doc)).toThrow(/do not fit inside the pulse/)
  })

  it('has defaults that describe a real, valid clock', () => {
    const d = parseDriverDocument(buildTrapezoidDocument(DRIVER_FORM_DEFAULTS, 'clk', null))
    const trap = driverTrapezoid(d)
    expect(1 / trap.period_s).toBeCloseTo(25e6, 0)
  })
})
