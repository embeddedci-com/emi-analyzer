/**
 * What every board is checked for, as a table on the front page.
 *
 * Built from ruleCatalogue.json, which the worker exports from its own rule catalogue. A
 * worker test fails when the file is stale, so this list cannot drift from what actually
 * runs — it is the same source the findings panel takes its rule names from.
 */

import { Badge, Card, Stack, Table, Text } from '@mantine/core'
import catalogue from '../lib/ruleCatalogue.json'

interface CatalogueRule {
  id: string
  title: string
  about: string
  category: string
}

/**
 * Emissions first, ordered by how often each family is behind a real emissions failure, then
 * immunity -- the other half of an EMC test.
 */
export const CATEGORY_ORDER = [
  'Return path', 'Power integrity', 'Signal integrity', 'Radiation', 'Conducted emissions', 'Immunity',
]
export const CATEGORY_COLOR: Record<string, string> = {
  'Return path': 'blue',
  'Power integrity': 'grape',
  'Signal integrity': 'teal',
  Radiation: 'orange',
  'Conducted emissions': 'cyan',
  Immunity: 'violet',
}

export function ChecksTable() {
  const rules = catalogue as CatalogueRule[]
  const groups = CATEGORY_ORDER
    .map((cat) => [cat, rules.filter((r) => r.category === cat)] as const)
    .concat([['Other', rules.filter((r) => !CATEGORY_ORDER.includes(r.category))] as const])
    .filter(([, rs]) => rs.length > 0)

  return (
    <Card withBorder padding="md">
      <Stack gap="sm">
        <div>
          <Text fw={500}>What every board is checked for</Text>
          <Text size="xs" c="dimmed" mt={2}>
            {rules.length} checks run on every upload, in seconds, from the board geometry and
            stackup: what the board radiates and conducts out through its cables, and where ESD
            and fast transients get in. They say where to look. A solve on a selected region, or
            a near-field scan loaded onto the board, confirms what is actually radiating. Thresholds and
            which checks run are set per board under Findings, Checks, or in an{' '}
            <code>emi.rules.yaml</code> uploaded next to the <code>.kicad_pcb</code> in a zip.
          </Text>
        </div>
        <Table.ScrollContainer minWidth={560}>
          <Table verticalSpacing="xs" withRowBorders>
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={170}>Area</Table.Th>
                <Table.Th w={200}>Check</Table.Th>
                <Table.Th>What it looks for</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {groups.map(([cat, rs]) =>
                rs.map((r, i) => (
                  <Table.Tr key={r.id}>
                    {i === 0 && (
                      <Table.Td rowSpan={rs.length} style={{ verticalAlign: 'top' }}>
                        {/* Not uppercased: "Power integrity" fits the column where
                            "POWER INTEGRITY" was cut to "POWER INTEG…". */}
                        <Badge variant="light" tt="none" color={CATEGORY_COLOR[cat] ?? 'gray'}>
                          {cat}
                        </Badge>
                      </Table.Td>
                    )}
                    <Table.Td style={{ verticalAlign: 'top' }}>
                      <Text size="sm" fw={500}>{r.title}</Text>
                    </Table.Td>
                    <Table.Td style={{ verticalAlign: 'top' }}>
                      <Text size="sm" c="dimmed">{r.about}</Text>
                    </Table.Td>
                  </Table.Tr>
                )),
              )}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Stack>
    </Card>
  )
}
