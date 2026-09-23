/**
 * Findings from the fast rules tier.
 *
 * Every row is clickable and zooms the viewer to the spot. That is the whole point: a
 * finding the user cannot locate is a complaint, not a diagnosis.
 */

import { useMemo, useState } from 'react'
import {
  Accordion, Alert, Anchor, Badge, Group, SegmentedControl, Stack, Text, ThemeIcon,
} from '@mantine/core'
import { Link } from 'react-router'
import { useEmiBase } from '../host'
import type { RuleFinding, RulesDoc } from '../lib/boardTypes'
import catalogue from '../lib/ruleCatalogue.json'

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
}

const SEVERITY_COLOR: Record<string, string> = {
  critical: 'red',
  warning: 'yellow',
  info: 'blue',
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
  rules, onFocus, onSelectNet, onSimulate, onShowInKiCad,
}: RuleFindingsProps) {
  const base = useEmiBase()
  const [filter, setFilter] = useState('all')

  const grouped = useMemo(() => {
    if (!rules) return []
    const wanted = rules.findings.filter((f) => filter === 'all' || f.severity === filter)
    const byRule = new Map<string, RuleFinding[]>()
    for (const f of wanted) {
      const list = byRule.get(f.rule)
      if (list) list.push(f)
      else byRule.set(f.rule, [f])
    }
    // Rules with a critical finding sort first; the panel should open on the worst thing.
    return [...byRule.entries()].sort((a, b) => {
      const worst = (list: RuleFinding[]) =>
        list.some((f) => f.severity === 'critical') ? 0
          : list.some((f) => f.severity === 'warning') ? 1 : 2
      return worst(a[1]) - worst(b[1])
    })
  }, [rules, filter])

  if (!rules) {
    return (
      <Text size="sm" c="dimmed">
        No checks have run yet. Re-analyse the board from the Board tab.
      </Text>
    )
  }

  const { critical, warning, info } = rules.summary

  if (rules.findings.length === 0) {
    return (
      <Alert color="green" variant="light" title="No findings">
        The layout checks found nothing at{' '}
        {(rules.summary.assumed_max_frequency_hz / 1e6).toFixed(0)} MHz. That is not a clean
        bill of health: they are not a simulation. Under Checks you can see which ran and
        raise the top frequency.
      </Alert>
    )
  }

  return (
    <Stack gap="sm">
      <Group gap="xs" justify="space-between">
        <Group gap={6}>
          <Badge color="red" variant={critical ? 'filled' : 'light'}>{critical} critical</Badge>
          <Badge color="yellow" variant={warning ? 'filled' : 'light'}>{warning} warning</Badge>
          {info > 0 && <Badge color="blue" variant="light">{info} info</Badge>}
        </Group>
        <SegmentedControl
          size="xs"
          value={filter}
          onChange={setFilter}
          data={[
            { label: 'All', value: 'all' },
            { label: 'Critical', value: 'critical' },
            { label: 'Warning', value: 'warning' },
          ]}
        />
      </Group>

      <Text size="xs" c="dimmed">
        Evaluated against a top frequency of{' '}
        {(rules.summary.assumed_max_frequency_hz / 1e6).toFixed(0)} MHz. These are geometric
        checks: they say where to look, not how much your board radiates.
      </Text>

      <Accordion variant="separated" defaultValue={grouped[0]?.[0]} chevronPosition="left">
        {grouped.map(([rule, findings]) => (
          <Accordion.Item key={rule} value={rule}>
            <Accordion.Control>
              <Group gap="xs" wrap="nowrap">
                <Text size="sm" fw={500} style={{ flex: 1 }}>
                  {RULE_LABEL[rule] ?? rule}
                </Text>
                <Badge size="sm" variant="light" color={SEVERITY_COLOR[findings[0].severity]}>
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
                    onClick={() => {
                      onFocus(f)
                      if (f.net) onSelectNet?.(f.net)
                    }}
                    onSimulate={onSimulate && SIMULATABLE.has(f.rule) && f.net ? () => onSimulate(f) : undefined}
                    onShowInKiCad={onShowInKiCad && f.net ? () => onShowInKiCad([f.net!]) : undefined}
                  />
                ))}
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>
        ))}
      </Accordion>

      {/* Suppressed findings and settings that were not applied are in the notes above the
          findings (AnalysisNotes), with everything else the worker said about the board. */}

      {rules.findings.length > 0 && (
        <Text size="xs" c="dimmed">
          Modelled estimate from board geometry. See{' '}
          <Anchor component={Link} to={`${base}/limitations`} size="xs">the limitations</Anchor>
          {' '}before acting on these.
        </Text>
      )}
    </Stack>
  )
}

