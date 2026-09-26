/**
 * Conducted emissions: what the switching regulators put back onto the power input, against
 * FCC 15.107 (experimental, differential mode only).
 *
 * The first scan finds the power input and the regulators on it and runs on assumed settings;
 * the panel then offers each regulator's settings to fill in from its datasheet, and marks every
 * value still assumed. The result is the spectrum at the LISN, the worst margin, and what the
 * worker tried: adding a capacitor or a filter, and taking each capacitor away.
 */

import { useMemo, useState } from 'react'
import {
  Accordion, Alert, Anchor, Badge, Button, Group, List, Loader, NumberInput, SegmentedControl, Select,
  SimpleGrid, Stack, Table, Text, Tooltip,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { useEmiBase } from '../host'
import { EmiApi, TERMINAL_STATUSES, type Run, type WorkerInfo } from '../lib/emiApi'
import {
  assumedSummary, conductedParams, enteredFrom, fmtChange, fmtHz, fmtShare, PARAM_FIELDS, toDisplay, verdict,
  type Entered,
} from '../lib/conducted'
import type { ConductedDoc, ConductedParams, ConductedRegulator } from '../lib/conductedTypes'
import { ConductedChart } from './ConductedChart'
import { EXPERIMENTAL, Experimental } from './Experimental'
import { RunProgress } from './RunProgress'

export interface ConductedScanProps {
  api: EmiApi
  projectId: string
  boardId: string | null | undefined
  runs: Run[]
  workers: WorkerInfo[]
  onFocus: (x: number, y: number) => void
  onStarted?: () => void
}

export function ConductedScan({ api, projectId, boardId, runs, workers, onFocus, onStarted }: ConductedScanProps) {
  const base = useEmiBase()
  const qc = useQueryClient()
  const scans = useMemo(
    () => runs.filter((r) => r.kind === 'conducted').sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)),
    [runs],
  )
  const latestDone = scans.find((r) => r.status === 'done') ?? null
  const active = scans.find((r) => !TERMINAL_STATUSES.includes(r.status)) ?? null
  const newest = scans[0] ?? null
  const failed = newest && newest.status !== 'done' && TERMINAL_STATUSES.includes(newest.status) ? newest : null
  const lastParams = (newest?.params ?? null) as ConductedParams | null

  const [cls, setCls] = useState<'A' | 'B'>(lastParams?.class ?? 'B')
  const [entry, setEntry] = useState<string | null>(lastParams?.entry ?? null)
  // null means "what the last run was given", so reopening the tab keeps the user's values.
  const [edited, setEdited] = useState<Entered | null>(null)
  const entered = edited ?? enteredFrom(lastParams)
  const [compareId, setCompareId] = useState<string | null>(null)

  const result = useQuery({
    queryKey: ['emi', 'conducted', latestDone?.id],
    queryFn: () => api.fetchConducted(latestDone!.id),
    enabled: !!latestDone,
    staleTime: Infinity,
  })
  const capable = workers.some((w) => w.online && (w.capabilities.kinds ?? []).includes('conducted'))

  const start = useMutation({
    mutationFn: () => api.createConductedRun(projectId, boardId!, conductedParams(cls, entered, entry)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
      onStarted?.()
    },
  })

  const setField = (ref: string, name: string, value: number | null) => {
    const next: Entered = { ...entered, [ref]: { ...(entered[ref] ?? {}) } }
    if (value === null) delete next[ref][name as keyof Entered[string]]
    else next[ref][name as keyof Entered[string]] = value
    setEdited(next)
  }

  const doc = result.data
  return (
    <Stack gap="md">
      <Group gap={6} wrap="nowrap" align="flex-start">
        <Text size="xs" c="dimmed" style={{ flex: 1 }}>
          What the switching regulators put back onto the power input, through the input filter as
          laid out, measured with a LISN against FCC 15.107. Differential mode only. Settings you
          have not entered are assumed. See the{' '}
          <Anchor component={Link} to={`${base}/limitations`} size="xs">limitations</Anchor>.
        </Text>
        <Experimental why={EXPERIMENTAL.conducted} />
      </Group>

      <Group gap="xs" wrap="nowrap" align="flex-end">
        <Stack gap={2} style={{ flex: 1 }}>
          <Text size="xs" fw={500}>FCC 15.107 class</Text>
          <SegmentedControl size="xs" fullWidth value={cls} onChange={(v) => setCls(v as 'A' | 'B')}
                            data={[{ value: 'B', label: 'Class B' }, { value: 'A', label: 'Class A' }]} />
        </Stack>
        <Tooltip label="The board is still being processed" disabled={!!boardId} withArrow>
          <Button size="xs" onClick={() => start.mutate()} loading={start.isPending || !!active}
                  disabled={!boardId || !capable}>
            Scan
          </Button>
        </Tooltip>
      </Group>
      {doc && doc.entries.length > 1 && (
        <Select size="xs" label="Power input" data={doc.entries} value={entry ?? doc.entry?.id ?? null}
                onChange={setEntry} allowDeselect={false} />
      )}
      {!capable && (
        <Text size="xs" c="dimmed">
          No connected worker has the circuit simulator (ngspice). Start or restart the worker, then
          try again.
        </Text>
      )}
      {start.isError && <Alert color="red" variant="light">{(start.error as Error).message}</Alert>}
      {active && <RunProgress run={active} />}
      {failed && !active && (
        <Alert color="red" variant="light" title="The last scan failed">
          {failed.error || 'No reason was reported.'}
        </Alert>
      )}
      {!latestDone && !active && (
        <Text size="sm" c="dimmed">
          No scan yet. The first scan finds the power input and its regulators, using assumed
          settings; enter the real ones afterwards and scan again.
        </Text>
      )}
      {result.isLoading && <Loader size="sm" />}
      {result.isError && <Alert color="red" variant="light">{(result.error as Error).message}</Alert>}

      {doc && doc.regulators.length > 0 && (
        <Stack gap="xs">
          <Text size="xs" fw={500}>Regulators</Text>
          {doc.regulators.map((r) => (
            <RegulatorForm key={r.ref} reg={r} entered={entered[r.ref] ?? {}} onFocus={onFocus}
                           onChange={(name, v) => setField(r.ref, name, v)} />
          ))}
        </Stack>
      )}

      {doc && (
        <ResultView doc={doc} compareId={compareId} onCompare={setCompareId} onFocus={onFocus} />
      )}
    </Stack>
  )
}

