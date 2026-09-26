/**
 * |Z| against frequency for one IC's supply: the total, each capacitor alone, the target, and
 * where the total is above it.
 *
 * Plain SVG on the same two log axes as the component preview (ComponentImpedancePreview):
 * one chart of lines and bands did not justify a charting dependency.
 */

import { useMemo, useState } from 'react'
import { Group, Stack, Text } from '@mantine/core'
import {
  bandEnd, branchMag, fmtHz, gapsOf, type DecBranch, type NoiseMark,
} from '../lib/decoupling'
import { fmtOhm } from './ComponentImpedancePreview'

const W = 560
const H = 240
const PAD = { left: 48, right: 12, top: 14, bottom: 30 }

const C = {
  total: 'var(--mantine-color-blue-7)',
  part: 'currentColor',
  selected: 'var(--mantine-color-grape-6)',
  target: 'var(--mantine-color-orange-7)',
  gap: 'var(--mantine-color-red-6)',
  whatIf: 'var(--mantine-color-teal-7)',
  noise: 'var(--mantine-color-teal-7)',
  bad: 'var(--mantine-color-red-7)',
}

export interface DecouplingChartProps {
  freqs: number[]
  total: number[]
  branches: DecBranch[]
  target: number
  bandHz: number
  antiResonances: { hz: number; ohm: number }[]
  noise: NoiseMark[]
  /** A capacitor to draw in color rather than grey. */
  selected?: string | null
  /** A recommendation's curve, drawn dashed over the total. */
  whatIf?: number[] | null
}

