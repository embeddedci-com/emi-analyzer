/**
 * From a driver document to the voltage it pushes at each frequency — the browser's copy.
 *
 * Drives the live preview and feeds the near-field re-weighting (docs/implementation.md §4). The
 * worker holds the other implementation (`worker/emi_worker/drivers/resolve.py`); both assert
 * against `server/emi/testdata/driver_document_fixtures.json`.
 *
 * The substance here is the *failures*. A trapezoid or a captured waveform is a **line**
 * spectrum: a 25 MHz clock has energy at 25 and 75 MHz and none at 40. A solve asked for
 * 40 MHz is not driven by that clock, and saying so is the honest answer — a small number
 * there would look measured and be invented. docs/emi-driver-format.md §4 says the same about a
 * waveform above its capture bandwidth with no declared rise time.
 *
 * So every resolution returns `null` where the driver has nothing to say and names those
 * frequencies. The completeness gate (docs/implementation.md §7.5) reads that list.
 */

import {
  cornerFrequencies,
  DriverError,
  envelopeV,
  RMS_PER_PEAK,
  piecewiseLinearSeries,
  validateTrapezoid,
  type Complex,
  type Trapezoid,
} from './driverSpectrum'
import { driverTrapezoid, type Driver } from './driverDocument'

/**
 * How close a requested frequency must be to a harmonic to count as that harmonic, in parts
 * per million. Solve frequencies come from the same clock the driver describes, so they line
 * up exactly in the normal case; this absorbs float arithmetic, not disagreement.
 */
export const HARMONIC_TOLERANCE_PPM = 100

export interface Resolved {
  frequencies_hz: number[]
  /** Open-circuit source voltage. `null` where the driver drives nothing. */
  volts: (Complex | null)[]
  /** Why each undriven frequency is undriven, keyed by frequency. */
  undriven: Map<number, string>
  /** True when the values carry no meaningful phase (an uploaded spectrum is magnitudes). */
  magnitudeOnly: boolean
  /**
   * The driver's own characteristic voltage, independent of which frequencies were asked
   * for. A null has to be recognisable from a single-frequency request, so it cannot be
   * judged against the peak of whatever happened to be requested.
   */
  scaleV: number
}

export const drivenCount = (r: Resolved): number => r.volts.filter((v) => v !== null).length
export const isComplete = (r: Resolved): boolean => r.undriven.size === 0

const cAbs = (c: Complex): number => Math.hypot(c.re, c.im)

function harmonicIndex(frequencyHz: number, periodS: number): number | null {
  const exact = frequencyHz * periodS
  const n = Math.round(exact)
  if (n < 1) return null
  if (Math.abs(exact - n) / n > HARMONIC_TOLERANCE_PPM / 1e6) return null
  return n
}

function resolveLines(
  times: number[],
  values: number[],
  periodS: number,
  frequencies: number[],
  what: string,
): { volts: (Complex | null)[]; undriven: Map<number, string> } {
  const volts: (Complex | null)[] = []
  const undriven = new Map<number, string>()
  const fundamentalMhz = 1 / periodS / 1e6
  for (const f of frequencies) {
    const n = harmonicIndex(f, periodS)
    if (n === null) {
      volts.push(null)
      undriven.set(
        f,
        `${f / 1e6} MHz is not a harmonic of this ${what} (${fundamentalMhz} MHz ` +
          `fundamental), so it drives nothing there`,
      )
      continue
    }
    const c = piecewiseLinearSeries(times, values, periodS, n)
    // An RMS phasor, like every amplitude here (RMS_PER_PEAK).
    volts.push({ re: 2 * RMS_PER_PEAK * c.re, im: 2 * RMS_PER_PEAK * c.im })
  }
  return { volts, undriven }
}

