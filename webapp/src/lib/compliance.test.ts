/**
 * The browser half of the compliance contract (§19, C3 and C4).
 *
 * The same fixtures are checked by `worker/tests/test_predict.py`. A margin and the confidence
 * beside it are the two numbers most likely to be quoted at someone else, so a disagreement
 * here is a disagreement about what the tool said — not a rounding detail.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/compliance_fixtures.json'
import { combineSigma, fromDbuv, outlook, phi, toDbuv, type PathInput } from './compliance'

const asPaths = (c: (typeof fixtures.cases)[number]): PathInput[] =>
  c.input.paths.map((p) => ({
    kind: p.kind,
    label: p.label,
    driver_id: p.driver_id,
    field_v_per_m: p.field_dbuv_per_m.map(fromDbuv),
    sigma_terms: p.sigma_terms as Record<string, number>,
  }))

describe('compliance, against the shared fixtures', () => {
  for (const c of fixtures.cases) {
    it(c.name, () => {
      const o = outlook(
        asPaths(c),
        c.input.frequencies_hz,
        c.input.standard_id,
        c.input.shared_sigma_terms as unknown as Record<string, number>,
      )
      const want = c.expected

      expect(o.points.map((p) => p.frequencyHz)).toEqual(
        want.spectrum.map((p) => p.frequency_hz),
      )
      o.points.forEach((got, k) => {
        const w = want.spectrum[k]
        expect(got.fieldDbuv).toBeCloseTo(w.field_dbuv_per_m, 9)
        expect(got.limitDbuv).toBeCloseTo(w.limit_dbuv_per_m, 9)
        expect(got.marginDb).toBeCloseTo(w.margin_db, 9)
        expect(got.sharesIndicative).toBe(w.shares_indicative)
        expect(got.contributions.map((x) => x.label)).toEqual(
          w.contributions.map((x) => x.label),
        )
        got.contributions.forEach((x, i) => {
          expect(x.share).toBeCloseTo(w.contributions[i].share, 9)
        })
      })

      expect(o.worst?.frequencyHz ?? null).toBe(want.worst_frequency_hz)
      expect(o.worst!.marginDb).toBeCloseTo(want.margin_db!, 9)
      expect(o.sigmaDb).toBeCloseTo(want.sigma_db, 9)
      expect(Object.keys(o.sigmaTerms).sort()).toEqual(
        Object.keys(want.sigma_terms).sort(),
      )
      // The fixture's per-case sigma_terms have different keys, so TypeScript infers a union
      // of object literals with optional members rather than a plain map. The cast is the
      // narrowing, not a claim about the data.
      for (const [k, v] of Object.entries(
        want.sigma_terms as unknown as Record<string, number>,
      )) {
        expect(o.sigmaTerms[k]).toBeCloseTo(v, 9)
      }
      // The erf approximation is good to 1.5e-7, so confidence agrees to six places -- far
      // beyond the two the UI shows, and enough that a real disagreement cannot hide in it.
      expect(o.confidence!).toBeCloseTo(want.confidence!, 6)
      expect(o.range80Db![0]).toBeCloseTo(want.range_80_db![0], 9)
      expect(o.range80Db![1]).toBeCloseTo(want.range_80_db![1], 9)
      expect(o.nearMisses.map((p) => p.frequencyHz)).toEqual(want.near_misses_hz)
    })
  }
})

describe('compliance arithmetic', () => {
  it('reproduces the design document worked example', () => {
    // cable 4.5, driver 3, mesh normal 2, interpolation 1 -> 5.85; margin +5.1 -> 81 %.
    const sigma = combineSigma({ cable: 4.5, driver: 3, mesh: 2, interpolation: 1 })
    expect(sigma).toBeCloseTo(5.85, 2)
    // The document quotes 81 %, which is what 0.8082 shows as. Matched to the same
    // two places the Python side uses, since that is the precision the figure is
    // ever displayed at.
    expect(phi(5.1 / sigma)).toBeCloseTo(0.81, 2)
    expect(5.1 - 1.28 * sigma).toBeCloseTo(-2.4, 1)
    expect(5.1 + 1.28 * sigma).toBeCloseTo(12.6, 1)
  })

  it('adds one driver\'s paths in amplitude and different drivers in power', () => {
    const one = outlook(
      [
        { kind: 'board', label: 'a', driver_id: 'clk', field_v_per_m: [1e-3], sigma_terms: {} },
        { kind: 'cable', label: 'b', driver_id: 'clk', field_v_per_m: [1e-3], sigma_terms: {} },
      ],
      [100e6], 'fcc-15b-radiated-3m',
    )
    expect(one.points[0].fieldVPerM).toBeCloseTo(2e-3, 12)

    const two = outlook(
      [
        { kind: 'board', label: 'a', driver_id: 'clk', field_v_per_m: [1e-3], sigma_terms: {} },
        { kind: 'board', label: 'b', driver_id: 'buck', field_v_per_m: [1e-3], sigma_terms: {} },
      ],
      [100e6], 'fcc-15b-radiated-3m',
    )
    expect(two.points[0].fieldVPerM).toBeCloseTo(Math.SQRT2 * 1e-3, 12)
  })

  it('phi is a CDF', () => {
    expect(phi(0)).toBeCloseTo(0.5, 6)
    expect(phi(-6)).toBeLessThan(1e-6)
    expect(phi(6)).toBeGreaterThan(1 - 1e-6)
    expect(phi(1.28)).toBeCloseTo(0.8997, 3)
  })

  it('round-trips dBµV/m', () => {
    expect(toDbuv(1e-6)).toBeCloseTo(0, 12)
    expect(fromDbuv(toDbuv(3.3e-4))).toBeCloseTo(3.3e-4, 15)
  })
})
