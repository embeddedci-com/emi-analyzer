/**
 * What the pages need from whoever mounts them: where they are mounted, and what to say about
 * the server behind them.
 *
 * The analyzer names no operator. A hosted copy is someone's server, and only that someone
 * knows where it keeps boards, who can read them and how to sign in, so those sentences are
 * the host's to supply. The defaults are true of any server, and say no more than that.
 */

import { createContext, useContext } from 'react'
import { Outlet, useParams, useResolvedPath } from 'react-router'

export interface EmiHostCopy {
  /** Where board files are stored and who processes them, for the upload card. One sentence. */
  storage?: string
  /** The privacy entry on the limitations page. */
  privacy?: { what: string; why: string }
  /** The hint on the "Not signed in" badge. */
  signIn?: string
  /** Where installing the KiCad plugin is explained. No link when omitted. */
  pluginDocsUrl?: string
}

export const DEFAULT_HOST_COPY = {
  storage: 'Boards are stored and processed by the server you are connected to.',
  privacy: {
    what: 'Board data is stored and processed on the server',
    why:
      'Uploaded board files and their results are stored and processed by the server you are ' +
      'connected to, and whoever runs it can read them. Handle them as you would any other ' +
      'upload of an unreleased design.',
  },
  signIn: 'Your boards are private to this browser. Sign in to keep them with your account.',
} satisfies EmiHostCopy

export function resolveHostCopy(host: EmiHostCopy = {}) {
  return {
    storage: host.storage ?? DEFAULT_HOST_COPY.storage,
    privacy: host.privacy ?? DEFAULT_HOST_COPY.privacy,
    signIn: host.signIn ?? DEFAULT_HOST_COPY.signIn,
    pluginDocsUrl: host.pluginDocsUrl,
  }
}

const EmiBaseContext = createContext<string | null>(null)

/**
 * The path the route rendering this has consumed.
 *
 * `.` resolves to that, except under a splat route (`/tools/emi/*`), where it resolves to the
 * whole URL including what the splat matched; that part is taken back off.
 */
function useConsumedPath(): string {
  const here = useResolvedPath('.').pathname.replace(/\/$/, '')
  const splat = useParams()['*']
  return splat && here.endsWith(`/${splat}`) ? here.slice(0, -(splat.length + 1)) : here
}

/**
 * A pathless layout route that records the path the host mounted the fragment at, so the
 * pages can build links from it however deep they are.
 */
export function EmiBase() {
  const base = useConsumedPath()
  return (
    <EmiBaseContext.Provider value={base}>
      <Outlet />
    </EmiBaseContext.Provider>
  )
}

/**
 * The path the analyzer is mounted at, without a trailing slash, for building links.
 *
 * A page rendered outside {@link emiRoutes} has no mount point recorded, and falls back to its
 * own route; that is right for the index page and nothing else.
 */
export function useEmiBase(): string {
  const mounted = useContext(EmiBaseContext)
  const here = useConsumedPath()
  return mounted ?? here
}
