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

/** Mirrors settings.snapshot() in the worker. */
export interface SettingsSnapshot {
  board: Record<string, Sourced>
  rules: Record<string, RuleSnapshot>
  groups: { match?: string; netclass?: string; params: Record<string, unknown> }[]
  suppress: { rule: string; net: string; reason: string }[]
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
}

export interface CatalogueRule {
  id: string
  title: string
  about: string
  category: string
  params: { key: string; default: number }[]
}

export interface BoardSetting {
  key: string
  default: number
  positive: boolean
}

export const RULES = catalogue as CatalogueRule[]
export const BOARD_SETTINGS = boardCatalogue as BoardSetting[]

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
  return { ...base, board, rules }
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

export function isEmptyLayer(layer: AppLayer): boolean {
  return !layer.board && !layer.rules
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
  if (s.groups.length) doc.groups = s.groups
  if (s.suppress.length) doc.suppress = s.suppress
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
