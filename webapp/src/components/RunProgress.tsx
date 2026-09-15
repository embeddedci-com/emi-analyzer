/**
 * Live run progress, including the energy-decay curve.
 *
 * The curve is not decoration. In FDTD, total field energy falling steadily toward the
 * cutoff is what "converging" looks like; a curve that flattens means the structure is
 * ringing and the run will not terminate on its own. It is the one number that tells an
 * engineer whether to keep waiting or to stop and re-mesh — which on a run measured in
 * hours is worth a chart.
 *
 * Drawn on a canvas rather than with a chart library, matching how AnalyzerPage already
 * draws waveforms, so this adds no dependency.
 */

import { useEffect, useRef } from 'react'
import { Badge, Group, Paper, Progress, Stack, Text } from '@mantine/core'
import type { Run } from '../lib/emiApi'
import { formatDuration } from '../lib/estimate'

export interface RunProgressProps {
  run: Run
  /** Energy samples accumulated by the caller across polls: [timestep, dB]. */
  energyHistory?: [number, number][]
  height?: number
}

const STATUS_COLOR: Record<string, string> = {
  new: 'gray',
  retry_pending: 'gray',
  in_progress: 'blue',
  stopping: 'orange',
  done: 'green',
  failed: 'red',
  timed_out: 'red',
}

export function RunProgress({ run, energyHistory = [], height = 90 }: RunProgressProps) {
  const p = run.progress
  const pct = p?.pct ?? (run.status === 'done' ? 100 : 0)

  return (
    <Stack gap="xs">
      <Group justify="space-between" gap="xs">
        <Group gap="xs">
          <Badge color={STATUS_COLOR[run.status] ?? 'gray'} variant="light">
            {run.status.replace('_', ' ')}
          </Badge>
          {p?.stage && (
            <Text size="sm" fw={500}>
              {p.stage}
            </Text>
          )}
        </Group>
        {/* Once the worker has meshed, its numbers replace the estimate made before the
            mesh existed — which on a dense region can be an order of magnitude low. */}
        {p?.cells ? (
          <Text size="xs" c="dimmed" ff="monospace">
            {(p.cells / 1e6).toFixed(2)} M cells · {(p.cells * 72 / 1e9).toFixed(2)} GB
            {p.total_timesteps ? ` · ${(p.total_timesteps / 1000).toFixed(0)}k steps` : ''}
          </Text>
        ) : run.estimate ? (
          <Text size="xs" c="dimmed" ff="monospace">
            at least {(run.estimate.cells / 1e6).toFixed(2)} M cells ·{' '}
            {formatDuration(run.estimate.eta_seconds)}
          </Text>
        ) : null}
      </Group>

      <Progress
        value={pct}
        animated={run.status === 'in_progress'}
        color={STATUS_COLOR[run.status] ?? 'blue'}
        size="sm"
      />

      {p?.message && (
        <Text size="xs" c="dimmed">
          {p.message}
        </Text>
      )}

      {p?.total_timesteps ? (
        <Group gap="lg">
          <Text size="xs" c="dimmed" ff="monospace">
            timestep {(p.timestep ?? 0).toLocaleString()} /{' '}
            {p.total_timesteps.toLocaleString()}
          </Text>
          {p.energy_db !== undefined && (
            <Text size="xs" c="dimmed" ff="monospace">
              energy {p.energy_db.toFixed(1)} dB
            </Text>
          )}
        </Group>
      ) : null}

      {energyHistory.length > 1 && (
        <Paper withBorder p={4} radius="sm">
          <EnergyChart samples={energyHistory} height={height} />
          <Text size="10px" c="dimmed" ta="center" mt={2}>
            field energy vs timestep — a curve that flattens is not converging
          </Text>
        </Paper>
      )}

      {run.error && (
        <Text size="sm" c="red">
          {run.error}
        </Text>
      )}
    </Stack>
  )
}

function EnergyChart({ samples, height }: { samples: [number, number][]; height: number }) {
  const ref = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const w = canvas.clientWidth
    canvas.width = Math.max(1, Math.floor(w * dpr))
    canvas.height = Math.floor(height * dpr)

    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, w, height)

    const style = getComputedStyle(canvas)
    const ink = style.getPropertyValue('color') || '#888'

    const xs = samples.map((s) => s[0])
    const ys = samples.map((s) => s[1])
    const minX = Math.min(...xs)
    const maxX = Math.max(...xs, minX + 1)
    // Fix the vertical scale to the -60 dB convergence target so the shape is comparable
    // between runs rather than auto-scaling to whatever this one happened to reach.
    const minY = Math.min(-60, ...ys)
    const maxY = Math.max(0, ...ys)

    const px = (x: number) => 4 + ((x - minX) / (maxX - minX)) * (w - 8)
    const py = (y: number) => 4 + (1 - (y - minY) / (maxY - minY)) * (height - 12)

    // -40 dB is the usual "energy has decayed enough" cutoff.
    ctx.strokeStyle = ink
    ctx.globalAlpha = 0.18
    ctx.setLineDash([3, 3])
    ctx.beginPath()
    ctx.moveTo(4, py(-40))
    ctx.lineTo(w - 4, py(-40))
    ctx.stroke()
    ctx.setLineDash([])

    ctx.globalAlpha = 1
    ctx.strokeStyle = 'var(--mantine-color-blue-6)'
    ctx.strokeStyle = style.getPropertyValue('--mantine-color-blue-6') || '#3b82f6'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    samples.forEach(([x, y], i) => (i ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y))))
    ctx.stroke()
  }, [samples, height])

  return <canvas ref={ref} style={{ display: 'block', width: '100%', height }} />
}
