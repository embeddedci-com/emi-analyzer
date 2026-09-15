/**
 * Export the net list as a CSV.
 *
 * Two formats because there are two readers. A spreadsheet needs the file matched to its
 * locale — semicolons and comma decimals in much of Europe — or it opens as one column, or
 * with numbers silently misread. A script wants plain RFC 4180 and nothing clever.
 */

import { useEffect, useState } from 'react'
import { Button, Menu, Stack, Text } from '@mantine/core'
import type { BoardDoc } from '../lib/boardTypes'
import type { EmiApi } from '../lib/emiApi'
import {
  buildNetsCsv, csvFileName, downloadText, PLAIN_CSV, reportFromBoard, spreadsheetFormat,
  type CsvFormat,
} from '../lib/netsCsv'

export interface NetsExportButtonProps {
  api: EmiApi
  /** The ingest run whose nets.json to export. */
  runId: string | undefined
  doc: BoardDoc
  projectName: string
  /** Start a fresh analysis of this board, for one analysed before these columns existed. */
  onReanalyse?: () => void
  reanalysing?: boolean
}

export function NetsExportButton({
  api, runId, doc, projectName, onReanalyse, reanalysing,
}: NetsExportButtonProps) {
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const local = spreadsheetFormat()

  // A different analysis is on screen, so whatever the last export found no longer applies.
  useEffect(() => {
    setNote(null)
    setError(null)
  }, [runId])

  const exportAs = async (format: CsvFormat) => {
    setBusy(true)
    setError(null)
    try {
      const report = (runId ? await api.fetchNets(runId) : null) ?? reportFromBoard(doc)
      setNote(
        report.partial
          ? 'This board was analysed before paths, delays and skew were measured, so those columns are empty. Re-analyse it to fill them in; copper length is already accurate.'
          : null,
      )
      downloadText(csvFileName(projectName), buildNetsCsv(report, format))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <Menu position="bottom-end" withinPortal>
        <Menu.Target>
          <Button size="compact-xs" variant="light" loading={busy}>
            Export CSV
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item onClick={() => exportAs(local)}>
            <Text size="xs" fw={500}>For a spreadsheet on this computer</Text>
            <Text size="xs" c="dimmed">
              {local.delimiter === ';' ? 'semicolon' : 'comma'} separated, “{local.decimal}” decimals
            </Text>
          </Menu.Item>
          <Menu.Item onClick={() => exportAs(PLAIN_CSV)}>
            <Text size="xs" fw={500}>Plain CSV</Text>
            <Text size="xs" c="dimmed">comma separated, “.” decimals — for scripts</Text>
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
      {note && (
        <Stack gap={4} mt={4} maw={240}>
          <Text size="xs" c="dimmed">{note}</Text>
          {onReanalyse && (
            <Button
              size="compact-xs"
              variant="subtle"
              onClick={onReanalyse}
              loading={reanalysing}
              disabled={reanalysing}
            >
              {reanalysing ? 'Re-analysing…' : 'Re-analyse this board'}
            </Button>
          )}
        </Stack>
      )}
      {error && <Text size="xs" c="red" mt={4}>Export failed: {error}</Text>}
    </div>
  )
}
