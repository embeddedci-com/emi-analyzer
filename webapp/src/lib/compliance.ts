/**
 * Combining paths into a margin and a confidence — the browser's copy of
 * `worker/emi_worker/compliance/predict.py` (§16, §17).
 *
 * It exists in the browser for the same reason the driver re-weighting does: a cable-side
 * what-if — a choke, a shorter cable, a different far end — changes only the antenna model, and
 * the answer should come back while the user is still looking at the control they moved. Going
 * to a worker for that would make a one-second question a thirty-second one.
 *
 * Both halves are asserted against `compliance_fixtures.json`. The margin and the confidence
 * beside it are the two numbers most likely to be quoted at someone else, so they have to be
 * identical between here and the worker rather than nearly identical.
 */

import { limitAt } from './limits'

export const MICRO = 1e-6

export const toDbuv = (vPerM: number): number =>
  vPerM > 0 ? 20 * Math.log10(vPerM / MICRO) : Number.NEGATIVE_INFINITY

export const fromDbuv = (dbuv: number): number => MICRO * 10 ** (dbuv / 20)

export interface PathInput {
  kind: string
  label: string
  driver_id: string
  /** One entry per frequency, in the same order as `frequencies_hz`. */
  field_v_per_m: number[]
  sigma_terms: Record<string, number>
}

export interface Contribution {
  label: string
  kind: string
  driverId: string
  fieldVPerM: number
  /** Share of the total power at this frequency, 0..1. */
  share: number
}

export interface Point {
  frequencyHz: number
  fieldVPerM: number
  fieldDbuv: number
  limitDbuv: number
  marginDb: number
  contributions: Contribution[]
  /** True when one driver reached the antenna by more than one path here. */
  sharesIndicative: boolean
}

export interface Outlook {
  points: Point[]
  worst: Point | null
  sigmaDb: number
  sigmaTerms: Record<string, number>
  nearMisses: Point[]
  confidence: number | null
  range80Db: [number, number] | null
}

/** Standard normal CDF, via an Abramowitz–Stegun erf. */
export function phi(z: number): number {
  return 0.5 * (1 + erf(z / Math.SQRT2))
}

function erf(x: number): number {
  // Abramowitz & Stegun 7.1.26: |error| < 1.5e-7, which is four orders below the precision
  // anything downstream claims. Written out rather than pulled in as a dependency because one
  // function is not worth a package, and because the two implementations of this file have to
  // agree to more places than a package boundary would make visible.
  const sign = x < 0 ? -1 : 1
  const a = Math.abs(x)
  const t = 1 / (1 + 0.3275911 * a)
  const y =
    1 -
    ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t +
      0.254829592) *
      t *
      Math.exp(-a * a)
  return sign * y
}

export const combineSigma = (terms: Record<string, number>): number =>
  Math.sqrt(Object.values(terms).reduce((s, v) => s + v * v, 0))

/**
 * §16.2's combination.
 *
 *     E_d(f) = Σ_paths |E_{d,path}(f)|   one driver's paths add in AMPLITUDE
 *     E(f)   = sqrt( Σ_d E_d(f)² )       different drivers add in POWER
 *
 * Amplitude for one driver is the worst case over a relative phase the model does not know.
 * Two equal paths are 6 dB up this way and 3 dB in power — getting it backwards is optimistic
 * exactly where two paths matter.
 */
export function combine(
  paths: PathInput[],
  frequencies: number[],
  standardId: string,
): Point[] {
  return frequencies.map((f, k) => {
    const byDriver = new Map<string, number>()
    const present: { path: PathInput; e: number }[] = []
    for (const p of paths) {
      const e = p.field_v_per_m[k] ?? 0
      if (!(e > 0)) continue
      present.push({ path: p, e })
      byDriver.set(p.driver_id, (byDriver.get(p.driver_id) ?? 0) + e)
    }
    const total = Math.sqrt([...byDriver.values()].reduce((s, v) => s + v * v, 0))
    const contributions: Contribution[] = present
      .map(({ path, e }) => ({
        label: path.label,
        kind: path.kind,
        driverId: path.driver_id,
        fieldVPerM: e,
        share: total > 0 ? (e * e) / (total * total) : 0,
      }))
      .sort((a, b) => b.share - a.share)

    // Exact only when no driver contributed through more than one path: an amplitude sum is
    // not a power sum, so the parts stop adding to the whole.
    const sharesIndicative = [...byDriver.keys()].some(
      (d) => present.filter((q) => q.path.driver_id === d).length > 1,
    )
    const limitDbuv = limitAt(standardId, f)
    const fieldDbuv = toDbuv(total)
    return {
      frequencyHz: f,
      fieldVPerM: total,
      fieldDbuv,
      limitDbuv,
      marginDb: limitDbuv - fieldDbuv,
      contributions,
      sharesIndicative,
    }
  })
}

/**
 * σ at one frequency, each path's terms weighted by its power share (§17.1).
 *
 * A budget taking the worst path's terms unweighted would report a cable's 4.5 dB for a
 * frequency where the cable contributes 2 % of the power, and the confidence figure would then
 * describe a different prediction from the one on screen.
 */
export function sigmaAt(
  point: Point,
  paths: PathInput[],
  sharedTerms: Record<string, number>,
): { sigmaDb: number; terms: Record<string, number> } {
  const byLabel = new Map(paths.map((p) => [p.label, p]))
  const terms: Record<string, number> = { ...sharedTerms }
  for (const c of point.contributions) {
    const path = byLabel.get(c.label)
    if (!path) continue
    for (const [name, value] of Object.entries(path.sigma_terms)) {
      const key = `${name} (${c.label})`
      const prev = terms[key] ?? 0
      terms[key] = Math.sqrt(prev * prev + c.share * value * value)
    }
  }
  return { sigmaDb: combineSigma(terms), terms }
}

export function outlook(
  paths: PathInput[],
  frequencies: number[],
  standardId: string,
  sharedTerms: Record<string, number> = {},
): Outlook {
  const points = combine(paths, frequencies, standardId)
  const scored = points.filter((p) => p.fieldVPerM > 0)
  if (scored.length === 0) {
    return {
      points, worst: null, sigmaDb: 0, sigmaTerms: {}, nearMisses: [],
      confidence: null, range80Db: null,
    }
  }
  const worst = scored.reduce((a, b) => (b.marginDb < a.marginDb ? b : a))
  const { sigmaDb, terms } = sigmaAt(worst, paths, sharedTerms)
  const nearMisses = scored
    .filter((p) => p !== worst && p.marginDb <= worst.marginDb + sigmaDb)
    .sort((a, b) => a.marginDb - b.marginDb)

  return {
    points,
    worst,
    sigmaDb,
    sigmaTerms: terms,
    nearMisses,
    confidence: sigmaDb > 0 ? phi(worst.marginDb / sigmaDb) : null,
    range80Db: [worst.marginDb - 1.28 * sigmaDb, worst.marginDb + 1.28 * sigmaDb],
  }
}
