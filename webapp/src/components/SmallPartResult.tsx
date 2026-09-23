/**
 * A small-part solve's result: progress while it runs, then the map and the ports, and what
 * both mean.
 */

import { Alert, Card, Loader, Select, Stack, Text } from '@mantine/core'
import type { EmiApi, Run } from '../lib/emiApi'
import type { FieldOverlayData } from '../lib/overlay'
import { HotspotResults, type SolveManifest } from './HotspotResults'
import { PortNetwork } from './PortNetwork'
import { RunProgress } from './RunProgress'

export interface SmallPartResultProps {
  api: EmiApi
  projectId: string
  runs: Run[]
  run: Run | null
  manifest?: SolveManifest
  manifestError: string | null
  energyHistory: [number, number][]
  onSelectRun: (id: string) => void
  onOverlayChange: (overlay: FieldOverlayData | null) => void
  onGateChange: (gateDb: number) => void
}

function label(run: Run): string {
  const p = run.params as { coupon?: { nets?: string[] } } | undefined
  const what = p?.coupon?.nets?.join(' + ') ?? 'region'
  return `${what} · ${new Date(run.created_at).toLocaleString()} · ${run.status.replace('_', ' ')}`
}

export function SmallPartResult({
  api, projectId, runs, run, manifest, manifestError, energyHistory, onSelectRun,
  onOverlayChange, onGateChange,
}: SmallPartResultProps) {
  if (!run) return <Text size="sm" c="dimmed">No part has been solved yet.</Text>
  return (
    <Stack gap="sm">
      {runs.length > 1 && (
        <Select size="xs" allowDeselect={false} value={run.id} onChange={(v) => v && onSelectRun(v)}
                data={runs.map((r) => ({ value: r.id, label: label(r) }))} />
      )}
      {run.status !== 'done' ? (
        <>
          <RunProgress run={run} energyHistory={energyHistory} />
          {run.status === 'failed' && run.error && (
            <Alert color="red" variant="light" title="This part was not solved">
              <Text size="xs">{run.error}</Text>
            </Alert>
          )}
        </>
      ) : manifestError ? (
        <Alert color="red" variant="light">{manifestError}</Alert>
      ) : !manifest ? (
        <Loader size="sm" />
      ) : (
        <>
          <Card withBorder padding="xs" radius="sm">
            <Text size="xs">
              <b>What this shows:</b> where this net&apos;s current flows at each frequency,
              relative to the loudest point, and what its driven end sees looking in with 50 Ω at
              the other end.
            </Text>
            <Text size="xs" mt={4}>
              <b>What it does not:</b> a level a test lab would measure, the far field, or
              coupling into nets left out of the cut.
            </Text>
          </Card>
          <HotspotResults
            api={api} runId={run.id} projectId={projectId} manifest={manifest}
            onOverlayChange={onOverlayChange} onGateChange={onGateChange} smallPart
          />
          <PortNetwork api={api} runId={run.id} />
        </>
      )}
    </Stack>
  )
}
