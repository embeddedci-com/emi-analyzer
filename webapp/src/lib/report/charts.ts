/**
 * The report's charts, as SVG text.
 *
 * The same axes and scaling as CableBudgetChart, TransientChart, DecouplingChart and
 * ConductedChart, drawn as strings because
 * the report is one static file: no React, no CSS variables, fixed colors that print. Every
 * label is a number formatted here, so nothing user-controlled reaches these strings.
 */

import type { CableBudgetPoint } from '../cableTypes'
import { xOf } from '../conducted'
import { bandEnd, branchMag, curve, fmtHz as decFmtHz, gapsOf, type DecBranch } from '../decoupling'
import { limitLine } from '../limits'
import { fmtOhm } from '../../components/ComponentImpedancePreview'

export const CHART_COLORS = {
  budget: '#1c7ed6',
  peak: '#e8590c',
  grid: '#dee2e6',
  axis: '#6c757d',
  impedance: '#1971c2',
  part: '#adb5bd',
  target: '#e8590c',
  gap: '#fa5252',
  over: '#e03131',
  under: '#1c7ed6',
  limit: '#495057',
  as_laid_out: '#e8590c',
  clamp_at_connector: '#0ca678',
  ideal_ground: '#4dabf7',
  reference_clamp: '#0ca678',
} as const

const f1 = (v: number) => (Number.isFinite(v) ? v.toFixed(1) : '0')

export const fmtHz = (f: number) =>
  f >= 1e9 ? `${(f / 1e9).toFixed(1)} GHz` : `${f / 1e6 >= 10 ? (f / 1e6).toFixed(0) : (f / 1e6).toFixed(1)} MHz`

/** A cable's allowed common-mode current against frequency, with its radiation peaks. */
export function cableBudgetSvg(points: CableBudgetPoint[], peaks: number[] = []): string {
  const W = 520
  const H = 220
  const PAD = { left: 40, right: 14, top: 22, bottom: 30 }
  const pts = points.filter((p) => Number.isFinite(p.max_current_dbua) && p.frequency_hz > 0)
  if (pts.length < 2) return ''
  const fLo = pts[0].frequency_hz
  const fHi = pts[pts.length - 1].frequency_hz
  if (!(fHi > fLo)) return ''
  const values = pts.map((p) => p.max_current_dbua)
  const top = Math.ceil(Math.max(...values) / 10) * 10
  const bottom = Math.floor(Math.min(...values) / 10) * 10 - 5
  const x = (f: number) =>
    PAD.left + ((Math.log10(f) - Math.log10(fLo)) / (Math.log10(fHi) - Math.log10(fLo))) * (W - PAD.left - PAD.right)
  const y = (v: number) => PAD.top + ((top - v) / (top - bottom)) * (H - PAD.top - PAD.bottom)

  const parts: string[] = []
  for (let v = bottom + 5; v <= top; v += 10) {
    parts.push(
      `<line x1="${PAD.left}" x2="${W - PAD.right}" y1="${f1(y(v))}" y2="${f1(y(v))}" stroke="${CHART_COLORS.grid}"/>`,
      `<text x="${PAD.left - 5}" y="${f1(y(v) + 3)}" text-anchor="end" font-size="10" fill="${CHART_COLORS.axis}">${v}</text>`,
    )
  }
  for (let d = Math.ceil(Math.log10(fLo)); d <= Math.floor(Math.log10(fHi)); d++) {
    const f = 10 ** d
    parts.push(
      `<line x1="${f1(x(f))}" x2="${f1(x(f))}" y1="${PAD.top}" y2="${H - PAD.bottom}" stroke="${CHART_COLORS.grid}"/>`,
      `<text x="${f1(x(f))}" y="${H - PAD.bottom + 13}" text-anchor="middle" font-size="10" fill="${CHART_COLORS.axis}">${fmtHz(f)}</text>`,
    )
  }
  for (const f of peaks) {
    if (!(f >= fLo && f <= fHi)) continue
    parts.push(
      `<line x1="${f1(x(f))}" x2="${f1(x(f))}" y1="${PAD.top}" y2="${H - PAD.bottom}" stroke="${CHART_COLORS.peak}" stroke-dasharray="3 3"/>`,
    )
  }
  const d = pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${f1(x(p.frequency_hz))},${f1(y(p.max_current_dbua))}`)
    .join(' ')
  parts.push(`<path d="${d}" fill="none" stroke="${CHART_COLORS.budget}" stroke-width="1.8"/>`)
  parts.push(`<text x="4" y="12" font-size="10" fill="${CHART_COLORS.axis}">dBuA</text>`)

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Allowed common-mode current against frequency">${parts.join('')}</svg>`
}

/**
 * One IC's supply |Z| against frequency, as DecouplingChart draws it: each part faint, the
 * total, the target, the ranges above it shaded, and the anti-resonances in the band.
 */
