import { describe, expect, it } from 'vitest'
import type { BoardDoc, RuleFinding, RulesDoc } from '../boardTypes'
import type { CablesDoc } from '../cableTypes'
import type { Run } from '../emiApi'
import type { TransientDoc } from '../transientTypes'
import { cableBudgetSvg, transientSvg } from './charts'
import { esc, renderReportHtml } from './html'
import {
  DISCLAIMER, SECTIONS, assembleReport, changedSettings, groupFindings, reportFileName, reportJson,
  safeImage, type ReportInput, type SectionId,
} from './model'

const EVIL = '<script>alert(1)</script>'
const EVIL_ATTR = '"><img src=x onerror=alert(1)>'

const run = (kind: Run['kind'], summary: Record<string, unknown> = {}): Run => ({
  id: `${kind}-1`, project_id: 'p', board_id: 'b2', kind, status: 'done', summary,
  created_at: '2026-09-20T10:00:00Z', updated_at: '2026-09-20T10:01:00Z', finished_at: '2026-09-20T10:01:00Z',
})

const board: BoardDoc = {
  format_version: 1, source: {}, units: 'mm',
  coordinate_system: { x: 'right', y: 'up', z: 'up', origin: 'bottom-left' },
  board: { width_mm: 50, height_mm: 30, thickness_mm: 1.6, outline: [[[0, 0], [50, 0], [50, 30], [0, 30], [0, 0]]] },
  layers: [{ name: 'F.Cu', kind: 'signal', index: 0, z_mm: 1.6 }, { name: 'B.Cu', kind: 'signal', index: 1, z_mm: 0 }],
  stackup: [
    { name: 'F.Cu', role: 'copper', type: 'copper', thickness_mm: 0.035, material: 'copper', epsilon_r: null, loss_tangent: null, from_file: true, z_bottom_mm: 1.565, z_top_mm: 1.6 },
    { name: `core ${EVIL}`, role: 'dielectric', type: 'core', thickness_mm: 1.51, material: 'FR4', epsilon_r: 4.5, loss_tangent: 0.02, from_file: false, z_bottom_mm: 0.035, z_top_mm: 1.565 },
  ],
  nets: [], vias: [], pads: [],
  geometry: { file: 'geometry.bin', dtype: 'float32', components: 2, primitive: 'triangles', vertex_count: 0, byte_length: 0, groups: [] },
  kicad: { version: 20240108, generator: 'pcbnew' },
  warnings: [`Parser guessed ${EVIL}`],
}

const finding = (over: Partial<RuleFinding>): RuleFinding => ({
  id: 'f', rule: 'plane-gap', severity: 'warning', title: 'A gap', detail: 'Detail', ...over,
})

const rules: RulesDoc = {
  format_version: 1,
  notes: [`Rules read from ${EVIL_ATTR}.`, 'Zones are not filled, so plane checks skip F.Cu.'],
  findings: [
    finding({ id: 'a', rule: 'plane-gap', severity: 'critical', title: `Net ${EVIL} crosses a gap`, net: EVIL_ATTR, x: 10, y: 5, layer: 'F.Cu' }),
    finding({ id: 'b', rule: 'radiator', severity: 'warning', title: 'Long net', net: 'CLK', x: 20, y: 25 }),
    finding({ id: 'c', rule: 'radiator', severity: 'info', title: 'Summary', x: null, y: null }),
  ],
  summary: { critical: 1, warning: 1, info: 1, assumed_max_frequency_hz: 1e9 },
}

const cables: CablesDoc = {
  format: 'emi-cables', format_version: 1, solver: 'nec2c', standard_id: 'fcc-15b-radiated-3m',
  assumptions: ['Cable over a ground plane.'],
  cables: [{
    ref: `J1${EVIL}`, cable_id: 'usb2', cable_name: 'USB 2.0', length_m: 1, declared_none: false,
    radiation_peaks_hz: [1e8], tightest: { frequency_hz: 1e8, max_current_dbua: 12.3, limit_dbuv_per_m: 43.5 },
    points: [
      { frequency_hz: 3e7, limit_dbuv_per_m: 40, max_current_dbua: 30, e_per_amp: 1, radiation_peak: false },
      { frequency_hz: 1e8, limit_dbuv_per_m: 43.5, max_current_dbua: 12.3, e_per_amp: 1, radiation_peak: true },
      { frequency_hz: 1e9, limit_dbuv_per_m: 54, max_current_dbua: 25, e_per_amp: 1, radiation_peak: false },
    ],
  }],
  unassigned: [{ ref: 'J2', footprint: EVIL, suggested: null, reason: 'nothing declared' }],
  notes: [],
}

