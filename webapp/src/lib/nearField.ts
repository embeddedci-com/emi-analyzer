/**
 * Near-field probe scans, drawn on the board.
 *
 * Rule checks say where to look; a probe run over the real board says what is actually
 * radiating. This puts the second on top of the first, using the same overlay a solve
 * result uses, so a hotspot in the scan can be read against the copper under it.
 *
 * The file format is deliberately loose, because every scanner and every home-made rig
 * writes something slightly different: rows of x, y and a reading, comma, semicolon, tab or
 * whitespace separated, with or without a header, with decimal points or — as European tools
 * often write them — decimal commas. Columns are found by name when there is a header.
 *
 * Everything happens in the browser. The scan is not uploaded anywhere.
 */

import type { FieldOverlayData } from './overlay'

export interface ScanPoint {
  x: number
  y: number
  v: number
}

export interface ParsedScan {
  points: ScanPoint[]
  /** The reading column's header, e.g. "level_dBuV"; '' when the file had none. */
  valueLabel: string
  /** A dB column name, or any negative reading, suggests the values are already in dB. */
  looksLikeDb: boolean
  /** Rows that could not be read as three numbers. */
  skipped: number
}

const COMMENT = /^\s*(#|\/\/|%)/

export function parseScan(text: string): ParsedScan {
  const lines = text
    .replace(/^﻿/, '')
    .split(/\r?\n/)
    .filter((l) => l.trim() !== '' && !COMMENT.test(l))
  if (lines.length === 0) throw new Error('the file is empty')

  const head = lines[0]
  const delimiter = head.includes('\t') ? '\t' : head.includes(';') ? ';' : head.includes(',') ? ',' : ' '
  const tokens = (l: string) =>
    (delimiter === ' ' ? l.trim().split(/\s+/) : l.split(delimiter))
      .map((t) => t.trim().replace(/^"(.*)"$/, '$1'))
  // A comma is only a decimal separator when it is not the column separator.
  const num = (t: string) => (t === '' ? NaN : Number(delimiter === ',' ? t : t.replace(',', '.')))

  const first = tokens(head)
  const hasHeader = first.some((t) => Number.isNaN(num(t)))
  let xi = 0
  let yi = 1
  let vi = 2
  let valueLabel = ''
  if (hasHeader) {
    const low = first.map((t) => t.toLowerCase())
    const find = (re: RegExp, exclude: number[]) =>
      low.findIndex((t, i) => !exclude.includes(i) && re.test(t))
    const fx = find(/^(pos_?)?x(\b|_)/, [])
    if (fx >= 0) xi = fx
    const fy = find(/^(pos_?)?y(\b|_)/, [xi])
    if (fy >= 0) yi = fy
    const fv = find(/db|amp|level|field|value|mag|reading|^(v|e|h|z)(\b|_)/, [xi, yi])
    vi = fv >= 0 ? fv : ([0, 1, 2, 3].find((i) => i !== xi && i !== yi) ?? 2)
    valueLabel = first[vi] ?? ''
  }

  const points: ScanPoint[] = []
  let skipped = 0
  for (const line of lines.slice(hasHeader ? 1 : 0)) {
    const t = tokens(line)
    const x = num(t[xi] ?? '')
    const y = num(t[yi] ?? '')
    const v = num(t[vi] ?? '')
    if (Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(v)) points.push({ x, y, v })
    else skipped++
  }
  if (points.length < 4) {
    throw new Error(`only ${points.length} readable points; expected columns of x, y and a reading`)
  }
  return {
    points,
    valueLabel,
    looksLikeDb: /db/i.test(valueLabel) || points.some((p) => p.v < 0),
    skipped,
  }
}

export interface Placement {
  /** Added to scan coordinates to land them in board space (mm). */
  offsetX: number
  offsetY: number
  /** The scanner's Y axis points down, from the board's top edge. */
  flipY: boolean
  boardHeight: number
  /** Readings are already dB; otherwise they are linear and converted with 20·log10. */
  valuesAreDb: boolean
  /** How far below the peak the colour ramp reaches. */
  rangeDb: number
}

export interface ScanOverlay {
  overlay: FieldOverlayData
  width: number
  height: number
  stepX: number
  stepY: number
  /** Peak reading in dB (converted from linear when needed). */
  peak: number
  /** Where the peak is, in board space. */
  peakAt: { x: number; y: number }
}

const MAX_CELLS = 4_000_000

function axis(values: number[]) {
  const uniq = Array.from(new Set(values.map((v) => Math.round(v * 1e6) / 1e6))).sort((a, b) => a - b)
  const diffs = uniq.slice(1).map((v, i) => v - uniq[i]).filter((d) => d > 1e-9).sort((a, b) => a - b)
  // The median spacing, so one mis-stepped row does not change the pitch of the whole scan.
  const step = diffs.length ? diffs[Math.floor(diffs.length / 2)] : 1
  const min = uniq[0]
  const max = uniq[uniq.length - 1]
  return { min, max, step, count: Math.round((max - min) / step) + 1 }
}

export function scanToOverlay(scan: ParsedScan, p: Placement): ScanOverlay {
  const ax = axis(scan.points.map((q) => q.x))
  const ay = axis(scan.points.map((q) => q.y))
  if (ax.count * ay.count > MAX_CELLS) {
    throw new Error(`a ${ax.count}×${ay.count} grid is too large to draw; resample the scan`)
  }

  const toDb = (v: number) => (p.valuesAreDb ? v : 20 * Math.log10(Math.max(Math.abs(v), 1e-30)))
  const grid = new Float64Array(ax.count * ay.count).fill(NaN)
  let peak = -Infinity
  let peakAt = { x: 0, y: 0 }

  for (const q of scan.points) {
    const col = Math.round((q.x - ax.min) / ax.step)
    // Overlay row 0 is the lowest board Y. With a downward scanner axis that is the
    // scanner's largest y, so rows are written in reverse.
    let row = Math.round((q.y - ay.min) / ay.step)
    if (p.flipY) row = ay.count - 1 - row
    const i = row * ax.count + col
    const d = toDb(q.v)
    if (Number.isNaN(grid[i]) || d > grid[i]) grid[i] = d
    if (d > peak) {
      peak = d
      peakAt = { x: q.x + p.offsetX, y: (p.flipY ? p.boardHeight - q.y : q.y) + p.offsetY }
    }
  }

  const floorDb = -Math.abs(p.rangeDb)
  const values = new Float32Array(grid.length)
  for (let i = 0; i < grid.length; i++) {
    // Cells the probe never visited go below the floor, so the gate hides them rather than
    // the ramp painting them as quiet.
    values[i] = Number.isNaN(grid[i]) ? floorDb - 10 : grid[i] - peak
  }

  const yLo = p.flipY ? p.boardHeight - ay.max : ay.min
  const yHi = p.flipY ? p.boardHeight - ay.min : ay.max
  // Readings are cell centres; the extent covers the cells, or the drawing sits half a
  // pitch off from where the probe actually was.
  const extent: [number, number, number, number] = [
    ax.min - ax.step / 2 + p.offsetX,
    yLo - ay.step / 2 + p.offsetY,
    ax.max + ax.step / 2 + p.offsetX,
    yHi + ay.step / 2 + p.offsetY,
  ]

  return {
    overlay: { values, width: ax.count, height: ay.count, extent, floorDb },
    width: ax.count,
    height: ay.count,
    stepX: ax.step,
    stepY: ay.step,
    peak,
    peakAt,
  }
}
