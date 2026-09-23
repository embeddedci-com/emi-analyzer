/**
 * ESD simulation: an IEC 61000-4-2 contact discharge on every line that leaves the board.
 *
 * The geometric check says a clamp is missing or far away; this puts a number on it. Each line
 * is simulated as laid out and again with the clamp moved to the connector (or, for an
 * unprotected line, with a clamp added there). The comparison is what to act on — the absolute
 * volts rest on a generic IC pin model, and the panel says so beside every number.
 *
 * Vendor SPICE models are attached per part value, from the clamps a simulation found. The
 * file is uploaded as-is; the worker decides whether it is a model it will run, and the next
 * simulation reports what it checked.
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Accordion, Alert, Anchor, Badge, Button, FileButton, Group, List, Loader, SegmentedControl, Select,
  Stack, Table, Text, Tooltip,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { useEmiBase } from '../host'
import type { BoardDoc } from '../lib/boardTypes'
import { EmiApi, TERMINAL_STATUSES, type Run, type WorkerInfo } from '../lib/emiApi'
import {
  defaultPinMap, guessSubckt, parseSubckts, sortPadNumbers, type SubcktHeader,
} from '../lib/spiceHeader'
import type {
  ClampModelKind, TransientDoc, TransientLine, TransientModelRef, TransientParams, TransientVariant,
} from '../lib/transientTypes'
import { RunProgress } from './RunProgress'
import { TransientChart } from './TransientChart'

const LEVELS = [
  { value: '1', label: '2 kV' },
  { value: '2', label: '4 kV' },
  { value: '3', label: '6 kV' },
  { value: '4', label: '8 kV' },
]

const MODEL_LABEL: Record<ClampModelKind, string> = {
  vendor: 'uploaded model',
  table: 'datasheet model',
  illustrative: 'illustrative model',
}
const MODEL_COLOR: Record<ClampModelKind, string> = { vendor: 'teal', table: 'blue', illustrative: 'gray' }

const VARIANT_COLOR: Record<string, string> = {
  as_laid_out: 'var(--mantine-color-orange-6)',
  clamp_at_connector: 'var(--mantine-color-teal-6)',
  ideal_ground: 'var(--mantine-color-blue-5)',
  reference_clamp: 'var(--mantine-color-teal-6)',
}

const MODEL_ACCEPT = '.lib,.cir,.sub,.subckt,.mod,.model,.sp,.spi,.spice,.ckt,.txt,.inc'

export interface EsdSimulationProps {
  api: EmiApi
  projectId: string
  boardId: string | null | undefined
  doc: BoardDoc
  runs: Run[]
  workers: WorkerInfo[]
  /** A net to open, e.g. when arriving from an esd-protection finding. */
  focusNet?: string | null
  onFocus: (x: number, y: number) => void
  onSelectNet: (net: string | null) => void
  onStarted?: () => void
}

function fmtV(v: number | undefined): string {
  if (v === undefined) return '—'
  const a = Math.abs(v)
  return `${a >= 100 ? v.toFixed(0) : v.toFixed(1)} V`
}

/**
 * The peak worth showing for a variant, and where it was measured.
 *
 * At the IC pin when the line reaches one. Without an IC the clamp node is the only thing on the
 * board to measure; without either, the connector. The panel says which, because a line with no
 * IC showed its clamp voltage under a heading that said "pin", and a line with nothing on it at
 * all showed the generator's open-circuit voltage — about ten kilovolts — as if a pin saw it.
 */
function measured(v: TransientVariant | undefined): { volts?: number; where: 'pin' | 'clamp' | 'connector' } {
  if (!v) return { where: 'connector' }
  if (v.v_pin_peak_v !== undefined) return { volts: v.v_pin_peak_v, where: 'pin' }
  if (v.v_clamp_peak_v !== undefined) return { volts: v.v_clamp_peak_v, where: 'clamp' }
  return { volts: v.v_connector_peak_v, where: 'connector' }
}