function trapezoidBreakpoints(t: Trapezoid): { times: number[]; values: number[] } {
  validateTrapezoid(t)
  const flat = t.pulse_width_s - (t.rise_s + t.fall_s) / 2
  const times = [0, t.rise_s, t.rise_s + flat, t.rise_s + flat + t.fall_s]
  const values = [0, t.amplitude_v, t.amplitude_v, 0]
  if (times[times.length - 1] < t.period_s) {
    times.push(t.period_s)
    values.push(0)
  }
  return { times, values }
}

export function resolveDriver(driver: Driver, frequencies: number[]): Resolved {
  if (frequencies.length === 0) throw new DriverError('ask for at least one frequency')
  if (frequencies.some((f) => f <= 0)) throw new DriverError('frequencies must be positive')

  if (driver.kind === 'trapezoid') {
    const trap = driverTrapezoid(driver)
    const { times, values } = trapezoidBreakpoints(trap)
    const { volts, undriven } = resolveLines(times, values, trap.period_s, frequencies, 'clock')
    return {
      frequencies_hz: [...frequencies],
      volts,
      undriven,
      magnitudeOnly: false,
      scaleV: Math.abs(trap.amplitude_v),
    }
  }
  if (driver.kind === 'waveform') return resolveWaveform(driver, frequencies)
  return resolveSpectrum(driver, frequencies)
}

function resolveWaveform(driver: Driver, frequencies: number[]): Resolved {
  const samples = driver.payload.samples_v as number[]
  const interval = driver.payload.sample_interval_s as number
  const bandwidth = driver.payload.bandwidth_hz as number | null
  const period = driver.values.period_s.value

  // A sampled waveform is piecewise linear between its samples, so the same exact transform
  // the trapezoid uses applies unchanged. One period of it: transforming the whole capture
  // would fold the repetition into the answer.
  const nPeriod = Math.round(period / interval)
  if (nPeriod < 2) {
    throw new DriverError(
      `one period is ${(period * 1e9).toPrecision(4)} ns and the sample interval is ` +
        `${(interval * 1e9).toPrecision(4)} ns, which is fewer than two samples per period`,
    )
  }
  const take = Math.min(nPeriod + 1, samples.length)
  const times = Array.from({ length: take }, (_, k) => k * interval)
  const values = samples.slice(0, take)

  const { volts, undriven } = resolveLines(times, values, period, frequencies, 'waveform')

  if (bandwidth !== null && bandwidth !== undefined) {
    const rise = driver.values.rise_s
    let envelopeTrap: Trapezoid | null = null
    if (rise) {
      const amplitude = Math.max(...values) - Math.min(...values)
      if (amplitude > 0 && rise.value > 0) {
        const candidate: Trapezoid = {
          amplitude_v: amplitude,
          period_s: period,
          pulse_width_s: period / 2,
          rise_s: rise.value,
          fall_s: rise.value,
        }
        try {
          validateTrapezoid(candidate)
          envelopeTrap = candidate
        } catch {
          envelopeTrap = null
        }
      }
    }

    frequencies.forEach((f, i) => {
      if (f <= bandwidth || volts[i] === null) return
      if (envelopeTrap === null) {
        volts[i] = null
        undriven.set(
          f,
          `${f / 1e6} MHz is above this capture's ${bandwidth / 1e6} MHz bandwidth and the ` +
            `driver declares no rise time, so nothing is known about it`,
        )
      } else {
        volts[i] = { re: envelopeV(envelopeTrap, f), im: 0 }
      }
    })

    if (envelopeTrap !== null) {
      const scale = joinScale(times, values, period, bandwidth, envelopeTrap)
      frequencies.forEach((f, i) => {
        const v = volts[i]
        if (f > bandwidth && v !== null) volts[i] = { re: v.re * scale, im: v.im * scale }
      })
    }
  }

  return {
    frequencies_hz: [...frequencies],
    volts,
    undriven,
    magnitudeOnly: false,
    scaleV: Math.max(...values) - Math.min(...values),
  }
}

/** How many of the capture's own harmonics the envelope is matched against at the join. */
export const JOIN_HARMONICS = 32

