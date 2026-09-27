/**
 * Tools -> EMI Analyzer: project list and new-board upload.
 *
 * A first visit has one job: get a board in. The upload and the sample sit at the top, the
 * boards already here under them, and how the analysis works is folded away below.
 */

import { useCallback, useRef, useState } from 'react'
import {
  Accordion, Alert, Anchor, Badge, Box, Button, Card, Container, FileButton, Group, List,
  Loader, Progress, SimpleGrid, Stack, Text, TextInput, Title, Tooltip,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router'
import { EmiApi, hashFile } from '../lib/emiApi'
import { SAMPLE_BOARD_NAME, sampleBoardFile } from '../lib/sampleBoard'
import { ChecksTable } from '../components/ChecksTable'
import type { EmiDeployment } from '../routes'
import { resolveHostCopy, useEmiBase, type EmiHostCopy } from '../host'

export interface EmiAnalyzerPageProps {
  api: EmiApi
  deployment?: EmiDeployment
  /** What a hosted copy says about its server; see {@link EmiHostCopy}. */
  host?: EmiHostCopy
}

const ACCEPT = /\.(kicad_pcb|zip)$/i

export function EmiAnalyzerPage({ api, deployment = 'hosted', host }: EmiAnalyzerPageProps) {
  const local = deployment === 'local'
  const copy = resolveHostCopy(host)
  const base = useEmiBase()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [naming, setNaming] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  const [uploadPct, setUploadPct] = useState<number | null>(null)
  const [stage, setStage] = useState<string | null>(null)
  const resetFile = useRef<() => void>(null)

  const projects = useQuery({ queryKey: ['emi', 'projects'], queryFn: api.listProjects })
  const me = useQuery({ queryKey: ['emi', 'whoami'], queryFn: api.whoami, staleTime: 60_000 })
  const features = useQuery({ queryKey: ['emi', 'features'], queryFn: api.features, staleTime: 5 * 60_000 })
  const workers = useQuery({
    queryKey: ['emi', 'workers'],
    queryFn: api.listWorkers,
    refetchInterval: 15_000,
  })

  const create = useMutation({
    // `as` names the project regardless of the name field: the sample board is always the
    // sample board, whatever was typed before it was chosen.
    mutationFn: async ({ file, as }: { file: File; as?: string }) => {
      // Hash before anything else. If this organisation has already uploaded these exact
      // bytes, the board is here — parsed, with its findings and any solves — and the
      // right answer is to open it rather than send it again.
      setStage('Checking for a copy already here…')
      const sha = await hashFile(file)
      if (sha) {
        const hit = await api.lookupBoard(sha)
        if (hit.found && hit.project && hit.parsed) {
          return { project: hit.project, existing: true, boardId: hit.board?.id }
        }
      }

      // The worker decides the real format by looking inside the upload, so this is a
      // label rather than a switch — a zip of Gerbers still ingests correctly if it lands
      // here marked "kicad".
      setStage(null)
      const project = await api.createProject(
        as ?? (name.trim() || file.name.replace(ACCEPT, '')),
        /\.zip$/i.test(file.name) ? 'gerber' : 'kicad',
      )
      setUploadPct(0)
      try {
        await api.uploadBoard(project.id, file, (f) => setUploadPct(f * 100), sha)
      } catch (err) {
        // Without this a failed upload left an empty project in the list, one per attempt.
        // Best effort: the upload's error is the one worth showing.
        await api.deleteProject(project.id).catch(() => undefined)
        throw err
      }
      return { project, existing: false, boardId: undefined }
    },
    onSuccess: ({ project, existing, boardId }) => {
      setUploadPct(null)
      setStage(null)
      setName('')
      setNaming(false)
      resetFile.current?.()
      qc.invalidateQueries({ queryKey: ['emi', 'projects'] })
      // Opened at the version these bytes are, which need not be the newest one in that project.
      // "Reused" is said on the board's page: it is gone the moment it is navigated away from.
      navigate(`${base}/${project.id}${boardId ? `?version=${boardId}` : ''}`,
               { state: existing ? { reused: true } : undefined })
    },
    onError: () => {
      setUploadPct(null)
      setStage(null)
    },
  })

  const onPick = useCallback(
    (file: File | null) => {
      if (!file) return
      if (!ACCEPT.test(file.name)) {
        setRefused(file.name)
        return
      }
      setRefused(null)
      create.mutate({ file })
    },
    [create],
  )

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    if (create.isPending) return
    onPick(e.dataTransfer.files?.[0] ?? null)
  }

  const online = workers.data?.online_count ?? 0

  return (
    <Container size="lg" py="lg">
      <Stack gap="lg">
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <div>
            <Title order={2}>EMI Analyzer</Title>
            <Text c="dimmed" size="sm" mt={4}>
              Upload a KiCad board and see where it may fail EMI and EMC tests, and what to
              change.
              {!local && copy.pluginDocsUrl && (
                <>
                  {' '}Also{' '}
                  <Anchor component={Link} to={copy.pluginDocsUrl} inherit>
                    a KiCad plugin
                  </Anchor>
                  .
                </>
              )}
            </Text>
          </div>
          {local && (
            <Badge
              variant="light"
              color="green"
              size="lg"
              style={{ flex: 'none' }}
              title="Boards and results stay on this computer."
            >
              On this computer
            </Badge>
          )}
          {!local && me.isSuccess && (
            <Badge
              variant="light"
              color={me.data.anonymous ? 'gray' : 'green'}
              size="lg"
              style={{ flex: 'none' }}
              title={
                me.data.anonymous
                  ? copy.signIn
                  : `Boards are filed under organisation ${me.data.organization_id}`
              }
            >
              {me.data.anonymous ? 'Not signed in' : (me.data.login || me.data.user_id)}
            </Badge>
          )}
        </Group>

        <Card withBorder padding="md">
          <Stack gap="sm">
            {/* The whole card area is a drop target; the button is for those who prefer a dialog. */}
            <Box
              onDragEnter={(e) => { e.preventDefault(); setDragging(true) }}
              onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy' }}
              onDragLeave={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false)
              }}
              onDrop={onDrop}
              p="lg"
              style={{
                border: `2px dashed var(--mantine-color-${dragging ? 'blue-6' : 'default-border'})`,
                borderRadius: 8,
                background: dragging ? 'var(--mantine-color-blue-light)' : undefined,
                transition: 'background 120ms, border-color 120ms',
              }}
            >
              <Stack gap="sm" align="center">
                <Text fw={500} ta="center">Drop a board file here</Text>
                <Group gap="sm" justify="center">
                  <FileButton
                    resetRef={resetFile}
                    onChange={onPick}
                    accept=".kicad_pcb,.zip,application/zip"
                  >
                    {(props) => (
                      <Button {...props} loading={create.isPending}>
                        Choose a file
                      </Button>
                    )}
                  </FileButton>
                  {/* The same upload as a user's own file, from a copy bundled with the app,
                      so a first look needs neither a board nor a network. Uploaded once: after
                      that the hash finds it and this opens the one already here. */}
                  <Button variant="light" disabled={create.isPending}
                          onClick={() => create.mutate({ file: sampleBoardFile(), as: SAMPLE_BOARD_NAME })}>
                    Try the sample board
                  </Button>
                </Group>
                {/* One line that says what is accepted, or how the upload is going: the same
                    height either way, so nothing below moves. */}
                <Box mih={34} w="100%" maw={560}>
                  {stage ? (
                    <Group gap="xs" justify="center">
                      <Loader size="xs" />
                      <Text size="xs" c="dimmed">{stage}</Text>
                    </Group>
                  ) : uploadPct !== null ? (
                    <Group gap="xs" wrap="nowrap">
                      <Progress value={uploadPct} size="sm" animated style={{ flex: 1 }} />
                      <Text size="xs" c="dimmed" w={120}>
                        {uploadPct < 100 ? `Uploading ${uploadPct.toFixed(0)}%` : 'Opening…'}
                      </Text>
                    </Group>
                  ) : (
                    <Text size="xs" c="dimmed" ta="center">
                      A <Text span ff="monospace" size="xs">.kicad_pcb</Text>, a KiCad project
                      zip, or a Gerber zip with drill file and IPC-D-356 netlist.
                    </Text>
                  )}
                </Box>
              </Stack>
            </Box>

            {naming ? (
              <TextInput
                size="xs"
                label="Board name"
                placeholder="the file name"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
                disabled={create.isPending}
                maw={360}
                data-autofocus
              />
            ) : (
              <Group gap="xs">
                <Anchor component="button" type="button" size="xs" c="dimmed"
                        onClick={() => setNaming(true)}>
                  Name it (optional)
                </Anchor>
              </Group>
            )}

            {!local && workers.isSuccess && online === 0 && (
              <Text size="xs" c="orange">
                No worker is connected. A new board waits until one is.
              </Text>
            )}

            {refused && (
              <Alert color="red" variant="light" p="xs" withCloseButton onClose={() => setRefused(null)}>
                <Text size="xs">{refused} is not a .kicad_pcb or a .zip.</Text>
              </Alert>
            )}
            {create.isError && (
              <Alert color="red" variant="light" title="That board could not be opened">
                <Text size="xs">{(create.error as Error).message}</Text>
              </Alert>
            )}

            <Text size="xs" c="dimmed">
              {local ? 'Files stay on this computer.' : copy.storage}{' '}
              <Anchor component={Link} to={`${base}/limitations`} size="xs">
                Limitations
              </Anchor>
            </Text>
          </Stack>
        </Card>

        <div>
          <Text fw={500} mb="xs">Your boards</Text>

          {projects.isLoading && <Loader size="sm" />}
          {projects.isError && (
            <Alert color="red" variant="light" title="Your boards could not be listed">
              <Text size="xs">{(projects.error as Error).message}</Text>
            </Alert>
          )}
          {projects.isSuccess && projects.data.length === 0 && (
            <Text c="dimmed" size="sm">No boards yet.</Text>
          )}

          <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="sm">
            {projects.data?.map((p) => (
              <Card
                key={p.id}
                withBorder
                padding="sm"
                component={Link}
                to={`${base}/${p.id}`}
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

        <Accordion variant="contained" chevronPosition="left">
          <Accordion.Item value="how">
            <Accordion.Control>
              <Text fw={500}>How it works</Text>
            </Accordion.Control>
            <Accordion.Panel>
              <Stack gap="md">
                {/* One short line per analysis, so each can change without touching the others. */}
                <List size="sm" spacing={6}>
                  <List.Item>
                    <Text size="sm" fw={500} span>Checks (seconds).</Text>{' '}
                    <Text size="sm" c="dimmed" span>
                      Layout checks on every upload. They are listed below.
                    </Text>
                  </List.Item>
                  <List.Item>
                    <Text size="sm" fw={500} span>ESD (on demand).</Text>{' '}
                    <Text size="sm" c="dimmed" span>
                      An IEC 61000-4-2 discharge on each connector line, in ngspice.
                    </Text>
                  </List.Item>
                  <List.Item>
                    <Text size="sm" fw={500} span>Cables (seconds).</Text>{' '}
                    <Text size="sm" c="dimmed" span>
                      How much common-mode current each cable may carry, from an antenna model.
                    </Text>
                  </List.Item>
                  <List.Item>
                    <Text size="sm" fw={500} span>Small-part solve (minutes).</Text>{' '}
                    {features.isSuccess && !features.data.small_part_solve && <OffBadge />}{' '}
                    <Text size="sm" c="dimmed" span>
                      One net and the planes under it, solved in openEMS: current map and port
                      impedance. No far field.
                    </Text>
                  </List.Item>
                  <List.Item>
                    <Text size="sm" fw={500} span>Full-wave solve (hours).</Text>{' '}
                    {features.isSuccess && !features.data.full_wave && <OffBadge />}{' '}
                    <Text size="sm" c="dimmed" span>
                      A region of the board in{' '}
                      <Anchor href="https://www.openems.de/" target="_blank" rel="noreferrer" inherit>
                        openEMS
                      </Anchor>
                      : current maps, far field and S-parameters.
                    </Text>
                  </List.Item>
                </List>
                <Text size="xs" c="dimmed">
                  Read{' '}
                  <Anchor component={Link} to={`${base}/limitations`} size="xs">the limitations</Anchor>
                  {' '}and{' '}
                  <Anchor component={Link} to={`${base}/limits`} size="xs">the emission limits</Anchor>
                  {' '}before acting on a result.
                </Text>
                <ChecksTable />
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      </Stack>
    </Container>
  )
}

function OffBadge() {
  return (
    <Tooltip withArrow events={{ hover: true, focus: true, touch: true }}
             label="Experimental, and off in this build.">
      <Badge component="span" size="xs" variant="light" color="yellow"
             style={{ cursor: 'help', display: 'inline-flex', verticalAlign: 'middle' }}>
        experimental · off
      </Badge>
    </Tooltip>
  )
}
