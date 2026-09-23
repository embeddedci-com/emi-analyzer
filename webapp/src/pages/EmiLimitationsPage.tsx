/**
 * What this tool can and cannot tell you.
 *
 * Every entry is in the one table, including the most important one: comparisons between
 * two versions of the same board are reliable, absolute field strengths are not. That entry
 * is listed first because the other limitations are easier to read with it in mind.
 *
 * Keep it free of API calls: a host may prerender it statically for search engines.
 */

import { Anchor, Badge, Card, Container, List, Stack, Table, Text, Title } from '@mantine/core'
import { Link } from 'react-router'
import type { EmiDeployment } from '../routes'
import { resolveHostCopy, useEmiBase, type EmiHostCopy } from '../host'
import { EXPERIMENTAL, EXPERIMENTAL_TITLES } from '../components/Experimental'

interface Limitation {
  what: string
  kind: 'hard' | 'band' | 'setup' | 'method' | 'privacy' | 'legal'
  why: string
}

const LOCAL_PRIVACY: Limitation = {
  what: 'Board data stays on this computer',
  kind: 'privacy',
  why:
    'Uploaded board files, results and components are stored in the app\'s data folder and ' +
    'processed by a worker container on this computer. Nothing is sent to any other ' +
    'service; the only network access is Docker pulling the worker image.',
}

// Stands in for the privacy entry, which depends on where the tool runs.
const PRIVACY_PLACEHOLDER: Limitation = { what: '', kind: 'privacy', why: '' }

