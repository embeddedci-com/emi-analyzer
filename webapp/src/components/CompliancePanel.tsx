/**
 * The compliance outlook (§16, §17).
 *
 * This is the most quotable screen in the tool, which is what most of its design is about. Three
 * rules it holds to:
 *
 * **No number until the inputs are whole.** When `complete` is false there is no margin in the
 * document at all, and this shows the gap list in its place — as a to-do list, because every gap
 * names the tab that fixes it. Showing a margin with a warning would be worse than showing
 * nothing: the warning is what gets dropped when someone screenshots the number.
 *
 * **Confidence is not a pass probability**, and is labelled uncalibrated until real test results
 * have been recorded against the budget (§17.4). It is stated as what it is — the probability the
 * true margin is positive *under the model's own assumptions* — rather than as a percentage with
 * a tooltip.
 *
 * **The budget shows its own working.** A σ a reader cannot interrogate is worse than no σ, so
 * the terms are listed with their values and the note that they are conservative placeholders.
 */

import { useState } from 'react'
import {
  Alert, Badge, Card, Collapse, Group, List, Progress, Stack, Table, Text, Title, Tooltip,
} from '@mantine/core'
import { driverProvenance, fmtHz, hasMargin, type ComplianceDoc } from '../lib/complianceTypes'
import { ComplianceSpectrum } from './ComplianceSpectrum'
import { EXPERIMENTAL, Experimental } from './Experimental'

const TAB_LABEL: Record<string, string> = {
  drivers: 'Drivers', cables: 'Cables', solve: 'Solve', product: 'Product',
}

const marginColour = (db: number) => (db < 0 ? 'red' : db < 6 ? 'orange' : 'teal')

