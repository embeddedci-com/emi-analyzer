/**
 * Composing a solve, a driver and a cable into a field at three metres — the browser's copy
 * of `compose_cable` in `worker/emi_worker/compliance/assemble.py` (§7, §10).
 *
 *     I_cm(f) = |H_cm(f)| · |Z_s + Z_in| / |Z_d + Z_in| · V_d(f) / |Z_ant(f)|
 *     E(f)    = I_cm(f) · E_per_amp(f)
 *
 * This runs in the browser for the same reason the near-field re-weighting does: §10 says a
 * driver is attached **after** the solve, so every term except the driver's voltage has to be
 * in the result already. `cable_ports.json` carries H_cm, `cable_antenna.json` Z_ant and
 * E_per_amp, and `ports.json` the port's input impedance.
 *
 * **The same composition as the compliance estimate, not a resemblance of it.** The chart used
 * to compose on the solve's 60-point grid, which drives a clock only where a harmonic happens
 * to land on a grid point, and it assumed the driver's source impedance equalled the port's.
 * It now evaluates every harmonic, interpolating the transfer functions between grid points,
 * and applies `|Z_s + Z_in| / |Z_d + Z_in|`, exactly as the worker does. Both are pinned by
 * `server/emi/testdata/cable_emission_fixtures.json`.
 *
 * The composition is exact for a linear system provided the board and the cable interact only
 * through the gap. M0 measured what that costs: 1.2 dB median on a synthetic board; cable
 * test 4 measures it on real boards (docs/verification/cables-and-drivers.md).
 */

import { DriverError, type Complex } from './driverSpectrum'
import { resolveDriver } from './driverResolve'
import type { Driver } from './driverDocument'
import { inRange, limitAt, standard } from './limits'

/** One microvolt per metre, the reference for dBµV/m. */
export const MICRO = 1e-6

/** A gap port's transfer function, the `transfer` block of a `cable_ports.json` entry. */
export interface CableTransfer {
  frequencies_hz: number[]
  h_real: number[]
  h_imag: number[]
  /** False where the solve delivered no source energy, so H is a ratio of two small numbers. */
  usable?: boolean[]
}

/** One entry of `cable_ports.json`'s `ports`. */
export interface CablePort {
  ref: string
  /** The excited port whose source the transfer function is per volt of. */
  driven_by: string
  transfer: CableTransfer
}

/**
 * The gap ports in `cable_ports.json`, as `worker/emi_worker/openems/post.py` writes it:
 * `{ports: [{ref, anchor_mm, driven_by, transfer: {...}}]}`. Reading it any other way yields no
 * cables, and the chart silently never renders. An entry without a ref or a transfer is skipped.
 */
export function parseCablePorts(json: unknown): CablePort[] {
  const ports = (json as { ports?: Partial<CablePort>[] } | null)?.ports ?? []
  return ports
    .filter((p): p is CablePort => !!p && typeof p.ref === 'string' && !!p.transfer)
    .map((p) => ({ ref: p.ref, driven_by: p.driven_by ?? '', transfer: p.transfer }))
}

/** The antenna solver's terms, as `cable_antenna.json` writes them. */
export interface CableAntenna {
  ref: string
  cable_id: string
  length_m: number
  distance_m: number
  frequencies_hz: number[]
  z_real: number[]
  z_imag: number[]
  e_per_amp: number[]
}

/** A port's dense spectrum, the `dense` block of a `ports.json` entry. */
export interface PortSpectrum {
  frequencies_hz: number[]
  v_real: number[]
  v_imag: number[]
  i_real: number[]
  i_imag: number[]
}

export interface EmissionPoint {
  frequency_hz: number
  /** Common-mode current the layout drives into this cable, amps RMS. */
  current_a: number
  /** Field at the standard's distance, V/m RMS. */
  field_v_per_m: number
  limit_dbuv_per_m: number
}

