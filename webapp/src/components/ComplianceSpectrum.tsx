/**
 * The predicted spectrum against the limit line (§16, §17.3).
 *
 * Drawn whether or not the inputs are complete, because seeing which frequencies sit close is
 * useful long before there is a margin to quote — but drawn **greyed** when incomplete, so the
 * shape is readable without implying the level is settled.
 *
 * The prediction is drawn as **stems, not a line**. A clock is a line spectrum: it has energy
 * at its harmonics and none between them, and a line joining the harmonics draws a level at
 * every frequency in between that nothing produces. The limit is drawn from the table's own
 * segments, so its band-edge steps stay vertical.
 */

import { Group, Stack, Text } from '@mantine/core'
import { fmtHz, type ComplianceDoc } from '../lib/complianceTypes'
import { limitLine } from '../lib/limits'

const W = 360
const H = 190
const PAD = { left: 34, right: 12, top: 18, bottom: 28 }

export function ComplianceSpectrum({ doc }: { doc: ComplianceDoc }) {
  const pts = doc.spectrum
    .filter((p) => typeof p.field_dbuv_per_m === 'number' && Number.isFinite(p.field_dbuv_per_m))
    .map((p) => ({ ...p, field: p.field_dbuv_per_m as number }))
  if (pts.length < 1 || doc.spectrum.length < 2) return null
  const greyed = !doc.complete

  const fLo = doc.spectrum[0].frequency_hz
  const fHi = doc.spectrum[doc.spectrum.length - 1].frequency_hz
  const limit = limitLine(doc.standard_id)
    .filter((q) => q.frequency_hz >= fLo && q.frequency_hz <= fHi)
  const levels = [...pts.map((p) => p.field), ...doc.spectrum.map((p) => p.limit_dbuv_per_m)]
  const top = Math.ceil(Math.max(...levels) / 10) * 10 + 5
  const bottom = Math.floor(Math.min(...levels) / 10) * 10 - 5

  const x = (f: number) =>
    PAD.left +
    ((Math.log10(f) - Math.log10(fLo)) / (Math.log10(fHi) - Math.log10(fLo))) *
      (W - PAD.left - PAD.right)
  const y = (v: number) => PAD.top + ((top - v) / (top - bottom)) * (H - PAD.top - PAD.bottom)

  // The limit from the table's segments, bounded to the spectrum's span on both ends.
  const limitPts = [
    { frequency_hz: fLo, level_db: doc.spectrum[0].limit_dbuv_per_m },
    ...limit,
    { frequency_hz: fHi, level_db: doc.spectrum[doc.spectrum.length - 1].limit_dbuv_per_m },
  ]
  const limitPath = limitPts
    .map((q, i) => `${i === 0 ? 'M' : 'L'}${x(q.frequency_hz).toFixed(1)},${y(q.level_db).toFixed(1)}`)
    .join(' ')

  const decades: number[] = []
  for (let d = Math.ceil(Math.log10(fLo)); d <= Math.floor(Math.log10(fHi)); d++) decades.push(10 ** d)
  const rows: number[] = []
  for (let v = Math.ceil(bottom / 10) * 10; v <= top; v += 10) rows.push(v)

  const worst = doc.worst
  const misses = doc.near_misses ?? []

  return (
    <Stack gap={4}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label={`Predicted field against the ${doc.standard} limit`}>
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

        {misses.map((m) => (
          <circle key={m.frequency_hz} cx={x(m.frequency_hz)} cy={y(m.field_dbuv_per_m)} r={2.5}
                  fill="var(--mantine-color-orange-6)" />
        ))}

        <path d={limitPath} fill="none"
              stroke="var(--mantine-color-red-7)" strokeWidth={1.4} strokeDasharray="4 3"
              opacity={greyed ? 0.45 : 1} />
        {pts.map((p) => (
          <g key={p.frequency_hz}
             stroke={greyed ? 'var(--mantine-color-gray-5)' : 'var(--mantine-color-blue-7)'}>
            <line x1={x(p.frequency_hz)} x2={x(p.frequency_hz)} y1={y(bottom)} y2={y(p.field)}
                  strokeWidth={1.2} />
            <circle cx={x(p.frequency_hz)} cy={y(p.field)} r={1.6}
                    fill={greyed ? 'var(--mantine-color-gray-5)' : 'var(--mantine-color-blue-7)'} />
          </g>
        ))}

        {worst && !greyed && (
          <g>
            <circle cx={x(worst.frequency_hz)} cy={y(worst.field_dbuv_per_m)} r={3.5}
                    fill="none" stroke="var(--mantine-color-blue-7)" strokeWidth={1.5} />
            <text x={x(worst.frequency_hz)} y={y(worst.field_dbuv_per_m) - 8}
                  textAnchor="middle" fontSize={10} fill="currentColor" fillOpacity={0.75}>
              worst
            </text>
          </g>
        )}
        <text x={4} y={11} fontSize={10} fill="currentColor" fillOpacity={0.55}>dBµV/m</text>
      </svg>

      <Group gap="md">
        <Text size="xs" c={greyed ? 'dimmed' : 'blue.7'}>| predicted (RMS)</Text>
        <Text size="xs" c="red.7">– – {doc.standard} limit</Text>
        {misses.length > 0 && (
          <Text size="xs" c="orange.7">● within one σ of the worst</Text>
        )}
        {greyed && <Text size="xs" c="dimmed">greyed: inputs incomplete</Text>}
      </Group>
    </Stack>
  )
}
