/**
 * Comparing two versions of one board.
 *
 * A version is a board in a project: uploading a changed file into an existing project adds a
 * board to it, and every run names the board it was made on. Nothing here fetches anything;
 * it takes the artifacts of two versions and says what changed between them.
 *
 * Findings are the hard part. Their ids are positional (`plane-gap-3`), so the third plane-gap
 * finding of one version has nothing to do with the third of the next, and their titles carry
 * measurements that change with every edit. What survives an edit is the rule, the net, the
 * layer and roughly where it is, so that is what a finding is matched on.
 */

import type { NetReport, NetRow, RuleFinding } from './boardTypes'
import type { Board, Run } from './emiApi'
import type { CableBudgetPoint, CablesDoc } from './cableTypes'
import type { TransientDoc, TransientLine, TransientVariant } from './transientTypes'

// ---- versions ----

export interface Version {
  board: Board
  /** 1 for the first upload into the project. */
  number: number
  /** The uploaded file's name, from its storage key. */
  filename: string
}

/** The boards of a project as numbered versions, oldest first. */
export function versionsOf(boards: Board[]): Version[] {
  return [...boards]
    .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at) || a.id.localeCompare(b.id))
    .map((board, i) => ({
      board,
      number: i + 1,
      filename: board.s3_input_key.split('/').pop() || 'board',
    }))
}

/**
 * The pair a comparison opens on: the two newest versions, unless the URL named others.
 * Null when there are fewer than two versions.
 */
export function defaultPair(
  versions: Version[], before?: string | null, after?: string | null,
): [Version, Version] | null {
  if (versions.length < 2) return null
  const find = (id?: string | null) => (id ? versions.find((v) => v.board.id === id) : undefined)
  const b = find(after) ?? versions[versions.length - 1]
  const named = find(before)
  // A version compared with itself says nothing, so the same id twice falls back too.
  const a = named && named.board.id !== b.board.id
    ? named
    : [...versions].reverse().find((v) => v.board.id !== b.board.id)!
  return [a, b]
}

/**
 * The newest finished run of a kind on one board. An ingest that finished is what is on
 * screen for that board, the same choice the board page makes.
 */
export function latestDone(runs: Run[], boardId: string, kind: Run['kind']): Run | null {
  return (
    runs
      .filter((r) => r.board_id === boardId && r.kind === kind && r.status === 'done')
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0] ?? null
  )
}

// ---- findings ----

/** How far a finding may move between versions and still be the same finding, in mm. */
export const MATCH_TOLERANCE_MM = 2

export type FindingStatus = 'fixed' | 'new' | 'unchanged'

export interface FindingChange {
  status: FindingStatus
  rule: string
  /** In the older version. Absent for a new finding. */
  before?: RuleFinding
  /** In the newer version. Absent for a fixed one. */
  after?: RuleFinding
}

type Severity = RuleFinding['severity']
export type SeverityCounts = Record<Severity, number> & { total: number }

export interface FindingsDiff {
  changes: FindingChange[]
  counts: Record<FindingStatus, SeverityCounts>
}

function located(f: RuleFinding): boolean {
  return f.x != null && f.y != null
}

/**
 * How far apart two findings of one rule, net and layer are, or Infinity when they cannot be
 * the same finding. A finding with no place on the board (a summary) only matches another
 * without one, and a matching title makes the pair closer than any other.
 */
function distance(a: RuleFinding, b: RuleFinding, tol: number): number {
  if (located(a) !== located(b)) return Infinity
  if (!located(a)) return a.title === b.title ? 0 : tol
  return Math.hypot(a.x! - b.x!, a.y! - b.y!)
}

const groupKey = (f: RuleFinding) => `${f.rule}\u0000${f.net ?? ''}\u0000${f.layer ?? ''}`

/**
 * Pair the findings of two versions. Within each rule, net and layer, the closest pairs are
 * taken first, so two findings that swapped order in the file still match their own partner.
 * What pairs is unchanged, what is left over in the older version is fixed, and what is left
 * over in the newer one is new.
 */