export interface Emission {
  ref: string
  cableId: string
  standardId: string
  /** Where the field is evaluated, in metres — the standard's measuring distance. */
  distanceM: number
  /** True when the points are the driver's harmonics rather than a continuous spectrum. */
  line: boolean
  points: EmissionPoint[]
  /** Frequency -> why there is no point there. Never silently dropped: §17.3 reads this. */
  undriven: Map<number, string>
}

export const fieldDbuv = (p: EmissionPoint): number =>
  p.field_v_per_m > 0 ? 20 * Math.log10(p.field_v_per_m / MICRO) : Number.NEGATIVE_INFINITY

export const currentDbua = (p: EmissionPoint): number =>
  p.current_a > 0 ? 20 * Math.log10(p.current_a / 1e-6) : Number.NEGATIVE_INFINITY

/** Positive means under the limit. Negative is a predicted failure. */
export const marginDb = (p: EmissionPoint): number => p.limit_dbuv_per_m - fieldDbuv(p)

/** The worst margin in a set of points, which is what a summary line should quote. */
export function worstPoint(e: Emission): EmissionPoint | null {
  let worst: EmissionPoint | null = null
  for (const p of e.points) {
    if (!Number.isFinite(marginDb(p))) continue
    if (worst === null || marginDb(p) < marginDb(worst)) worst = p
  }
  return worst
}

/**
 * Below this fraction of the driver's own scale, a harmonic is a spectral null rather than a
 * small number — the same floor `driverApply` and the worker use. It becomes a point of zero
 * current, which the chart leaves out and the estimate counts as zero.
 */
export const NULL_FLOOR = 1e-9

/** The most harmonics one composition evaluates, as `MAX_LINES` in the worker. */
export const MAX_LINES = 50_000

// ---- interpolation, never extrapolation --------------------------------------------------

function bracket(fs: number[], f: number): [number, number, number] | null {
  if (fs.length === 0 || f < fs[0] * (1 - 1e-9) || f > fs[fs.length - 1] * (1 + 1e-9)) {
    return null
  }
  for (let k = 0; k < fs.length - 1; k++) {
    if (fs[k] <= f && f <= fs[k + 1]) {
      if (fs[k + 1] === fs[k]) return [k, k, 0]
      return [k, k + 1, Math.log(f / fs[k]) / Math.log(fs[k + 1] / fs[k])]
    }
  }
  const k = f <= fs[0] ? 0 : fs.length - 1
  return [k, k, 0]
}

/** A magnitude at `f`, interpolated in dB against log frequency. */
export function interpDb(fs: number[], mags: number[], f: number): number | null {
  const b = bracket(fs, f)
  if (b === null) return null
  const [i, j, t] = b
  const a = mags[i]
  const c = mags[j]
  if (a <= 0 || c <= 0) return t === 0 ? a : t === 1 ? c : Math.max(a, c)
  return Math.exp((1 - t) * Math.log(a) + t * Math.log(c))
}

/** A complex value at `f`, its parts interpolated linearly in log frequency. */
export function interpComplex(
  fs: number[], re: number[], im: number[], f: number,
): Complex | null {
  const b = bracket(fs, f)
  if (b === null) return null
  const [i, j, t] = b
  return { re: (1 - t) * re[i] + t * re[j], im: (1 - t) * im[i] + t * im[j] }
}

function usableSeries(
  fs: number[], usable: boolean[], ...series: number[][]
): { fs: number[]; series: number[][]; holes: number[] } {
  const keep = usable.flatMap((ok, k) => (ok ? [k] : []))
  if (keep.length === 0) return { fs: [], series: series.map(() => []), holes: [] }
  const lo = fs[keep[0]]
  const hi = fs[keep[keep.length - 1]]
  const holes = fs.filter((f, k) => !usable[k] && lo < f && f < hi)
  return { fs: keep.map((k) => fs[k]), series: series.map((s) => keep.map((k) => s[k])), holes }
}

