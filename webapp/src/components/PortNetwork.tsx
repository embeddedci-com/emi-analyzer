/**
 * What a small-part solve measured at its ports: the impedance the driver sees, and how much
 * reaches the far end.
 *
 * Read from `network.json` (worker `openems/network.py`). A frequency the run did not resolve
 * is `null` there, and is left as a gap in the line rather than drawn: the worker already
 * decided it is not a number.
 */

import { useEffect, useState } from 'react'
import { Alert, Group, Stack, Table, Text } from '@mantine/core'
import type { EmiApi } from '../lib/emiApi'
import { formatHz } from '../lib/portPlacement'

export interface PortNetworkDoc {
  format: string
  format_version: number
  reference_impedance_ohm: number
  frequencies_hz: number[]
  driven: { name: string; net?: string; pad?: string }
  z_in_real: (number | null)[]
  z_in_imag: (number | null)[]
  s11_db: (number | null)[]
  transmission: { to: string; net?: string; pad?: string; s_db: (number | null)[] }[]
  usable: boolean[]
  truncated_hz: number[]
  unusable_reason: string | null
}

const W = 360
const H = 150
const PAD = { left: 38, right: 12, top: 16, bottom: 26 }

const fmtAxis = (f: number) =>
  f >= 1e9 ? `${(f / 1e9).toFixed(f % 1e9 ? 1 : 0)} GHz` : `${(f / 1e6).toFixed(0)} MHz`

/** One log-frequency chart of one or more lines, with gaps where a value is null. */
function Chart({
  f, lines, unit, log = false, label,
}: {
  f: number[]
  lines: { values: (number | null)[]; color: string; name: string }[]
  unit: string
  log?: boolean
  label: string
}) {
  const all = lines.flatMap((l) => l.values).filter((v): v is number => v !== null && Number.isFinite(v))
  if (f.length < 2 || all.length < 2) return null
  const tf = (v: number) => (log ? Math.log10(Math.max(v, 1e-3)) : v)
  let lo = Math.min(...all.map(tf))
  let hi = Math.max(...all.map(tf))
  if (log) {
    lo = Math.floor(lo)
    hi = Math.ceil(hi)
  } else {
    lo = Math.floor(lo / 10) * 10
    hi = Math.max(lo + 10, Math.ceil(hi / 10) * 10)
  }
  if (hi === lo) hi = lo + 1
  const fLo = f[0], fHi = f[f.length - 1]
  const x = (v: number) =>
    PAD.left + ((Math.log10(v) - Math.log10(fLo)) / (Math.log10(fHi) - Math.log10(fLo))) * (W - PAD.left - PAD.right)
  const y = (v: number) => PAD.top + ((hi - tf(v)) / (hi - lo)) * (H - PAD.top - PAD.bottom)
  const path = (values: (number | null)[]) => {
    let d = ''
    let pen = false
    values.forEach((v, i) => {
      if (v === null || !Number.isFinite(v)) {
        pen = false
        return
      }
      d += `${pen ? 'L' : 'M'}${x(f[i]).toFixed(1)},${y(v).toFixed(1)} `
      pen = true
    })
    return d
  }
  const rows: number[] = []
  if (log) for (let k = lo; k <= hi; k++) rows.push(10 ** k)
  else for (let v = lo; v <= hi; v += (hi - lo) > 40 ? 20 : 10) rows.push(v)
  const ticks: number[] = []
  for (const m of [1, 2, 5]) {
    for (let d = Math.floor(Math.log10(fLo)); d <= Math.ceil(Math.log10(fHi)); d++) {
      const t = m * 10 ** d
      if (t >= fLo * 0.999 && t <= fHi * 1.001) ticks.push(t)
    }
  }
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={label}>
      {rows.map((v) => (
        <g key={v}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(v)} y2={y(v)} stroke="currentColor" strokeOpacity={0.12} />
          <text x={PAD.left - 5} y={y(v) + 3} textAnchor="end" fontSize={10} fill="currentColor" fillOpacity={0.55}>
            {log ? (v >= 1000 ? `${v / 1000}k` : v) : v}
          </text>
        </g>
      ))}
      {ticks.map((t) => (
        <g key={t}>
          <line x1={x(t)} x2={x(t)} y1={PAD.top} y2={H - PAD.bottom} stroke="currentColor" strokeOpacity={0.12} />
          <text x={x(t)} y={H - PAD.bottom + 12} textAnchor="middle" fontSize={10} fill="currentColor" fillOpacity={0.55}>
            {fmtAxis(t)}
          </text>
        </g>
      ))}
      {lines.map((l) => (
        <path key={l.name} d={path(l.values)} fill="none" stroke={l.color} strokeWidth={1.6} />
      ))}
      <text x={4} y={11} fontSize={10} fill="currentColor" fillOpacity={0.55}>{unit}</text>
    </svg>
  )
}

