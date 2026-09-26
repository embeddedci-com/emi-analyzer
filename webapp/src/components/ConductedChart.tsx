/**
 * The conducted spectrum: each harmonic at the LISN as a line from the floor, the FCC 15.107
 * quasi-peak and average limits, and optionally a what-if's harmonics as dots on top.
 *
 * Inline SVG, like TransientChart: the side panel is 340 px wide and a line chart of a few
 * dozen stems does not justify a charting library.
 */

import { Group, Text } from '@mantine/core'
import { limitLine } from '../lib/limits'
import { fmtHz, xOf } from '../lib/conducted'
import type { ConductedLine } from '../lib/conductedTypes'

const W = 300
const PAD = { l: 30, r: 8, t: 8, b: 20 }
const OVER = 'var(--mantine-color-red-6)'
const UNDER = 'var(--mantine-color-blue-6)'
const WHAT_IF = 'var(--mantine-color-teal-6)'

export interface ConductedChartProps {
  lines: ConductedLine[]
  quasiPeak: string
  average: string
  compare?: { label: string; lines: ConductedLine[] } | null
  height?: number
}

export function ConductedChart({ lines, quasiPeak, average, compare, height = 180 }: ConductedChartProps) {
  const H = height
  const qp = limitLine(quasiPeak)
  const avg = limitLine(average)
  const levels = [...lines, ...(compare?.lines ?? [])].map((l) => l.dbuv)
  const limitLevels = [...qp, ...avg].map((p) => p.level_db)
  const top = Math.ceil((Math.max(...levels, ...limitLevels) + 5) / 10) * 10
  // The floor is 40 dB under the lowest limit: a harmonic further down than that decides
  // nothing, and scaling the axis to reach it squashed the limits into the top third.
  const lowestLimit = Math.min(...limitLevels)
  const bottom = Math.floor(Math.max(Math.min(...levels, lowestLimit - 10), lowestLimit - 40) / 10) * 10
  const x0 = PAD.l
  const x1 = W - PAD.r
  const sy = (db: number) => PAD.t + (1 - (Math.max(db, bottom) - bottom) / (top - bottom)) * (H - PAD.t - PAD.b)
  const path = (pts: { frequency_hz: number; level_db: number }[]) =>
    pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${xOf(p.frequency_hz, x0, x1).toFixed(1)},${sy(p.level_db).toFixed(1)}`).join(' ')
  const yTicks: number[] = []
  for (let v = bottom; v <= top; v += top - bottom > 60 ? 20 : 10) yTicks.push(v)
  const xTicks = [150e3, 500e3, 1e6, 5e6, 10e6, 30e6]
  const over = lines.filter((l) => l.margin_avg_db < 0).length

  return (
    <div>
      <Text size="10px" c="dimmed" mb={2}>dBµV at the LISN</Text>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label={`Conducted spectrum, ${lines.length} harmonics, ${over} over the average limit`}
        style={{ display: 'block', color: 'var(--mantine-color-dimmed)' }}
      >
        {yTicks.map((y) => (
          <g key={`y${y}`}>
            <line x1={x0} x2={x1} y1={sy(y)} y2={sy(y)} stroke="currentColor" strokeOpacity={0.12} />
            <text x={x0 - 4} y={sy(y) + 3} fontSize={9} textAnchor="end" fill="currentColor">{y}</text>
          </g>
        ))}
        {xTicks.map((f) => (
          <text key={`x${f}`} x={xOf(f, x0, x1)} y={H - 6} fontSize={9} textAnchor="middle" fill="currentColor">
            {fmtHz(f).replace(' kHz', 'k').replace('.00 MHz', 'M').replace('.0 MHz', 'M')}
          </text>
        ))}
        {lines.map((l) => (
          <line
            key={l.f_hz}
            x1={xOf(l.f_hz, x0, x1)} x2={xOf(l.f_hz, x0, x1)}
            y1={sy(bottom)} y2={sy(l.dbuv)}
            stroke={l.margin_avg_db < 0 ? OVER : UNDER}
            strokeWidth={1.2}
          />
        ))}
        {compare?.lines.map((l) => (
          <circle key={`c${l.f_hz}`} cx={xOf(l.f_hz, x0, x1)} cy={sy(l.dbuv)} r={1.8} fill={WHAT_IF} />
        ))}
        <path d={path(qp)} fill="none" stroke="currentColor" strokeWidth={1.2} strokeDasharray="4 3" />
        <path d={path(avg)} fill="none" stroke="currentColor" strokeWidth={1.2} />
      </svg>
      <Group gap="sm" mt={2}>
        <Legend color={UNDER} label="As laid out" />
        {over > 0 && <Legend color={OVER} label="Over the average limit" />}
        {compare && <Legend color={WHAT_IF} label={compare.label} dot />}
        <Legend color="var(--mantine-color-dimmed)" label="Average limit" />
        <Legend color="var(--mantine-color-dimmed)" label="Quasi-peak limit" dashed />
      </Group>
    </div>
  )
}

function Legend({ color, label, dashed, dot }: { color: string; label: string; dashed?: boolean; dot?: boolean }) {
  return (
    <Group gap={4} wrap="nowrap">
      <span
        style={{
          width: dot ? 5 : 10, height: dot ? 5 : 0, borderRadius: dot ? 3 : 0,
          background: dot ? color : undefined,
          borderTop: dot ? undefined : `2px ${dashed ? 'dashed' : 'solid'} ${color}`,
          display: 'inline-block',
        }}
      />
      <Text size="10px" c="dimmed">{label}</Text>
    </Group>
  )
}
