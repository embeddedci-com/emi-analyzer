/**
 * Setting up a solve: region, ports, frequencies, and what it will cost.
 *
 * The cost estimate is the point of this panel. An FDTD run is measured in hours, so the
 * user has to be able to see the price before committing — and see it move as they change
 * the region or the mesh, which is the only way the trade-off becomes intuitive.
 */

import { useMemo, useState } from 'react'
import {
  ActionIcon, Alert, Badge, Button, Card, Chip, Divider, Group, NumberInput,
  Select, Stack, Switch, Table, Text, Tooltip,
} from '@mantine/core'
import { EXPERIMENTAL, Experimental } from './Experimental'
import type { BoardDoc } from '../lib/boardTypes'
import {
  estimate,
  formatBytes,
  formatCount,
  formatDuration,
  MESH_MULTIPLIER_FLOOR,
  type EstimateInput,
} from '../lib/estimate'
import {
  formatHz, harmonics, isReferenceNet, placePortOnNet, suggestRoi, type PortSpec,
} from '../lib/portPlacement'
import type { WorkerInfo } from '../lib/emiApi'

export interface SolveSetupProps {
  doc: BoardDoc
  roi: [number, number, number, number] | null
  onRoiChange: (roi: [number, number, number, number]) => void
  ports: PortSpec[]
  onPortsChange: (ports: PortSpec[]) => void
  /** True while the canvas is waiting for a pad click. */
  pickingPad: boolean
  onPickPad: (picking: boolean) => void
  /** True when the last click while picking found nothing near enough. */
  pickMiss?: boolean
  drawingRoi: boolean
  onDrawRoi: (drawing: boolean) => void
  workers: WorkerInfo[]
  onSubmit: (params: SolveRequest) => void
  submitting?: boolean
  error?: string | null
  /** What the Cables tab has assigned, so this run can be asked to model the emissions. */
  cableAssignments?: Record<string, { type: string; length_m?: number }>
}

export interface SolveRequest {
  roi: { min_x_mm: number; min_y_mm: number; max_x_mm: number; max_y_mm: number }
  frequencies_hz: number[]
  ports: Omit<PortSpec, 'origin' | 'net' | 'padRef'>[]
  mesh: { dx_um: number; dy_um: number; dz_um: number }
  estimateInput: EstimateInput
  /** docs/implementation.md §3. The server attaches the caller's library when this is set. */
  model_components?: boolean
  /**
   * `{ "USB1": { type, length_m } }`: the connectors to fit a Tier B gap port to
   * (docs/implementation.md §5.2).
   */
  cable_ports?: Record<string, { type: string; length_m?: number }>
  /** Record the NF2FF box the board's own radiation is computed from (implementation §6). */
  far_field?: boolean
}

/** Mesh presets. Named for what they are for, not by their numbers. */
const MESH_PRESETS = [
  { value: 'coarse', label: 'Coarse — a first look', dx: 150, dy: 150, dz: 100 },
  { value: 'normal', label: 'Normal — comparing layouts', dx: 75, dy: 75, dz: 50 },
  { value: 'fine', label: 'Fine — a considered answer', dx: 50, dy: 50, dz: 25 },
]

