/**
 * EMI Analyzer API client.
 *
 * Mirrors the routes registered by `emi.Mount`. The one thing worth noticing: board files
 * and result artifacts never pass through this client's JSON calls. They move directly
 * between the browser and object storage over presigned URLs, and the API only ever mints
 * those URLs. That is what keeps a 1 vCPU control plane viable for half-gigabyte results.
 */

import type { BoardDoc, RulesDoc, NetReport } from './boardTypes'
import type { Estimate, EstimateInput } from './estimate'

export type RunKind = 'ingest' | 'solve' | 'transient' | 'cable' | 'compliance'
export type RunStatus =
  | 'new' | 'retry_pending' | 'in_progress' | 'stopping'
  | 'done' | 'failed' | 'timed_out'

export const TERMINAL_STATUSES: RunStatus[] = ['done', 'failed', 'timed_out']

export interface Project {
  id: string
  organization_id: string
  name: string
  source_kind: 'kicad' | 'gerber'
  created_at: string
}

/** A component in the library: the emi-component document, plus its row fields. */
export interface StoredComponent {
  id: string
  owner_user_id: string
  organization_id: string
  shared: boolean
  kind: string
  name: string
  match?: unknown
  model?: unknown
  sources?: unknown
  version: number
  created_at: string
  updated_at: string
}

/** A driver stored with a project: the emi-driver document of docs/emi-driver-format.md, plus its
 * row fields. */
export interface StoredDriver {
  id: string
  organization_id: string
  project_id: string
  created_by?: string
  name: string
  role: 'signal' | 'switching-regulator'
  kind: 'trapezoid' | 'waveform' | 'spectrum'
  document: unknown
  created_at: string
  updated_at: string
}

/**
 * What a compliance run is asked. The solve and the driver are named, not sent: the server
 * checks the solve belongs to this project and board, and the worker reads everything that
 * decides the answer from it. The rest is what only the user knows, and can only add to what
 * the estimate needs (a connector declared as carrying no cable is recorded as a decision).
 */
export interface ComplianceParams {
  standard_id: string
  solve_run_id?: string
  driver_id?: string
  /** Connector -> cable, where a type of "none" means the product never cables it. */
  cable_assignments?: Record<string, { type: string | null; length_m?: number }>
  power?: 'dc' | 'mains' | ''
  enclosure?: 'none' | 'plastic' | 'metal'
  source_nets?: string[]
  /** A higher frequency than the driver's clock, if the product has one. */
  highest_frequency_hz?: number
  /** Rule findings, which recommendations are drawn from. Never part of the gate. */
  findings?: Record<string, unknown>[]
}

export interface Board {
  id: string
  project_id: string
  s3_input_key: string
  s3_board_key?: string
  layer_count: number
  net_count: number
  content_sha256?: string
  size_bytes?: number
  created_at: string
}

/**
 * Who the server resolved this request to be.
 *
 * The page is public, so this has an answer either way and the difference matters to the
 * user: signed in, their boards are filed under their own organisation and will be there
 * next week; anonymous, they go to a space of this visitor's own, which lasts only as long
 * as the browser keeps its visitor cookie.
 */
export interface Identity {
  user_id: string
  organization_id: string
  login?: string
  anonymous: boolean
}

/**
 * Experimental parts of the analyzer, as the server has them. Off is enforced by the server --
 * a gated run is refused, not just hidden -- so the UI reads this only to avoid offering it.
 *
 * full_wave: openEMS solves and everything built on them (hotspot maps, far field, cable
 * emissions, the compliance estimate). Off by default: nothing about it is verified yet.
 */
export interface Features {
  full_wave: boolean
}

/** The answer to "have I uploaded this file before?". */
export interface BoardLookup {
  found: boolean
  board?: Board
  project?: Project
  /** False for a board whose ingest never finished — linking there shows a blank page. */
  parsed?: boolean
}

/**
 * SHA-256 of a file, as lowercase hex, using the browser's own crypto.
 *
 * Reads the whole file into memory, which is what `File.arrayBuffer()` does anyway on the
 * upload path that follows. Returns undefined rather than throwing when SubtleCrypto is
 * unavailable — it needs a secure context, so plain http on a LAN address has none, and
 * the right behaviour there is to upload as before rather than to fail.
 */
export async function hashFile(file: File): Promise<string | undefined> {
  if (!globalThis.crypto?.subtle) return undefined
  try {
    const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('')
  } catch {
    return undefined
  }
}

