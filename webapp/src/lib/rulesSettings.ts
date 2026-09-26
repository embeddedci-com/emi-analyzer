/**
 * Rule settings edited in the app.
 *
 * The worker merges settings in layers, most general first: built-in defaults, project
 * settings, the emi.rules.yaml committed beside the board, then the run's own parameters
 * (worker/emi_worker/rules/settings.py). The app owns that last layer. It is saved as the
 * ingest run's `params.settings`, so a re-analysis is what applies it and the run is where
 * it survives a restart; nothing else stores it.
 *
 * rules.json reports the merged settings twice: `applied`, what the checks ran with, and
 * `base`, the same without the app's layer. The editor shows base overlaid with the layer
 * being edited, so clearing an edit shows the value underneath before anything re-runs.
 */

import catalogue from './ruleCatalogue.json'
import boardCatalogue from './boardSettings.json'

export const SEVERITIES = ['critical', 'warning', 'info'] as const
export type Severity = (typeof SEVERITIES)[number]

export type SettingSource = 'default' | 'project' | 'file' | 'run'

export interface Sourced {
  value: number
  source: SettingSource
}

export interface RuleSnapshot {
  enabled: boolean
  enabled_source: SettingSource
  /** An override of the severity the rule would give; empty for its own. */
  severity: string
  severity_source: SettingSource
  params: Record<string, Sourced>
}

/**
 * Overrides for the nets a pattern matches (emi.rules.yaml `groups`). Only the parameters a
 * rule reads per net can be set here; the catalogue marks them `per_net`.
 */
export interface NetGroup {
  match?: string
  netclass?: string
  params: Record<string, number>
}

/** A finding somebody has decided about (emi.rules.yaml `suppress`). */
export interface Suppression {
  rule: string
  net: string
  reason: string
}

/** A group or suppression as rules.json reports it, with the layer that declared it. */
export type WithSource<T> = T & { source?: SettingSource }

/** Mirrors settings.snapshot() in the worker. */
export interface SettingsSnapshot {
  board: Record<string, Sourced>
  rules: Record<string, RuleSnapshot>
  groups: WithSource<NetGroup>[]
  suppress: WithSource<Suppression>[]
  cables: Record<string, unknown>
  cable_solver: string
}

/** rules.json `settings`. */
export interface RulesSettingsReport {
  /** The rules file that was applied; empty for none. */
  file: string
  /** Why a rules file that was found was not used. */
  file_error: string
  applied: SettingsSnapshot
  base: SettingsSnapshot
}

/** The app's layer, in the same shape as emi.rules.yaml so the worker reads it unchanged. */
export interface AppLayer {
  board?: Record<string, number>
  rules?: Record<string, { enabled?: boolean; severity?: Severity; params?: Record<string, number> }>
  groups?: NetGroup[]
  suppress?: Suppression[]
}

export interface CatalogueRule {
  id: string
  title: string
  about: string
  category: string
  params: { key: string; default: number; per_net?: boolean }[]
}

export interface BoardSetting {
  key: string
  default: number
  positive: boolean
}

export const RULES = catalogue as CatalogueRule[]
export const BOARD_SETTINGS = boardCatalogue as BoardSetting[]

/** What a net group can set: the parameters a rule reads per net, with the rule they belong to. */
export const PER_NET_PARAMS = RULES.flatMap((r) =>
  r.params.filter((p) => p.per_net).map((p) => ({ rule: r, key: p.key, default: p.default })))

