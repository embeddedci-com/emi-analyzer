/**
 * Net groups and suppressions in the Checks view.
 *
 * Both used to live only in emi.rules.yaml. The app now edits its own, in the same "run"
 * layer as the rule switches and thresholds, and lists the rules file's beside them read-only:
 * a file committed with the board is not the app's to rewrite, and the export carries both.
 *
 * Every pattern shows which of this board's nets it covers, matched the way the worker
 * matches them (rulesSettings.globToRegExp), because a pattern that covers nothing is the
 * usual reason an override "does not work".
 */

import { useMemo, useState } from 'react'
import {
  Anchor, Badge, Box, Button, CloseButton, Group, Modal, Paper, Select, Stack, Text, Textarea,
  TextInput, Tooltip,
} from '@mantine/core'
import type { RuleFinding } from '../lib/boardTypes'
import {
  PER_NET_PARAMS, RULES, SOURCE_LABEL, checkGroup, checkSuppression, matchNets, netList,
  paramLabel, setGroups, setSuppressions, type AppLayer, type GroupErrors, type NetGroup,
  type SettingSource, type SettingsSnapshot, type Suppression, type SuppressionErrors,
} from '../lib/rulesSettings'

const RULE_TITLE: Record<string, string> = Object.fromEntries(RULES.map((r) => [r.id, r.title]))

export function SourceBadge({ source }: { source: SettingSource }) {
  return (
    <Badge size="xs" variant="light" tt="none" color={source === 'run' ? 'blue' : 'grape'}>
      {SOURCE_LABEL[source]}
    </Badge>
  )
}

/** "Matches 3 nets: CLK, DATA, DQ0", or a warning when a pattern covers nothing here. */
function NetPreview({ pattern, nets }: { pattern: string; nets: string[] }) {
  const hits = useMemo(() => (pattern.trim() ? matchNets(pattern.trim(), nets) : []), [pattern, nets])
  if (!pattern.trim() || !nets.length) return null
  if (!hits.length) return <Text size="xs" c="yellow.8">Matches no nets on this board.</Text>
  return (
    <Text size="xs" c="dimmed">
      Matches {hits.length === nets.length ? 'every net' : `${hits.length} net${hits.length === 1 ? '' : 's'}`}
      {hits.length < nets.length && <>: <Text span size="xs" ff="monospace">{netList(hits)}</Text></>}
    </Text>
  )
}

function paramText(key: string, value: number): string {
  const { label, unit } = paramLabel(key)
  return `${label} ${value}${unit ? ` ${unit}` : ''}`
}

const PARAM_OPTIONS = RULES
  .map((r) => ({
    group: r.title,
    items: PER_NET_PARAMS.filter((p) => p.rule.id === r.id).map((p) => {
      const { label, unit } = paramLabel(p.key)
      return { value: p.key, label: unit ? `${label} (${unit})` : label }
    }),
  }))
  .filter((g) => g.items.length > 0)

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

interface EditorProps {
  base: SettingsSnapshot
  draft: AppLayer
  saved: AppLayer
  nets: string[]
  onChange: (next: AppLayer) => void
  /** Whether a form is open, so the page can hold the save until it is finished. */
  onEditing: (editing: boolean) => void
}

/** One row of a list: what it is, where it came from, and edit/remove for the app's own. */
function Row({ title, children, source, unsaved, onEdit, onRemove, label }: {
  /** The first line, beside the badges; the rest runs the full width underneath. */
  title: React.ReactNode
  children: React.ReactNode
  source: SettingSource
  unsaved: boolean
  onEdit?: () => void
  onRemove?: () => void
  label: string
}) {
  return (
    <Stack gap={0}>
      <Group gap="xs" wrap="nowrap" align="center">
        <Box style={{ flex: 1, minWidth: 0 }}>{title}</Box>
        <Group gap={4} wrap="nowrap" style={{ flex: 'none' }}>
          {unsaved && <Badge size="xs" variant="outline" tt="none">unsaved</Badge>}
          <Tooltip label="Edit it in the rules file" disabled={source === 'run'} withArrow>
            <span><SourceBadge source={source} /></span>
          </Tooltip>
          {onEdit && <Anchor size="xs" component="button" type="button" onClick={onEdit}>Edit</Anchor>}
          {onRemove && <CloseButton size="xs" aria-label={`Remove ${label}`} onClick={onRemove} />}
        </Group>
      </Group>
      {children}
    </Stack>
  )
}

// ---- net groups ----

