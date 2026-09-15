/**
 * Load a near-field probe scan and draw it on the board.
 *
 * The measurement half of the loop: the checks say where to look, a solve confirms one
 * mechanism, and a probe run over the real board shows what is actually radiating. It uses
 * the same overlay a solve result does, so the two can be read against the same copper.
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Button, Checkbox, FileButton, Group, NumberInput, SegmentedControl, Stack, Switch, Text,
} from '@mantine/core'
import type { FieldOverlayData } from '../lib/overlay'
import { parseScan, scanToOverlay, type ParsedScan, type ScanOverlay } from '../lib/nearField'

export interface NearFieldImportProps {
  boardHeight: number
  onOverlayChange: (overlay: FieldOverlayData | null) => void
  onGateChange: (gateDb: number) => void
}

const mm = (v: number) => (Math.round(v * 100) / 100).toString()

// Discriminated on `kind` on purpose: TypeScript widens an `{ ok } | { err }` union so that
// an `in` check no longer narrows it.
type ScanResult = { kind: 'ok'; value: ScanOverlay } | { kind: 'error'; message: string }

export function NearFieldImport({ boardHeight, onOverlayChange, onGateChange }: NearFieldImportProps) {
  const [scan, setScan] = useState<ParsedScan | null>(null)
  const [fileName, setFileName] = useState('')
  const [offsetX, setOffsetX] = useState(0)
  const [offsetY, setOffsetY] = useState(0)
  const [flipY, setFlipY] = useState(false)
  const [units, setUnits] = useState<'db' | 'linear'>('db')
  const [rangeDb, setRangeDb] = useState(40)
  const [show, setShow] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const result = useMemo<ScanResult | null>(() => {
    if (!scan) return null
    try {
      return {
        kind: 'ok',
        value: scanToOverlay(scan, {
          offsetX, offsetY, flipY, boardHeight, valuesAreDb: units === 'db', rangeDb,
        }),
      }
    } catch (err) {
      return { kind: 'error', message: (err as Error).message }
    }
  }, [scan, offsetX, offsetY, flipY, boardHeight, units, rangeDb])

  useEffect(() => {
    // Only touch the overlay once a scan is loaded, so opening this panel does not clear a
    // solve result that is already on the board.
    if (!scan) return
    if (result?.kind === 'ok' && show) {
      onOverlayChange(result.value.overlay)
      onGateChange(-rangeDb)
    } else {
      onOverlayChange(null)
    }
  }, [scan, result, show, rangeDb, onOverlayChange, onGateChange])

  const load = async (file: File | null) => {
    if (!file) return
    setError(null)
    try {
      const parsed = parseScan(await file.text())
      setScan(parsed)
      setFileName(file.name)
      setUnits(parsed.looksLikeDb ? 'db' : 'linear')
      setShow(true)
    } catch (err) {
      setError(`${file.name}: ${(err as Error).message}`)
    }
  }

  const clear = () => {
    setScan(null)
    setFileName('')
    onOverlayChange(null)
  }

  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed">
        Load a probe scan — rows of x, y and a reading — to see measured hotspots on the board,
        drawn like a solve result. Coordinates are millimetres from the board's bottom-left
        corner with Y up; use the offsets to line it up. The file stays in your browser.
      </Text>
      <Group gap="xs">
        <FileButton onChange={load} accept=".csv,.tsv,.txt,text/csv,text/plain">
          {(props) => (
            <Button {...props} size="compact-xs" variant="light">
              {scan ? 'Load another scan' : 'Load near-field scan'}
            </Button>
          )}
        </FileButton>
        {scan && (
          <Button size="compact-xs" variant="subtle" color="gray" onClick={clear}>
            Clear
          </Button>
        )}
      </Group>
      {error && <Text size="xs" c="red">{error}</Text>}

      {scan && result?.kind === 'error' && <Text size="xs" c="red">{result.message}</Text>}
      {scan && result?.kind === 'ok' && (
        <Stack gap={6}>
          <Text size="xs">
            {fileName}: {scan.points.length} points on a {result.value.width}×{result.value.height} grid,
            {' '}{mm(result.value.stepX)}×{mm(result.value.stepY)} mm pitch
            {scan.skipped > 0 ? ` (${scan.skipped} unreadable rows skipped)` : ''}. Peak{' '}
            {result.value.peak.toFixed(1)} {units === 'db' ? (scan.valueLabel || 'dB') : 'dB (from linear)'} at
            ({mm(result.value.peakAt.x)}, {mm(result.value.peakAt.y)}) mm.
          </Text>
          <Checkbox
            size="xs"
            label="Show on the board (replaces a solve overlay while shown)"
            checked={show}
            onChange={(e) => setShow(e.currentTarget.checked)}
          />
          <SegmentedControl
            size="xs"
            value={units}
            onChange={(v) => setUnits(v as 'db' | 'linear')}
            data={[
              { label: 'Readings in dB', value: 'db' },
              { label: 'Linear readings', value: 'linear' },
            ]}
          />
          <Group grow gap="xs">
            <NumberInput
              size="xs" label="Offset X (mm)" value={offsetX} step={0.5} decimalScale={2}
              onChange={(v) => setOffsetX(Number(v) || 0)}
            />
            <NumberInput
              size="xs" label="Offset Y (mm)" value={offsetY} step={0.5} decimalScale={2}
              onChange={(v) => setOffsetY(Number(v) || 0)}
            />
          </Group>
          <Switch
            size="xs"
            label="Scanner Y axis points down"
            checked={flipY}
            onChange={(e) => setFlipY(e.currentTarget.checked)}
          />
          <NumberInput
            size="xs" label="Range shown below the peak (dB)" value={rangeDb} min={3} max={120} step={5}
            onChange={(v) => setRangeDb(Math.max(3, Number(v) || 40))}
          />
        </Stack>
      )}
    </Stack>
  )
}