export function diffFindings(
  before: RuleFinding[], after: RuleFinding[], tol = MATCH_TOLERANCE_MM,
): FindingsDiff {
  const groups = new Map<string, { a: RuleFinding[]; b: RuleFinding[] }>()
  const group = (k: string) => {
    let g = groups.get(k)
    if (!g) groups.set(k, (g = { a: [], b: [] }))
    return g
  }
  for (const f of before) group(groupKey(f)).a.push(f)
  for (const f of after) group(groupKey(f)).b.push(f)

  const changes: FindingChange[] = []
  for (const { a, b } of groups.values()) {
    const pairs: [number, number, number][] = []
    a.forEach((fa, i) => b.forEach((fb, j) => {
      const d = distance(fa, fb, tol)
      if (d <= tol) pairs.push([d, i, j])
    }))
    pairs.sort((p, q) => p[0] - q[0] || p[1] - q[1] || p[2] - q[2])
    const usedA = new Set<number>()
    const usedB = new Set<number>()
    for (const [, i, j] of pairs) {
      if (usedA.has(i) || usedB.has(j)) continue
      usedA.add(i)
      usedB.add(j)
      changes.push({ status: 'unchanged', rule: b[j].rule, before: a[i], after: b[j] })
    }
    a.forEach((f, i) => { if (!usedA.has(i)) changes.push({ status: 'fixed', rule: f.rule, before: f }) })
    b.forEach((f, j) => { if (!usedB.has(j)) changes.push({ status: 'new', rule: f.rule, after: f }) })
  }

  const empty = (): SeverityCounts => ({ critical: 0, warning: 0, info: 0, total: 0 })
  const counts: Record<FindingStatus, SeverityCounts> = { fixed: empty(), new: empty(), unchanged: empty() }
  for (const c of changes) {
    const sev = (c.after ?? c.before)!.severity
    counts[c.status][sev] += 1
    counts[c.status].total += 1
  }
  return { changes, counts }
}

const STATUS_ORDER: Record<FindingStatus, number> = { new: 0, fixed: 1, unchanged: 2 }
const SEVERITY_ORDER: Record<Severity, number> = { critical: 0, warning: 1, info: 2 }

/**
 * Changes grouped by rule. Rules with something new come first, then something fixed; inside
 * a rule, new before fixed before unchanged, worst first.
 */
export function groupByRule(changes: FindingChange[]): [string, FindingChange[]][] {
  const byRule = new Map<string, FindingChange[]>()
  for (const c of changes) {
    const list = byRule.get(c.rule)
    if (list) list.push(c)
    else byRule.set(c.rule, [c])
  }
  const sev = (c: FindingChange) => SEVERITY_ORDER[(c.after ?? c.before)!.severity]
  for (const list of byRule.values()) {
    list.sort((p, q) => STATUS_ORDER[p.status] - STATUS_ORDER[q.status] || sev(p) - sev(q))
  }
  const rank = (list: FindingChange[]) => Math.min(...list.map((c) => STATUS_ORDER[c.status]))
  return [...byRule.entries()].sort((p, q) => rank(p[1]) - rank(q[1]) || p[0].localeCompare(q[0]))
}

/** "3 fixed, 1 new", or "No change" when both are zero. */
export function findingsHeadline(counts: FindingsDiff['counts']): string {
  const parts: string[] = []
  if (counts.fixed.total) parts.push(`${counts.fixed.total} fixed`)
  if (counts.new.total) parts.push(`${counts.new.total} new`)
  return parts.length ? parts.join(', ') : 'No change'
}

// ---- nets ----

export interface NetChange {
  net: string
  status: 'changed' | 'added' | 'removed'
  /** The routed pad-to-pad length when there is one, otherwise all the copper on the net. */
  lengthBefore?: number
  lengthAfter?: number
  viasBefore?: number
  viasAfter?: number
}

/** Below this a length is the same length: rounding in the report, not an edit. */
export const LENGTH_EPSILON_MM = 0.01

const netLength = (r: NetRow) => r.path_mm ?? r.copper_mm
const netVias = (r: NetRow) => r.vias_total ?? r.path_vias ?? 0

/** Nets whose length or via count changed, and nets only one version has. */
export function diffNets(before: NetReport, after: NetReport): NetChange[] {
  const a = new Map(before.rows.map((r) => [r.net, r]))
  const b = new Map(after.rows.map((r) => [r.net, r]))
  const out: NetChange[] = []
  for (const [net, rb] of b) {
    const ra = a.get(net)
    if (!ra) {
      out.push({ net, status: 'added', lengthAfter: netLength(rb), viasAfter: netVias(rb) })
      continue
    }
    const la = netLength(ra)
    const lb = netLength(rb)
    const va = netVias(ra)
    const vb = netVias(rb)
    if (Math.abs(la - lb) > LENGTH_EPSILON_MM || va !== vb) {
      out.push({ net, status: 'changed', lengthBefore: la, lengthAfter: lb, viasBefore: va, viasAfter: vb })
    }
  }
  for (const [net, ra] of a) {
    if (!b.has(net)) out.push({ net, status: 'removed', lengthBefore: netLength(ra), viasBefore: netVias(ra) })
  }
  // The biggest length change first: that is the edit most likely to matter.
  const delta = (c: NetChange) => Math.abs((c.lengthAfter ?? 0) - (c.lengthBefore ?? 0))
  const order = { changed: 0, added: 1, removed: 2 }
  return out.sort((p, q) => order[p.status] - order[q.status] || delta(q) - delta(p) || p.net.localeCompare(q.net))
}

