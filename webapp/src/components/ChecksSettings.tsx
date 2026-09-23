/**
 * Which checks run on this board, and with which thresholds.
 *
 * Until this existed the only way to change a threshold was an emi.rules.yaml zipped up with
 * the board. The file still wins over nothing and loses to nothing it did not already lose
 * to: edits here are the worker's "run" layer, on top of it, applied by analysing the board
 * again. Every value says where it came from, and the export writes the result as a rules
 * file to commit beside the board, which gives the same analysis without this app.
 */

import { useMemo, useState } from 'react'
import {
  Accordion, Alert, Badge, Box, Button, Center, CloseButton, Group, Paper, Select, Stack,
  Switch, Text, TextInput, Tooltip,
} from '@mantine/core'
import type { RulesDoc } from '../lib/boardTypes'
import {
  BOARD_SETTINGS, RULES, SEVERITIES, SOURCE_LABEL, defaultSnapshot, editBoard, editRule,
  overlay, paramLabel, parseSetting, rulesFileText, sameLayer, type AppLayer, type Severity,
  type SettingSource, type Sourced,
} from '../lib/rulesSettings'
import { CATEGORY_COLOR, CATEGORY_ORDER } from './ChecksTable'

const BOARD_LABEL: Record<string, { label: string; unit: string; hint?: string }> = {
  max_frequency_hz: { label: 'Top frequency', unit: 'Hz', hint: 'Length checks compare against this.' },
  epsilon_r: { label: 'Permittivity', unit: '', hint: '0 uses the board file.' },
  epsilon_r_at_hz: { label: 'Permittivity at', unit: 'Hz' },
  via_ps: { label: 'Delay per via', unit: 'ps' },
}

/** 1e9 rather than 1000000000, which nobody can count at a glance. */
function fmt(n: number): string {
  return Math.abs(n) >= 1e5 ? n.toExponential().replace('e+', 'e') : String(n)
}

export interface ChecksSettingsProps {
  rules: RulesDoc | null
  /** The app's layer the analysis on screen ran with. */
  saved: AppLayer
  /** Analyse again with this layer. */
  onApply: (layer: AppLayer) => void
  applying: boolean
  /** Why applying is not possible right now, if it is not. */
  applyBlocked?: string
}

