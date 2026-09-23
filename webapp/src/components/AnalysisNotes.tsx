/**
 * What the worker had to say about this board, above its findings.
 *
 * A finding list reads as complete. When zones were not filled, or a rules file was refused,
 * it is not, and the only record of that used to be a note in rules.json that nothing showed.
 * Notes that switch a check off stand out; the ones that need nothing done fold away.
 */

import { useMemo, useState } from 'react'
import { Anchor, Box, CloseButton, Group, Paper, Stack, Text } from '@mantine/core'
import type { BoardDoc, RulesDoc } from '../lib/boardTypes'
import { collectNotices, type Notice, type NoticeLevel } from '../lib/notices'
import { dismiss, isDismissed } from '../lib/dismissed'

const COLOR: Record<NoticeLevel, string> = { off: 'orange', warn: 'yellow', info: 'gray' }

export interface AnalysisNotesProps {
  rules: RulesDoc | null
  doc: BoardDoc | null
  /** The analysis these notes belong to. Closing them is remembered for it, not forever. */
  runId?: string
  /** Open the settings view. */
  onOpenChecks?: () => void
}

export function AnalysisNotes({ rules, doc, runId, onOpenChecks }: AnalysisNotesProps) {
  const { notices, rulesFile, appSettings } = useMemo(() => collectNotices(rules, doc), [rules, doc])
  const key = `notes.${runId ?? ''}`
  const [closed, setClosed] = useState(() => isDismissed(key))
  const [showQuiet, setShowQuiet] = useState(false)

  const loud = notices.filter((n) => n.level !== 'info')
  const quiet = notices.filter((n) => n.level === 'info')

  const source = (
    <Text size="xs" c="dimmed">
      Rules:{' '}
      <Text span size="xs" fw={500} c="var(--mantine-color-text)">
        {rulesFile ?? 'built-in defaults'}
      </Text>
      {appSettings && ' + settings from this app'}
      {onOpenChecks && (
        <>
          {' · '}
          <Anchor component="button" type="button" size="xs" onClick={onOpenChecks}>
            Change
          </Anchor>
        </>
      )}
    </Text>
  )

  if (notices.length === 0) return source

  if (closed) {
    return (
      <Group gap={6} wrap="nowrap">
        {source}
        <Text size="xs" c="dimmed">·</Text>
        <Anchor component="button" type="button" size="xs" onClick={() => setClosed(false)}>
          {notices.length} note{notices.length === 1 ? '' : 's'}
        </Anchor>
      </Group>
    )
  }

  return (
    <Paper withBorder p="xs" radius="sm">
      <Stack gap={6}>
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <Text size="xs" fw={600}>About this analysis</Text>
          <CloseButton size="sm" aria-label="Hide these notes"
                       onClick={() => { dismiss(key); setClosed(true) }} />
        </Group>
        {source}
        {loud.map((n, i) => <NoticeLine key={i} notice={n} />)}
        {quiet.length > 0 && (showQuiet || loud.length === 0
          ? quiet.map((n, i) => <NoticeLine key={`q${i}`} notice={n} />)
          : (
            <Anchor component="button" type="button" size="xs" c="dimmed" ta="left"
                    onClick={() => setShowQuiet(true)}>
              {quiet.length} more note{quiet.length === 1 ? '' : 's'}
            </Anchor>
          ))}
      </Stack>
    </Paper>
  )
}

function NoticeLine({ notice }: { notice: Notice }) {
  return (
    <Box pl={8} style={{ borderLeft: `2px solid var(--mantine-color-${COLOR[notice.level]}-6)` }}>
      <Text size="xs" c={notice.level === 'info' ? 'dimmed' : undefined}
            fw={notice.level === 'off' ? 500 : undefined}>
        {notice.text}
      </Text>
    </Box>
  )
}
