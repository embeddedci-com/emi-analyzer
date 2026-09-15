/**
 * What a cable is predicted to radiate, against the limit it has to stay under (§7, §17).
 *
 * The budget chart next to this one answers "how much current may this cable carry"; this one
 * answers "how much is this layout putting into it". They are deliberately separate charts:
 * the budget is a property of the cable and the room and exists before any solve, while this
 * needs a solve, a driver and the antenna solver together, and is the first number in the tool
 * that can be wrong about a real board rather than merely pessimistic.
 *
 * Two lines and the gap between them. The gap is the margin, which is the only quantity anyone
 * acts on, so it is filled rather than left to be read off two axes — red where the prediction
 * is over the limit.
 *
 * Plain SVG, like the other previews here.
 */

import { Group, Stack, Text } from '@mantine/core'
import {
  fieldDbuv, marginDb, worstPoint, type Emission,
} from '../lib/cableEmission'

const W = 360
const H = 185
const PAD = { left: 34, right: 12, top: 20, bottom: 28 }

const fmtHz = (f: number) =>
  f >= 1e9
    ? `${(f / 1e9).toFixed(1)} GHz`
    : `${f / 1e6 >= 10 ? (f / 1e6).toFixed(0) : (f / 1e6).toFixed(1)} MHz`

export function CableEmissionChart({ emission }: { emission: Emission }) {
  const points = emission.points.filter((p) => Number.isFinite(fieldDbuv(p)))
  if (points.length < 2) return null

  const fLo = points[0].frequency_hz
  const fHi = points[points.length - 1].frequency_hz
  const levels = points.flatMap((p) => [fieldDbuv(p), p.limit_dbuv_per_m])
  const top = Math.ceil(Math.max(...levels) / 10) * 10 + 5
  const bottom = Math.floor(Math.min(...levels) / 10) * 10 - 5

  const x = (f: number) =>
    PAD.left +
    ((Math.log10(f) - Math.log10(fLo)) / (Math.log10(fHi) - Math.log10(fLo))) *
      (W - PAD.left - PAD.right)
  const y = (v: number) => PAD.top + ((top - v) / (top - bottom)) * (H - PAD.top - PAD.bottom)

  const line = (get: (p: (typeof points)[number]) => number) =>
    points
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.frequency_hz).toFixed(1)},${y(get(p)).toFixed(1)}`)
      .join(' ')

  // The margin band: field along the top, back along the limit. Split into runs of the same
  // sign so a segment that crosses the limit is not painted one colour for its whole length.
  const bands: { over: boolean; d: string }[] = []
  let run: typeof points = []
  let runOver = marginDb(points[0]) < 0
  const flush = () => {
    if (run.length < 2) return
    const up = run.map((p) => `${x(p.frequency_hz).toFixed(1)},${y(fieldDbuv(p)).toFixed(1)}`)
    const down = [...run]
      .reverse()
      .map((p) => `${x(p.frequency_hz).toFixed(1)},${y(p.limit_dbuv_per_m).toFixed(1)}`)
    bands.push({ over: runOver, d: `M${up.join(' L')} L${down.join(' L')} Z` })
  }
  for (const p of points) {
    const over = marginDb(p) < 0
    if (over !== runOver) {
      run.push(p)
      flush()
      run = [p]
      runOver = over
    } else {
      run.push(p)
    }
  }
  flush()

  const decades: number[] = []
  for (let d = Math.ceil(Math.log10(fLo)); d <= Math.floor(Math.log10(fHi)); d++) {
    decades.push(10 ** d)
  }
  const rows: number[] = []
  for (let v = Math.ceil(bottom / 10) * 10; v <= top; v += 10) rows.push(v)

  const worst = worstPoint(emission)

  return (
    <Stack gap={4}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label={`Predicted field from ${emission.ref} against the ${emission.standardId} limit`}
      >
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

        {bands.map((b, i) => (
          <path key={i} d={b.d} fill={b.over ? 'var(--mantine-color-red-6)'
                                             : 'var(--mantine-color-teal-6)'}
                fillOpacity={b.over ? 0.22 : 0.13} stroke="none" />
        ))}

        <path d={line((p) => p.limit_dbuv_per_m)} fill="none"
              stroke="var(--mantine-color-red-7)" strokeWidth={1.4} strokeDasharray="4 3" />
        <path d={line(fieldDbuv)} fill="none"
              stroke="var(--mantine-color-blue-7)" strokeWidth={1.8} />

        {worst && (
          <g>
            <circle cx={x(worst.frequency_hz)} cy={y(fieldDbuv(worst))} r={3}
                    fill="var(--mantine-color-blue-7)" />
            <text x={x(worst.frequency_hz)} y={y(fieldDbuv(worst)) - 7} textAnchor="middle"
                  fontSize={10} fill="currentColor" fillOpacity={0.7}>
              {marginDb(worst) >= 0 ? `+${marginDb(worst).toFixed(1)}` : marginDb(worst).toFixed(1)} dB
            </text>
          </g>
        )}
        <text x={4} y={11} fontSize={10} fill="currentColor" fillOpacity={0.55}>dBµV/m</text>
      </svg>

      <Group gap="md">
        <Text size="xs" c="blue.7">— predicted at {emission.distanceM} m</Text>
        <Text size="xs" c="red.7">– – limit</Text>
        {worst && (
          <Text size="xs" c={marginDb(worst) < 0 ? 'red.7' : 'dimmed'}>
            worst {marginDb(worst) >= 0 ? 'margin' : 'overshoot'}{' '}
            {Math.abs(marginDb(worst)).toFixed(1)} dB at {fmtHz(worst.frequency_hz)}
          </Text>
        )}
        {emission.undriven.size > 0 && (
          <Text size="xs" c="dimmed">
            {emission.undriven.size} frequenc{emission.undriven.size === 1 ? 'y' : 'ies'} not modelled
          </Text>
        )}
      </Group>
    </Stack>
  )
}