export function ChecksSettings({ rules, saved, onApply, applying, applyBlocked }: ChecksSettingsProps) {
  const report = rules?.settings
  const base = useMemo(() => report?.base ?? defaultSnapshot(), [report])
  const [draft, setDraft] = useState<AppLayer>(saved)
  // What is typed, per field, so a half-typed or invalid number stays on screen.
  const [texts, setTexts] = useState<Record<string, string>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const shown = useMemo(() => overlay(base, draft), [base, draft])
  const changed = !sameLayer(draft, saved)
  const invalid = Object.keys(errors).length > 0

  const edit = (field: string, input: string, opts: { positive?: boolean; key?: string },
    commit: (v: number) => AppLayer) => {
    setTexts((t) => ({ ...t, [field]: input }))
    const parsed = parseSetting(input, opts)
    setErrors(({ [field]: _, ...rest }) => ('error' in parsed ? { ...rest, [field]: parsed.error } : rest))
    if ('value' in parsed) setDraft(commit(parsed.value))
  }
  const clear = (field: string, next: AppLayer) => {
    setTexts(({ [field]: _, ...rest }) => rest)
    setErrors(({ [field]: _, ...rest }) => rest)
    setDraft(next)
  }

  const download = () => {
    const blob = new Blob([rulesFileText(shown)], { type: 'text/yaml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'emi.rules.yaml'
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  const groups = CATEGORY_ORDER
    .map((cat) => [cat, RULES.filter((r) => r.category === cat)] as const)
    .concat([['Other', RULES.filter((r) => !CATEGORY_ORDER.includes(r.category))] as const])
    .filter(([, rs]) => rs.length > 0)

  const extras = base.groups.length + base.suppress.length

  return (
    <Stack gap="sm">
      <Text size="xs" c="dimmed">
        {report?.file
          ? <>Values from <Text span size="xs" ff="monospace">{report.file}</Text> are marked. </>
          : report?.file_error
            ? 'The rules file with this board could not be read, so defaults apply. '
            : 'No rules file came with this board. '}
        Changes here apply when the board is analysed again.
      </Text>

      {!report && (
        <Alert color="blue" variant="light" p="xs">
          <Text size="xs">
            This analysis predates settings in the app, so a rules file it used is not shown.
            Analyse again to see where each value comes from.
          </Text>
        </Alert>
      )}

      <Group gap="xs">
        <Tooltip label={applyBlocked} disabled={!applyBlocked} withArrow>
          <Button size="xs" onClick={() => onApply(draft)} loading={applying}
                  disabled={!changed || invalid || !!applyBlocked}>
            Save and re-analyse
          </Button>
        </Tooltip>
        {changed && (
          <Button size="xs" variant="default"
                  onClick={() => { setDraft(saved); setTexts({}); setErrors({}) }}>
            Discard
          </Button>
        )}
        <Tooltip label="A rules file with these settings, to commit next to the board" withArrow>
          <Button size="xs" variant="light" onClick={download} disabled={invalid}>
            Export emi.rules.yaml
          </Button>
        </Tooltip>
      </Group>

      {extras > 0 && (
        <Text size="xs" c="dimmed">
          {base.groups.length > 0 && `${base.groups.length} net group${base.groups.length === 1 ? '' : 's'}`}
          {base.groups.length > 0 && base.suppress.length > 0 && ' and '}
          {base.suppress.length > 0 && `${base.suppress.length} suppression${base.suppress.length === 1 ? '' : 's'}`}
          {' '}from the rules file also apply. Edit them in the file; the export keeps them.
        </Text>
      )}

      <Paper withBorder p="xs">
        <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={4}>Board</Text>
        <Stack gap={6}>
          {BOARD_SETTINGS.map((b) => {
            const field = `board.${b.key}`
            const v = shown.board[b.key] ?? { value: b.default, source: 'default' as const }
            const meta = BOARD_LABEL[b.key] ?? { label: paramLabel(b.key).label, unit: paramLabel(b.key).unit }
            return (
              <ValueRow key={b.key} label={meta.label} unit={meta.unit} hint={meta.hint}
                        text={texts[field] ?? fmt(v.value)} error={errors[field]} value={v}
                        onChange={(t) => edit(field, t, { positive: b.positive, key: b.key },
                          (n) => editBoard(draft, base, b.key, n))}
                        onReset={() => clear(field, editBoard(draft, base, b.key, null))} />
            )
          })}
        </Stack>
      </Paper>

      {groups.map(([cat, rs]) => (
        <Stack key={cat} gap={4}>
          <Badge size="xs" variant="dot" color={CATEGORY_COLOR[cat] ?? 'gray'} tt="none"
                 style={{ alignSelf: 'flex-start' }}>{cat}</Badge>
          <Accordion multiple variant="contained" chevronPosition="left">
            {rs.map((r) => {
              const s = shown.rules[r.id]
              if (!s) return null
              const baseSeverity = base.rules[r.id]?.severity ?? ''
              return (
                <Accordion.Item key={r.id} value={r.id}>
                  <Center>
                    <Accordion.Control py={4}>
                      <Group gap={6} wrap="nowrap">
                        <Text size="sm" fw={500} c={s.enabled ? undefined : 'dimmed'}
                              td={s.enabled ? undefined : 'line-through'} style={{ flex: 1 }}>
                          {r.title}
                        </Text>
                        {s.severity && <Badge size="xs" variant="light" tt="none">{s.severity}</Badge>}
                        {s.enabled_source !== 'default' && <SourceBadge source={s.enabled_source} />}
                      </Group>
                    </Accordion.Control>
                    <Tooltip label={s.enabled ? 'Runs on this board' : 'Off for this board'} withArrow>
                      <Switch size="xs" mx="xs" checked={s.enabled} aria-label={`Run ${r.title}`}
                              onChange={(e) => setDraft(editRule(draft, base, r.id,
                                { enabled: e.currentTarget.checked }))} />
                    </Tooltip>
                  </Center>
                  <Accordion.Panel>
                    <Stack gap={6}>
                      <Text size="xs" c="dimmed">{r.about}</Text>
                      <Group gap="xs" wrap="nowrap" align="center">
                        <Text size="xs" style={{ flex: 1 }}>Severity</Text>
                        <Select
                          size="xs" w={104} allowDeselect={false}
                          value={s.severity || ''}
                          data={[
                            ...(baseSeverity ? [] : [{ value: '', label: 'As found' }]),
                            ...SEVERITIES.map((v) => ({ value: v, label: v })),
                          ]}
                          onChange={(v) => setDraft(editRule(draft, base, r.id, {
                            severity: !v || v === baseSeverity ? null : (v as Severity),
                          }))}
                        />
                        <SourceTag source={s.severity_source} />
                      </Group>
                      {r.params.map((p) => {
                        const field = `${r.id}.${p.key}`
                        const v = s.params[p.key] ?? { value: p.default, source: 'default' as const }
                        const { label, unit } = paramLabel(p.key)
                        return (
                          <ValueRow key={p.key} label={label} unit={unit}
                                    text={texts[field] ?? fmt(v.value)} error={errors[field]} value={v}
                                    onChange={(t) => edit(field, t, {},
                                      (n) => editRule(draft, base, r.id, { param: [p.key, n] }))}
                                    onReset={() => clear(field,
                                      editRule(draft, base, r.id, { param: [p.key, null] }))} />
                        )
                      })}
                      {r.params.length === 0 && (
                        <Text size="xs" c="dimmed">No thresholds to set.</Text>
                      )}
                    </Stack>
                  </Accordion.Panel>
                </Accordion.Item>
              )
            })}
          </Accordion>
        </Stack>
      ))}
    </Stack>
  )
}

function ValueRow({ label, unit, hint, text, error, value, onChange, onReset }: {
  label: string
  unit: string
  hint?: string
  text: string
  error?: string
  value: Sourced
  onChange: (text: string) => void
  onReset: () => void
}) {
  return (
    <Group gap="xs" wrap="nowrap" align="flex-start">
      <Box style={{ flex: 1, minWidth: 0 }} pt={4}>
        <Text size="xs">{label}{unit && <Text span size="xs" c="dimmed"> ({unit})</Text>}</Text>
        {hint && <Text size="xs" c="dimmed">{hint}</Text>}
      </Box>
      <TextInput size="xs" w={104} value={text} error={error} inputMode="decimal"
                 aria-label={label}
                 onChange={(e) => onChange(e.currentTarget.value)}
                 rightSection={value.source === 'run' || error
                   ? <CloseButton size="xs" aria-label={`Reset ${label}`} onClick={onReset} />
                   : undefined} />
      <SourceTag source={value.source} />
    </Group>
  )
}

/** Where a value came from, in a fixed-width slot so the inputs line up. */
function SourceTag({ source }: { source: SettingSource }) {
  return (
    <Box w={58} pt={4} style={{ flex: 'none' }}>
      {source === 'default'
        ? <Text size="xs" c="dimmed">default</Text>
        : <SourceBadge source={source} />}
    </Box>
  )
}

function SourceBadge({ source }: { source: SettingSource }) {
  return (
    <Badge size="xs" variant="light" tt="none" color={source === 'run' ? 'blue' : 'grape'}>
      {SOURCE_LABEL[source]}
    </Badge>
  )
}