/** Settings as they are with no rules file: what an old rules.json without a report means. */
export function defaultSnapshot(): SettingsSnapshot {
  return {
    board: Object.fromEntries(
      BOARD_SETTINGS.map((b) => [b.key, { value: b.default, source: 'default' as const }]),
    ),
    rules: Object.fromEntries(RULES.map((r) => [r.id, {
      enabled: true, enabled_source: 'default' as const,
      severity: '', severity_source: 'default' as const,
      params: Object.fromEntries(
        r.params.map((p) => [p.key, { value: p.default, source: 'default' as const }]),
      ),
    }])),
    groups: [], suppress: [], cables: {}, cable_solver: 'nec2c',
  }
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

/**
 * The app's layer from an ingest run's params. Anything the editor would not have written
 * is dropped rather than trusted: this goes straight back to the worker on the next save.
 */
export function layerFromParams(params: unknown): AppLayer {
  const s = isObject(params) ? params.settings : undefined
  if (!isObject(s)) return {}
  const out: AppLayer = {}
  if (isObject(s.board)) {
    for (const [k, v] of Object.entries(s.board)) {
      if (typeof v === 'number' && BOARD_SETTINGS.some((b) => b.key === k)) {
        (out.board ??= {})[k] = v
      }
    }
  }
  if (isObject(s.rules)) {
    for (const [id, spec] of Object.entries(s.rules)) {
      const rule = RULES.find((r) => r.id === id)
      if (!rule || !isObject(spec)) continue
      const entry: NonNullable<AppLayer['rules']>[string] = {}
      if (typeof spec.enabled === 'boolean') entry.enabled = spec.enabled
      if (SEVERITIES.includes(spec.severity as Severity)) entry.severity = spec.severity as Severity
      if (isObject(spec.params)) {
        for (const [k, v] of Object.entries(spec.params)) {
          if (typeof v === 'number' && rule.params.some((p) => p.key === k)) {
            (entry.params ??= {})[k] = v
          }
        }
      }
      if (Object.keys(entry).length) (out.rules ??= {})[id] = entry
    }
  }
  if (Array.isArray(s.groups)) {
    for (const g of s.groups) {
      if (!isObject(g) || typeof g.match !== 'string' || !isObject(g.params)) continue
      const params: Record<string, number> = {}
      for (const [k, v] of Object.entries(g.params)) {
        if (typeof v === 'number' && PER_NET_PARAMS.some((p) => p.key === k)) params[k] = v
      }
      if (Object.keys(params).length) (out.groups ??= []).push({ match: g.match, params })
    }
  }
  if (Array.isArray(s.suppress)) {
    for (const x of s.suppress) {
      if (!isObject(x) || typeof x.rule !== 'string' || typeof x.net !== 'string') continue
      ;(out.suppress ??= []).push({
        rule: x.rule, net: x.net, reason: typeof x.reason === 'string' ? x.reason : '',
      })
    }
  }
  return out
}

/** Base with the app's layer on top: what the next analysis will run with. */
export function overlay(base: SettingsSnapshot, layer: AppLayer): SettingsSnapshot {
  const board = { ...base.board }
  for (const [k, v] of Object.entries(layer.board ?? {})) board[k] = { value: v, source: 'run' }
  const rules: Record<string, RuleSnapshot> = {}
  for (const [id, r] of Object.entries(base.rules)) {
    const l = layer.rules?.[id]
    const params = { ...r.params }
    for (const [k, v] of Object.entries(l?.params ?? {})) params[k] = { value: v, source: 'run' }
    rules[id] = {
      ...r,
      ...(l?.enabled !== undefined ? { enabled: l.enabled, enabled_source: 'run' as const } : {}),
      ...(l?.severity ? { severity: l.severity, severity_source: 'run' as const } : {}),
      params,
    }
  }
  const run = { source: 'run' as const }
  return {
    ...base, board, rules,
    groups: [...base.groups, ...(layer.groups ?? []).map((g) => ({ ...g, ...run }))],
    suppress: [...base.suppress, ...(layer.suppress ?? []).map((x) => ({ ...x, ...run }))],
  }
}

/** Drop what the layer sets to the value already underneath, and anything left empty. */
function prune(layer: AppLayer, base: SettingsSnapshot): AppLayer {
  const out: AppLayer = {}
  for (const [k, v] of Object.entries(layer.board ?? {})) {
    if (base.board[k]?.value !== v) (out.board ??= {})[k] = v
  }
  for (const [id, l] of Object.entries(layer.rules ?? {})) {
    const b = base.rules[id]
    if (!b) continue
    const entry: NonNullable<AppLayer['rules']>[string] = {}
    if (l.enabled !== undefined && l.enabled !== b.enabled) entry.enabled = l.enabled
    if (l.severity && l.severity !== b.severity) entry.severity = l.severity
    for (const [k, v] of Object.entries(l.params ?? {})) {
      if (b.params[k]?.value !== v) (entry.params ??= {})[k] = v
    }
    if (Object.keys(entry).length) (out.rules ??= {})[id] = entry
  }
  if (layer.groups?.length) out.groups = layer.groups
  if (layer.suppress?.length) out.suppress = layer.suppress
  return out
}

type RuleEdit = { enabled?: boolean | null; severity?: Severity | null; param?: [string, number | null] }

/** One edit to a rule. `null` clears the app's value, showing the one underneath again. */
export function editRule(layer: AppLayer, base: SettingsSnapshot, id: string, edit: RuleEdit): AppLayer {
  const cur = { ...(layer.rules?.[id] ?? {}) }
  if (edit.enabled !== undefined) {
    if (edit.enabled === null) delete cur.enabled
    else cur.enabled = edit.enabled
  }
  if (edit.severity !== undefined) {
    if (edit.severity === null) delete cur.severity
    else cur.severity = edit.severity
  }
  if (edit.param) {
    const [k, v] = edit.param
    const params = { ...(cur.params ?? {}) }
    if (v === null) delete params[k]
    else params[k] = v
    cur.params = params
  }
  return prune({ ...layer, rules: { ...(layer.rules ?? {}), [id]: cur } }, base)
}

export function editBoard(layer: AppLayer, base: SettingsSnapshot, key: string, value: number | null): AppLayer {
  const board = { ...(layer.board ?? {}) }
  if (value === null) delete board[key]
  else board[key] = value
  return prune({ ...layer, board }, base)
}

/** The app's net groups replaced wholesale; the rules file's are not the app's to change. */
export function setGroups(layer: AppLayer, base: SettingsSnapshot, groups: NetGroup[]): AppLayer {
  return prune({ ...layer, groups }, base)
}

export function setSuppressions(layer: AppLayer, base: SettingsSnapshot, suppress: Suppression[]): AppLayer {
  return prune({ ...layer, suppress }, base)
}

/** The layer with one more suppression, unless the same one already applies. */
export function addSuppression(layer: AppLayer, base: SettingsSnapshot, s: Suppression): AppLayer {
  const key = (x: Suppression) => [x.rule, x.net, x.reason].join('\u0000')
  if ([...base.suppress, ...(layer.suppress ?? [])].some((x) => key(x) === key(s))) return layer
  return setSuppressions(layer, base, [...(layer.suppress ?? []), s])
}

export function isEmptyLayer(layer: AppLayer): boolean {
  return !layer.board && !layer.rules && !layer.groups && !layer.suppress
}

// ---- net patterns ----

/**
 * A net pattern as a RegExp, with the worker's semantics (Python fnmatchcase): `*` any run,
 * `?` one character, `[abc]` and `[!abc]` a set. Case-sensitive, as net names are.
 */
export function globToRegExp(pattern: string): RegExp {
  let re = ''
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i]
    if (c === '*') re += '.*'
    else if (c === '?') re += '.'
    else if (c === '[') {
      let j = i + 1
      if (pattern[j] === '!') j++
      if (pattern[j] === ']') j++
      while (j < pattern.length && pattern[j] !== ']') j++
      if (j >= pattern.length) {
        re += '\\['
        continue
      }
      // A leading ] is part of the set; JavaScript would read [] as an empty one.
      let body = pattern.slice(i + 1, j).replace(/\\/g, '\\\\').replace(/]/g, '\\]')
      if (body.startsWith('!')) body = '^' + body.slice(1)
      else if (body.startsWith('^')) body = '\\' + body
      re += `[${body}]`
      i = j
    } else re += c.replace(/[.+^${}()|\\/\]]/g, '\\$&')
  }
  return new RegExp(`^${re}$`, 's')
}

