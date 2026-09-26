/**
 * Setting up a small-part solve: pick a net (or draw a region), see the cost, run it.
 *
 * The net is the usual way in. It is cut out with its planes and ports at its two ends
 * (`lib/smallPart.ts`), which is what makes the run take minutes rather than hours: the mesh
 * follows one net's copper, not every trace near it, and a net terminated at both ends rings
 * down in a few nanoseconds. A drawn region is there for the part of a net, or a spot with no
 * obvious net, and needs a port placed by hand.
 */

import { useMemo, useState } from 'react'
import {
  Alert, Badge, Button, Card, Group, NumberInput, SegmentedControl, Select, Stack, Switch, Text,
} from '@mantine/core'
import type { BoardDoc } from '../lib/boardTypes'
import { formatDuration } from '../lib/estimate'
import { harmonics, isReferenceNet, type PortSpec } from '../lib/portPlacement'
import {
  BANDS, estimateSmallPart, pairOf, planCoupon, PRESETS, smallPartParams, type Roi,
} from '../lib/smallPart'
import { EXPERIMENTAL, Experimental } from './Experimental'

export interface SmallPartSetupProps {
  doc: BoardDoc
  geometry: ArrayBuffer | null
  roi: Roi | null
  onRoiChange: (roi: Roi | null) => void
  ports: PortSpec[]
  onPortsChange: (ports: PortSpec[]) => void
  onHighlightNet: (net: string | null) => void
  pickingPad: boolean
  onPickPad: (picking: boolean) => void
  pickMiss?: boolean
  drawingRoi: boolean
  onDrawRoi: (drawing: boolean) => void
  onSubmit: (params: ReturnType<typeof smallPartParams>) => void
  submitting?: boolean
  error?: string | null
}

