/**
 * The EMI Analyzer as a local app.
 *
 * Mounts exactly the routes and API client every other host does -- src/ is not forked for
 * this -- inside a shell of its own: no site header, no sign-in, and a status line for the
 * one thing that is different locally, the worker container this computer runs.
 *
 * Served by `emi-local` (server/cmd/emi-local), either in a browser tab or inside the
 * desktop app's window.
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Anchor, AppShell, Group, MantineProvider } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Link, Navigate, Route, Routes } from 'react-router'
import '@mantine/core/styles.css'

import { EmiApi } from '../src/lib/emiApi'
import { emiRoutes } from '../src/routes'
import { WorkerStatus, WorkerBanner } from './WorkerStatus'

// Same origin, no credentials: emi-local serves one fixed local identity, and only to
// requests that reach it on the loopback interface.
const api = new EmiApi({ baseUrl: '/api' })

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1 } } })

function Shell() {
  return (
    <AppShell header={{ height: 52 }} padding={0}>
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Anchor component={Link} to="/tools/emi" underline="never" c="inherit" fw={700}>
            EMI Analyzer
          </Anchor>
          <WorkerStatus />
        </Group>
      </AppShell.Header>
      <AppShell.Main>
        <WorkerBanner />
        <Routes>
          <Route path="/" element={<Navigate to="/tools/emi" replace />} />
          {/* The same prefix the hosted app mounts at, because the pages link to it. */}
          <Route path="/tools/emi">{emiRoutes(api, { deployment: 'local' })}</Route>
          <Route path="*" element={<Navigate to="/tools/emi" replace />} />
        </Routes>
      </AppShell.Main>
    </AppShell>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MantineProvider defaultColorScheme="auto">
      <QueryClientProvider client={qc}>
        <BrowserRouter>
          <Shell />
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
)
