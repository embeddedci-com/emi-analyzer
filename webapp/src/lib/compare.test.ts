/**
 * Comparing two versions of a board. The finding matcher is the part that can be quietly
 * wrong: a finding that moved half a millimetre must not be reported as one fixed and one new.
 */

import { describe, expect, it } from 'vitest'
import type { NetReport, NetRow, RuleFinding } from './boardTypes'
import type { Board, Run } from './emiApi'
import type { CablesDoc } from './cableTypes'
import type { TransientDoc, TransientLine } from './transientTypes'
import {
  defaultPair, diffCables, diffEsd, diffFindings, diffNets, findingsHeadline, groupByRule,
  latestDone, resample, versionsOf,
} from './compare'

function finding(over: Partial<RuleFinding>): RuleFinding {
  return {
    id: 'x', rule: 'return-via', severity: 'warning', title: 'Missing return via',
    detail: '', net: 'CLK', layer: 'F.Cu', x: 10, y: 10, ...over,
  }
}

describe('diffFindings', () => {
  it('matches a finding that moved within the tolerance, whatever its id and title', () => {
    const d = diffFindings(
      [finding({ id: 'return-via-0', title: 'Via 3.1 mm from a return' })],
      [finding({ id: 'return-via-4', title: 'Via 2.8 mm from a return', x: 11.2, y: 10.5 })],
    )
    expect(d.changes.map((c) => c.status)).toEqual(['unchanged'])
    expect(findingsHeadline(d.counts)).toBe('No change')
  })

  it('reports a finding that moved too far as one fixed and one new', () => {
    const d = diffFindings([finding({})], [finding({ x: 20 })])
    expect(d.changes.map((c) => c.status).sort()).toEqual(['fixed', 'new'])
  })

  it('never matches across rule, net or layer', () => {
    const d = diffFindings(
      [finding({}), finding({ net: 'DATA' }), finding({ layer: 'B.Cu' })],
      [finding({ rule: 'plane-gap' }), finding({ net: 'SDA' }), finding({ layer: 'In1.Cu' })],
    )
    expect(d.counts.fixed.total).toBe(3)
    expect(d.counts.new.total).toBe(3)
    expect(d.counts.unchanged.total).toBe(0)
  })

  it('pairs the closest findings first, not the first in the file', () => {
    // Before has two findings on one net; after has the far one only, listed first. A greedy
    // match in file order would pair the near one with it and call the far one fixed.
    const near = finding({ id: 'a', x: 10, y: 10 })
    const far = finding({ id: 'b', x: 11.5, y: 10 })
    const d = diffFindings([near, far], [finding({ id: 'c', x: 11.6, y: 10 })])
    const unchanged = d.changes.find((c) => c.status === 'unchanged')!
    expect(unchanged.before!.id).toBe('b')
    expect(d.changes.find((c) => c.status === 'fixed')!.before!.id).toBe('a')
  })

  it('matches findings with no place on the board to each other, by title first', () => {
    const summary = (title: string) => finding({ x: null, y: null, title, net: '' })
    const d = diffFindings([summary('A'), summary('B')], [summary('B'), summary('C')])
    const pairs = d.changes.filter((c) => c.status === 'unchanged')
    expect(pairs.map((c) => [c.before!.title, c.after!.title]).sort()).toEqual([['A', 'C'], ['B', 'B']])
    // One located and one not are never the same finding.
    expect(diffFindings([summary('A')], [finding({ net: '' })]).counts.unchanged.total).toBe(0)
  })

  it('counts by severity, taking a matched finding at its new severity', () => {
    const d = diffFindings(
      [finding({ severity: 'critical', net: 'A' }), finding({ severity: 'warning', net: 'B' })],
      [finding({ severity: 'warning', net: 'B' }), finding({ severity: 'info', net: 'C' }),
        finding({ severity: 'critical', net: 'D' })],
    )
    expect(d.counts.fixed).toEqual({ critical: 1, warning: 0, info: 0, total: 1 })
    expect(d.counts.new).toEqual({ critical: 1, warning: 0, info: 1, total: 2 })
    expect(findingsHeadline(d.counts)).toBe('1 fixed, 2 new')
  })

  it('groups by rule, rules with something new first, new before fixed inside a rule', () => {
    const d = diffFindings(
      [finding({ rule: 'plane-gap', net: 'A' }), finding({ net: 'B' })],
      [finding({ net: 'C', severity: 'info' }), finding({ net: 'D', severity: 'critical' })],
    )
    const groups = groupByRule(d.changes)
    expect(groups.map(([rule]) => rule)).toEqual(['return-via', 'plane-gap'])
    expect(groups[0][1].map((c) => `${c.status}:${(c.after ?? c.before)!.net}`))
      .toEqual(['new:D', 'new:C', 'fixed:B'])
  })
})

