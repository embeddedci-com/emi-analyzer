/**
 * The net list as a CSV, for people who want to look at the numbers themselves — filter the
 * DDR nets, sort by skew, compare two lanes side by side.
 *
 * The worker produces structured rows (nets.json); this file turns them into text. The
 * formatting lives here rather than in the worker because the one thing that decides whether
 * the file opens correctly is the reader's spreadsheet locale, and only the browser knows it:
 *
 *   - Excel in a comma-decimal locale (Belgium, Germany, France…) splits CSV on semicolons and
 *     reads "12.34" as text or, worse, "1.500" as fifteen hundred. A comma-separated,
 *     dot-decimal file opens there as one column of garbage, or as silently wrong numbers.
 *   - Net names like "+3V3" and "-12V" start with a character Excel reads as a formula, and a
 *     board file is somebody else's upload — so a cell beginning "=HYPERLINK(" is an injection,
 *     not a name. Such cells get a leading apostrophe in the spreadsheet format.
 */

import type { BoardDoc, NetReport, NetRow } from './boardTypes'

export interface CsvFormat {
  delimiter: ',' | ';'
  decimal: '.' | ','
  /** Prefix text cells a spreadsheet would evaluate as a formula. */
  guardFormulas: boolean
  /** Byte-order mark, so Excel reads UTF-8 instead of guessing a legacy code page. */
  bom: boolean
}

/** RFC 4180 as written: for scripts and anything that is not a spreadsheet. */
export const PLAIN_CSV: CsvFormat = { delimiter: ',', decimal: '.', guardFormulas: false, bom: false }

/** Matched to how this computer's spreadsheet will read the file. */
export function spreadsheetFormat(locale?: string): CsvFormat {
  const decimal = (1.5).toLocaleString(locale).includes(',') ? ',' : '.'
  return { delimiter: decimal === ',' ? ';' : ',', decimal, guardFormulas: true, bom: true }
}

interface Column {
  header: string
  key: keyof NetRow
  kind: 'text' | 'number'
  decimals?: number
}

const COLUMNS: Column[] = [
  { header: 'Net', key: 'net', kind: 'text' },
  { header: 'Netclass', key: 'netclass', kind: 'text' },
  { header: 'Kind', key: 'kind', kind: 'text' },
  { header: 'Topology', key: 'topology', kind: 'text' },
  { header: 'Pads', key: 'pads', kind: 'number', decimals: 0 },
  { header: 'Unreachable pads', key: 'unreachable_pads', kind: 'number', decimals: 0 },
  // Two lengths on purpose. Copper is all the copper on the net; path is the longest
  // pad-to-pad route, which is what a signal travels. They agree on a point-to-point net
  // and differ on a branched one — and length matching is about the second.
  { header: 'Copper (mm)', key: 'copper_mm', kind: 'number', decimals: 3 },
  { header: 'Path (mm)', key: 'path_mm', kind: 'number', decimals: 3 },
  { header: 'Path from', key: 'path_from', kind: 'text' },
  { header: 'Path to', key: 'path_to', kind: 'text' },
  { header: 'Path by layer (mm)', key: 'path_by_layer', kind: 'text' },
  { header: 'Vias on path', key: 'path_vias', kind: 'number', decimals: 0 },
  { header: 'Vias total', key: 'vias_total', kind: 'number', decimals: 0 },
  { header: 'Delay (ps)', key: 'delay_ps', kind: 'number', decimals: 2 },
  { header: 'Min width (mm)', key: 'width_min_mm', kind: 'number', decimals: 3 },
  { header: 'Max width (mm)', key: 'width_max_mm', kind: 'number', decimals: 3 },
  { header: 'Diff pair partner', key: 'diff_pair_partner', kind: 'text' },
  { header: 'Match group', key: 'match_group', kind: 'text' },
  { header: 'Match kind', key: 'match_kind', kind: 'text' },
  { header: 'Match reference', key: 'match_reference', kind: 'text' },
  { header: 'Reference delay (ps)', key: 'reference_delay_ps', kind: 'number', decimals: 2 },
  { header: 'Skew vs reference (ps)', key: 'skew_ps', kind: 'number', decimals: 2 },
  { header: 'Skew vs reference (mm)', key: 'skew_mm', kind: 'number', decimals: 3 },
  { header: 'Tolerance (ps)', key: 'tolerance_ps', kind: 'number', decimals: 1 },
  { header: 'Within tolerance', key: 'within_tolerance', kind: 'text' },
]