export interface RunProgress {
  stage: string
  pct: number
  message?: string
  cells?: number
  timestep?: number
  total_timesteps?: number
  energy_db?: number
  at?: string
}

export interface Run {
  id: string
  project_id: string
  board_id?: string
  kind: RunKind
  status: RunStatus
  params?: unknown
  estimate?: Estimate
  progress?: RunProgress
  summary?: Record<string, unknown>
  error?: string
  created_at: string
  updated_at: string
  finished_at?: string
}

export interface WorkerInfo {
  name: string
  online: boolean
  capabilities: {
    cores: number
    ram_gb: number
    max_cells: number
    openems_version?: string
    tools?: string[]
    kinds?: RunKind[]
  }
  last_seen_at?: string
}

export interface ArtifactRef {
  id: string
  run_id: string
  name: string
  content_type: string
  size_bytes: number
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message)
  }
}

/**
 * Raised when the server refuses a solve because no connected worker can take it.
 *
 * Carried as its own type because the UI has something specific and useful to say: which
 * limit was hit and by how much, rather than "request failed".
 */
export class RunTooLargeError extends ApiError {
  constructor(
    public estimatedCells: number,
    public largestMaxCells: number,
    public workersOnline: number,
    message: string,
  ) {
    super(422, message)
  }
}

/**
 * The message for a download from storage that did not succeed.
 *
 * A presigned URL fails in ways the API never sees: it expires, or the object behind it was
 * removed. Parsing the error page as the artifact is what used to surface as a RangeError or
 * a SyntaxError, which says nothing about what to do.
 */
export function downloadErrorMessage(what: string, status: number): string {
  if (status === 403) return `The link to ${what} has expired. Reload the page to get a new one.`
  if (status === 404) return `${what} is missing from storage.`
  return `${what} could not be downloaded (HTTP ${status}).`
}

/**
 * GET a presigned URL, and throw an {@link ApiError} carrying the status unless it answers
 * 2xx. `what` names the file in the message.
 */
export async function fetchOk(fetchImpl: typeof fetch, url: string, what: string): Promise<Response> {
  const res = await fetchImpl(url)
  if (!res.ok) throw new ApiError(res.status, downloadErrorMessage(what, res.status))
  return res
}

export interface EmiApiOptions {
  /** Base path the emi package is mounted under. Matches `emi.Mount(mux, prefix, ...)`. */
  baseUrl?: string
  /** Extra headers, e.g. the app's Authorization bearer. */
  headers?: () => Record<string, string>
  fetchImpl?: typeof fetch
}

export class EmiApi {
  private base: string
  private headers: () => Record<string, string>
  private fetchImpl: typeof fetch

  constructor(opts: EmiApiOptions = {}) {
    this.base = (opts.baseUrl ?? '/api').replace(/\/$/, '')
    this.headers = opts.headers ?? (() => ({}))
    this.fetchImpl = opts.fetchImpl ?? ((...a) => fetch(...a))
  }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await this.fetchImpl(`${this.base}${path}`, {
      method,
      // Never answer from the browser's HTTP cache. Several replies carry presigned URLs that
      // expire in minutes, and the server's no-store only governs replies from now on: before
      // it, Cloudflare stamped a 4-hour browser max-age on artifact URLs ending in ".bin", so
      // a browser kept reusing an expired geometry URL no matter how often the page reloaded.
      cache: 'no-store',
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...this.headers(),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })

