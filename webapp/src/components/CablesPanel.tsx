/**
 * Cables: what leaves the board, and how much current each may carry (§4, §5).
 *
 * Below about 300 MHz a cable usually radiates more than the board it is attached to, which
 * is why this exists as its own tab rather than as a footnote on a solve. It needs no solve:
 * a cable's resonances depend on the cable, so the answer arrives in seconds.
 *
 * The rule the UI has to hold onto is that **unassigned is not zero**. A connector nobody has
 * decided about is not modelled at all, and compliance treats that as incomplete. Showing it
 * the same as a connector declared "never cabled" would tell someone their board is covered
 * when it is not.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Alert, Badge, Button, Card, Group, List, NumberInput, Select, Stack, Text, Title,
  Tooltip,
} from '@mantine/core'
import { TERMINAL_STATUSES, type EmiApi, type Run } from '../lib/emiApi'
import type { CableResult, CablesDoc } from '../lib/cableTypes'
import { CableBudgetChart } from './CableBudgetChart'
import { EXPERIMENTAL, Experimental } from './Experimental'
import cableLibrary from '../../../worker/emi_worker/cables/library.json'

interface LibraryCable {
  id: string
  name: string
  length_m: number
  length_options_m: number[]
  far_end: string
}

const LIBRARY: LibraryCable[] = (cableLibrary as { cables: LibraryCable[] }).cables

interface Props {
  api: EmiApi
  projectId: string
  boardId?: string
  /** A previous cable run to show, when there is one. */
  runId?: string
  /** Clock harmonics to mark on the chart, when a driver declares them. */
  harmonics?: number[]
  /** Workers connected right now. With none, a run would sit in the queue saying nothing. */
  workersOnline?: number
  /**
   * What each connector is cabled with. Owned by the page rather than by this panel, because
   * a solve needs it too: §7's gap port is part of the mesh, so "which connectors carry a
   * cable" has to be decided before the run rather than attached to the result afterwards the
   * way a driver is.
   */
  assignments: Record<string, Assignment>
  onAssignmentsChange: (next: Record<string, Assignment>) => void
}

export type Assignment = { type: string; length_m?: number }