const LIMITATIONS: Limitation[] = [
  {
    what: 'Full-wave solving is experimental and off by default',
    kind: 'method',
    why:
      'Full-wave solving works — a board solves end to end and the field maps come back — but ' +
      'none of it has been checked against a real board, and no run has been made at the ' +
      'record length a radiated result needs (30 MHz means 100 ns of simulated time, millions ' +
      'of timesteps). Until that is done, full-wave solving and everything built on it ' +
      '(hotspot maps, the far field, cable emissions and the compliance estimate) is switched ' +
      'off unless a server enables it with EMI_EXPERIMENTAL=full-wave. The geometric checks, ' +
      'the ESD simulation and the cable budget do not use it.',
  },
  {
    what: 'Small-part solves are experimental and off by default',
    kind: 'method',
    why:
      'A small-part solve cuts one net out with its planes and solves it in minutes, with no ' +
      'far field. It has been checked against closed forms for lines and vias, for how much ' +
      'the size of the cut changes the answer, and on coupons from real boards, but not ' +
      'against a measurement. It is off unless a server enables it with ' +
      'EMI_EXPERIMENTAL=small-part-solve. Neighbouring nets are left out of the cut, so it ' +
      'does not show coupling into them.',
  },
  {
    what: 'Only comparisons between board versions are reliable',
    kind: 'method',
    why:
      'Comparing two versions of the same board gives a reliable result, for example that ' +
      'one layout radiates 8 dB less than another at 480 MHz, and which trace causes the ' +
      'difference. Modelling errors largely cancel between two runs of the same design, so ' +
      'a comparison remains valid despite the other limitations on this page. Absolute ' +
      'field strengths do not. Treat any single value as an order-of-magnitude estimate, ' +
      'not as a measurement.',
  },
  {
    what: 'Whole-board solves are not supported',
    kind: 'hard',
    why:
      'A 100 × 80 mm board meshed at 25 µm has about 1.9 billion cells. That requires about ' +
      '138 GB of memory and roughly ten weeks of computation. A graded mesh reduces this to ' +
      'about two weeks. Analysis therefore runs on a region that you select. This is a ' +
      'limit of the FDTD method, not of this implementation.',
  },
  {
    what: 'Cables are modelled separately from the board, and only as a budget',
    kind: 'hard',
    why:
      'Below about 300 MHz, most emission failures are caused by common-mode current on an ' +
      'attached cable harness, which acts as the antenna. A full-wave solve contains only the ' +
      'board and cannot show that. The Cables tab covers it separately: each declared cable is ' +
      'modelled as a wire over a ground plane, and the result is a budget — how much ' +
      'common-mode current that cable can carry before it reaches a limit — not a prediction ' +
      'of what it does carry. A connector you do not declare is not modelled at all.',
  },
  {
    what: 'The useful frequency range is about 100 MHz to 3 GHz',
    kind: 'band',
    why:
      'To resolve the lowest frequency of interest, the simulation must run for 3 / f_min. ' +
      'The timestep is set by the smallest mesh cell, which is around 0.05 ps for PCB ' +
      'features. Resolving 30 MHz therefore needs several hundred thousand additional ' +
      'timesteps, and emissions in that range are usually dominated by cable effects.',
  },
  {
    what: 'Results are relative unless you supply a driver spectrum',
    kind: 'setup',
    why:
      'openEMS simulates passive structures. It calculates how the layout responds to an ' +
      'excitation, but it has no information about the signals on your board, such as a ' +
      'buck converter switching 3 A in 4 ns. Absolute field strengths require a defined ' +
      'source: a declared edge rate and amplitude, or a measured switching edge.',
  },
  {
    what: 'Components are not modelled',
    kind: 'setup',
    why:
      'ICs, connectors and decoupling capacitors are represented only by their copper ' +
      'geometry, unless lumped models are provided. Package parasitics dominate above ' +
      'about 1 GHz, so results in that range describe the bare board and not the assembled ' +
      'board.',
  },
  {
    what: 'Stackup material values must be correct',
    kind: 'setup',
    why:
      'Permittivity and loss tangent depend on frequency and on the fabricator. If your ' +
      'board file does not include them, FR-4 values are used and marked "assumed" in the ' +
      'stackup panel. An incorrect εr shifts every resonance in the result.',
  },
  {
    what: 'Mesh resolution limits accuracy',
    kind: 'method',
    why:
      'FDTD uses a rectilinear grid, so curved and angled copper is approximated in steps. ' +
      'A finer mesh reduces this error. Each halving of the cell size costs roughly four ' +
      'times as much computation.',
  },
  {
    what: 'Immunity checks are layout rules, not an immunity test',
    kind: 'method',
    why:
      'The ESD, shield and reset-line checks look for the layout mistakes behind most ' +
      'IEC 61000-4-2 and 61000-4-4 failures, from how parts are placed and connected. They ' +
      'inject nothing, so they cannot say what discharge or burst level a board survives; ' +
      'the ESD simulation below estimates one line at a time. Parts are recognised by reference designator, value and footprint: a ' +
      'protection device the tool does not recognise is reported as missing, and a connector ' +
      'counts as I/O by how close it sits to the board edge.',
  },
  {
    what: 'ESD simulation voltages are estimates; compare them',
    kind: 'method',
    why:
      'The simulation models the trace, the clamp and its ground via from the layout, but an ' +
      "IC's internal protection is not published, so every pin is the same generic CMOS " +
      'input. Absolute volts at a pin are therefore estimates. What holds is the comparison ' +
      'between variants of the same line — the clamp where it is against the clamp at the ' +
      'connector. Planes are ideal (no ground bounce or coupling to neighbouring traces), only ' +
      'contact discharge is simulated, and it is injected at a pin, which is the worst case: a ' +
      'test discharges to a metal shell. Uploaded SPICE models replace the datasheet model only ' +
      'after the worker has checked them.',
  },
  PRIVACY_PLACEHOLDER,
  {
    what: 'Results are not a compliance prediction',
    kind: 'legal',
    why:
      'Where an FCC Part 15B limit line is shown, it is a reference for comparing versions of ' +
      'your own board. It is not a pass or fail result, it is not a pre-scan, and it does not ' +
      'replace testing at a test house. CISPR 32 limits are not included.',
  },
]

const KIND_COLOR: Record<Limitation['kind'], string> = {
  hard: 'red',
  band: 'orange',
  setup: 'yellow',
  method: 'yellow',
  privacy: 'blue',
  legal: 'red',
}

export interface EmiLimitationsPageProps {
  deployment?: EmiDeployment
  /** What a hosted copy says about its server; only its privacy entry is used here. */
  host?: EmiHostCopy
}