export function PortNetwork({ api, runId }: { api: EmiApi; runId: string }) {
  const [doc, setDoc] = useState<PortNetworkDoc | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setError(null)
    api.artifactJson<PortNetworkDoc>(runId, 'network.json')
      .then((d) => !cancelled && setDoc(d))
      .catch((e) => !cancelled && setError((e as Error).message))
    return () => {
      cancelled = true
    }
  }, [api, runId])

  if (error) {
    return <Alert color="red" variant="light" title="Could not load the port results">{error}</Alert>
  }
  if (!doc) return null

  const f = doc.frequencies_hz
  const zMag = doc.z_in_real.map((r, i) => {
    const im = doc.z_in_imag[i]
    return r === null || im === null ? null : Math.hypot(r, im)
  })
  const who = (p: { name: string; pad?: string }) => (p.pad ? `${p.name} (${p.pad})` : p.name)
  const through = doc.transmission[0]
  // A few rows across the band, for reading numbers off rather than a line.
  const rows = [0, 10, 20, 30, 40, 50, f.length - 1].filter((i, k, a) => i < f.length && a.indexOf(i) === k)

  return (
    <Stack gap="xs">
      <Text size="xs" fw={600} tt="uppercase" c="dimmed">Ports</Text>
      {doc.unusable_reason && (
        <Alert color="red" variant="light" title="No port numbers from this run">
          <Text size="xs">{doc.unusable_reason}</Text>
        </Alert>
      )}
      {!doc.unusable_reason && doc.truncated_hz.length > 0 && (
        <Text size="xs" c="orange">
          {doc.truncated_hz.length} of {f.length} frequencies were dropped: the run stopped
          while the part was still ringing there. They are gaps in the lines.
        </Text>
      )}

      <Text size="xs">
        Impedance into {who(doc.driven)}, with every other port a {doc.reference_impedance_ohm} Ω load.
      </Text>
      <Chart f={f} unit="|Z| Ω" log label="Input impedance against frequency"
             lines={[{ values: zMag, color: 'var(--mantine-color-blue-7)', name: 'z' }]} />

      <Group gap="md">
        <Text size="xs" c="blue.7">— S11 at {doc.driven.name}</Text>
        {through && <Text size="xs" c="teal.7">— S21 to {who({ name: through.to, pad: through.pad })}</Text>}
      </Group>
      <Chart f={f} unit="dB" label="S-parameters against frequency"
             lines={[
               { values: doc.s11_db, color: 'var(--mantine-color-blue-7)', name: 's11' },
               ...(through ? [{ values: through.s_db, color: 'var(--mantine-color-teal-7)', name: 's21' }] : []),
             ]} />

      <Table verticalSpacing={2} fz="xs">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>f</Table.Th>
            <Table.Th ta="right">Z</Table.Th>
            <Table.Th ta="right">S11</Table.Th>
            {through && <Table.Th ta="right">S21</Table.Th>}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((i) => {
            const r = doc.z_in_real[i], im = doc.z_in_imag[i]
            return (
              <Table.Tr key={i}>
                <Table.Td>{formatHz(f[i])}</Table.Td>
                <Table.Td ta="right" ff="monospace">
                  {r === null || im === null ? '—' : `${r.toFixed(1)}${im >= 0 ? '+' : '−'}${Math.abs(im).toFixed(1)}j`}
                </Table.Td>
                <Table.Td ta="right" ff="monospace">
                  {doc.s11_db[i] === null ? '—' : `${doc.s11_db[i]!.toFixed(1)} dB`}
                </Table.Td>
                {through && (
                  <Table.Td ta="right" ff="monospace">
                    {through.s_db[i] === null ? '—' : `${through.s_db[i]!.toFixed(2)} dB`}
                  </Table.Td>
                )}
              </Table.Tr>
            )
          })}
        </Table.Tbody>
      </Table>
    </Stack>
  )
}