function row(over: Partial<NetRow>): NetRow {
  return {
    net: 'SIG', netclass: '', kind: 'signal', topology: 'point-to-point', pads: 2,
    unreachable_pads: 0, copper_mm: 10, path_mm: 10, path_from: '', path_to: '',
    path_by_layer: '', path_vias: 0, vias_total: 0, delay_ps: null,
    width_min_mm: 0.2, width_max_mm: 0.2, diff_pair_partner: '', match_group: '',
    match_kind: '', match_reference: '', reference_delay_ps: null, skew_ps: null, skew_mm: null,
    tolerance_ps: null, within_tolerance: '', clock_net: '', vs_clock_ps: null, vs_clock_mm: null,
    ...over,
  }
}
const report = (rows: NetRow[]): NetReport => ({ format_version: 1, clock_net: '', epsilon_assumed: false, rows })

describe('diffNets', () => {
  it('lists changed, added and removed nets, the biggest length change first', () => {
    const out = diffNets(
      report([row({ net: 'A' }), row({ net: 'B' }), row({ net: 'C', vias_total: 1 }),
        row({ net: 'OLD' }), row({ net: 'SAME', path_mm: 5.004 })]),
      report([row({ net: 'A', path_mm: 11 }), row({ net: 'B', path_mm: 16 }),
        row({ net: 'C', vias_total: 2 }), row({ net: 'NEW' }), row({ net: 'SAME', path_mm: 5.0 })]),
    )
    expect(out.map((c) => `${c.status}:${c.net}`))
      .toEqual(['changed:B', 'changed:A', 'changed:C', 'added:NEW', 'removed:OLD'])
    expect(out[2]).toMatchObject({ viasBefore: 1, viasAfter: 2 })
  })

  it('falls back to all the copper when a net has no routed path', () => {
    const out = diffNets(report([row({ path_mm: null, copper_mm: 4 })]),
      report([row({ path_mm: null, copper_mm: 6 })]))
    expect(out[0]).toMatchObject({ lengthBefore: 4, lengthAfter: 6 })
  })
})