function FindingRow({
  finding, onClick, onSimulate, onShowInKiCad,
}: {
  finding: RuleFinding
  onClick: () => void
  onSimulate?: () => void
  onShowInKiCad?: () => void
}) {
  // JSON from the worker carries absent coordinates as null, not undefined, so this has
  // to be a loose check. An info-severity finding ("41 nets exceed lambda/20") has no
  // single place on the board and legitimately has none.
  const locatable = finding.x != null && finding.y != null
  return (
    <Stack
      gap={2}
      p="xs"
      onClick={locatable ? onClick : undefined}
      style={{
        cursor: locatable ? 'pointer' : 'default',
        borderLeft: `2px solid var(--mantine-color-${SEVERITY_COLOR[finding.severity]}-6)`,
        borderRadius: 2,
        background: 'var(--mantine-color-default-hover)',
      }}
      role={locatable ? 'button' : undefined}
      tabIndex={locatable ? 0 : undefined}
      onKeyDown={(e) => {
        if (locatable && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault()
          onClick()
        }
      }}
    >
      {/* The layer badge used to sit beside the title, which squeezed a two-line title
          into a narrow column and left the badge floating against the first line. Title
          gets the full width; the badge belongs with the other metadata, underneath. */}
      <Group gap="xs" wrap="nowrap" align="flex-start">
        <ThemeIcon
          size={8}
          radius="xl"
          color={SEVERITY_COLOR[finding.severity]}
          mt={6}
          style={{ flex: 'none' }}
        >
          <span />
        </ThemeIcon>
        <Text size="sm" fw={500} style={{ flex: 1 }}>
          {finding.title}
        </Text>
      </Group>

      <Text size="xs" c="dimmed" pl={20}>
        {finding.detail}
      </Text>

      {(finding.layer || locatable || onSimulate || onShowInKiCad) && (
        <Group gap={6} pl={20} wrap="nowrap" align="center">
          {finding.layer && (
            <Badge size="xs" variant="outline" color="gray" style={{ flex: 'none' }}>
              {finding.layer}
            </Badge>
          )}
          {locatable && (
            <Text size="10px" c="dimmed" ff="monospace" style={{ whiteSpace: 'nowrap' }}>
              {finding.x!.toFixed(1)}, {finding.y!.toFixed(1)} mm — click to zoom
            </Text>
          )}
          {onShowInKiCad && (
            <Anchor
              size="xs"
              component="button"
              type="button"
              ml="auto"
              style={{ whiteSpace: 'nowrap' }}
              onClick={(e) => {
                // The row zooms this viewer; this points the PCB Editor at the same net.
                e.stopPropagation()
                onShowInKiCad()
              }}
            >
              Show in KiCad
            </Anchor>
          )}
          {onSimulate && (
            <Anchor
              size="xs"
              component="button"
              type="button"
              ml={onShowInKiCad ? undefined : 'auto'}
              style={{ whiteSpace: 'nowrap' }}
              onClick={(e) => {
                // The row itself zooms; this opens the simulation instead of doing both.
                e.stopPropagation()
                onSimulate()
              }}
            >
              Simulate discharge
            </Anchor>
          )}
        </Group>
      )}
    </Stack>
  )
}
