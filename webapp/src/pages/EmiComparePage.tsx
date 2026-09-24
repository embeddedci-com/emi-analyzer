/**
 * Tools -> EMI Analyzer -> one board -> compare two versions.
 *
 * The analyzer's numbers rest on generic models, so the honest use of them is relative: did
 * this change make the board better or worse. This page puts two versions of one board side
 * by side, checks first because they are what every version has, then the on-demand results
 * where both versions have run them. A version missing a result says so and offers to run it,
 * rather than drawing half a comparison.
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Accordion, Alert, Badge, Box, Button, Card, Container, Group, Loader, SegmentedControl, Select,
  SimpleGrid, Stack, Table, Tabs, Text, Title,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router'
import { useEmiBase } from '../host'
import type { BoardDoc, NetReport, RuleFinding } from '../lib/boardTypes'
import catalogue from '../lib/ruleCatalogue.json'
import { EmiApi, TERMINAL_STATUSES, type Run } from '../lib/emiApi'
import { reportFromBoard } from '../lib/netsCsv'
import type { CablesDoc } from '../lib/cableTypes'
import type { TransientDoc, TransientParams } from '../lib/transientTypes'
import {
  LENGTH_EPSILON_MM, defaultPair, diffCables, diffEsd, diffFindings, diffNets, findingsHeadline,
  groupByRule, latestDone, resample, versionsOf,
  type CableChange, type EsdChange, type FindingChange, type FindingsDiff, type FindingStatus,
  type Version,
} from '../lib/compare'
import { FINDING_MARKER_HEX, findingMarkerColor } from '../lib/markers'
import { BoardCanvas } from '../components/BoardCanvas'
import { CableBudgetChart } from '../components/CableBudgetChart'
import { RunProgress } from '../components/RunProgress'
import { TransientChart } from '../components/TransientChart'

const RULE_LABEL: Record<string, string> = Object.fromEntries(
  (catalogue as { id: string; title: string }[]).map((r) => [r.id, r.title]),
)
const SEVERITY_COLOR: Record<string, string> = { critical: 'red', warning: 'yellow', info: 'blue' }
const STATUS_COLOR: Record<FindingChange['status'], string> = { new: 'red', fixed: 'green', unchanged: 'gray' }

const BEFORE_COLOR = 'var(--mantine-color-gray-6)'
const AFTER_COLOR = 'var(--mantine-color-orange-6)'

export interface EmiComparePageProps {
  api: EmiApi
}

const label = (v: Version) => `v${v.number}`
const fmtMm = (v?: number) => (v === undefined ? '' : `${v.toFixed(2)} mm`)
const fmtV = (v?: number) => (v === undefined ? '' : `${Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)} V`)
const fmtHz = (f: number) => (f >= 1e9 ? `${(f / 1e9).toFixed(2)} GHz` : `${(f / 1e6).toFixed(0)} MHz`)
const signed = (v: number, digits: number) => `${v > 0 ? '+' : ''}${v.toFixed(digits)}`

export function EmiComparePage({ api }: EmiComparePageProps) {
  const { projectId = '' } = useParams()
  const base = useEmiBase()
  const qc = useQueryClient()
  const [search, setSearch] = useSearchParams()
  const [tab, setTab] = useState<string | null>('findings')
  const [focus, setFocus] = useState<{ x: number; y: number; zoom?: number } | null>(null)
  const [net, setNet] = useState<string | null>(null)
  const [canvasSide, setCanvasSide] = useState<'after' | 'before'>('after')

  const project = useQuery({
    queryKey: ['emi', 'project', projectId],
    queryFn: () => api.getProject(projectId),
    enabled: !!projectId,
  })
  const boards = useQuery({
    queryKey: ['emi', 'boards', projectId],
    queryFn: () => api.listBoards(projectId),
    enabled: !!projectId,
  })
  const versions = useMemo(() => versionsOf(boards.data ?? []), [boards.data])
  const pair = defaultPair(versions, search.get('before'), search.get('after'))
  const [before, after] = pair ?? [null, null]

  // Polled only while something is running, the same way the board page does it.
  const [pollMs, setPollMs] = useState<number | false>(false)
  const runs = useQuery({
    queryKey: ['emi', 'runs', projectId],
    queryFn: () => api.listRuns(projectId, 200),
    enabled: !!projectId,
    refetchInterval: pollMs,
  })
  useEffect(() => {
    const active = runs.data?.some((r) => !TERMINAL_STATUSES.includes(r.status)) ?? false
    setPollMs((prev) => {
      const next = active ? 1500 : (false as const)
      return prev === next ? prev : next
    })
  }, [runs.data])

  const a = useVersionData(api, runs.data ?? [], before)
  const b = useVersionData(api, runs.data ?? [], after)

  const findings = useMemo(
    () => (a.rules && b.rules ? diffFindings(a.rules, b.rules) : null),
    [a.rules, b.rules],
  )
  const nets = useMemo(() => (a.nets && b.nets ? diffNets(a.nets, b.nets) : null), [a.nets, b.nets])
  const cables = useMemo(() => (a.cables && b.cables ? diffCables(a.cables, b.cables) : null), [a.cables, b.cables])
  const esd = useMemo(() => (a.esd && b.esd ? diffEsd(a.esd, b.esd) : null), [a.esd, b.esd])

  const choose = (side: 'before' | 'after', id: string | null) => {
    if (!id || !before || !after) return
    const next = { before: before.board.id, after: after.board.id, [side]: id }
    setSearch(next, { replace: true })
  }

  const openFinding = (f: RuleFinding, side: 'before' | 'after') => {
    setCanvasSide(side)
    if (f.net) setNet(f.net)
    if (f.x != null && f.y != null) setFocus({ x: f.x, y: f.y, zoom: 28 })
    setTab('board')
  }

  const boardLink = (v: Version) => `${base}/${projectId}?version=${v.board.id}`

  if (boards.isSuccess && versions.length < 2) {
    return (
      <Container size="sm" py="lg">
        <Alert color="blue" variant="light" title="Only one version so far">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">
              Upload a changed board file from the board&apos;s menu to compare it with this one.
            </Text>
            <Button size="xs" variant="light" component={Link} to={`${base}/${projectId}`}>
              Back to the board
            </Button>
          </Stack>
        </Alert>
      </Container>
    )
  }

  const options = [...versions].reverse().map((v) => ({
    value: v.board.id,
    label: `${label(v)} · ${v.filename} · ${new Date(v.board.created_at).toLocaleDateString()}`,
  }))

  return (
    <Container size="xl" py="md">
      <Stack gap="md">
        <Group justify="space-between" align="flex-end" wrap="wrap">
          <div>
            <Title order={3}>Compare versions</Title>
            <Text size="sm" c="dimmed">{project.data?.name}</Text>
          </div>
          <Button size="xs" variant="default" component={Link}
                  to={after ? boardLink(after) : `${base}/${projectId}`}>
            Back to the board
          </Button>
        </Group>

        {(boards.isLoading || runs.isLoading) && <Loader size="sm" />}
        {(boards.isError || runs.isError) && (
          <Alert color="red" variant="light" title="The versions could not be loaded">
            <Text size="xs">{((boards.error ?? runs.error) as Error).message}</Text>
          </Alert>
        )}

        {before && after && (
          <>
            <Group gap="sm" align="flex-end" wrap="wrap">
              <Select label="Before" size="xs" w={300} allowDeselect={false} data={options}
                      value={before.board.id} onChange={(id) => choose('before', id)} />
              <Select label="After" size="xs" w={300} allowDeselect={false} data={options}
                      value={after.board.id} onChange={(id) => choose('after', id)} />
              <Button size="xs" variant="subtle"
                      onClick={() => setSearch({ before: after.board.id, after: before.board.id }, { replace: true })}>
                Swap
              </Button>
            </Group>

            <Summary findings={findings} loading={!findings && (a.pending || b.pending)} />

            <Tabs value={tab} onChange={setTab} keepMounted={false}>
              <Tabs.List>
                <Tabs.Tab value="findings">Findings</Tabs.Tab>
                <Tabs.Tab value="nets">Nets</Tabs.Tab>
                <Tabs.Tab value="cables">Cables</Tabs.Tab>
                <Tabs.Tab value="esd">ESD</Tabs.Tab>
                <Tabs.Tab value="board">Board</Tabs.Tab>
              </Tabs.List>

              <Tabs.Panel value="findings" pt="md">
                {findings ? (
                  <FindingsView diff={findings} onOpen={openFinding} />
                ) : (
                  <Waiting a={a} b={b} before={before} after={after} what="checks" />
                )}
              </Tabs.Panel>

              <Tabs.Panel value="nets" pt="md">
                {nets ? (
                  <NetsView changes={nets} partial={!!(a.nets?.partial || b.nets?.partial)} />
                ) : (
                  <Waiting a={a} b={b} before={before} after={after} what="net report" />
                )}
              </Tabs.Panel>

              <Tabs.Panel value="cables" pt="md">
                {cables ? (
                  <CablesView changes={cables} before={before} after={after} />
                ) : (
                  <MissingRun
                    api={api} projectId={projectId} kind="cable" versions={[before, after]}
                    data={[a, b]} boardLink={boardLink}
                    onStarted={() => { setPollMs(1500); qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] }) }}
                  />
                )}
              </Tabs.Panel>

              <Tabs.Panel value="esd" pt="md">
                {esd ? (
                  <EsdView changes={esd} before={before} after={after}
                           levels={[a.esd!.kv, b.esd!.kv]} />
                ) : (
                  <MissingRun
                    api={api} projectId={projectId} kind="transient" versions={[before, after]}
                    data={[a, b]} boardLink={boardLink}
                    onStarted={() => { setPollMs(1500); qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] }) }}
                  />
                )}
              </Tabs.Panel>

              <Tabs.Panel value="board" pt="md">
                <CanvasView
                  api={api} side={canvasSide} onSide={setCanvasSide}
                  data={canvasSide === 'after' ? b : a}
                  before={before} after={after}
                  findings={findings} focus={focus} net={net}
                  onPick={(f) => openFinding(f, canvasSide)}
                />
              </Tabs.Panel>
            </Tabs>
          </>
        )}
      </Stack>
    </Container>
  )
}

// ---- data ----

interface VersionData {
  ingest: Run | null
  /** The newest ingest of any status, to say why there is nothing yet. */
  anyIngest: Run | null
  rules: RuleFinding[] | null
  nets: NetReport | null
  cableRun: Run | null
  cables: CablesDoc | null
  /** A cable or ESD run still queued or running on this version. */
  cableActive: Run | null
  esdActive: Run | null
  esdRun: Run | null
  esd: TransientDoc | null
  /** Something this version has is still being fetched or run. */
  pending: boolean
  error: Error | null
}

