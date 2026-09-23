/**
 * |Z| against frequency for a capacitor model, with the self-resonance marked
 * (docs/implementation.md §3).
 *
 * This exists for one reason, stated in the design doc: a typo of 4 nH for 0.4 nH should show
 * before saving. Ten times the inductance moves the SRF by a factor of three and the whole
 * curve with it, which is obvious on a log-log plot and invisible in a form field.
 *
 * Plain SVG, like the driver preview. Two log axes and a V-shaped curve did not justify a
 * charting dependency.
 */

import { Group, Stack, Text } from '@mantine/core'
import { impedanceAt, selfResonanceHz, type SeriesRLC } from '../lib/componentDocument'

const W = 520
const H = 190
const PAD = { left: 46, right: 10, top: 20, bottom: 26 }

export interface Props {
  rlc: SeriesRLC
  /** Marked on the axis, so it is visible whether the part is still a capacitor there. */
  marks?: number[]
}

const fmtHz = (f: number) =>
  f >= 1e9 ? `${(f / 1e9).toPrecision(3)} GHz`
    : f >= 1e6 ? `${(f / 1e6).toPrecision(3)} MHz`
      : `${(f / 1e3).toPrecision(3)} kHz`

/**
 * Ohms, without exponential notation.
 *
 * `toPrecision(2)` returns "1.0e+2" for 100, so a 0.1 Ω gridline rendered as "1.0e+2 mΩ" and
 * clipped to "0e+2 mΩ" — an axis label that means nothing.
 */
export const fmtOhm = (z: number): string => {
  const scaled = z >= 1 ? [z, 'Ω'] as const
    : z >= 1e-3 ? [z * 1e3, 'mΩ'] as const
      : [z * 1e6, 'µΩ'] as const
  const [v, unit] = scaled
  return `${v >= 10 ? v.toFixed(0) : v.toFixed(1)} ${unit}`
}

export function ComponentImpedancePreview({ rlc, marks = [] }: Props) {
  const srf = selfResonanceHz(rlc)
  if (rlc.esl_h === null || rlc.esr_ohm === null || srf === null || !(rlc.c_f > 0)) {
    return (
      <Text size="xs" c="dimmed">
        The impedance curve appears once this model has a capacitance, an ESL and an ESR.
        Without them there is no self-resonance to show.
      </Text>
    )
  }

  // Four decades either side of the resonance: enough to see both slopes and the notch.
  const fLo = srf / 1e3
  const fHi = srf * 1e3
  const N = 240
  const points: { f: number; z: number }[] = []
  for (let i = 0; i <= N; i++) {
    const f = 10 ** (Math.log10(fLo) + (i / N) * (Math.log10(fHi) - Math.log10(fLo)))
    const z = impedanceAt(rlc, f)
    points.push({ f, z: Math.hypot(z.re, z.im) })
  }

  const zMin = Math.min(...points.map((p) => p.z))
  const zMax = Math.max(...points.map((p) => p.z))
  const top = Math.log10(zMax)
  const bottom = Math.log10(Math.max(zMin, zMax / 1e6))

  const x = (f: number) =>
    PAD.left + ((Math.log10(f) - Math.log10(fLo)) / (Math.log10(fHi) - Math.log10(fLo))) *
      (W - PAD.left - PAD.right)
  const y = (z: number) => {
    const l = Math.max(bottom, Math.min(top, Math.log10(z)))
    return PAD.top + ((top - l) / (top - bottom)) * (H - PAD.top - PAD.bottom)
  }

  const path = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.f).toFixed(1)},${y(p.z).toFixed(1)}`)
    .join(' ')

  const decades: number[] = []
  for (let d = Math.ceil(Math.log10(fLo)); d <= Math.floor(Math.log10(fHi)); d++) {
    decades.push(10 ** d)
  }
  const zDecades: number[] = []
  for (let d = Math.ceil(bottom); d <= Math.floor(top); d++) zDecades.push(10 ** d)

  return (
    <Stack gap={4}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label="Capacitor impedance against frequency, with its self-resonance marked">
        {zDecades.map((z) => (
          <g key={z}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(z)} y2={y(z)}
                  stroke="currentColor" strokeOpacity={0.12} />
            <text x={PAD.left - 5} y={y(z) + 3} textAnchor="end" fontSize={9}
                  fill="currentColor" fillOpacity={0.55}>{fmtOhm(z)}</text>
          </g>
        ))}
        {decades.map((f) => (
          <g key={f}>
            <line x1={x(f)} x2={x(f)} y1={PAD.top} y2={H - PAD.bottom}
                  stroke="currentColor" strokeOpacity={0.12} />
            <text x={x(f)} y={H - PAD.bottom + 12} textAnchor="middle" fontSize={9}
                  fill="currentColor" fillOpacity={0.55}>{fmtHz(f)}</text>
          </g>
        ))}

        <line x1={x(srf)} x2={x(srf)} y1={PAD.top} y2={H - PAD.bottom}
              stroke="var(--mantine-color-orange-6)" strokeDasharray="3 3" />
        <text x={x(srf) + 4} y={PAD.top + 9} fontSize={9}
              fill="var(--mantine-color-orange-7)">SRF</text>

        {marks.filter((f) => f >= fLo && f <= fHi).map((f) => (
          <circle key={`mark-${f}`} cx={x(f)} cy={H - PAD.bottom} r={2.5}
                  fill="var(--mantine-color-teal-6)" />
        ))}

        <path d={path} fill="none" stroke="var(--mantine-color-blue-7)" strokeWidth={1.6} />
        <text x={4} y={11} fontSize={9} fill="currentColor" fillOpacity={0.55}>|Z|</text>
      </svg>

      <Group gap="md">
        <Text size="xs">
          Self-resonates at <Text span fw={600} ff="monospace">{fmtHz(srf)}</Text>
          {' '}— above that it is an inductor.
        </Text>
        <Text size="xs" c="dimmed">
          minimum <Text span ff="monospace">{fmtOhm(rlc.esr_ohm)}</Text>
        </Text>
      </Group>
    </Stack>
  )
}
