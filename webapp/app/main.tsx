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

import { Component, StrictMode, useLayoutEffect, useRef, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { Anchor, AppShell, Button, Code, Group, MantineProvider, Stack, Text, Title } from '@mantine/core'
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

const HEADER_HEIGHT = 52

/**
 * A blank window is the worst thing a desktop app can do: there is no address bar to retype
 * and no console to look at. A render error is caught here and shown with a way out.
 */
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <Stack gap="md" p="xl" maw={640}>
        <Title order={3}>Something went wrong in the app</Title>
        <Text size="sm">
          Your boards and results are unaffected — they are files on this computer. Reloading
          usually clears this.
        </Text>
        <Code block style={{ whiteSpace: 'pre-wrap' }}>{this.state.error.message}</Code>
        <Group gap="xs">
          <Button size="xs" onClick={() => window.location.reload()}>Reload</Button>
          <Button size="xs" variant="default" component="a" href="/tools/emi">
            Back to your boards
          </Button>
        </Group>
        <Text size="xs" c="dimmed">
          If it keeps happening, please report it at{' '}
          <Anchor size="xs" href="https://github.com/embeddedci-com/emi-analyzer/issues"
                  target="_blank" rel="noreferrer">
            github.com/embeddedci-com/emi-analyzer/issues
          </Anchor>
          .
        </Text>
      </Stack>
    )
  }
}

function Shell() {
  // The project page fills the window, so it has to know how much of the window is already
  // taken. The banner appears and disappears (worker starting, image downloading), so this is
  // measured rather than assumed -- with a fixed height the page overflowed on first run and
  // the whole app scrolled.
  const chromeRef = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const el = chromeRef.current
    if (!el) return
    const apply = () => {
      document.documentElement.style.setProperty(
        '--emi-chrome-height', `${HEADER_HEIGHT + el.offsetHeight}px`)
    }
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  return (
    <AppShell header={{ height: HEADER_HEIGHT }} padding={0}>
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Anchor component={Link} to="/tools/emi" underline="never" c="inherit" fw={700}>
            EMI Analyzer
          </Anchor>
          <WorkerStatus />
        </Group>
      </AppShell.Header>
      <AppShell.Main>
        <div ref={chromeRef}>
          <WorkerBanner />
        </div>
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
        <ErrorBoundary>
          <BrowserRouter>
            <Shell />
          </BrowserRouter>
        </ErrorBoundary>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
)
