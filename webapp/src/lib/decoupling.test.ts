import { describe, expect, it } from 'vitest'
import {
  applyChange, branchesOf, curve, fmtRange, frequencies, gapsOf, networkMag, noiseMarks,
  partModelLabel,
  rankRecommendations, targetOhm, worstOf, type DecBranch, type DecouplingDoc,
} from './decoupling'
import fixture from './decouplingFixture.json'

const doc = fixture as unknown as DecouplingDoc
const freqs = frequencies(doc)
const rail = doc.rails[0]
const ic = rail.ics[0]

const db = (a: number, b: number) => 20 * Math.log10(a / b)

describe('the network', () => {
  it('uses the worker grid', () => {
    expect(freqs).toHaveLength(161)
    expect(freqs[0]).toBeCloseTo(1e5)
    expect(freqs[160]).toBeCloseTo(1e9, -3)
  })

  it('peaks where two capacitors anti-resonate, as the closed form says', () => {
    // Same pair as the worker's test: 8.55 MHz, 0.291 ohm.
    const bulk: DecBranch = { id: 'C1', c_f: 10e-6, l_h: 2e-9, r_ohm: 0.01 }
    const small: DecBranch = { id: 'C2', c_f: 100e-9, l_h: 1.5e-9, r_ohm: 0.03 }
    const cs = (bulk.c_f * small.c_f) / (bulk.c_f + small.c_f)
    const f = 1 / (2 * Math.PI * Math.sqrt((bulk.l_h + small.l_h) * cs))
    const w = 2 * Math.PI * f
    const z1 = Math.hypot(bulk.r_ohm, w * bulk.l_h - 1 / (w * bulk.c_f))
    const z2 = Math.hypot(small.r_ohm, w * small.l_h - 1 / (w * small.c_f))
    const closed = (z1 * z2) / (bulk.r_ohm + small.r_ohm)
    expect(Math.abs(db(networkMag([bulk, small], 0, f), closed))).toBeLessThan(0.5)
    expect(closed).toBeCloseTo(0.291, 2)
  })

  it('divides the ESR of identical parts at their resonance', () => {
    const one: DecBranch = { id: 'C', c_f: 100e-9, l_h: 1e-9, r_ohm: 0.05 }
    const srf = 1 / (2 * Math.PI * Math.sqrt(one.l_h * one.c_f))
    const bank = [0, 1, 2, 3].map((i) => ({ ...one, id: `C${i}` }))
    expect(networkMag(bank, 0, srf)).toBeCloseTo(0.0125, 6)
  })
})

describe('against the worker', () => {
  const branches = branchesOf(ic, rail.parts)
  const z = curve(branches, ic.series_l_h, freqs)

  it('finds the same gaps and the same worst point', () => {
    const gaps = gapsOf(freqs, z, ic.target_ohm, ic.band_hz)
    expect(gaps).toHaveLength(ic.gaps.length)
    gaps.forEach(([lo, hi], i) => {
      expect(lo / ic.gaps[i][0]).toBeCloseTo(1, 3)
      expect(hi / ic.gaps[i][1]).toBeCloseTo(1, 3)
    })
    const worst = worstOf(freqs, z, ic.target_ohm, ic.band_hz)
    expect(worst.hz / ic.worst!.hz).toBeCloseTo(1, 3)
    // Four significant figures in rules.json: within 0.01 dB.
    expect(Math.abs(worst.excessDb - ic.worst!.excess_db)).toBeLessThan(0.01)
  })

  it('ranks the what-ifs as the worker did, at the worker target', () => {
    const ranked = rankRecommendations(ic, rail.parts, freqs, ic.target_ohm)
    expect(ranked.map((r) => r.text)).toEqual(ic.recommendations.map((r) => r.text))
    ranked.forEach((r, i) => {
      expect(Math.abs(r.improvementDb - ic.recommendations[i].improvement_db)).toBeLessThan(0.05)
    })
  })

  it('has nothing to recommend when the target is met', () => {
    const generous = Math.max(...z.slice(0, 121)) * 2
    expect(rankRecommendations(ic, rail.parts, freqs, generous)).toEqual([])
    expect(gapsOf(freqs, z, generous, ic.band_hz)).toEqual([])
  })

  it('applies a change without touching the other branches', () => {
    const removed = applyChange(branches, { op: 'remove', id: 'C2' })
    expect(removed.map((b) => b.id)).toEqual(['C1', 'C3'])
    const added = applyChange(branches, { op: 'add', branch: { id: 'new', c_f: 1e-9, l_h: 1e-9, r_ohm: 0.2 } })
    expect(added).toHaveLength(4)
    const moved = applyChange(branches, { op: 'replace', branch: { id: 'C3', c_f: 1e-5, l_h: 3e-9, r_ohm: 0.015 } })
    expect(moved.find((b) => b.id === 'C3')!.l_h).toBe(3e-9)
    expect(moved.find((b) => b.id === 'C1')).toBe(branches[0])
  })
})

describe('noise frequencies', () => {
  it('marks the harmonics above the target as not filtered, and none above the band', () => {
    const branches = branchesOf(ic, rail.parts)
    const marks = noiseMarks(ic.noise, branches, ic.series_l_h, ic.target_ohm, ic.band_hz, 1e9)
    // 16 MHz, harmonics up to the fifteenth.
    expect(marks[0]).toMatchObject({ hz: 16e6, harmonic: 1, source: 'Y1 16MHz' })
    expect(marks).toHaveLength(15)
    for (const m of marks) {
      expect(m.aboveBand).toBe(m.hz > 100e6)
      if (m.aboveBand) expect(m.notFiltered).toBe(false)
    }
    // 32 MHz sits in the gap that starts near 32 MHz on this board.
    expect(marks.find((m) => m.harmonic === 6)!.notFiltered).toBe(true)
  })
})

describe('target and labels', () => {
  it('computes the target from volts, ripple and step', () => {
    expect(targetOhm(3.3, 5, 0.5)).toBeCloseTo(0.33)
    expect(targetOhm(1.8, 3, 2)).toBeCloseTo(0.027)
    expect(targetOhm(3.3, 5, 0)).toBe(Infinity)
  })

  it('writes ranges plainly', () => {
    expect(fmtRange([30e6, 60e6])).toBe('30 to 60 MHz')
    expect(fmtRange([500e3, 2e6])).toBe('500 kHz to 2 MHz')
  })
})

describe('the model column', () => {
  it('says where a library part came from, from the worker', () => {
    expect(partModelLabel(rail.parts.C1).text).toBe('generic 0402')
  })

  it('names a datasheet part and puts how it matched in the tooltip', () => {
    const got = partModelLabel({
      ...rail.parts.C1, basis: 'datasheet (Samsung CL05B104KO5NNNC)',
      matched_by: 'part number', matched_on: 'LCSC C1525', model: 'Samsung CL05B104KO5NNNC',
    })
    expect(got.text).toBe('datasheet (Samsung CL05B104KO5NNNC)')
    expect(got.title).toContain('Matched on LCSC C1525')
  })

  it('falls back to "library" for a result from before the label existed', () => {
    expect(partModelLabel({ ...rail.parts.C1, basis: undefined }).text).toBe('library')
  })

  it('says what was assumed', () => {
    const got = partModelLabel({ ...rail.parts.C1, source: 'assumed', assumed: ['ESL', 'ESR'] })
    expect(got).toEqual({ text: 'assumed', title: 'Assumed: ESL, ESR' })
  })
})
