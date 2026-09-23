/**
 * The "Tools" pop-out in the header.
 *
 * A plain Mantine `Menu`, so it inherits the host's theme rather than introducing a second
 * kind of menu. DEFAULT_TOOLS holds the tools this repository provides, at the path the local
 * app and the dev harness mount them; a host that mounts elsewhere passes its own entries.
 */

import { Menu, Stack, Text, UnstyledButton } from '@mantine/core'
import { Link } from 'react-router'
import type { CSSProperties, ReactNode } from 'react'

export interface ToolEntry {
  label: string
  description?: string
  to: string
  icon?: ReactNode
  /** False hides the entry, e.g. a tool the signed-in organisation has no access to. */
  enabled?: boolean
  /**
   * Load a new document instead of navigating client-side. For a host whose router on the
   * current page does not have this route, e.g. a marketing bundle linking into the app.
   */
  reloadDocument?: boolean
}

export const DEFAULT_TOOLS: ToolEntry[] = [
  {
    label: 'EMI Analyzer',
    description: 'PCB EMI and EMC checks, and full-wave EM simulation',
    to: '/tools/emi',
  },
]

export interface ToolsMenuProps {
  tools?: ToolEntry[]
  /** The trigger's style, so it can match the host's other header links. */
  triggerStyle?: CSSProperties
  label?: string
}

export function ToolsMenu({ tools = DEFAULT_TOOLS, triggerStyle, label = 'Tools' }: ToolsMenuProps) {
  const visible = tools.filter((t) => t.enabled !== false)
  if (visible.length === 0) return null

  return (
    <Menu shadow="md" width={260} position="bottom-start" trigger="click-hover" openDelay={80}>
      <Menu.Target>
        <UnstyledButton style={triggerStyle} aria-label={`${label} menu`}>
          {label}{' '}
          <span aria-hidden style={{ fontSize: '0.75em', opacity: 0.6 }}>
            ▾
          </span>
        </UnstyledButton>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{label}</Menu.Label>
        {visible.map((tool) => (
          <Menu.Item
            key={tool.to}
            component={Link}
            to={tool.to}
            reloadDocument={tool.reloadDocument}
            leftSection={tool.icon}
          >
            <Stack gap={0}>
              <Text size="sm">{tool.label}</Text>
              {tool.description && (
                <Text size="xs" c="dimmed">
                  {tool.description}
                </Text>
              )}
            </Stack>
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  )
}
