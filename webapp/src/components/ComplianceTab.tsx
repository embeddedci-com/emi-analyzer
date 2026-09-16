/**
 * The Compliance tab: assemble what the project knows, run the estimate, show it.
 *
 * The assembly happens here because only the browser knows which solve the user is looking at,
 * which driver they attached and which cables they declared. The **gate** deliberately does not
 * happen here: the worker decides whether the inputs are whole, because putting the one rule
 * that stops a number being published into the one place an API client can skip would defeat
 * it.
 *
 * What this screen has to do well is the case where there is nothing to show yet, which is most
 * of the time on a new project. So the readiness list is the primary content until it is empty,
 * not a footnote under an empty chart.
 */

import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Alert, Badge, Button, Card, Group, List, Loader, Stack, Text, Title, Tooltip,
} from '@mantine/core'
import type { EmiApi, Run } from '../lib/emiApi'
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
  /** A previous compliance run to show. */
  runId?: string
}

/** What has to be true before the estimate can say anything, checked before spending a run. */
interface Readiness {
  ok: boolean
  label: string
  detail: string
}

export function ComplianceTab({
  api, projectId, boardId, solveRunId, solveManifest, cableAssignments,
  findings = [], runId,
}: ComplianceTabProps) {
  const [startedRunId, setStartedRunId] = useState<string | undefined>(runId)

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
        : 'Run a solve first — without one there is no board-side radiation to estimate.',
    },
    {
      ok: hasFarField,
      label: "The board's far field",
      detail: hasFarField
        ? 'That solve recorded the surface the board\'s own radiation is computed from.'
        : 'That solve did not record a far-field box, so the board contributes nothing. '
          + 'Turn on "Record the board\'s far field" in Solve and run it again.',
    },
    {
      ok: assigned.length > 0,
      label: 'Cables declared',
      detail: assigned.length > 0
        ? `${assigned.length} connector${assigned.length === 1 ? '' : 's'} declared on the Cables tab.`
        : 'Declare what each connector carries on the Cables tab — an undeclared connector is '
          + 'not modelled at all, which is different from carrying nothing.',
    },
    {
      ok: cableRefs.length > 0,
      label: 'Cable drive modelled',
      detail: cableRefs.length > 0
        ? `Gap ports on ${cableRefs.join(', ')}.`
        : 'That solve fitted no gap port, so a cable can be given a budget but not a predicted '
          + 'level. Turn on "Model cable emissions" in Solve.',
    },
  ]
  const blockers = readiness.filter((r) => !r.ok)

  const params = useMemo(() => ({
    standard_id: 'fcc-15b-radiated-3m',
    connectors: assigned,
    cable_assignments: cableAssignments,
    driven_ports: solveManifest ? ['p1'] : [],
    far_field_refs: hasFarField ? ['p1'] : [],
    findings,
    // Paths are assembled by the worker from the artifacts it can read; what the browser
    // supplies is which run they came from.
    solve_run_id: solveRunId,
    paths: [],
  }), [assigned, cableAssignments, solveManifest, hasFarField, findings, solveRunId])

  const start = useMutation({
    mutationFn: (): Promise<Run> => {
      if (!boardId) throw new Error('this project has no ingested board')
      return api.createComplianceRun(projectId, boardId, params)
    },
    onSuccess: (run) => setStartedRunId(run.id),
  })

  // Polls while the run is in flight, for the same reason the Cables panel does: the artifact
  // does not exist until the worker uploads it, and a single failed fetch is indistinguishable
  // from a failed run unless the UI keeps asking.
  const doc = useQuery({
    queryKey: ['emi', 'compliance', startedRunId],
    queryFn: () => api.fetchCompliance(startedRunId!),
    enabled: !!startedRunId,
    retry: 20,
    retryDelay: 1500,
  })

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
            carries. Not a pre-compliance test.
          </Text>
        </Stack>
        <Tooltip
          label={blockers.length > 0
            ? 'Runs anyway and shows exactly what is missing'
            : 'A few seconds — no solver involved'}
          withArrow
        >
          <Button size="xs" onClick={() => start.mutate()}
                  loading={start.isPending} disabled={!boardId}>
            {startedRunId ? 'Re-estimate' : 'Estimate'}
          </Button>
        </Tooltip>
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

      {startedRunId && doc.isLoading && <Loader size="sm" />}
      {startedRunId && doc.isError && (
        <Alert color="yellow" variant="light" title="No result yet">
          <Text size="xs">
            The estimate ran but produced nothing to read. That is a worker failure rather
            than a missing input — open Runs, at the top of this page, to see what the run
            reported.
          </Text>
        </Alert>
      )}
      {doc.data && <CompliancePanel doc={doc.data} />}
    </Stack>
  )
}
