import { describe, expect, it } from 'vitest'
import {
  addSuppression, checkGroup, checkSuppression, defaultSnapshot, editBoard, editRule, globToRegExp, isEmptyLayer,
  layerFromParams, matchNets, netList, overlay, paramLabel, parseSetting, PER_NET_PARAMS,
  rulesFileText, sameLayer, setGroups, setSuppressions, toRulesDocument, toYaml,
  type SettingsSnapshot,
} from './rulesSettings'
// Written by this suite's own export, and read by worker/tests/test_rules_export.py.
import fixture from '../../../worker/tests/fixtures/rules_export.yaml?raw'
// Also run by worker/tests/test_ingest_settings_notes.py against the worker's matcher.
import patterns from '../../../worker/tests/fixtures/net_patterns.json'

/** Settings as a rules file left them: one parameter, a group and a suppression. */
function fromFile(): SettingsSnapshot {
  const s = defaultSnapshot()
  s.rules['ddr-skew'].params.byte_lane_ps = { value: 6, source: 'file' }
  s.rules['plane-gap'].severity = 'info'
  s.rules['plane-gap'].severity_source = 'file'
  s.groups = [{ match: 'DDR_DQ*', params: { byte_lane_ps: 5 }, source: 'file' }]
  s.suppress = [{ rule: 'edge-proximity', net: 'GND', reason: 'guard ring, intentional', source: 'file' }]
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
    layer = setGroups(layer, base, [{ match: 'CLK*', params: { single_ended_ohm: 50 } }])
    layer = setSuppressions(layer, base, [{ rule: 'plane-gap', net: 'DATA', reason: 'slot is intentional' }])
    expect(rulesFileText(overlay(base, layer))).toBe(fixture)
  })

  it('writes almost nothing for a board on defaults', () => {
    expect(rulesFileText(defaultSnapshot()).split('\n').filter((l) => l && !l.startsWith('#')))
      .toEqual(['version: 1'])
  })
})

describe('net groups and suppressions', () => {
  it('lists the rules file first, then the app, each saying where it came from', () => {
    const base = fromFile()
    let layer = setGroups({}, base, [{ match: 'CLK*', params: { single_ended_ohm: 50 } }])
    layer = setSuppressions(layer, base, [{ rule: 'radiator', net: 'DATA', reason: 'test pad' }])
    const s = overlay(base, layer)
    expect(s.groups.map((g) => [g.match, g.source])).toEqual([['DDR_DQ*', 'file'], ['CLK*', 'run']])
    expect(s.suppress.map((x) => [x.rule, x.source])).toEqual([['edge-proximity', 'file'], ['radiator', 'run']])
    // The export is a rules file: sources are not part of it.
    const doc = toRulesDocument(s) as { groups: object[]; suppress: object[] }
    expect(doc.groups).toEqual([
      { match: 'DDR_DQ*', params: { byte_lane_ps: 5 } }, { match: 'CLK*', params: { single_ended_ohm: 50 } }])
    expect(doc.suppress[1]).toEqual({ rule: 'radiator', net: 'DATA', reason: 'test pad' })
  })

  it('is part of the layer, and removing the last one empties it', () => {
    const base = fromFile()
    const layer = setSuppressions({}, base, [{ rule: 'radiator', net: 'DATA', reason: 'x' }])
    expect(isEmptyLayer(layer)).toBe(false)
    expect(sameLayer(layer, {})).toBe(false)
    expect(isEmptyLayer(setSuppressions(layer, base, []))).toBe(true)
    expect(isEmptyLayer(setGroups({}, base, []))).toBe(true)
  })

  it('adds a suppression from a finding once', () => {
    const base = fromFile()
    const s = { rule: 'radiator', net: 'DATA', reason: 'test pad' }
    const layer = addSuppression({}, base, s)
    expect(layer.suppress).toEqual([s])
    expect(addSuppression(layer, base, s)).toBe(layer)
    // Already in the rules file: nothing to add.
    const inFile = { rule: 'edge-proximity', net: 'GND', reason: 'guard ring, intentional' }
    expect(addSuppression({}, base, inFile)).toEqual({})
  })

  it('is read back from a run, dropping what the worker would not use', () => {
    expect(layerFromParams({ settings: {
      groups: [
        { match: 'DQ*', params: { byte_lane_ps: 4, max_distance_mm: 3 } },
        { match: 'X*', params: { max_distance_mm: 3 } },
        { netclass: 'USB', params: { differential_ohm: 90 } },
        'junk',
      ],
      suppress: [{ rule: 'radiator', net: 'DATA', reason: 'ok' }, { rule: 'radiator' }],
    } })).toEqual({
      groups: [{ match: 'DQ*', params: { byte_lane_ps: 4 } }],
      suppress: [{ rule: 'radiator', net: 'DATA', reason: 'ok' }],
    })
  })

  it('matches net patterns the way the worker does', () => {
    for (const [pattern, net, expected] of patterns as [string, string, boolean][]) {
      expect([pattern, net, globToRegExp(pattern).test(net)]).toEqual([pattern, net, expected])
    }
    expect(matchNets('D*', ['CLK', 'DATA', 'DQ0'])).toEqual(['DATA', 'DQ0'])
    expect(netList(['A', 'B', 'C', 'D', 'E', 'F'])).toBe('A, B, C, D and 2 more')
  })

  it('only offers the parameters a rule reads per net', () => {
    expect(PER_NET_PARAMS.map((p) => `${p.rule.id}.${p.key}`).sort()).toEqual([
      'ddr-skew.address_command_ps', 'ddr-skew.byte_lane_ps', 'ddr-skew.intra_pair_ps',
      'impedance.differential_ohm', 'impedance.single_ended_ohm',
    ])
  })

  it('checks a group as the worker would', () => {
    expect(checkGroup(' DQ* ', [['byte_lane_ps', '4']])).toEqual({ group: { match: 'DQ*', params: { byte_lane_ps: 4 } } })
    expect(checkGroup('', [])).toEqual({ errors: {
      pattern: 'Enter a net name or pattern', params: 'Add at least one setting', values: {} } })
    expect(checkGroup('DQ*', [['max_distance_mm', '3'], ['byte_lane_ps', '-1']])).toEqual({ errors: {
      values: { max_distance_mm: 'Not a per-net setting', byte_lane_ps: 'Must be zero or more' } } })
  })

  it('needs a reason and a check that exists before it will suppress anything', () => {
    expect(checkSuppression({ rule: 'radiator', net: 'DATA', reason: ' test pad ' }))
      .toEqual({ suppression: { rule: 'radiator', net: 'DATA', reason: 'test pad' } })
    expect(checkSuppression({ rule: 'radiatr', net: '', reason: ' ' })).toEqual({ errors: {
      rule: 'No check has this id', net: 'Enter a net name or pattern',
      reason: 'Say why, for whoever reads this next' } })
    expect(checkSuppression({ rule: '*', net: 'GND', reason: 'r' })).toHaveProperty('suppression')
  })
})
