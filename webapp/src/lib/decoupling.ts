/**
 * The decoupling view's arithmetic, in the browser (worker: rules/decoupling_view.py, pdn.py).
 *
 * The worker ships branches, not curves: every capacitor as a series R-L-C and the plane pair
 * as one more. Rebuilding |Z| here is a few thousand complex divisions, and it is what lets the
 * target be edited without another analysis: the gaps, the not-filtered harmonics and the
 * ranking of the recommendations all follow the target, and all are recomputed from the same
 * branches with the same formulas the worker used.
 */

export interface DecBranch {
  id: string
  c_f: number
  l_h: number
  r_ohm: number
  kind?: 'cap' | 'plane'
}

export type DecChange =
  | { op: 'replace'; branch: DecBranch }
  | { op: 'add'; branch: DecBranch }
  | { op: 'remove'; id: string }

export interface DecRecommendation {
  kind: 'move' | 'via' | 'add' | 'remove'
  text: string
  change: DecChange
  improvement_db: number
  at_hz: number
  worst_after_db: number
  fixes: [number, number][]
}

/** One capacitor as one IC sees it: the part is in the rail's `parts`, keyed by ref. */
export interface DecCap {
  ref: string
  pin: string
  mount_l_h: number
  l_h: number
  distance_mm: number
  useful_up_to_hz: number
  loop_mm: number
  height_mm: number
  ground_vias: number
  ground_via_mm: number
  spreading_nh: number
  connection_nh: number
}

export interface DecPart {
  value: string
  package: string
  c_f: number
  esl_h: number
  esr_ohm: number
  source: 'library' | 'assumed'
  model: string
  assumed: string[]
  x: number
  y: number
}

export interface DecIC {
  ref: string
  pins: string[]
  x: number
  y: number
  ripple_pct: number
  step_current_a: number
  target_ohm: number
  band_hz: number
  series_l_h: number
  plane_branch: DecBranch | null
  caps: DecCap[]
  assumed: string[]
  noise: { hz: number; source: string }[]
  recommendations: DecRecommendation[]
  status: 'ok' | 'gaps'
  gaps: [number, number][]
  anti_resonances: { hz: number; ohm: number }[]
  worst: { hz: number; ohm: number; excess_db: number } | null
}

export interface DecRail {
  net: string
  v: number
  v_assumed: boolean
  plane: {
    layer: string
    ground_layer: string
    cavity_mm: number
    area_mm2: number
    eps_r: number
    c_f: number
    resonance_hz: number
    assumed: boolean
  } | null
  status: 'ok' | 'gaps'
  parts: Record<string, DecPart>
  ics: DecIC[]
}

export interface DecouplingDoc {
  format_version: number
  f_min_hz: number
  f_max_hz: number
  points_per_decade: number
  stackup_assumed: boolean
  note: string
  rails: DecRail[]
}

/** The worker's grid: log-spaced, the same points, so a gap edge here is a gap edge there. */
export function frequencies(doc: Pick<DecouplingDoc, 'f_min_hz' | 'f_max_hz' | 'points_per_decade'>): number[] {
  const lo = Math.log10(doc.f_min_hz)
  const hi = Math.log10(doc.f_max_hz)
  const n = Math.round((hi - lo) * doc.points_per_decade) + 1
  return Array.from({ length: n }, (_, i) => 10 ** (lo + (i / (n - 1)) * (hi - lo)))
}

export function branchesOf(ic: DecIC, parts: Record<string, DecPart>): DecBranch[] {
  const out: DecBranch[] = ic.caps.map((c) => ({
    id: c.ref, c_f: parts[c.ref]?.c_f ?? 0, l_h: c.l_h, r_ohm: parts[c.ref]?.esr_ohm ?? 0, kind: 'cap',
  }))
  if (ic.plane_branch) out.push({ ...ic.plane_branch, kind: 'plane' })
  return out
}

export function applyChange(branches: DecBranch[], change: DecChange): DecBranch[] {
  if (change.op === 'remove') return branches.filter((b) => b.id !== change.id)
  if (change.op === 'add') return [...branches, change.branch]
  return branches.map((b) => (b.id === change.branch.id ? { ...b, ...change.branch } : b))
}

/** |Z| of one branch alone. */
export function branchMag(b: DecBranch, f: number): number {
  const w = 2 * Math.PI * f
  return Math.hypot(b.r_ohm, w * b.l_h - 1 / (w * b.c_f))
}

/** |Z| of the branches in parallel, behind a series inductance common to all of them. */
export function networkMag(branches: DecBranch[], seriesLH: number, f: number): number {
  if (!branches.length) return Infinity
  const w = 2 * Math.PI * f
  let yr = 0
  let yi = 0
  for (const b of branches) {
    const r = b.r_ohm
    const x = w * b.l_h - 1 / (w * b.c_f)
    const d = r * r + x * x
    yr += r / d
    yi += -x / d
  }
  const d = yr * yr + yi * yi
  const zr = yr / d
  const zi = -yi / d + w * seriesLH
  return Math.hypot(zr, zi)
}

export function curve(branches: DecBranch[], seriesLH: number, freqs: number[]): number[] {
  return freqs.map((f) => networkMag(branches, seriesLH, f))
}

export function targetOhm(volts: number, ripplePct: number, stepA: number): number {
  return stepA > 0 ? (volts * ripplePct) / 100 / stepA : Infinity
}

