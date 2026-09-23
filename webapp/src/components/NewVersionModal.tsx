/**
 * Upload a changed board into the project it is a version of.
 *
 * A version is a board in the same project, so the upload is the ordinary one with a project
 * that already exists. The hash is checked against this project's versions first: picking the
 * file that is already here opens that version instead of adding a copy of it.
 */

import { useRef, useState } from 'react'
import { Alert, Button, FileButton, Group, Modal, Progress, Stack, Text } from '@mantine/core'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { hashFile, type Board, type EmiApi } from '../lib/emiApi'
import type { Version } from '../lib/compare'

export interface NewVersionModalProps {
  api: EmiApi
  projectId: string
  opened: boolean
  versions: Version[]
  onClose: () => void
  /** Called with the new board, or with the version that already had these bytes. */
  onDone: (boardId: string) => void
}

export function NewVersionModal({ api, projectId, opened, versions, onClose, onDone }: NewVersionModalProps) {
  const qc = useQueryClient()
  const [pct, setPct] = useState<number | null>(null)
  const [same, setSame] = useState<Version | null>(null)
  const resetFile = useRef<() => void>(null)

  const upload = useMutation({
    mutationFn: async (file: File): Promise<Board | null> => {
      setSame(null)
      const sha = await hashFile(file)
      const existing = sha ? versions.find((v) => v.board.content_sha256 === sha) : undefined
      if (existing) {
        setSame(existing)
        return null
      }
      setPct(0)
      const { board } = await api.uploadBoard(projectId, file, (f) => setPct(f * 100), sha)
      return board
    },
    onSuccess: (board) => {
      setPct(null)
      resetFile.current?.()
      if (!board) return
      qc.invalidateQueries({ queryKey: ['emi', 'boards', projectId] })
      qc.invalidateQueries({ queryKey: ['emi', 'runs', projectId] })
      onDone(board.id)
    },
    onError: () => setPct(null),
  })

  const close = () => {
    if (upload.isPending) return
    setSame(null)
    upload.reset()
    onClose()
  }

  return (
    <Modal opened={opened} onClose={close} title="Upload a new version" size="sm" centered>
      <Stack gap="sm">
        <Text size="sm">
          Pick the changed board file. It is analyzed as version {versions.length + 1} of this
          board, and the earlier versions stay here to compare against.
        </Text>
        {same && (
          <Alert color="blue" variant="light">
            <Stack gap="xs" align="flex-start">
              <Text size="xs">That file is already version {same.number}.</Text>
              <Button size="compact-xs" variant="light"
                      onClick={() => { onDone(same.board.id); close() }}>
                Open version {same.number}
              </Button>
            </Stack>
          </Alert>
        )}
        {upload.isError && (
          <Alert color="red" variant="light" title="That file could not be uploaded">
            <Text size="xs">{(upload.error as Error).message}</Text>
          </Alert>
        )}
        {pct !== null && (
          <Stack gap={4}>
            <Progress value={pct} size="sm" animated />
            <Text size="xs" c="dimmed">
              {pct < 100 ? `Uploading, ${pct.toFixed(0)}%` : 'Uploaded. Starting the analysis.'}
            </Text>
          </Stack>
        )}
        <Group justify="flex-end" gap="xs">
          <Button size="xs" variant="default" onClick={close} disabled={upload.isPending}>
            Cancel
          </Button>
          <FileButton resetRef={resetFile} onChange={(f) => f && upload.mutate(f)}
                      accept=".kicad_pcb,.zip,application/zip">
            {(props) => (
              <Button size="xs" {...props} loading={upload.isPending}>Choose a board file</Button>
            )}
          </FileButton>
        </Group>
      </Stack>
    </Modal>
  )
}