    if (!res.ok) {
      let parsed: Record<string, unknown> = {}
      try {
        parsed = await res.json()
      } catch {
        /* a non-JSON error body is still an error */
      }
      const msg = String(parsed.error ?? `${method} ${path} failed with ${res.status}`)
      if (res.status === 422 && typeof parsed.estimated_cells === 'number') {
        throw new RunTooLargeError(
          parsed.estimated_cells as number,
          (parsed.largest_max_cells as number) ?? 0,
          (parsed.workers_online as number) ?? 0,
          msg,
        )
      }
      throw new ApiError(res.status, msg, parsed)
    }
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }

  // ---- projects ----

  listProjects = () =>
    this.call<{ projects: Project[] }>('GET', '/emi/projects').then((r) => r.projects ?? [])

  createProject = (name: string, sourceKind: 'kicad' | 'gerber' = 'kicad') =>
    this.call<Project>('POST', '/emi/projects', { name, source_kind: sourceKind })

  getProject = (id: string) => this.call<Project>('GET', `/emi/projects/${id}`)

  renameProject = (id: string, name: string) =>
    this.call<Project>('PATCH', `/emi/projects/${id}`, { name })

  /** Removes the project, its board file and every result on it. */
  deleteProject = (id: string) => this.call<void>('DELETE', `/emi/projects/${id}`)

  listBoards = (projectId: string) =>
    this.call<{ boards: Board[] }>('GET', `/emi/projects/${projectId}/boards`)
      .then((r) => r.boards ?? [])

  /** Removes one version of the project's board and every result on it. Not the last one. */
  deleteBoard = (projectId: string, boardId: string) =>
    this.call<void>('DELETE', `/emi/projects/${projectId}/boards/${boardId}`)

  // ---- components ----
  //
  // Not project-scoped: a component is a library entry that spans every board, which is the
  // whole reason saving one needs an account.

  listComponents = () =>
    this.call<{ components: StoredComponent[] }>('GET', '/emi/components')
      .then((r) => r.components ?? [])

  createComponent = (body: Record<string, unknown>) =>
    this.call<StoredComponent>('POST', '/emi/components', body)

  updateComponent = (id: string, body: Record<string, unknown>) =>
    this.call<StoredComponent>('PUT', `/emi/components/${id}`, body)

  shareComponent = (id: string, shared: boolean) =>
    this.call<{ id: string; shared: boolean }>('POST', `/emi/components/${id}/share`, { shared })

  deleteComponent = (id: string) => this.call<void>('DELETE', `/emi/components/${id}`)

  listDrivers = (projectId: string) =>
    this.call<{ drivers: StoredDriver[] }>('GET', `/emi/projects/${projectId}/drivers`)
      .then((r) => r.drivers ?? [])

  /**
   * POST the driver document as the request body, verbatim.
   *
   * Not wrapped in an envelope: the server stores the bytes that arrive, so that a document
   * BenchPod wrote or a CI diff reads stays byte-identical through a round trip. The cap is
   * the 2 MB of docs/emi-driver-format.md rather than the 1 MB the other control-plane calls use.
   */
  createDriver = (projectId: string, document: unknown) =>
    this.call<StoredDriver>('POST', `/emi/projects/${projectId}/drivers`, document)

  deleteDriver = (projectId: string, driverId: string) =>
    this.call<void>('DELETE', `/emi/projects/${projectId}/drivers/${driverId}`)

  /**
   * The project's runs, newest first. The server's default page is 50, which a project with
   * several versions outgrows: pass up to 200 to still see the older versions' runs.
   */
  listRuns = (projectId: string, limit?: number) =>
    this.call<{ runs: Run[] }>(
      'GET', `/emi/projects/${projectId}/runs${limit ? `?limit=${limit}` : ''}`,
    ).then((r) => r.runs ?? [])

  // ---- upload ----

  /**
   * Look for a board this organisation has already uploaded, by content hash.
   *
   * Asked before any bytes move, so the common case of re-opening a board you worked on
   * last week costs one small GET instead of a 12 MB upload and a re-parse.
   */
  lookupBoard = (sha256: string) =>
    this.call<BoardLookup>('GET', `/emi/boards/lookup?sha256=${encodeURIComponent(sha256)}`)

  /** Which experimental parts of the analyzer this server has switched on. */
  features = () => this.call<Features>('GET', '/emi/features')

  /** Who the server thinks is asking. See the note on {@link Identity}. */
  whoami = () => this.call<Identity>('GET', '/emi/whoami')

  /**
   * Upload a board file and queue its ingest run.
   *
   * Two steps by design: the server mints a presigned PUT, the browser sends the bytes
   * straight to object storage, and only then does the server hear about it. A 12 MB
   * KiCad file never touches the control plane.
   *
   * Three, when the hash is known. Uploads are content-addressed, so the server can say
   * "I already have those bytes" and the PUT is skipped entirely — which is what happens
   * when the same board is added to a second project, or re-added after a failed ingest.
   */
  async uploadBoard(
    projectId: string,
    file: File,
    onProgress?: (fraction: number) => void,
    sha256?: string,
  ): Promise<{ board: Board; run: Run }> {
    const init = await this.call<{
      upload_url?: string; key: string; content_type: string; already_uploaded?: boolean
    }>(
      'POST', `/emi/projects/${projectId}/uploads`,
      {
        filename: file.name,
        content_type: file.type || 'application/octet-stream',
        sha256: sha256 ?? '',
        size_bytes: file.size,
      },
    )

    if (init.already_uploaded) {
      // Storage already holds exactly these bytes. Report the jump to 100% so the caller's
      // progress UI resolves rather than sitting at zero.
      onProgress?.(1)
    } else if (init.upload_url) {
      await putWithProgress(init.upload_url, file, init.content_type, onProgress)
    } else {
      throw new Error('the server offered neither an upload URL nor an existing object')
    }

    return this.call<{ board: Board; run: Run }>(
      'POST', `/emi/projects/${projectId}/boards`,
      { input_key: init.key, sha256: sha256 ?? '' },
    )
  }

  // ---- runs ----

  getRun = (runId: string) => this.call<Run>('GET', `/emi/runs/${runId}`)
  stopRun = (runId: string) => this.call<unknown>('POST', `/emi/runs/${runId}/stop`, {})
  retryRun = (runId: string) => this.call<Run>('POST', `/emi/runs/${runId}/retry`, {})

  /**
   * Analyse an existing board again, as a new ingest run.
   *
   * Needed whenever the worker learns to measure something new. The retry endpoint only
   * re-runs failed runs, and uploading the same file again is recognised by its hash and
   * opens the existing project — so without this, a board analysed last week could never
   * gain this week's columns.
   */
  reanalyse = (projectId: string, boardId: string) =>
    this.call<Run>('POST', `/emi/projects/${projectId}/runs`, { board_id: boardId, kind: 'ingest' })

  createSolveRun = (projectId: string, boardId: string, params: unknown, est: EstimateInput) =>
    this.call<Run>('POST', `/emi/projects/${projectId}/runs`, {
      board_id: boardId, kind: 'solve', params, estimate_input: est,
    })

  /**
   * An ESD simulation of every exposed line. On demand: part models are generic until the
   * user confirms them, so this never runs on its own.
   */
  /**
   * A Tier A cable budget. Seconds on any worker with nec2c, and no solve involved: a
   * cable's resonances depend on the cable, not the layout.
   */
  createCableRun = (
    projectId: string,
    boardId: string,
    connectors: Record<string, { type: string; length_m?: number } | string>,
    standardId = 'fcc-15b-radiated-3m',
  ) =>
    this.call<Run>('POST', `/emi/projects/${projectId}/runs`, {
      board_id: boardId, kind: 'cable',
      params: { connectors, standard_id: standardId },
    })

  /**
   * A compliance run: seconds of arithmetic on what other runs already produced
   * (docs/implementation.md §7).
   *
   * The params name the solve and the driver and carry what only the user knows. The worker
   * reads the solve's artifacts, the driver and the board itself, assembles the paths and
   * applies the completeness gate — deciding *here* whether the inputs are whole would put the
   * one rule that stops a number being published in the one place a client can skip.
   */
  createComplianceRun = (projectId: string, boardId: string, params: ComplianceParams) =>
    this.call<Run>('POST', `/emi/projects/${projectId}/runs`, {
      board_id: boardId, kind: 'compliance', params,
    })

  async fetchCompliance(runId: string): Promise<import('./complianceTypes').ComplianceDoc> {
    const ref = await this.artifactUrl(runId, 'compliance.json')
    const res = await this.fetchImpl(ref.url)
    if (!res.ok) throw new ApiError(res.status, `compliance.json failed with ${res.status}`)
    return (await res.json()) as import('./complianceTypes').ComplianceDoc
  }

  fetchCables = (runId: string) =>
    this.artifactJson<import('./cableTypes').CablesDoc>(runId, 'cables.json')

  createTransientRun = (projectId: string, boardId: string, params: import('./transientTypes').TransientParams) =>
    this.call<Run>('POST', `/emi/projects/${projectId}/runs`, {
      board_id: boardId, kind: 'transient', params,
    })

  /**
   * Upload a vendor SPICE model file. Through the same content-addressed endpoint as boards,
   * so the key stays inside this organisation's uploads — the only place the server lets a
   * transient run point at. The bytes are not inspected here; the worker vets the file before
   * ngspice sees it.
   */
  async uploadModelFile(projectId: string, file: File): Promise<{ key: string; filename: string }> {
    const sha256 = await hashFile(file)
    const init = await this.call<{
      upload_url?: string; key: string; content_type: string; already_uploaded?: boolean
    }>(
      'POST', `/emi/projects/${projectId}/uploads`,
      { filename: file.name, content_type: 'text/plain', sha256: sha256 ?? '', size_bytes: file.size },
    )
    if (!init.already_uploaded) {
      if (!init.upload_url) throw new Error('the server offered neither an upload URL nor an existing object')
      await putWithProgress(init.upload_url, file, init.content_type)
    }
    return { key: init.key, filename: file.name }
  }

  fetchTransient = (runId: string) =>
    this.artifactJson<import('./transientTypes').TransientDoc>(runId, 'transient.json')

  estimate = (input: EstimateInput) =>
    this.call<{ estimate: Estimate; eta_human: string; ram_gb: number }>(
      'POST', '/emi/estimate', input,
    )

  listWorkers = () =>
    this.call<{ workers: WorkerInfo[]; online_count: number }>('GET', '/emi/workers')

  // ---- artifacts ----

  listArtifacts = (runId: string) =>
    this.call<{ artifacts: ArtifactRef[] }>('GET', `/emi/runs/${runId}/artifacts`)
      .then((r) => r.artifacts ?? [])

  artifactUrl = (runId: string, name: string) =>
    this.call<{ url: string; content_type: string; size_bytes: number }>(
      'GET', `/emi/runs/${runId}/artifacts/${name}`,
    )

  /** Download one artifact, failing with an ApiError unless storage answers 2xx. */
  async artifactResponse(runId: string, name: string): Promise<Response> {
    const ref = await this.artifactUrl(runId, name)
    return fetchOk(this.fetchImpl, ref.url, name)
  }

  artifactJson = <T>(runId: string, name: string): Promise<T> =>
    this.artifactResponse(runId, name).then((r) => r.json() as Promise<T>)

  artifactBytes = (runId: string, name: string): Promise<ArrayBuffer> =>
    this.artifactResponse(runId, name).then((r) => r.arrayBuffer())

  /** Fetch board.json and geometry.bin together — the viewer needs both or neither. */
  async fetchBoard(runId: string): Promise<{ doc: BoardDoc; geometry: ArrayBuffer }> {
    const [doc, geometry] = await Promise.all([
      this.artifactJson<BoardDoc>(runId, 'board.json'),
      this.artifactBytes(runId, 'geometry.bin'),
    ])
    return { doc, geometry }
  }

  async fetchRules(runId: string): Promise<RulesDoc | null> {
    try {
      return await this.artifactJson<RulesDoc>(runId, 'rules.json')
    } catch (err) {
      // Rules are additive: a board that renders without findings is still useful, so a
      // missing rules.json must not take the viewer down with it.
      if (err instanceof ApiError && err.status === 404) return null
      throw err
    }
  }

  /**
   * The net report behind the CSV export. Null for a board analysed before the worker wrote
   * one, so the caller can fall back to what board.json has rather than failing.
   */
  async fetchNets(runId: string): Promise<NetReport | null> {
    try {
      return await this.artifactJson<NetReport>(runId, 'nets.json')
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return null
      throw err
    }
  }
}