export function matchNets(pattern: string, nets: string[]): string[] {
  const re = globToRegExp(pattern)
  return nets.filter((n) => re.test(n))
}

/** "CLK, DATA and 3 more": the start of a list of net names, for a preview line. */
export function netList(nets: string[], shown = 4): string {
  if (nets.length <= shown) return nets.join(', ')
  return `${nets.slice(0, shown).join(', ')} and ${nets.length - shown} more`
}

// ---- validation, as the worker does it ----

export type GroupErrors = { pattern?: string; params?: string; values: Record<string, string> }

/**
 * A net group as the editor holds it, with the typed text of each value, checked the way the
 * worker checks it (settings.py `_apply`): only per-net parameters, each a valid number.
 */
export function checkGroup(pattern: string, values: [string, string][]):
  { group: NetGroup } | { errors: GroupErrors } {
  const errors: GroupErrors = { values: {} }
  if (!pattern.trim()) errors.pattern = 'Enter a net name or pattern'
  if (!values.length) errors.params = 'Add at least one setting'
  const params: Record<string, number> = {}
  for (const [key, text] of values) {
    if (!PER_NET_PARAMS.some((p) => p.key === key)) {
      errors.values[key] = 'Not a per-net setting'
      continue
    }
    if (key in params) {
      errors.values[key] = 'Set twice'
      continue
    }
    const parsed = parseSetting(text)
    if ('error' in parsed) errors.values[key] = parsed.error
    else params[key] = parsed.value
  }
  if (errors.pattern || errors.params || Object.keys(errors.values).length) return { errors }
  return { group: { match: pattern.trim(), params } }
}