/**
 * The columns for a report. The clock comparison is only added when a DDR clock was
 * identified, and its header names that clock — a column called "vs clock" leaves the reader
 * guessing which net it was measured against.
 */
export function columnsFor(report: NetReport): Column[] {
  if (!report.clock_net) return COLUMNS
  return [
    ...COLUMNS,
    { header: `Delay vs ${report.clock_net} (ps)`, key: 'vs_clock_ps', kind: 'number', decimals: 2 },
    { header: `Delay vs ${report.clock_net} (mm)`, key: 'vs_clock_mm', kind: 'number', decimals: 3 },
  ]
}

const FORMULA_START = /^[=+\-@\t\r]/

function text(value: string, format: CsvFormat): string {
  let v = value
  if (format.guardFormulas && FORMULA_START.test(v)) v = `'${v}`
  const needsQuotes =
    v.includes(format.delimiter) || v.includes('"') || /[\r\n]/.test(v) || v !== v.trim()
  return needsQuotes ? `"${v.replace(/"/g, '""')}"` : v
}

function number(value: number, decimals: number, format: CsvFormat): string {
  // Fixed decimals and no grouping separators, whatever the locale: "1.234,50" is two
  // numbers to half the spreadsheets that will open this.
  const s = value.toFixed(decimals)
  return format.decimal === ',' ? s.replace('.', ',') : s
}

function cell(row: NetRow, col: Column, format: CsvFormat): string {
  const v = row[col.key]
  if (v === null || v === undefined || v === '') return ''
  if (col.kind === 'number' && typeof v === 'number' && Number.isFinite(v)) {
    return number(v, col.decimals ?? 3, format)
  }
  return text(String(v), format)
}

export function buildNetsCsv(report: NetReport, format: CsvFormat): string {
  const cols = columnsFor(report)
  const lines = [
    cols.map((c) => text(c.header, format)).join(format.delimiter),
    ...report.rows.map((row) => cols.map((c) => cell(row, c, format)).join(format.delimiter)),
  ]
  return (format.bom ? '﻿' : '') + lines.join('\r\n') + '\r\n'
}

/**
 * A report from board.json alone, for boards analysed before nets.json existed.
 *
 * Copper length, vias and the net names are all there; paths, delays and skew are not, so
 * those columns are left empty rather than filled with something that looks like a value.
 */
export function reportFromBoard(doc: BoardDoc): NetReport {
  return {
    format_version: 0,
    clock_net: '',
    epsilon_assumed: true,
    partial: true,
    rows: doc.nets
      .filter((n) => n.name)
      .map((n) => ({
        net: n.name,
        netclass: '',
        kind: '',
        topology: '',
        pads: null,
        unreachable_pads: null,
        copper_mm: n.length_mm,
        path_mm: null,
        path_from: '',
        path_to: '',
        path_by_layer: '',
        path_vias: null,
        vias_total: n.vias,
        delay_ps: null,
        width_min_mm: null,
        width_max_mm: null,
        diff_pair_partner: '',
        match_group: '',
        match_kind: '',
        match_reference: '',
        reference_delay_ps: null,
        skew_ps: null,
        skew_mm: null,
        tolerance_ps: null,
        within_tolerance: '',
        clock_net: '',
        vs_clock_ps: null,
        vs_clock_mm: null,
      })),
  }
}

/** A file name that survives every operating system a download might land on. */
export function csvFileName(projectName: string): string {
  const slug = projectName.trim().replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || 'board'
  return `${slug}-nets.csv`
}

/** Hand the text to the browser as a download. Not exercised by the tests: it is all DOM. */
export function downloadText(filename: string, content: string): void {
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
