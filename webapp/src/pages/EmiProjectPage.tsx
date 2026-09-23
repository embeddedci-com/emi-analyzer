/**
 * Tools -> EMI Analyzer -> one board.
 *
 * The board viewer with layers, nets and rule findings beside it. Clicking a finding zooms
 * the viewer to it and highlights its net, which is the interaction the whole page exists
 * for: the checks say where to look, and this is the looking.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ActionIcon, Alert, Badge, Box, Button, Group, Loader, Menu, Modal, Paper, SegmentedControl,
  Stack, Tabs, Text, TextInput, Title, Tooltip,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate, useParams } from 'react-router'
import type { BoardRenderer } from '../lib/BoardRenderer'
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
import { useKiCad } from '../lib/kicad'
import type { EmiDeployment } from '../routes'
import { useEmiBase } from '../host'

type SolveView = 'setup' | 'result' | 'drivers' | 'parts'

export interface EmiProjectPageProps {
  api: EmiApi
  deployment?: EmiDeployment
}

export function EmiProjectPage({ api, deployment = 'hosted' }: EmiProjectPageProps) {
  const local = deployment === 'local'
  const { projectId = '' } = useParams()
  const navigate = useNavigate()
  const base = useEmiBase()
  // Set by the upload page when the file turned out to be a board that was already here.
  const location = useLocation()
  const [reused, setReused] = useState(
    () => (location.state as { reused?: boolean } | null)?.reused === true,
  )
  const [visibility, setVisibility] = useState<Record<string, boolean>>({})
  const [net, setNet] = useState<string | null>(null)
  const [focus, setFocus] = useState<{ x: number; y: number; zoom?: number } | null>(null)
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null)
  const [glError, setGlError] = useState<string | null>(null)
  // Only inside the KiCad plugin: the PCB Editor next door, and what it last said back.
  const kicad = useKiCad()
  const [kicadNote, setKicadNote] = useState<string | null>(null)

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
  // Which part of the full-wave workflow is showing: setting one up, its result, or the
  // libraries that feed it.
  const [solveView, setSolveView] = useState<SolveView>('setup')
  // The panel is the whole right-hand side; on a small screen the board needs the room back.
  const [panelOpen, setPanelOpen] = useState(true)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const rendererRef = useRef<BoardRenderer | null>(null)
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
  // Per run: a second solve used to draw its curve on the end of the first one's.
  const energyRef = useRef<[number, number][]>([])
  const energyRunId = useRef<string | null>(null)
  const activeRun = runs.data?.find((r) => !TERMINAL_STATUSES.includes(r.status)) ?? null
  useEffect(() => {
    const id = activeRun?.id ?? null
    if (id !== energyRunId.current) {
      energyRunId.current = id
      energyRef.current = []
    }
    const p = activeRun?.progress
    if (p?.timestep && p.energy_db !== undefined) {
      const last = energyRef.current[energyRef.current.length - 1]
      if (!last || last[0] !== p.timestep) {
        const sample: [number, number] = [p.timestep, p.energy_db]
        energyRef.current = [...energyRef.current, sample].slice(-400)
      }
    }
  }, [activeRun?.id, activeRun?.progress])

  const doc: BoardDoc | null = board.data?.doc ?? null

  // Point the PCB Editor at a finding's net. It is next to this window, so saying nothing
  // when it refuses would look like the click did not register.
  const showInKiCad = useCallback(
    (nets: string[]) => {
      if (!kicad) return
      setKicadNote(null)
      kicad
        .select(nets)
        .then((n) =>
          setKicadNote(n ? null : `Nothing to select: ${nets.join(', ')} has no copper on the board.`),
        )
        .catch((e: Error) => setKicadNote(`KiCad did not select it: ${e.message}`))
    },
    [kicad],
  )

  // Same query key as the analyzer page, so the two share one answer rather than asking twice.

  // Full-wave solving and the tabs that only feed or read a solve are experimental, and hidden
  // until the server says they are on -- including while it has not answered yet.
  const features = useQuery({ queryKey: ['emi', 'features'], queryFn: api.features, staleTime: 5 * 60_000 })
  const fullWave = features.data?.full_wave === true

  const me = useQuery({
    queryKey: ['emi', 'whoami'], queryFn: api.whoami, staleTime: 60_000,
    // Only the component library asks who you are, and that is behind full-wave.
    enabled: features.data?.full_wave === true,
  })

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
    queryFn: () => api.artifactJson<SolveManifest>(activeSolve!.id, 'manifest.json'),
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
          // docs/implementation.md §5.2. Omitted when empty, so a solve that wants no cable
          // emissions is meshed exactly as it was before gap ports existed.
          ...(req.cable_ports && Object.keys(req.cable_ports).length > 0
            ? { cable_ports: req.cable_ports }
            : {}),
          // docs/implementation.md §6. Omitted when off, so a hotspot solve is meshed and dumped
          // exactly as it was before the far field existed.
          ...(req.far_field ? { far_field: true } : {}),
        },
        req.estimateInput,
      )
    },
    onSuccess: (run) => {
      setSelectedSolveId(run.id)
      setTab('fullwave')
      setSolveView('result')
      setPollMs(1500)
      qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
    },
  })

  const workersOnline = workers.data?.online_count ?? 0

  // A run can fail, and until now the only way out was to upload the board again.
  const retry = useMutation({
    mutationFn: (runId: string) => api.retryRun(runId),
    onSuccess: () => {
      setPollMs(1500)
      qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
    },
  })
  const stop = useMutation({
    mutationFn: (runId: string) => api.stopRun(runId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] }),
  })

  // What the runs menu and Re-analyse did not manage. A refusal (a gated kind, a run that is
  // no longer failed) used to change nothing on screen, which read as a button that is broken.
  const actionError = [retry, stop, reanalyse].find((m) => m.isError)

  const rename = useMutation({
    mutationFn: (name: string) => api.renameProject(projectId, name),
    onSuccess: () => {
      setRenaming(null)
      qc.invalidateQueries({ queryKey: ['emi', 'project', projectId] })
      qc.invalidateQueries({ queryKey: ['emi', 'projects'] })
    },
  })
  const remove = useMutation({
    mutationFn: () => api.deleteProject(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['emi', 'projects'] })
      navigate(base || '/')
    },
  })

  // Which connector carries which cable. Lives here rather than in the Cables tab because
  // the Solve tab needs it too: a gap port changes the mesh, so the cable has to be chosen
  // before the run, unlike a driver.
  const [cableAssignments, setCableAssignments] = useState<Record<string, Assignment>>({})

  // The newest finished cable run, so the tab shows its result after a reload instead of
  // offering to start the same run again.
  const lastCableRun = useMemo(
    () => (runs.data ?? []).find((r) => r.kind === 'cable' && r.status === 'done'),
    [runs.data],
  )
  // Assignments are not kept anywhere of their own: they are the params the last cable run was
  // started with, which is the one place they already survive a restart.
  const cablesHydrated = useRef(false)
  useEffect(() => {
    if (cablesHydrated.current || !lastCableRun) return
    const connectors = (lastCableRun.params as { connectors?: Record<string, Assignment> } | undefined)?.connectors
    if (connectors && Object.keys(connectors).length > 0) setCableAssignments(connectors)
    cablesHydrated.current = true
  }, [lastCableRun])

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
    const missing = (project.error as { status?: number }).status === 404
    return (
      <Alert color={missing ? 'yellow' : 'red'} m="md"
             title={missing ? 'There is no such board here' : 'This board could not be opened'}>
        <Stack gap="xs" align="flex-start">
          <Text size="sm">
            {missing
              ? 'It may have been deleted, or the link may come from another installation.'
              : (project.error as Error).message}
          </Text>
          <Button size="xs" variant="light" onClick={() => navigate(base || '/')}>
            Back to your boards
          </Button>
        </Stack>
      </Alert>
    )
  }

  return (
    /*
      The height of whatever the host puts above this page, as a variable rather than a
      constant: the local app's header is a different height and it shows a banner above the
      routes while the worker starts, so a fixed 60 px made the page taller than the window
      and the whole app scrolled on first run.
    */
    <Stack gap={0} h="calc(100dvh - var(--emi-chrome-height, 60px))">
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
        <Group gap="xs" wrap="nowrap">
          {cursor && (
            <Text size="xs" c="dimmed" ff="monospace">
              {cursor.x.toFixed(2)}, {cursor.y.toFixed(2)} mm
            </Text>
          )}
          <RunsMenu runs={runs.data ?? []} busy={retry.isPending || stop.isPending}
                    onRetry={(id) => retry.mutate(id)} onStop={(id) => stop.mutate(id)} />
          <Menu position="bottom-end" withinPortal>
            <Menu.Target>
              <ActionIcon variant="subtle" color="gray" aria-label="Board actions">
                <Text span size="sm">&#8943;</Text>
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item onClick={() => setRenaming(project.data?.name ?? '')}>
                Rename&#8230;
              </Menu.Item>
              <Menu.Item color="red" onClick={() => setConfirmDelete(true)}>
                Delete this board&#8230;
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
          <Tooltip label={panelOpen ? 'Hide the panel' : 'Show the panel'} withArrow>
            <ActionIcon variant="subtle" color="gray" onClick={() => setPanelOpen((v) => !v)}
                        aria-label={panelOpen ? 'Hide the panel' : 'Show the panel'}>
              <Text span size="sm">{panelOpen ? '\u276f' : '\u276e'}</Text>
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>

      {actionError && (
        <Alert color="red" variant="light" mx="md" mb="xs" withCloseButton
               onClose={() => actionError.reset()}>
          <Text size="xs">{(actionError.error as Error).message}</Text>
        </Alert>
      )}
      {reused && (
        <Alert color="blue" variant="light" mx="md" mb="xs" withCloseButton
               onClose={() => setReused(false)} title="Opened the copy you already had">
          <Text size="xs">
            This board was uploaded before, so nothing was sent again. These are its existing
            checks and results.
          </Text>
        </Alert>
      )}

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
          {!glError && board.isError && (
            <Alert color="red" m="md" title="The board could not be loaded">
              <Stack gap="xs" align="flex-start">
                <Text size="sm">
                  {local
                    ? 'Its geometry could not be read back. Everything is on this computer, so this is usually a file that was moved or removed.'
                    : (board.error as Error).message}
                </Text>
                <Button size="xs" variant="light"
                        onClick={() => qc.invalidateQueries({ queryKey: ['emi', 'board'] })}>
                  Try again
                </Button>
              </Stack>
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
                  <>
                    <RunProgress run={ingest} />
                    {!TERMINAL_STATUSES.includes(ingest.status) && workersOnline === 0 && (
                      <Text c="dimmed" size="xs">
                        {local
                          ? 'Nothing is working on it yet: no worker is connected. The app starts one by itself. Its status is at the top of the window.'
                          : 'Nothing is working on it yet: no worker is connected. It starts when one does.'}
                      </Text>
                    )}
                  </>
                ) : runs.isLoading ? (
                  <Loader size="sm" />
                ) : runs.isError ? (
                  <Text c="red" size="sm">The runs for this board could not be loaded.</Text>
                ) : (
                  <Text c="dimmed" size="sm">
                    This board has not been analysed yet.
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
              onReady={(r) => { rendererRef.current = r }}
              onError={(e) => setGlError(e.message)}
            />
          )}
          {/* Panning the board out of view used to need a page reload to undo. */}
          {!glError && doc && board.data && (
            <Button size="compact-xs" variant="default"
                    style={{ position: 'absolute', right: 12, bottom: 12 }}
                    onClick={() => rendererRef.current?.fit()}>
              Fit board
            </Button>
          )}
        </Box>

        {/* Side panel */}
        {/*
          420 rather than 340. The charts in these tabs are the product: a budget curve, a
          predicted spectrum against a limit. At 340 the panel gave them about 274 px, which
          scaled a 560-unit viewBox to 49 % and rendered its 9 px labels at four and a half.
        */}
        {panelOpen && (
        <Paper
          withBorder
          radius={0}
          w="clamp(340px, 32vw, 460px)"
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
            {/*
              The tabs scroll rather than grow, and their padding is tighter than Mantine's
              default. Four fit comfortably; full-wave adds two more, and `grow` used to wrap
              them onto a second row with the last one clipped by the panel edge. One row that
              scrolls is legible at any width.
            */}
            <Tabs.List style={{ flexWrap: 'nowrap', overflowX: 'auto', overflowY: 'hidden' }}>
              <Tabs.Tab value="findings" px={6}>
                <Group gap={6} wrap="nowrap">
                  <span>Findings</span>
                  {rules.data && rules.data.summary.critical > 0 && (
                    <Badge size="xs" color="red" circle>
                      {rules.data.summary.critical}
                    </Badge>
                  )}
                </Group>
              </Tabs.Tab>
              {fullWave && <Tabs.Tab value="fullwave" px={6}>Full-wave</Tabs.Tab>}
              <Tabs.Tab value="cables" px={6}>Cables</Tabs.Tab>
              {fullWave && <Tabs.Tab value="compliance" px={6}>Compliance</Tabs.Tab>}
              <Tabs.Tab value="esd" px={6}>ESD</Tabs.Tab>
              <Tabs.Tab value="board" px={6}>Board</Tabs.Tab>
            </Tabs.List>

            <Tabs.Panel value="findings" p="sm">
              {rules.isLoading && <Loader size="sm" />}
              {rules.isError && (
                <Alert color="red" variant="light" title="The findings could not be loaded">
                  <Text size="xs">{(rules.error as Error).message}</Text>
                </Alert>
              )}
              {kicadNote && (
                <Alert color="yellow" variant="light" mb="xs" withCloseButton
                       onClose={() => setKicadNote(null)}>
                  <Text size="xs">{kicadNote}</Text>
                </Alert>
              )}
              {ingestDone && (
                <RuleFindings
                  rules={(rules.data as RulesDoc | null) ?? null}
                  onFocus={onFocusFinding}
                  onSelectNet={setNet}
                  onSimulate={onSimulateFinding}
                  onShowInKiCad={kicad ? showInKiCad : undefined}
                />
              )}
            </Tabs.Panel>

            {/*
              Setting a solve up, the two libraries that feed it and the result it produces are
              one task, so they share a tab and a switch rather than four tabs competing with
              the rest of the panel for width.
            */}
            {fullWave && (
            <Tabs.Panel value="fullwave" p="sm">
              <Stack gap="sm">
                <SegmentedControl
                  size="xs"
                  fullWidth
                  value={solveView}
                  onChange={(v) => setSolveView(v as SolveView)}
                  data={[
                    { label: 'Set up', value: 'setup' },
                    { label: 'Result', value: 'result' },
                    { label: 'Drivers', value: 'drivers' },
                    { label: 'Parts', value: 'parts' },
                  ]}
                />

                {solveView === 'setup' && (
                  <>
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
                  </>
                )}

                {solveView === 'result' && (
                  <>
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
                  </>
                )}

                {solveView === 'drivers' && (
                  <>
                    <DriversPanel
                      api={api}
                      projectId={projectId}
                      nets={doc?.nets?.map((n) => n.name) ?? []}
                    />
                  </>
                )}

                {solveView === 'parts' && (
                  <>
                    <ComponentsPanel api={api} deployment={deployment}
                                     signedIn={me.data ? !me.data.anonymous : false} />
                  </>
                )}
              </Stack>
            </Tabs.Panel>
            )}

            <Tabs.Panel value="cables" p="sm">
              <CablesPanel
                api={api} projectId={projectId} boardId={ingest?.board_id}
                runId={lastCableRun?.id} workersOnline={workersOnline}
                assignments={cableAssignments} onAssignmentsChange={setCableAssignments}
              />
            </Tabs.Panel>

            {fullWave && (
            <Tabs.Panel value="compliance" p="sm">
              <ComplianceTab
                api={api}
                projectId={projectId}
                boardId={ingest?.board_id}
                solveRunId={activeSolve?.status === 'done' ? activeSolve.id : undefined}
                solveManifest={solveManifest.data}
                cableAssignments={cableAssignments}
                runId={(runs.data ?? []).find((r) => r.kind === 'compliance')?.id}
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
                    <Tooltip label="The board has not finished processing yet"
                             disabled={!!ingest?.board_id} withArrow>
                      <Button
                        size="compact-xs"
                        variant="light"
                        onClick={() => reanalyse.mutate()}
                        loading={reanalysing || reanalyse.isPending}
                        disabled={!ingest?.board_id}
                      >
                        Re-analyse
                      </Button>
                    </Tooltip>
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

                  {/* Draws a measured scan the way a solve result is drawn, so it belongs with
                      full-wave rather than beside the checks. */}
                  {fullWave && (
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
                  )}

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
        )}
      </Group>

      <Modal opened={renaming !== null} onClose={() => setRenaming(null)} title="Rename this board"
             size="sm" centered>
        {/* A real form, so Enter submits the way it does in every other dialog. A keydown
            handler on the input looked equivalent and was not. */}
        <form onSubmit={(e) => {
          e.preventDefault()
          if (renaming?.trim()) rename.mutate(renaming.trim())
        }}>
          <Stack gap="sm">
            <TextInput value={renaming ?? ''} onChange={(e) => setRenaming(e.currentTarget.value)}
                       placeholder="Board name" data-autofocus />
            {rename.isError && (
              <Text size="xs" c="red">{(rename.error as Error).message}</Text>
            )}
            <Group justify="flex-end" gap="xs">
              <Button size="xs" variant="default" type="button"
                      onClick={() => setRenaming(null)}>Cancel</Button>
              <Button size="xs" type="submit" loading={rename.isPending} disabled={!renaming?.trim()}>
                Rename
              </Button>
            </Group>
          </Stack>
        </form>
      </Modal>

      <Modal opened={confirmDelete} onClose={() => setConfirmDelete(false)} title="Delete this board"
             size="sm" centered>
        <Stack gap="sm">
          <Text size="sm">
            <Text span fw={600}>{project.data?.name}</Text>, its board file and every result on
            it are deleted{local ? ' from this computer' : ''}. This cannot be undone.
          </Text>
          {remove.isError && (
            <Text size="xs" c="red">{(remove.error as Error).message}</Text>
          )}
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={() => setConfirmDelete(false)}>Cancel</Button>
            <Button size="xs" color="red" loading={remove.isPending} onClick={() => remove.mutate()}>
              Delete
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  )
}

