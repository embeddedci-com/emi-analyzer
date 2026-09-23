/**
 * Attaching a driver to a finished result (§8, §10).
 *
 * A solve is relative: it says one layout is 8 dB quieter than another, not how many µA/m
 * either produces. Because openEMS solves a linear structure, the response to any source
 * inside the excitation band is already in the result — so attaching a driver is arithmetic
 * on the recorded port spectra, not another run. M0 measured that this re-weighting is exact
 * to 0.01 dB wherever the stored record has energy at the frequency.
 *
 * The component's real work is refusing clearly. A driver can fail to say anything about a
 * frequency for three different reasons, and they are not interchangeable:
 *
 *   - the solve predates ports.json and cannot be re-weighted at all (re-run it);
 *   - the frequency is not a harmonic of this clock, so the clock does not drive it;
 *   - it is a harmonic, but a null of the driver's own spectrum.
 *
 * Showing "—" for all three would tell a user nothing about which one they are looking at.
 */

import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Badge, Group, Select, Stack, Text, Tooltip } from '@mantine/core'
import type { EmiApi } from '../lib/emiApi'
import { parseDriverDocument, sigmaDb, weakestSource } from '../lib/driverDocument'
import {
  applyDriver, portSpectrumFromJson, type PortSpectrum, type PortSpectrumJson,
} from '../lib/driverApply'
import { canAttachDriver, whyNoDriver, type SolveManifest } from './HotspotResults'
import { EXPERIMENTAL, Experimental } from './Experimental'

interface PortsJson {
  ports: {
    port: string
    at_dump_frequencies: PortSpectrumJson
    dense: PortSpectrumJson | null
  }[]
}

export interface DriverAttachProps {
  api: EmiApi
  runId: string
  projectId: string
  manifest: SolveManifest
  frequencyHz: number
  /** Peak of the selected layer at this frequency, in dB relative to the run's peak. */
  peakDb: number | null
  onOffsetChange?: (offsetDb: number | null) => void
}

export function DriverAttach({
  api, runId, projectId, manifest, frequencyHz, peakDb, onOffsetChange,
}: DriverAttachProps) {
  const [driverId, setDriverId] = useState<string | null>(null)
  const [ports, setPorts] = useState<PortSpectrum | null>(null)
  const [portsError, setPortsError] = useState<string | null>(null)

  const attachable = canAttachDriver(manifest)

  const drivers = useQuery({
    queryKey: ['emi', 'drivers', projectId],
    queryFn: () => api.listDrivers(projectId),
    enabled: attachable,
  })

  useEffect(() => {
    if (!attachable) return
    let cancelled = false
    api
      .artifactJson(runId, 'ports.json')
      .then((d) => {
        if (cancelled) return
        const first = (d as PortsJson).ports?.[0]
        if (!first) {
          setPortsError('This result records no excited port.')
          return
        }
        setPorts(portSpectrumFromJson(first.at_dump_frequencies))
      })
      .catch((e) => !cancelled && setPortsError((e as Error).message))
    return () => {
      cancelled = true
    }
  }, [api, runId, attachable])

  const selected = drivers.data?.find((d) => d.id === driverId) ?? null

  const applied = useMemo(() => {
    if (!selected || !ports) return null
    try {
      const parsed = parseDriverDocument(selected.document)
      return {
        parsed,
        result: applyDriver(parsed, ports, manifest.reference_magnitude, [frequencyHz]),
        error: null as string | null,
      }
    } catch (e) {
      return { parsed: null, result: null, error: (e as Error).message }
    }
  }, [selected, ports, manifest.reference_magnitude, frequencyHz])

  const offset = applied?.result?.offsetDb[0] ?? null
  useEffect(() => {
    onOffsetChange?.(offset)
  }, [offset, onOffsetChange])

  if (!attachable) {
    return (
      <Alert color="gray" variant="light" title="Results are relative">
        <Text size="xs">{whyNoDriver(manifest)}</Text>
      </Alert>
    )
  }

  const options = (drivers.data ?? []).map((d) => ({ value: d.id, label: d.name }))

  return (
    <Stack gap={6}>
      <Group gap="xs" align="flex-end">
        <Select
          label="Driver"
          size="xs"
          w={220}
          placeholder="none — relative dB"
          clearable
          data={options}
          value={driverId}
          onChange={setDriverId}
          disabled={options.length === 0}
        />
        {applied?.parsed && (
          <Tooltip label="The least certain number in this driver" withArrow>
            <Badge size="xs" variant="dot" mb={6}>
              {weakestSource(applied.parsed)} ±{sigmaDb(applied.parsed)} dB
            </Badge>
          </Tooltip>
        )}
      </Group>

      {options.length === 0 && !drivers.isLoading && (
        <Text size="xs" c="dimmed">
          No drivers yet. Add one on the Drivers tab to read this result in dBµA/m.
        </Text>
      )}

      {portsError && <Text size="xs" c="orange">{portsError}</Text>}
      {applied?.error && <Text size="xs" c="orange">{applied.error}</Text>}

      {selected && applied?.result && (
        offset === null ? (
          <Alert color="yellow" variant="light" title="This driver says nothing here">
            <Text size="xs">
              {applied.result.undriven.get(frequencyHz) ??
                'The driver does not drive this frequency.'}
            </Text>
          </Alert>
        ) : (
          <Group gap={6} align="center">
            <Text size="xs">
              Peak on this layer:{' '}
              <Text span ff="monospace" fw={600}>
                {peakDb === null ? '—' : `${(peakDb + offset).toFixed(1)} dBµA/m`}
              </Text>{' '}
              <Text span c="dimmed">
                ({peakDb === null ? '—' : `${peakDb.toFixed(1)} dB relative`}, driver offset{' '}
                {offset >= 0 ? '+' : ''}{offset.toFixed(1)} dB)
              </Text>
            </Text>
            <Experimental why={EXPERIMENTAL.hotspotMap} />
          </Group>
        )
      )}
    </Stack>
  )
}
