/**
 * The published emission limits, as tables (§15.3).
 *
 * Every number here comes from `worker/emi_worker/compliance/limits/fcc.json`, the same file
 * the worker reads when it computes a margin. Nothing on this page is retyped: a limit a user
 * reads here and a limit a report is measured against are the same bytes, and there is no
 * version of this page that can drift from the tool.
 *
 * Statically prerendered for search engines (embeddedci-server's scripts/prerender.mjs), so
 * keep it free of API calls.
 */

import {
  Alert,
  Anchor,
  Badge,
  Card,
  Container,
  Group,
  Stack,
  Table,
  Text,
  Title,
} from '@mantine/core'
import { Link } from 'react-router'
import { limitLine, SCAN_RANGE, STANDARDS, type Standard } from '../lib/limits'

const MHZ = (hz: number) => {
  if (hz >= 1e9) return `${hz / 1e9} GHz`
  if (hz >= 1e6) return `${hz / 1e6} MHz`
  return `${hz / 1e3} kHz`
}

/** Cornell's LII mirror: the CFR itself, free, and stable enough to link. */
const CLAUSE_LINKS: Record<string, string> = {
  '47 CFR 15.107': 'https://www.law.cornell.edu/cfr/text/47/15.107',
  '47 CFR 15.109': 'https://www.law.cornell.edu/cfr/text/47/15.109',
  '47 CFR 15.33': 'https://www.law.cornell.edu/cfr/text/47/15.33',
}

function clauseHref(clause: string): string | undefined {
  const base = clause.replace(/\([a-z]\)$/, '').trim()
  return CLAUSE_LINKS[base]
}

