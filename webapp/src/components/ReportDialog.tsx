/**
 * Export a report: the thing a user hands to their team or a test lab.
 *
 * Built in the browser from the artifacts the board page already reads, so it works the same
 * against a hosted server, emi-local and the desktop app, with nothing new on the server. The
 * HTML file carries everything inline and prints to PDF from any browser; the JSON is the same
 * content for a script.
 *
 * A result that has not been run is listed with a button to run it, never silently left out.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Anchor, Box, Button, Checkbox, Group, Loader, Modal, ScrollArea, SegmentedControl, Select,
  Stack, Text,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { BoardDoc, RulesDoc } from '../lib/boardTypes'
import { latestDone, type Version } from '../lib/compare'
import { TERMINAL_STATUSES, type EmiApi, type Features, type Run } from '../lib/emiApi'
import { isSmallPartRun } from '../lib/smallPart'
import type { TransientParams } from '../lib/transientTypes'
import { renderBoardImage } from '../lib/report/boardImage'
import { renderReportHtml } from '../lib/report/html'
import {
  SECTIONS, assembleReport, reportFileName, reportJson, type SectionId,
} from '../lib/report/model'
import type { Assignment } from './CablesPanel'

/** Stamped at build time (vite.app.config.ts); empty in a host that does not set it. */
const APP_VERSION: string = import.meta.env.VITE_EMI_VERSION ?? ''

type Status = 'ready' | 'loading' | 'running' | 'missing' | 'failed' | 'unavailable'

interface SectionState {
  id: SectionId
  label: string
  status: Status
  note?: string
}

export interface ReportDialogProps {
  api: EmiApi
  projectId: string
  projectName: string
  versions: Version[]
  version: Version
  /** Every run of the project, all versions. */
  runs: Run[]
  ingest: Run
  doc: BoardDoc
  geometry: ArrayBuffer
  rules: RulesDoc | null
  features: Features | undefined
  cableAssignments: Record<string, Assignment>
  onClose: () => void
  /** A run was started from here; the page polls again. */
  onStarted: () => void
  /** Open a tab of the board page, to set a run up there instead. */
  onOpenTab: (tab: 'cables' | 'esd') => void
}

function download(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

const newestOf = (runs: Run[], boardId: string, kind: Run['kind']) =>
  runs
    .filter((r) => r.board_id === boardId && r.kind === kind)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0] ?? null

