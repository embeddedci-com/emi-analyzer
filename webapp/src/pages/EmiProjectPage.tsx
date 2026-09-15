/**
 * Tools -> EMI Analyzer -> one board.
 *
 * The board viewer with layers, nets and rule findings beside it. Clicking a finding zooms
 * the viewer to it and highlights its net, which is the interaction the whole page exists
 * for: the checks say where to look, and this is the looking.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Badge, Box, Button, Group, Loader, Paper, Stack, Tabs, Text, Title,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router'
import { BoardCanvas, type CanvasMode } from '../components/BoardCanvas'
import { EsdSimulation } from '../components/EsdSimulation'
import { HotspotResults, type SolveManifest } from '../components/HotspotResults'
import { LayerRail } from '../components/LayerRail'
import { NetPicker } from '../components/NetPicker'
import { NetsExportButton } from '../components/NetsExportButton'
import { NearFieldImport } from '../components/NearFieldImport'
import { RuleFindings } from '../components/RuleFindings'
import { RunProgress } from '../components/RunProgress'
import { DriversPanel } from '../components/DriversPanel'
import { ComponentsPanel } from '../components/ComponentsPanel'
import { CablesPanel, type Assignment } from '../components/CablesPanel'
import { ComplianceTab } from '../components/ComplianceTab'
import { SolveSetup, type SolveRequest } from '../components/SolveSetup'
import type { BoardDoc, RuleFinding, RulesDoc } from '../lib/boardTypes'
import type { FieldOverlayData } from '../lib/overlay'
import { placePortOnAnchor, type PortAnchor, type PortSpec } from '../lib/portPlacement'
import { EmiApi, TERMINAL_STATUSES, type Run } from '../lib/emiApi'

export interface EmiProjectPageProps {
  api: EmiApi
}

export function EmiProjectPage({ api }: EmiProjectPageProps) {
  const { projectId = '' } = useParams()
  const [visibility, setVisibility] = useState<Record<string, boolean>>({})
  const [net, setNet] = useState<string | null>(null)
  const [focus, setFocus] = useState<{ x: number; y: number; zoom?: number } | null>(null)
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null)
  const [glError, setGlError] = useState<string | null>(null)

  // Solve setup
  const [tab, setTab] = useState<string | null>('findings')
  const [roi, setRoi] = useState<[number, number, number, number] | null>(null)
  const [ports, setPorts] = useState<PortSpec[]>([])
  const [pickingPad, setPickingPad] = useState(false)
  const [drawingRoi, setDrawingRoi] = useState(false)
  const [overlay, setOverlay] = useState<FieldOverlayData | null>(null)
  const [gateDb, setGateDb] = useState(-45)
  const [selectedSolveId, setSelectedSolveId] = useState<string | null>(null)
  // The line to open in the ESD tab, when arriving there from a finding.
  const [esdNet, setEsdNet] = useState<string | null>(null)
  const qc = useQueryClient()

  const project = useQuery({
    queryKey: ['emi', 'project', projectId],
    queryFn: () => api.getProject(projectId),
    enabled: !!projectId,
  })

  // Poll only while something is still running; a finished board should not keep the tab
  // talking to the server forever. Driven by explicit state rather than by a
  // refetchInterval callback reading query internals — that form silently stopped
  // re-arming after the first fetch, which left a completed run showing as in-progress
  // forever. This is longer and it is observable.
  const [pollMs, setPollMs] = useState<number | false>(1500)

  const runs = useQuery({
    queryKey: ['emi', 'runs', projectId],
    queryFn: () => api.listRuns(projectId),
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

  // The analysis on screen is the newest one that *finished*. Taking the newest one of any
  // status would blank the viewer for the whole of a re-analysis, and leave it blank if that
  // re-analysis failed — while a perfectly good earlier result sat unused.
  const ingest = useMemo(() => {
    const ingests = (runs.data ?? [])
      .filter((r) => r.kind === 'ingest')
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
    return ingests.find((r) => r.status === 'done') ?? ingests[0] ?? null
  }, [runs.data])
  const ingestDone = ingest?.status === 'done'

  // A newer analysis than the one on screen, still running.
  const reanalysing = useMemo(
    () =>
      (runs.data ?? []).some(
        (r) =>
          r.kind === 'ingest' &&
          r.id !== ingest?.id &&
          !TERMINAL_STATUSES.includes(r.status) &&
          (!ingest || Date.parse(r.created_at) > Date.parse(ingest.created_at)),
      ),
    [runs.data, ingest],
  )
  const reanalyse = useMutation({
    mutationFn: () => api.reanalyse(projectId, ingest!.board_id!),
    // Refetching runs restarts the page's polling, which is what notices it finishing.
    onSuccess: () => qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] }),
  })

  const board = useQuery({
    queryKey: ['emi', 'board', ingest?.id],
    queryFn: () => api.fetchBoard(ingest!.id),
    enabled: !!ingest && ingestDone,
    // board.json and geometry.bin are immutable for a given run, so never refetch them.
    staleTime: Infinity,
    gcTime: 30 * 60_000,
  })

  const rules = useQuery({
    queryKey: ['emi', 'rules', ingest?.id],
    queryFn: () => api.fetchRules(ingest!.id),
    enabled: !!ingest && ingestDone,
    staleTime: Infinity,
  })

  // Accumulate the energy curve across polls; the server only ever holds the latest point.
  const energyRef = useRef<[number, number][]>([])
  const activeRun = runs.data?.find((r) => !TERMINAL_STATUSES.includes(r.status)) ?? null
  useEffect(() => {
    const p = activeRun?.progress
    if (p?.timestep && p.energy_db !== undefined) {
      const last = energyRef.current[energyRef.current.length - 1]
      if (!last || last[0] !== p.timestep) {
        const sample: [number, number] = [p.timestep, p.energy_db]
        energyRef.current = [...energyRef.current, sample].slice(-400)
      }
    }
  }, [activeRun?.progress])

  const doc: BoardDoc | null = board.data?.doc ?? null

  // Same query key as the analyzer page, so the two share one answer rather than asking twice.
  const me = useQuery({ queryKey: ['emi', 'whoami'], queryFn: api.whoami, staleTime: 60_000 })

  // Full-wave solving and the tabs that only feed or read a solve are experimental, and hidden
  // until the server says they are on -- including while it has not answered yet.
  const features = useQuery({ queryKey: ['emi', 'features'], queryFn: api.features, staleTime: 5 * 60_000 })
  const fullWave = features.data?.full_wave === true

  const workers = useQuery({
    queryKey: ['emi', 'workers'],
    queryFn: api.listWorkers,
    refetchInterval: 20_000,
  })

  // Solve runs, newest first. The one being viewed defaults to the newest finished one.
  const solves = useMemo(
    () => (runs.data ?? []).filter((r) => r.kind === 'solve'),
    [runs.data],
  )
  const activeSolve: Run | null = useMemo(() => {
    if (selectedSolveId) return solves.find((r) => r.id === selectedSolveId) ?? null
    return solves.find((r) => r.status === 'done')
      ?? solves.find((r) => !TERMINAL_STATUSES.includes(r.status))
      ?? solves[0] ?? null
  }, [solves, selectedSolveId])

  const solveManifest = useQuery({
    queryKey: ['emi', 'manifest', activeSolve?.id],
    queryFn: async () => {
      const ref = await api.artifactUrl(activeSolve!.id, 'manifest.json')
      return (await fetch(ref.url).then((r) => r.json())) as SolveManifest
    },
    enabled: !!activeSolve && activeSolve.status === 'done',
    staleTime: Infinity,
  })

  const startSolve = useMutation({
    mutationFn: async (req: SolveRequest) => {
      if (!ingest?.board_id && !board.data) throw new Error('the board is not ready')
      const boardId = ingest?.board_id
      if (!boardId) throw new Error('this project has no ingested board')
      return api.createSolveRun(
        projectId, boardId,
        {
          roi: req.roi,
          mesh: req.mesh,
          frequencies_hz: req.frequencies_hz,
          f_min_hz: Math.min(...req.frequencies_hz),
          ports: req.ports,
          // The server attaches the caller's component library when this is set; the worker
          // reads the resolved copies back out. Omitted when off, so a run that does not want
          // components looks exactly as it did before they existed.
          ...(req.model_components ? { model_components: true } : {}),
          // §7. Omitted when empty, so a solve that wants no cable emissions is meshed
          // exactly as it was before gap ports existed.
          ...(req.cable_ports && Object.keys(req.cable_ports).length > 0
            ? { cable_ports: req.cable_ports }
            : {}),
          // §16.2. Omitted when off, so a hotspot solve is meshed and dumped exactly as it was
          // before the far field existed.
          ...(req.far_field ? { far_field: true } : {}),
        },
        req.estimateInput,
      )
    },
    onSuccess: (run) => {
      setSelectedSolveId(run.id)
      setTab('results')
      setPollMs(1500)
      qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
    },
  })

  // Which connector carries which cable. Lives here rather than in the Cables tab because
  // the Solve tab needs it too: a gap port changes the mesh, so §7's cable has to be chosen
  // before the run, unlike a driver.
  const [cableAssignments, setCableAssignments] = useState<Record<string, Assignment>>({})

  const [pickMiss, setPickMiss] = useState(false)
  const onPadPick = useCallback((anchor: PortAnchor | null) => {
    if (!doc) return
    if (!anchor) {
      setPickMiss(true)
      return
    }
    setPickMiss(false)
    setPorts((prev) => [...prev, placePortOnAnchor(doc, anchor, prev.length)])
    setPickingPad(false)
  }, [doc])

  const canvasMode: CanvasMode = drawingRoi ? 'roi' : pickingPad ? 'pick-pad' : 'pan'
  // The ports being set up, or — when there are none — the ports of the solve on screen. A
  // result is never shown without marking where current was injected: the loudest point of a
  // map is usually at or right beside its port, and unmarked it reads as a hotspot.
  const markers = useMemo(() => {
    const solved = (activeSolve?.params as { ports?: PortSpec[] } | undefined)?.ports ?? []
    return (ports.length ? ports : solved).map((p) => ({ x: p.x_mm, y: p.y_mm, label: p.name }))
  }, [ports, activeSolve])

  const onFocusFinding = (f: RuleFinding) => {
    if (f.x != null && f.y != null) setFocus({ x: f.x, y: f.y, zoom: 28 })
  }

  const onSimulateFinding = (f: RuleFinding) => {
    setEsdNet(f.net ?? null)
    if (f.net) setNet(f.net)
    onFocusFinding(f)
    setTab('esd')
  }

  if (project.isError) {
    return <Alert color="red" m="md">{(project.error as Error).message}</Alert>
  }

  return (
    <Stack gap={0} h="calc(100vh - 60px)">
      <Group justify="space-between" px="md" py="sm" wrap="nowrap">
        <Group gap="sm" wrap="nowrap">
          <Title order={4}>{project.data?.name ?? 'Board'}</Title>
          {doc && (
            <Text size="xs" c="dimmed" ff="monospace">
              {/* Triangle count used to be here. It is how the viewer draws the board, not
                  anything about the board, and nobody can act on it. Board thickness can be
                  acted on: with the layer count it is the stackup in one line. */}
              {doc.board.width_mm} × {doc.board.height_mm} mm · {doc.layers.length} layers ·{' '}
              {doc.board.thickness_mm.toFixed(2)} mm thick · {doc.nets.length} nets
            </Text>
          )}
        </Group>
        {cursor && (
          <Text size="xs" c="dimmed" ff="monospace">
            {cursor.x.toFixed(2)}, {cursor.y.toFixed(2)} mm
          </Text>
        )}
      </Group>

      {/* The two panes must be bounded by the row, not by their own content. Without
          overflow:hidden here and minHeight:0 on both children, flex stretches every item
          to the tallest one — and a findings list runs to thousands of pixels, which drags
          the canvas to the same height and shrinks the board to a sliver. */}
      <Group align="stretch" gap={0} style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
        {/* Viewer */}
        <Box
          style={{
            flex: 1,
            minWidth: 0,
            minHeight: 0,
            height: '100%',
            position: 'relative',
            background: '#0e1211',
          }}
        >
          {glError && (
            <Alert color="red" m="md" title="The board viewer could not start">
              {glError}
            </Alert>
          )}
          {!glError && board.isLoading && ingestDone && (
            <Group justify="center" h="100%">
              <Loader size="sm" />
              <Text size="sm" c="dimmed">
                Loading board geometry…
              </Text>
            </Group>
          )}
          {!glError && !ingestDone && (
            <Group justify="center" h="100%" p="xl">
              <Stack gap="sm" maw={420}>
                {ingest ? (
                  <RunProgress run={ingest} />
                ) : runs.isLoading ? (
                  <Loader size="sm" />
                ) : (
                  <Text c="dimmed" size="sm">
                    No ingest run for this project yet.
                  </Text>
                )}
              </Stack>
            </Group>
          )}
          {!glError && doc && board.data && (
            <BoardCanvas
              doc={doc}
              geometry={board.data.geometry}
              mode={canvasMode}
              roi={roi}
              onRoiChange={setRoi}
              markers={markers}
              onPadPick={onPadPick}
              overlay={overlay}
              overlayOptions={{ gateDb }}
              layerVisibility={visibility}
              highlightNet={net}
              focus={focus}
              onCursorMove={setCursor}
              onError={(e) => setGlError(e.message)}
            />
          )}
        </Box>

        {/* Side panel */}
        {/*
          420 rather than 340. The charts in these tabs are the product: a budget curve, a
          predicted spectrum against a limit. At 340 the panel gave them about 274 px, which
          scaled a 560-unit viewBox to 49 % and rendered its 9 px labels at four and a half.
        */}
        <Paper
          withBorder
          radius={0}
          w={420}
          style={{
            borderTop: 0,
            borderBottom: 0,
            borderRight: 0,
            height: '100%',
            minHeight: 0,
            flex: 'none',
            // A column, so the stackup can stay put while the tab below it scrolls.
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {/* Above the tabs on purpose: which layers are visible changes what every tab is
              talking about, so it is not the business of one of them. Buried in the Board
              tab it also meant leaving whatever you were reading to hide a layer. */}
          {doc && (
            <Box
              p="sm"
              style={{
                flex: 'none',
                borderBottom: '1px solid var(--mantine-color-default-border)',
              }}
            >
              <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={6}>
                Stackup
              </Text>
              <LayerRail
                doc={doc}
                visibility={visibility}
                onToggle={(layer, v) => setVisibility((prev) => ({ ...prev, [layer]: v }))}
              />
            </Box>
          )}

          <Box style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          <Tabs value={tab} onChange={setTab}>
            {/* Five tabs in a 340 px panel: Mantine's default horizontal padding wrapped the last
                one onto a second row, so the tabs carry tighter padding than the default. */}
            <Tabs.List grow>
              <Tabs.Tab value="findings" px={8}>
                Findings
                {rules.data && rules.data.summary.critical > 0 && (
                  <Badge size="xs" color="red" ml={4} circle>
                    {rules.data.summary.critical}
                  </Badge>
                )}
              </Tabs.Tab>
              {fullWave && <Tabs.Tab value="solve" px={8}>Solve</Tabs.Tab>}
              {fullWave && <Tabs.Tab value="drivers" px={8}>Drivers</Tabs.Tab>}
              {fullWave && <Tabs.Tab value="components" px={8}>Components</Tabs.Tab>}
              <Tabs.Tab value="cables" px={8}>Cables</Tabs.Tab>
              {fullWave && (
                <Tabs.Tab value="results" px={8} disabled={solves.length === 0}>
                  Results
                </Tabs.Tab>
              )}
              {fullWave && <Tabs.Tab value="compliance" px={8}>Compliance</Tabs.Tab>}
              <Tabs.Tab value="esd" px={8}>ESD</Tabs.Tab>
              <Tabs.Tab value="board" px={8}>Board</Tabs.Tab>
            </Tabs.List>

            <Tabs.Panel value="findings" p="sm">
              {rules.isLoading && <Loader size="sm" />}
              {ingestDone && (
                <RuleFindings
                  rules={(rules.data as RulesDoc | null) ?? null}
                  onFocus={onFocusFinding}
                  onSelectNet={setNet}
                  onSimulate={onSimulateFinding}
                />
              )}
            </Tabs.Panel>

            {fullWave && (
            <Tabs.Panel value="solve" p="sm">
              {doc ? (
                <SolveSetup
                  doc={doc}
                  roi={roi}
                  onRoiChange={setRoi}
                  ports={ports}
                  onPortsChange={setPorts}
                  pickingPad={pickingPad}
                  onPickPad={(v) => { setPickingPad(v); setPickMiss(false) }}
                  pickMiss={pickMiss}
                  drawingRoi={drawingRoi}
                  onDrawRoi={setDrawingRoi}
                  workers={workers.data?.workers ?? []}
                  cableAssignments={cableAssignments}
                  onSubmit={(req) => startSolve.mutate(req)}
                  submitting={startSolve.isPending}
                  error={startSolve.error ? (startSolve.error as Error).message : null}
                />
              ) : (
                <Text size="sm" c="dimmed">
                  The board has to finish processing before it can be solved.
                </Text>
              )}
            </Tabs.Panel>
            )}

            {fullWave && (
            <Tabs.Panel value="drivers" p="sm">
              <DriversPanel
                api={api}
                projectId={projectId}
                nets={doc?.nets?.map((n) => n.name) ?? []}
              />
            </Tabs.Panel>
            )}

            {fullWave && (
            <Tabs.Panel value="components" p="sm">
              <ComponentsPanel api={api} signedIn={me.data ? !me.data.anonymous : false} />
            </Tabs.Panel>
            )}

            <Tabs.Panel value="cables" p="sm">
              <CablesPanel
                api={api} projectId={projectId} boardId={ingest?.board_id}
                assignments={cableAssignments} onAssignmentsChange={setCableAssignments}
              />
            </Tabs.Panel>

            {fullWave && (
            <Tabs.Panel value="results" p="sm">
              {!activeSolve ? (
                <Text size="sm" c="dimmed">No solve has been run yet.</Text>
              ) : activeSolve.status !== 'done' ? (
                <RunProgress run={activeSolve} energyHistory={energyRef.current} />
              ) : solveManifest.isLoading ? (
                <Loader size="sm" />
              ) : solveManifest.isError ? (
                <Alert color="red" variant="light">
                  {(solveManifest.error as Error).message}
                </Alert>
              ) : solveManifest.data ? (
                <HotspotResults
                  api={api}
                  runId={activeSolve.id}
                  projectId={projectId}
                  manifest={solveManifest.data}
                  onOverlayChange={setOverlay}
                  onGateChange={setGateDb}
                />
              ) : null}
            </Tabs.Panel>
            )}

            {fullWave && (
            <Tabs.Panel value="compliance" p="sm">
              <ComplianceTab
                api={api}
                projectId={projectId}
                boardId={ingest?.board_id}
                solveRunId={activeSolve?.status === 'done' ? activeSolve.id : undefined}
                solveManifest={solveManifest.data}
                cableAssignments={cableAssignments}
                findings={
                  ((rules.data as RulesDoc | null)?.findings ?? []) as unknown as
                    Record<string, unknown>[]
                }
              />
            </Tabs.Panel>
            )}

            <Tabs.Panel value="esd" p="sm">
              {doc ? (
                <EsdSimulation
                  api={api}
                  projectId={projectId}
                  boardId={ingest?.board_id}
                  doc={doc}
                  runs={runs.data ?? []}
                  workers={workers.data?.workers ?? []}
                  focusNet={esdNet}
                  onFocus={(x, y) => setFocus({ x, y, zoom: 28 })}
                  onSelectNet={setNet}
                  onStarted={() => setPollMs(1500)}
                />
              ) : (
                <Text size="sm" c="dimmed">
                  The board has to finish processing before a discharge can be simulated.
                </Text>
              )}
            </Tabs.Panel>

            <Tabs.Panel value="board" p="sm">
              {doc ? (
                <Stack gap="lg">
                  {/* Always available. It used to appear only when an export found no net data,
                      but every new check needs existing boards analysed again to show up — a
                      board with complete exports had no way to get them. */}
                  <Group justify="space-between" wrap="nowrap" gap="xs">
                    <Text size="xs" c="dimmed">
                      {reanalysing || reanalyse.isPending
                        ? 'Re-analysing with the latest checks…'
                        : ingest?.finished_at
                          ? `Analysed ${new Date(ingest.finished_at).toLocaleString()}`
                          : 'Analysed'}
                    </Text>
                    <Button
                      size="compact-xs"
                      variant="light"
                      onClick={() => reanalyse.mutate()}
                      loading={reanalysing || reanalyse.isPending}
                      disabled={!ingest?.board_id}
                    >
                      Re-analyse
                    </Button>
                  </Group>
                  <div>
                    <Group justify="space-between" align="flex-start" mb="xs" wrap="nowrap">
                      <Text size="xs" fw={600} tt="uppercase" c="dimmed">
                        Nets
                      </Text>
                      <NetsExportButton
                        api={api}
                        runId={ingest?.id}
                        doc={doc}
                        projectName={project.data?.name ?? 'board'}
                        onReanalyse={ingest?.board_id ? () => reanalyse.mutate() : undefined}
                        reanalysing={reanalysing || reanalyse.isPending}
                      />
                    </Group>
                    <NetPicker doc={doc} selected={net} onSelect={setNet} />
                  </div>

                  <div>
                    <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb="xs">
                      Near-field scan
                    </Text>
                    <NearFieldImport
                      boardHeight={doc.board.height_mm}
                      onOverlayChange={setOverlay}
                      onGateChange={setGateDb}
                    />
                  </div>

                  {doc.warnings.length > 0 && (
                    <Alert color="yellow" variant="light" title={`${doc.warnings.length} parse warnings`}>
                      <Stack gap={4}>
                        {doc.warnings.slice(0, 5).map((w, i) => (
                          <Text key={i} size="xs">
                            {w}
                          </Text>
                        ))}
                        {doc.warnings.length > 5 && (
                          <Text size="xs" c="dimmed">
                            …and {doc.warnings.length - 5} more.
                          </Text>
                        )}
                      </Stack>
                    </Alert>
                  )}
                </Stack>
              ) : (
                <Text size="sm" c="dimmed">
                  The board is still being processed.
                </Text>
              )}
            </Tabs.Panel>
          </Tabs>
          </Box>
        </Paper>
      </Group>
    </Stack>
  )
}