function StandardCard({ std }: { std: Standard }) {
  const href = clauseHref(std.clause)
  return (
    <Card withBorder padding="md">
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <Stack gap={2}>
            <Title order={4}>{std.name}</Title>
            <Text size="xs" c="dimmed">
              {href ? (
                <Anchor href={href} target="_blank" rel="noreferrer">
                  {std.clause}
                </Anchor>
              ) : (
                std.clause
              )}
              {' · '}
              {std.detector} detector
              {std.distance_m ? ` · measured at ${std.distance_m} m` : ''}
            </Text>
          </Stack>
          <Badge variant="light" color={std.verified === 'standard text' ? 'teal' : 'yellow'}>
            {std.verified}
          </Badge>
        </Group>

        <Table withTableBorder withColumnBorders striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Frequency</Table.Th>
              <Table.Th>Limit ({std.unit})</Table.Th>
              {std.segments.some((s) => s.microvolts_per_m !== undefined) && (
                <Table.Th>As published</Table.Th>
              )}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {std.segments.map((seg) => (
              <Table.Tr key={`${seg.f_lo_hz}-${seg.f_hi_hz}`}>
                <Table.Td ff="monospace">
                  {MHZ(seg.f_lo_hz)} – {MHZ(seg.f_hi_hz)}
                </Table.Td>
                <Table.Td ff="monospace">
                  {seg.level_db_hi !== undefined && seg.level_db_hi !== null
                    ? `${seg.level_db} → ${seg.level_db_hi}`
                    : seg.level_db}
                  {seg.detector && ` ${seg.detector}`}
                  {typeof seg.peak_level_db === 'number' && `, ${seg.peak_level_db} peak`}
                </Table.Td>
                {std.segments.some((s) => s.microvolts_per_m !== undefined) && (
                  <Table.Td ff="monospace">
                    {seg.microvolts_per_m !== undefined ? `${seg.microvolts_per_m} µV/m` : '—'}
                  </Table.Td>
                )}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>

        {std.segments.some((s) => s.level_db_hi !== undefined && s.level_db_hi !== null) && (
          <Text size="xs" c="dimmed">
            A segment written <Text span ff="monospace">66 → 56</Text> falls linearly with the{' '}
            <em>logarithm</em> of frequency. At the geometric mean of its endpoints the limit
            is therefore the arithmetic mean of the two levels — 61 dBµV at 0.274 MHz, not at
            0.325.
          </Text>
        )}

        <Text size="xs" c="dimmed">
          Verified against {std.source}.
        </Text>
      </Stack>
    </Card>
  )
}

export function EmiLimitsPage() {
  const radiated = STANDARDS.filter((s) => s.scan === 'radiated')
  const conducted = STANDARDS.filter((s) => s.scan === 'conducted')
  // One worked example of the band-edge rule, taken from the live table rather than typed.
  const edge = limitLine('fcc-15b-radiated-3m').filter((p) => p.frequency_hz === 88e6)

  return (
    <Container size="md" py="xl">
      <Stack gap="lg">
        <Stack gap={4}>
          <Title order={2}>Emission limits</Title>
          <Text c="dimmed">
            The published limits this tool reads results against. Every number below comes from
            the same file the analyzer uses, so there is no version of this page that can
            disagree with a result.
          </Text>
        </Stack>

        <Alert color="gray" variant="light" title="This is reference material, not a test">
          <Text size="sm">
            Where a result can be compared with them — a cable's common-mode budget, or a
            full-wave prediction where that is enabled — the analyzer shows the margin and how
            confident it is. It is not a full pre-compliance test — see{' '}
            <Anchor component={Link} to="/tools/emi/limitations">
              what it cannot say
            </Anchor>
            . Only a laboratory measurement decides compliance.
          </Text>
        </Alert>

        <Title order={3}>Radiated</Title>
        {radiated.map((std) => (
          <StandardCard key={std.id} std={std} />
        ))}

        <Title order={3}>Conducted, at the mains port</Title>
        {conducted.map((std) => (
          <StandardCard key={std.id} std={std} />
        ))}

        <Card withBorder padding="md">
          <Stack gap="xs">
            <Title order={4}>At a band edge, the tighter limit applies</Title>
            <Text size="sm">
              A frequency that falls exactly on the boundary between two segments belongs to
              both, and the stricter of the two governs. At{' '}
              <Text span ff="monospace">
                88 MHz
              </Text>{' '}
              exactly, FCC Class B is{' '}
              <Text span ff="monospace">
                {Math.min(...edge.map((p) => p.level_db))} dBµV/m
              </Text>{' '}
              (100 µV/m) and not{' '}
              <Text span ff="monospace">
                {Math.max(...edge.map((p) => p.level_db))}
              </Text>
              . A clock harmonic landing on a band edge is a common way to be surprised by a
              test report.
            </Text>
          </Stack>
        </Card>

        <Card withBorder padding="md">
          <Stack gap="xs">
            <Title order={4}>How far up the scan has to go</Title>
            <Text size="xs" c="dimmed">
              {(() => {
                const href = clauseHref(SCAN_RANGE.clause)
                return href ? (
                  <Anchor href={href} target="_blank" rel="noreferrer">
                    {SCAN_RANGE.clause}
                  </Anchor>
                ) : (
                  SCAN_RANGE.clause
                )
              })()}
            </Text>
            <Table withTableBorder withColumnBorders striped>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Highest frequency generated or used</Table.Th>
                  <Table.Th>Measure up to</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {SCAN_RANGE.rules.map((rule) => (
                  <Table.Tr key={rule.highest_below_hz}>
                    <Table.Td ff="monospace">below {MHZ(rule.highest_below_hz)}</Table.Td>
                    <Table.Td ff="monospace">{MHZ(rule.scan_to_hz)}</Table.Td>
                  </Table.Tr>
                ))}
                <Table.Tr>
                  <Table.Td ff="monospace">
                    {MHZ(SCAN_RANGE.rules[SCAN_RANGE.rules.length - 1].highest_below_hz)} and above
                  </Table.Td>
                  <Table.Td ff="monospace">
                    the {SCAN_RANGE.above.harmonic}th harmonic, or{' '}
                    {MHZ(SCAN_RANGE.above.cap_hz)}, whichever is lower
                  </Table.Td>
                </Table.Tr>
              </Table.Tbody>
            </Table>
            <Text size="xs" c="dimmed">
              Verified against {SCAN_RANGE.source}.
            </Text>
          </Stack>
        </Card>

        <Card withBorder padding="md">
          <Stack gap="xs">
            <Title order={4}>Reading the units</Title>
            <Text size="sm">
              Radiated limits are field strengths in µV/m, quoted here in dBµV/m as{' '}
              <Text span ff="monospace">
                20·log₁₀(µV/m)
              </Text>
              . 100 µV/m is 40 dBµV/m. Conducted limits are voltages at the mains port in
              dBµV, measured across a 50 µH / 50 Ω line impedance stabilisation network.
            </Text>
            <Text size="sm">
              Quasi-peak and average are different detectors, not different limits: a source
              has to pass both where both are given, and a broadband or low-repetition-rate
              source reads very differently on the two.
            </Text>
          </Stack>
        </Card>

        <Text size="xs" c="dimmed">
          CISPR 32 / EN 55032 limits are not included yet. That standard is sold rather than
          freely published; when it is added, its limit values will come from cross-checked
          public sources, without reproducing its text.
        </Text>
      </Stack>
    </Container>
  )
}
