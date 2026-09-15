/**
 * The stackup, as a vertical strip you can toggle.
 *
 * Shown in physical order with real thicknesses rather than as a flat checkbox list,
 * because the vertical stack is not decoration here: the dielectric thickness between a
 * signal layer and its reference plane is what sets the vertical mesh resolution, which
 * sets the timestep, which sets how long a solve takes. Making that visible early is
 * cheaper than explaining it later.
 */

import { Badge, Box, Checkbox, Group, Stack, Text, Tooltip } from '@mantine/core'
import { DEFAULT_LAYER_COLORS } from '../lib/BoardRenderer'
import type { BoardDoc } from '../lib/boardTypes'

export interface LayerRailProps {
  doc: BoardDoc
  visibility: Record<string, boolean>
  onToggle: (layer: string, visible: boolean) => void
}

const rgb = (c: [number, number, number]) =>
  `rgb(${c.map((v) => Math.round(v * 255)).join(', ')})`

export function LayerRail({ doc, visibility, onToggle }: LayerRailProps) {
  const copperIndex = new Map(doc.layers.map((l, i) => [l.name, i]))

  // Walk the stackup top-down so the strip matches the physical board.
  const rows = doc.stackup.filter((s) => s.role !== 'other' || s.thickness_mm > 0)

  return (
    <Stack gap={2}>
      {rows.map((entry) => {
        if (entry.role === 'copper') {
          const idx = copperIndex.get(entry.name)
          const color =
            idx === undefined
              ? 'var(--mantine-color-dimmed)'
              : rgb(DEFAULT_LAYER_COLORS[idx % DEFAULT_LAYER_COLORS.length])
          const layer = doc.layers.find((l) => l.name === entry.name)
          return (
            <Group key={entry.name} gap="xs" wrap="nowrap" py={2}>
              <Checkbox
                size="xs"
                checked={visibility[entry.name] ?? true}
                onChange={(e) => onToggle(entry.name, e.currentTarget.checked)}
                aria-label={`Show ${entry.name}`}
              />
              <Box w={12} h={12} style={{ background: color, borderRadius: 2, flex: 'none' }} />
              <Text size="xs" fw={500} style={{ flex: 1 }}>
                {entry.name}
              </Text>
              {/* What is actually poured on this layer, not what the file's layer-type
                  field says. Those disagree often and the file loses: a 4-layer board
                  typically declares its inner layers "power" while both are ground, and
                  reading POWER there sends you looking for a supply that is not on it. */}
              {layer?.plane_net ? (
                <Tooltip
                  label={`${layer.plane_net} pour covers ${Math.round(
                    (layer.plane_coverage ?? 0) * 100,
                  )}% of the board on ${entry.name}${
                    layer.kind && layer.kind !== 'signal'
                      ? ` — the board file types this layer "${layer.kind}"`
                      : ''
                  }`}
                >
                  <Badge size="xs" variant="light" color="gray" style={{ cursor: 'help' }}>
                    {layer.plane_net} {Math.round((layer.plane_coverage ?? 0) * 100)}%
                  </Badge>
                </Tooltip>
              ) : (
                layer?.kind &&
                layer.kind !== 'signal' && (
                  <Tooltip label={`The board file types this layer "${layer.kind}", but no single net pours over enough of it to call it a plane.`}>
                    <Badge size="xs" variant="outline" color="gray" style={{ cursor: 'help' }}>
                      {layer.kind}
                    </Badge>
                  </Tooltip>
                )
              )}
              <Text size="xs" c="dimmed" ff="monospace">
                {entry.thickness_mm ? `${(entry.thickness_mm * 1000).toFixed(0)} µm` : ''}
              </Text>
            </Group>
          )
        }

        // Dielectric and mask rows: not toggleable, but they carry the numbers a solve
        // depends on. A guessed epsilon_r is called out, because it shifts every resonance
        // and the user is the only one who can correct it.
        const guessed = !entry.from_file
        return (
          <Group key={`${entry.name}-${entry.z_bottom_mm}`} gap="xs" wrap="nowrap" pl={30} py={1}>
            <Box
              style={{
                flex: 1,
                height: Math.max(3, Math.min(14, entry.thickness_mm * 8)),
                background:
                  entry.role === 'dielectric'
                    ? 'var(--mantine-color-green-light)'
                    : 'var(--mantine-color-gray-light)',
                borderRadius: 1,
              }}
            />
            <Text size="10px" c="dimmed" ff="monospace" style={{ whiteSpace: 'nowrap' }}>
              {entry.thickness_mm.toFixed(3)} mm
              {entry.epsilon_r ? ` · εr ${entry.epsilon_r}` : ''}
            </Text>
            {guessed && entry.role === 'dielectric' && (
              <Tooltip
                multiline
                w={240}
                label="This value is not in the board file — we assumed it. A wrong εr shifts every resonance, so correct it before trusting a solve."
              >
                <Badge size="xs" color="yellow" variant="light">
                  assumed
                </Badge>
              </Tooltip>
            )}
          </Group>
        )
      })}
    </Stack>
  )
}
