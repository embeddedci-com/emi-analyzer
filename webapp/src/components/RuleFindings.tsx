/**
 * Findings from the fast rules tier.
 *
 * Every row is clickable and zooms the viewer to the spot. That is the whole point: a
 * finding the user cannot locate is a complaint, not a diagnosis.
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Accordion, Alert, Anchor, Badge, Box, Collapse, Group, Stack, Text,
} from '@mantine/core'
import { Link } from 'react-router'
import { useEmiBase } from '../host'
import type { RuleFinding, RulesDoc } from '../lib/boardTypes'
import { findingAction, groupFindings } from '../lib/findings'
import { SEVERITY_MARKER_HEX } from '../lib/markers'
import catalogue from '../lib/ruleCatalogue.json'
import { SOURCE_LABEL, type Suppression } from '../lib/rulesSettings'
import { SuppressFindingModal } from './ChecksOverrides'

export interface RuleFindingsProps {
  rules: RulesDoc | null
  onFocus: (finding: RuleFinding) => void
  /** Highlight the finding's net in the viewer as well as zooming to it. */
  onSelectNet?: (net: string | null) => void
  /**
   * Open the ESD simulation on this finding's line. Offered on esd-protection findings, where
   * "the clamp is 19 mm away" is exactly the sentence a simulation turns into volts.
   */
  onSimulate?: (finding: RuleFinding) => void
  /**
   * Select a finding's net on the board in KiCad. Only supplied inside the KiCad plugin,
   * where these pages sit beside the PCB Editor; everywhere else the buttons are absent.
   */
  onShowInKiCad?: (nets: string[]) => void
  /**
   * Suppress a finding: rule and net filled in, a reason asked for. The suppression goes to
   * the Checks view's draft, to be saved with everything else there.
   */
  onSuppress?: (s: Suppression) => void
  /** The board's net names, for the Suppress dialog's pattern preview. */
  nets?: string[]
  /** Each finding's number, the one its marker on the board carries. */
  numbers?: Map<string, number>
  /** The finding picked on the board or in the list: opened, scrolled to and outlined. */
  selectedId?: string | null
}

// The same colors as the markers on the board, so a number reads the same in both places.
// Warnings are yellow, not orange: orange is the copper color, and orange markers on orange
// traces were the hardest thing on the board to see.
const SEVERITY_COLOR: Record<string, string> = {
  critical: 'red',
  warning: 'yellow',
  info: 'gray',
}

/** Rules whose findings are lines a discharge can be simulated on. */
const SIMULATABLE = new Set(['esd-protection'])

/** Plain-language name for each rule, so the group headings read as findings, not slugs. */
// Rule names come from the catalogue the worker exports, so a new check is named here the
// moment it exists rather than showing its id until somebody remembers this list.
const RULE_LABEL: Record<string, string> = Object.fromEntries(
  (catalogue as { id: string; title: string }[]).map((r) => [r.id, r.title]),
)

