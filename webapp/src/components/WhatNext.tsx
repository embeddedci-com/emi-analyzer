/**
 * Where to go once a board's findings are in, as one line shown until closed. Each tab is one
 * the eye skips on the way to the findings, and each is where the numbers are.
 */

import { useState } from 'react'
import { Anchor, CloseButton, Group, Text } from '@mantine/core'
import { dismiss, isDismissed } from '../lib/dismissed'

const KEY = 'what-next'

export function WhatNext({ onTab }: { onTab: (tab: string) => void }) {
  const [closed, setClosed] = useState(() => isDismissed(KEY))
  if (closed) return null
  const tab = (value: string, label: string) => (
    <Anchor component="button" type="button" size="xs" fw={500} onClick={() => onTab(value)}>
      {label}
    </Anchor>
  )
  return (
    <Group gap={4} wrap="nowrap" px={8} py={2}
           style={{ borderRadius: 4, background: 'var(--mantine-color-blue-light)' }}>
      <Text size="xs" style={{ flex: 1 }}>
        Next: cable budgets in {tab('cables', 'Cables')}, a discharge test in {tab('esd', 'ESD')}.
      </Text>
      <CloseButton size="xs" aria-label="Hide this hint"
                   onClick={() => { dismiss(KEY); setClosed(true) }} />
    </Group>
  )
}
