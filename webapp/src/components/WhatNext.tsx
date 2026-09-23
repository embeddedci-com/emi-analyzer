/**
 * The three things to do once a board's findings are in, shown once per browser. Each is a
 * tab the eye skips on the way to the findings, and each is where the numbers are.
 */

import { useState } from 'react'
import { Alert, Anchor, List, Text } from '@mantine/core'
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
    <Alert color="blue" variant="light" p="xs" title="What next" withCloseButton
           closeButtonLabel="Hide this hint"
           onClose={() => { dismiss(KEY); setClosed(true) }}>
      <List size="xs" spacing={2}>
        <List.Item><Text size="xs">Click a finding to zoom the board to it.</Text></List.Item>
        <List.Item>
          <Text size="xs">{tab('cables', 'Cables')}: say what plugs into each connector.</Text>
        </List.Item>
        <List.Item>
          <Text size="xs">{tab('esd', 'ESD')}: simulate a discharge on each connector line.</Text>
        </List.Item>
      </List>
    </Alert>
  )
}
