/**
 * Solve results: which layer, which frequency, and how loud.
 *
 * The map is normalised against one reference shared across every layer and frequency, so
 * switching between them shows a real difference rather than each view being stretched to
 * fill the colour scale. That is what makes "this trace is the loud one" a claim the
 * picture actually supports.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Badge, Box, Card, Group, Loader, SegmentedControl, Slider, Stack, Switch, Table, Text,
} from '@mantine/core'
import { rampCss, type FieldOverlayData } from '../lib/overlay'
import { formatHz } from '../lib/portPlacement'
import type { EmiApi } from '../lib/emiApi'
import { DriverAttach } from './DriverAttach'
import { CableEmissionPanel } from './CableEmissionPanel'
import { ModelledParts } from './ModelledParts'
import { EXPERIMENTAL, Experimental } from './Experimental'
import {
  assessGrid, convergence, NOISE_MARGIN_DB, suggestedGate,
} from '../lib/solveQuality'

export interface ModelledPart {
  ref: string
  component_id: string
  component: string
  generic: boolean
  source: string | null
  c_f: number
  esl_h: number | null
  esr_ohm: number | null
  self_resonance_hz: number | null
  layer: string
}

export interface SolveManifest {
  format_version: number
  metric: string
  metric_detail?: string
  dynamic_range_db: number
  reference_magnitude: number
  frequencies_hz: number[]
  layers: {
    layer: string
    z_mm: number
    grids: {
      frequency_hz: number
      file: string
      width: number
      height: number
      extent_mm: [number, number, number, number]
      z_mm: number
      peak_db: number
    }[]
  }[]
  has_sparams: boolean
  /** Version 2 onwards. Absent on older results, which is exactly what gates the driver UI. */
  has_port_spectra?: boolean
  dense_frequencies_hz?: number[]
  /**
   * Which parts were modelled rather than left as bare copper (docs/implementation.md §3). Empty
   * means none were; absent means the result predates the list. Those are different answers.
   */
  modelled_parts?: ModelledPart[]
  /**
   * Connectors this solve fitted a Tier B gap port to (docs/implementation.md §5.2), and those the
   * antenna solver then produced terms for. They are separate lists because a worker without
   * `nec2c` can produce the first and not the second, and the UI has different things to say about
   * each.
   */
  cable_ports?: string[]
  cable_antenna?: string[]
  cable_antenna_note?: string
  run?: Record<string, unknown>
}

/**
 * Whether a driver can be attached to this result (docs/implementation.md §4).
 *
 * A version-1 solve never recorded the complex port spectra and they cannot be recovered
 * from the artifacts it did write, so the honest answer is to offer a re-run rather than a
 * control that would silently do nothing.
 */
export const PORT_SPECTRA_FORMAT_VERSION = 2

export function canAttachDriver(manifest: SolveManifest): boolean {
  return manifest.format_version >= PORT_SPECTRA_FORMAT_VERSION && !!manifest.has_port_spectra
}

export function whyNoDriver(manifest: SolveManifest): string | null {
  if (canAttachDriver(manifest)) return null
  if (manifest.format_version < PORT_SPECTRA_FORMAT_VERSION) {
    return 'Re-run this solve to attach a driver: it predates the port spectra drivers need.'
  }
  return 'This solve recorded no excited port, so there is nothing for a driver to drive.'
}

export interface SParams {
  ports: {
    port: string
    reference_impedance: number
    points: {
      frequency_hz: number
      z_real: number | null
      z_imag: number | null
      s11_db: number | null
    }[]
  }[]
}

export interface HotspotResultsProps {
  api: EmiApi
  runId: string
  projectId: string
  manifest: SolveManifest
  onOverlayChange: (overlay: FieldOverlayData | null) => void
  onGateChange: (gateDb: number) => void
}

/** The layer with the loudest field at the first frequency: the one worth opening on. */
function loudestLayer(m: SolveManifest): string {
  const f = m.frequencies_hz[0]
  let best = m.layers[0]?.layer ?? ''
  let peak = -Infinity
  for (const l of m.layers) {
    const g = l.grids.find((x) => x.frequency_hz === f)
    if (g && g.peak_db > peak) {
      peak = g.peak_db
      best = l.layer
    }
  }
  return best
}

