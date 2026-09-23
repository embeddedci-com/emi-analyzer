/**
 * The Compliance tab: name the solve and the driver, run the estimate, show it.
 *
 * The browser used to assemble the inputs and sent an empty list of paths with a note that the
 * worker would fill them in, which nothing did. Now it names what only the user knows -- which
 * solve, which driver, which connectors carry no cable, the power and the enclosure -- and the
 * worker reads the rest from the solve and the board. The **gate** is the worker's alone:
 * putting the one rule that stops a number being published into the one place an API client
 * can skip would defeat it.
 *
 * The run is the project's newest compliance run, passed in from the page, so the result
 * survives a reload. It is polled by status, and a failed run shows the worker's own error
 * rather than a guess about what went wrong.
 */

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Badge, Button, Card, Group, List, Loader, Select, Stack, Text, Title, Tooltip,
} from '@mantine/core'
import {
  TERMINAL_STATUSES, type ComplianceParams, type EmiApi, type Run,
} from '../lib/emiApi'
import { complianceParams, runPhase } from '../lib/complianceRun'
import type { SolveManifest } from './HotspotResults'
import { CompliancePanel } from './CompliancePanel'
import { EXPERIMENTAL, Experimental } from './Experimental'

export interface ComplianceTabProps {
  api: EmiApi
  projectId: string
  boardId?: string
  /** The solve the Results tab is showing, if it finished. */
  solveRunId?: string
  solveManifest?: SolveManifest & { cable_ports?: string[]; far_field?: unknown }
  /** Connector -> cable, from the Cables tab. */
  cableAssignments: Record<string, { type: string; length_m?: number }>
  /** Rule findings from ingest, so recommendations can be drawn from them. */
  findings?: Record<string, unknown>[]
  /** The project's newest compliance run, so its result survives a reload. */
  runId?: string
}

/** What has to be true before the estimate can say anything, checked before spending a run. */
interface Readiness {
  ok: boolean
  label: string
  detail: string
}

const STANDARD_ID = 'fcc-15b-radiated-3m'