export function CompliancePanel({ doc }: { doc: ComplianceDoc }) {
  const [showBudget, setShowBudget] = useState(false)
  const scored = hasMargin(doc)
  const margin = doc.margin_db ?? 0
  const anyCable = doc.paths.some((p) => p.kind === 'cable')
  const provenance = driverProvenance(doc)

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-start">
        <Stack gap={0}>
          <Group gap="xs">
            <Title order={5}>Compliance outlook</Title>
            <Experimental why={EXPERIMENTAL.complianceEstimate} />
            {anyCable && <Experimental why={EXPERIMENTAL.cableEmissions} />}
          </Group>
          <Text size="xs" c="dimmed">
            {doc.standard}
            {doc.distance_m ? ` at ${doc.distance_m} m` : ''} · experimental estimate, not a
            pre-compliance test
          </Text>
          {provenance && (
            <Group gap={6} mt={4}>
              <Text size="xs" c="dimmed">Driver: {provenance.name}</Text>
              <Tooltip
                multiline w={300} withArrow
                label={
                  provenance.assumed.length > 0
                    ? `Assumed: ${provenance.assumed.join(', ')}. The least certain value sets ` +
                      `this driver's term in σ.`
                    : "The least certain value sets this driver's term in σ."
                }
              >
                <Badge size="xs" variant="dot" tt="none" style={{ cursor: 'help' }}
                       color={provenance.source === 'assumed' ? 'orange' : 'gray'}>
                  {provenance.source} ±{provenance.sigmaDb} dB
                </Badge>
              </Tooltip>
            </Group>
          )}
        </Stack>
        {scored && (
          <Stack gap={0} align="flex-end">
            <Text size="xl" fw={700} c={marginColour(margin)}>
              {margin >= 0 ? '+' : ''}{margin.toFixed(1)} dB
            </Text>
            <Text size="xs" c="dimmed">
              margin at {fmtHz(doc.worst!.frequency_hz)}
            </Text>
          </Stack>
        )}
      </Group>

      {doc.no_paths && (
        <Alert color="gray" variant="light" title="Nothing radiating is modelled yet">
          <Text size="xs">{doc.no_paths}</Text>
        </Alert>
      )}

      {!doc.complete && doc.gaps.length > 0 && (
        <Alert color="yellow" variant="light" title="Incomplete inputs">
          <Text size="xs" mb={6}>
            There is no margin to show yet. Each of these changes the answer rather than
            refining it, so the estimate waits until they are settled.
          </Text>
          <List size="xs" spacing={4}>
            {doc.gaps.map((g) => (
              <List.Item key={g.key}>
                <Group gap={6} wrap="nowrap" align="flex-start">
                  <Badge size="xs" variant="light" tt="none">
                    {TAB_LABEL[g.fixed_on] ?? g.fixed_on}
                  </Badge>
                  <Text size="xs">{g.message}</Text>
                </Group>
              </List.Item>
            ))}
          </List>
        </Alert>
      )}

      {doc.spectrum.length > 1 && <ComplianceSpectrum doc={doc} />}

      {doc.notes && doc.notes.length > 0 && (
        <List size="xs" spacing={2} c="dimmed">
          {doc.notes.map((n) => <List.Item key={n}>{n}</List.Item>)}
        </List>
      )}

      {scored && (
        <>
          <Card withBorder padding="sm" radius="md">
            <Group justify="space-between" mb={6}>
              <Text size="xs" fw={600} tt="uppercase" c="dimmed">Model confidence</Text>
              <Tooltip
                multiline w={330} withArrow
                label={
                  'The probability the true margin is positive under the model\'s own ' +
                  'uncertainty budget. It says how much weight this margin can bear — it is ' +
                  'not a probability of passing a test, and it stays uncalibrated until lab ' +
                  'results have been recorded against it.'
                }
              >
                <Badge size="xs" variant="light" color="gray" tt="none"
                       style={{ cursor: 'help' }}>uncalibrated</Badge>
              </Tooltip>
            </Group>
            <Progress value={(doc.confidence_uncalibrated ?? 0) * 100} size="lg" radius="sm"
                      color={marginColour(margin)} />
            <Group justify="space-between" mt={6}>
              <Text size="sm" fw={600}>{Math.round((doc.confidence_uncalibrated ?? 0) * 100)} %</Text>
              <Text size="xs" c="dimmed">
                σ {doc.sigma_db?.toFixed(2)} dB · 80 % range{' '}
                {doc.range_80_db?.[0].toFixed(1)} … {doc.range_80_db?.[1].toFixed(1)} dB
              </Text>
            </Group>

            {/* A button, so the keyboard and a screen reader can reach it too. */}
            <Text component="button" type="button" size="xs" c="dimmed" mt={8}
                  aria-expanded={showBudget}
                  style={{ cursor: 'pointer', background: 'none', border: 0, padding: 0 }}
                  onClick={() => setShowBudget((v) => !v)}>
              {showBudget ? '▾' : '▸'} where σ comes from
            </Text>
            <Collapse expanded={showBudget}>
              <Table verticalSpacing={2} fz="xs" mt={6}>
                <Table.Tbody>
                  {Object.entries(doc.sigma_terms ?? {}).map(([name, v]) => (
                    <Table.Tr key={name}>
                      <Table.Td><Text size="xs">{name}</Text></Table.Td>
                      <Table.Td ta="right">
                        <Text size="xs" ff="monospace">{v.toFixed(2)} dB</Text>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
              <Text size="xs" c="dimmed" mt={4}>{doc.uncertainty_note}</Text>
            </Collapse>
          </Card>

          {doc.contributions && doc.contributions.length > 0 && (
            <div>
              <Group gap="xs" mb={4}>
                <Text size="xs" fw={600} tt="uppercase" c="dimmed">
                  What is driving it at {fmtHz(doc.worst!.frequency_hz)}
                </Text>
                {doc.shares_indicative && (
                  <Tooltip
                    multiline w={320} withArrow
                    label={
                      'One driver reaches the antenna by more than one path here. Those add in ' +
                      'amplitude rather than in power, so the shares no longer add to the ' +
                      'whole and are indicative of the ordering rather than exact.'
                    }
                  >
                    <Badge size="xs" variant="light" color="gray" tt="none"
                           style={{ cursor: 'help' }}>shares indicative</Badge>
                  </Tooltip>
                )}
              </Group>
              <Table verticalSpacing={2} fz="xs">
                <Table.Tbody>
                  {doc.contributions.map((c) => (
                    <Table.Tr key={c.label}>
                      <Table.Td><Text size="xs">{c.label}</Text></Table.Td>
                      <Table.Td w={90}>
                        <Progress value={c.share * 100} size="sm" radius="sm" />
                      </Table.Td>
                      <Table.Td ta="right" w={70}>
                        <Text size="xs" ff="monospace" c="dimmed">
                          {Math.round(c.share * 100)} %
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </div>
          )}

          {doc.near_misses && doc.near_misses.length > 0 && (
            <Alert color="orange" variant="light" title="Other frequencies close to the limit">
              <Text size="xs" mb={4}>
                Confidence above is evaluated at the worst frequency alone, so it is optimistic
                while these sit within one σ of it.
              </Text>
              <Text size="xs" ff="monospace">
                {doc.near_misses.map((m) =>
                  `${fmtHz(m.frequency_hz)} ${m.margin_db >= 0 ? '+' : ''}${m.margin_db.toFixed(1)} dB`,
                ).join(' · ')}
              </Text>
            </Alert>
          )}

          {doc.recommendations && (
            <div>
              <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={4}>
                What to change — {doc.recommendations.path_label}
              </Text>
              {doc.recommendations.general_only ? (
                <Alert color="gray" variant="light">
                  <Text size="xs" fw={600} mb={4}>
                    General guidance — nothing specific found on this board
                  </Text>
                  <List size="xs" spacing={4}>
                    {doc.recommendations.general.map((g) => <List.Item key={g}>{g}</List.Item>)}
                  </List>
                </Alert>
              ) : (
                <Stack gap={6}>
                  {doc.recommendations.items.map((r) => (
                    <Card key={r.finding_id} withBorder padding="xs" radius="sm">
                      <Group gap={6} mb={2}>
                        <Badge size="xs" variant="light"
                               color={r.severity === 'critical' ? 'red'
                                 : r.severity === 'warning' ? 'orange' : 'gray'}>
                          {r.severity}
                        </Badge>
                        <Text size="xs" fw={600}>{r.title}</Text>
                        {r.net && <Text size="xs" c="dimmed" ff="monospace">{r.net}</Text>}
                      </Group>
                      <Text size="xs" c="dimmed">{r.detail}</Text>
                    </Card>
                  ))}
                </Stack>
              )}
            </div>
          )}
        </>
      )}
    </Stack>
  )
}