export function ReportDialog(props: ReportDialogProps) {
  const {
    api, projectId, projectName, versions, version, runs, ingest, doc, geometry, rules, features,
    cableAssignments, onClose, onStarted, onOpenTab,
  } = props
  const qc = useQueryClient()
  const boardId = version.board.id
  const fullWave = features?.full_wave === true
  const smallPart = features?.small_part_solve === true || fullWave

  // ---- the runs behind each section ----

  const cableRun = newestOf(runs, boardId, 'cable')
  const cableDone = latestDone(runs, boardId, 'cable')
  const esdRun = newestOf(runs, boardId, 'transient')
  const esdDone = latestDone(runs, boardId, 'transient')
  const complianceDone = fullWave ? latestDone(runs, boardId, 'compliance') : null
  const solves = useMemo(
    () => runs.filter((r) => r.board_id === boardId && r.kind === 'solve' && r.status === 'done'
      && (isSmallPartRun(r.params) ? smallPart : fullWave)),
    [runs, boardId, smallPart, fullWave],
  )

  const cables = useQuery({
    queryKey: ['emi', 'cables', cableDone?.id],
    queryFn: () => api.fetchCables(cableDone!.id),
    enabled: !!cableDone,
    staleTime: Infinity,
  })
  const esd = useQuery({
    queryKey: ['emi', 'transient', esdDone?.id],
    queryFn: () => api.fetchTransient(esdDone!.id),
    enabled: !!esdDone,
    staleTime: Infinity,
  })
  const compliance = useQuery({
    queryKey: ['emi', 'compliance', complianceDone?.id],
    queryFn: () => api.fetchCompliance(complianceDone!.id),
    enabled: !!complianceDone,
    staleTime: Infinity,
  })

  // ---- the earlier version to compare with ----

  const earlier = versions.filter((v) => v.number < version.number)
  const [sinceId, setSinceId] = useState<string | null>(earlier[earlier.length - 1]?.board.id ?? null)
  const since = earlier.find((v) => v.board.id === sinceId) ?? null
  const [deselected, setDeselected] = useState<Set<SectionId>>(new Set())
  const wantChanges = !!since && !deselected.has('changes')
  const sinceIngest = since ? latestDone(runs, since.board.id, 'ingest') : null
  const sinceCable = since ? latestDone(runs, since.board.id, 'cable') : null
  const sinceEsd = since ? latestDone(runs, since.board.id, 'transient') : null
  const before = useQuery({
    queryKey: ['emi', 'report', 'before', sinceIngest?.id, sinceCable?.id, sinceEsd?.id, ingest.id],
    queryFn: async () => {
      const [rulesBefore, netsBefore, netsAfter, cablesBefore, esdBefore] = await Promise.all([
        api.fetchRules(sinceIngest!.id),
        api.fetchNets(sinceIngest!.id),
        api.fetchNets(ingest.id),
        sinceCable ? api.fetchCables(sinceCable.id) : Promise.resolve(null),
        sinceEsd ? api.fetchTransient(sinceEsd.id) : Promise.resolve(null),
      ])
      return { rulesBefore, netsBefore, netsAfter, cablesBefore, esdBefore }
    },
    enabled: wantChanges && !!sinceIngest,
    staleTime: Infinity,
  })

  // ---- starting a missing run ----

  const runCables = useMutation({
    mutationFn: () => api.createCableRun(projectId, boardId, cableAssignments),
    onSuccess: () => {
      onStarted()
      qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
    },
  })
  const runEsd = useMutation({
    mutationFn: () => {
      // The level and the vendor models the last simulation used, as the ESD tab would.
      const last = (esdRun?.params ?? null) as TransientParams | null
      return api.createTransientRun(projectId, boardId, {
        level: last?.level ?? 4, polarity: 'both', models: last?.models ?? [],
      })
    },
    onSuccess: () => {
      onStarted()
      qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
    },
  })

  // ---- what each section can be ----

  const runState = (newest: Run | null, done: Run | null, loading: boolean, error: boolean): Pick<SectionState, 'status' | 'note'> => {
    // A finished result is used even while a newer run is going: it is still this version's.
    if (done) {
      if (error) return { status: 'failed', note: 'The result could not be loaded' }
      return loading ? { status: 'loading' } : { status: 'ready' }
    }
    if (newest && !TERMINAL_STATUSES.includes(newest.status)) return { status: 'running', note: 'Running' }
    if (newest) return { status: 'failed', note: 'The last run did not finish' }
    return { status: 'missing', note: 'Not run on this version yet' }
  }

  const states: SectionState[] = SECTIONS.flatMap((s): SectionState[] => {
    switch (s.id) {
      case 'findings':
        return [rules ? { ...s, status: 'ready' } : { ...s, status: 'unavailable', note: 'No findings were loaded' }]
      case 'cables':
        return [{ ...s, ...runState(cableRun, cableDone, cables.isLoading, cables.isError) }]
      case 'esd':
        return [{ ...s, ...runState(esdRun, esdDone, esd.isLoading, esd.isError) }]
      case 'changes':
        if (earlier.length === 0) return [{ ...s, status: 'unavailable', note: 'This is the first version' }]
        if (!sinceIngest) return [{ ...s, status: 'unavailable', note: 'That version has no finished analysis' }]
        if (before.isError) return [{ ...s, status: 'failed', note: (before.error as Error).message }]
        return [{ ...s, status: wantChanges && before.isLoading ? 'loading' : 'ready' }]
      case 'experimental':
        // Never offered unless an experimental feature is on.
        if (!fullWave && !smallPart) return []
        if (complianceDone && compliance.isLoading) return [{ ...s, status: 'loading' }]
        return [complianceDone || solves.length
          ? { ...s, status: 'ready' }
          : { ...s, status: 'unavailable', note: 'No experimental run on this version' }]
      default:
        return [{ ...s, status: 'ready' }]
    }
  })

  const selected = states
    .filter((s) => (s.status === 'ready' || s.status === 'loading') && !deselected.has(s.id))
    .map((s) => s.id)
  const loading = states.some((s) => s.status === 'loading' && selected.includes(s.id))
  const omitted = states
    .filter((s) => (s.status === 'missing' || s.status === 'running' || s.status === 'failed') && !deselected.has(s.id))
    .map((s) => ({
      label: s.label,
      reason: s.status === 'running' ? 'still running when this report was made' : (s.note ?? 'not available').toLowerCase(),
    }))

  const toggle = (id: SectionId, on: boolean) =>
    setDeselected((prev) => {
      const next = new Set(prev)
      if (on) next.delete(id)
      else next.add(id)
      return next
    })

  // ---- the report ----

  const [format, setFormat] = useState<'html' | 'json'>('html')
  // Drawn once per dialog: a dense board is half a million triangles.
  const [image, setImage] = useState<ReturnType<typeof renderBoardImage> | undefined>(undefined)
  useEffect(() => {
    const t = setTimeout(() => setImage(renderBoardImage(doc, geometry)), 0)
    return () => clearTimeout(t)
  }, [doc, geometry])

  const selectedKey = selected.join(',')
  const data = useMemo(() => {
    if (loading || image === undefined) return null
    return assembleReport({
      projectName,
      version: {
        number: version.number,
        count: versions.length,
        filename: version.filename,
        uploadedAt: version.board.created_at,
        sha256: version.board.content_sha256,
      },
      ingest,
      board: doc,
      rules,
      features: features ?? { full_wave: false },
      appVersion: APP_VERSION,
      generatedAt: new Date(),
      sections: selectedKey.split(',').filter(Boolean) as SectionId[],
      boardImage: image,
      cables: cableDone && cables.data ? { run: cableDone, doc: cables.data } : null,
      esd: esdDone && esd.data ? { run: esdDone, doc: esd.data } : null,
      compare: since && before.data ? { versionNumber: since.number, ...before.data } : null,
      experimental: { compliance: complianceDone && compliance.data ? { run: complianceDone, doc: compliance.data } : null, solves },
    })
  }, [loading, image, projectName, version, versions.length, ingest, doc, rules, features, selectedKey,
      cableDone, cables.data, esdDone, esd.data, since, before.data, complianceDone, compliance.data, solves])

  const omittedKey = JSON.stringify(omitted)
  const html = useMemo(
    () => (data ? renderReportHtml(data, doc, { omitted: JSON.parse(omittedKey) }) : ''),
    [data, doc, omittedKey],
  )
  const json = useMemo(() => (data ? reportJson(data) : ''), [data])
  const frame = useRef<HTMLIFrameElement>(null)

  const save = () => {
    if (!data) return
    if (format === 'html') download(reportFileName(projectName, version.number, 'html'), html, 'text/html;charset=utf-8')
    else download(reportFileName(projectName, version.number, 'json'), json, 'application/json')
  }

  return (
    <Modal opened onClose={onClose} title="Export report" size="min(1200px, 96vw)" centered>
      <Box style={{ display: 'grid', gridTemplateColumns: 'minmax(260px, 340px) minmax(0, 1fr)', gap: 16 }}>
        <Stack gap="sm">
          <Text size="xs" c="dimmed">
            A single file to share with your team or a test lab. It states plainly that it is not a
            pre-compliance test.
          </Text>
          <Stack gap={6}>
            <Text size="xs" fw={600} tt="uppercase" c="dimmed">Sections</Text>
            <Checkbox size="xs" checked readOnly label="Title page: board, versions, stackup, settings" />
            {states.map((s) => (
              <Box key={s.id}>
                <Checkbox
                  size="xs"
                  label={s.label}
                  checked={(s.status === 'ready' || s.status === 'loading') && !deselected.has(s.id)}
                  disabled={!(s.status === 'ready' || s.status === 'loading')}
                  onChange={(e) => toggle(s.id, e.currentTarget.checked)}
                />
                {s.id === 'changes' && earlier.length > 0 && (
                  <Select
                    size="xs" ml={28} mt={4} w={200} aria-label="Compare with"
                    allowDeselect={false}
                    value={sinceId}
                    onChange={setSinceId}
                    data={[...earlier].reverse().map((v) => ({ value: v.board.id, label: `since v${v.number}` }))}
                  />
                )}
                {s.status === 'loading' && <Group gap={6} ml={28}><Loader size={10} /><Text size="xs" c="dimmed">Loading</Text></Group>}
                {s.note && s.status !== 'ready' && s.status !== 'loading' && (
                  <Group gap={6} ml={28} mt={2} wrap="nowrap">
                    {s.status === 'running' && <Loader size={10} />}
                    <Text size="xs" c={s.status === 'failed' ? 'red' : 'dimmed'}>{s.note}</Text>
                    {(s.id === 'cables' || s.id === 'esd') && (s.status === 'missing' || s.status === 'failed') && (
                      <>
                        <Button size="compact-xs" variant="light"
                                loading={s.id === 'cables' ? runCables.isPending : runEsd.isPending}
                                onClick={() => (s.id === 'cables' ? runCables : runEsd).mutate()}>
                          Run now
                        </Button>
                        <Anchor size="xs" component="button" type="button" onClick={() => onOpenTab(s.id as 'cables' | 'esd')}>
                          Set up
                        </Anchor>
                      </>
                    )}
                  </Group>
                )}
              </Box>
            ))}
          </Stack>
          {(runCables.error || runEsd.error) && (
            <Alert color="red" variant="light">
              <Text size="xs">{((runCables.error ?? runEsd.error) as Error).message}</Text>
            </Alert>
          )}
          {omitted.length > 0 && (
            <Text size="xs" c="dimmed">
              Left out for now: {omitted.map((o) => o.label).join(', ')}. The report lists
              them as not included.
            </Text>
          )}
          <Stack gap={4}>
            <Text size="xs" fw={600} tt="uppercase" c="dimmed">Format</Text>
            <SegmentedControl size="xs" value={format} onChange={(v) => setFormat(v as 'html' | 'json')}
                              data={[{ label: 'HTML report', value: 'html' }, { label: 'JSON data', value: 'json' }]} />
            <Text size="xs" c="dimmed">
              {format === 'html'
                ? 'One file with everything inline. Open it in a browser and print to save a PDF.'
                : 'The same content as structured data, for scripts and CI.'}
            </Text>
          </Stack>
          <Group gap="xs">
            <Button size="xs" onClick={save} disabled={!data}>
              Download {format === 'html' ? 'HTML' : 'JSON'}
            </Button>
            {format === 'html' && (
              <Button size="xs" variant="default" disabled={!data}
                      onClick={() => frame.current?.contentWindow?.print()}>
                Print or save as PDF
              </Button>
            )}
          </Group>
        </Stack>

        <Box style={{ border: '1px solid var(--mantine-color-default-border)', borderRadius: 4, overflow: 'hidden' }}
             h="min(70vh, 760px)">
          {!data ? (
            <Group justify="center" h="100%"><Loader size="sm" /><Text size="sm" c="dimmed">Preparing the report</Text></Group>
          ) : format === 'html' ? (
            // No scripts: the report has none, and its own policy forbids them. Same origin only
            // so the print button can reach it.
            <iframe ref={frame} title="Report preview" srcDoc={html} sandbox="allow-same-origin allow-modals"
                    style={{ width: '100%', height: '100%', border: 0, background: '#fff' }} />
          ) : (
            <ScrollArea h="100%">
              <Box component="pre" m={0} p="sm" style={{ fontSize: 11 }}>
                {json.length > 200_000 ? `${json.slice(0, 200_000)}\n…` : json}
              </Box>
            </ScrollArea>
          )}
        </Box>
      </Box>
    </Modal>
  )
}