/**
 * The runs behind what is on screen, and the two things a user can do about one that went
 * wrong. Without this a failed or stuck run was invisible: the tab it belonged to simply never
 * filled in, and re-uploading the board was the only way forward.
 */
function RunsMenu({ runs, busy, onRetry, onStop }: {
  runs: Run[]
  busy: boolean
  onRetry: (id: string) => void
  onStop: (id: string) => void
}) {
  if (runs.length === 0) return null
  const failed = runs.filter((r) => r.status === 'failed' || r.status === 'timed_out').length
  const running = runs.filter((r) => !TERMINAL_STATUSES.includes(r.status)).length
  const colour = (s: Run['status']) =>
    s === 'done' ? 'green' : s === 'failed' || s === 'timed_out' ? 'red' : 'blue'

  return (
    <Menu position="bottom-end" withinPortal width={320}>
      <Menu.Target>
        <Button size="compact-xs" variant={failed > 0 ? 'light' : 'subtle'}
                color={failed > 0 ? 'red' : 'gray'}>
          {running > 0 ? `${running} running` : failed > 0 ? `${failed} failed` : 'Runs'}
        </Button>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Recent runs</Menu.Label>
        {runs.slice(0, 8).map((r) => (
          <Menu.Item key={r.id} closeMenuOnClick={false} component="div">
            <Group justify="space-between" wrap="nowrap" gap="xs">
              <Group gap={6} wrap="nowrap">
                <Badge size="xs" variant="light" color={colour(r.status)} tt="none">
                  {r.status.replace('_', ' ')}
                </Badge>
                <Text size="xs">{r.kind}</Text>
              </Group>
              {(r.status === 'failed' || r.status === 'timed_out') && (
                <Button size="compact-xs" variant="light" disabled={busy}
                        onClick={() => onRetry(r.id)}>
                  Retry
                </Button>
              )}
              {!TERMINAL_STATUSES.includes(r.status) && (
                <Button size="compact-xs" variant="subtle" color="gray" disabled={busy}
                        onClick={() => onStop(r.id)}>
                  Stop
                </Button>
              )}
            </Group>
            {r.error && (
              <Text size="xs" c="dimmed" lineClamp={2}>{r.error}</Text>
            )}
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  )
}