function RegulatorForm({ reg, entered, onChange, onFocus }: {
  reg: ConductedRegulator
  entered: Entered[string]
  onChange: (name: string, value: number | null) => void
  onFocus: (x: number, y: number) => void
}) {
  return (
    <Stack gap={4} p={6} style={{ border: '1px solid var(--mantine-color-default-border)', borderRadius: 4 }}>
      <Group gap={6} wrap="nowrap">
        <Anchor size="xs" fw={500} component="button" type="button"
                onClick={() => reg.x !== undefined && reg.y !== undefined && onFocus(reg.x, reg.y)}>
          {reg.ref}
        </Anchor>
        <Text size="xs" c="dimmed" truncate>
          {reg.input_net} to {reg.switch_net}{reg.output_net ? ` to ${reg.output_net}` : ''}
        </Text>
      </Group>
      <SimpleGrid cols={2} spacing={6} verticalSpacing={4}>
        {PARAM_FIELDS.map((f) => {
          const typed = entered[f.name]
          const shown = reg.params[f.name]
          const assumed = typed === undefined && shown.assumed
          return (
            <NumberInput
              key={f.name}
              size="xs"
              label={
                // Wraps: side by side in a half-width column, the badge was cut to "assum…".
                <Group gap={4} wrap="wrap">
                  <Text size="xs">{f.label}, {f.unit}</Text>
                  {assumed && (
                    <Tooltip label={shown.source === 'rail names'
                      ? 'Worked out from the input and output rail names. Enter it to confirm it.'
                      : 'Not on the board. This default is assumed; enter the real value.'} withArrow multiline w={220}>
                      <Badge size="xs" variant="light" color="yellow" tt="none">assumed</Badge>
                    </Tooltip>
                  )}
                </Group>
              }
              placeholder={String(toDisplay(f, shown.value))}
              value={typed ?? ''}
              min={f.min}
              max={f.max}
              decimalScale={f.decimals}
              hideControls
              onChange={(v) => onChange(f.name, typeof v === 'number' ? v : v === '' ? null : Number(v))}
            />
          )
        })}
      </SimpleGrid>
    </Stack>
  )
}

