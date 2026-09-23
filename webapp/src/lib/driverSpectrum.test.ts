/**
 * The browser's half of the shared driver-spectrum contract.
 *
 * Reads the same fixtures as `worker/tests/test_driver_spectrum.py`. If the two disagree,
 * the spectrum a user previews while typing is not the spectrum the worker uses to put an
 * absolute level on their result.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/driver_fixtures.json'
import {
  cornerFrequencies,
  DriverError,
  envelopeV,
  reweight,
  reweightDb,
  trapezoidSeries,
  validateTrapezoid,
  type Trapezoid,
} from './driverSpectrum'

interface FixtureCase {
  name: string
  input: Trapezoid
  harmonics: number
  expected: {
    corner_1_hz: number
    corner_2_hz: number
    series: { frequency_hz: number; amplitude_v: number }[]
    envelope: { label: string; frequency_hz: number; amplitude_v: number }[]
  }
}

const cases = (fixtures as { cases: FixtureCase[] }).cases

const sinc = (x: number) => (x === 0 ? 1 : Math.sin(x) / x)

function closedForm(t: Trapezoid, n: number): number {
  const tauOverT = t.pulse_width_s / t.period_s
  const trOverT = t.rise_s / t.period_s
  return (
    // RMS, as every amplitude in the tool is: the textbook's peak over sqrt(2).
    Math.SQRT2 * t.amplitude_v * tauOverT *
    Math.abs(sinc(n * Math.PI * tauOverT)) *
    Math.abs(sinc(n * Math.PI * trOverT))
  )
}

describe('shared fixtures', () => {
  it('has cases to check', () => {
    expect(cases.length).toBeGreaterThan(0)
  })

  for (const c of cases) {
    it(`reproduces ${c.name}`, () => {
      const got = trapezoidSeries(c.input, c.harmonics)
      expect(got).toHaveLength(c.expected.series.length)
      // Scale by the peak of the series, not by each harmonic. A spectral null sits ~17
      // decades below the fundamental, so its own relative error is floating-point noise
      // and says nothing; what matters is that no harmonic differs by anything visible
      // next to the largest one.
      const peak = Math.max(...c.expected.series.map((p) => p.amplitude_v))
      got.forEach((point, i) => {
        const want = c.expected.series[i]
        expect(point.frequency_hz).toBeCloseTo(want.frequency_hz, 6)
        expect(Math.abs(point.amplitude_v - want.amplitude_v) / peak).toBeLessThan(1e-12)
      })

      const { f1, f2 } = cornerFrequencies(c.input)
      expect(f1 / c.expected.corner_1_hz - 1).toBeCloseTo(0, 12)
      expect(f2 / c.expected.corner_2_hz - 1).toBeCloseTo(0, 12)

      for (const point of c.expected.envelope) {
        const v = envelopeV(c.input, point.frequency_hz)
        expect(v / point.amplitude_v - 1).toBeCloseTo(0, 12)
      }
    })
  }
})

describe('D1 — the closed form', () => {
  it.each([0.5, 0.25, 0.1, 0.4])('matches within 0.1 dB at duty %s', (duty) => {
    const period = 4e-8
    const t: Trapezoid = {
      amplitude_v: 3.3,
      period_s: period,
      pulse_width_s: duty * period,
      rise_s: 1.2e-9,
      fall_s: 1.2e-9,
    }
    trapezoidSeries(t, 60).forEach((point, i) => {
      const want = closedForm(t, i + 1)
      if (want < 1e-9) {
        expect(point.amplitude_v).toBeLessThan(1e-9)
        return
      }
      expect(Math.abs(20 * Math.log10(point.amplitude_v / want))).toBeLessThan(0.1)
    })
  })

  it('gives a unipolar square wave 2A/(n*pi), not 4A/(n*pi)', () => {
    const t: Trapezoid = {
      amplitude_v: 1,
      period_s: 1e-6,
      pulse_width_s: 5e-7,
      rise_s: 1e-12,
      fall_s: 1e-12,
    }
    trapezoidSeries(t, 7).forEach((point, i) => {
      const n = i + 1
      if (n % 2 === 1) {
        expect(point.amplitude_v).toBeCloseTo(2 / (n * Math.PI) / Math.SQRT2, 5)
      } else {
        expect(point.amplitude_v).toBeLessThan(1e-6)
      }
    })
  })
})

describe('asymmetric edges', () => {
  const common = { amplitude_v: 3.3, period_s: 4e-8, pulse_width_s: 1.5e-8 }

  it('is not the same as averaging the two edges', () => {
    const skewed = trapezoidSeries({ ...common, rise_s: 1e-9, fall_s: 5e-9 }, 40)
    const averaged = trapezoidSeries({ ...common, rise_s: 3e-9, fall_s: 3e-9 }, 40)
    const diffs = skewed
      .map((p, i) => ({ a: p.amplitude_v, b: averaged[i].amplitude_v }))
      .filter(({ a, b }) => a > 1e-9 && b > 1e-9)
      .map(({ a, b }) => Math.abs(20 * Math.log10(a / b)))
    expect(Math.max(...diffs)).toBeGreaterThan(3)
  })

  it('is unchanged by swapping rise and fall, which is a time reversal', () => {
    const a = trapezoidSeries({ ...common, rise_s: 1e-9, fall_s: 4e-9 }, 30)
    const b = trapezoidSeries({ ...common, rise_s: 4e-9, fall_s: 1e-9 }, 30)
    a.forEach((p, i) => {
      expect(p.amplitude_v).toBeCloseTo(b[i].amplitude_v, 12)
    })
  })
})

describe('the envelope', () => {
  const t: Trapezoid = {
    amplitude_v: 3.3,
    period_s: 4e-8,
    pulse_width_s: 1e-8,
    rise_s: 1e-10,
    fall_s: 1e-10,
  }

  it('bounds the line spectrum', () => {
    for (const point of trapezoidSeries(t, 80)) {
      expect(point.amplitude_v).toBeLessThanOrEqual(envelopeV(t, point.frequency_hz) * 1.001)
    }
  })

  it('has the stated slopes', () => {
    const { f1, f2 } = cornerFrequencies(t)
    const flat = (2 * 3.3 * 1e-8) / 4e-8 / Math.SQRT2
    expect(envelopeV(t, f1 / 10)).toBeCloseTo(flat, 12)
    expect(20 * Math.log10(envelopeV(t, f1 * 10) / flat)).toBeCloseTo(-20, 9)
    expect(20 * Math.log10(envelopeV(t, f2 * 10) / envelopeV(t, f2))).toBeCloseTo(-40, 9)
  })

  it('takes its second corner from the faster edge', () => {
    const { f2 } = cornerFrequencies({ ...t, rise_s: 1e-9, fall_s: 5e-9 })
    expect(f2).toBeCloseTo(1 / (Math.PI * 1e-9), 3)
  })
})

describe('refusals', () => {
  it('rejects edges that do not fit the pulse', () => {
    expect(() =>
      validateTrapezoid({
        amplitude_v: 3.3,
        period_s: 4e-8,
        pulse_width_s: 1e-9,
        rise_s: 4e-9,
        fall_s: 4e-9,
      }),
    ).toThrow(DriverError)
  })

  it('rejects a pulse longer than its period', () => {
    expect(() =>
      validateTrapezoid({
        amplitude_v: 3.3,
        period_s: 4e-9,
        pulse_width_s: 3.5e-9,
        rise_s: 2e-9,
        fall_s: 2e-9,
      }),
    ).toThrow(/longer than the period/)
  })

  it('refuses a port with no current rather than dividing by it', () => {
    expect(() => reweight({ re: 1, im: 0 }, 50, { re: 1, im: 0 }, { re: 0, im: 0 })).toThrow(
      /nothing to re-weight/,
    )
  })
})

describe('re-weighting', () => {
  it('is the ratio of currents', () => {
    const vPort = { re: 2, im: 0 }
    const iPort = { re: 0.02, im: 0 }
    const factor = reweight({ re: 1, im: 0 }, 50, vPort, iPort)
    expect(factor.re).toBeCloseTo(1 / 150 / 0.02, 12)
    expect(reweightDb({ re: 1, im: 0 }, 50, vPort, iPort)).toBeCloseTo(
      20 * Math.log10(1 / 150 / 0.02),
      12,
    )
  })

  it('leaves a solve re-weighted by its own source unchanged', () => {
    const vPort = { re: 1.5, im: -0.4 }
    const iPort = { re: 0.03, im: 0.01 }
    // v = i * (Zs + Zin), with Zin = v/i
    const zIn = {
      re: (vPort.re * iPort.re + vPort.im * iPort.im) / (iPort.re ** 2 + iPort.im ** 2),
      im: (vPort.im * iPort.re - vPort.re * iPort.im) / (iPort.re ** 2 + iPort.im ** 2),
    }
    const sum = { re: 50 + zIn.re, im: zIn.im }
    const vEquivalent = {
      re: iPort.re * sum.re - iPort.im * sum.im,
      im: iPort.re * sum.im + iPort.im * sum.re,
    }
    const factor = reweight(vEquivalent, 50, vPort, iPort)
    expect(factor.re).toBeCloseTo(1, 12)
    expect(factor.im).toBeCloseTo(0, 12)
  })
})
