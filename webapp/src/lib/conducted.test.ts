import { describe, expect, it } from 'vitest'
import {
  assumedSummary, conductedParams, enteredFrom, fieldsFor, fmtHz, fmtShare, PARAM_FIELDS, upgradeDoc, verdict, xOf,
  type Entered,
} from './conducted'
import type { ConductedDoc, ConductedRegulator, ConductedTopology } from './conductedTypes'

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
    const worker = {
      frequency_hz: [10e3, 10e6], input_current_a: [1e-3, 100], duty: [0.02, 0.98], rise_s: [0.1e-9, 1e-6],
      phase_deg: [0, 360], inductance_h: [10e-9, 10e-3],
    }
    for (const f of PARAM_FIELDS) {
      expect(f.min * f.scale).toBeCloseTo(worker[f.name][0], 12)
      expect(f.max * f.scale).toBeCloseTo(worker[f.name][1], 12)
    }
  })

  it('sends a changed type, a removal and a confirmation, and reads them back', () => {
    const entered: Entered = {
      'U1/VLX1': { topology: 'boost', confirmed: true, phase_deg: 180, inductance_h: 4.7 },
      U2: { removed: true },
    }
    const p = conductedParams('B', entered)
    expect(p.regulators).toEqual({
      'U1/VLX1': { topology: 'boost', confirmed: true, phase_deg: 180, inductance_h: 4.7e-6 },
      U2: { removed: true },
    })
    expect(enteredFrom(p)).toEqual(entered)
  })

  it('reads a result from before regulators had ids as bucks', () => {
    const old = {
      format: 1,
      regulators: [{ ref: 'U1', switch_net: '/SW', found_by: 'its name', params: {}, assumed: [] }],
    } as unknown as ConductedDoc
    const r = upgradeDoc(old).regulators[0]
    expect([r.id, r.topology, r.found_as, r.group, r.confirmed]).toEqual(['U1', 'buck', 'buck', 'U1', false])
    expect(r.found_by).toBe('switch node /SW (its name)')
  })

  it('shows the fields each topology depends on', () => {
    const reg = { params: { phase_deg: { value: 0, source: 'assumed', assumed: true } } } as unknown as ConductedRegulator
    const names = (t: ConductedTopology, shared: boolean) => fieldsFor(reg, t, shared).map((f) => f.name)
    expect(names('buck', false)).toEqual(['frequency_hz', 'input_current_a', 'duty', 'rise_s'])
    expect(names('boost', false)).toEqual(['frequency_hz', 'input_current_a', 'duty', 'inductance_h'])
    expect(names('buck', true)).toContain('phase_deg')
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
        { id: 'U3', ref: 'U3', assumed: ['frequency_hz', 'input_current_a'] },
        { id: 'U4', ref: 'U4', assumed: [] },
        { id: 'U1/VLX2', ref: 'U1', assumed: ['phase_deg', 'inductance_h'] },
      ],
    } as unknown as ConductedDoc
    expect(assumedSummary(doc)).toBe('U3 (frequency, current), U1/VLX2 (phase, inductor)')
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
