/**
 * The local worker container, as the user sees it.
 *
 * Nothing in the analyzer works without a worker -- even showing a board needs an ingest run
 * -- and locally the worker is a Docker container this app starts. So the states that matter
 * are the ones between "app is open" and "worker is running": Docker missing, Docker not
 * started, the first image pull. Each gets a sentence saying what to do about it.
 */

import { useState } from 'react'
import {
  Alert, Anchor, Badge, Button, Code, Group, Loader, Modal, ScrollArea, Stack, Text,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

export type WorkerState =
  | 'disabled' | 'docker_missing' | 'docker_not_running' | 'pulling' | 'starting'
  | 'running' | 'error' | 'stopped'

export interface LocalStatus {
  version: string
  data_dir: string
  worker: {
    mode: 'docker' | 'none'
    state: WorkerState
    message?: string
    /** What docker actually printed. Kept out of the banner; shown with the log. */
    detail?: string
    image?: string
    container?: string
  }
}

async function getJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json() as Promise<T>
}

function useLocalStatus() {
  return useQuery({
    queryKey: ['local', 'status'],
    queryFn: () => getJSON<LocalStatus>('/api/local/status'),
    // Fast while something is changing, slow once it has settled.
    refetchInterval: (q) => (q.state.data?.worker.state === 'running' ? 15_000 : 2_000),
  })
}

const LABEL: Record<WorkerState, { text: string; color: string }> = {
  disabled: { text: 'Worker: external', color: 'gray' },
  docker_missing: { text: 'Docker not installed', color: 'red' },
  docker_not_running: { text: 'Docker not running', color: 'orange' },
  pulling: { text: 'Downloading worker', color: 'blue' },
  starting: { text: 'Starting worker', color: 'blue' },
  running: { text: 'Worker running', color: 'green' },
  // Not "stopped": it failed, and saying so is what sends the user to the reason.
  error: { text: 'Worker problem', color: 'red' },
  stopped: { text: 'Worker stopped', color: 'gray' },
}

export function WorkerStatus() {
  const status = useLocalStatus()
  const [open, setOpen] = useState(false)
  if (!status.data) return null
  const w = status.data.worker
  const label = LABEL[w.state] ?? LABEL.error
  const busy = w.state === 'pulling' || w.state === 'starting'
  return (
    <>
      <Badge
        variant="light"
        color={label.color}
        size="lg"
        style={{ cursor: 'pointer' }}
        leftSection={busy ? <Loader size={10} color={label.color} /> : undefined}
        onClick={() => setOpen(true)}
      >
        {label.text}
      </Badge>
      <WorkerModal status={status.data} opened={open} onClose={() => setOpen(false)} />
    </>
  )
}

function WorkerModal({ status, opened, onClose }: { status: LocalStatus; opened: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const logs = useQuery({
    queryKey: ['local', 'logs'],
    queryFn: () => fetch('/api/local/worker/logs').then((r) => r.text()),
    enabled: opened && status.worker.mode === 'docker',
    refetchInterval: opened ? 5_000 : false,
  })
  const restart = useMutation({
    mutationFn: () => getJSON('/api/local/worker/restart', { method: 'POST' }),
    onSettled: () => qc.invalidateQueries({ queryKey: ['local'] }),
  })
  const w = status.worker
  return (
    <Modal opened={opened} onClose={onClose} title="Local worker" size="xl">
      <Stack gap="sm">
        <Text size="sm">{w.message}</Text>
        {w.detail && (
          <ScrollArea.Autosize mah={140} type="auto">
            <Code block style={{ whiteSpace: 'pre-wrap', fontSize: 11 }}>{w.detail}</Code>
          </ScrollArea.Autosize>
        )}
        {w.image && (
          <Text size="xs" c="dimmed">
            Image <Code>{w.image}</Code>
            {w.container && <> · container <Code>{w.container}</Code></>}
          </Text>
        )}
        <Text size="xs" c="dimmed">
          Data folder <Code>{status.data_dir}</Code> · version {status.version}
        </Text>
        {w.mode === 'docker' && (
          <>
            <Group>
              <Button size="xs" variant="light" loading={restart.isPending} onClick={() => restart.mutate()}>
                Restart worker
              </Button>
            </Group>
            <ScrollArea.Autosize mah="45vh" type="auto">
              <Code block style={{ whiteSpace: 'pre-wrap', fontSize: 11 }}>
                {logs.data || 'No output yet.'}
              </Code>
            </ScrollArea.Autosize>
          </>
        )}
      </Stack>
    </Modal>
  )
}

/** A full-width explanation, shown only while the worker is not usable. */
export function WorkerBanner() {
  const status = useLocalStatus()
  const w = status.data?.worker
  if (status.isError) {
    return (
      <Alert color="red" variant="light" radius={0} title="The local server is not answering">
        {(status.error as Error).message}
      </Alert>
    )
  }
  if (!w || w.state === 'running' || w.state === 'disabled') return null

  const tone = w.state === 'pulling' || w.state === 'starting' ? 'blue' : 'orange'
  const title: Partial<Record<WorkerState, string>> = {
    docker_missing: 'Docker is needed to analyse boards',
    docker_not_running: 'Start Docker to analyse boards',
    pulling: 'Downloading the worker image',
    starting: 'Starting the worker',
    error: 'The worker is not running',
    stopped: 'The worker is not running',
  }
  return (
    <Alert color={tone} variant="light" radius={0} title={title[w.state]}>
      <Text size="sm">{w.message}</Text>
      {w.state === 'docker_missing' && (
        <Text size="sm" mt={4}>
          Install{' '}
          <Anchor size="sm" href="https://docs.docker.com/get-started/get-docker/"
                  target="_blank" rel="noreferrer">
            Docker Desktop
          </Anchor>
          {' '}(or Docker Engine on Linux) and start it — this page picks it up on its own.
        </Text>
      )}
      {(w.state === 'error' || w.state === 'stopped') && (
        <Text size="sm" mt={4}>
          Open the worker status at the top of the window to read the log, or to restart it.
        </Text>
      )}
    </Alert>
  )
}