const esd: TransientDoc = {
  format_version: 1, standard: 'IEC 61000-4-2', discharge: 'contact', level: 4, kv: 8, polarities: ['positive', 'negative'],
  source_check: [], reference_clamp: { part: 'TPD1E10B06', model: 'table', assumptions: [] },
  lines: [{
    net: `USB_D+${EVIL}`, connector: { ref: 'J1', pad: '3' }, x: 1, y: 2,
    clamp: { ref: 'D1', pad: '1', part: 'ESD9', model: 'illustrative', source: 'generic', assumptions: [] },
    series_resistor: null, through: null, ic: null,
    geometry: {
      routed: true, trunk: { length_mm: 10, td_ps: 60, z0_ohm: 50, vias: 0 }, clamp_stub: null, ic_stub: null,
      lead: null, clamp_ground_mm: 1, clamp_ground_nh: 1, via_nh: 0.5,
    },
    notes: [],
    variants: [
      { id: 'as_laid_out', label: 'As laid out', worst_polarity: 'positive', v_pin_peak_v: 42.5, v_connector_peak_v: 300, unclamped: false, v_pin: [0, 40, 10] },
      { id: 'clamp_at_connector', label: 'Clamp at the connector', worst_polarity: 'positive', v_pin_peak_v: 21.2, v_connector_peak_v: 250, unclamped: false, v_pin: [0, 20, 5] },
    ],
    t_ns: [0, 1, 5],
  }],
  models: [], assumptions: ['Planes are ideal.'], notes: [],
}

const ALL: SectionId[] = SECTIONS.map((s) => s.id)

function input(over: Partial<ReportInput> = {}): ReportInput {
  return {
    projectName: `My board ${EVIL}`,
    version: { number: 2, count: 2, filename: `${EVIL_ATTR}.kicad_pcb`, uploadedAt: '2026-09-20T09:00:00Z', sha256: 'abc123' },
    ingest: run('ingest', { worker: 'local', worker_version: '0.3.0', input_sha256: 'abc123' }),
    board, rules,
    features: { full_wave: false },
    appVersion: '0.3.0',
    generatedAt: new Date('2026-09-25T12:00:00Z'),
    sections: ALL,
    cables: { run: run('cable'), doc: cables },
    esd: { run: run('transient'), doc: esd },
    ...over,
  }
}

describe('assembleReport', () => {
  it('includes the sections asked for that have data, in order', () => {
    const d = assembleReport(input())
    expect(d.sections).toEqual(['board', 'findings', 'notes', 'cables', 'esd'])
    expect(d.cables?.modelled[0].tightest?.maxCurrentDbua).toBe(12.3)
    expect(d.esd?.lines[0].peak).toEqual({ volts: 42.5, where: 'pin' })
    expect(d.esd?.lines[0].comparison).toEqual({ what: 'Clamp moved to the connector', volts: 21.2 })
  })

  it('leaves out a section without its run, and one not asked for', () => {
    const d = assembleReport(input({ cables: null, sections: ['findings', 'esd'] }))
    expect(d.sections).toEqual(['findings', 'esd'])
    expect(d.cables).toBeNull()
    expect(d.notes).toEqual([])
    expect(d.boardImage).toBeNull()
  })

  it('never includes experimental results while their feature is off', () => {
    const solve = run('solve')
    const off = assembleReport(input({ experimental: { solves: [solve] } }))
    expect(off.sections).not.toContain('experimental')
    expect(off.experimental).toBeNull()
    const on = assembleReport(input({ features: { full_wave: true }, experimental: { solves: [solve] } }))
    expect(on.sections).toContain('experimental')
    const html = renderReportHtml(on, board)
    expect(html).toContain('<strong>Experimental.</strong>')
    expect(html).toContain('Full-wave solve')
  })

  it('records the versions, the hash and the features', () => {
    const d = assembleReport(input())
    expect(d.meta).toMatchObject({
      appVersion: '0.3.0', workerVersion: '0.3.0', worker: 'local', sha256: 'abc123',
      features: { fullWave: false, smallPartSolve: false }, rulesFile: EVIL_ATTR,
    })
    const bare = assembleReport(input({ appVersion: '', ingest: run('ingest') }))
    expect(bare.meta.appVersion).toBe('not recorded')
    expect(bare.meta.workerVersion).toBe('not recorded')
  })

  it('numbers the located findings, worst first, and only those', () => {
    const { groups, markers } = groupFindings(rules.findings)
    expect(groups.map((g) => g.severity)).toEqual(['critical', 'warning', 'info'])
    expect(markers.map((m) => m.n)).toEqual([1, 2])
    expect(groups[2].rules[0].findings[0].marker).toBeNull()
  })

  it('lists settings that are not the defaults', () => {
    const withSettings: RulesDoc = {
      ...rules,
      settings: {
        file: 'emi.rules.yaml', file_error: '',
        base: { board: {}, rules: {}, groups: [], suppress: [], cables: {}, cable_solver: '' },
        applied: {
          board: { max_frequency_hz: { value: 2e9, source: 'file' } },
          rules: {
            'via-stub': { enabled: false, enabled_source: 'run', severity: '', severity_source: 'default', params: {} },
            'plane-gap': { enabled: true, enabled_source: 'default', severity: '', severity_source: 'default', params: { min_crossing_mm: { value: 0.6, source: 'default' } } },
          },
          groups: [], suppress: [], cables: {}, cable_solver: '',
        },
      },
    }
    expect(changedSettings(withSettings)).toEqual([
      { what: 'Board: max_frequency_hz', value: '2000000000', source: 'file' },
      { what: 'Via stubs', value: 'off', source: 'run' },
    ])
  })

  it('compares with an earlier version', () => {
    const d = assembleReport(input({
      compare: {
        versionNumber: 1,
        rulesBefore: { ...rules, findings: [rules.findings[1], finding({ id: 'old', rule: 'via-stub', title: 'Stub', x: 3, y: 3 })] },
        cablesBefore: cables,
        esdBefore: { ...esd, lines: [{ ...esd.lines[0], variants: [{ ...esd.lines[0].variants![0], v_pin_peak_v: 60 }] }] },
      },
    }))
    expect(d.sections).toContain('changes')
    expect(d.changes?.findings?.counts).toEqual({ fixed: 1, new: 2, unchanged: 1 })
    expect(d.changes?.esd?.[0]).toMatchObject({ beforeV: 60, afterV: 42.5 })
    expect(d.changes?.nets).toBeNull()
    expect(renderReportHtml(d, board)).toContain('Changes since version 1')
  })
})

