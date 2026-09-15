/**
 * Driver spectra — the browser's copy.
 *
 * The Python worker holds the other implementation (`worker/emi_worker/drivers/spectrum.py`)
 * and both are checked against `server/emi/testdata/driver_fixtures.json`, the cost model's
 * rule. The driver form previews a spectrum live while the user types; the worker then uses
 * that same spectrum to turn a relative solve into absolute dBµA/m. A preview that disagreed
 * with the result would be worse than no preview.
 *
 * A trapezoid is not special-cased here either. It is four breakpoints of a piecewise-linear
 * periodic waveform, whose exact Fourier coefficient has a closed form — which reproduces the
 * textbook sinc-times-sinc result for a symmetric edge, handles an asymmetric rise and fall
 * exactly rather than averaging them, and will transform an uploaded waveform unchanged,
 * since a sampled waveform is piecewise linear between its samples.
 */

export class DriverError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'DriverError'
  }
}

export interface Trapezoid {
  amplitude_v: number
  period_s: number
  /** Width at 50 % amplitude, which is what §9.1's closed form means by tau. */
  pulse_width_s: number
  rise_s: number
  fall_s: number
}

export interface SeriesPoint {
  frequency_hz: number
  /** One-sided peak amplitude of this harmonic, in volts. */
  amplitude_v: number
}

/** Complex arithmetic, kept local: this is the only file in the webapp that needs it. */
interface Complex {
  re: number
  im: number
}

const cAdd = (a: Complex, b: Complex): Complex => ({ re: a.re + b.re, im: a.im + b.im })
const cSub = (a: Complex, b: Complex): Complex => ({ re: a.re - b.re, im: a.im - b.im })
const cMul = (a: Complex, b: Complex): Complex => ({
  re: a.re * b.re - a.im * b.im,
  im: a.re * b.im + a.im * b.re,
})
const cScale = (a: Complex, k: number): Complex => ({ re: a.re * k, im: a.im * k })
const cAbs = (a: Complex): number => Math.hypot(a.re, a.im)

const cDiv = (a: Complex, b: Complex): Complex => {
  const d = b.re * b.re + b.im * b.im
  return { re: (a.re * b.re + a.im * b.im) / d, im: (a.im * b.re - a.re * b.im) / d }
}

/** exp(-j*x) */
const cExpNegJ = (x: number): Complex => ({ re: Math.cos(x), im: -Math.sin(x) })

export function flatTopSeconds(t: Trapezoid): number {
  return t.pulse_width_s - (t.rise_s + t.fall_s) / 2
}

export function validateTrapezoid(t: Trapezoid): void {
  if (!(t.period_s > 0)) throw new DriverError('period must be positive')
  if (!(t.rise_s > 0) || !(t.fall_s > 0)) throw new DriverError('rise and fall must be positive')
  if (!(t.pulse_width_s > 0)) throw new DriverError('pulse width must be positive')
  const flat = flatTopSeconds(t)
  if (flat < 0) {
    throw new DriverError(
      `the edges do not fit inside the pulse: a ${(t.pulse_width_s * 1e9).toPrecision(3)} ns ` +
        `pulse at 50 % cannot hold a ${(t.rise_s * 1e9).toPrecision(3)} ns rise and a ` +
        `${(t.fall_s * 1e9).toPrecision(3)} ns fall`,
    )
  }
  if (t.rise_s + flat + t.fall_s > t.period_s) {
    throw new DriverError(
      `one pulse is longer than the period: ` +
        `${((t.rise_s + flat + t.fall_s) * 1e9).toPrecision(4)} ns in a ` +
        `${(t.period_s * 1e9).toPrecision(4)} ns period`,
    )
  }
}

/** Times and values of the waveform's corners, one period starting at t = 0. */
export function breakpoints(t: Trapezoid): { times: number[]; values: number[] } {
  validateTrapezoid(t)
  const flat = flatTopSeconds(t)
  const times = [0, t.rise_s, t.rise_s + flat, t.rise_s + flat + t.fall_s]
  const values = [0, t.amplitude_v, t.amplitude_v, 0]
  if (times[times.length - 1] < t.period_s) {
    times.push(t.period_s)
    values.push(0)
  }
  return { times, values }
}

/**
 * Exact complex Fourier coefficient `C_n` of a periodic piecewise-linear waveform.
 *
 * The integral of `(a + b*u) * exp(-j*w*u)` over a segment is elementary, so there is no
 * quadrature here and the result does not degrade at high harmonic numbers the way a sampled
 * DFT does.
 */
