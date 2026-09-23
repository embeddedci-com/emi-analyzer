/**
 * Cable emissions for a finished solve (§7, §10, §17).
 *
 * The first place in the tool that predicts an absolute field on a real board rather than a
 * budget or a relative map, so most of this component is about being clear when it cannot.
 * Three things have to be present and any of them can be missing on its own:
 *
 *   - **a gap port** — the solve has to have been asked for one, which changes the mesh, so
 *     this cannot be added to a result afterwards the way a driver can;
 *   - **the antenna solver's terms** — computed beside the solve, absent if `nec2c` was not
 *     installed on the worker that ran it;
 *   - **a driver** — attached here, because §10 keeps that decision out of the run.
 *
 * Each gets its own sentence naming what to do about it. "No chart" would be the same display
 * for a solve that cannot ever produce one and a solve that needs one more click.
 */

import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Badge, Group, Select, Stack, Text, Tooltip } from '@mantine/core'
import type { EmiApi } from '../lib/emiApi'
import {
  driverOptionLabel, parseDriverDocument, sigmaDb, weakestSource,
} from '../lib/driverDocument'
import {
  composeCable, parseCablePorts, portResistance, whyNoCableDriver, type CableAntenna, type CablePort,
  type PortSpectrum,
} from '../lib/cableEmission'
import { CableEmissionChart } from './CableEmissionChart'
import { EXPERIMENTAL, Experimental } from './Experimental'
import type { SolveManifest } from './HotspotResults'

interface CableAntennaJson {
  cables: CableAntenna[]
}
interface PortsJson {
  ports: { port: string; dense?: PortSpectrum }[]
}

export interface CableEmissionPanelProps {
  api: EmiApi
  runId: string
  projectId: string
  manifest: SolveManifest & { cable_ports?: string[]; cable_antenna?: string[];
                              cable_antenna_note?: string }
}