export type SuppressionErrors = Partial<Record<'rule' | 'net' | 'reason', string>>

/** A suppression checked the way the worker warns about it: a known rule, and a reason. */
export function checkSuppression(s: Suppression): { suppression: Suppression } | { errors: SuppressionErrors } {
  const errors: SuppressionErrors = {}
  const re = globToRegExp(s.rule.trim() || '*')
  if (!s.rule.trim()) errors.rule = 'Pick a check'
  else if (!RULES.some((r) => re.test(r.id))) errors.rule = 'No check has this id'
  if (!s.net.trim()) errors.net = 'Enter a net name or pattern'
  if (!s.reason.trim()) errors.reason = 'Say why, for whoever reads this next'
  if (Object.keys(errors).length) return { errors }
  return { suppression: { rule: s.rule.trim(), net: s.net.trim(), reason: s.reason.trim() } }
}

/** Same edits, whatever the key order. */
export function sameLayer(a: AppLayer, b: AppLayer): boolean {
  const norm = (l: AppLayer): string => JSON.stringify(l, (_k, v) =>
    isObject(v) ? Object.fromEntries(Object.entries(v).sort(([x], [y]) => x.localeCompare(y))) : v)
  return norm(a) === norm(b)
}

/**
 * A typed value checked the way the worker checks it (settings.check_value), so a bad value
 * is refused here with the same reason rather than after a re-analysis.
 */
export function parseSetting(
  input: string, opts: { positive?: boolean; key?: string } = {},
): { value: number } | { error: string } {
  const t = input.trim()
  if (!t) return { error: 'Enter a number' }
  const n = Number(t)
  if (!Number.isFinite(n)) return { error: 'Must be a number' }
  if (n < 0) return { error: 'Must be zero or more' }
  if (opts.positive && n === 0) return { error: 'Must be above zero' }
  if (opts.key === 'epsilon_r' && n > 0 && n < 1) return { error: 'Must be 1 or more, or 0 for the board file\'s' }
  return { value: n }
}

const UNITS: [RegExp, string][] = [
  [/_mm2$/, 'mm²'], [/_mm$/, 'mm'], [/_ps$/, 'ps'], [/_pct$/, '%'], [/_ohm$/, 'Ω'],
  [/_hz$/, 'Hz'], [/_m$/, 'm'],
]

/** "max_distance_mm" as a label and a unit: "Max distance", "mm". */
export function paramLabel(key: string): { label: string; unit: string } {
  const [re, unit] = UNITS.find(([r]) => r.test(key)) ?? [/$^/, '']
  const words = key.replace(re, '').replace(/_/g, ' ')
  return { label: words.charAt(0).toUpperCase() + words.slice(1), unit }
}

export const SOURCE_LABEL: Record<SettingSource, string> = {
  default: 'default',
  project: 'project',
  file: 'rules file',
  run: 'this app',
}

// ---- export ----

/**
 * The settings as an emi.rules.yaml document: every value that is not a built-in default,
 * whichever layer set it, plus the groups, suppressions and cables the rules file carried.
 * Uploaded beside the board it gives the same analysis with no app layer at all.
 */