function lineKey(l: TransientLine) {
  return `${l.connector.ref}:${l.connector.pad}:${l.net}`
}

export function EsdSimulation({
  api, projectId, boardId, doc, runs, workers, focusNet, onFocus, onSelectNet, onStarted,
}: EsdSimulationProps) {
  const base = useEmiBase()
  const qc = useQueryClient()
  const transients = useMemo(
    () =>
      runs
        .filter((r) => r.kind === 'transient')
        .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)),
    [runs],
  )
  const latestDone = transients.find((r) => r.status === 'done') ?? null
  const active = transients.find((r) => !TERMINAL_STATUSES.includes(r.status)) ?? null
  const newest = transients[0] ?? null
  const failed = newest && newest.status !== 'done' && TERMINAL_STATUSES.includes(newest.status) ? newest : null
  const lastParams = (newest?.params ?? null) as TransientParams | null

  const [level, setLevel] = useState(String(lastParams?.level ?? 4))
  // null means "whatever the last run used", so models attached in an earlier session survive.
  const [models, setModels] = useState<TransientModelRef[] | null>(null)
  const attached = models ?? lastParams?.models ?? []

  const result = useQuery({
    queryKey: ['emi', 'transient', latestDone?.id],
    queryFn: () => api.fetchTransient(latestDone!.id),
    enabled: !!latestDone,
    staleTime: Infinity,
  })

  const capable = workers.some((w) => w.online && (w.capabilities.kinds ?? []).includes('transient'))

  const start = useMutation({
    mutationFn: () =>
      api.createTransientRun(projectId, boardId!, {
        level: Number(level),
        polarity: 'both',
        models: attached,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
      onStarted?.()
    },
  })

  const [open, setOpen] = useState<string | null>(null)
  const lines = useMemo(() => {
    const all = result.data?.lines ?? []
    const laid = (l: TransientLine) => l.variants?.find((v) => v.id === 'as_laid_out')
    // Lines nothing protects first, then by how high the line's peak goes.
    return [...all].sort((a, b) => {
      const ua = laid(a)?.unclamped ? 1 : 0
      const ub = laid(b)?.unclamped ? 1 : 0
      if (ua !== ub) return ub - ua
      return Math.abs(measured(laid(b)).volts ?? 0) - Math.abs(measured(laid(a)).volts ?? 0)
    })
  }, [result.data])

  useEffect(() => {
    if (!focusNet) return
    const hit = lines.find((l) => l.net === focusNet)
    if (hit) setOpen(lineKey(hit))
  }, [focusNet, lines])

  return (
    <Stack gap="md">
      <Text size="xs" c="dimmed">
        Simulates an IEC 61000-4-2 contact discharge on every line that leaves the board through an
        edge connector, with the trace, the clamp and its ground via as circuit elements. Each line
        is compared with the clamp moved to the connector. Compare those numbers; the absolute
        volts are an estimate —{' '}
        <Anchor component={Link} to={`${base}/limitations`} size="xs">limitations</Anchor>.
      </Text>

      <Group gap="xs" wrap="nowrap" align="flex-end">
        <Stack gap={2} style={{ flex: 1 }}>
          <Text size="xs" fw={500}>Contact discharge</Text>
          <SegmentedControl size="xs" fullWidth value={level} onChange={setLevel} data={LEVELS} />
        </Stack>
        <Tooltip label="The board is still being processed" disabled={!!boardId} withArrow>
          <Button
            size="xs"
            onClick={() => start.mutate()}
            loading={start.isPending || !!active}
            disabled={!boardId || !capable}
          >
            Simulate
          </Button>
        </Tooltip>
      </Group>
      {!capable && (
        <Text size="xs" c="dimmed">
          No connected worker has the circuit simulator (ngspice). Start or restart the worker,
          then try again.
        </Text>
      )}
      {start.isError && (
        <Alert color="red" variant="light">{(start.error as Error).message}</Alert>
      )}

      {active && <RunProgress run={active} />}
      {failed && !active && (
        <Alert color="red" variant="light" title="The last simulation failed">
          {failed.error || 'No reason was reported.'}
        </Alert>
      )}

      {!latestDone && !active && (
        <Text size="sm" c="dimmed">No simulation yet. Pick a level and press Simulate.</Text>
      )}
      {result.isLoading && <Loader size="sm" />}
      {result.isError && (
        <Alert color="red" variant="light">{(result.error as Error).message}</Alert>
      )}

      {result.data && (
        <ResultView
          result={result.data}
          lines={lines}
          open={open}
          onOpen={setOpen}
          onFocus={onFocus}
          onSelectNet={onSelectNet}
        />
      )}

      {result.data && (
        <ModelsSection
          api={api}
          projectId={projectId}
          doc={doc}
          result={result.data}
          attached={attached}
          onChange={setModels}
        />
      )}
    </Stack>
  )
}