function useVersionData(api: EmiApi, runs: Run[], version: Version | null): VersionData {
  const id = version?.board.id ?? ''
  const ingest = latestDone(runs, id, 'ingest')
  const anyIngest = runs
    .filter((r) => r.board_id === id && r.kind === 'ingest')
    .sort((p, q) => Date.parse(q.created_at) - Date.parse(p.created_at))[0] ?? null
  const cableRun = latestDone(runs, id, 'cable')
  const esdRun = latestDone(runs, id, 'transient')
  const active = (kind: Run['kind']) =>
    runs.find((r) => r.board_id === id && r.kind === kind && !TERMINAL_STATUSES.includes(r.status)) ?? null

  const rules = useQuery({
    queryKey: ['emi', 'rules', ingest?.id],
    queryFn: () => api.fetchRules(ingest!.id),
    enabled: !!ingest,
    staleTime: Infinity,
  })
  // The report the CSV export uses; a board analyzed before it existed is read from board.json.
  const nets = useQuery({
    queryKey: ['emi', 'compare-nets', ingest?.id],
    queryFn: async () =>
      (await api.fetchNets(ingest!.id))
        ?? reportFromBoard(await api.artifactJson<BoardDoc>(ingest!.id, 'board.json')),
    enabled: !!ingest,
    staleTime: Infinity,
  })
  const cables = useQuery({
    queryKey: ['emi', 'cables', cableRun?.id],
    queryFn: () => api.fetchCables(cableRun!.id),
    enabled: !!cableRun,
    staleTime: Infinity,
  })
  const esd = useQuery({
    queryKey: ['emi', 'transient', esdRun?.id],
    queryFn: () => api.fetchTransient(esdRun!.id),
    enabled: !!esdRun,
    staleTime: Infinity,
  })

  return {
    ingest, anyIngest, cableRun, esdRun,
    cableActive: active('cable'), esdActive: active('transient'),
    rules: rules.data === undefined ? null : (rules.data?.findings ?? []),
    nets: nets.data ?? null,
    cables: cables.data ?? null,
    esd: esd.data ?? null,
    pending: rules.isLoading || nets.isLoading || cables.isLoading || esd.isLoading,
    error: (rules.error ?? nets.error ?? cables.error ?? esd.error) as Error | null,
  }
}