export function decouplingSvg(
  freqs: number[], branches: DecBranch[], seriesLH: number, target: number, bandHz: number,
  antiResonances: { hz: number; ohm: number }[] = [],
): string {
  const W = 560
  const H = 240
  const PAD = { left: 48, right: 22, top: 22, bottom: 30 }
  if (freqs.length < 2 || branches.length === 0 || !(target > 0) || !Number.isFinite(target)) return ''
  const total = curve(branches, seriesLH, freqs)
  const finite = total.filter(Number.isFinite)
  if (finite.length < 2) return ''
  const fLo = freqs[0]
  const fHi = freqs[freqs.length - 1]
  const zMax = Math.max(...finite, target) * 2
  const zMin = Math.max(Math.min(...finite, target) / 3, zMax / 1e6)
  const top = Math.ceil(Math.log10(zMax))
  const bottom = Math.floor(Math.log10(zMin))
  const x = (f: number) =>
    PAD.left + ((Math.log10(f) - Math.log10(fLo)) / (Math.log10(fHi) - Math.log10(fLo))) * (W - PAD.left - PAD.right)
  const y = (z: number) => {
    const l = Math.max(bottom, Math.min(top, Math.log10(Math.max(z, 1e-12))))
    return PAD.top + ((top - l) / (top - bottom)) * (H - PAD.top - PAD.bottom)
  }
  const path = (z: number[]) => z
    .map((v, i) => `${i === 0 ? 'M' : 'L'}${f1(x(freqs[i]))},${f1(y(v))}`)
    .join(' ')
  const plotBottom = H - PAD.bottom
  const bandX = x(freqs[bandEnd(freqs, bandHz) - 1])

  const parts: string[] = []
  // Above the band the package decouples, not the board: shown, not judged.
  parts.push(`<rect x="${f1(bandX)}" y="${PAD.top}" width="${f1(W - PAD.right - bandX)}" height="${plotBottom - PAD.top}" fill="#f1f3f5"/>`)
  if (bandX < W - PAD.right - 70) {
    parts.push(`<text x="${W - PAD.right - 4}" y="${PAD.top + 10}" text-anchor="end" font-size="10" fill="${CHART_COLORS.axis}">package range</text>`)
  }
  for (const [lo, hi] of gapsOf(freqs, total, target, bandHz)) {
    parts.push(`<rect x="${f1(x(lo))}" y="${PAD.top}" width="${f1(Math.max(x(hi) - x(lo), 2))}" height="${plotBottom - PAD.top}" fill="${CHART_COLORS.gap}" fill-opacity="0.15"/>`)
  }
  for (let d = bottom; d <= top; d++) {
    const z = 10 ** d
    parts.push(
      `<line x1="${PAD.left}" x2="${W - PAD.right}" y1="${f1(y(z))}" y2="${f1(y(z))}" stroke="${CHART_COLORS.grid}"/>`,
      `<text x="${PAD.left - 5}" y="${f1(y(z) + 3)}" text-anchor="end" font-size="10" fill="${CHART_COLORS.axis}">${fmtOhm(z)}</text>`,
    )
  }
  for (let d = Math.ceil(Math.log10(fLo)); d <= Math.floor(Math.log10(fHi)); d++) {
    const f = 10 ** d
    parts.push(
      `<line x1="${f1(x(f))}" x2="${f1(x(f))}" y1="${PAD.top}" y2="${plotBottom}" stroke="${CHART_COLORS.grid}"/>`,
      `<text x="${f1(x(f))}" y="${plotBottom + 13}" text-anchor="middle" font-size="10" fill="${CHART_COLORS.axis}">${decFmtHz(f)}</text>`,
    )
  }
  for (const b of branches) {
    parts.push(`<path d="${path(freqs.map((f) => branchMag(b, f)))}" fill="none" stroke="${CHART_COLORS.part}" stroke-width="0.8"/>`)
  }
  parts.push(
    `<line x1="${PAD.left}" x2="${f1(bandX)}" y1="${f1(y(target))}" y2="${f1(y(target))}" stroke="${CHART_COLORS.target}" stroke-width="1.4" stroke-dasharray="5 3"/>`,
    `<path d="${path(total)}" fill="none" stroke="${CHART_COLORS.impedance}" stroke-width="2"/>`,
  )
  for (const a of antiResonances) {
    if (!(a.hz >= fLo && a.hz <= fHi)) continue
    parts.push(`<circle cx="${f1(x(a.hz))}" cy="${f1(y(a.ohm))}" r="3.5" fill="${CHART_COLORS.over}" stroke="#fff" stroke-width="1.5"/>`)
  }
  parts.push(`<text x="4" y="11" font-size="10" fill="${CHART_COLORS.axis}">|Z|</text>`)
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Supply impedance against frequency, with the target">${parts.join('')}</svg>`
}

/**
 * The conducted spectrum as ConductedChart draws it: each harmonic at the LISN as a stem, red
 * where it is over the average limit, with the quasi-peak (dashed) and average limits.
 */
