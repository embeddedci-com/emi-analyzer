/**
 * A cable's current budget against frequency, with its radiation peaks marked.
 *
 * The line is "how much common-mode current this cable may carry before it reaches the
 * limit". It dips where the cable radiates best, which is the whole point: those dips are the
 * frequencies a clock harmonic must not land on.
 *
 * Plain SVG, like the other two previews here. A log frequency axis and one line did not
 * justify a charting dependency.
 */

import { Group, Stack, Text } from '@mantine/core'
import type { CableBudgetPoint } from '../lib/cableTypes'

const W = 360
const H = 170
const PAD = { left: 34, right: 12, top: 20, bottom: 28 }

const fmtHz = (f: number) =>
  f >= 1e9 ? `${(f / 1e9).toFixed(1)} GHz` : `${f / 1e6 >= 10 ? (f / 1e6).toFixed(0) : (f / 1e6).toFixed(1)} MHz`

export function CableBudgetChart({
  points, peaks = [], marks = [],
}: {
  points: CableBudgetPoint[]
  peaks?: number[]
  /** Clock harmonics, so it is visible whether any lands in a dip. */
  marks?: number[]
}) {
  if (points.length < 2) return null

  const fLo = points[0].frequency_hz
  const fHi = points[points.length - 1].frequency_hz
  const values = points.map((p) => p.max_current_dbua).filter((v) => Number.isFinite(v))
  if (values.length < 2) return null
  const top = Math.ceil(Math.max(...values) / 10) * 10
  const bottom = Math.floor(Math.min(...values) / 10) * 10 - 5

  const x = (f: number) =>
    PAD.left + ((Math.log10(f) - Math.log10(fLo)) / (Math.log10(fHi) - Math.log10(fLo))) *
      (W - PAD.left - PAD.right)
  const y = (v: number) =>
    PAD.top + ((top - v) / (top - bottom)) * (H - PAD.top - PAD.bottom)

  const path = points
    .filter((p) => Number.isFinite(p.max_current_dbua))
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.frequency_hz).toFixed(1)},${y(p.max_current_dbua).toFixed(1)}`)
    .join(' ')

  const decades: number[] = []
  for (let d = Math.ceil(Math.log10(fLo)); d <= Math.floor(Math.log10(fHi)); d++) decades.push(10 ** d)
  const rows: number[] = []
  for (let v = bottom + 5; v <= top; v += 10) rows.push(v)

  return (
    <Stack gap={4}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label="Allowed common-mode current against frequency, with radiation peaks marked">
        {rows.map((v) => (
          <g key={v}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(v)} y2={y(v)}
                  stroke="currentColor" strokeOpacity={0.12} />
            <text x={PAD.left - 5} y={y(v) + 3} textAnchor="end" fontSize={10}
                  fill="currentColor" fillOpacity={0.55}>{v}</text>
          </g>
        ))}
        {decades.map((f) => (
          <g key={f}>
            <line x1={x(f)} x2={x(f)} y1={PAD.top} y2={H - PAD.bottom}
                  stroke="currentColor" strokeOpacity={0.12} />
            <text x={x(f)} y={H - PAD.bottom + 12} textAnchor="middle" fontSize={10}
                  fill="currentColor" fillOpacity={0.55}>{fmtHz(f)}</text>
          </g>
        ))}

        {peaks.filter((f) => f >= fLo && f <= fHi).map((f) => (
          <line key={`peak-${f}`} x1={x(f)} x2={x(f)} y1={PAD.top} y2={H - PAD.bottom}
                stroke="var(--mantine-color-orange-6)" strokeDasharray="3 3" />
        ))}
        {marks.filter((f) => f >= fLo && f <= fHi).map((f) => (
          <circle key={`mark-${f}`} cx={x(f)} cy={H - PAD.bottom} r={2.5}
                  fill="var(--mantine-color-teal-6)" />
        ))}

        <path d={path} fill="none" stroke="var(--mantine-color-blue-7)" strokeWidth={1.6} />
        <text x={4} y={11} fontSize={10} fill="currentColor" fillOpacity={0.55}>dBµA</text>
      </svg>

      <Group gap="md">
        <Text size="xs" c="dimmed">
          <Text span c="orange.7">— —</Text> where it radiates best
        </Text>
        {marks.length > 0 && <Text size="xs" c="teal">● clock harmonics</Text>}
        <Text size="xs" c="dimmed">lower is less headroom</Text>
      </Group>
    </Stack>
  )
}
