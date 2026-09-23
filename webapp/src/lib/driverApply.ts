/**
 * Applying a driver to a finished solve — the browser's copy (docs/implementation.md §4).
 *
 * The solve recorded fields in whatever units its Gaussian excitation produced, stored as dB
 * relative to one shared peak. A driver turns those into absolute units, and docs/implementation.md
 * §4 asks for that as **one dB offset per frequency** so the near-field shader adds a uniform
 * rather than rewriting a texture.
 *
 *     displayed dBµA/m  =  stored dB (relative to peak)  +  offsetDb(f)
 *
 *     offsetDb(f) = 20*log10(referenceMagnitude / 1e-6)   relative -> absolute µA/m
 *                 + 20*log10(|I_new(f) / I_solve(f)|)     the driver's current
 *
 * Keeping both halves in one number is deliberate: two numbers invite a caller to apply one
 * and forget the other, and forgetting the micro prefix is a 120 dB error that still looks
 * like a plausible emissions figure.
 */

import { DriverError, reweight, type Complex } from './driverSpectrum'
import type { Driver } from './driverDocument'
import { resolveDriver, type Resolved } from './driverResolve'

/** One microamp per metre, the reference for dBµA/m. */
export const MICRO = 1e-6

/**
 * Below this fraction of the driver's own scale, a harmonic is a spectral null rather than a
 * small number. A 50 % duty clock has no even harmonics at all; the transform returns a
 * rounding residue around 1e-16 there, and taking its logarithm turns a few ulp into tens of
 * dB. "-204 dBµA/m" would be noise presented as a measurement, and two implementations would
 * not agree on the noise.
 */
export const NULL_FLOOR = 1e-9

export interface PortSpectrum {
  frequencies_hz: number[]
  v: Complex[]
  i: Complex[]
}

export interface PortSpectrumJson {
  frequencies_hz: number[]
  v_real: number[]
  v_imag: number[]
  i_real: number[]
  i_imag: number[]
}

export function portSpectrumFromJson(block: PortSpectrumJson): PortSpectrum {
  const f = block.frequencies_hz.map(Number)
  const v = block.v_real.map((re, k) => ({ re, im: block.v_imag[k] }))
  const i = block.i_real.map((re, k) => ({ re, im: block.i_imag[k] }))
  if (f.length !== v.length || f.length !== i.length) {
    throw new DriverError('port spectrum arrays have different lengths')
  }
  return { frequencies_hz: f, v, i }
}

export function portAt(
  port: PortSpectrum,
  frequencyHz: number,
  tolerancePpm = 100,
): { v: Complex; i: Complex } {
  for (let k = 0; k < port.frequencies_hz.length; k++) {
    const f = port.frequencies_hz[k]
    if (f === frequencyHz || Math.abs(f - frequencyHz) <= (frequencyHz * tolerancePpm) / 1e6) {
      return { v: port.v[k], i: port.i[k] }
    }
  }
  const shown = port.frequencies_hz.slice(0, 8).map((x) => x / 1e6).join(', ')
  throw new DriverError(
    `the solve recorded no port spectrum at ${frequencyHz / 1e6} MHz; it has ${shown} MHz`,
  )
}

export interface Applied {
  frequencies_hz: number[]
  /** dB to add to a stored value to read it in dBµA/m. `null` where not driven. */
  offsetDb: (number | null)[]
  undriven: Map<number, string>
}

export const appliedIsComplete = (a: Applied): boolean => a.undriven.size === 0

/**
 * The relative-to-absolute half of the offset, on its own.
 *
 * `referenceMagnitude` is the peak |H| in A/m that every stored dB value is relative to.
 * This is the only place the micro prefix is applied.
 */
export function referenceOffsetDb(referenceMagnitude: number): number {
  if (!(referenceMagnitude > 0)) {
    throw new DriverError(
      'the solve reports a reference magnitude of zero, so it produced no field and there ' +
        'is nothing to put a unit on',
    )
  }
  return 20 * Math.log10(referenceMagnitude / MICRO)
}

const cAbs = (c: Complex) => Math.hypot(c.re, c.im)

/** Offsets that turn this solve's stored dB into dBµA/m under `driver`. */
export function applyDriver(
  driver: Driver,
  port: PortSpectrum,
  referenceMagnitude: number,
  frequencies?: number[],
  resolved?: Resolved,
): Applied {
  const freqs = frequencies ? [...frequencies] : [...port.frequencies_hz]
  if (freqs.length === 0) throw new DriverError('ask for at least one frequency')
  const source = resolved ?? resolveDriver(driver, freqs)
  if (source.frequencies_hz.length !== freqs.length ||
      source.frequencies_hz.some((f, k) => f !== freqs[k])) {
    throw new DriverError('the resolved driver does not cover the requested frequencies')
  }

  const reference = referenceOffsetDb(referenceMagnitude)
  const zS = driver.values.source_impedance_ohm.value
  // The driver's own scale, not the peak of whatever was asked for: a caller asking only
  // about 50 MHz must still be told that 50 MHz is a null.
  const scale = source.scaleV

  const offsetDb: (number | null)[] = []
  const undriven = new Map(source.undriven)

  freqs.forEach((f, k) => {
    const vSource = source.volts[k]
    if (vSource === null) {
      offsetDb.push(null)
      return
    }
    if (scale > 0 && cAbs(vSource) < scale * NULL_FLOOR) {
      offsetDb.push(null)
      undriven.set(
        f,
        `${f / 1e6} MHz falls on a null of this driver's spectrum, so it drives no current ` +
          `there`,
      )
      return
    }
    const { v, i } = portAt(port, f)
    let factor: number
    try {
      factor = cAbs(reweight(vSource, zS, v, i))
    } catch (e) {
      offsetDb.push(null)
      undriven.set(f, (e as Error).message)
      return
    }
    if (!(factor > 0)) {
      offsetDb.push(null)
      undriven.set(
        f,
        `${f / 1e6} MHz falls on a null of this driver's spectrum, so it drives no current ` +
          `there`,
      )
      return
    }
    offsetDb.push(reference + 20 * Math.log10(factor))
  })

  return { frequencies_hz: freqs, offsetDb, undriven }
}