/** A port's input impedance from its dense spectrum, where the current is not zero. */
export function portImpedance(block: PortSpectrum): { f: number[]; zr: number[]; zi: number[] } {
  const zr: number[] = []
  const zi: number[] = []
  const ok: boolean[] = []
  block.frequencies_hz.forEach((_, k) => {
    const v = { re: block.v_real[k], im: block.v_imag[k] }
    const i = { re: block.i_real[k], im: block.i_imag[k] }
    const d = i.re * i.re + i.im * i.im
    ok.push(d !== 0)
    zr.push(d === 0 ? NaN : (v.re * i.re + v.im * i.im) / d)
    zi.push(d === 0 ? NaN : (v.im * i.re - v.re * i.im) / d)
  })
  const u = usableSeries(block.frequencies_hz, ok, zr, zi)
  return { f: u.fs, zr: u.series[0], zi: u.series[1] }
}

// ---- the driver --------------------------------------------------------------------------

const period = (d: Driver): number | null =>
  d.kind === 'trapezoid' || d.kind === 'waveform' ? d.values.period_s.value : null

function evaluationFrequencies(
  d: Driver, band: [number, number], grid: number[], standardId: string,
): { freqs: number[]; line: boolean } {
  const [lo, hi] = band
  const T = period(d)
  if (T !== null) {
    const n0 = Math.max(1, Math.ceil(lo * T * (1 - 1e-12)))
    const n1 = Math.floor(hi * T * (1 + 1e-12))
    if (n1 - n0 + 1 > MAX_LINES) {
      throw new DriverError(
        `this driver has ${(n1 - n0 + 1).toLocaleString('en-US')} harmonics in ` +
          `${lo / 1e6}-${hi / 1e6} MHz, more than the ` +
          `${MAX_LINES.toLocaleString('en-US')} a compliance run evaluates`,
      )
    }
    const freqs: number[] = []
    for (let n = n0; n <= n1; n++) {
      const f = n / T
      if (inRange(standardId, f)) freqs.push(f)
    }
    return { freqs, line: true }
  }
  const pts = (d.payload.points as [number, number][] | undefined) ?? []
  if (pts.length === 0) return { freqs: [], line: false }
  const sLo = pts[0][0]
  const sHi = pts[pts.length - 1][0]
  return {
    freqs: grid.filter((f) => sLo <= f && f <= sHi && inRange(standardId, f)),
    line: false,
  }
}

/** RMS source volts at `freqs`: null where the driver says nothing, 0 on its own nulls. */
function sourceVolts(d: Driver, freqs: number[], undriven: Map<number, string>): (number | null)[] {
  if (freqs.length === 0) return []
  const r = resolveDriver(d, freqs)
  for (const [f, why] of r.undriven) undriven.set(f, why)
  return r.volts.map((v) => {
    if (v === null) return null
    const mag = Math.hypot(v.re, v.im)
    return r.scaleV > 0 && mag < r.scaleV * NULL_FLOOR ? 0 : mag
  })
}

function sourceFactor(zS: number, zD: number, zIn: Complex): number {
  const den = Math.hypot(zD + zIn.re, zIn.im)
  if (den === 0) {
    throw new DriverError("the driver's source impedance cancels the port's input impedance")
  }
  return Math.hypot(zS + zIn.re, zIn.im) / den
}

// ---- the composition ---------------------------------------------------------------------

export interface CableComposition {
  /** The band the transfer function, antenna and port all cover, or null. */
  covered: [number, number] | null
  /** `covered` inside the standard's scan, or null when they do not overlap. */
  band: [number, number] | null
  emission: Emission
}

/**
 * Compose a cable's emission for one driver, as the compliance estimate does.
 *
 * `zS` is the resistance the solve drove the port from (`manifest.run.ports`); the driver's own
 * source impedance replaces it through `|Z_s + Z_in| / |Z_d + Z_in|`.
 */