function ResultView({
  result, lines, open, onOpen, onFocus, onSelectNet,
}: {
  result: TransientDoc
  lines: TransientLine[]
  open: string | null
  onOpen: (key: string | null) => void
  onFocus: (x: number, y: number) => void
  onSelectNet: (net: string | null) => void
}) {
  return (
    <Stack gap="sm">
      <Text size="xs" c="dimmed">
        {result.kv} kV contact discharge, both polarities, on {lines.length} line
        {lines.length === 1 ? '' : 's'}; the worse polarity is shown. Each line's peak as laid out is
        measured at its IC pin, or at the clamp or the connector when the line reaches no IC.
      </Text>
      {result.source_check.length > 0 && (
        <Alert color="red" variant="light" title="The discharge source missed the standard">
          {result.source_check.join('; ')}
        </Alert>
      )}
      {lines.length === 0 && (
        <Text size="sm" c="dimmed">
          {/* The worker's note is the specific reason; the fallback covers a run that said
              nothing, which used to show a two-word shrug. */}
          {result.notes[result.notes.length - 1]
            ?? 'No line on this board leaves it through an edge connector, so there is nothing '
               + 'for a discharge to reach.'}
        </Text>
      )}

      <Accordion variant="separated" value={open} onChange={onOpen} chevronPosition="left">
        {lines.map((line) => (
          <LineItem
            key={lineKey(line)}
            line={line}
            onFocus={onFocus}
            onSelectNet={onSelectNet}
          />
        ))}
      </Accordion>

      {(result.notes.length > 0 || result.assumptions.length > 0) && (
        <Accordion variant="contained" chevronPosition="left">
          <Accordion.Item value="assumptions">
            <Accordion.Control>
              <Text size="xs" fw={500}>What this simulation assumes</Text>
            </Accordion.Control>
            <Accordion.Panel>
              <List size="xs" spacing={4}>
                {[...result.assumptions, ...result.notes].map((a, i) => (
                  <List.Item key={i}><Text size="xs" c="dimmed">{a}</Text></List.Item>
                ))}
              </List>
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      )}
    </Stack>
  )
}

