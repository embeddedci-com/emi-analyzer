/**
 * A small-part map's loudest spots away from the ports, numbered as on the board.
 *
 * One peak is a weak answer when a second spot is nearly as loud: on a real differential pair
 * the two mesh presets put the peak at opposite ends of the net, 2.7 dB apart, and both ends
 * needed fixing. The worker lists every separate spot within a few dB of the loudest
 * (`worker/emi_worker/openems/hotspots.py`), and they are shown together here.
 */

import { Badge, Group, Table, Text, UnstyledButton } from '@mantine/core'
import { HOTSPOT_MARKER_HEX } from '../lib/markers'
import type { HotSpot } from '../lib/smallPart'

export interface LoudestSpotsProps {
  spots: HotSpot[]
  withinDb: number
  onFocus?: (x: number, y: number) => void
}

export function LoudestSpots({ spots, withinDb, onFocus }: LoudestSpotsProps) {
  if (spots.length === 0) return null
  return (
    <div>
      <Text size="xs" fw={600} tt="uppercase" c="dimmed" mb={4}>
        {spots.length > 1 ? 'Loudest spots' : 'Loudest spot'}
      </Text>
      {spots.length > 1 && (
        <Text size="xs" mb={4}>
          The loudest {spots.length} spots are within {withinDb} dB of each other, so treat
          them together.
        </Text>
      )}
      <Table verticalSpacing={2} fz="xs">
        <Table.Tbody>
          {spots.map((s, i) => (
            <Table.Tr key={`${s.x_mm},${s.y_mm}`}>
              <Table.Td>
                <UnstyledButton
                  onClick={() => onFocus?.(s.x_mm, s.y_mm)}
                  title="Show on the board"
                  aria-label={`Show spot ${i + 1} on the board`}
                >
                  <Group gap={6} wrap="nowrap">
                    <Badge size="xs" circle variant="filled" color={HOTSPOT_MARKER_HEX}>{i + 1}</Badge>
                    <Text size="xs">
                      {[s.net, s.part].filter(Boolean).join(', ') || 'near the net'}
                    </Text>
                  </Group>
                </UnstyledButton>
              </Table.Td>
              <Table.Td ta="right">
                <Text size="xs" ff="monospace" c="dimmed">
                  ({s.x_mm.toFixed(1)}, {s.y_mm.toFixed(1)}) mm
                </Text>
              </Table.Td>
              <Table.Td ta="right">
                <Text size="xs" ff="monospace">{s.db.toFixed(1)} dB</Text>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Text size="10px" c="dimmed" mt={4}>
        Away from the ports, which are always loud. Marked in the same color on the board; click one to zoom there.
      </Text>
    </div>
  )
}
