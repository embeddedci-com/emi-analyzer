/**
 * A small waveform chart for the ESD panel: the pin voltage of each variant of a line.
 *
 * Inline SVG rather than a charting library: three polylines and two axes do not justify a
 * dependency, and the side panel is 340 px wide, so the chart has to be drawn for that width.
 */

import { Group, Text } from '@mantine/core'

export interface ChartSeries {
  label: string
  values: number[]
  color: string
}

export interface TransientChartProps {
  t: number[]
  series: ChartSeries[]
  unit: string
  /** Show only the first part of the waveform, where the peak is. */
  windowNs?: number
  height?: number
}

const W = 300
const PAD = { l: 36, r: 8, t: 8, b: 20 }

function niceTick(v: number): string {
  const a = Math.abs(v)
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(0) : v.toFixed(1)
}

export function TransientChart({ t, series, unit, windowNs = 10, height = 150 }: TransientChartProps) {
  const H = height
  const idx = t.map((x, i) => [x, i] as const).filter(([x]) => x <= windowNs).map(([, i]) => i)
  if (idx.length < 2 || series.length === 0) return null

  let min = 0
  let max = 0
  for (const s of series) {
    for (const i of idx) {
      const v = s.values[i]
      if (v < min) min = v
      if (v > max) max = v
    }
  }
  if (max - min < 1e-9) max = min + 1
  const span = max - min
  min -= span * 0.05
  max += span * 0.05

  const sx = (x: number) => PAD.l + (x / windowNs) * (W - PAD.l - PAD.r)
  const sy = (y: number) => PAD.t + (1 - (y - min) / (max - min)) * (H - PAD.t - PAD.b)
  const yTicks = [max, (max + min) / 2, min]
  const xTicks = [0, windowNs / 2, windowNs]

  return (
    <div>
      {/* The unit sits above the plot. Drawn inside it, it covered the top tick — the one
          label that says how high the peak goes. */}
      <Text size="10px" c="dimmed" mb={2}>{unit}</Text>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label={`Pin voltage over the first ${windowNs} ns for ${series.map((s) => s.label).join(', ')}`}
        style={{ display: 'block', color: 'var(--mantine-color-dimmed)' }}
      >
        {yTicks.map((y) => (
          <g key={`y${y}`}>
            <line x1={PAD.l} x2={W - PAD.r} y1={sy(y)} y2={sy(y)} stroke="currentColor" strokeOpacity={0.15} />
            <text x={PAD.l - 4} y={sy(y) + 3} fontSize={9} textAnchor="end" fill="currentColor">
              {niceTick(y)}
            </text>
          </g>
        ))}
        {min < 0 && max > 0 && (
          <line x1={PAD.l} x2={W - PAD.r} y1={sy(0)} y2={sy(0)} stroke="currentColor" strokeOpacity={0.45} />
        )}
        {xTicks.map((x) => (
          <text key={`x${x}`} x={sx(x)} y={H - 6} fontSize={9} textAnchor="middle" fill="currentColor">
            {x} ns
          </text>
        ))}
        {series.map((s) => (
          <polyline
            key={s.label}
            fill="none"
            stroke={s.color}
            strokeWidth={1.4}
            strokeLinejoin="round"
            points={idx.map((i) => `${sx(t[i]).toFixed(1)},${sy(s.values[i]).toFixed(1)}`).join(' ')}
          />
        ))}
      </svg>
      <Group gap="sm" mt={2}>
        {series.map((s) => (
          <Group key={s.label} gap={4} wrap="nowrap">
            <span style={{ width: 10, height: 2, background: s.color, display: 'inline-block' }} />
            <Text size="10px" c="dimmed">{s.label}</Text>
          </Group>
        ))}
      </Group>
    </div>
  )
}