export function CableEmissionPanel({ api, runId, projectId, manifest }: CableEmissionPanelProps) {
  const [driverId, setDriverId] = useState<string | null>(null)
  const [ref, setRef] = useState<string | null>(null)
  const [data, setData] = useState<{
    transfers: CablePort[]
    antennas: CableAntenna[]
    spectra: Map<string, PortSpectrum>
  } | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const refs = manifest.cable_ports ?? []
  const hasPorts = refs.length > 0
  const hasAntenna = (manifest.cable_antenna ?? []).length > 0

  const drivers = useQuery({
    queryKey: ['emi', 'drivers', projectId],
    queryFn: () => api.listDrivers(projectId),
    enabled: hasPorts && hasAntenna,
  })

  useEffect(() => {
    if (!hasPorts || !hasAntenna) return
    let cancelled = false
    const get = (name: string) => api.artifactJson(runId, name)
    // ports.json is optional: an older solve has none, and says so below rather than here.
    Promise.all([
      get('cable_ports.json'), get('cable_antenna.json'), get('ports.json').catch(() => null),
    ])
      .then(([cablePorts, antenna, ports]) => {
        if (cancelled) return
        const transfers = parseCablePorts(cablePorts)
        const spectra = new Map<string, PortSpectrum>()
        for (const p of (ports as PortsJson | null)?.ports ?? []) {
          if (p.dense) spectra.set(p.port, p.dense)
        }
        setData({ transfers, antennas: (antenna as CableAntennaJson).cables ?? [], spectra })
        setRef((r) => r ?? transfers[0]?.ref ?? null)
      })
      .catch((e) => !cancelled && setLoadError((e as Error).message))
    return () => {
      cancelled = true
    }
  }, [api, runId, hasPorts, hasAntenna])

  const selectedDriver = drivers.data?.find((d) => d.id === driverId) ?? null

  const composed = useMemo(() => {
    if (!data || !ref || !selectedDriver) return null
    const port = data.transfers.find((t) => t.ref === ref)
    const antenna = data.antennas.find((a) => a.ref === ref)
    if (!port || !antenna) return null
    const spectrum = data.spectra.get(port.driven_by) ?? null
    const stale = whyNoCableDriver(manifest, spectrum)
    if (stale !== null || spectrum === null) {
      return { parsed: null, emission: null, error: stale }
    }
    try {
      const parsed = parseDriverDocument(selectedDriver.document)
      // The estimate's own composition: every harmonic, with this driver's source impedance.
      const { emission } = composeCable(
        port, antenna, spectrum, portResistance(manifest, port.driven_by), parsed,
      )
      // A null of the driver's spectrum is a point of zero current; the chart leaves it out.
      emission.points = emission.points.filter((p) => p.current_a > 0)
      return { parsed, emission, error: null as string | null }
    } catch (e) {
      return { parsed: null, emission: null, error: (e as Error).message }
    }
  }, [data, ref, selectedDriver, manifest])

  if (!hasPorts) return null

  if (manifest.run?.converged === false) {
    return (
      <Alert color="gray" variant="light" title="No cable emissions for this result">
        <Text size="xs">
          This solve stopped before its fields settled, so its transfer functions
          are not used.
        </Text>
      </Alert>
    )
  }

  if (!hasAntenna) {
    return (
      <Alert color="gray" variant="light" title="No cable emissions for this result">
        <Text size="xs">
          {manifest.cable_antenna_note ??
            'This solve fitted a gap port but carries no antenna impedance, so there is ' +
              'nothing to compose its transfer function with. Re-run it on a worker with the ' +
              'antenna solver installed.'}
        </Text>
      </Alert>
    )
  }

  const options = (drivers.data ?? []).map((d) => ({
    value: d.id, label: driverOptionLabel(d.name, d.document),
  }))

  return (
    <Stack gap={6}>
      <Group gap="xs" align="flex-end">
        <Experimental why={EXPERIMENTAL.cableEmissions} mb={6} />
        {refs.length > 1 && (
          <Select
            label="Cable" size="xs" w={140} data={refs.map((r) => ({ value: r, label: r }))}
            value={ref} onChange={setRef}
          />
        )}
        <Select
          label="Driver"
          size="xs"
          w={220}
          placeholder="none — no absolute level"
          clearable
          data={options}
          value={driverId}
          onChange={setDriverId}
          disabled={options.length === 0}
        />
        {composed?.parsed && (
          <Tooltip label="The least certain number in this driver" withArrow>
            <Badge size="xs" variant="dot" mb={6}>
              {weakestSource(composed.parsed)} ±{sigmaDb(composed.parsed)} dB
            </Badge>
          </Tooltip>
        )}
      </Group>

      {options.length === 0 && !drivers.isLoading && (
        <Text size="xs" c="dimmed">
          No drivers yet. Add one on the Drivers tab — without a source this solve says which
          layout is quieter, not how many µV/m it radiates.
        </Text>
      )}
      {loadError && <Text size="xs" c="orange">{loadError}</Text>}
      {composed?.error && <Text size="xs" c="orange">{composed.error}</Text>}

      {composed?.emission && composed.emission.points.length > 0 && (
        <CableEmissionChart emission={composed.emission} />
      )}
      {composed?.emission && composed.emission.points.length === 0 && (
        <Alert color="yellow" variant="light" title="This driver says nothing about this cable">
          <Text size="xs">
            {[...composed.emission.undriven.values()][0] ??
              'No frequency in this result is driven by the selected driver.'}
          </Text>
        </Alert>
      )}

      {composed?.emission && composed.emission.points.length > 0 && (
        <Text size="xs" c="dimmed">
          Predicted from the layout's common-mode drive at {ref}, an antenna model of the cable,
          and this driver. Not a pre-compliance test: a measured result can differ by more than
          the margin shown, and this composition is not yet validated on a real board — see the
          experimental note above.
        </Text>
      )}
    </Stack>
  )
}
