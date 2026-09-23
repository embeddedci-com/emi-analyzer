/**
 * Which parts a solve modelled, and on whose authority (docs/implementation.md §3).
 *
 * A result that models some capacitors and not others is two different results in one
 * picture, and the difference is invisible in the field map. This says which is which.
 *
 * The provenance column is the point of it. A generic figure is a class average and is
 * labelled as one — never attributed to a manufacturer, because it describes none — while a
 * component from a datasheet or from the user's own library says so. Someone acting on a
 * self-resonance deserves to know whether it came from a part or from a category.
 */

import { Badge, Card, Group, Stack, Table, Text, Title, Tooltip } from '@mantine/core'
import type { ModelledPart } from './HotspotResults'

const fmtHz = (hz: number | null): string => {
  if (hz === null) return '—'
  if (hz >= 1e9) return `${(hz / 1e9).toFixed(1)} GHz`
  if (hz >= 1e6) return `${hz / 1e6 >= 10 ? (hz / 1e6).toFixed(0) : (hz / 1e6).toFixed(1)} MHz`
  return `${(hz / 1e3).toFixed(0)} kHz`
}

const fmtFarads = (f: number): string => {
  if (f >= 1e-6) return `${(f * 1e6).toPrecision(2).replace(/\.0+$/, '')} µF`
  if (f >= 1e-9) return `${(f * 1e9).toPrecision(2).replace(/\.0+$/, '')} nF`
  return `${(f * 1e12).toPrecision(2).replace(/\.0+$/, '')} pF`
}

export function ModelledParts({ parts }: { parts?: ModelledPart[] }) {
  if (parts === undefined) {
    // Not the same as "none were modelled": this result was produced before the list existed.
    return null
  }
  if (parts.length === 0) {
    return (
      <Text size="xs" c="dimmed">
        No components were modelled in this solve — every part is bare copper, as it was
        before component models existed.
      </Text>
    )
  }

  const generic = parts.filter((p) => p.generic).length

  return (
    <Card withBorder padding="sm">
      <Stack gap="xs">
        <Group justify="space-between" align="baseline">
          <Title order={6}>Modelled parts</Title>
          <Text size="xs" c="dimmed">
            {parts.length} placed
            {generic > 0 && `, ${generic} from generic figures`}
          </Text>
        </Group>

        <Table verticalSpacing={2} fz="xs" withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Ref</Table.Th>
              <Table.Th>Value</Table.Th>
              <Table.Th>Self-resonance</Table.Th>
              <Table.Th>Model</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {parts.map((p) => (
              <Table.Tr key={p.ref}>
                <Table.Td ff="monospace">{p.ref}</Table.Td>
                <Table.Td ff="monospace">{fmtFarads(p.c_f)}</Table.Td>
                <Table.Td ff="monospace">{fmtHz(p.self_resonance_hz)}</Table.Td>
                <Table.Td>
                  <Group gap={6} wrap="nowrap">
                    <Text size="xs" truncate>{p.component}</Text>
                    {p.generic ? (
                      <Tooltip
                        label="Typical for this package and value — not a measurement of this part"
                        withArrow
                      >
                        <Badge size="xs" variant="light" color="gray">generic</Badge>
                      </Tooltip>
                    ) : (
                      <Tooltip label={p.source ?? 'from its model'} withArrow>
                        <Badge size="xs" variant="light" color="teal">cited</Badge>
                      </Tooltip>
                    )}
                  </Group>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>

        {generic > 0 && (
          <Text size="xs" c="dimmed">
            A generic figure puts a self-resonance in the right decade for the package and
            value. It is not a measurement of the part on this board, so treat the frequency
            as approximate and add the part to your library if it matters.
          </Text>
        )}
      </Stack>
    </Card>
  )
}
