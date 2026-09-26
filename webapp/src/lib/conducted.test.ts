import { describe, expect, it } from 'vitest'
import {
  assumedSummary, conductedParams, enteredFrom, fmtHz, fmtShare, PARAM_FIELDS, verdict, xOf,
} from './conducted'
import type { ConductedDoc } from './conductedTypes'

describe('conducted params', () => {
  it('sends entered values in SI and leaves the rest assumed', () => {
    const p = conductedParams('B', { U3: { frequency_hz: 2200, duty: 27.5 }, U4: {} })
    expect(p.class).toBe('B')
    expect(p.regulators).toEqual({ U3: { frequency_hz: 2.2e6, duty: 0.275 } })
    expect(p.entry).toBeUndefined()
  })

  it('drops a value outside the range the worker accepts rather than send a refused run', () => {
    const p = conductedParams('A', { U3: { frequency_hz: 1, rise_s: 5 } }, 'J1:+12V')
    expect(p.regulators).toEqual({ U3: { rise_s: 5e-9 } })
    expect(p.entry).toBe('J1:+12V')
  })

  it('round-trips a previous run\'s params into the form', () => {
    const entered = enteredFrom({ regulators: { U3: { frequency_hz: 2.2e6, input_current_a: 0.8, rise_s: 5e-9 } } })
    expect(entered).toEqual({ U3: { frequency_hz: 2200, input_current_a: 0.8, rise_s: 5 } })
    expect(conductedParams('B', entered).regulators).toEqual({
      U3: { frequency_hz: 2.2e6, input_current_a: 0.8, rise_s: 5e-9 },
    })
  })

  it('matches the worker\'s ranges', () => {
    // worker/emi_worker/conducted/sources.py LIMITS, in SI.
    const worker = { frequency_hz: [10e3, 10e6], input_current_a: [1e-3, 100], duty: [0.02, 0.98], rise_s: [0.1e-9, 1e-6] }
    for (const f of PARAM_FIELDS) {
      expect(f.min * f.scale).toBeCloseTo(worker[f.name][0], 12)
      expect(f.max * f.scale).toBeCloseTo(worker[f.name][1], 12)
    }
  })
})

describe('conducted wording', () => {
  it('says which way the margin goes', () => {
    const w = { f_hz: 500e3, margin_db: -12.34, dbuv: 58, detector: 'average' as const, sources: [] }
    expect(verdict(w)).toBe('Over the limit by 12.3 dB at 500 kHz (average limit).')
    expect(verdict({ ...w, margin_db: 3 })).toBe('Under the limit by 3.0 dB at 500 kHz (average limit).')
    expect(verdict(null)).toMatch(/No harmonic/)
  })

  it('names the assumed settings', () => {
    const doc = {
      regulators: [
        { ref: 'U3', assumed: ['frequency_hz', 'input_current_a'] },
        { ref: 'U4', assumed: [] },
      ],
    } as unknown as ConductedDoc
    expect(assumedSummary(doc)).toBe('U3 (frequency, current)')
  })

  it('keeps small shares readable', () => {
    expect(fmtShare(0.98)).toBe('98 %')
    expect(fmtShare(0.00012)).toBe('0.012 %')
    expect(fmtShare(1e-9)).toBe('less than 0.001 %')
  })

  it('formats frequencies and spans the band on a log axis', () => {
    expect(fmtHz(450e3)).toBe('450 kHz')
    expect(fmtHz(2.2e6)).toBe('2.20 MHz')
    expect(xOf(150e3, 0, 100)).toBeCloseTo(0)
    expect(xOf(30e6, 0, 100)).toBeCloseTo(100)
  })
})
