/**
 * The browser half of the cable-emission contract.
 *
 * The same fixtures are checked by `worker/tests/test_cable_emission.py`. If this file and
 * that one disagree, a user reading a margin in the browser is reading a different number
 * from the one a worker put in the result — and both will look equally confident.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/cable_emission_fixtures.json'
import {
  composeEmission, currentDbua, fieldDbuv, marginDb, worstPoint,
  type CableAntenna, type CableTransfer,
} from './cableEmission'
import type { Complex } from './driverSpectrum'

const caseInput = (c: (typeof fixtures.cases)[number]) => {
  const transfer = c.input.transfer as CableTransfer
  const antenna = c.input.antenna as CableAntenna
  const volts: (Complex | null)[] = (c.input.source_volts as (number[] | null)[]).map((v) =>
    v === null ? null : { re: v[0], im: v[1] },
  )
  return { transfer, antenna, volts, standardId: c.input.standard_id }
}

describe('cable emission, against the shared fixtures', () => {
  for (const c of fixtures.cases) {
    it(c.name, () => {
      const { transfer, antenna, volts, standardId } = caseInput(c)
      const em = composeEmission(transfer, antenna, volts, standardId)

      expect(em.points.map((p) => p.frequency_hz)).toEqual(
        c.expected.points.map((p) => p.frequency_hz),
      )
      em.points.forEach((got, k) => {
        const want = c.expected.points[k]
        expect(currentDbua(got)).toBeCloseTo(want.current_dbua, 9)
        expect(fieldDbuv(got)).toBeCloseTo(want.field_dbuv_per_m, 9)
        expect(got.limit_dbuv_per_m).toBeCloseTo(want.limit_dbuv_per_m, 9)
        expect(marginDb(got)).toBeCloseTo(want.margin_db, 9)
      })
      expect([...em.undriven.keys()].sort((a, b) => a - b)).toEqual(
        c.expected.undriven_hz,
      )
    })
  }
})

describe('cable emission', () => {
  const base = fixtures.cases[0]

  it('worst margin is the worst, not the highest field', () => {
    const { transfer, antenna, volts, standardId } = caseInput(base)
    const em = composeEmission(transfer, antenna, volts, standardId)
    const worst = worstPoint(em)
    expect(worst).not.toBeNull()
    // 100 MHz is 14.9 dB over; 300 MHz radiates less but sits under a limit that steps up,
    // so a chart that ranked by field alone would point at the wrong frequency.
    expect(worst!.frequency_hz).toBe(100e6)
    for (const p of em.points) expect(marginDb(p)).toBeGreaterThanOrEqual(marginDb(worst!))
  })

  it('refuses a driver resolved at a different number of frequencies', () => {
    const { transfer, antenna, volts, standardId } = caseInput(base)
    expect(() => composeEmission(transfer, antenna, volts.slice(1), standardId)).toThrow(
      /resolved at 3 frequencies/,
    )
  })

  it('multiplies the phases rather than the magnitudes', () => {
    // H and V both at 45 degrees: the product is at 90 and its magnitude is the product of
    // the two. Adding magnitudes, or dropping the imaginary parts, both give a different
    // answer -- and on real data neither would look obviously wrong.
    const r = Math.SQRT1_2
    const transfer: CableTransfer = {
      ref: 'X1', frequencies_hz: [100e6], h_real: [r * 0.02], h_imag: [r * 0.02],
      usable: [true],
    }
    const antenna: CableAntenna = {
      ref: 'X1', cable_id: 'dc-pigtail', length_m: 1, distance_m: 3,
      frequencies_hz: [100e6], z_real: [100], z_imag: [0], e_per_amp: [10],
    }
    const em = composeEmission(transfer, antenna, [{ re: r * 2, im: r * 2 }])
    expect(em.points).toHaveLength(1)
    // |H| = 0.02*sqrt(2)*... -> |H||V|/|Z| = 0.02*2/100
    expect(em.points[0].current_a).toBeCloseTo((0.02 * 2) / 100, 12)
  })

  it('a frequency the antenna solver never saw is reported, not skipped', () => {
    const { transfer, antenna, volts, standardId } = caseInput(fixtures.cases[1])
    const em = composeEmission(transfer, antenna, volts, standardId)
    expect(em.points).toHaveLength(0)
    expect([...em.undriven.values()].filter((m) => /antenna solver/.test(m))).toHaveLength(1)
  })
})