describe('renderReportHtml', () => {
  const html = renderReportHtml(
    assembleReport(input({ boardImage: { src: 'data:image/png;base64,iVBORw0KGgo=', widthPx: 10, heightPx: 6 } })),
    board,
    { omitted: [{ label: `Thing ${EVIL}`, reason: 'not run' }] },
  )

  it('escapes every piece of user-controlled text', () => {
    expect(html).not.toContain('<script')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('onerror=alert(1)>')
    expect(html).toContain(esc(`My board ${EVIL}`))
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('&quot;&gt;&lt;img src=x onerror=alert(1)&gt;')
  })

  it('loads nothing from anywhere', () => {
    expect(html).toContain(`content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"`)
    const refs = [...html.matchAll(/\b(?:src|href)="([^"]*)"/g)].map((m) => m[1])
    expect(refs.length).toBeGreaterThan(0)
    for (const r of refs) expect(r.startsWith('data:image/png;base64,')).toBe(true)
    expect(html).not.toMatch(/url\(|@import/)
  })

  it('states the disclaimer on the title page and on every printed page', () => {
    expect(html).toContain(DISCLAIMER.title)
    expect(html).toContain(DISCLAIMER.text)
    // On every printed page, as a page margin box.
    expect(html).toMatch(/@bottom-left \{ content: "Compares versions of your own board\. Not a pre-compliance test\."/)
    expect(html.indexOf(DISCLAIMER.text)).toBeLessThan(html.indexOf('id="board"'))
    expect(html).toMatch(/@media print/)
  })

  it('draws the board with its markers, the charts and what was left out', () => {
    expect(html).toContain('<image href="data:image/png;base64,iVBORw0KGgo="')
    expect(html.match(/<circle /g)?.length).toBe(2)
    expect(html).toContain('Allowed common-mode current against frequency')
    expect(html).toContain('Pin voltage against time')
    expect(html).toContain('Not included: ')
    expect(html).toContain(esc(`Thing ${EVIL}`))
  })

  it('uses no em-dashes in its copy', () => {
    expect(html).not.toContain('—')
  })
})

describe('the JSON export', () => {
  it('carries the same data and the disclaimer, without the picture', () => {
    const d = assembleReport(input({ boardImage: { src: 'data:image/png;base64,AAAA', widthPx: 1, heightPx: 1 } }))
    const parsed = JSON.parse(reportJson(d))
    expect(parsed.format).toBe('emi-report')
    expect(parsed.disclaimer.text).toBe(DISCLAIMER.text)
    expect(parsed.boardImage).toBeUndefined()
    expect(parsed.findings[0].rules[0].findings[0].net).toBe(EVIL_ATTR)
  })
})

describe('helpers', () => {
  it('only lets a PNG data URL through as the board image', () => {
    expect(safeImage({ src: 'https://example.com/a.png', widthPx: 1, heightPx: 1 })).toBeNull()
    expect(safeImage({ src: 'data:image/svg+xml;base64,AAAA', widthPx: 1, heightPx: 1 })).toBeNull()
    expect(safeImage({ src: 'data:image/png;base64,AA"A', widthPx: 1, heightPx: 1 })).toBeNull()
    expect(safeImage({ src: 'data:image/png;base64,AAAA', widthPx: 1, heightPx: 1 })).not.toBeNull()
  })

  it('names files safely', () => {
    expect(reportFileName('My board / rev B', 3, 'html')).toBe('My-board-rev-B-v3-emi-report.html')
    expect(reportFileName('<<>>', 1, 'json')).toBe('board-v1-emi-report.json')
  })

  it('draws nothing from too few points', () => {
    expect(cableBudgetSvg([])).toBe('')
    expect(transientSvg([0], [{ color: '#000', values: [1] }])).toBe('')
  })
})