// ---- cables ----

export interface CableChange {
  ref: string
  before?: CableSide
  after?: CableSide
}

export interface CableSide {
  cable?: string
  /** Not modelled: nothing declared, or declared as never cabled. */
  note?: string
  tightest?: { frequency_hz: number; max_current_dbua: number } | null
  points?: CableBudgetPoint[]
}

function cableSides(doc: CablesDoc): Map<string, CableSide> {
  const out = new Map<string, CableSide>()
  for (const c of doc.cables) {
    out.set(c.ref, c.declared_none
      ? { note: 'Never cabled' }
      : {
          cable: [c.cable_name ?? c.cable_id, c.length_m != null ? `${c.length_m} m` : '']
            .filter(Boolean).join(', '),
          tightest: c.tightest ?? null,
          points: c.points,
        })
  }
  for (const u of doc.unassigned) if (!out.has(u.ref)) out.set(u.ref, { note: 'No cable set' })
  return out
}

/** Every connector either version has, in reference order. */
export function diffCables(before: CablesDoc, after: CablesDoc): CableChange[] {
  const a = cableSides(before)
  const b = cableSides(after)
  const refs = [...new Set([...a.keys(), ...b.keys()])]
  refs.sort((p, q) => p.localeCompare(q, undefined, { numeric: true }))
  return refs.map((ref) => ({ ref, before: a.get(ref), after: b.get(ref) }))
}

// ---- ESD ----

export interface EsdChange {
  key: string
  net: string
  connector: string
  /** Peak at the IC pin as laid out, or at the clamp or connector when there is no pin. */
  before?: EsdSide
  after?: EsdSide
}

export interface EsdSide {
  line: TransientLine
  volts?: number
  where: 'pin' | 'clamp' | 'connector'
}

/** The peak the ESD panel shows for a variant, and where it was measured. Same choice. */
export function peakOf(v: TransientVariant | undefined): { volts?: number; where: EsdSide['where'] } {
  if (!v) return { where: 'connector' }
  if (v.v_pin_peak_v !== undefined) return { volts: v.v_pin_peak_v, where: 'pin' }
  if (v.v_clamp_peak_v !== undefined) return { volts: v.v_clamp_peak_v, where: 'clamp' }
  return { volts: v.v_connector_peak_v, where: 'connector' }
}

const esdKey = (l: TransientLine) => `${l.connector.ref}:${l.connector.pad}:${l.net}`

function esdSide(l: TransientLine): EsdSide {
  const v = l.variants?.find((x) => x.id === 'as_laid_out')
  return { line: l, ...peakOf(v) }
}

/** Every exposed line either version simulated, matched on connector pin and net. */
export function diffEsd(before: TransientDoc, after: TransientDoc): EsdChange[] {
  const a = new Map(before.lines.map((l) => [esdKey(l), l]))
  const b = new Map(after.lines.map((l) => [esdKey(l), l]))
  const keys = [...new Set([...a.keys(), ...b.keys()])]
  return keys
    .map((key) => {
      const la = a.get(key)
      const lb = b.get(key)
      const l = (lb ?? la)!
      return {
        key, net: l.net, connector: `${l.connector.ref}.${l.connector.pad}`,
        before: la ? esdSide(la) : undefined, after: lb ? esdSide(lb) : undefined,
      }
    })
    .sort((p, q) => p.connector.localeCompare(q.connector, undefined, { numeric: true }))
}

/**
 * A waveform resampled onto another time grid, linearly. Two simulations need not share one,
 * and the chart draws every series against a single axis.
 */
export function resample(t: number[], values: number[], onto: number[]): number[] {
  if (t.length === 0) return onto.map(() => 0)
  let i = 0
  return onto.map((x) => {
    if (x <= t[0]) return values[0]
    if (x >= t[t.length - 1]) return values[t.length - 1]
    while (i < t.length - 2 && t[i + 1] < x) i++
    while (i > 0 && t[i] > x) i--
    const span = t[i + 1] - t[i]
    const f = span > 0 ? (x - t[i]) / span : 0
    return values[i] + f * (values[i + 1] - values[i])
  })
}