describe('versions', () => {
  const board = (id: string, created: string, key = `uploads/o/${id}/${id}.kicad_pcb`): Board =>
    ({ id, project_id: 'p', s3_input_key: key, layer_count: 2, net_count: 3, created_at: created })

  it('numbers boards oldest first and names them by their file', () => {
    const v = versionsOf([board('b', '2026-01-02T00:00:00Z'), board('a', '2026-01-01T00:00:00Z')])
    expect(v.map((x) => [x.number, x.board.id, x.filename])).toEqual([[1, 'a', 'a.kicad_pcb'], [2, 'b', 'b.kicad_pcb']])
  })

  it('compares the two newest by default, and what the URL names otherwise', () => {
    const v = versionsOf([board('a', '2026-01-01T00:00:00Z'), board('b', '2026-01-02T00:00:00Z'),
      board('c', '2026-01-03T00:00:00Z')])
    expect(defaultPair(v)!.map((x) => x.board.id)).toEqual(['b', 'c'])
    expect(defaultPair(v, 'a', 'b')!.map((x) => x.board.id)).toEqual(['a', 'b'])
    expect(defaultPair(v, 'nope', 'b')!.map((x) => x.board.id)).toEqual(['c', 'b'])
    expect(defaultPair(v, 'b', 'b')!.map((x) => x.board.id)).toEqual(['c', 'b'])
    expect(defaultPair(v.slice(0, 1))).toBeNull()
  })

  it('takes the newest finished run of a kind on that board only', () => {
    const run = (id: string, board_id: string, kind: Run['kind'], status: Run['status'], created: string): Run =>
      ({ id, project_id: 'p', board_id, kind, status, created_at: created, updated_at: created })
    const runs = [
      run('1', 'a', 'cable', 'done', '2026-01-01T00:00:00Z'),
      run('2', 'a', 'cable', 'done', '2026-01-02T00:00:00Z'),
      run('3', 'a', 'cable', 'failed', '2026-01-03T00:00:00Z'),
      run('4', 'b', 'cable', 'done', '2026-01-04T00:00:00Z'),
    ]
    expect(latestDone(runs, 'a', 'cable')!.id).toBe('2')
    expect(latestDone(runs, 'a', 'transient')).toBeNull()
  })
})

describe('diffCables', () => {
  const doc = (over: Partial<CablesDoc>): CablesDoc => ({
    format: 'emi-cables', format_version: 1, solver: 'nec2c', standard_id: 's', assumptions: [],
    cables: [], unassigned: [], notes: [], ...over,
  })
  it('lists every connector either version has, and says why one is not modelled', () => {
    const out = diffCables(
      doc({ cables: [{ ref: 'J2', cable_id: 'usb', cable_name: 'USB', length_m: 1, declared_none: false,
        tightest: { frequency_hz: 1e8, max_current_dbua: 20, limit_dbuv_per_m: 40 } }] }),
      doc({
        cables: [{ ref: 'J10', cable_id: null, declared_none: true }],
        unassigned: [{ ref: 'J2', footprint: '', suggested: null, reason: '' }],
      }),
    )
    expect(out.map((c) => c.ref)).toEqual(['J2', 'J10'])
    expect(out[0].before).toMatchObject({ cable: 'USB, 1 m' })
    expect(out[0].after).toEqual({ note: 'No cable set' })
    expect(out[1].before).toBeUndefined()
    expect(out[1].after).toEqual({ note: 'Never cabled' })
  })
})

describe('diffEsd', () => {
  const line = (net: string, pin?: number): TransientLine => ({
    net, connector: { ref: 'J1', pad: net === 'A' ? '1' : '2' }, x: 0, y: 0, clamp: null,
    series_resistor: null, through: null, ic: null,
    geometry: {
      routed: true, trunk: { length_mm: 1, td_ps: 1, z0_ohm: 50, vias: 0 }, clamp_stub: null,
      ic_stub: null, lead: null, clamp_ground_mm: 0, clamp_ground_nh: 0, via_nh: 0,
    },
    notes: [],
    variants: [{
      id: 'as_laid_out', label: '', worst_polarity: 'positive', v_pin_peak_v: pin,
      v_connector_peak_v: 9000, unclamped: false,
    }],
  })
  const doc = (lines: TransientLine[]) => ({ lines } as TransientDoc)

  it('pairs lines by connector pin and net, and reads the pin peak, else the connector', () => {
    const out = diffEsd(doc([line('A', 40), line('B')]), doc([line('A', 12)]))
    expect(out.map((c) => [c.connector, c.before?.volts, c.after?.volts]))
      .toEqual([['J1.1', 40, 12], ['J1.2', 9000, undefined]])
    expect(out[1].before!.where).toBe('connector')
  })
})

describe('resample', () => {
  it('interpolates linearly and holds the ends', () => {
    expect(resample([0, 1, 2], [0, 10, 20], [-1, 0.5, 1.5, 3])).toEqual([0, 5, 15, 20])
  })
})