/**
 * How much to scale the rise-time envelope so it meets the capture at its bandwidth.
 *
 * docs/emi-driver-format.md §4 continues a waveform above its bandwidth "with the envelope".
 * The envelope bounds a line spectrum from above and touches it at the tops of its lobes, so
 * the scale is the largest ratio of transform to envelope over the top octave below the
 * bandwidth.
 *
 * It used to be the ratio at the single highest *requested* frequency below the join. That
 * harmonic can sit on a null (every even harmonic of a square wave does), which scaled the
 * envelope to nothing, and it depended on what the caller asked for. This reads the capture's
 * own harmonics, with an odd stride so a square wave's nulls cannot fill the sample. The same
 * function is `join_scale` in `resolve.py`.
 */
export function joinScale(
  times: number[],
  values: number[],
  periodS: number,
  bandwidthHz: number,
  envelopeTrap: Trapezoid,
): number {
  const nHi = Math.floor(bandwidthHz * periodS * (1 + 1e-12))
  if (nHi < 1) return 1
  const nLo = Math.max(1, Math.ceil(nHi / 2))
  const count = nHi - nLo + 1
  let stride = Math.max(1, Math.ceil(count / JOIN_HARMONICS))
  if (stride % 2 === 0) stride += 1
  let best = 0
  for (let n = nHi; n >= nLo; n -= stride) {
    const line = 2 * RMS_PER_PEAK * cAbs(piecewiseLinearSeries(times, values, periodS, n))
    const bound = envelopeV(envelopeTrap, n / periodS)
    if (bound > 0) best = Math.max(best, line / bound)
  }
  // A capture with nothing in its top octave has nothing to match against; the unscaled
  // envelope from its own amplitude and rise time is then the better guess.
  return best > 0 ? best : 1
}

function resolveSpectrum(driver: Driver, frequencies: number[]): Resolved {
  const points = driver.payload.points as [number, number][]
  const lo = points[0][0]
  const hi = points[points.length - 1][0]
  const volts: (Complex | null)[] = []
  const undriven = new Map<number, string>()
  for (const f of frequencies) {
    if (f < lo || f > hi) {
      volts.push(null)
      undriven.set(
        f,
        `the uploaded spectrum covers ${lo / 1e6}-${hi / 1e6} MHz and says nothing at ` +
          `${f / 1e6} MHz`,
      )
      continue
    }
    volts.push({ re: dbuvToVolts(interpolateDb(points, f)), im: 0 })
  }
  return {
    frequencies_hz: [...frequencies],
    volts,
    undriven,
    magnitudeOnly: true,
    scaleV: dbuvToVolts(Math.max(...points.map(([, level]) => level))),
  }
}

/** Interpolate a level in dB against log frequency, as the compliance stage reads a spectrum. */
function interpolateDb(points: [number, number][], frequencyHz: number): number {
  for (let i = 0; i < points.length - 1; i++) {
    const [f0, l0] = points[i]
    const [f1, l1] = points[i + 1]
    if (f0 <= frequencyHz && frequencyHz <= f1) {
      if (f1 === f0) return l0
      const along = Math.log10(frequencyHz / f0) / Math.log10(f1 / f0)
      return l0 + along * (l1 - l0)
    }
  }
  throw new DriverError(`${frequencyHz} Hz is outside the uploaded spectrum`)
}

export const dbuvToVolts = (dbuv: number): number => 10 ** (dbuv / 20) * 1e-6

export function voltsToDbuv(volts: number): number {
  if (!(volts > 0)) throw new DriverError('a level of zero has no dBuV value')
  return 20 * Math.log10(volts / 1e-6)
}

/** The two envelope corners, for the preview. Trapezoids only. */
export function describeCorners(driver: Driver): { f1: number; f2: number } | null {
  if (driver.kind !== 'trapezoid') return null
  return cornerFrequencies(driverTrapezoid(driver))
}
