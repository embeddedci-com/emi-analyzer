/**
 * The report's charts, as SVG text.
 *
 * The same axes and scaling as CableBudgetChart and TransientChart, drawn as strings because
 * the report is one static file: no React, no CSS variables, fixed colors that print. Every
 * label is a number formatted here, so nothing user-controlled reaches these strings.
 */

import type { CableBudgetPoint } from '../cableTypes'

export const CHART_COLORS = {
  budget: '#1c7ed6',
  peak: '#e8590c',
  grid: '#dee2e6',
  axis: '#6c757d',
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