export function SolveSetup({
  doc, roi, onRoiChange, ports, onPortsChange,
  pickingPad, onPickPad, pickMiss, drawingRoi, onDrawRoi,
  workers, onSubmit, submitting, error, cableAssignments = {},
}: SolveSetupProps) {
  const [netQuery, setNetQuery] = useState<string | null>(null)
  const [fundamentalMhz, setFundamentalMhz] = useState<number>(100)
  const [harmonicCount, setHarmonicCount] = useState<number>(3)
  // Coarse by default. A normal or fine run on a real board is measured in days, not hours
  // (see MESH_MULTIPLIER_FLOOR), so the cheap first look is the right thing to land on and
  // the finer presets are a deliberate choice rather than the path of least resistance.
  const [meshPreset, setMeshPreset] = useState('coarse')
  // Off by default, and it stays that way until someone asks: with it off a solve is
  // bit-for-bit what it was before component models existed, which is the promise K2 makes.
  const [modelComponents, setModelComponents] = useState(false)
  // Off by default for the same reason, and with a sharper edge: a gap port extends the grid
  // past the board, so turning this on changes the mesh and the cost, not just the result.
  const [modelCables, setModelCables] = useState(false)
  // Also off by default, and for a sharper reason than the other two: it adds twelve dumps and
  // forces the run to cover 30 MHz, which is 100 ns of record however small the board is.
  const [farField, setFarField] = useState(false)
  const [placementNote, setPlacementNote] = useState<string | null>(null)

  const mesh = MESH_PRESETS.find((m) => m.value === meshPreset) ?? MESH_PRESETS[1]

  const signalNets = useMemo(
    () =>
      doc.nets
        .filter((n) => n.name && !isReferenceNet(n.name))
        .sort((a, b) => b.length_mm - a.length_mm)
        .map((n) => ({
          value: n.name,
          label: `${n.name} — ${n.length_mm.toFixed(0)} mm`,
        })),
    [doc.nets],
  )

  const frequencies = useMemo(
    () => harmonics(fundamentalMhz * 1e6, harmonicCount, 6e9),
    [fundamentalMhz, harmonicCount],
  )

  // The stack's air box, needed for the vertical extent of the estimate.
  const boardThickness = doc.board.thickness_mm || 1.6
  const airMm = 5

  const est = useMemo(() => {
    if (!roi || frequencies.length === 0) return null
    const input: EstimateInput = {
      roi_x_mm: Math.max(0.1, roi[2] - roi[0]),
      roi_y_mm: Math.max(0.1, roi[3] - roi[1]),
      roi_z_mm: boardThickness + 2 * airMm,
      dx_um: mesh.dx, dy_um: mesh.dy, dz_um: mesh.dz,
      f_min_hz: Math.min(...frequencies),
      ports: Math.max(1, ports.filter((p) => p.excited).length),
      // Measured on four real boards, per preset -- see MESH_MULTIPLIER_FLOOR. This used to
      // be a flat 0.15, a guess made before any mesh had been built, which put the figure
      // shown here roughly 20x below reality: a fine 20 x 20 mm run read as 9.6 hours when
      // the typical measured cost is 105. The worker still recomputes the true figure at the
      // mesh stage; this is what the user is shown while deciding whether to start.
      fill_factor: MESH_MULTIPLIER_FLOOR[meshPreset] ?? 1.0,
    }
    try {
      return { input, value: estimate(input) }
    } catch {
      return null
    }
  }, [roi, frequencies, mesh, meshPreset, ports, boardThickness])

  const capableWorkers = workers.filter(
    (w) => w.online && (w.capabilities.kinds ?? []).includes('solve'),
  )
  const largest = Math.max(0, ...capableWorkers.map((w) => w.capabilities.max_cells ?? 0))
  const tooBig = est != null && largest > 0 && est.value.cells > largest

  const addPortFromNet = (net: string | null) => {
    setNetQuery(net)
    if (!net) return
    const result = placePortOnNet(doc, net, roi, ports.length)
    setPlacementNote(result.reason)
    if (result.port) {
      const next = [...ports, result.port]
      onPortsChange(next)
      if (!roi) onRoiChange(suggestRoi(doc, next))
    }
  }

  const removePort = (i: number) => {
    const next = ports.filter((_, k) => k !== i).map((p, k) => ({
      ...p, name: `p${k + 1}`, excited: k === 0,
    }))
    onPortsChange(next)
  }

  const canSubmit = !!roi && ports.length > 0 && frequencies.length > 0 && !tooBig && !submitting

  const submit = () => {
    if (!roi || !est) return
    onSubmit({
      roi: { min_x_mm: roi[0], min_y_mm: roi[1], max_x_mm: roi[2], max_y_mm: roi[3] },
      frequencies_hz: frequencies,
      ports: ports.map(({ origin, net, padRef, ...rest }) => rest),
      mesh: { dx_um: mesh.dx, dy_um: mesh.dy, dz_um: mesh.dz },
      estimateInput: est.input,
      model_components: modelComponents,
      cable_ports: modelCables ? cableAssignments : undefined,
      far_field: farField || undefined,
    })
  }

  return (
    <Stack gap="md">
      {/* --- ports --- */}
      <div>
        <Group justify="space-between" mb={4}>
          <Text size="xs" fw={600} tt="uppercase" c="dimmed">
            What is driving?
          </Text>
          <Tooltip
            multiline w={250}
            label="openEMS solves a passive structure. Without a source it has nothing to excite, and every field comes out zero."
          >
            <Badge size="xs" variant="light" color="gray">why?</Badge>
          </Tooltip>
        </Group>

        <Stack gap="xs">
          <Select
            size="xs"
            searchable
            clearable
            placeholder="Pick a net — longest first"
            data={signalNets}
            value={netQuery}
            onChange={addPortFromNet}
            nothingFoundMessage="No signal net matches"
          />
          <Group gap="xs">
            <Button
              size="xs"
              variant={pickingPad ? 'filled' : 'default'}
              onClick={() => onPickPad(!pickingPad)}
            >
              {pickingPad ? 'Click a pad on the board…' : 'Or click a pad'}
            </Button>
          </Group>

          {pickMiss && (
            <Text size="xs" c="orange">
              Nothing there — click closer to a pad or a via, or zoom in first.
            </Text>
          )}

          {placementNote && (
            <Text size="xs" c="dimmed">
              {placementNote}
            </Text>
          )}

          {ports.length > 0 && (
            <Table verticalSpacing={4} horizontalSpacing="xs" fz="xs">
              <Table.Tbody>
                {ports.map((p, i) => (
                  <Table.Tr key={p.name}>
                    <Table.Td>
                      <Group gap={6} wrap="nowrap">
                        <Badge size="xs" variant={p.excited ? 'filled' : 'light'}>
                          {p.name}
                        </Badge>
                        <Text size="xs" truncate>
                          {p.net ?? p.padRef ?? 'port'}
                        </Text>
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      <Text size="10px" c="dimmed" ff="monospace">
                        {p.x_mm.toFixed(1)}, {p.y_mm.toFixed(1)} · {p.layer}
                      </Text>
                    </Table.Td>
                    <Table.Td w={28}>
                      <ActionIcon
                        size="xs" variant="subtle" color="red"
                        onClick={() => removePort(i)} aria-label={`Remove ${p.name}`}
                      >
                        ×
                      </ActionIcon>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
          {ports.length > 1 && (
            <Text size="xs" c="dimmed">
              Only {ports[0].name} is excited; the rest are 50 Ω loads.
            </Text>
          )}
          <Switch
            size="xs"
            mt={4}
            label={
              <Group gap={6} wrap="nowrap">
                <span>Record the board&apos;s far field</span>
                <Experimental why={EXPERIMENTAL.farField} />
              </Group>
            }
            checked={farField}
            onChange={(e) => setFarField(e.currentTarget.checked)}
            description="Adds the surface the board's own radiation at 3 m is computed from, so this solve can contribute to a compliance estimate rather than only to a hotspot map. It makes the run substantially longer: the radiated band starts at 30 MHz, and resolving that needs 100 ns of simulated time whatever the board's size."
          />
          <Switch
            size="xs"
            mt={4}
            label="Model cable emissions"
            checked={modelCables}
            disabled={Object.keys(cableAssignments).length === 0}
            onChange={(e) => setModelCables(e.currentTarget.checked)}
            description={
              Object.keys(cableAssignments).length === 0
                ? 'Assign a cable to a connector on the Cables tab first. Without one there is nothing for a gap port to hand current to.'
                : `Fits a gap port to ${Object.keys(cableAssignments).sort().join(', ')} and extends the grid past that edge, so the result can predict a field at 3 m rather than a current budget. Unlike a driver this cannot be added afterwards: it changes the mesh.`
            }
          />
          <Switch
            size="xs"
            mt={4}
            label="Model decoupling capacitors"
            checked={modelComponents}
            onChange={(e) => setModelComponents(e.currentTarget.checked)}
            description="Places a series R-L-C across each capacitor the library recognises, instead of leaving it as bare copper. Costs well under 1 % more cells and does not change the timestep. With it off the solve is exactly what it was before component models existed."
          />
          {ports.some((p) => p.excited) && (
            <Text size="xs" c="dimmed">
              <b>Driver: none (relative).</b> This solve records the port spectra a driver
              needs, so one can be attached to the result afterwards — on the Drivers tab —
              without solving again. Choosing it now would change nothing about the run.
            </Text>
          )}
        </Stack>
      </div>

      <Divider />

      {/* --- region --- */}
      <div>
        <Group justify="space-between" mb={4}>
          <Text size="xs" fw={600} tt="uppercase" c="dimmed">
            Region to solve
          </Text>
          <Button
            size="compact-xs"
            variant={drawingRoi ? 'filled' : 'default'}
            onClick={() => onDrawRoi(!drawingRoi)}
          >
            {drawingRoi ? 'Drag on the board…' : 'Draw'}
          </Button>
        </Group>
        {roi ? (
          <Group gap="xs">
            <Text size="xs" ff="monospace" c="dimmed">
              {(roi[2] - roi[0]).toFixed(1)} × {(roi[3] - roi[1]).toFixed(1)} mm
            </Text>
            <Text size="10px" c="dimmed">
              at {roi[0].toFixed(1)}, {roi[1].toFixed(1)}
            </Text>
          </Group>
        ) : (
          <Text size="xs" c="dimmed">
            Whole-board solves are not possible — see the limitations page. Pick a region
            around whatever you think is radiating.
          </Text>
        )}
      </div>

      <Divider />

      {/* --- frequencies --- */}
      <div>
        <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={4}>
          Frequencies
        </Text>
        <Group gap="xs" align="flex-end">
          <NumberInput
            size="xs" label="Clock" suffix=" MHz" min={1} max={6000}
            value={fundamentalMhz}
            onChange={(v) => setFundamentalMhz(Number(v) || 100)}
            style={{ flex: 1 }}
          />
          <NumberInput
            size="xs" label="Harmonics" min={1} max={8} w={90}
            value={harmonicCount}
            onChange={(v) => setHarmonicCount(Number(v) || 1)}
          />
        </Group>
        <Group gap={4} mt="xs">
          {frequencies.map((f) => (
            <Chip key={f} size="xs" checked readOnly>
              {formatHz(f)}
            </Chip>
          ))}
        </Group>
        <Text size="10px" c="dimmed" mt={4}>
          Odd harmonics only — a square-wave clock has essentially no even ones, so solving
          for them would spend hours on frequencies your source does not produce.
        </Text>
      </div>

      <Divider />

      {/* --- mesh + cost --- */}
      <div>
        <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={4}>
          Mesh
        </Text>
        <Select
          size="xs"
          data={MESH_PRESETS.map((m) => ({ value: m.value, label: m.label }))}
          value={meshPreset}
          onChange={(v) => setMeshPreset(v ?? 'coarse')}
          allowDeselect={false}
        />
      </div>

      {est && (
        <Card withBorder padding="xs" radius="sm">
          <Stack gap={4}>
            <Group justify="space-between">
              <Text size="xs" c="dimmed">Cells (at least)</Text>
              <Text size="xs" ff="monospace">{formatCount(est.value.cells)}</Text>
            </Group>
            <Group justify="space-between">
              <Text size="xs" c="dimmed">Memory</Text>
              <Text size="xs" ff="monospace">{formatBytes(est.value.ram_bytes)}</Text>
            </Group>
            <Group justify="space-between">
              <Text size="xs" c="dimmed">Timesteps</Text>
              <Text size="xs" ff="monospace">{formatCount(est.value.timesteps)}</Text>
            </Group>
            <Group justify="space-between">
              <Text size="xs" fw={600}>At least</Text>
              <Text size="xs" fw={600} ff="monospace">
                {formatDuration(est.value.eta_seconds)}
              </Text>
            </Group>
            <Text size="10px" c="dimmed">
              A lower bound. It assumes the sparsest of the boards this was measured on; a
              densely routed region forces more grid lines and can run about three times
              longer. The worker publishes the real figure within seconds of starting, and
              refuses the run rather than beginning something it cannot finish.
            </Text>
          </Stack>
        </Card>
      )}

      {tooBig && (
        <Alert color="red" variant="light" title="Too large for any connected worker">
          This needs {formatCount(est!.value.cells)} cells but the largest worker online
          handles {formatCount(largest)}. Shrink the region, choose a coarser mesh, or start
          a bigger worker.
        </Alert>
      )}

      {capableWorkers.length === 0 && (
        <Alert color="yellow" variant="light" title="No worker can solve">
          Workers are online but none has openEMS available, so a solve would queue
          indefinitely.
        </Alert>
      )}

      {error && (
        <Alert color="red" variant="light" title="Could not start the solve">
          {error}
        </Alert>
      )}

      <Button onClick={submit} disabled={!canSubmit} loading={submitting}>
        {ports.length === 0
          ? 'Choose what is driving'
          : !roi
            ? 'Choose a region'
            : `Solve — at least ${formatDuration(est?.value.eta_seconds ?? 0)}`}
      </Button>
    </Stack>
  )
}
