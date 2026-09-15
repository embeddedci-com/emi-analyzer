/**
 * The CSV export. Mostly about opening correctly in somebody's spreadsheet, because a file
 * that opens as one column — or with 1.500 read as fifteen hundred — is worse than no file.
 */

import { describe, expect, it } from 'vitest'
import type { BoardDoc, NetReport, NetRow } from './boardTypes'
import {
  buildNetsCsv, columnsFor, csvFileName, PLAIN_CSV, reportFromBoard, spreadsheetFormat,
} from './netsCsv'

function row(over: Partial<NetRow>): NetRow {
  return {
    net: 'SIG', netclass: '', kind: 'signal', topology: 'point-to-point', pads: 2,
    unreachable_pads: 0, copper_mm: 10, path_mm: 10, path_from: 'U1.1', path_to: 'U2.1',
    path_by_layer: 'F.Cu:10.00', path_vias: 0, vias_total: 0, delay_ps: 56.4,
    width_min_mm: 0.2, width_max_mm: 0.2, diff_pair_partner: '', match_group: '',
    match_kind: '', match_reference: '', reference_delay_ps: null, skew_ps: null, skew_mm: null,
    tolerance_ps: null, within_tolerance: '', clock_net: '', vs_clock_ps: null, vs_clock_mm: null,
    ...over,
  }
}

const report = (rows: NetRow[], clock = ''): NetReport =>
  ({ format_version: 1, clock_net: clock, epsilon_assumed: false, rows })

const lines = (csv: string) => csv.replace(/^﻿/, '').trimEnd().split('\r\n')

describe('plain CSV', () => {
  it('is comma-separated with dot decimals and no BOM', () => {
    const csv = buildNetsCsv(report([row({ copper_mm: 1234.5 })]), PLAIN_CSV)
    expect(csv.startsWith('﻿')).toBe(false)
    const [header, first] = lines(csv)
    expect(header.split(',')[0]).toBe('Net')
    expect(first).toContain(',1234.500,')
  })

  it('quotes values that contain the delimiter or a quote', () => {
    const csv = buildNetsCsv(report([row({ net: 'A,B', netclass: 'say "hi"' })]), PLAIN_CSV)
    expect(lines(csv)[1].startsWith('"A,B","say ""hi"""')).toBe(true)
  })

  it('leaves unknown values empty rather than writing null or NaN', () => {
    const csv = buildNetsCsv(report([row({ path_mm: null, delay_ps: null })]), PLAIN_CSV)
    expect(csv).not.toMatch(/null|NaN|undefined/)
  })
})

describe('spreadsheet format', () => {
  it('uses semicolons and comma decimals where the locale does', () => {
    const f = spreadsheetFormat('nl-BE')
    expect(f).toMatchObject({ delimiter: ';', decimal: ',', bom: true })

    const [, first] = lines(buildNetsCsv(report([row({ copper_mm: 1234.5, skew_ps: -20.17 })]), f))
    const cells = first.split(';')
    // No grouping separator, or "1.234,500" is two numbers to half the readers.
    expect(cells).toContain('1234,500')
    expect(cells).toContain('-20,17')
  })

  it('keeps commas and dots in an English locale', () => {
    expect(spreadsheetFormat('en-US')).toMatchObject({ delimiter: ',', decimal: '.' })
  })

  it('stops net names being read as formulas', () => {
    const f = spreadsheetFormat('nl-BE')
    const csv = buildNetsCsv(report([
      row({ net: '+3V3' }),
      row({ net: '-12V' }),
      row({ net: '=HYPERLINK("http://example.invalid")' }),
    ]), f)
    const nets = lines(csv).slice(1).map((l) => l.split(';')[0])
    expect(nets[0]).toBe("'+3V3")
    expect(nets[1]).toBe("'-12V")
    expect(nets[2].startsWith(`"'=HYPERLINK`)).toBe(true)
  })

  it('does not guard numbers, only text', () => {
    const f = spreadsheetFormat('en-US')
    const [, first] = lines(buildNetsCsv(report([row({ skew_ps: -3.5 })]), f))
    expect(first.split(',')).toContain('-3.50')
  })
})

describe('clock columns', () => {
  it('appear only when a DDR clock was identified, named after it', () => {
    expect(columnsFor(report([])).some((c) => c.key === 'vs_clock_ps')).toBe(false)

    const cols = columnsFor(report([], 'DDR_CK_P'))
    expect(cols.map((c) => c.header)).toContain('Delay vs DDR_CK_P (ps)')
  })
})

describe('older boards', () => {
  it('fall back to copper length and leave path, delay and skew empty', () => {
    const doc = { nets: [{ name: 'GND', length_mm: 12.5, vias: 3 }, { name: '', length_mm: 0, vias: 0 }] } as unknown as BoardDoc
    const r = reportFromBoard(doc)
    expect(r.partial).toBe(true)
    expect(r.rows).toHaveLength(1)
    expect(r.rows[0]).toMatchObject({ net: 'GND', copper_mm: 12.5, vias_total: 3, path_mm: null, skew_ps: null })
  })
})

describe('file name', () => {
  it('is safe on every filesystem', () => {
    expect(csvFileName('vbench pod / rev B')).toBe('vbench-pod-rev-B-nets.csv')
    expect(csvFileName('   ')).toBe('board-nets.csv')
  })
})