// ---- summary ----

function Summary({ findings, loading }: { findings: FindingsDiff | null; loading: boolean }) {
  if (loading) return <Loader size="sm" />
  if (!findings) return null
  const { fixed, new: added, unchanged } = findings.counts
  const bySeverity = (c: typeof fixed) =>
    (['critical', 'warning', 'info'] as const).filter((s) => c[s] > 0).map((s) => `${c[s]} ${s}`).join(', ')
  return (
    <Group gap="sm" wrap="wrap">
      <Text fw={600}>{findingsHeadline(findings.counts)}</Text>
      <Badge color="green" variant={fixed.total ? 'filled' : 'light'} title={bySeverity(fixed)}>
        {fixed.total} fixed
      </Badge>
      <Badge color="red" variant={added.total ? 'filled' : 'light'} title={bySeverity(added)}>
        {added.total} new
      </Badge>
      <Badge color="gray" variant="light">{unchanged.total} unchanged</Badge>
      {(fixed.total > 0 || added.total > 0) && (
        <Text size="xs" c="dimmed">
          {[fixed.total && `fixed: ${bySeverity(fixed)}`, added.total && `new: ${bySeverity(added)}`]
            .filter(Boolean).join(' · ')}
        </Text>
      )}
    </Group>
  )
}

/** Why a comparison of what every version has is not on screen yet. */
function Waiting({ a, b, before, after, what }: {
  a: VersionData; b: VersionData; before: Version; after: Version; what: string
}) {
  const sides: [VersionData, Version][] = [[a, before], [b, after]]
  const err = a.error ?? b.error
  if (err) {
    return (
      <Alert color="red" variant="light" title={`The ${what} could not be loaded`}>
        <Text size="xs">{err.message}</Text>
      </Alert>
    )
  }
  const unready = sides.filter(([d]) => !d.ingest)
  if (unready.length === 0) return <Loader size="sm" />
  return (
    <Stack gap="sm" maw={420}>
      {unready.map(([d, v]) => (
        <Stack key={v.board.id} gap={4}>
          <Text size="sm">
            {d.anyIngest && TERMINAL_STATUSES.includes(d.anyIngest.status)
              ? `${label(v)} could not be analyzed.`
              : `${label(v)} is not analyzed yet.`}
          </Text>
          {d.anyIngest && <RunProgress run={d.anyIngest} />}
        </Stack>
      ))}
    </Stack>
  )
}