export function DecouplingChart({
  freqs, total, branches, target, bandHz, antiResonances, noise, selected = null, whatIf = null,
}: DecouplingChartProps) {
  const [hover, setHover] = useState<number | null>(null)
  const fLo = freqs[0]
  const fHi = freqs[freqs.length - 1]

  const perPart = useMemo(
    () => branches.map((b) => ({ id: b.id, z: freqs.map((f) => branchMag(b, f)) })),
    [branches, freqs],
  )

  // The total, the target and the band decide the scale; a single part's curve may leave it.
  const finite = total.filter(Number.isFinite)
  const zMax = Math.max(...finite, target) * 2
  const zMin = Math.max(Math.min(...finite, target) / 3, zMax / 1e6)
  const top = Math.ceil(Math.log10(zMax))
  const bottom = Math.floor(Math.log10(zMin))

  const x = (f: number) =>
    PAD.left + ((Math.log10(f) - Math.log10(fLo)) / (Math.log10(fHi) - Math.log10(fLo))) *
      (W - PAD.left - PAD.right)
  const y = (z: number) => {
    const l = Math.max(bottom, Math.min(top, Math.log10(Math.max(z, 1e-12))))
    return PAD.top + ((top - l) / (top - bottom)) * (H - PAD.top - PAD.bottom)
  }
  const path = (z: number[]) => z
    .map((v, i) => `${i === 0 ? 'M' : 'L'}${x(freqs[i]).toFixed(1)},${y(v).toFixed(1)}`)
    .join(' ')

  const gaps = gapsOf(freqs, total, target, bandHz)
  const k = bandEnd(freqs, bandHz)
  const bandX = x(freqs[k - 1])
  const decades: number[] = []
  for (let d = Math.ceil(Math.log10(fLo)); d <= Math.floor(Math.log10(fHi)); d++) decades.push(10 ** d)
  const zDecades: number[] = []
  for (let d = bottom; d <= top; d++) zDecades.push(10 ** d)

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const px = ((e.clientX - rect.left) / rect.width) * W
    if (px < PAD.left || px > W - PAD.right) { setHover(null); return }
    let best = 0
    for (let i = 1; i < freqs.length; i++) if (Math.abs(x(freqs[i]) - px) < Math.abs(x(freqs[best]) - px)) best = i
    setHover(best)
  }

  const plotBottom = H - PAD.bottom
  const tip = hover === null ? null : {
    f: freqs[hover], z: total[hover],
    over: hover < k && total[hover] > target,
  }

  return (
    <Stack gap={4}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label="Supply impedance against frequency, with the target and the gaps above it"
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {/* Above the band the package decouples, not the board: shown, not judged. */}
        <rect x={bandX} y={PAD.top} width={W - PAD.right - bandX} height={plotBottom - PAD.top}
              fill="currentColor" fillOpacity={0.05} />
        {bandX < W - PAD.right - 40 && (
          <text x={W - PAD.right - 4} y={PAD.top + 10} textAnchor="end" fontSize={9}
                fill="currentColor" fillOpacity={0.55}>package range</text>
        )}
        {gaps.map(([lo, hi]) => (
          <rect key={`gap-${lo}`} x={x(lo)} y={PAD.top} width={Math.max(x(hi) - x(lo), 2)}
                height={plotBottom - PAD.top} fill={C.gap} fillOpacity={0.12} />
        ))}

        {zDecades.map((z) => (
          <g key={z}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(z)} y2={y(z)}
                  stroke="currentColor" strokeOpacity={0.1} />
            <text x={PAD.left - 5} y={y(z) + 3} textAnchor="end" fontSize={9}
                  fill="currentColor" fillOpacity={0.55}>{fmtOhm(z)}</text>
          </g>
        ))}
        {decades.map((f) => (
          <g key={f}>
            <line x1={x(f)} x2={x(f)} y1={PAD.top} y2={plotBottom}
                  stroke="currentColor" strokeOpacity={0.1} />
            <text x={x(f)} y={plotBottom + 12} textAnchor="middle" fontSize={9}
                  fill="currentColor" fillOpacity={0.55}>{fmtHz(f)}</text>
          </g>
        ))}

        {perPart.map((p) => (
          <path key={p.id} d={path(p.z)} fill="none"
                stroke={p.id === selected ? C.selected : C.part}
                strokeOpacity={p.id === selected ? 1 : 0.22}
                strokeWidth={p.id === selected ? 1.6 : 1}>
            <title>{p.id === 'plane' ? 'Plane pair' : p.id}</title>
          </path>
        ))}

        <line x1={PAD.left} x2={bandX} y1={y(target)} y2={y(target)}
              stroke={C.target} strokeWidth={1.4} strokeDasharray="5 3" />
        <line x1={bandX} x2={W - PAD.right} y1={y(target)} y2={y(target)}
              stroke={C.target} strokeWidth={1} strokeOpacity={0.4} strokeDasharray="2 3" />

        <path d={path(total)} fill="none" stroke={C.total} strokeWidth={2} />
        {whatIf && <path d={path(whatIf)} fill="none" stroke={C.whatIf} strokeWidth={1.6} strokeDasharray="4 3" />}

        {antiResonances.filter((a) => a.hz >= fLo && a.hz <= fHi).map((a) => (
          <g key={`ar-${a.hz}`}>
            <circle cx={x(a.hz)} cy={y(a.ohm)} r={4} fill={C.bad}
                    stroke="var(--mantine-color-body)" strokeWidth={2} />
            <title>{`Anti-resonance: ${fmtOhm(a.ohm)} at ${fmtHz(a.hz)}`}</title>
          </g>
        ))}

        {noise.map((n) => (
          <g key={`n-${n.source}-${n.harmonic}`}>
            <line x1={x(n.hz)} x2={x(n.hz)} y1={plotBottom - (n.harmonic === 1 ? 12 : 7)} y2={plotBottom}
                  stroke={n.notFiltered ? C.bad : C.noise} strokeWidth={n.harmonic === 1 ? 2 : 1.4} />
            <title>{`${n.source}${n.harmonic > 1 ? `, harmonic ${n.harmonic}` : ''}: ${fmtHz(n.hz)}${
              n.notFiltered ? ', not filtered' : n.aboveBand ? ', above the board range' : ''}`}</title>
          </g>
        ))}

        {tip && hover !== null && (
          <g pointerEvents="none">
            <line x1={x(tip.f)} x2={x(tip.f)} y1={PAD.top} y2={plotBottom}
                  stroke="currentColor" strokeOpacity={0.35} />
            <circle cx={x(tip.f)} cy={y(tip.z)} r={3.5} fill={C.total}
                    stroke="var(--mantine-color-body)" strokeWidth={2} />
          </g>
        )}
        <text x={4} y={10} fontSize={9} fill="currentColor" fillOpacity={0.55}>|Z|</text>
      </svg>

      <Group gap="md" wrap="wrap" style={{ fontSize: 11 }}>
        <Legend color={C.total} label="All parts" />
        <Legend color={C.part} label="Each part" faint />
        <Legend color={C.target} label="Target" dashed />
        <Group gap={4} wrap="nowrap">
          <span style={{ width: 12, height: 10, background: C.gap, opacity: 0.25, display: 'inline-block' }} />
          <Text span size="xs">Above target</Text>
        </Group>
        {whatIf && <Legend color={C.whatIf} label="With the change" dashed />}
      </Group>
      <Text size="xs" c="dimmed" style={{ minHeight: 18 }}>
        {tip
          ? <>{fmtHz(tip.f)}: <Text span ff="monospace" size="xs">{fmtOhm(tip.z)}</Text>
              {tip.over ? ', above target' : ''}</>
          : 'Hover the chart to read a value.'}
      </Text>
    </Stack>
  )
}

function Legend({ color, label, dashed = false, faint = false }:
  { color: string; label: string; dashed?: boolean; faint?: boolean }) {
  return (
    <Group gap={4} wrap="nowrap">
      <svg width={16} height={8} aria-hidden>
        <line x1={0} x2={16} y1={4} y2={4} stroke={color} strokeWidth={2}
              strokeOpacity={faint ? 0.35 : 1} strokeDasharray={dashed ? '4 2' : undefined} />
      </svg>
      <Text span size="xs">{label}</Text>
    </Group>
  )
}