export function NetGroupsEditor({ base, draft, saved, nets, onChange, onEditing }: EditorProps) {
  const [editing, setEditingState] = useState<number | 'new' | null>(null)
  const setEditing = (v: number | 'new' | null) => { setEditingState(v); onEditing(v !== null) }
  const mine = draft.groups ?? []
  const file = base.groups

  const commit = (group: NetGroup) => {
    const next = editing === 'new' ? [...mine, group] : mine.map((g, i) => (i === editing ? group : g))
    onChange(setGroups(draft, base, next))
    setEditing(null)
  }

  return (
    <Paper withBorder p="xs">
      <Group justify="space-between" mb={4}>
        <Text size="xs" fw={600} tt="uppercase" c="dimmed">Net groups</Text>
        {editing === null && (
          <Button size="compact-xs" variant="subtle" onClick={() => setEditing('new')}>Add group</Button>
        )}
      </Group>
      <Stack gap={6}>
        <Text size="xs" c="dimmed">
          Different budgets for some nets, such as a tighter byte lane on DQ*. The last matching
          group wins.
        </Text>
        {file.map((g, i) => (
          <Row key={`f${i}`} source={g.source ?? 'file'} unsaved={false} label="group"
               title={<GroupTitle group={g} />}>
            <GroupSummary group={g} nets={nets} />
          </Row>
        ))}
        {mine.map((g, i) => editing === i
          ? <GroupForm key={`m${i}`} initial={g} nets={nets} onDone={commit} onCancel={() => setEditing(null)} />
          : (
            <Row key={`m${i}`} source="run" label={`group ${g.match}`} title={<GroupTitle group={g} />}
                 unsaved={!(saved.groups ?? []).some((s) => same(s, g))}
                 onEdit={editing === null ? () => setEditing(i) : undefined}
                 onRemove={editing === null
                   ? () => onChange(setGroups(draft, base, mine.filter((_, j) => j !== i)))
                   : undefined}>
              <GroupSummary group={g} nets={nets} />
            </Row>
          ))}
        {editing === 'new' && <GroupForm nets={nets} onDone={commit} onCancel={() => setEditing(null)} />}
        {!file.length && !mine.length && editing === null && (
          <Text size="xs" c="dimmed">None. Every net uses the rule&apos;s own values.</Text>
        )}
      </Stack>
    </Paper>
  )
}

function GroupTitle({ group }: { group: NetGroup }) {
  return (
    <Text size="xs" ff="monospace" fw={600} style={{ wordBreak: 'break-all' }}>
      {group.netclass ? `netclass ${group.netclass}` : group.match}
    </Text>
  )
}

function GroupSummary({ group, nets }: { group: NetGroup; nets: string[] }) {
  return (
    <Stack gap={0}>
      <Text size="xs">
        {Object.entries(group.params).map(([k, v]) => paramText(k, v)).join(', ') || 'No settings'}
      </Text>
      {!group.netclass && <NetPreview pattern={group.match ?? '*'} nets={nets} />}
    </Stack>
  )
}

function GroupForm({ initial, nets, onDone, onCancel }: {
  initial?: NetGroup
  nets: string[]
  onDone: (g: NetGroup) => void
  onCancel: () => void
}) {
  const [pattern, setPattern] = useState(initial?.match ?? '')
  const [rows, setRows] = useState<[string, string][]>(
    initial ? Object.entries(initial.params).map(([k, v]) => [k, String(v)]) : [[PER_NET_PARAMS[0].key, '']],
  )
  // Errors show from the first Done on, and then follow the typing, so a fixed field clears.
  const [tried, setTried] = useState(false)
  const checked = checkGroup(pattern, rows)
  const errors: GroupErrors | null = tried && 'errors' in checked ? checked.errors : null
  const unused = PER_NET_PARAMS.find((p) => !rows.some(([k]) => k === p.key))

  const done = () => {
    setTried(true)
    if ('group' in checked) onDone(checked.group)
  }

  return (
    <Paper withBorder p="xs" bg="var(--mantine-color-default-hover)">
      <Stack gap={6}>
        <TextInput size="xs" label="Nets matching" placeholder="DDR_DQ*" value={pattern}
                   description="* is any run of characters, ? one, [0-3] a range"
                   error={errors?.pattern} data-autofocus
                   onChange={(e) => setPattern(e.currentTarget.value)} />
        <NetPreview pattern={pattern} nets={nets} />
        {rows.map(([key, text], i) => (
          <Group key={i} gap="xs" wrap="nowrap" align="flex-start">
            <Select size="xs" style={{ flex: 1 }} data={PARAM_OPTIONS} value={key} allowDeselect={false}
                    aria-label="Setting"
                    onChange={(v) => v && setRows(rows.map((r, j) => (j === i ? [v, r[1]] : r)))} />
            <TextInput size="xs" w={80} value={text} inputMode="decimal" aria-label="Value"
                       placeholder={String(PER_NET_PARAMS.find((p) => p.key === key)?.default ?? '')}
                       error={errors?.values[key]}
                       onChange={(e) => {
                         const t = e.currentTarget.value
                         setRows(rows.map((r, j) => (j === i ? [r[0], t] : r)))
                       }} />
            <CloseButton size="sm" mt={4} aria-label="Remove setting"
                         onClick={() => setRows(rows.filter((_, j) => j !== i))} />
          </Group>
        ))}
        {errors?.params && <Text size="xs" c="red">{errors.params}</Text>}
        <Group gap="xs">
          {unused && (
            <Anchor size="xs" component="button" type="button"
                    onClick={() => setRows([...rows, [unused.key, '']])}>Add setting</Anchor>
          )}
          <Box style={{ flex: 1 }} />
          <Button size="compact-xs" variant="default" onClick={onCancel}>Cancel</Button>
          <Button size="compact-xs" onClick={done}>Done</Button>
        </Group>
        {rows.some(([k]) => PER_NET_PARAMS.find((p) => p.key === k)?.rule.id === 'ddr-skew') && (
          <Text size="xs" c="dimmed">
            Length matching applies it to a matched group whose reference net matches.
          </Text>
        )}
      </Stack>
    </Paper>
  )
}