export function EmiLimitationsPage({ deployment = 'hosted', host }: EmiLimitationsPageProps = {}) {
  const base = useEmiBase()
  const hosted: Limitation = { ...resolveHostCopy(host).privacy, kind: 'privacy' }
  const limitations = LIMITATIONS.map((l) =>
    l === PRIVACY_PLACEHOLDER ? (deployment === 'local' ? LOCAL_PRIVACY : hosted) : l,
  )
  return (
    <Container size="md" py="xl">
      <Stack gap="lg">
        <div>
          <Title order={1} size="h2">
            EMI Analyzer limitations
          </Title>
          <Text c="dimmed" mt="xs">
            The EMI Analyzer works from the geometry of your board: it checks the layout,
            simulates an ESD discharge, budgets each cable, and — where full-wave simulation is
            enabled — runs an electromagnetic field solver over a region you choose. It gives
            more detail than a rule-based design checker, but it does not replace measurements
            at a test house. This page describes what the results can
            and cannot be used for.
          </Text>
        </div>

        <Card withBorder padding="md">
          <Title order={2} size="h4" mb="sm">
            Geometric checks
          </Title>
          <Text size="sm" c="dimmed">
            The findings shown a few seconds after upload are <strong>geometric</strong>.
            They look for known causes of emissions: a trace crossing a gap in its reference
            plane, a layer change with no nearby return via, an unterminated via stub, or
            copper close to the board edge. These findings show where to look. They do not
            run a simulation and cannot tell you how much your board radiates. A board with
            no findings has not passed any test.
          </Text>
        </Card>

        <div>
          <Title order={2} size="h4" mb="sm">
            Limitations
          </Title>
          <Table.ScrollContainer minWidth={560}>
            <Table verticalSpacing="sm" horizontalSpacing="md" withTableBorder>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th style={{ width: '32%' }}>Limitation</Table.Th>
                  <Table.Th style={{ width: 90 }}>Kind</Table.Th>
                  <Table.Th>Explanation</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {limitations.map((l) => (
                  <Table.Tr key={l.what}>
                    <Table.Td>
                      <Text size="sm" fw={500}>
                        {l.what}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Badge size="xs" color={KIND_COLOR[l.kind]} variant="light">
                        {l.kind}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" c="dimmed">
                        {l.why}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </div>

        {/* From the same list the badges read, so this cannot drift from what the tool shows. */}
        <div>
          <Title order={2} size="h4" mb="sm">
            Experimental results
          </Title>
          <Text size="sm" c="dimmed" mb="sm">
            A result marked experimental works, but the model behind it has not been checked
            against the case in front of you. What is unproven in each:
          </Text>
          <Stack gap="sm">
            {(Object.keys(EXPERIMENTAL) as (keyof typeof EXPERIMENTAL)[]).map((k) => (
              <div key={k}>
                <Text size="sm" fw={500}>{EXPERIMENTAL_TITLES[k]}</Text>
                <Text size="sm" c="dimmed">{EXPERIMENTAL[k]}</Text>
              </div>
            ))}
          </Stack>
        </div>

        <Card withBorder padding="md">
          <Title order={2} size="h4" mb="sm">
            How the numbers are calculated
          </Title>
          <List size="sm" spacing="xs">
            <List.Item>
              The solver is{' '}
              <Anchor href="https://www.openems.de/" target="_blank" rel="noreferrer">
                openEMS
              </Anchor>
              , a free, open-source finite-difference time-domain (FDTD) field solver. Its
              physics are used unmodified.
            </List.Item>
            <List.Item>
              Memory use is 72 bytes per mesh cell: six field and operator arrays with three
              components each, in single precision.
            </List.Item>
            <List.Item>
              Run time is cells × timesteps ÷ throughput. Throughput on a typical worker is
              150 to 250 million cell updates per second. FDTD is limited by memory
              bandwidth, so adding CPU cores has a smaller effect than you might expect.
            </List.Item>
            <List.Item>
              The timestep is set by the Courant limit, which depends on the smallest cell
              along <em>any</em> axis. On a PCB this is almost always the vertical mesh
              through a thin dielectric layer, not the trace width. For this reason,
              refining the vertical mesh costs about four times more than it appears to.
            </List.Item>
          </List>
        </Card>

        <Text size="sm" c="dimmed">
          The published limits a prediction is measured against are on{' '}
          <Anchor component={Link} to={`${base}/limits`}>the emission limits page</Anchor>.
          Back to <Anchor component={Link} to={base || '/'}>the EMI Analyzer</Anchor>.
        </Text>
      </Stack>
    </Container>
  )
}