// ---- findings ----

function FindingsView({ diff, onOpen }: {
  diff: FindingsDiff
  onOpen: (f: RuleFinding, side: 'before' | 'after') => void
}) {
  const [show, setShow] = useState<'changes' | 'all'>('changes')
  const groups = useMemo(
    () => groupByRule(show === 'all' ? diff.changes : diff.changes.filter((c) => c.status !== 'unchanged')),
    [diff, show],
  )

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Text size="xs" c="dimmed">
          Matched by rule, net and layer, then by position within 2 mm or by identical wording.
        </Text>
        <SegmentedControl size="xs" value={show} onChange={(v) => setShow(v as 'changes' | 'all')}
                          data={[{ label: 'Changes', value: 'changes' }, { label: 'All', value: 'all' }]} />
      </Group>
      {groups.length === 0 ? (
        <Text size="sm" c="dimmed">No finding was fixed or added.</Text>
      ) : (
        <Accordion variant="separated" multiple defaultValue={groups.slice(0, 3).map(([r]) => r)}
                   chevronPosition="left">
          {groups.map(([rule, list]) => {
            const n = (s: FindingChange['status']) => list.filter((c) => c.status === s).length
            return (
              <Accordion.Item key={rule} value={rule}>
                <Accordion.Control>
                  <Group gap="xs" wrap="nowrap">
                    <Text size="sm" fw={500}>{RULE_LABEL[rule] ?? rule}</Text>
                    {n('new') > 0 && <Badge size="xs" color="red">{n('new')} new</Badge>}
                    {n('fixed') > 0 && <Badge size="xs" color="green">{n('fixed')} fixed</Badge>}
                    {n('unchanged') > 0 && <Badge size="xs" color="gray" variant="light">{n('unchanged')} unchanged</Badge>}
                  </Group>
                </Accordion.Control>
                <Accordion.Panel>
                  <Stack gap={6}>
                    {list.map((c, i) => <FindingRow key={i} change={c} onOpen={onOpen} />)}
                  </Stack>
                </Accordion.Panel>
              </Accordion.Item>
            )
          })}
        </Accordion>
      )}
    </Stack>
  )
}

