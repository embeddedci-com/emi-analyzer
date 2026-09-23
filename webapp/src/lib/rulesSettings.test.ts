import { describe, expect, it } from 'vitest'
import {
  defaultSnapshot, editBoard, editRule, isEmptyLayer, layerFromParams, overlay, paramLabel,
  parseSetting, rulesFileText, sameLayer, toYaml, type SettingsSnapshot,
} from './rulesSettings'
// Written by this suite's own export, and read by worker/tests/test_rules_export.py.
import fixture from '../../../worker/tests/fixtures/rules_export.yaml?raw'

/** Settings as a rules file left them: one parameter, a group and a suppression. */
function fromFile(): SettingsSnapshot {
  const s = defaultSnapshot()
  s.rules['ddr-skew'].params.byte_lane_ps = { value: 6, source: 'file' }
  s.rules['plane-gap'].severity = 'info'
  s.rules['plane-gap'].severity_source = 'file'
  s.groups = [{ match: 'DDR_DQ*', params: { byte_lane_ps: 5 } }]
  s.suppress = [{ rule: 'edge-proximity', net: 'GND', reason: 'guard ring, intentional' }]
  return s
}

describe('the app layer', () => {
  it('holds only what differs from what is underneath', () => {
    const base = fromFile()
    let layer = editRule({}, base, 'radiator', { enabled: false })
    layer = editRule(layer, base, 'via-stub', { param: ['resonance_margin', 6] })
    // Setting a value to what the rules file already says is not an edit.
    layer = editRule(layer, base, 'ddr-skew', { param: ['byte_lane_ps', 6] })
    layer = editBoard(layer, base, 'max_frequency_hz', 1.6e9)
    expect(layer).toEqual({
      board: { max_frequency_hz: 1.6e9 },
      rules: { radiator: { enabled: false }, 'via-stub': { params: { resonance_margin: 6 } } },
    })

    // Clearing an edit removes it, and an emptied rule goes with it.
    layer = editRule(layer, base, 'radiator', { enabled: null })
    layer = editRule(layer, base, 'via-stub', { param: ['resonance_margin', null] })
    layer = editBoard(layer, base, 'max_frequency_hz', null)
    expect(isEmptyLayer(layer)).toBe(true)
  })

  it('shows each value with the layer it came from', () => {
    const base = fromFile()
    const layer = editRule({}, base, 'plane-gap', { severity: 'critical' })
    const s = overlay(base, layer)
    expect(s.rules['plane-gap']).toMatchObject({ severity: 'critical', severity_source: 'run' })
    expect(s.rules['ddr-skew'].params.byte_lane_ps).toEqual({ value: 6, source: 'file' })
    expect(s.rules['ddr-skew'].params.intra_pair_ps).toEqual({ value: 2, source: 'default' })
    expect(overlay(base, {})).toEqual(base)
  })

  it('is read back from a run, dropping anything the editor would not write', () => {
    const params = {
      settings: {
        board: { max_frequency_hz: 2e9, bogus: 1 },
        rules: {
          radiator: { enabled: false, severity: 'loud' },
          'via-stub': { params: { resonance_margin: 5, nope: 1 } },
          'no-such-rule': { enabled: false },
        },
      },
    }
    expect(layerFromParams(params)).toEqual({
      board: { max_frequency_hz: 2e9 },
      rules: { radiator: { enabled: false }, 'via-stub': { params: { resonance_margin: 5 } } },
    })
    expect(layerFromParams(undefined)).toEqual({})
    expect(layerFromParams({ settings: 'x' })).toEqual({})
  })

  it('compares edits regardless of key order', () => {
    expect(sameLayer(
      { rules: { a: { enabled: false, severity: 'info' } }, board: { x: 1 } },
      { board: { x: 1 }, rules: { a: { severity: 'info', enabled: false } } },
    )).toBe(true)
    expect(sameLayer({}, { rules: { a: { enabled: false } } })).toBe(false)
  })
})

describe('parseSetting', () => {
  it('refuses what the worker would refuse, with its reason', () => {
    expect(parseSetting('2.5')).toEqual({ value: 2.5 })
    expect(parseSetting('1e9', { positive: true })).toEqual({ value: 1e9 })
    expect(parseSetting('')).toEqual({ error: 'Enter a number' })
    expect(parseSetting('1GHz')).toEqual({ error: 'Must be a number' })
    expect(parseSetting('-1')).toEqual({ error: 'Must be zero or more' })
    expect(parseSetting('0', { positive: true })).toEqual({ error: 'Must be above zero' })
    expect(parseSetting('0.5', { key: 'epsilon_r' })).toHaveProperty('error')
    expect(parseSetting('0', { key: 'epsilon_r' })).toEqual({ value: 0 })
  })
})

describe('paramLabel', () => {
  it('splits the unit off the key', () => {
    expect(paramLabel('max_distance_mm')).toEqual({ label: 'Max distance', unit: 'mm' })
    expect(paramLabel('min_area_mm2')).toEqual({ label: 'Min area', unit: 'mm²' })
    expect(paramLabel('tolerance_pct')).toEqual({ label: 'Tolerance', unit: '%' })
    expect(paramLabel('resonance_margin')).toEqual({ label: 'Resonance margin', unit: '' })
  })
})

describe('export', () => {
  it('writes YAML that PyYAML reads back as the same values', () => {
    expect(toYaml({ a: 1e-7, b: 'x: y', c: [], d: {}, e: [{ f: 1 }], 'g h': true })).toBe(
      'a: 1.0e-7\nb: "x: y"\nc: []\nd: {}\ne:\n  - f: 1\n"g h": true\n',
    )
  })

  /**
   * The same file is read by worker/tests/test_rules_export.py, which loads it as a rules file
   * and checks every value lands. After a deliberate change to the writer, replace the fixture
   * with the new output and check that test still passes.
   */
  it('matches the fixture the worker reads', () => {
    const base = fromFile()
    let layer = editRule({}, base, 'radiator', { enabled: false })
    layer = editRule(layer, base, 'edge-proximity', { severity: 'info' })
    layer = editRule(layer, base, 'via-stub', { param: ['resonance_margin', 6] })
    layer = editBoard(layer, base, 'max_frequency_hz', 1.6e9)
    expect(rulesFileText(overlay(base, layer))).toBe(fixture)
  })

  it('writes almost nothing for a board on defaults', () => {
    expect(rulesFileText(defaultSnapshot()).split('\n').filter((l) => l && !l.startsWith('#')))
      .toEqual(['version: 1'])
  })
})
