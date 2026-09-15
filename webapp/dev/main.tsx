/**
 * Standalone harness for the EMI Analyzer frontend.
 *
 * Mounts exactly the same components, routes and API client that embeddedci-server will,
 * so what is verified here is the real thing rather than a mock of it. The only difference
 * is the shell around them: this file provides the Mantine and react-query providers that
 * the host app already has.
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { MantineProvider, AppShell, Group, Title } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Link, Route, Routes, Navigate } from 'react-router'
import '@mantine/core/styles.css'

import { EmiApi } from '../src/lib/emiApi'
import { emiRoutes } from '../src/routes'
import { ToolsMenu } from '../src/ToolsMenu'

// No credentials: the standalone server treats every request as a signed-out visitor, and
// sign-in belongs to the host that mounts the analyzer.
const api = new EmiApi({ baseUrl: '/api' })

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1 } } })

function Shell() {
  return (
    <AppShell header={{ height: 56 }} padding={0}>
      <AppShell.Header>
        <Group h="100%" px="md" gap="lg">
          <Title order={5} component={Link} to="/tools/emi" style={{ textDecoration: 'none' }}>
            EmbeddedCI
          </Title>
          <ToolsMenu triggerStyle={{ fontSize: 14, cursor: 'pointer' }} />
        </Group>
      </AppShell.Header>
      <AppShell.Main>
        <Routes>
          <Route path="/" element={<Navigate to="/tools/emi" replace />} />
          {/* The fragment's paths are relative, so the prefix is supplied here -- the same
              prefix embeddedci-server mounts it at. */}
          <Route path="/tools/emi">{emiRoutes(api)}</Route>
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