export function toRulesDocument(s: SettingsSnapshot): Record<string, unknown> {
  const doc: Record<string, unknown> = { version: 1 }
  const board = Object.fromEntries(
    Object.entries(s.board).filter(([, v]) => v.source !== 'default').map(([k, v]) => [k, v.value]),
  )
  if (Object.keys(board).length) doc.board = board

  const rules: Record<string, unknown> = {}
  for (const [id, r] of Object.entries(s.rules)) {
    const entry: Record<string, unknown> = {}
    if (r.enabled_source !== 'default' || !r.enabled) entry.enabled = r.enabled
    if (r.severity) entry.severity = r.severity
    const params = Object.fromEntries(
      Object.entries(r.params).filter(([, v]) => v.source !== 'default').map(([k, v]) => [k, v.value]),
    )
    if (Object.keys(params).length) entry.params = params
    // A rule that is only switched off reads best the way the docs write it.
    if (Object.keys(entry).length === 1 && entry.enabled === false) rules[id] = false
    else if (Object.keys(entry).length) rules[id] = entry
  }
  if (Object.keys(rules).length) doc.rules = rules
  // Where each came from is for the app to show; the file it is written to is the source.
  if (s.groups.length) doc.groups = s.groups.map(({ source: _, ...g }) => g)
  if (s.suppress.length) doc.suppress = s.suppress.map(({ source: _, ...x }) => x)
  const connectors = Object.keys(s.cables ?? {}).length
  if (connectors || (s.cable_solver && s.cable_solver !== 'nec2c')) {
    doc.cables = {
      ...(s.cable_solver !== 'nec2c' ? { solver: s.cable_solver } : {}),
      ...(connectors ? { connectors: s.cables } : {}),
    }
  }
  return doc
}

function scalar(v: unknown): string {
  if (typeof v === 'number') {
    // PyYAML reads 1e-7 as a string; it wants the mantissa to have a dot.
    const t = String(v)
    return /e/i.test(t) && !t.includes('.') ? t.replace(/e/i, '.0e') : t
  }
  if (typeof v === 'boolean' || v === null) return String(v)
  const t = String(v)
  // A plain word stays plain ("severity: info"). Anything else is quoted as JSON, which is
  // valid YAML, so no value is read back as another type ("no", "1e3", "a: b").
  if (/^[A-Za-z][\w.-]*$/.test(t) && !/^(y|n|yes|no|on|off|true|false|null)$/i.test(t)) return t
  return JSON.stringify(t)
}

const key = (k: string) => (/^[A-Za-z_][\w.-]*$/.test(k) ? k : JSON.stringify(k))

function emit(v: unknown, indent: string): string[] {
  if (Array.isArray(v)) {
    if (!v.length) return []
    return v.flatMap((item) => {
      const lines = isObject(item) || Array.isArray(item) ? emit(item, indent + '  ') : null
      if (!lines) return [`${indent}- ${scalar(item)}`]
      if (!lines.length) return [`${indent}- {}`]
      return [`${indent}- ${lines[0].trimStart()}`, ...lines.slice(1)]
    })
  }
  if (isObject(v)) {
    return Object.entries(v).flatMap(([k, val]) => {
      if (isObject(val) || Array.isArray(val)) {
        const inner = emit(val, indent + '  ')
        if (!inner.length) return [`${indent}${key(k)}: ${Array.isArray(val) ? '[]' : '{}'}`]
        return [`${indent}${key(k)}:`, ...inner]
      }
      return [`${indent}${key(k)}: ${scalar(val)}`]
    })
  }
  return [`${indent}${scalar(v)}`]
}

/** A small YAML writer: mappings, lists and scalars, which is all a rules document holds. */
export function toYaml(doc: Record<string, unknown>): string {
  return emit(doc, '').join('\n') + '\n'
}

export function rulesFileText(s: SettingsSnapshot): string {
  return [
    '# EMI Analyzer rules. Put this file next to the .kicad_pcb and upload the project as a zip.',
    '# Reference: docs/rules-file.md',
    toYaml(toRulesDocument(s)),
  ].join('\n')
}