function LineItem({
  line, onFocus, onSelectNet,
}: {
  line: TransientLine
  onFocus: (x: number, y: number) => void
  onSelectNet: (net: string | null) => void
}) {
  const variants = line.variants ?? []
  const laid = variants.find((v) => v.id === 'as_laid_out')
  const better = variants.find((v) => v.id === 'clamp_at_connector' || v.id === 'reference_clamp')
  const peak = measured(laid)
  const g = line.geometry

  const comparison = (() => {
    if (!laid?.v_pin_peak_v || better?.v_pin_peak_v === undefined) return null
    const from = Math.abs(laid.v_pin_peak_v)
    const to = Math.abs(better.v_pin_peak_v)
    const what = better.id === 'reference_clamp' ? 'A clamp at the connector' : 'Moving the clamp to the connector'
    if (to >= from * 0.9) {
      return `${what} would change the pin's peak little (${fmtV(from)} to ${fmtV(to)}).`
    }
    return `${what} lowers the pin's peak from ${fmtV(from)} to ${fmtV(to)}.`
  })()

  const path = [
    line.connector.ref,
    line.series_resistor ? `${line.series_resistor.ref} (${line.series_resistor.ohm} Ω)` : null,
    line.clamp ? line.clamp.ref : 'no clamp',
    line.ic ? line.ic.ref : 'no IC',
  ].filter(Boolean).join(' → ')

  return (
    <Accordion.Item value={lineKey(line)}>
      <Accordion.Control>
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <div style={{ minWidth: 0 }}>
            <Text size="sm" fw={500} truncate>{line.net}</Text>
            <Text size="xs" c="dimmed" truncate>
              {path}{!line.error && !laid?.unclamped && ` · at the ${peak.where}`}
            </Text>
          </div>
          {line.error ? (
            <Badge color="red" variant="light" style={{ flex: 'none' }}>failed</Badge>
          ) : laid?.unclamped ? (
            <Badge color="red" variant="light" style={{ flex: 'none' }}>unclamped</Badge>
          ) : (
            <Badge color="gray" variant="light" style={{ flex: 'none' }}>{fmtV(peak.volts)}</Badge>
          )}
        </Group>
      </Accordion.Control>
      <Accordion.Panel>
        {line.error ? (
          <Alert color="red" variant="light" title="This line could not be simulated">{line.error}</Alert>
        ) : (
          <Stack gap="xs">
            {laid?.unclamped && (
              <Alert color="red" variant="light">
                Nothing on this line takes the discharge: no clamp, and nothing else that loads it, so
                the connector rises to about the generator's full voltage. A cable plugged in here
                hands the discharge to whatever the line reaches.
              </Alert>
            )}
            {line.t_ns && variants.some((v) => v.v_pin) && (
              <TransientChart
                t={line.t_ns}
                unit="V at pin"
                series={variants
                  .filter((v) => v.v_pin)
                  .map((v) => ({ label: v.label, values: v.v_pin!, color: VARIANT_COLOR[v.id] ?? 'gray' }))}
              />
            )}
            {comparison && <Text size="xs">{comparison}</Text>}

            <Table fz="xs" verticalSpacing={2} horizontalSpacing={4}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th />
                  <Table.Th ta="right">Pin</Table.Th>
                  <Table.Th ta="right">Into pin</Table.Th>
                  <Table.Th ta="right">Clamp</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {variants.map((v) => (
                  <Table.Tr key={v.id}>
                    <Table.Td>{v.label}</Table.Td>
                    <Table.Td ta="right" ff="monospace">{fmtV(v.v_pin_peak_v)}</Table.Td>
                    <Table.Td ta="right" ff="monospace">
                      {v.i_pin_peak_a !== undefined ? `${Math.abs(v.i_pin_peak_a).toFixed(2)} A` : '—'}
                    </Table.Td>
                    <Table.Td ta="right" ff="monospace">
                      {v.i_clamp_peak_a !== undefined ? `${Math.abs(v.i_clamp_peak_a).toFixed(1)} A` : '—'}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>

            {line.clamp && (
              <Group gap={6} wrap="nowrap" align="flex-start">
                <Badge size="xs" variant="light" color={MODEL_COLOR[line.clamp.model]} style={{ flex: 'none' }}>
                  {MODEL_LABEL[line.clamp.model]}
                </Badge>
                <Text size="xs" c="dimmed">
                  {line.clamp.ref} {line.clamp.part}
                  {line.clamp.assumptions.length > 0 && ` — ${line.clamp.assumptions.join('; ')}`}
                </Text>
              </Group>
            )}

            <Text size="xs" c="dimmed">
              {g.lead && `Lead ${g.lead.length_mm} mm · `}
              Trunk {g.trunk.length_mm} mm
              {g.clamp_stub && ` · clamp stub ${g.clamp_stub.length_mm} mm`}
              {g.ic_stub && ` · IC stub ${g.ic_stub.length_mm} mm`}
              {line.clamp && ` · clamp ground ${g.clamp_ground_mm} mm (${g.clamp_ground_nh} nH)`}
              {!!g.net_c_nf && ` · ${g.net_c_nf >= 1000 ? `${(g.net_c_nf / 1000).toFixed(1)} µF` : `${g.net_c_nf.toFixed(0)} nF`} on the net`}
              {!g.routed && ' · placed from straight-line distances'}
            </Text>
            {line.notes.map((n, i) => (
              <Text key={i} size="xs" c="dimmed">{n}</Text>
            ))}
            <Anchor
              size="xs"
              component="button"
              type="button"
              onClick={() => {
                onFocus(line.x, line.y)
                onSelectNet(line.net)
              }}
            >
              Show on the board
            </Anchor>
          </Stack>
        )}
      </Accordion.Panel>
    </Accordion.Item>
  )
}

interface Draft {
  part: string
  ref: string
  file: File
  headers: SubcktHeader[]
  subckt: string
  pins: Record<string, string>
}

function ModelsSection({
  api, projectId, doc, result, attached, onChange,
}: {
  api: EmiApi
  projectId: string
  doc: BoardDoc
  result: TransientDoc
  attached: TransientModelRef[]
  onChange: (models: TransientModelRef[]) => void
}) {
  const clamps = useMemo(() => {
    const byPart = new Map<string, Set<string>>()
    for (const l of result.lines) {
      if (!l.clamp) continue
      const refs = byPart.get(l.clamp.part) ?? new Set<string>()
      refs.add(l.clamp.ref)
      byPart.set(l.clamp.part, refs)
    }
    return [...byPart.entries()].map(([part, refs]) => ({ part, refs: [...refs].sort() }))
  }, [result])

  const [draft, setDraft] = useState<Draft | null>(null)
  const [readError, setReadError] = useState<string | null>(null)
  const upload = useMutation({
    mutationFn: async (d: Draft) => {
      const { key, filename } = await api.uploadModelFile(projectId, d.file)
      return { part: d.part, key, filename, subckt: d.subckt, pins: d.pins } satisfies TransientModelRef
    },
    onSuccess: (model) => {
      onChange([...attached.filter((m) => m.part.toUpperCase() !== model.part.toUpperCase()), model])
      setDraft(null)
    },
  })

  if (clamps.length === 0) return null

  const padsOf = (ref: string) => sortPadNumbers(doc.pads.filter((p) => p.ref === ref).map((p) => p.number))
  const netOf = (ref: string, number: string) => doc.pads.find((p) => p.ref === ref && p.number === number)?.net ?? ''

  const pick = async (part: string, ref: string, file: File | null) => {
    setReadError(null)
    if (!file) return
    const text = await file.text()
    const headers = parseSubckts(text)
    if (headers.length === 0) {
      setReadError(`${file.name} has no .subckt, so there is nothing to connect to ${ref}'s pads.`)
      return
    }
    const pads = padsOf(ref)
    const guess = guessSubckt(headers, part, pads.length) ?? headers[0]
    setDraft({ part, ref, file, headers, subckt: guess.name, pins: defaultPinMap(pads, guess.pins) })
  }

  return (
    <Stack gap="xs">
      <Text size="xs" fw={600} tt="uppercase" c="dimmed">Clamp models</Text>
      <Text size="xs" c="dimmed">
        Upload the manufacturer's SPICE model to replace the datasheet model. The worker checks
        the file is a model — nothing that runs commands or reads files — and that it clamps,
        before using it in the next simulation.
      </Text>
      {readError && <Alert color="red" variant="light">{readError}</Alert>}

      {clamps.map(({ part, refs }) => {
        const mine = attached.find((m) => m.part.toUpperCase() === part.toUpperCase())
        const report = result.models.find((m) => m.part.toUpperCase() === part.toUpperCase())
        return (
          <Stack key={part} gap={4} p="xs" style={{ background: 'var(--mantine-color-default-hover)', borderRadius: 4 }}>
            <Group justify="space-between" wrap="nowrap">
              <div style={{ minWidth: 0 }}>
                <Text size="sm" fw={500} truncate>{part}</Text>
                <Text size="xs" c="dimmed" truncate>{refs.join(', ')}</Text>
              </div>
              <FileButton accept={MODEL_ACCEPT} onChange={(f) => pick(part, refs[0], f)}>
                {(props) => (
                  <Button {...props} size="compact-xs" variant="light">
                    {mine ? 'Replace model' : 'Upload model'}
                  </Button>
                )}
              </FileButton>
            </Group>
            {mine && (
              <Group gap={6} wrap="nowrap">
                <Text size="xs" style={{ flex: 1 }} truncate>
                  {mine.filename} · {mine.subckt}
                  {report?.file === mine.filename ? '' : ' — used from the next simulation'}
                </Text>
                <Anchor
                  size="xs"
                  component="button"
                  type="button"
                  c="red"
                  onClick={() => onChange(attached.filter((m) => m !== mine))}
                >
                  Remove
                </Anchor>
              </Group>
            )}
            {report && (
              <Stack gap={2}>
                <Badge size="xs" variant="light" color={report.status === 'accepted' ? 'teal' : 'red'}>
                  {report.status === 'accepted' ? 'model accepted' : 'model rejected'}
                </Badge>
                {report.checks.map((c, i) => (
                  <Text key={i} size="xs" c={c.ok ? 'dimmed' : 'red'}>
                    {c.ok ? '✓' : '✗'} {c.name}: {c.detail}
                  </Text>
                ))}
                {report.status === 'rejected' && report.checks.length === 0 && report.reasons.map((r, i) => (
                  <Text key={i} size="xs" c="red">{r}</Text>
                ))}
              </Stack>
            )}

            {draft?.part === part && (
              <Stack gap={4} mt={4}>
                <Text size="xs">
                  Map {draft.ref}'s pads to the pins of the model in {draft.file.name}.
                </Text>
                {draft.headers.length > 1 && (
                  <Select
                    size="xs"
                    label="Subcircuit"
                    data={draft.headers.map((h) => h.name)}
                    value={draft.subckt}
                    allowDeselect={false}
                    onChange={(name) => {
                      const h = draft.headers.find((x) => x.name === name)
                      if (h) setDraft({ ...draft, subckt: h.name, pins: defaultPinMap(padsOf(draft.ref), h.pins) })
                    }}
                  />
                )}
                {padsOf(draft.ref).map((pad) => (
                  <Group key={pad} gap="xs" wrap="nowrap">
                    <Text size="xs" w={110} truncate>
                      pad {pad}{netOf(draft.ref, pad) ? ` · ${netOf(draft.ref, pad)}` : ''}
                    </Text>
                    <Select
                      size="xs"
                      style={{ flex: 1 }}
                      placeholder="not connected"
                      clearable
                      data={draft.headers.find((h) => h.name === draft.subckt)?.pins ?? []}
                      value={draft.pins[pad] ?? null}
                      onChange={(pin) => {
                        const pins = { ...draft.pins }
                        if (pin) pins[pad] = pin
                        else delete pins[pad]
                        setDraft({ ...draft, pins })
                      }}
                    />
                  </Group>
                ))}
                {upload.isError && (
                  <Alert color="red" variant="light">{(upload.error as Error).message}</Alert>
                )}
                <Group gap="xs" justify="flex-end">
                  <Button size="compact-xs" variant="subtle" onClick={() => setDraft(null)}>Cancel</Button>
                  <Button size="compact-xs" loading={upload.isPending} onClick={() => upload.mutate(draft)}>
                    Use this model
                  </Button>
                </Group>
              </Stack>
            )}
          </Stack>
        )
      })}
    </Stack>
  )
}