export function ComplianceTab({
  api, projectId, boardId, solveRunId, solveManifest, cableAssignments,
  findings = [], runId,
}: ComplianceTabProps) {
  const queryClient = useQueryClient()
  const [startedRunId, setStartedRunId] = useState<string | undefined>(runId)
  // A newer run from the page (another tab, a reload) replaces the one on screen.
  useEffect(() => {
    if (runId) setStartedRunId(runId)
  }, [runId])

  const drivers = useQuery({
    queryKey: ['emi', 'drivers', projectId],
    queryFn: () => api.listDrivers(projectId),
  })

  // The run on screen, polled by status: its artifact does not exist until the worker has
  // uploaded it, so fetching early is a miss, not a failure.
  const run = useQuery({
    queryKey: ['emi', 'run', startedRunId],
    queryFn: () => api.getRun(startedRunId!),
    enabled: !!startedRunId,
    refetchInterval: (q) => {
      const status = (q.state.data as Run | undefined)?.status
      return status && TERMINAL_STATUSES.includes(status) ? false : 1500
    },
  })
  const status = run.data?.status
  const previous = run.data?.params as ComplianceParams | undefined

  // The declarations start from the last run's, so a reload does not lose them.
  const [driverId, setDriverId] = useState<string | null>(null)
  const [power, setPower] = useState<string>('')
  const [enclosure, setEnclosure] = useState<string>('none')
  useEffect(() => {
    if (!previous) return
    setDriverId((d) => d ?? previous.driver_id ?? null)
    setPower((p) => p || previous.power || '')
    setEnclosure((e) => (e === 'none' ? previous.enclosure ?? 'none' : e))
  }, [previous])
  useEffect(() => {
    if (driverId === null && drivers.data?.length === 1) setDriverId(drivers.data[0].id)
  }, [drivers.data, driverId])

  const doc = useQuery({
    queryKey: ['emi', 'compliance', startedRunId],
    queryFn: () => api.fetchCompliance(startedRunId!),
    enabled: !!startedRunId && status === 'done',
    staleTime: Infinity,
  })

  const hasSolve = !!solveRunId && !!solveManifest
  const hasFarField = !!solveManifest?.far_field
  const cableRefs = solveManifest?.cable_ports ?? []
  const assigned = Object.keys(cableAssignments)

  const readiness: Readiness[] = [
    {
      ok: hasSolve,
      label: 'A finished solve',
      detail: hasSolve
        ? 'Using the solve shown on the Results tab.'
        : 'Run a solve first. Without one there is no radiation to estimate.',
    },
    {
      ok: hasFarField,
      label: "The board's far field",
      detail: hasFarField
        ? 'That solve recorded the far-field box.'
        : 'That solve has no far-field box, so the board contributes nothing. Turn on '
          + '"Record the board\'s far field" in Solve and run it again.',
    },
    {
      ok: !!driverId,
      label: 'A driver',
      detail: driverId
        ? 'Its voltage sets the level at every harmonic.'
        : 'Pick the driver that drives the solve\'s port. Without one the result is relative.',
    },
    {
      ok: assigned.length > 0 || cableRefs.length > 0,
      label: 'Cables declared',
      detail: cableRefs.length > 0
        ? `The solve modelled cables on ${cableRefs.join(', ')}.`
        : assigned.length > 0
          ? `${assigned.length} connector${assigned.length === 1 ? '' : 's'} declared on the `
            + 'Cables tab. A declared cable only counts once a solve models it.'
          : 'Declare what each connector carries on the Cables tab. An undeclared connector '
            + 'is not modelled at all, which is different from carrying nothing.',
    },
    {
      ok: !!power,
      label: 'Power',
      detail: power ? 'Declared.' : 'Say how the product is powered.',
    },
  ]

  const start = useMutation({
    mutationFn: (): Promise<Run> => {
      if (!boardId) throw new Error('This project has no analyzed board.')
      const params = complianceParams({
        standardId: STANDARD_ID, solveRunId, driverId, cableAssignments, power, enclosure,
        findings,
      })
      return api.createComplianceRun(projectId, boardId, params)
    },
    onSuccess: (r) => {
      setStartedRunId(r.id)
      queryClient.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
    },
  })

  const phase = runPhase(run.data)
  const running = !!startedRunId && (phase.phase === 'running' || run.isLoading)
  const driverOptions = useMemo(
    () => (drivers.data ?? []).map((d) => ({ value: d.id, label: `${d.name} (${d.kind})` })),
    [drivers.data],
  )

  return (
    <Stack gap="sm">
      <Group justify="space-between" align="flex-start">
        <Stack gap={0}>
          <Group gap="xs">
            <Title order={5}>Compliance</Title>
            <Experimental why={EXPERIMENTAL.complianceEstimate} />
          </Group>
          <Text size="xs" c="dimmed">
            An estimated margin against FCC Part 15 Class B at 3 m, with the uncertainty it
            carries. Experimental and uncalibrated. Not a pre-compliance test.
          </Text>
        </Stack>
        <Tooltip label="Runs anyway and lists exactly what is missing" withArrow>
          <Button size="xs" onClick={() => start.mutate()}
                  loading={start.isPending || running} disabled={!boardId}>
            {startedRunId ? 'Estimate again' : 'Estimate'}
          </Button>
        </Tooltip>
      </Group>

      <Group gap="sm" align="flex-end" wrap="wrap">
        <Select
          size="xs" label="Driver" w={220} placeholder="Pick a driver"
          data={driverOptions} value={driverId} onChange={setDriverId}
          nothingFoundMessage="No drivers yet. Add one on the Drivers tab."
        />
        <Select
          size="xs" label="Power" w={160} placeholder="Declare it"
          data={[{ value: 'dc', label: 'DC (battery or adapter)' },
            { value: 'mains', label: 'Mains' }]}
          value={power || null} onChange={(v) => setPower(v ?? '')}
        />
        <Select
          size="xs" label="Enclosure" w={140}
          data={[{ value: 'none', label: 'None' }, { value: 'plastic', label: 'Plastic' },
            { value: 'metal', label: 'Metal' }]}
          value={enclosure} onChange={(v) => setEnclosure(v ?? 'none')}
        />
      </Group>

      <Card withBorder padding="sm" radius="md">
        <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={6}>
          What this estimate can see
        </Text>
        <List size="xs" spacing={6}>
          {readiness.map((r) => (
            <List.Item
              key={r.label}
              icon={
                <Badge size="xs" variant="light" color={r.ok ? 'teal' : 'gray'} circle>
                  {r.ok ? '✓' : '–'}
                </Badge>
              }
            >
              <Text size="xs" fw={r.ok ? 400 : 600}>{r.label}</Text>
              <Text size="xs" c="dimmed">{r.detail}</Text>
            </List.Item>
          ))}
        </List>
      </Card>

      {start.isError && (
        <Alert color="red" variant="light" title="Could not start the estimate">
          <Text size="xs">{(start.error as Error).message}</Text>
        </Alert>
      )}

      {running && (
        <Group gap="xs">
          <Loader size="xs" />
          <Text size="xs" c="dimmed">{phase.message || 'Loading the last estimate'}</Text>
        </Group>
      )}
      {run.isError && (
        <Alert color="red" variant="light" title="The estimate could not be read">
          <Text size="xs">{(run.error as Error).message}</Text>
        </Alert>
      )}
      {phase.phase === 'failed' && (
        <Alert color="red" variant="light" title="The estimate failed">
          <Text size="xs">{phase.message}</Text>
        </Alert>
      )}
      {status === 'done' && doc.isError && (
        <Alert color="red" variant="light" title="The result could not be read">
          <Text size="xs">{(doc.error as Error).message}</Text>
        </Alert>
      )}
      {doc.data && <CompliancePanel doc={doc.data} />}
    </Stack>
  )
}