export function SmallPartSetup({
  doc, geometry, roi, onRoiChange, ports, onPortsChange, onHighlightNet,
  pickingPad, onPickPad, pickMiss, drawingRoi, onDrawRoi, onSubmit, submitting, error,
}: SmallPartSetupProps) {
  const [by, setBy] = useState<'net' | 'region'>('net')
  const [net, setNet] = useState<string | null>(null)
  const [withPair, setWithPair] = useState(true)
  const [bandValue, setBandValue] = useState<string>(BANDS[0].value)
  // Null until the user picks one: normal where it fits the budget, coarse where only that does.
  // The sample board's first net is over budget on normal, so its first solve was refused.
  const [presetChoice, setPresetChoice] = useState<string | null>(null)
  const [clockMhz, setClockMhz] = useState<number | ''>('')
  const band = BANDS.find((b) => b.value === bandValue) ?? BANDS[0]
  const autoCoarse = useMemo(
    () => !!roi && !!estimateSmallPart(roi, PRESETS[1], band, doc).refused
      && !estimateSmallPart(roi, PRESETS[0], band, doc).refused,
    [roi, band, doc],
  )
  const presetValue = presetChoice ?? (autoCoarse ? 'coarse' : 'normal')
  const preset = PRESETS.find((p) => p.value === presetValue) ?? PRESETS[1]

  const nets = useMemo(
    () => doc.nets
      .filter((n) => n.name && !isReferenceNet(n.name) && n.length_mm > 0)
      .sort((a, b) => b.length_mm - a.length_mm)
      .map((n) => ({ value: n.name, label: `${n.name} · ${n.length_mm.toFixed(0)} mm` })),
    [doc.nets],
  )
  const pair = net ? pairOf(doc, net) : null
  const chosen = net ? (pair && withPair ? [net, pair] : [net]) : []

  const plan = useMemo(
    () => (by === 'net' && chosen.length ? planCoupon(doc, geometry, doc.geometry, chosen) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [by, doc, geometry, chosen.join('|')],
  )

  const replan = (value: string | null, pairToo: boolean) => {
    const other = value ? pairOf(doc, value) : null
    const p = value ? planCoupon(doc, geometry, doc.geometry, other && pairToo ? [value, other] : [value]) : null
    onRoiChange(p?.roi ?? null)
    onPortsChange(p?.ports ?? [])
  }
  const pickNet = (value: string | null) => {
    setNet(value)
    onHighlightNet(value)
    replan(value, withPair)
  }
  const togglePair = (on: boolean) => {
    setWithPair(on)
    replan(net, on)
  }

  const est = useMemo(
    () => (roi ? estimateSmallPart(roi, preset, band, doc) : null),
    [roi, preset, band, doc],
  )
  const clockHz = typeof clockMhz === 'number' && clockMhz > 0 ? clockMhz * 1e6 : 0
  const maps = clockHz ? harmonics(clockHz, 5, band.hi).filter((f) => f >= band.lo) : []

  const ready = !!roi && ports.length > 0 && !!est && !est.refused && !(by === 'net' && plan?.error)
  const submit = () => {
    if (!roi || !ready) return
    onSubmit(smallPartParams({
      roi, ports, nets: by === 'net' ? chosen : [], band, preset, frequencies_hz: maps,
    }))
  }

  return (
    <Stack gap="md">
      <Group gap={6}>
        <Text size="sm" fw={600}>Solve a small part</Text>
        <Experimental why={EXPERIMENTAL.smallPart} />
      </Group>
      <Text size="xs" c="dimmed">
        Cuts one net out with the planes under it and solves it in minutes. You get where its
        current flows and what its ends see.
      </Text>

      <SegmentedControl
        size="xs" fullWidth value={by}
        onChange={(v) => {
          setBy(v as 'net' | 'region')
          onRoiChange(null)
          onPortsChange([])
          onHighlightNet(null)
          setNet(null)
        }}
        data={[{ label: 'A net', value: 'net' }, { label: 'A region', value: 'region' }]}
      />

      {by === 'net' ? (
        <Stack gap="xs">
          <Select
            size="xs" searchable clearable placeholder="Pick a net, longest first"
            data={nets} value={net} onChange={pickNet} nothingFoundMessage="No net matches"
          />
          {pair && (
            <Switch size="xs" checked={withPair} onChange={(e) => togglePair(e.currentTarget.checked)}
                    label={`With its pair, ${pair}`} />
          )}
          {plan?.error && <Text size="xs" c="orange">{plan.error}</Text>}
          {plan && !plan.error && (
            <Text size="xs" c="dimmed">
              {plan.roi && `${(plan.roi[2] - plan.roi[0]).toFixed(1)} × ${(plan.roi[3] - plan.roi[1]).toFixed(1)} mm, `}
              {plan.margin_mm.toFixed(1)} mm of plane around it. Driven at{' '}
              {plan.ports[0]?.padRef ?? plan.ports[0]?.name}
              {plan.ports.length > 1 && `, loaded at ${plan.ports.slice(1).map((p) => p.padRef ?? p.name).join(', ')}`}.
              {plan.notes.map((n) => ` ${n}`)}
            </Text>
          )}
        </Stack>
      ) : (
        <Stack gap="xs">
          <Group gap="xs">
            <Button size="xs" variant={drawingRoi ? 'filled' : 'default'} onClick={() => onDrawRoi(!drawingRoi)}>
              {drawingRoi ? 'Drag on the board…' : 'Draw the region'}
            </Button>
            <Button size="xs" variant={pickingPad ? 'filled' : 'default'} onClick={() => onPickPad(!pickingPad)}>
              {pickingPad ? 'Click a pad…' : 'Add a port'}
            </Button>
          </Group>
          {pickMiss && <Text size="xs" c="orange">Nothing there. Click closer to a pad or a via.</Text>}
          <Text size="xs" c="dimmed">
            {roi ? `${(roi[2] - roi[0]).toFixed(1)} × ${(roi[3] - roi[1]).toFixed(1)} mm` : 'No region yet'}
            {` · ${ports.length} port${ports.length === 1 ? '' : 's'}`}
            {ports.length > 1 ? `, ${ports[0].name} driven` : ''}
          </Text>
          <Text size="xs" c="dimmed">
            Everything in the region is solved, so it meshes more densely than a net would.
          </Text>
        </Stack>
      )}

      <Group grow gap="xs" align="flex-end">
        <Select size="xs" label="Band" allowDeselect={false} value={bandValue} onChange={(v) => setBandValue(v ?? BANDS[0].value)}
                data={BANDS.map((b) => ({ value: b.value, label: b.label }))} />
        <Select size="xs" label="Mesh" allowDeselect={false} value={presetValue} onChange={(v) => setPresetChoice(v)}
                data={PRESETS.map((p) => ({ value: p.value, label: p.label }))} />
      </Group>
      {presetChoice === null && autoCoarse && (
        <Text size="xs" c="dimmed">Coarse, because this part is over budget on normal.</Text>
      )}
      <NumberInput
        size="xs" label="Clock, for maps at its harmonics (optional)" suffix=" MHz" min={1} max={3000}
        value={clockMhz} onChange={(v) => setClockMhz(typeof v === 'number' ? v : '')}
      />
      <Group gap={4}>
        {[band.lo, ...maps.filter((f) => f !== band.lo && f !== band.hi), band.hi]
          .sort((a, b) => a - b)
          .map((f) => <Badge key={f} size="xs" variant="light">{f >= 1e9 ? `${f / 1e9} GHz` : `${f / 1e6} MHz`}</Badge>)}
      </Group>

      {est && (
        <Card withBorder padding="xs" radius="sm">
          <Stack gap={4}>
            <Group justify="space-between">
              <Text size="xs" c="dimmed">Cells (about)</Text>
              <Text size="xs" ff="monospace">{(est.cells / 1e6).toFixed(2)} M</Text>
            </Group>
            <Group justify="space-between">
              <Text size="xs" c="dimmed">Simulated time, at most</Text>
              <Text size="xs" ff="monospace">{(est.record_s * 1e9).toFixed(0)} ns</Text>
            </Group>
            <Group justify="space-between">
              <Text size="xs" fw={600}>Time</Text>
              <Text size="xs" fw={600} ff="monospace">
                about {formatDuration(Math.max(est.typical_seconds, 10))}, at most {formatDuration(est.worst_seconds)}
              </Text>
            </Group>
            <Text size="10px" c="dimmed">
              An estimate from the part&apos;s size. The worker meshes it within seconds of
              starting, shows the real numbers, and stops it if they are over budget.
            </Text>
          </Stack>
        </Card>
      )}
      {est?.refused && (
        <Alert color="red" variant="light" title="Too large for a small-part solve">
          <Text size="xs">{est.refused} Pick a shorter net, draw a smaller region or use the coarse mesh.</Text>
        </Alert>
      )}
      {error && (
        <Alert color="red" variant="light" title="Could not start the solve">
          <Text size="xs">{error}</Text>
        </Alert>
      )}

      <Button onClick={submit} disabled={!ready || submitting} loading={submitting}>
        {!roi ? (by === 'net' ? 'Pick a net' : 'Draw a region') : ports.length === 0 ? 'Add a port' : 'Solve this part'}
      </Button>
    </Stack>
  )
}