function FindingRow({ change, onOpen }: {
  change: FindingChange
  onOpen: (f: RuleFinding, side: 'before' | 'after') => void
}) {
  const f = (change.after ?? change.before)!
  // A fixed finding is only on the old version, so that is where it is shown.
  const side = change.after ? 'after' : 'before'
  const where = f.x != null && f.y != null
  const was = change.before && change.after && change.before.severity !== change.after.severity
    ? change.before.severity : null
  return (
    <Box
      p={6}
      style={{ borderRadius: 4, cursor: where ? 'pointer' : undefined, border: '1px solid var(--mantine-color-default-border)' }}
      onClick={where ? () => onOpen(f, side) : undefined}
    >
      <Group gap={6} wrap="nowrap" align="flex-start">
        <Badge size="xs" color={STATUS_COLOR[change.status]} style={{ flex: 'none' }}
               variant={change.status === 'unchanged' ? 'light' : 'filled'}>
          {change.status}
        </Badge>
        <Badge size="xs" color={SEVERITY_COLOR[f.severity]} variant="light" style={{ flex: 'none' }}>
          {f.severity}
        </Badge>
        <Stack gap={0} style={{ minWidth: 0 }}>
          <Text size="sm">{f.title}</Text>
          <Text size="xs" c="dimmed" lineClamp={2}>
            {[f.net, f.layer, was ? `was ${was}` : '', f.detail].filter(Boolean).join(' · ')}
          </Text>
        </Stack>
      </Group>
    </Box>
  )
}

// ---- nets ----

const NET_ROWS = 200