export function RuleFindings({
  rules, onFocus, onSelectNet, onSimulate, onShowInKiCad, onSuppress, nets = [], numbers,
  selectedId = null,
}: RuleFindingsProps) {
  const base = useEmiBase()
  const [filter, setFilter] = useState('all')
  const [suppressing, setSuppressing] = useState<RuleFinding | null>(null)

  // Rules with a critical finding sort first; the panel should open on the worst thing. The
  // order is the one the board's marker numbers come from (lib/findings).
  const grouped = useMemo(() => {
    if (!rules) return []
    return groupFindings(rules.findings.filter((f) => filter === 'all' || f.severity === filter))
  }, [rules, filter])

  // Which rule is open. Controlled, so a marker clicked on the board can open its rule.
  const [open, setOpen] = useState<string | null>(null)
  const firstRule = grouped[0]?.[0] ?? null
  const shownOpen = open ?? firstRule
  const selected = rules?.findings.find((f) => f.id === selectedId) ?? null
  useEffect(() => {
    if (!selected) return
    setOpen(selected.rule)
    if (filter !== 'all' && filter !== selected.severity) setFilter('all')
    // After the rule's panel has opened, so the row has a place to scroll to.
    const t = window.setTimeout(() => {
      document.querySelector(`[data-finding-id="${CSS.escape(selected.id)}"]`)
        ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }, 220)
    return () => window.clearTimeout(t)
    // Only a new selection scrolls; a filter change on its own must not jump the list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id])

  if (!rules) {
    return (
      <Text size="sm" c="dimmed">
        No checks have run yet. Re-analyse the board from the Board tab.
      </Text>
    )
  }

  const { critical, warning, info } = rules.summary

  const topMhz = (rules.summary.assumed_max_frequency_hz / 1e6).toFixed(0)

  if (rules.findings.length === 0) {
    return (
      <Stack gap="sm">
        <Alert color="green" variant="light" title="No findings">
          Nothing found up to {topMhz} MHz. These are layout checks, not a simulation. Under
          Checks you can raise the top frequency.
        </Alert>
        <SuppressedList rules={rules} />
      </Stack>
    )
  }

  return (
    <Stack gap="sm">
      {/* The counts are the filter: click one to show only those, again to show all. */}
      <Group gap={6}>
        {([['critical', critical, 'red'], ['warning', warning, 'yellow'], ['info', info, 'gray']] as const)
          .filter(([sev, n]) => n > 0 || sev !== 'info')
          .map(([sev, n, color]) => (
            <Badge
              key={sev}
              component="button"
              type="button"
              color={color}
              autoContrast
              variant={filter === sev || (filter === 'all' && n > 0) ? 'filled' : 'light'}
              aria-pressed={filter === sev}
              title={filter === sev ? 'Show all' : `Show only ${sev}`}
              onClick={() => setFilter(filter === sev ? 'all' : sev)}
              style={{ cursor: 'pointer', border: 0, opacity: filter !== 'all' && filter !== sev ? 0.5 : 1 }}
            >
              {n} {sev}
            </Badge>
          ))}
      </Group>

      <Accordion variant="separated" value={shownOpen} onChange={setOpen}
                 chevronPosition="left">
        {grouped.map(([rule, findings]) => (
          <Accordion.Item key={rule} value={rule}>
            <Accordion.Control>
              <Group gap="xs" wrap="nowrap">
                <Text size="sm" fw={500} style={{ flex: 1 }}>
                  {RULE_LABEL[rule] ?? rule}
                </Text>
                <Badge size="sm" variant="light" color={SEVERITY_COLOR[findings[0].severity]}
                       style={{ flex: 'none' }}>
                  {findings.length}
                </Badge>
              </Group>
            </Accordion.Control>
            <Accordion.Panel>
              <Stack gap="xs">
                {findings.map((f) => (
                  <FindingRow
                    key={f.id}
                    finding={f}
                    number={numbers?.get(f.id)}
                    selected={f.id === selectedId}
                    onClick={() => {
                      onFocus(f)
                      if (f.net) onSelectNet?.(f.net)
                    }}
                    onSimulate={onSimulate && SIMULATABLE.has(f.rule) && f.net ? () => onSimulate(f) : undefined}
                    onShowInKiCad={onShowInKiCad && f.net ? () => onShowInKiCad([f.net!]) : undefined}
                    onSuppress={onSuppress ? () => setSuppressing(f) : undefined}
                  />
                ))}
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>
        ))}
      </Accordion>

      {/* Settings that were not applied are in the notes above the findings (AnalysisNotes),
          with everything else the worker said about the board. */}
      <SuppressedList rules={rules} />

      <SuppressFindingModal
        finding={suppressing}
        nets={nets}
        onClose={() => setSuppressing(null)}
        onAdd={(s) => { setSuppressing(null); onSuppress?.(s) }}
      />

      <Text size="xs" c="dimmed">
        Layout checks up to {topMhz} MHz. They show where to look, not how much the board
        radiates.{' '}
        <Anchor component={Link} to={`${base}/limitations`} size="xs">Limitations</Anchor>
      </Text>
    </Stack>
  )
}

/** What suppressions hid, and why: a hidden finding should still be findable. */
function SuppressedList({ rules }: { rules: RulesDoc }) {
  const hidden = rules.suppressed_findings ?? []
  if (!hidden.length) return null
  return (
    <Accordion variant="contained" chevronPosition="left">
      <Accordion.Item value="suppressed">
        <Accordion.Control>
          <Text size="xs" c="dimmed">{hidden.length} suppressed</Text>
        </Accordion.Control>
        <Accordion.Panel>
          <Stack gap={6}>
            {hidden.map((h, i) => (
              <Stack key={i} gap={0}>
                <Text size="xs" fw={500}>{h.title}</Text>
                <Text size="xs" c="dimmed">
                  {h.reason || 'No reason given'}
                  {h.source && ` (${SOURCE_LABEL[h.source] ?? h.source})`}
                </Text>
              </Stack>
            ))}
          </Stack>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  )
}

function FindingRow({
  finding, number, selected, onClick, onSimulate, onShowInKiCad, onSuppress,
}: {
  finding: RuleFinding
  number?: number
  selected?: boolean
  onClick: () => void
  onSimulate?: () => void
  onShowInKiCad?: () => void
  onSuppress?: () => void
}) {
  const [why, setWhy] = useState(false)
  // JSON from the worker carries absent coordinates as null, not undefined, so this has
  // to be a loose check. An info-severity finding ("41 nets exceed lambda/20") has no
  // single place on the board and legitimately has none.
  const locatable = finding.x != null && finding.y != null
  const color = SEVERITY_MARKER_HEX[finding.severity] ?? SEVERITY_MARKER_HEX.info
  const link = (label: string, run: () => void, dimmed = false) => (
    <Anchor size="xs" component="button" type="button" c={dimmed ? 'dimmed' : undefined}
            style={{ whiteSpace: 'nowrap' }}
            onClick={(e) => {
              // The row itself zooms; these do their own thing instead.
              e.stopPropagation()
              run()
            }}>
      {label}
    </Anchor>
  )
  return (
    <Stack
      gap={2}
      p="xs"
      data-finding-id={finding.id}
      onClick={locatable ? onClick : undefined}
      style={{
        cursor: locatable ? 'pointer' : 'default',
        borderLeft: `2px solid ${color}`,
        borderRadius: 2,
        background: 'var(--mantine-color-default-hover)',
        outline: selected ? `2px solid ${color}` : undefined,
        outlineOffset: -1,
      }}
      role={locatable ? 'button' : undefined}
      tabIndex={locatable ? 0 : undefined}
      onKeyDown={(e) => {
        if (locatable && e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault()
          onClick()
        }
      }}
    >
      <Group gap={6} wrap="nowrap" align="center">
        {/* The number its marker on the board carries, in the marker's color. */}
        <Box
          component="span"
          style={{
            flex: 'none', minWidth: 18, height: 18, padding: '0 4px', borderRadius: 9,
            background: color, color: '#fff', font: '600 11px/18px sans-serif',
            textAlign: 'center',
          }}
        >
          {number ?? ''}
        </Box>
        <Text size="sm" fw={500} lineClamp={1} title={finding.title} style={{ flex: 1 }}>
          {finding.title}
        </Text>
      </Group>

      <Text size="xs" pl={24}>{findingAction(finding)}</Text>

      {/* Wraps: with the links beside the coordinates and a layer badge, one line overflowed the panel. */}
      <Group gap={6} pl={24} wrap="wrap" align="center" style={{ rowGap: 2 }}>
        {finding.layer && (
          <Badge size="xs" variant="outline" color="gray" style={{ flex: 'none' }}>
            {finding.layer}
          </Badge>
        )}
        {locatable && (
          <Text size="10px" c="dimmed" ff="monospace" style={{ whiteSpace: 'nowrap' }}>
            {finding.x!.toFixed(1)}, {finding.y!.toFixed(1)} mm
          </Text>
        )}
        <Group gap={8} ml="auto" wrap="nowrap">
          {onShowInKiCad && link('Show in KiCad', onShowInKiCad)}
          {onSimulate && link('Simulate discharge', onSimulate)}
          {link(why ? 'Hide why' : 'Why', () => setWhy((v) => !v), true)}
        </Group>
      </Group>

      <Collapse expanded={why}>
        <Stack gap={4} pl={24} pt={2} style={{ cursor: 'default' }}
               onClick={(e) => e.stopPropagation()}>
          <Text size="xs" c="dimmed">{finding.detail}</Text>
          {onSuppress && <Group>{link('Suppress\u2026', onSuppress, true)}</Group>}
        </Stack>
      </Collapse>
    </Stack>
  )
}
