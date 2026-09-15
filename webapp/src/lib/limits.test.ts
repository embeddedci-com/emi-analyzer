/**
 * The browser's half of C1 and C2.
 *
 * Same assertions as `worker/tests/test_limits.py`, against the same JSON file — not a copy
 * of it. If a limit value is ever edited, both halves move together or both fail.
 */

import { describe, expect, it } from 'vitest'
import {
  limitAt,
  limitLine,
  LimitError,
  scanToHz,
  standard,
  STANDARDS,
} from './limits'

describe('C1 — segments match the cited text', () => {
  it('Class B radiated is 100/150/200/500 uV/m at 3 m', () => {
    for (const [fMhz, uv] of [[40, 100], [87.9, 100], [100, 150], [215, 150],
                              [300, 200], [959, 200], [1000, 500]] as const) {
      expect(limitAt('fcc-15b-radiated-3m', fMhz * 1e6)).toBeCloseTo(20 * Math.log10(uv), 1)
    }
  })

  it('Class A radiated is 90/150/210/300 uV/m at 10 m', () => {
    for (const [fMhz, uv] of [[40, 90], [100, 150], [300, 210], [1000, 300]] as const) {
      expect(limitAt('fcc-15a-radiated-10m', fMhz * 1e6)).toBeCloseTo(20 * Math.log10(uv), 1)
    }
  })

  it('has agreeing decibel and microvolt columns', () => {
    for (const std of STANDARDS) {
      for (const seg of std.segments) {
        if (seg.microvolts_per_m === undefined) continue
        expect(seg.level_db).toBeCloseTo(20 * Math.log10(seg.microvolts_per_m), 1)
      }
    }
  })

  it('takes the tighter limit at a band edge', () => {
    expect(limitAt('fcc-15b-radiated-3m', 88e6)).toBeCloseTo(40.0, 1)
    expect(limitAt('fcc-15b-radiated-3m', 216e6)).toBeCloseTo(43.5, 1)
    expect(limitAt('fcc-15b-radiated-3m', 960e6)).toBeCloseTo(46.0, 1)
    expect(limitAt('fcc-15b-radiated-3m', 88.001e6)).toBeCloseTo(43.5, 1)
  })

  it('takes the tighter side where the conducted limit steps up at 5 MHz', () => {
    expect(limitAt('fcc-15b-conducted-qp', 5e6)).toBeCloseTo(56.0, 6)
    expect(limitAt('fcc-15b-conducted-qp', 5.001e6)).toBeCloseTo(60.0, 6)
  })
})

describe('C2 — conducted interpolation', () => {
  it('is linear in log frequency: 61 dBuV at the geometric mean', () => {
    expect(limitAt('fcc-15b-conducted-qp', 0.15e6)).toBeCloseTo(66.0, 6)
    expect(limitAt('fcc-15b-conducted-qp', 0.5e6)).toBeCloseTo(56.0, 6)
    const geometricMean = Math.sqrt(0.15e6 * 0.5e6)
    expect(geometricMean / 0.2739e6).toBeCloseTo(1, 3)
    expect(limitAt('fcc-15b-conducted-qp', geometricMean)).toBeCloseTo(61.0, 6)
  })

  it('slopes the average detector the same way', () => {
    expect(limitAt('fcc-15b-conducted-avg', Math.sqrt(0.15e6 * 0.5e6))).toBeCloseTo(51.0, 6)
  })

  it('leaves Class A conducted flat', () => {
    expect(limitAt('fcc-15a-conducted-qp', 0.3e6)).toBeCloseTo(79.0, 6)
  })
})

describe('refusals', () => {
  it('refuses a frequency outside the standard rather than extrapolating', () => {
    expect(() => limitAt('fcc-15b-radiated-3m', 10e6)).toThrow(/says nothing at/)
    expect(() => limitAt('fcc-15b-conducted-qp', 50e6)).toThrow(LimitError)
  })

  it('lists the known standards when asked for an unknown one', () => {
    expect(() => limitAt('en-55032-b', 100e6)).toThrow(/known: fcc-15a/)
  })
})

describe('drawing', () => {
  it('emits both sides of every band edge so the step stays vertical', () => {
    const line = limitLine('fcc-15b-radiated-3m')
    const at88 = line.filter((p) => p.frequency_hz === 88e6)
    expect(at88).toHaveLength(2)
    expect(at88.map((p) => p.level_db).sort()).toEqual([40.0, 43.5])
  })
})

describe('15.33(b) scan range', () => {
  it('follows the table', () => {
    expect(scanToHz(1e6)).toBeCloseTo(30e6)
    expect(scanToHz(100e6)).toBeCloseTo(1e9)
    expect(scanToHz(200e6)).toBeCloseTo(2e9)
    expect(scanToHz(600e6)).toBeCloseTo(5e9)
  })

  it('becomes the fifth harmonic above 1 GHz, capped at 40 GHz', () => {
    expect(scanToHz(2e9)).toBeCloseTo(10e9)
    expect(scanToHz(9e9)).toBeCloseTo(40e9)
  })
})

describe('provenance', () => {
  it('declares where every standard came from', () => {
    for (const std of STANDARDS) {
      expect(['standard text', 'secondary sources']).toContain(std.verified)
      expect(std.source.length).toBeGreaterThan(0)
      expect(std.clause.length).toBeGreaterThan(0)
    }
  })

  it('knows the Class B geometry', () => {
    const std = standard('fcc-15b-radiated-3m')
    expect(std.distance_m).toBe(3)
    expect(std.unit).toBe('dBuV/m')
  })
})