export function conductedSvg(
  lines: { frequencyHz: number; dbuv: number; marginAvgDb: number }[], quasiPeak: string, average: string,
): string {
  const W = 560
  const H = 240
  const PAD = { l: 40, r: 22, t: 22, b: 26 }
  if (lines.length === 0) return ''
  let qp: { frequency_hz: number; level_db: number }[]
  let avg: { frequency_hz: number; level_db: number }[]
  try {
    qp = limitLine(quasiPeak)
    avg = limitLine(average)
  } catch {
    return ''
  }
  const levels = lines.map((l) => l.dbuv).filter(Number.isFinite)
  const limitLevels = [...qp, ...avg].map((p) => p.level_db)
  const top = Math.ceil((Math.max(...levels, ...limitLevels) + 5) / 10) * 10
  const lowestLimit = Math.min(...limitLevels)
  const bottom = Math.floor(Math.max(Math.min(...levels, lowestLimit - 10), lowestLimit - 40) / 10) * 10
  const x0 = PAD.l
  const x1 = W - PAD.r
  const sy = (db: number) => PAD.t + (1 - (Math.max(db, bottom) - bottom) / (top - bottom)) * (H - PAD.t - PAD.b)
  const path = (pts: { frequency_hz: number; level_db: number }[]) =>
    pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${f1(xOf(p.frequency_hz, x0, x1))},${f1(sy(p.level_db))}`).join(' ')

  const parts: string[] = []
  for (let v = bottom; v <= top; v += top - bottom > 60 ? 20 : 10) {
    parts.push(
      `<line x1="${x0}" x2="${x1}" y1="${f1(sy(v))}" y2="${f1(sy(v))}" stroke="${CHART_COLORS.grid}"/>`,
      `<text x="${x0 - 5}" y="${f1(sy(v) + 3)}" text-anchor="end" font-size="10" fill="${CHART_COLORS.axis}">${v}</text>`,
    )
  }
  for (const f of [150e3, 500e3, 1e6, 5e6, 10e6, 30e6]) {
    parts.push(`<text x="${f1(xOf(f, x0, x1))}" y="${H - 8}" text-anchor="middle" font-size="10" fill="${CHART_COLORS.axis}">${decFmtHz(f)}</text>`)
  }
  for (const l of lines) {
    if (!Number.isFinite(l.dbuv) || !(l.frequencyHz > 0)) continue
    const lx = f1(xOf(l.frequencyHz, x0, x1))
    parts.push(`<line x1="${lx}" x2="${lx}" y1="${f1(sy(bottom))}" y2="${f1(sy(l.dbuv))}" stroke="${l.marginAvgDb < 0 ? CHART_COLORS.over : CHART_COLORS.under}" stroke-width="1.4"/>`)
  }
  parts.push(
    `<path d="${path(qp)}" fill="none" stroke="${CHART_COLORS.limit}" stroke-width="1.3" stroke-dasharray="5 3"/>`,
    `<path d="${path(avg)}" fill="none" stroke="${CHART_COLORS.limit}" stroke-width="1.3"/>`,
    `<text x="4" y="10" font-size="10" fill="${CHART_COLORS.axis}">dBuV</text>`,
  )
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Conducted spectrum at the LISN against the limits">${parts.join('')}</svg>`
}

export interface WaveSeries {
  color: string
  values: number[]
}

/** Pin voltage against time for each variant of a line, over the first `windowNs`. */
export function transientSvg(t: number[], series: WaveSeries[], windowNs = 10): string {
  const W = 520
  const H = 200
  const PAD = { l: 44, r: 12, t: 10, b: 26 }
  const idx = t.map((v, i) => [v, i] as const).filter(([v]) => Number.isFinite(v) && v <= windowNs).map(([, i]) => i)
  if (idx.length < 2 || series.length === 0) return ''
  let min = 0
  let max = 0
  for (const s of series) {
    for (const i of idx) {
      const v = s.values[i]
      if (!Number.isFinite(v)) continue
      if (v < min) min = v
      if (v > max) max = v
    }
  }
  if (max - min < 1e-9) max = min + 1
  const span = max - min
  min -= span * 0.05
  max += span * 0.05
  const sx = (v: number) => PAD.l + (v / windowNs) * (W - PAD.l - PAD.r)
  const sy = (v: number) => PAD.t + (1 - (v - min) / (max - min)) * (H - PAD.t - PAD.b)
  const tick = (v: number) => (Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(1))

  const parts: string[] = []
  for (const v of [max, (max + min) / 2, min]) {
    parts.push(
      `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${f1(sy(v))}" y2="${f1(sy(v))}" stroke="${CHART_COLORS.grid}"/>`,
      `<text x="${PAD.l - 5}" y="${f1(sy(v) + 3)}" text-anchor="end" font-size="10" fill="${CHART_COLORS.axis}">${tick(v)}</text>`,
    )
  }
  for (const v of [0, windowNs / 2, windowNs]) {
    parts.push(
      `<text x="${f1(sx(v))}" y="${H - PAD.b + 14}" text-anchor="middle" font-size="10" fill="${CHART_COLORS.axis}">${tick(v)} ns</text>`,
    )
  }
  for (const s of series) {
    const d = idx
      .filter((i) => Number.isFinite(s.values[i]))
      .map((i, k) => `${k === 0 ? 'M' : 'L'}${f1(sx(t[i]))},${f1(sy(s.values[i]))}`)
      .join(' ')
    if (d) parts.push(`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.6"/>`)
  }
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Pin voltage against time">${parts.join('')}</svg>`
}
