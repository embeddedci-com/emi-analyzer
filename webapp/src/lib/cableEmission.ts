/**
 * Composing a solve, a driver and a cable into a field at three metres — the browser's copy
 * of `worker/emi_worker/cables/emission.py` (§7, §10).
 *
 *     I_cm(f) = H_cm(f) · V_src(f) / Z_ant(f)
 *     E(f)    = I_cm(f) · E_per_amp(f)
 *
 * This runs in the browser for the same reason the near-field re-weighting does: §10 says a
 * driver is attached **after** the solve, so every term except the driver's voltage has to be
 * in the result already. `cable_ports.json` carries H_cm and `cable_antenna.json` carries
 * Z_ant and E_per_amp, both on the solve's dense grid; the driver supplies the rest.
 *
 * The composition is exact for a linear system provided the board and the cable interact only
 * through the gap. M0 measured what that costs: 1.2 dB median on a synthetic board, and
 * cable test 4 is re-measuring it on the real fixtures.
 */

import { DriverError, type Complex } from './driverSpectrum'
import type { Resolved } from './driverResolve'
import { limitAt } from './limits'

/** One microvolt per metre, the reference for dBµV/m. */
export const MICRO = 1e-6

/** §7's transfer function, as `cable_ports.json` writes it. */
export interface CableTransfer {
  ref: string
  frequencies_hz: number[]
  h_real: number[]
  h_imag: number[]
  /** False where the solve delivered no source energy, so H is a ratio of two small numbers. */
  usable?: boolean[]
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

export interface EmissionPoint {
  frequency_hz: number
  /** Common-mode current the layout drives into this cable, amps. */
  current_a: number
  /** Field at the standard's distance, V/m. */
  field_v_per_m: number
  limit_dbuv_per_m: number
}

export interface Emission {
  ref: string
  cableId: string
  standardId: string
  /** Where the field is evaluated, in metres — the standard's measuring distance. */
  distanceM: number
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

const cAbs = (c: Complex) => Math.hypot(c.re, c.im)

/**
 * Put the three halves together.
 *
 * `sourceVolts` is the driver's complex open-circuit voltage at each frequency, `null` where
 * the driver says nothing — which is carried through as undriven rather than dropped, because
 * a quietly shorter list of points looks like a cleaner result.
 *
 * Frequencies come from the transfer function. The antenna terms are matched by value rather
 * than by index: both grids come from the same solve today, and an assumption that silently
 * pairs 100 MHz with 120 MHz is not one worth leaving in place for when they do not.
 */
export function composeEmission(
  transfer: CableTransfer,
  antenna: CableAntenna,
  sourceVolts: (Complex | null)[],
  standardId = 'fcc-15b-radiated-3m',
): Emission {
  const freqs = transfer.frequencies_hz
  if (sourceVolts.length !== freqs.length) {
    throw new DriverError(
      `the driver was resolved at ${sourceVolts.length} frequencies and the transfer function ` +
        `has ${freqs.length}`,
    )
  }

  const byFreq = new Map<number, number>()
  antenna.frequencies_hz.forEach((f, k) => byFreq.set(f, k))

  const points: EmissionPoint[] = []
  const undriven = new Map<number, string>()

  freqs.forEach((f, k) => {
    if (transfer.usable && transfer.usable[k] === false) {
      undriven.set(
        f,
        `the solve delivered no source energy at ${f / 1e6} MHz, so the transfer function ` +
          `there is a ratio of two small numbers rather than a measurement`,
      )
      return
    }
    const v = sourceVolts[k]
    if (v === null) {
      undriven.set(f, `the driver says nothing at ${f / 1e6} MHz`)
      return
    }
    const j = byFreq.get(f)
    if (j === undefined) {
      undriven.set(f, `the antenna solver was not run at ${f / 1e6} MHz`)
      return
    }
    const zre = antenna.z_real[j]
    const zim = antenna.z_imag[j]
    const zAbs = Math.hypot(zre, zim)
    if (zAbs === 0) {
      undriven.set(f, `the cable has zero input impedance at ${f / 1e6} MHz`)
      return
    }

    // |H · V / Z|. Only the magnitude survives into a field, so the phases never have to be
    // carried past here — but they do have to be multiplied, not added as magnitudes.
    const h = { re: transfer.h_real[k], im: transfer.h_imag[k] }
    const hv = { re: h.re * v.re - h.im * v.im, im: h.re * v.im + h.im * v.re }
    const current = cAbs(hv) / zAbs

    points.push({
      frequency_hz: f,
      current_a: current,
      field_v_per_m: current * antenna.e_per_amp[j],
      limit_dbuv_per_m: limitAt(standardId, f),
    })
  })

  return {
    ref: transfer.ref, cableId: antenna.cable_id, standardId,
    distanceM: antenna.distance_m, points, undriven,
  }
}


/**
 * Below this fraction of the driver's own scale, a harmonic is a spectral null rather than a
 * small number — the same floor `driverApply` uses, and for the same reason: a 50 % duty clock
 * has no even harmonics at all, and the transform's rounding residue there is about 1e-16.
 * Taking its logarithm turns a few ulp into a field level that looks like a measurement.
 */
export const NULL_FLOOR = 1e-9

/**
 * A resolved driver's voltages, with its own nulls turned into `null`.
 *
 * `resolveDriver` already returns `null` where the driver says nothing about a frequency; this
 * additionally floors the harmonics it does drive but drives to nothing. `undriven` is
 * extended in place so the reason survives into the composition rather than being rediscovered
 * as "the driver says nothing", which is a different sentence and a different fix.
 */
export function sourceVolts(resolved: Resolved): (Complex | null)[] {
  const scale = resolved.scaleV
  return resolved.volts.map((v, k) => {
    if (v === null) return null
    if (scale > 0 && Math.hypot(v.re, v.im) < scale * NULL_FLOOR) {
      const f = resolved.frequencies_hz[k]
      resolved.undriven.set(
        f,
        `${f / 1e6} MHz falls on a null of this driver's spectrum, so it drives no current there`,
      )
      return null
    }
    return v
  })
}