/** How many grid points the target is judged over: up to the board's band and no further. */
export function bandEnd(freqs: number[], bandHz: number): number {
  let k = 0
  while (k < freqs.length && freqs[k] <= bandHz * (1 + 1e-9)) k++
  return Math.max(k, 2)
}

/** Contiguous ranges where |Z| is above the target, within the band. */
export function gapsOf(freqs: number[], z: number[], target: number, bandHz: number): [number, number][] {
  const k = bandEnd(freqs, bandHz)
  const out: [number, number][] = []
  let start = -1
  for (let i = 0; i < k; i++) {
    const over = z[i] > target
    if (over && start < 0) start = i
    if (!over && start >= 0) {
      out.push([freqs[start], freqs[i - 1]])
      start = -1
    }
  }
  if (start >= 0) out.push([freqs[start], freqs[k - 1]])
  return out
}

export function worstOf(freqs: number[], z: number[], target: number, bandHz: number):
  { i: number; hz: number; excessDb: number } {
  const k = bandEnd(freqs, bandHz)
  let best = 0
  for (let i = 1; i < k; i++) if (z[i] / target > z[best] / target) best = i
  return { i: best, hz: freqs[best], excessDb: 20 * Math.log10(z[best] / target) }
}

export interface RankedRecommendation extends DecRecommendation {
  /** Recomputed for the target on screen. */
  improvementDb: number
  atHz: number
  fixesNow: [number, number][]
}

/**
 * The worker's what-ifs, re-scored for this target: dB at the worst frequency, and what each
 * one brings under the line. Ranked by how much of the worst gap it closes, as the worker does.
 */
export function rankRecommendations(
  ic: DecIC, parts: Record<string, DecPart>, freqs: number[], target: number,
): RankedRecommendation[] {
  const base = branchesOf(ic, parts)
  const before = curve(base, ic.series_l_h, freqs)
  const worst = worstOf(freqs, before, target, ic.band_hz)
  if (worst.excessDb <= 0) return []
  const k = bandEnd(freqs, ic.band_hz)
  const scored = ic.recommendations.map((r) => {
    const after = curve(applyChange(base, r.change), ic.series_l_h, freqs)
    const improvementDb = 20 * Math.log10(before[worst.i] / after[worst.i])
    const newWorst = worstOf(freqs, after, target, ic.band_hz).excessDb
    const reduction = worst.excessDb - Math.max(newWorst, 0)
    const fixesNow: [number, number][] = []
    let start = -1
    for (let i = 0; i < k; i++) {
      const fixed = before[i] > target && after[i] <= target
      if (fixed && start < 0) start = i
      if (!fixed && start >= 0) { fixesNow.push([freqs[start], freqs[i - 1]]); start = -1 }
    }
    if (start >= 0) fixesNow.push([freqs[start], freqs[k - 1]])
    const fixedDecades = fixesNow.reduce((s, [lo, hi]) => s + Math.log10(hi / lo), 0)
    return { ...r, improvementDb, atHz: worst.hz, fixesNow, reduction, fixedDecades }
  })
  // As the worker: what helps the worst point first, then what closes a gap elsewhere.
  return scored
    .filter((r) => (r.improvementDb > 0.1 && r.reduction > 0.1) || r.fixedDecades > 0)
    .sort((a, b) =>
      Math.max(b.reduction, 0) - Math.max(a.reduction, 0)
      || Math.max(b.improvementDb, 0) - Math.max(a.improvementDb, 0)
      || b.fixedDecades - a.fixedDecades)
    .map(({ reduction: _r, fixedDecades: _d, ...r }) => r)
}

export interface NoiseMark {
  hz: number
  harmonic: number
  source: string
  /** Above the target within the band. */
  notFiltered: boolean
  /** Above the band: the package's job, not the board's. */
  aboveBand: boolean
}

/** A fundamental and its harmonics up to the chart's end, each judged against the target. */
export function noiseMarks(
  noise: { hz: number; source: string }[], branches: DecBranch[], seriesLH: number,
  target: number, bandHz: number, fMax: number, maxHarmonics = 15,
): NoiseMark[] {
  const out: NoiseMark[] = []
  for (const n of noise) {
    for (let h = 1; h <= maxHarmonics && n.hz * h <= fMax; h++) {
      const f = n.hz * h
      out.push({
        hz: f, harmonic: h, source: n.source,
        notFiltered: f <= bandHz && networkMag(branches, seriesLH, f) > target,
        aboveBand: f > bandHz,
      })
    }
  }
  return out
}

export const fmtHz = (f: number): string =>
  f >= 1e9 ? `${+(f / 1e9).toPrecision(3)} GHz`
    : f >= 1e6 ? `${+(f / 1e6).toPrecision(3)} MHz`
      : `${+(f / 1e3).toPrecision(3)} kHz`

export const fmtFarads = (c: number): string =>
  c >= 1e-6 ? `${+(c / 1e-6).toPrecision(3)} µF`
    : c >= 1e-9 ? `${+(c / 1e-9).toPrecision(3)} nF`
      : `${+(c / 1e-12).toPrecision(3)} pF`

/** "30 to 60 MHz", with the unit once when both ends share it. */
export function fmtRange([lo, hi]: [number, number]): string {
  const a = fmtHz(lo)
  const b = fmtHz(hi)
  if (a === b) return a
  const ua = a.split(' ')[1]
  const ub = b.split(' ')[1]
  return ua === ub ? `${a.split(' ')[0]} to ${b}` : `${a} to ${b}`
}