// ---- suppressions ----

const RULE_OPTIONS = [
  { value: '*', label: 'Any check' },
  ...RULES.map((r) => ({ value: r.id, label: r.title })),
]

export function SuppressionsEditor({ base, draft, saved, nets, onChange, onEditing }: EditorProps) {
  const [editing, setEditingState] = useState<number | 'new' | null>(null)
  const setEditing = (v: number | 'new' | null) => { setEditingState(v); onEditing(v !== null) }
  const mine = draft.suppress ?? []
  const file = base.suppress

  const commit = (s: Suppression) => {
    const next = editing === 'new' ? [...mine, s] : mine.map((x, i) => (i === editing ? s : x))
    onChange(setSuppressions(draft, base, next))
    setEditing(null)
  }

  return (
    <Paper withBorder p="xs">
      <Group justify="space-between" mb={4}>
        <Text size="xs" fw={600} tt="uppercase" c="dimmed">Suppressions</Text>
        {editing === null && (
          <Button size="compact-xs" variant="subtle" onClick={() => setEditing('new')}>Add suppression</Button>
        )}
      </Group>
      <Stack gap={6}>
        <Text size="xs" c="dimmed">
          Findings already decided about. They are hidden, counted, and listed under Findings.
        </Text>
        {file.map((s, i) => (
          <Row key={`f${i}`} source={s.source ?? 'file'} unsaved={false} label="suppression"
               title={<SuppressionTitle s={s} />}>
            <SuppressionSummary s={s} nets={nets} />
          </Row>
        ))}
        {mine.map((s, i) => editing === i
          ? <SuppressionForm key={`m${i}`} initial={s} nets={nets} onDone={commit} onCancel={() => setEditing(null)} />
          : (
            <Row key={`m${i}`} source="run" label={`suppression of ${s.rule} on ${s.net}`}
                 title={<SuppressionTitle s={s} />}
                 unsaved={!(saved.suppress ?? []).some((x) => same(x, s))}
                 onEdit={editing === null ? () => setEditing(i) : undefined}
                 onRemove={editing === null
                   ? () => onChange(setSuppressions(draft, base, mine.filter((_, j) => j !== i)))
                   : undefined}>
              <SuppressionSummary s={s} nets={nets} />
            </Row>
          ))}
        {editing === 'new' && <SuppressionForm nets={nets} onDone={commit} onCancel={() => setEditing(null)} />}
        {!file.length && !mine.length && editing === null && (
          <Text size="xs" c="dimmed">None. To hide a finding, use Suppress on it under Findings.</Text>
        )}
      </Stack>
    </Paper>
  )
}

function SuppressionTitle({ s }: { s: Suppression }) {
  return (
    <Text size="xs" fw={600}>
      {s.rule === '*' ? 'Any check' : (RULE_TITLE[s.rule] ?? s.rule)} on{' '}
      <Text span size="xs" ff="monospace" fw={600} style={{ wordBreak: 'break-all' }}>{s.net}</Text>
    </Text>
  )
}