export function CablesPanel({
  api, projectId, boardId, runId, harmonics = [], workersOnline, assignments, onAssignmentsChange,
}: Props) {
  const [startedRunId, setStartedRunId] = useState<string | undefined>(runId)

  // The page finds the last finished run asynchronously, so it usually arrives after this
  // panel is mounted. Without this the tab offered to start a run that had already been run.
  useEffect(() => {
    if (runId && !startedRunId) setStartedRunId(runId)
  }, [runId, startedRunId])

  // What the run on screen was started with, so an assignment changed afterwards can say so
  // rather than being silently absent from the result.
  const ranWith = useRef<string | null>(null)

  // Poll while the run is in flight. Without this the panel said "try again shortly" and the
  // only button on screen started a *second* run -- so the honest reading of the UI was that
  // the first one had failed.
  const doc = useQuery({
    queryKey: ['emi', 'cables', startedRunId],
    queryFn: () => api.fetchCables(startedRunId!),
    enabled: !!startedRunId,
    retry: 20,
    retryDelay: 1500,
  })

  // The artifact alone cannot tell a slow run from a failed one: both are a 404. Watching the
  // run itself is what turns "waiting for the result" -- which used to stay on screen forever
  // -- into a reason and a retry.
  const run = useQuery({
    queryKey: ['emi', 'run', startedRunId],
    queryFn: () => api.getRun(startedRunId!),
    enabled: !!startedRunId && !doc.data,
    refetchInterval: (q) =>
      q.state.data && TERMINAL_STATUSES.includes(q.state.data.status) ? false : 1500,
  })
  const failed = run.data && (run.data.status === 'failed' || run.data.status === 'timed_out')

  const retry = useMutation({
    mutationFn: () => api.retryRun(startedRunId!),
    onSuccess: () => { run.refetch(); doc.refetch() },
  })

  const start = useMutation({
    mutationFn: (): Promise<Run> => {
      if (!boardId) throw new Error('this project has no ingested board')
      return api.createCableRun(projectId, boardId, assignments)
    },
    onSuccess: (started) => {
      ranWith.current = JSON.stringify(assignments)
      setStartedRunId(started.id)
    },
  })

  // Connectors come from a previous run's report; before the first run there is nothing to
  // list, because finding them means parsing the board and that happens on the worker.
  const connectors = useMemo(() => {
    const d = doc.data as CablesDoc | undefined
    if (!d) return []
    return [
      ...d.cables.map((c) => ({ ref: c.ref, suggested: null as string | null, footprint: '' })),
      ...d.unassigned.map((u) => ({ ref: u.ref, suggested: u.suggested, footprint: u.footprint })),
    ].sort((a, b) => a.ref.localeCompare(b.ref))
  }, [doc.data])

  const setAssignment = (ref: string, type: string | null, length?: number) => {
    const next = { ...assignments }
    if (!type) delete next[ref]
    else next[ref] = { type, ...(length ? { length_m: length } : {}) }
    onAssignmentsChange(next)
  }

  const d = doc.data as CablesDoc | undefined
  const modelled = (d?.cables ?? []).filter((c) => c.cable_id)
  // Connectors the *result* says are unassigned, minus any that have been assigned since.
  const stillUnassigned = (d?.unassigned ?? []).filter((u) => !assignments[u.ref])
  const changedSinceRun =
    !!d && ranWith.current !== null && ranWith.current !== JSON.stringify(assignments)

  return (
    <Stack gap="sm">
      <Group justify="space-between" align="center">
        <Stack gap={0}>
          <Title order={5}>Cables</Title>
          <Text size="xs" c="dimmed">
            Below about 300 MHz a cable usually radiates more than the board. This runs in
            seconds and needs no solve.
          </Text>
          <Experimental why={EXPERIMENTAL.cableBudget} label="budget, not a prediction" mb={0} />
        </Stack>
        <Tooltip label="The board is still being processed" disabled={!!boardId} withArrow>
          <Button size="xs" variant="light" loading={start.isPending}
                  disabled={!boardId}
                  onClick={() => start.mutate()}>
            {startedRunId ? 'Run again' : 'Find connectors'}
          </Button>
        </Tooltip>
      </Group>

      {start.error && (
        <Alert color="red" variant="light">{(start.error as Error).message}</Alert>
      )}
      {startedRunId && !d && !failed && (
        <Text size="sm" c="dimmed">
          {workersOnline === 0
            ? 'Waiting for a worker to pick this up — none is connected yet.'
            : 'Running — a cable run takes a few seconds.'}
        </Text>
      )}
      {failed && (
        <Alert color="red" variant="light" title="The cable run did not finish">
          <Stack gap="xs" align="flex-start">
            <Text size="xs">{run.data?.error || 'No reason was reported.'}</Text>
            <Button size="compact-xs" variant="light" loading={retry.isPending}
                    onClick={() => retry.mutate()}>
              Try again
            </Button>
          </Stack>
        </Alert>
      )}
      {changedSinceRun && (
        <Alert color="blue" variant="light" title="Assignments have changed">
          <Text size="xs">
            The result below is from the previous set of cables. Press{' '}
            <Text span fw={600}>Run again</Text> to bring it up to date.
          </Text>
        </Alert>
      )}

      {!startedRunId && (
        <Text size="sm" c="dimmed">
          Run this once to list the connectors on this board and what the library suggests for
          each. Nothing is assumed: a connector you do not assign is not modelled at all.
        </Text>
      )}

      {/*
        One row per connector, stacked rather than tabulated. A three-column table put the
        cable Select -- the only control that matters here -- in 73 px while reserving 110 px
        for a Length cell that is empty until a cable is chosen. The panel is a fixed narrow
        column beside the board, so the width is not going to arrive later.
      */}
      {connectors.length > 0 && (
        <Stack gap={6}>
          {connectors.map((c) => {
            const chosen = assignments[c.ref]
            const cable = LIBRARY.find((l) => l.id === chosen?.type)
            const declaredNone = chosen?.type === 'none'
            return (
              <Card key={c.ref} withBorder padding="xs" radius="sm">
                <Group gap={6} mb={4} wrap="nowrap">
                  <Text size="xs" fw={600} ff="monospace">{c.ref}</Text>
                  {declaredNone && (
                    <Badge size="xs" variant="light" color="gray">never cabled</Badge>
                  )}
                  {cable && (
                    <Badge size="xs" variant="light" color="teal">modelled</Badge>
                  )}
                  {!chosen && c.suggested && (
                    <Tooltip label={`Suggested from ${c.footprint}`} withArrow>
                      <Badge size="xs" variant="light" color="gray" tt="none"
                             style={{ cursor: 'help' }}>
                        suggests {c.suggested}
                      </Badge>
                    </Tooltip>
                  )}
                  {!chosen && !c.suggested && (
                    <Badge size="xs" variant="light" color="yellow">not modelled</Badge>
                  )}
                </Group>
                <Group gap={6} wrap="nowrap" align="flex-end">
                  <Select
                    size="xs"
                    style={{ flex: 1, minWidth: 0 }}
                    placeholder="not modelled"
                    clearable
                    data={[
                      { value: 'none', label: 'never cabled' },
                      ...LIBRARY.map((l) => ({ value: l.id, label: l.name })),
                    ]}
                    value={chosen?.type ?? null}
                    onChange={(v) => setAssignment(c.ref, v, undefined)}
                  />
                  {cable && (
                    <NumberInput
                      size="xs"
                      w={86}
                      step={0.1}
                      decimalScale={2}
                      min={0.05}
                      value={chosen?.length_m ?? cable.length_m}
                      onChange={(v) =>
                        setAssignment(c.ref, cable.id, typeof v === 'number' ? v : undefined)}
                      rightSection={<Text size="10px" c="dimmed" pr={4}>m</Text>}
                      rightSectionWidth={22}
                    />
                  )}
                  {c.suggested && !chosen && (
                    <Button size="compact-xs" variant="light"
                            onClick={() => setAssignment(c.ref, c.suggested!, undefined)}>
                      use
                    </Button>
                  )}
                </Group>
              </Card>
            )
          })}
        </Stack>
      )}

      {stillUnassigned.length > 0 && (
        <Alert color="yellow" variant="light" title="Not every connector is decided">
          <Text size="xs">
            {stillUnassigned.map((u) => u.ref).join(', ')}{' '}
            {stillUnassigned.length === 1 ? 'has' : 'have'} no cable assigned, so{' '}
            {stillUnassigned.length === 1 ? 'it is' : 'they are'} not modelled at all — which is
            not the same as carrying nothing. Assign a cable, or mark it{' '}
            <Text span fw={600}>never cabled</Text> if it is a debug header that never leaves
            the bench.
          </Text>
        </Alert>
      )}

      {modelled.map((c: CableResult) => (
        <Card key={c.ref} withBorder padding="sm">
          <Stack gap="xs">
            <Group justify="space-between" align="baseline">
              <Group gap="xs">
                <Text size="sm" fw={600} ff="monospace">{c.ref}</Text>
                <Text size="sm">{c.cable_name}</Text>
                <Badge size="xs" variant="light" tt="none">{c.length_m} m</Badge>
                <Badge size="xs" variant="light" color="gray" tt="none">
                  far end {c.far_end}
                </Badge>
              </Group>
              {c.tightest && (
                <Text size="xs" c="dimmed">
                  tightest{' '}
                  <Text span ff="monospace" fw={600}>
                    {c.tightest.max_current_dbua.toFixed(0)} dBµA
                  </Text>{' '}
                  at {(c.tightest.frequency_hz / 1e6).toFixed(0)} MHz
                </Text>
              )}
            </Group>

            <CableBudgetChart
              points={c.points ?? []}
              peaks={c.radiation_peaks_hz ?? []}
              marks={harmonics}
            />

            {c.grid_too_coarse && (
              <Text size="xs" c="dimmed">
                The frequency grid is too coarse to pick out resonances here, so an empty list
                means &ldquo;not looked at closely enough&rdquo; rather than &ldquo;none&rdquo;.
              </Text>
            )}
            <Text size="xs" c="dimmed">{c.shield}</Text>
          </Stack>
        </Card>
      ))}

      {d && (
        <Card withBorder padding="sm">
          <Stack gap={4}>
            <Text size="xs" fw={600} tt="uppercase" c="dimmed">What this assumes</Text>
            <List size="xs" spacing={2}>
              {d.assumptions.map((a) => <List.Item key={a}>{a}</List.Item>)}
            </List>
            <Text size="10px" c="dimmed" mt={4}>
              Solver: {d.solver}. Limits: {d.standard_id}.
            </Text>
          </Stack>
        </Card>
      )}
    </Stack>
  )
}
