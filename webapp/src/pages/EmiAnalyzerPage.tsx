/**
 * Tools -> EMI Analyzer: project list and new-board upload.
 */

import { useCallback, useRef, useState } from 'react'
import {
  Alert, Anchor, Badge, Button, Card, Container, FileButton, Group, List, Loader,
  Progress, SimpleGrid, Stack, Text, TextInput, Title, Tooltip,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router'
import { EmiApi, hashFile } from '../lib/emiApi'
import { ChecksTable } from '../components/ChecksTable'
import type { EmiDeployment } from '../routes'

export interface EmiAnalyzerPageProps {
  api: EmiApi
  deployment?: EmiDeployment
}

export function EmiAnalyzerPage({ api, deployment = 'hosted' }: EmiAnalyzerPageProps) {
  const local = deployment === 'local'
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [uploadPct, setUploadPct] = useState<number | null>(null)
  const [stage, setStage] = useState<string | null>(null)
  const [reused, setReused] = useState<string | null>(null)
  const resetFile = useRef<() => void>(null)

  const projects = useQuery({ queryKey: ['emi', 'projects'], queryFn: api.listProjects })
  const me = useQuery({ queryKey: ['emi', 'whoami'], queryFn: api.whoami, staleTime: 60_000 })
  const features = useQuery({ queryKey: ['emi', 'features'], queryFn: api.features, staleTime: 5 * 60_000 })
  // Undecided until the server answers, so nothing promises a solver that may be off.
  const fullWave = features.data?.full_wave === true
  const workers = useQuery({
    queryKey: ['emi', 'workers'],
    queryFn: api.listWorkers,
    refetchInterval: 15_000,
  })

  const create = useMutation({
    mutationFn: async (file: File) => {
      // Hash before anything else. If this organisation has already uploaded these exact
      // bytes, the board is here — parsed, with its findings and any solves — and the
      // right answer is to open it rather than send it again.
      setStage('Checking whether this board is already here…')
      const sha = await hashFile(file)
      if (sha) {
        const hit = await api.lookupBoard(sha)
        if (hit.found && hit.project && hit.parsed) {
          return { project: hit.project, existing: true }
        }
      }

      // The worker decides the real format by looking inside the upload, so this is a
      // label rather than a switch — a zip of Gerbers still ingests correctly if it lands
      // here marked "kicad".
      setStage(null)
      const project = await api.createProject(
        name.trim() || file.name.replace(/\.(kicad_pcb|zip)$/i, ''),
        /\.zip$/i.test(file.name) ? 'gerber' : 'kicad',
      )
      setUploadPct(0)
      await api.uploadBoard(project.id, file, (f) => setUploadPct(f * 100), sha)
      return { project, existing: false }
    },
    onSuccess: ({ project, existing }) => {
      setUploadPct(null)
      setStage(null)
      setName('')
      resetFile.current?.()
      setReused(existing ? project.name : null)
      qc.invalidateQueries({ queryKey: ['emi', 'projects'] })
      navigate(`/tools/emi/${project.id}`)
    },
    onError: () => {
      setUploadPct(null)
      setStage(null)
    },
  })

  const onPick = useCallback(
    (file: File | null) => {
      if (file) create.mutate(file)
    },
    [create],
  )

  const online = workers.data?.online_count ?? 0

  return (
    <Container size="lg" py="lg">
      <Stack gap="lg">
        <Group justify="space-between" align="flex-start">
          <div>
            <Title order={2}>EMI Analyzer</Title>
            <Text c="dimmed" size="sm" mt={4}>
              Upload a KiCad board to see its copper, its stackup and a set of geometric EMI and
              EMC checks: where it radiates, what it conducts out through its cables, and where
              ESD and fast transients get in. An ESD discharge is simulated in ngspice and each
              cable gets a common-mode budget from an antenna model.
              {fullWave ? (
                <>
                  {' '}Full-wave analysis of a selected region runs on{' '}
                  <Anchor href="https://www.openems.de/" target="_blank" rel="noreferrer" inherit>
                    openEMS
                  </Anchor>
                  , a real field solver, not an approximation.
                </>
              ) : (
                <>
                  {' '}Full-wave simulation with{' '}
                  <Anchor href="https://www.openems.de/" target="_blank" rel="noreferrer" inherit>
                    openEMS
                  </Anchor>
                  {' '}is experimental and switched off in this build.
                </>
              )}
            </Text>
          </div>
          {local && (
            <Badge
              variant="light"
              color="green"
              size="lg"
              title="Boards, results and components are stored on this computer and never uploaded anywhere."
            >
              On this computer
            </Badge>
          )}
          {!local && me.isSuccess && (
            <Badge
              variant="light"
              color={me.data.anonymous ? 'gray' : 'green'}
              size="lg"
              title={
                me.data.anonymous
                  ? 'Your boards are private to this browser. Sign in to EmbeddedCI to keep them with your account and share them with your organisation.'
                  : `Boards are filed under organisation ${me.data.organization_id}`
              }
            >
              {me.data.anonymous ? 'Not signed in' : (me.data.login || me.data.user_id)}
            </Badge>
          )}
        </Group>

        <Card withBorder padding="md">
          <Stack gap="sm">
            <Text fw={500}>New board</Text>
            <Group align="flex-end" gap="sm">
              <TextInput
                label="Project name"
                placeholder="taken from the filename if left blank"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
                style={{ flex: 1 }}
                disabled={create.isPending}
              />
              <FileButton
                resetRef={resetFile}
                onChange={onPick}
                accept=".kicad_pcb,.zip,application/zip"
              >
                {(props) => (
                  <Button {...props} loading={create.isPending}>
                    Choose a board file
                  </Button>
                )}
              </FileButton>
            </Group>

            {stage && (
              <Group gap="xs">
                <Loader size="xs" />
                <Text size="xs" c="dimmed">{stage}</Text>
              </Group>
            )}

            {reused && (
              <Alert color="blue" variant="light" title="Opened the copy you already had">
                This board was uploaded before, so nothing was sent again — you are looking
                at {reused}, with its existing checks and results.
              </Alert>
            )}

            {uploadPct !== null && (
              <Stack gap={4}>
                <Progress value={uploadPct} size="sm" animated />
                <Text size="xs" c="dimmed">
                  {uploadPct < 100
                    ? `Uploading — ${uploadPct.toFixed(0)}%`
                    : 'Uploaded. Waiting for the analysis to start…'}
                </Text>
              </Stack>
            )}

            {create.isError && (
              <Alert color="red" variant="light" title="That board could not be opened">
                <Text size="xs">
                  {(create.error as Error).message}
                </Text>
                <Text size="xs" mt={4}>
                  A KiCad board, a zipped KiCad project, or a zip of Gerbers with the drill file
                  and an IPC-D-356 netlist. Anything else is refused before it is read.
                </Text>
              </Alert>
            )}

            <Text size="xs" c="dimmed">
              A <Text span ff="monospace" size="xs">.kicad_pcb</Text>, a zipped KiCad
              project, or a zip of your Gerber output. Gerbers must include the drill file
              and an <Text span ff="monospace" size="xs">IPC-D-356</Text> netlist — they
              carry no net information on their own, so without one there is no way to tell
              which copper is which signal.
            </Text>
            <Text size="xs" c="dimmed">
              {local
                ? 'Board files are stored on this computer and processed by the worker container running on it — '
                : 'Board files are stored in DigitalOcean Spaces and processed by an EmbeddedCI worker — '}
              <Anchor component={Link} to="/tools/emi/limitations" size="xs">
                what that means
              </Anchor>
              .
            </Text>
          </Stack>
        </Card>

        <div>
          <Group justify="space-between" mb="xs">
            <Text fw={500}>Your boards</Text>
          </Group>

          {projects.isLoading && <Loader size="sm" />}
          {projects.isError && (
            <Alert color="red" variant="light" title="Your boards could not be listed">
              <Text size="xs">{(projects.error as Error).message}</Text>
            </Alert>
          )}
          {projects.isSuccess && projects.data.length === 0 && (
            <Text c="dimmed" size="sm">
              No boards yet — choose a board file above to analyse one.
            </Text>
          )}

          <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="sm">
            {projects.data?.map((p) => (
              <Card
                key={p.id}
                withBorder
                padding="sm"
                component={Link}
                to={`/tools/emi/${p.id}`}
                style={{ textDecoration: 'none' }}
              >
                <Group justify="space-between" wrap="nowrap">
                  <Text fw={500} truncate>
                    {p.name}
                  </Text>
                  <Badge size="xs" variant="light">
                    {p.source_kind}
                  </Badge>
                </Group>
                <Text size="xs" c="dimmed" mt={4}>
                  {new Date(p.created_at).toLocaleDateString()}
                </Text>
              </Card>
            ))}
          </SimpleGrid>
        </div>

        <Card withBorder padding="md">
          <Title order={4} mb="xs">
            How the analysis works
          </Title>
          <Text size="sm" c="dimmed" mb="md">
            {fullWave
              ? 'Four stages, and only the full-wave solve is slow. Nothing here is a rule-of-thumb estimate standing in for a solver.'
              : 'Three stages run in this build, and all of them are quick. The fourth, full-wave simulation, is experimental and switched off.'}
          </Text>
          <List type="ordered" size="sm" spacing="sm">
            <List.Item>
              <Text size="sm" fw={500} span>Ingest.</Text>{' '}
              <Text size="sm" c="dimmed" span>
                A <Text span ff="monospace" size="xs">.kicad_pcb</Text> is parsed straight from
                its s-expression source, so nets, traces, vias, pads and the stackup arrive as
                they were drawn. Gerbers take a longer route: every copper layer is rasterised,
                its connected islands labelled, and the{' '}
                <Text span ff="monospace" size="xs">IPC-D-356</Text> netlist coordinates dropped
                onto them to recover which island is which net. Both paths end at one normalised
                board model, and everything after this point reads only that.
              </Text>
            </List.Item>
            <List.Item>
              <Text size="sm" fw={500} span>Geometric checks (seconds).</Text>{' '}
              {/* Points at the table rather than naming checks here: a second hand-kept list
                  is how this sentence came to describe five checks after there were thirteen. */}
              <Text size="sm" c="dimmed" span>
                Return paths and plane stitching, decoupling, length matching and impedance, and
                the layout details that make a board radiate. Then the other half of an EMC
                test: ESD protection at the connectors, shield and chassis grounding, reset lines
                that a transient can trip, and the power input and switching-regulator layout
                behind conducted emissions. Every check is in the table below. No solver is
                involved, which is why these run on any worker and finish while you wait.
              </Text>
            </List.Item>
            <List.Item>
              <Text size="sm" fw={500} span>ESD simulation (on demand).</Text>{' '}
              <Text size="sm" c="dimmed" span>
                An IEC 61000-4-2 contact discharge, simulated in ngspice on every line that leaves
                the board through an edge connector: the trace, the clamp — from its datasheet or
                a SPICE model you upload — its ground via, and the IC pin. Each line is compared
                with its clamp moved to the connector, and that difference is the number to act on.
              </Text>
            </List.Item>
            <List.Item>
              <Text size="sm" fw={500} span>Full-wave solve (hours).</Text>{' '}
              {features.isSuccess && !features.data.full_wave && (
                <Tooltip
                  multiline
                  w={300}
                  withArrow
                  events={{ hover: true, focus: true, touch: true }}
                  label="Experimental, and turned off on this server. Long openEMS solves are not yet numerically stable, so they are not offered until that is fixed. Everything else on this page works without them."
                >
                  <Badge
                    component="span"
                    size="xs"
                    variant="light"
                    color="yellow"
                    style={{ cursor: 'help', display: 'inline-flex', verticalAlign: 'middle' }}
                  >
                    experimental · off
                  </Badge>
                </Tooltip>
              )}{' '}
              {/* Said here rather than at the top of the page: it is a property of this one
                  stage, not of the tool. Shown only while solving is unavailable, and the
                  badge carries the short form so the reason is discoverable on hover without
                  a bare icon nobody notices. touch is enabled so it opens on a tap too. */}
              {features.data?.full_wave && workers.isSuccess && online === 0 && (
                <Tooltip
                  multiline
                  w={280}
                  withArrow
                  events={{ hover: true, focus: true, touch: true }}
                  label="openEMS solving currently not available. The solving takes a lot of computing power and is not always available on-demand."
                >
                  {/* component="span" + inline-flex so it sits in the sentence after the
                      stage name rather than breaking onto a line of its own. */}
                  <Badge
                    component="span"
                    size="xs"
                    variant="light"
                    color="gray"
                    style={{ cursor: 'help', display: 'inline-flex', verticalAlign: 'middle' }}
                  >
                    currently unavailable
                  </Badge>
                </Tooltip>
              )}{' '}
              <Text size="sm" c="dimmed" span>
                openEMS, an EC-FDTD solver, over a region of interest you select. The worker
                meshes the geometry, excites the nets you nominate, and steps the fields through
                time on a rectilinear grid. Results come back as frequency-domain surface-current
                maps per layer, a near-field-to-far-field radiation pattern, and S-parameters.
                Whole-board solves are not offered: a 100 x 80 mm board meshed at 25 um is around
                1.9 billion cells, which no amount of hardware makes practical.
              </Text>
            </List.Item>
          </List>
          <Text size="xs" c="dimmed" mt="md">
            The memory and runtime arithmetic behind a solve, and what the results can and
            cannot tell you, are on{' '}
            <Anchor component={Link} to="/tools/emi/limitations" size="xs">
              the limitations page
            </Anchor>
            . The published limits those results are read against are on{' '}
            <Anchor component={Link} to="/tools/emi/limits" size="xs">
              the emission limits page
            </Anchor>
            .
          </Text>
        </Card>

        <ChecksTable />

      </Stack>
    </Container>
  )
}
