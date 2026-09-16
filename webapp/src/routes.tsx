/**
 * Route fragment for the EMI Analyzer.
 *
 * The paths here are **relative to wherever the host mounts them**, which is what lets one
 * fragment serve two very different shells: embeddedci-server renders it inside a
 * `<Route path="/tools/emi/*">`, and the standalone harness wraps it in a `<Route path="/tools/emi">`.
 *
 * It deliberately does not carry its own `/tools/emi` prefix. A nested `<Routes>` matches
 * against the part of the URL its parent route has not already consumed, so a fragment that
 * repeated the prefix could only ever match under a host that had consumed nothing -- and
 * under one that mounts it at the prefix, as embeddedci-server does, it silently matches
 * nothing and renders a blank page. Which prefix the tool lives at is the host's decision.
 */

import { Route } from 'react-router'
import { EmiAnalyzerPage } from './pages/EmiAnalyzerPage'
import { EmiProjectPage } from './pages/EmiProjectPage'
import { EmiLimitationsPage } from './pages/EmiLimitationsPage'
import { EmiLimitsPage } from './pages/EmiLimitsPage'
import type { EmiApi } from './lib/emiApi'

/**
 * Where this copy of the analyzer runs. It changes only what the pages *say* -- where board
 * files are stored, who processes them -- never what they do.
 *
 *   hosted  mounted into a web server; files go to object storage and a remote worker
 *   local   the desktop app; files stay on this computer and a local container does the work
 */
export type EmiDeployment = 'hosted' | 'local'

export interface EmiRouteOptions {
  /** Defaults to 'hosted', so an existing host that passes nothing is unchanged. */
  deployment?: EmiDeployment
}

export function emiRoutes(api: EmiApi, options: EmiRouteOptions = {}) {
  const deployment = options.deployment ?? 'hosted'
  return (
    <>
      <Route index element={<EmiAnalyzerPage api={api} deployment={deployment} />} />
      <Route path="limitations" element={<EmiLimitationsPage deployment={deployment} />} />
      <Route path="limits" element={<EmiLimitsPage />} />
      <Route path=":projectId" element={<EmiProjectPage api={api} deployment={deployment} />} />
    </>
  )
}
