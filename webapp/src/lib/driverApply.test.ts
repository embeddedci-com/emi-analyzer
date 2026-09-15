/**
 * The browser's half of §10. The near-field shader applies these offsets, so this is the
 * implementation a user actually sees the result of.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/driver_document_fixtures.json'
import { parseDriverDocument } from './driverDocument'
import {
  applyDriver,
  appliedIsComplete,
  MICRO,
  NULL_FLOOR,
  portAt,
  portSpectrumFromJson,
  referenceOffsetDb,
  type PortSpectrumJson,
} from './driverApply'
import { DriverError } from './driverSpectrum'

interface ApplyCase {
  name: string
  document: unknown
  apply?: { frequencies_hz: number[]; offset_db: (number | null)[]; complete: boolean }
}

const data = fixtures as unknown as {
  port_spectrum: PortSpectrumJson
  reference_magnitude: number
  valid: ApplyCase[]
}
const PORT = portSpectrumFromJson(data.port_spectrum)
const REFERENCE = data.reference_magnitude
const withApply = data.valid.filter((c) => c.apply)
const byName = Object.fromEntries(data.valid.map((c) => [c.name, c]))

describe('shared fixtures', () => {
  it.each(withApply)('reproduces $name', (c) => {
    const applied = applyDriver(
      parseDriverDocument(c.document), PORT, REFERENCE, c.apply!.frequencies_hz,
    )
    expect(appliedIsComplete(applied)).toBe(c.apply!.complete)
    applied.offsetDb.forEach((got, i) => {
      const want = c.apply!.offset_db[i]
      if (want === null) {
        expect(got).toBeNull()
      } else {
        expect(got).not.toBeNull()
        expect(Math.abs(got! - want)).toBeLessThan(1e-9)
      }
    })
  })
})

describe('the reference offset', () => {
  it('is the micro prefix and nothing else', () => {
    expect(referenceOffsetDb(0.5)).toBeCloseTo(20 * Math.log10(0.5 / MICRO), 9)
    expect(referenceOffsetDb(MICRO)).toBeCloseTo(0, 9)
    expect(referenceOffsetDb(1)).toBeCloseTo(120, 9)
  })

  it('refuses a solve that produced no field', () => {
    expect(() => referenceOffsetDb(0)).toThrow(/nothing to put a unit on/)
  })
})

describe('the two ways a frequency ends up with no offset', () => {
  it('tells a null apart from an undriven frequency', () => {
    const d = parseDriverDocument(byName['trapezoid-spi-clock'].document)
    const applied = applyDriver(d, PORT, REFERENCE, [40e6, 50e6])
    expect(applied.offsetDb).toEqual([null, null])
    expect(applied.undriven.get(40e6)).toMatch(/not a harmonic/)
    expect(applied.undriven.get(50e6)).toMatch(/null of this driver's spectrum/)
  })

  it('calls a null a null even when it is the only frequency asked about', () => {
    const d = parseDriverDocument(byName['trapezoid-spi-clock'].document)
    const applied = applyDriver(d, PORT, REFERENCE, [50e6])
    expect(applied.offsetDb[0]).toBeNull()
    expect(NULL_FLOOR).toBeLessThan(1e-6)
  })
})

describe('the port record', () => {
  it('refuses a driven frequency the solve never recorded', () => {
    const d = parseDriverDocument(byName['trapezoid-spi-clock'].document)
    // 175 MHz is a real harmonic of this 25 MHz clock; the solve simply has no spectrum
    // there. That is the solve's gap, not the driver's.
    expect(() => applyDriver(d, PORT, REFERENCE, [175e6])).toThrow(/no port spectrum at/)
  })

  it('rejects ragged arrays', () => {
    expect(() =>
      portSpectrumFromJson({
        frequencies_hz: [1e6, 2e6],
        v_real: [1],
        v_imag: [0],
        i_real: [0.1],
        i_imag: [0],
      }),
    ).toThrow(DriverError)
  })

  it('matches a frequency within tolerance', () => {
    expect(() => portAt(PORT, 25e6 * (1 + 1e-9))).not.toThrow()
    expect(() => portAt(PORT, 25e6 * 1.01)).toThrow()
  })
})

describe('the whole chain, as the results view walks it', () => {
  it('turns a stored map peak into an absolute level', () => {
    // What the run recorded: a peak |H| of 0.5 A/m anywhere in the solve, and this layer
    // sitting 12 dB below it at the frequency being viewed.
    const referenceMagnitude = 0.5
    const peakDbRelative = -12

    const driver = parseDriverDocument(byName['trapezoid-spi-clock'].document)
    const applied = applyDriver(driver, PORT, referenceMagnitude, [25e6])
    const offset = applied.offsetDb[0]
    expect(offset).not.toBeNull()

    // By hand: 0.5 A/m is 113.98 dBµA/m; the driver pushes 2.0977/(40+100) A against the
    // 0.02 A the solve used, which is -2.51 dB; the layer is 12 dB down from the peak.
    const reference = 20 * Math.log10(referenceMagnitude / MICRO)
    const current = 20 * Math.log10(2.097736453123487 / 140 / 0.02)
    expect(peakDbRelative + offset!).toBeCloseTo(peakDbRelative + reference + current, 6)
    expect(peakDbRelative + offset!).toBeCloseTo(99.47, 1)
  })

  it('gives all three refusals a distinguishable message', () => {
    const driver = parseDriverDocument(byName['trapezoid-spi-clock'].document)
    const applied = applyDriver(driver, PORT, 0.5, [40e6, 50e6])
    // Not a harmonic of this clock.
    expect(applied.undriven.get(40e6)).toMatch(/not a harmonic/)
    // A harmonic, but a null of its own spectrum.
    expect(applied.undriven.get(50e6)).toMatch(/null of this driver/)
    // And the third: a result that predates ports.json cannot be re-weighted at all. That
    // one is decided before any of this runs -- see canAttachDriver -- which is why the two
    // here must not be phrased as if they were the same thing.
    expect(applied.undriven.get(40e6)).not.toEqual(applied.undriven.get(50e6))
  })
})