function SuppressionSummary({ s, nets }: { s: Suppression; nets: string[] }) {
  return (
    <Stack gap={0}>
      <Text size="xs" c={s.reason ? 'dimmed' : 'yellow.8'} fs={s.reason ? 'italic' : undefined}>
        {s.reason || 'No reason given'}
      </Text>
      {s.net !== '*' && <NetPreview pattern={s.net} nets={nets} />}
    </Stack>
  )
}

/** The fields of a suppression, shared by the inline form and the finding's Suppress dialog. */
function SuppressionFields({ value, onChange, errors, nets, ruleFixed }: {
  value: Suppression
  onChange: (s: Suppression) => void
  errors: SuppressionErrors | null
  nets: string[]
  ruleFixed?: boolean
}) {
  return (
    <>
      {ruleFixed
        ? <Text size="xs">Check: <Text span size="xs" fw={600}>{RULE_TITLE[value.rule] ?? value.rule}</Text></Text>
        : (
          <Select size="xs" label="Check" data={RULE_OPTIONS} value={value.rule} searchable
                  allowDeselect={false} error={errors?.rule}
                  onChange={(v) => v && onChange({ ...value, rule: v })} />
        )}
      <TextInput size="xs" label="Nets matching" value={value.net} error={errors?.net}
                 description="A net name, or a pattern such as /SWD*"
                 onChange={(e) => onChange({ ...value, net: e.currentTarget.value })} />
      {value.net.trim() === '*'
        ? <Text size="xs" c="yellow.8">Hides every finding of {value.rule === '*' ? 'every check' : 'this check'}.</Text>
        : <NetPreview pattern={value.net} nets={nets} />}
      <Textarea size="xs" label="Reason" autosize minRows={2} value={value.reason} error={errors?.reason}
                placeholder="Why this is fine, for whoever reads it next" data-autofocus
                onChange={(e) => onChange({ ...value, reason: e.currentTarget.value })} />
    </>
  )
}

function SuppressionForm({ initial, nets, onDone, onCancel }: {
  initial?: Suppression
  nets: string[]
  onDone: (s: Suppression) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState<Suppression>(initial ?? { rule: RULES[0].id, net: '', reason: '' })
  const [tried, setTried] = useState(false)
  const checked = checkSuppression(value)
  const errors: SuppressionErrors | null = tried && 'errors' in checked ? checked.errors : null
  const done = () => {
    setTried(true)
    if ('suppression' in checked) onDone(checked.suppression)
  }
  return (
    <Paper withBorder p="xs" bg="var(--mantine-color-default-hover)">
      <Stack gap={6}>
        <SuppressionFields value={value} onChange={setValue} errors={errors} nets={nets} />
        <Group gap="xs" justify="flex-end">
          <Button size="compact-xs" variant="default" onClick={onCancel}>Cancel</Button>
          <Button size="compact-xs" onClick={done}>Done</Button>
        </Group>
      </Stack>
    </Paper>
  )
}

/** Suppress… on a finding: rule and net filled in, a reason asked for. */
export function SuppressFindingModal({ finding, nets, onClose, onAdd }: {
  finding: RuleFinding | null
  nets: string[]
  onClose: () => void
  onAdd: (s: Suppression) => void
}) {
  return (
    <Modal opened={!!finding} onClose={onClose} title="Suppress finding" size="sm" centered>
      {finding && <SuppressFindingForm key={finding.id} finding={finding} nets={nets}
                                       onCancel={onClose} onAdd={onAdd} />}
    </Modal>
  )
}

function SuppressFindingForm({ finding, nets, onCancel, onAdd }: {
  finding: RuleFinding
  nets: string[]
  onCancel: () => void
  onAdd: (s: Suppression) => void
}) {
  const [value, setValue] = useState<Suppression>({ rule: finding.rule, net: finding.net || '*', reason: '' })
  const [tried, setTried] = useState(false)
  const checked = checkSuppression(value)
  const errors: SuppressionErrors | null = tried && 'errors' in checked ? checked.errors : null
  const add = () => {
    setTried(true)
    if ('suppression' in checked) onAdd(checked.suppression)
  }
  return (
    <Stack gap="xs">
      <Text size="sm" fw={500}>{finding.title}</Text>
      <SuppressionFields value={value} onChange={setValue} errors={errors} nets={nets} ruleFixed />
      <Text size="xs" c="dimmed">It is added under Checks, and applies when you save and re-analyse.</Text>
      <Group gap="xs" justify="flex-end">
        <Button size="xs" variant="default" onClick={onCancel}>Cancel</Button>
        <Button size="xs" onClick={add}>Add suppression</Button>
      </Group>
    </Stack>
  )
}