export function HotspotResults({
  api, runId, projectId, manifest, onOverlayChange, onGateChange,
}: HotspotResultsProps) {
  const layers = manifest.layers.map((l) => l.layer)
  // Open on the loudest layer, not the first in the stackup. The first is often shielded by a
  // plane and carries nothing but residue, which is a poor first impression of a result.
  const [layer, setLayer] = useState(() => loudestLayer(manifest))
  const [freq, setFreq] = useState(manifest.frequencies_hz[0] ?? 0)
  const floorDb = -manifest.dynamic_range_db
  // The gate starts from the first map that loads rather than a fixed -45 dB. Measured on a
  // converged solve, -45 dB showed 100% of the region: an area around a driven trace is
  // near-field everywhere, so a gate that far down paints the whole rectangle.
  const [gate, setGate] = useState(-25)
  const gateTouched = useRef(false)
  const gateChosen = useRef(false)
  const energyDb = manifest.run?.final_energy_db as number | undefined
  const state = convergence(energyDb, manifest.run?.converged as boolean | undefined)
  // A run whose fields never settled is not drawn unless asked for.
  const [showOverlay, setShowOverlay] = useState(state !== 'unusable')
  const [values, setValues] = useState<Float32Array | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sparams, setSparams] = useState<SParams | null>(null)
  const [sparamsError, setSparamsError] = useState<string | null>(null)

  const grid = useMemo(() => {
    const entry = manifest.layers.find((l) => l.layer === layer)
    return entry?.grids.find((g) => g.frequency_hz === freq) ?? null
  }, [manifest, layer, freq])

  /**
   * Peak per layer at the current frequency — the quickest read on the whole result.
   * 0 dB is the loudest thing anywhere in the run, so a layer at −25 dB is genuinely quiet
   * rather than merely scaled down.
   */
  const perLayerPeak = useMemo(() => {
    return manifest.layers.map((l) => {
      const peak = l.grids.find((g) => g.frequency_hz === freq)?.peak_db ?? null
      // Within a few dB of the floor is residue, not a quiet field: listing "-56.7 dB" beside
      // a real layer invites reading it as a small hotspot.
      return { layer: l.layer, peak, quiet: peak !== null && peak <= floorDb + NOISE_MARGIN_DB }
    })
  }, [manifest, freq, floorDb])

  const quality = useMemo(
    () => (values && grid ? assessGrid(values, grid.width, grid.height, floorDb, gate) : null),
    [values, grid, floorDb, gate],
  )

  useEffect(() => {
    onGateChange(gate)
  }, [gate, onGateChange])

  // Fetch one grid at a time. The whole result set would be tens of megabytes; the view
  // only ever shows one layer at one frequency.
  useEffect(() => {
    let cancelled = false
    if (!grid || !showOverlay) {
      onOverlayChange(null)
      setValues(null)
      return
    }
    setLoading(true)
    setError(null)
    api
      .artifactBytes(runId, grid.file)
      .then((buf) => {
        if (cancelled) return
        const values = new Float32Array(buf)
        if (values.length < grid.width * grid.height) {
          throw new Error(
            `field grid is ${values.length} values, manifest says ${grid.width * grid.height}`,
          )
        }
        setValues(values)
        if (!gateTouched.current && !gateChosen.current) {
          gateChosen.current = true
          setGate(suggestedGate(values, -manifest.dynamic_range_db))
        }
        // A layer with no field above the floor is not drawn: what is left of it is residue
        // at the region boundary, and on screen that reads as edge hotspots.
        const q = assessGrid(values, grid.width, grid.height, -manifest.dynamic_range_db, -manifest.dynamic_range_db)
        if (q.belowNoise) {
          onOverlayChange(null)
          return
        }
        onOverlayChange({
          values,
          width: grid.width,
          height: grid.height,
          extent: grid.extent_mm,
          floorDb: -manifest.dynamic_range_db,
        })
      })
      .catch((err) => !cancelled && setError((err as Error).message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [api, runId, grid, showOverlay, manifest.dynamic_range_db, onOverlayChange])

  useEffect(() => {
    if (!manifest.has_sparams) return
    let cancelled = false
    setSparamsError(null)
    api
      .artifactJson<SParams>(runId, 'sparams.json')
      .then((d) => !cancelled && setSparams(d))
      .catch((err) => !cancelled && setSparamsError((err as Error).message))
    return () => {
      cancelled = true
    }
  }, [api, runId, manifest.has_sparams])

  // The overlay lives on the page, so leaving the result (another run, another tab) would
  // otherwise keep this run's map drawn over the board.
  useEffect(() => () => onOverlayChange(null), [onOverlayChange])

  const warnings = (manifest.run?.warnings as string[] | undefined) ?? []

  return (
    <Stack gap="md">
      <Experimental why={EXPERIMENTAL.hotspotMap} mb={0} />
      {state === 'unusable' && (
        <Alert color="red" variant="light" title="This run stopped before its fields settled">
          Energy only fell to {energyDb?.toFixed(1)} dB, so this is a snapshot of fields still
          ringing, not the steady state a hotspot map needs. The map is hidden unless you turn it
          on. Run the solve again — the timestep limit is now worked out from the excitation, so
          it runs until the fields settle.
        </Alert>
      )}
      {state === 'partial' && (
        <Alert color="yellow" variant="light" title="The run did not fully settle">
          Energy fell to {energyDb?.toFixed(1)} dB, short of the cutoff. The loudest areas are
          reliable for comparison; treat quiet areas, and the lowest frequency, with caution.
        </Alert>
      )}
      {warnings.map((w, i) => (
        <Alert key={i} color="yellow" variant="light">
          <Text size="xs">{w}</Text>
        </Alert>
      ))}

      <div>
        <Group justify="space-between" mb={4}>
          <Text size="xs" fw={600} tt="uppercase" c="dimmed">Layer</Text>
          <Switch
            size="xs" label="Show map" checked={showOverlay}
            onChange={(e) => setShowOverlay(e.currentTarget.checked)}
          />
        </Group>
        <SegmentedControl
          size="xs" fullWidth value={layer} onChange={setLayer}
          data={layers.map((l) => ({ value: l, label: l }))}
        />
        <DriverAttach
          api={api}
          runId={runId}
          projectId={projectId}
          manifest={manifest}
          frequencyHz={freq}
          peakDb={perLayerPeak.find((p) => p.layer === layer)?.peak ?? null}
        />

        <Table verticalSpacing={2} fz="xs" mt={6}>
          <Table.Tbody>
            {perLayerPeak.map((p) => (
              <Table.Tr key={p.layer}>
                <Table.Td>
                  <Text size="xs" c={p.layer === layer ? undefined : 'dimmed'}>
                    {p.layer}
                  </Text>
                </Table.Td>
                <Table.Td ta="right">
                  <Text
                    size="xs"
                    ff="monospace"
                    c={p.peak === 0 ? 'orange' : 'dimmed'}
                    title={p.quiet && p.peak !== null ? `${p.peak.toFixed(1)} dB — within ${NOISE_MARGIN_DB} dB of the solver's floor` : undefined}
                  >
                    {p.peak === null ? '—' : p.quiet ? 'no field' : `${p.peak.toFixed(1)} dB`}
                  </Text>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </div>

      <ModelledParts parts={manifest.modelled_parts} />

      {/*
        Cables sit below the layer maps rather than beside them: this is the only number here
        that is a prediction against a limit, and mixing it into the relative-dB controls
        invites reading one as the other.
      */}
      <CableEmissionPanel api={api} runId={runId} projectId={projectId} manifest={manifest} />

      {showOverlay && quality?.belowNoise && (
        <Text size="xs" c="dimmed">
          {layer} carries no field above the solver's floor — usually because a plane shields it
          from the driven trace. There is nothing to draw, so the map is left off rather than
          showing residue at the edge of the region.
        </Text>
      )}
      {showOverlay && quality?.truncated && (
        <Alert color="blue" variant="light" title="Strong field reaches the edge of the region">
          <Text size="xs">
            The loudest point on {layer} is {quality.peakDb.toFixed(1)} dB and the region boundary
            still reaches {quality.edgeDb.toFixed(1)} dB, so copper carrying current continues
            past the edge. The structure is cut where the region ends, and values within a
            millimetre or two of that edge are partly an artefact of the cut. Enlarge the region
            to take in where the current goes.
          </Text>
        </Alert>
      )}

      <div>
        <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={4}>Frequency</Text>
        <SegmentedControl
          size="xs" fullWidth value={String(freq)}
          onChange={(v) => setFreq(Number(v))}
          data={manifest.frequencies_hz.map((f) => ({
            value: String(f), label: formatHz(f),
          }))}
        />
      </div>

      <div>
        <Group justify="space-between" mb={4}>
          <Text size="xs" fw={600} tt="uppercase" c="dimmed">Scale</Text>
          {loading && <Loader size={12} />}
        </Group>
        <Box
          h={10}
          style={{ background: rampCss(), borderRadius: 2, border: '1px solid var(--mantine-color-default-border)' }}
        />
        <Group justify="space-between" mt={2}>
          <Text size="10px" c="dimmed" ff="monospace">
            −{manifest.dynamic_range_db} dB
          </Text>
          <Text size="10px" c="dimmed" ff="monospace">0 dB (loudest)</Text>
        </Group>
        <Text size="xs" c="dimmed" mt="xs">
          Hide below {gate} dB
          {quality && !quality.belowNoise
            ? ` — showing ${Math.round(quality.visibleShare * 100)}% of the region`
            : ''}
        </Text>
        <Slider
          size="xs"
          // Not all the way to the floor: the bottom of the range is where residue lives, and a
          // slider that reaches it lets numerical leftovers be drawn as if they were field.
          min={floorDb + NOISE_MARGIN_DB}
          max={-5}
          step={1}
          value={gate}
          onChange={(v) => {
            gateTouched.current = true
            setGate(v)
          }}
          label={(v) => `${v} dB`}
        />
      </div>

      {error && (
        <Alert color="red" variant="light" title="Could not load the field map">
          {error}
        </Alert>
      )}

      {sparamsError && (
        <Alert color="red" variant="light" title="Could not load the port impedance">
          {sparamsError}
        </Alert>
      )}

      {sparams && (
        <div>
          <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={4}>
            Port impedance
          </Text>
          <Table verticalSpacing={2} fz="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>f</Table.Th>
                <Table.Th ta="right">Z</Table.Th>
                <Table.Th ta="right">S11</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {sparams.ports[0]?.points.map((pt) => (
                <Table.Tr key={pt.frequency_hz}>
                  <Table.Td>{formatHz(pt.frequency_hz)}</Table.Td>
                  <Table.Td ta="right" ff="monospace">
                    {pt.z_real === null
                      ? '—'
                      : `${pt.z_real.toFixed(0)}${(pt.z_imag ?? 0) >= 0 ? '+' : ''}${(pt.z_imag ?? 0).toFixed(0)}j`}
                  </Table.Td>
                  <Table.Td ta="right" ff="monospace">
                    {pt.s11_db === null ? '—' : `${pt.s11_db.toFixed(1)} dB`}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          <Text size="10px" c="dimmed" mt={4}>
            Measured at the port against{' '}
            {sparams.ports[0]?.reference_impedance ?? 50} Ω. An unterminated net reflects
            almost everything, so a value near 0 dB here is expected rather than a fault.
          </Text>
        </div>
      )}

      <Card withBorder padding="xs" radius="sm">
        <Text size="10px" c="dimmed">
          The magnetic field just above each copper layer. Over copper that is the surface
          current density; over bare board it is fringe field, not current. Values are relative to
          the loudest point in this run — usually at or beside the excitation port, which is marked
          on the board, because that is where current is injected — so they compare layers and
          frequencies within this run, not against another board and not against a limit.
        </Text>
      </Card>

      {grid && (
        <Group gap="xs">
          <Badge size="xs" variant="light">
            {grid.width}×{grid.height}
          </Badge>
          <Text size="10px" c="dimmed" ff="monospace">
            z = {grid.z_mm.toFixed(3)} mm
          </Text>
        </Group>
      )}
    </Stack>
  )
}