function NetsView({ changes, partial }: { changes: ReturnType<typeof diffNets>; partial: boolean }) {
  if (changes.length === 0) {
    return <Text size="sm" c="dimmed">No net changed length or via count.</Text>
  }
  return (
    <Stack gap="xs">
      {partial && (
        <Text size="xs" c="dimmed">
          One version was analyzed before the net report existed, so its lengths are all the
          copper on the net. Re-analyze it on the board page for routed lengths.
        </Text>
      )}
      <Table.ScrollContainer minWidth={560}>
        <Table striped highlightOnHover fz="xs">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Net</Table.Th>
              <Table.Th ta="right">Length before</Table.Th>
              <Table.Th ta="right">Length after</Table.Th>
              <Table.Th ta="right">Change</Table.Th>
              <Table.Th ta="right">Vias</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {changes.slice(0, NET_ROWS).map((c) => (
              <Table.Tr key={c.net}>
                <Table.Td ff="monospace">
                  {c.net}{' '}
                  {c.status !== 'changed' && (
                    <Badge size="xs" variant="light" color={c.status === 'added' ? 'blue' : 'gray'}>{c.status}</Badge>
                  )}
                </Table.Td>
                <Table.Td ta="right">{fmtMm(c.lengthBefore)}</Table.Td>
                <Table.Td ta="right">{fmtMm(c.lengthAfter)}</Table.Td>
                <Table.Td ta="right">
                  {c.status === 'changed' && Math.abs(c.lengthAfter! - c.lengthBefore!) > LENGTH_EPSILON_MM
                    ? signed(c.lengthAfter! - c.lengthBefore!, 2) : ''}
                </Table.Td>
                <Table.Td ta="right">
                  {c.status === 'changed'
                    ? (c.viasBefore === c.viasAfter ? c.viasAfter : `${c.viasBefore} → ${c.viasAfter}`)
                    : (c.viasAfter ?? c.viasBefore)}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {changes.length > NET_ROWS && (
        <Text size="xs" c="dimmed">And {changes.length - NET_ROWS} more.</Text>
      )}
    </Stack>
  )
}

// ---- cables ----

function CablesView({ changes, before, after }: { changes: CableChange[]; before: Version; after: Version }) {
  if (changes.length === 0) {
    return <Text size="sm" c="dimmed">Neither version has a connector a cable run found.</Text>
  }
  const cell = (s: CableChange['before']) =>
    !s ? 'Not on this version'
      : s.note ? s.note
        : s.tightest
          ? `${s.tightest.max_current_dbua.toFixed(1)} dBµA at ${fmtHz(s.tightest.frequency_hz)}`
          : 'No budget'
  return (
    <Stack gap="sm">
      <Text size="xs" c="dimmed">
        The tightest point is the least common-mode current the cable may carry. Higher is more
        headroom. A budget depends mostly on the cable, not the routing, so expect it to move
        when a connector or its cable changed.
      </Text>
      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="sm">
        {changes.map((c) => {
          const delta = c.before?.tightest && c.after?.tightest
            ? c.after.tightest.max_current_dbua - c.before.tightest.max_current_dbua : null
          return (
            <Card key={c.ref} withBorder padding="sm">
              <Group justify="space-between" mb={4}>
                <Text fw={600} ff="monospace">{c.ref}</Text>
                {c.after?.cable && <Text size="xs" c="dimmed">{c.after.cable}</Text>}
              </Group>
              <Text size="xs">{label(before)}: {cell(c.before)}</Text>
              <Text size="xs">
                {label(after)}: {cell(c.after)}
                {delta !== null && Math.abs(delta) >= 0.05 && (
                  <Text span size="xs" c={delta > 0 ? 'green' : 'red'}> ({signed(delta, 1)} dB)</Text>
                )}
              </Text>
              {c.after?.points && c.after.points.length > 1 && (
                <Box mt="xs">
                  <CableBudgetChart points={c.after.points} before={c.before?.points}
                                    peaks={c.after.peaks} />
                </Box>
              )}
            </Card>
          )
        })}
      </SimpleGrid>
    </Stack>
  )
}

// ---- ESD ----

function EsdView({ changes, before, after, levels }: {
  changes: EsdChange[]; before: Version; after: Version; levels: [number, number]
}) {
  if (changes.length === 0) {
    return <Text size="sm" c="dimmed">Neither version has a line that leaves the board.</Text>
  }
  return (
    <Stack gap="sm">
      {levels[0] !== levels[1] && (
        <Alert color="yellow" variant="light">
          <Text size="xs">
            The two simulations used different levels ({levels[0]} kV and {levels[1]} kV), so
            their peaks do not compare. Run both at the same level.
          </Text>
        </Alert>
      )}
      <Text size="xs" c="dimmed">
        Peak voltage as laid out, at the IC pin, or at the clamp or connector when the line
        reaches no pin. Lower is better.
      </Text>
      <Accordion variant="separated" chevronPosition="left">
        {changes.map((c) => {
          const delta = c.before?.volts !== undefined && c.after?.volts !== undefined
            ? c.after.volts - c.before.volts : null
          const t = c.after?.line.t_ns
          const va = c.before?.line.variants?.find((v) => v.id === 'as_laid_out')
          const vb = c.after?.line.variants?.find((v) => v.id === 'as_laid_out')
          const series = []
          if (t && va?.v_pin && c.before?.line.t_ns) {
            series.push({ label: label(before), values: resample(c.before.line.t_ns, va.v_pin, t), color: BEFORE_COLOR })
          }
          if (t && vb?.v_pin) series.push({ label: label(after), values: vb.v_pin, color: AFTER_COLOR })
          return (
            <Accordion.Item key={c.key} value={c.key}>
              <Accordion.Control>
                <Group gap="sm" wrap="nowrap">
                  <Text size="sm" ff="monospace" w={80}>{c.connector}</Text>
                  <Text size="sm" style={{ flex: 1, minWidth: 0 }} truncate>{c.net}</Text>
                  <Text size="xs" c="dimmed">
                    {c.before ? fmtV(c.before.volts) : 'not on ' + label(before)} →{' '}
                    {c.after ? fmtV(c.after.volts) : 'not on ' + label(after)}
                  </Text>
                  {delta !== null && Math.abs(delta) >= 0.05 && (
                    <Badge size="xs" color={delta < 0 ? 'green' : 'red'} variant="light">
                      {signed(delta, 1)} V
                    </Badge>
                  )}
                </Group>
              </Accordion.Control>
              <Accordion.Panel>
                {t && series.length > 0 ? (
                  <Box maw={420}>
                    <TransientChart t={t} series={series} unit={`V at the ${c.after?.where ?? 'pin'}`} />
                  </Box>
                ) : (
                  <Text size="xs" c="dimmed">No waveform to draw for this line.</Text>
                )}
              </Accordion.Panel>
            </Accordion.Item>
          )
        })}
      </Accordion>
    </Stack>
  )
}

// ---- a run one version is missing ----

function MissingRun({ api, projectId, kind, versions, data, boardLink, onStarted }: {
  api: EmiApi
  projectId: string
  kind: 'cable' | 'transient'
  versions: [Version, Version]
  data: [VersionData, VersionData]
  boardLink: (v: Version) => string
  onStarted: () => void
}) {
  const what = kind === 'cable' ? 'cable budget' : 'ESD simulation'
  const done = (d: VersionData) => (kind === 'cable' ? d.cableRun : d.esdRun)
  const loaded = (d: VersionData) => (kind === 'cable' ? d.cables : d.esd)

  const start = useMutation({
    mutationFn: ({ boardId, from }: { boardId: string; from: Run | null }) => {
      if (kind === 'cable') {
        const params = from?.params as { connectors?: Record<string, { type: string; length_m?: number }>; standard_id?: string }
        return api.createCableRun(projectId, boardId, params?.connectors ?? {}, params?.standard_id)
      }
      const params = (from?.params ?? {}) as Partial<TransientParams>
      return api.createTransientRun(projectId, boardId, {
        level: params.level ?? 4, polarity: params.polarity ?? 'both', models: params.models ?? [],
      })
    },
    onSuccess: onStarted,
  })

  return (
    <Stack gap="sm" maw={520}>
      {versions.map((v, i) => {
        const d = data[i]
        const other = data[1 - i]
        const run = done(d)
        const active = kind === 'cable' ? d.cableActive : d.esdActive
        if (run && loaded(d)) return null
        if (run) return <Loader key={v.board.id} size="sm" />
        // A run on its way shows its progress rather than a button that starts a second one.
        if (active) {
          return (
            <Card key={v.board.id} withBorder padding="sm">
              <Text size="sm" mb={4}>{label(v)}</Text>
              <RunProgress run={active} />
            </Card>
          )
        }
        const from = done(other)
        return (
          <Card key={v.board.id} withBorder padding="sm">
            <Stack gap="xs" align="flex-start">
              <Text size="sm">{label(v)} has no {what}.</Text>
              {!d.ingest ? (
                <Text size="xs" c="dimmed">It has to finish its analysis first.</Text>
              ) : kind === 'cable' && !from ? (
                <>
                  <Text size="xs" c="dimmed">Choose the cables on the board page first.</Text>
                  <Button size="compact-xs" variant="light" component={Link} to={boardLink(v)}>
                    Open {label(v)}
                  </Button>
                </>
              ) : (
                <Button size="compact-xs" variant="light" loading={start.isPending}
                        onClick={() => start.mutate({ boardId: v.board.id, from })}>
                  {from
                    ? `Run it on ${label(v)} with the same settings`
                    : `Run it on ${label(v)}`}
                </Button>
              )}
            </Stack>
          </Card>
        )
      })}
      {start.isError && (
        <Alert color="red" variant="light">
          <Text size="xs">{(start.error as Error).message}</Text>
        </Alert>
      )}
    </Stack>
  )
}

// ---- board ----

/** The marker color on the board, and what it means. */
function MarkerLegend({ status }: { status: FindingStatus }) {
  return (
    <Group gap={4} wrap="nowrap">
      <Box w={10} h={10} style={{ flex: 'none', borderRadius: 2, background: FINDING_MARKER_HEX[status] }} />
      <Text size="xs">{status === 'new' ? 'New finding' : 'Fixed finding'}</Text>
    </Group>
  )
}

function CanvasView({ api, side, onSide, data, before, after, findings, focus, net, onPick }: {
  api: EmiApi
  side: 'after' | 'before'
  onSide: (s: 'after' | 'before') => void
  /** What the version on screen has. */
  data: VersionData
  before: Version
  after: Version
  findings: FindingsDiff | null
  focus: { x: number; y: number; zoom?: number } | null
  net: string | null
  onPick: (f: RuleFinding) => void
}) {
  const board = useQuery({
    queryKey: ['emi', 'board', data.ingest?.id],
    queryFn: () => api.fetchBoard(data.ingest!.id),
    enabled: !!data.ingest,
    staleTime: Infinity,
    gcTime: 30 * 60_000,
  })
  const [glError, setGlError] = useState<string | null>(null)

  // The new version shows what is new on it; the old one what the new one fixed.
  const shown = useMemo(
    () => (findings?.changes ?? [])
      .filter((c) => c.status === (side === 'after' ? 'new' : 'fixed'))
      .map((c) => (side === 'after' ? c.after! : c.before!)),
    [findings, side],
  )
  const status: FindingStatus = side === 'after' ? 'new' : 'fixed'
  const markers = useMemo(
    () => {
      const color = findingMarkerColor(status)
      return shown.filter((f) => f.x != null && f.y != null)
        .map((f) => ({ x: f.x!, y: f.y!, label: f.rule, color }))
    },
    [shown, status],
  )

  return (
    <Stack gap="sm">
      <Group justify="space-between" wrap="wrap">
        <SegmentedControl size="xs" value={side} onChange={(v) => onSide(v as 'after' | 'before')}
                          data={[
                            { label: `${label(after)} with new findings`, value: 'after' },
                            { label: `${label(before)} with fixed findings`, value: 'before' },
                          ]} />
        <Group gap="xs" wrap="nowrap">
          <MarkerLegend status={status} />
          <Text size="xs" c="dimmed">Click one to zoom to it.</Text>
        </Group>
      </Group>
      <Group align="stretch" gap="sm" style={{ minHeight: 0 }}>
        <Box style={{ flex: '1 1 420px', minWidth: 0, height: 520, position: 'relative', background: '#0e1211', borderRadius: 4 }}>
          {glError && <Alert color="red" m="md" title="The board viewer could not start">{glError}</Alert>}
          {!data.ingest && (
            <Text c="dimmed" size="sm" p="md">
              {label(side === 'after' ? after : before)} is not analyzed yet.
            </Text>
          )}
          {board.isLoading && <Group justify="center" h="100%"><Loader size="sm" /></Group>}
          {board.isError && (
            <Alert color="red" m="md" title="The board could not be loaded">
              {(board.error as Error).message}
            </Alert>
          )}
          {!glError && board.data && (
            <BoardCanvas
              key={data.ingest?.id}
              doc={board.data.doc}
              geometry={board.data.geometry}
              markers={markers}
              highlightNet={net}
              focus={focus}
              onError={(e) => setGlError(e.message)}
            />
          )}
        </Box>
        <Stack gap={6} style={{ flex: '0 1 300px', minWidth: 0, maxHeight: 520, overflowY: 'auto' }}>
          {shown.length === 0 ? (
            <Text size="sm" c="dimmed">
              {side === 'after' ? 'No new findings.' : 'No fixed findings.'}
            </Text>
          ) : shown.map((f, i) => (
            <Box key={i} p={6}
                 style={{ borderRadius: 4, border: '1px solid var(--mantine-color-default-border)',
                   cursor: f.x != null ? 'pointer' : undefined }}
                 onClick={() => onPick(f)}>
              <Group gap={6} wrap="nowrap">
                <Badge size="xs" color={SEVERITY_COLOR[f.severity]} variant="light" style={{ flex: 'none' }}>
                  {f.severity}
                </Badge>
                <Text size="xs" truncate>{RULE_LABEL[f.rule] ?? f.rule}</Text>
              </Group>
              <Text size="xs" c="dimmed" lineClamp={2}>{f.title}{f.net ? ` · ${f.net}` : ''}</Text>
            </Box>
          ))}
        </Stack>
      </Group>
    </Stack>
  )
}