export function composeCable(
  port: CablePort,
  antenna: CableAntenna,
  spectrum: PortSpectrum,
  zS: number,
  driver: Driver,
  standardId = 'fcc-15b-radiated-3m',
): CableComposition {
  const undriven = new Map<number, string>()
  const emission: Emission = {
    ref: port.ref, cableId: antenna.cable_id, standardId, distanceM: antenna.distance_m,
    line: false, points: [], undriven,
  }
  const out: CableComposition = { covered: null, band: null, emission }

  const tr = port.transfer
  const u = usableSeries(
    tr.frequencies_hz, tr.usable ?? tr.frequencies_hz.map(() => true), tr.h_real, tr.h_imag,
  )
  for (const f of u.holes) {
    undriven.set(
      f,
      `the solve put no source energy near ${f / 1e6} MHz, so ${port.ref}'s transfer ` +
        `function is unknown there`,
    )
  }
  const zIn = portImpedance(spectrum)
  const fa = antenna.frequencies_hz
  if (u.fs.length === 0 || zIn.f.length === 0 || fa.length === 0) return out
  const lo = Math.max(u.fs[0], fa[0], zIn.f[0])
  const hi = Math.min(u.fs[u.fs.length - 1], fa[fa.length - 1], zIn.f[zIn.f.length - 1])
  if (lo > hi) return out
  out.covered = [lo, hi]
  const segs = standard(standardId).segments
  const band: [number, number] = [
    Math.max(lo, segs[0].f_lo_hz), Math.min(hi, segs[segs.length - 1].f_hi_hz),
  ]
  if (band[0] > band[1]) return out
  out.band = band

  const hMag = u.series[0].map((re, k) => Math.hypot(re, u.series[1][k]))
  const { freqs, line } = evaluationFrequencies(driver, band, u.fs, standardId)
  emission.line = line
  const volts = sourceVolts(driver, freqs, undriven)
  const zD = driver.values.source_impedance_ohm.value
  freqs.forEach((f, k) => {
    const v = volts[k]
    if (v === null) return
    const h = interpDb(u.fs, hMag, f)
    const zAnt = interpComplex(fa, antenna.z_real, antenna.z_imag, f)
    const eAmp = interpDb(fa, antenna.e_per_amp, f)
    const z = interpComplex(zIn.f, zIn.zr, zIn.zi, f)
    if (h === null || zAnt === null || eAmp === null || z === null) return
    const zAntAbs = Math.hypot(zAnt.re, zAnt.im)
    if (zAntAbs === 0) return
    const current = (h * sourceFactor(zS, zD, z) * v) / zAntAbs
    emission.points.push({
      frequency_hz: f,
      current_a: current,
      field_v_per_m: current * eAmp,
      limit_dbuv_per_m: limitAt(standardId, f),
    })
  })
  return out
}

/**
 * What a solve needs before the chart can take a driver, or why it cannot.
 *
 * A solve from before the port spectra, or before the record of the resistance each port was
 * driven from, cannot have its source replaced: the numbers are not in its artifacts. The
 * honest answer is a re-run, as the near-field re-weighting and the estimate both say.
 */
export function whyNoCableDriver(
  manifest: { format_version: number; run?: Record<string, unknown> },
  spectrum: PortSpectrum | null,
): string | null {
  const ports = (manifest.run as { ports?: unknown } | undefined)?.ports
  if (manifest.format_version < 2 || !Array.isArray(ports) || spectrum === null) {
    return (
      'Re-run this solve to see cable emissions with a driver: it predates the port ' +
      'records a driver needs.'
    )
  }
  return null
}

/** The resistance the solve drove `port` from, 50 ohm when the record does not say. */
export function portResistance(
  manifest: { run?: Record<string, unknown> }, port: string,
): number {
  const ports = (manifest.run as { ports?: { name: string; resistance_ohm?: number }[] })?.ports
  return ports?.find((p) => p.name === port)?.resistance_ohm ?? 50
}