/**
 * PUT with upload progress.
 *
 * `fetch` still cannot report request-body progress in any browser, and a 12 MB board on a
 * slow link is long enough that a progress bar is the difference between "working" and
 * "broken". XHR it is.
 */
function putWithProgress(
  url: string,
  file: File,
  contentType: string,
  onProgress?: (fraction: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', url)
    xhr.setRequestHeader('Content-Type', contentType)
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total)
      }
    }
    xhr.onload = () =>
      xhr.status < 300
        ? resolve()
        : reject(new ApiError(xhr.status, `upload failed with ${xhr.status}`))
    xhr.onerror = () => reject(new ApiError(0, 'upload failed: network error'))
    xhr.send(file)
  })
}

/**
 * Poll a run until it finishes.
 *
 * Polling rather than a websocket because a run's interesting output is its artifacts, and
 * the progress in between is a number that changes every couple of seconds. A socket would
 * be a second connection to keep alive for no extra information.
 */
export async function pollRun(
  api: EmiApi,
  runId: string,
  onUpdate: (run: Run) => void,
  opts: { intervalMs?: number; signal?: AbortSignal } = {},
): Promise<Run> {
  const interval = opts.intervalMs ?? 1500
  for (;;) {
    if (opts.signal?.aborted) throw new DOMException('aborted', 'AbortError')
    const run = await api.getRun(runId)
    onUpdate(run)
    if (TERMINAL_STATUSES.includes(run.status)) return run
    await new Promise((r) => setTimeout(r, interval))
  }
}
