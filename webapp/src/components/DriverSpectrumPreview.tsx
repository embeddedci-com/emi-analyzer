/**
 * The live preview a driver form draws while someone types (§9.4).
 *
 * Shows the line spectrum, the envelope, and both corners — `1/(πτ)` and `1/(πt_r)` — which
 * are the two numbers that decide everything above the fundamental. A form that only showed
 * the fields would let someone enter a rise time three decades away from what they meant and
 * see nothing wrong until a compliance report was already built on it.
 *
 * Plain SVG rather than a chart library: two log axes, a stem plot and two rules. Pulling in
 * a charting dependency for that would cost more than it saves.
 */

import { Group, Stack, Text } from '@mantine/core'
import { cornerFrequencies, envelopeV, trapezoidSeries, type Trapezoid } from '../lib/driverSpectrum'
import { voltsToDbuv } from '../lib/driverResolve'

const W = 520
const H = 180
// top leaves room for the unit label above the first gridline label, which would
// otherwise overlap it and read as one run-together number.
const PAD = { left: 44, right: 10, top: 22, bottom: 26 }

export interface PreviewProps {
  trapezoid: Trapezoid
  harmonics?: number
  /** Solve frequencies to mark, so the user can see which land on harmonics. */
  marks?: number[]
}

export function DriverSpectrumPreview({ trapezoid, harmonics = 60, marks = [] }: PreviewProps) {
  let series: { frequency_hz: number; amplitude_v: number }[]
  let corners: { f1: number; f2: number }
  try {
    series = trapezoidSeries(trapezoid, harmonics)
    corners = cornerFrequencies(trapezoid)
  } catch {
    return (
      <Text size="xs" c="dimmed">
        The preview appears once the numbers describe a possible waveform.
      </Text>
    )
  }

  const driven = series.filter((p) => p.amplitude_v > 0)
  if (driven.length === 0) return null

  const fMin = series[0].frequency_hz
  const fMax = series[series.length - 1].frequency_hz
  // A floor 80 dB below the fundamental: spectral nulls go to zero and would otherwise take
  // the axis with them.
  const top = voltsToDbuv(Math.max(...driven.map((p) => p.amplitude_v)))
  const bottom = top - 80

  const x = (f: number) =>
    PAD.left +
    ((Math.log10(f) - Math.log10(fMin)) / (Math.log10(fMax) - Math.log10(fMin))) *
      (W - PAD.left - PAD.right)
  const y = (db: number) =>
    PAD.top + ((top - db) / (top - bottom)) * (H - PAD.top - PAD.bottom)
  const clamp = (db: number) => Math.max(bottom, Math.min(top, db))

  const envelopePath = series
    .map((p, i) => {
      const db = clamp(voltsToDbuv(envelopeV(trapezoid, p.frequency_hz)))
      return `${i === 0 ? 'M' : 'L'}${x(p.frequency_hz).toFixed(1)},${y(db).toFixed(1)}`
    })
    .join(' ')

  const decades: number[] = []
  for (let d = Math.ceil(Math.log10(fMin)); d <= Math.floor(Math.log10(fMax)); d++) {
    decades.push(10 ** d)
  }

  const fmtHz = (f: number) =>
    f >= 1e9 ? `${(f / 1e9).toPrecision(3)} GHz`
      : f >= 1e6 ? `${(f / 1e6).toPrecision(3)} MHz`
        : `${(f / 1e3).toPrecision(3)} kHz`

  return (
    <Stack gap={4}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label="Driver spectrum with envelope and corner frequencies">
        {/* horizontal grid, every 20 dB */}
        {Array.from({ length: 5 }, (_, i) => top - i * 20).map((db) => (
          <g key={db}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(db)} y2={y(db)}
                  stroke="currentColor" strokeOpacity={0.12} />
            <text x={PAD.left - 6} y={y(db) + 3} textAnchor="end" fontSize={9}
                  fill="currentColor" fillOpacity={0.55}>{db.toFixed(0)}</text>
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

        {/* the two corners, which are the point of this preview */}
        {[
          { f: corners.f1, label: '1/πτ' },
          { f: corners.f2, label: '1/πt_r' },
        ].filter((c) => c.f >= fMin && c.f <= fMax).map((c) => (
          <g key={c.label}>
            <line x1={x(c.f)} x2={x(c.f)} y1={PAD.top} y2={H - PAD.bottom}
                  stroke="var(--mantine-color-orange-6)" strokeDasharray="3 3" />
            <text x={x(c.f) + 3} y={PAD.top + 10} fontSize={9}
                  fill="var(--mantine-color-orange-7)">{c.label}</text>
          </g>
        ))}

        <path d={envelopePath} fill="none" stroke="var(--mantine-color-blue-4)"
              strokeWidth={1.5} strokeDasharray="4 3" />

        {/* the line spectrum itself */}
        {series.map((p) => {
          if (p.amplitude_v <= 0) return null
          const db = voltsToDbuv(p.amplitude_v)
          if (db < bottom) return null
          return (
            <line key={p.frequency_hz} x1={x(p.frequency_hz)} x2={x(p.frequency_hz)}
                  y1={y(clamp(db))} y2={H - PAD.bottom}
                  stroke="var(--mantine-color-blue-7)" strokeWidth={1.2} />
          )
        })}

        {marks.filter((f) => f >= fMin && f <= fMax).map((f) => (
          <circle key={`mark-${f}`} cx={x(f)} cy={H - PAD.bottom} r={2.5}
                  fill="var(--mantine-color-teal-6)" />
        ))}

        <text x={4} y={11} fontSize={9} fill="currentColor" fillOpacity={0.55}>dBµV</text>
      </svg>

      <Group gap="md">
        <Text size="xs" c="dimmed">
          Corners: <Text span ff="monospace">{fmtHz(corners.f1)}</Text> and{' '}
          <Text span ff="monospace">{fmtHz(corners.f2)}</Text>
        </Text>
        {marks.length > 0 && (
          <Text size="xs" c="teal">● the solve&rsquo;s frequencies</Text>
        )}
      </Group>
    </Stack>
  )
}