export function piecewiseLinearSeries(
  times: number[],
  values: number[],
  periodS: number,
  n: number,
): Complex {
  if (times.length !== values.length) {
    throw new DriverError('times and values must be the same length')
  }
  if (times.length < 2) throw new DriverError('a waveform needs at least two points')
  if (!(periodS > 0)) throw new DriverError('period must be positive')
  if (n < 0) throw new DriverError('harmonic number must not be negative')

  const t = times.slice()
  const v = values.slice()
  if (t[t.length - 1] < periodS) {
    t.push(periodS)
    v.push(v[v.length - 1])
  }

  const w = (2 * Math.PI * n) / periodS
  let total: Complex = { re: 0, im: 0 }
  for (let k = 0; k < t.length - 1; k++) {
    const length = t[k + 1] - t[k]
    if (length < 0) throw new DriverError('waveform times must not go backwards')
    if (length === 0) continue
    const a = v[k]
    const b = (v[k + 1] - v[k]) / length
    if (n === 0) {
      total = cAdd(total, { re: a * length + (b * length * length) / 2, im: 0 })
      continue
    }
    const e = cExpNegJ(w * length)
    const oneMinusE = cSub({ re: 1, im: 0 }, e)
    const jw: Complex = { re: 0, im: w }
    // a*(1-e)/jw  -  b*L*e/jw  -  b*(1-e)/w^2
    const term = cSub(
      cSub(cDiv(cScale(oneMinusE, a), jw), cDiv(cScale(e, b * length), jw)),
      cScale(oneMinusE, b / (w * w)),
    )
    total = cAdd(total, cMul(cExpNegJ(w * t[k]), term))
  }
  return cScale(total, 1 / periodS)
}

/**
 * `(frequency, amplitude)` for harmonics 1..`harmonics`.
 *
 * The amplitude is the one-sided peak of that harmonic — `2 * |C_n|` — which is §9.1's
 * convention and what a spectrum analyser in peak mode reads. Note this is half the
 * textbook `4A/(n*pi)` quoted for square waves, because those swing between -A and +A while
 * a driver output swings between 0 and A.
 */
export function trapezoidSeries(t: Trapezoid, harmonics: number): SeriesPoint[] {
  validateTrapezoid(t)
  if (harmonics < 1) throw new DriverError('ask for at least one harmonic')
  const { times, values } = breakpoints(t)
  const out: SeriesPoint[] = []
  for (let n = 1; n <= harmonics; n++) {
    const c = piecewiseLinearSeries(times, values, t.period_s, n)
    out.push({ frequency_hz: n / t.period_s, amplitude_v: 2 * cAbs(c) })
  }
  return out
}

/**
 * The envelope's two breakpoints, `1/(pi*tau)` and `1/(pi*t_edge)`.
 *
 * The second uses the **faster** edge: both contribute, the faster dominates above the
 * corner, and taking the slower would put the corner low and under-predict every harmonic
 * above it — the wrong direction for an emissions tool.
 */
export function cornerFrequencies(t: Trapezoid): { f1: number; f2: number } {
  validateTrapezoid(t)
  const edge = Math.min(t.rise_s, t.fall_s)
  return { f1: 1 / (Math.PI * t.pulse_width_s), f2: 1 / (Math.PI * edge) }
}

/**
 * The spectral envelope at one frequency, in volts: flat to the first corner, then
 * -20 dB/decade, then -40 dB/decade. It bounds the line spectrum rather than reproducing
 * it — this is what the preview draws, and what continues an uploaded waveform above its
 * capture bandwidth (§9.3).
 */
export function envelopeV(t: Trapezoid, frequencyHz: number): number {
  validateTrapezoid(t)
  if (!(frequencyHz > 0)) throw new DriverError('envelope is defined above DC')
  const { f1, f2 } = cornerFrequencies(t)
  const flat = (2 * Math.abs(t.amplitude_v) * t.pulse_width_s) / t.period_s
  if (frequencyHz <= f1) return flat
  if (frequencyHz <= f2) return flat * (f1 / frequencyHz)
  return flat * (f1 / f2) * (f2 / frequencyHz) ** 2
}

/**
 * The factor every field at this frequency is multiplied by (§8).
 *
 * `vPort` and `iPort` are what the solve recorded, so their ratio is the input impedance the
 * driver sees and `iPort` is the current the solve used. The driver pushes
 * `vSource / (Zs + Zin)` instead.
 */
export function reweight(
  vSource: Complex,
  sourceImpedanceOhm: number,
  vPort: Complex,
  iPort: Complex,
): Complex {
  if (iPort.re === 0 && iPort.im === 0) {
    throw new DriverError(
      'the solve recorded no current at this port, so there is nothing to re-weight; ' +
        'the frequency is outside the excitation band or the run produced zeros',
    )
  }
  const zIn = cDiv(vPort, iPort)
  const denom = cAdd({ re: sourceImpedanceOhm, im: 0 }, zIn)
  if (denom.re === 0 && denom.im === 0) {
    throw new DriverError('source and input impedance cancel exactly; check the driver')
  }
  return cDiv(cDiv(vSource, denom), iPort)
}

/** `reweight` as a dB offset, which is how the near-field shader applies it (§10). */
export function reweightDb(
  vSource: Complex,
  sourceImpedanceOhm: number,
  vPort: Complex,
  iPort: Complex,
): number {
  const scale = cAbs(reweight(vSource, sourceImpedanceOhm, vPort, iPort))
  if (!(scale > 0)) throw new DriverError('a re-weighting factor of zero has no dB value')
  return 20 * Math.log10(scale)
}

export type { Complex }