function ResultView({ doc, compareId, onCompare, onFocus }: {
  doc: ConductedDoc
  compareId: string | null
  onCompare: (id: string | null) => void
  onFocus: (x: number, y: number) => void
}) {
  const laid = doc.variants.find((v) => v.id === 'as_laid_out')
  const compare = doc.variants.find((v) => v.id === compareId) ?? null
  const assumed = assumedSummary(doc)
  const caps = new Map(doc.network.caps.map((c) => [c.ref, c]))
  const dominant = doc.dominant
  const top = dominant?.shares.find((s) => s.ref !== 'LISN')
  const lisn = dominant?.shares.find((s) => s.ref === 'LISN')

  return (
    <Stack gap="sm">
      {doc.entry && (
        <Text size="xs" c="dimmed">
          Power input {doc.entry.net} at {doc.entry.connector}; rail {doc.rail_nets.join(', ')};{' '}
          {doc.network.caps.length} capacitor{doc.network.caps.length === 1 ? '' : 's'} on it.
        </Text>
      )}
      {assumed && (
        <Alert color="yellow" variant="light" p="xs">
          <Text size="xs">
            Built on assumed settings for {assumed}. The level moves with each of them; enter
            them from the datasheet and scan again.
          </Text>
        </Alert>
      )}
      {laid && laid.lines.length > 0 && (
        <>
          <Text size="sm" fw={500} c={doc.worst && doc.worst.margin_db < 0 ? 'red' : undefined}>
            {verdict(doc.worst)}
          </Text>
          <ConductedChart lines={laid.lines} quasiPeak={doc.standard.quasi_peak} average={doc.standard.average}
                          compare={compare ? { label: compare.label, lines: compare.lines } : null} />
          <Text size="xs" c="dimmed">
            Each harmonic is a steady tone, which reads the same on the quasi-peak and average
            detectors, so the average limit decides.
          </Text>
        </>
      )}
      {top && dominant && (
        <Text size="xs">
          At {fmtHz(dominant.f_hz)}, {top.ref}
          {caps.get(top.ref) ? ` (${caps.get(top.ref)!.value})` : ''} carries{' '}
          {(top.share * 100).toFixed(0)} % of {dominant.regulator}'s ripple current
          {lisn ? `; ${fmtShare(lisn.share)} reaches the LISN` : ''}.
        </Text>
      )}

      {doc.suggestions.length > 0 && (
        <Stack gap={4}>
          <Text size="xs" fw={500}>What to change</Text>
          <Table fz="xs" verticalSpacing={2} horizontalSpacing={4} highlightOnHover>
            <Table.Tbody>
              {doc.suggestions.map((s) => (
                <Table.Tr key={s.id} style={{ cursor: 'pointer' }}
                          onClick={() => onCompare(compareId === s.id ? null : s.id)}
                          bg={compareId === s.id ? 'var(--mantine-color-teal-light)' : undefined}>
                  <Table.Td>{s.label}</Table.Td>
                  <Table.Td ta="right" ff="monospace" c={(s.change_db ?? 0) > 1 ? 'teal' : 'dimmed'}
                            style={{ whiteSpace: 'nowrap' }}>
                    {fmtChange(s.change_db)}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          <Text size="10px" c="dimmed">
            Change in the worst margin. Click one to show it on the chart. Added parts are generic
            1206 capacitors and an ideal inductor.
          </Text>
        </Stack>
      )}

      {doc.components.length > 0 && (
        <Stack gap={4}>
          <Text size="xs" fw={500}>Capacitors on the input</Text>
          <Table fz="xs" verticalSpacing={2} horizontalSpacing={4}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Part</Table.Th>
                <Table.Th>Model</Table.Th>
                <Table.Th ta="right">Margin without it</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {doc.components.map((c) => {
                const cap = caps.get(c.ref)
                return (
                  <Table.Tr key={c.ref}>
                    <Table.Td>
                      <Anchor size="xs" component="button" type="button"
                              onClick={() => cap?.x !== undefined && cap?.y !== undefined && onFocus(cap.x, cap.y)}>
                        {c.ref}
                      </Anchor>{' '}
                      <Text span size="xs" c="dimmed">{cap?.value}</Text>
                    </Table.Td>
                    <Table.Td>
                      {cap?.assumed
                        ? <Badge size="xs" variant="light" color="yellow" tt="none">assumed</Badge>
                        : <Text size="10px" c="dimmed">library</Text>}
                    </Table.Td>
                    <Table.Td ta="right" ff="monospace" style={{ whiteSpace: 'nowrap' }}>
                      {fmtChange(c.without_change_db)}
                    </Table.Td>
                  </Table.Tr>
                )
              })}
            </Table.Tbody>
          </Table>
        </Stack>
      )}

      {doc.skipped.length > 0 && (
        <Stack gap={2}>
          {doc.skipped.map((s) => (
            <Text key={s.ref} size="xs" c="dimmed">Not scanned: {s.ref}, {s.why}.</Text>
          ))}
        </Stack>
      )}

      <Accordion variant="contained" chevronPosition="left">
        <Accordion.Item value="assumptions">
          <Accordion.Control><Text size="xs" fw={500}>What this scan assumes</Text></Accordion.Control>
          <Accordion.Panel>
            <List size="xs" spacing={4}>
              {[...doc.assumptions, ...doc.notes].map((a, i) => (
                <List.Item key={i}><Text size="xs" c="dimmed">{a}</Text></List.Item>
              ))}
            </List>
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value="not-modelled">
          <Accordion.Control><Text size="xs" fw={500}>Not modelled</Text></Accordion.Control>
          <Accordion.Panel>
            <List size="xs" spacing={4}>
              {doc.not_modelled.map((a, i) => (
                <List.Item key={i}><Text size="xs" c="dimmed">{a}</Text></List.Item>
              ))}
            </List>
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>
    </Stack>
  )
}
