/**
 * Search and highlight nets.
 *
 * Sorted by routed length rather than alphabetically: the long nets are the ones that
 * radiate, so the default order already answers "what should I look at".
 */

import { useMemo, useState } from 'react'
import { Group, NavLink, ScrollArea, Stack, Text, TextInput } from '@mantine/core'
import type { BoardDoc, BoardNet } from '../lib/boardTypes'

export interface NetPickerProps {
  doc: BoardDoc
  selected: string | null
  onSelect: (net: string | null) => void
  maxRows?: number
}

export function NetPicker({ doc, selected, onSelect, maxRows = 300 }: NetPickerProps) {
  const [query, setQuery] = useState('')

  const nets = useMemo(() => {
    const q = query.trim().toLowerCase()
    return [...doc.nets]
      .filter((n) => n.name && (!q || n.name.toLowerCase().includes(q)))
      .sort((a, b) => b.length_mm - a.length_mm)
      .slice(0, maxRows)
  }, [doc.nets, query, maxRows])

  const total = doc.nets.filter((n) => n.name).length

  return (
    <Stack gap="xs">
      <TextInput
        size="xs"
        placeholder={`Filter ${total} nets…`}
        value={query}
        onChange={(e) => setQuery(e.currentTarget.value)}
      />
      <ScrollArea.Autosize mah={280} type="auto">
        <Stack gap={0}>
          {nets.map((net) => (
            <NetRow
              key={net.name}
              net={net}
              active={selected === net.name}
              onClick={() => onSelect(selected === net.name ? null : net.name)}
            />
          ))}
          {nets.length === 0 && (
            <Text size="xs" c="dimmed" p="xs">
              No net matches “{query}”.
            </Text>
          )}
        </Stack>
      </ScrollArea.Autosize>
    </Stack>
  )
}

function NetRow({ net, active, onClick }: { net: BoardNet; active: boolean; onClick: () => void }) {
  return (
    <NavLink
      active={active}
      onClick={onClick}
      py={4}
      label={
        <Group gap="xs" wrap="nowrap" justify="space-between">
          <Text size="xs" truncate style={{ flex: 1 }} title={net.name}>
            {net.name}
          </Text>
          {/* Spelled out rather than abbreviated. "89v" next to a net called +3V3 reads as
              eighty-nine volts, which is both wrong and alarming. */}
          <Text size="10px" c="dimmed" ff="monospace" style={{ whiteSpace: 'nowrap' }}>
            {net.length_mm >= 1 ? `${net.length_mm.toFixed(0)} mm` : '—'}
          </Text>
          <Text
            size="10px"
            c="dimmed"
            ff="monospace"
            w={56}
            ta="right"
            style={{ whiteSpace: 'nowrap', flex: 'none' }}
          >
            {net.vias > 0 ? `${net.vias} via${net.vias === 1 ? '' : 's'}` : ''}
          </Text>
        </Group>
      }
    />
  )
}
