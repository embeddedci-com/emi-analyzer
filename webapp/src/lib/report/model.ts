/**
 * The content of a shared report, assembled from what the board page already has.
 *
 * One pure step between the artifacts and the two formats: the HTML report and the JSON
 * export read the same {@link ReportData}, so they can never disagree about what was found.
 * Nothing here fetches or draws; the board image arrives already rendered (boardImage.ts).
 */

import type { BoardDoc, NetReport, RuleFinding, RulesDoc, StackupEntry } from '../boardTypes'
import type { CableBudgetPoint, CablesDoc } from '../cableTypes'
import type { ComplianceDoc } from '../complianceTypes'
import type { Features, Run } from '../emiApi'
import type { TransientDoc, TransientVariantId } from '../transientTypes'
import {
  diffCables, diffEsd, diffFindings, diffNets, findingsHeadline, groupByRule, peakOf,
  type FindingChange,
} from '../compare'
import { collectNotices, type Notice } from '../notices'
import { RULES, type SettingSource } from '../rulesSettings'
import { EXPERIMENTAL } from '../../components/Experimental'

export const REPORT_FORMAT = 'emi-report'
export const REPORT_FORMAT_VERSION = 1

export type SectionId = 'board' | 'findings' | 'notes' | 'cables' | 'esd' | 'changes' | 'experimental'

export const SECTIONS: { id: SectionId; label: string }[] = [
  { id: 'board', label: 'Board image with finding markers' },
  { id: 'findings', label: 'Findings' },
  { id: 'notes', label: 'Analysis notes' },
  { id: 'cables', label: 'Cable budgets' },
  { id: 'esd', label: 'ESD simulation' },
  { id: 'changes', label: 'Changes since an earlier version' },
  { id: 'experimental', label: 'Experimental results' },
]

/** The plain statement every report carries, near the top and on every printed page. */
export const DISCLAIMER = {
  title: 'Not a pre-compliance test',
  text:
    'These results are for comparing versions of your own board. They are not a pass or fail ' +
    'result, not a pre-scan, and no replacement for testing at a test house. The checks are ' +
    'geometric and the simulations use generic models unless a vendor model is named, so ' +
    'absolute numbers are estimates. What holds is the comparison between versions and ' +
    'variants of the same board.',
  short: 'Compares versions of your own board. Not a pre-compliance test.',
}

/** The board image, as rendered at report time, and where board space lands on it. */
export interface BoardImage {
  /** A PNG data URL. Anything else is dropped rather than put into the report. */
  src: string
  widthPx: number
  heightPx: number
}

export interface ReportInput {
  projectName: string
  version: { number: number; count: number; filename: string; uploadedAt: string; sha256?: string }
  /** The ingest run whose results these are. */
  ingest: Run
  board: BoardDoc
  rules: RulesDoc | null
  features: Features
  appVersion?: string
  generatedAt: Date
  sections: SectionId[]
  boardImage?: BoardImage | null
  cables?: { run: Run; doc: CablesDoc } | null
  esd?: { run: Run; doc: TransientDoc } | null
  /** An earlier version's results, for "changes since". Each part is compared only when both have it. */
  compare?: {
    versionNumber: number
    rulesBefore: RulesDoc | null
    netsBefore?: NetReport | null
    netsAfter?: NetReport | null
    cablesBefore?: CablesDoc | null
    esdBefore?: TransientDoc | null
  } | null
  experimental?: {
    compliance?: { run: Run; doc: ComplianceDoc } | null
    /** Finished solve runs (full-wave or small-part), listed but not drawn. */
    solves?: Run[]
  } | null
}

export interface ReportFinding {
  /** The marker number on the board image, for findings that have a place. */
  marker: number | null
  id: string
  rule: string
  severity: RuleFinding['severity']
  title: string
  detail: string
  net?: string
  layer?: string
  x?: number
  y?: number
}

export interface RuleGroup {
  rule: string
  ruleTitle: string
  about?: string
  findings: ReportFinding[]
}

export interface SeverityGroup {
  severity: RuleFinding['severity']
  count: number
  rules: RuleGroup[]
}

export interface SettingLine {
  what: string
  value: string
  source: SettingSource
}

export interface CableSection {
  standard: string
  modelled: {
    ref: string
    cable: string
    lengthM?: number
    farEnd?: string
    shield?: string
    tightest: { frequencyHz: number; maxCurrentDbua: number; limitDbuvPerM: number } | null
    peaksHz: number[]
    gridTooCoarse: boolean
    points: CableBudgetPoint[]
  }[]
  neverCabled: string[]
  unassigned: { ref: string; footprint: string; reason: string }[]
  assumptions: string[]
  notes: string[]
}

export interface EsdVariantRow {
  id: TransientVariantId
  label: string
  pinPeakV?: number
  clampPeakV?: number
  connectorPeakV: number
  unclamped: boolean
  worstPolarity: string
}

export interface EsdLineSection {
  net: string
  connector: string
  x: number
  y: number
  clamp: { ref: string; part: string; model: string; source: string } | null
  seriesResistor: string | null
  ic: string | null
  peak: { volts?: number; where: 'pin' | 'clamp' | 'connector' }
  /** "Moving the clamp to the connector" or "a clamp at the connector", with its peak. */
  comparison: { what: string; volts?: number } | null
  unclamped: boolean
  error?: string
  variants: EsdVariantRow[]
  tNs: number[]
  waveforms: { id: TransientVariantId; label: string; values: number[] }[]
  notes: string[]
}

export interface EsdSection {
  standard: string
  kv: number
  level: number
  polarities: string[]
  sourceCheck: string[]
  referenceClamp: string
  lines: EsdLineSection[]
  modelsRejected: { part: string; reasons: string[] }[]
  assumptions: string[]
  notes: string[]
}

export interface ChangesSection {
  sinceVersion: number
  findings: {
    headline: string
    counts: { fixed: number; new: number; unchanged: number }
    byRule: { rule: string; ruleTitle: string; changes: { status: 'new' | 'fixed'; severity: string; title: string; net?: string }[] }[]
  } | null
  nets: { net: string; status: string; lengthBefore?: number; lengthAfter?: number; viasBefore?: number; viasAfter?: number }[] | null
  netsTotal: number
  cables: { ref: string; before: string; after: string }[] | null
  esd: { connector: string; net: string; beforeV?: number; afterV?: number }[] | null
}

export interface ExperimentalSection {
  why: string
  compliance: {
    standard: string
    complete: boolean
    marginDb?: number
    worst?: { frequencyHz: number; fieldDbuvPerM: number; limitDbuvPerM: number }
    gaps: string[]
    notes: string[]
    uncertainty: string
    why: string
  } | null
  solves: { id: string; kind: string; finishedAt?: string }[]
}

export interface ReportData {
  format: typeof REPORT_FORMAT
  format_version: typeof REPORT_FORMAT_VERSION
  disclaimer: typeof DISCLAIMER
  sections: SectionId[]
  meta: {
    board: string
    version: number
    versions: number
    filename: string
    uploadedAt: string
    analysedAt?: string
    generatedAt: string
    appVersion: string
    workerVersion: string
    worker?: string
    sha256?: string
    kicadVersion?: number
    size: { widthMm: number; heightMm: number; thicknessMm: number }
    copperLayers: number
    nets: number
    features: { fullWave: boolean; smallPartSolve: boolean }
    rulesFile: string | null
    rulesFileError: string | null
    appSettings: boolean
    assumedMaxFrequencyHz?: number
  }
  stackup: Pick<StackupEntry, 'name' | 'role' | 'thickness_mm' | 'material' | 'epsilon_r' | 'from_file'>[]
  settings: SettingLine[]
  summary: { critical: number; warning: number; info: number; suppressed: number }
  findings: SeverityGroup[]
  /** Findings with a place on the board, by marker number. */
  markers: { n: number; x: number; y: number; severity: RuleFinding['severity'] }[]
  boardImage: BoardImage | null
  notes: Notice[]
  cables: CableSection | null
  esd: EsdSection | null
  changes: ChangesSection | null
  experimental: ExperimentalSection | null
}

const SEVERITIES: RuleFinding['severity'][] = ['critical', 'warning', 'info']

const RULE_BY_ID = new Map(RULES.map((r) => [r.id, r]))

export const ruleTitle = (rule: string) => RULE_BY_ID.get(rule)?.title ?? rule

const num = (v: unknown): number | undefined =>
  typeof v === 'number' && Number.isFinite(v) ? v : undefined

/** Only a PNG data URL is allowed through: the report must not load anything from anywhere. */
export function safeImage(img: BoardImage | null | undefined): BoardImage | null {
  if (!img || !/^data:image\/png;base64,[A-Za-z0-9+/]+=*$/.test(img.src)) return null
  if (!(img.widthPx > 0) || !(img.heightPx > 0)) return null
  return img
}

/** Findings grouped by severity then rule, with located ones numbered in reading order. */
export function groupFindings(findings: RuleFinding[]): { groups: SeverityGroup[]; markers: ReportData['markers'] } {
  const markers: ReportData['markers'] = []
  let n = 0
  const groups: SeverityGroup[] = []
  for (const severity of SEVERITIES) {
    const ofSeverity = findings.filter((f) => f.severity === severity)
    if (ofSeverity.length === 0) continue
    const byRule = new Map<string, RuleFinding[]>()
    for (const f of ofSeverity) {
      const list = byRule.get(f.rule)
      if (list) list.push(f)
      else byRule.set(f.rule, [f])
    }
    const rules = [...byRule.entries()]
      .sort((a, b) => b[1].length - a[1].length || ruleTitle(a[0]).localeCompare(ruleTitle(b[0])))
      .map(([rule, list]): RuleGroup => ({
        rule,
        ruleTitle: ruleTitle(rule),
        about: RULE_BY_ID.get(rule)?.about,
        findings: list.map((f) => {
          const x = num(f.x)
          const y = num(f.y)
          const located = x !== undefined && y !== undefined
          const marker = located ? ++n : null
          if (located) markers.push({ n: marker!, x: x!, y: y!, severity })
          return {
            marker, id: f.id, rule: f.rule, severity, title: f.title, detail: f.detail,
            ...(f.net ? { net: f.net } : {}),
            ...(f.layer ? { layer: f.layer } : {}),
            ...(located ? { x, y } : {}),
          }
        }),
      }))
    groups.push({ severity, count: ofSeverity.length, rules })
  }
  return { groups, markers }
}

/** Every setting that is not the built-in default, and where it came from. */
export function changedSettings(rules: RulesDoc | null): SettingLine[] {
  const applied = rules?.settings?.applied
  if (!applied) return []
  const out: SettingLine[] = []
  for (const [key, v] of Object.entries(applied.board ?? {})) {
    if (v.source !== 'default') out.push({ what: `Board: ${key}`, value: String(v.value), source: v.source })
  }
  for (const [id, r] of Object.entries(applied.rules ?? {})) {
    const name = ruleTitle(id)
    if (r.enabled_source !== 'default') {
      out.push({ what: name, value: r.enabled ? 'on' : 'off', source: r.enabled_source })
    }
    if (r.severity && r.severity_source !== 'default') {
      out.push({ what: `${name}: severity`, value: r.severity, source: r.severity_source })
    }
    for (const [k, p] of Object.entries(r.params ?? {})) {
      if (p.source !== 'default') out.push({ what: `${name}: ${k}`, value: String(p.value), source: p.source })
    }
  }
  return out
}

function cableSection(doc: CablesDoc): CableSection {
  const modelled = doc.cables
    .filter((c) => !c.declared_none && c.cable_id)
    .sort((a, b) => a.ref.localeCompare(b.ref, undefined, { numeric: true }))
    .map((c) => ({
      ref: c.ref,
      cable: c.cable_name ?? c.cable_id ?? '',
      ...(num(c.length_m) !== undefined ? { lengthM: c.length_m } : {}),
      ...(c.far_end ? { farEnd: c.far_end } : {}),
      ...(c.shield ? { shield: c.shield } : {}),
      tightest: c.tightest
        ? {
            frequencyHz: c.tightest.frequency_hz,
            maxCurrentDbua: c.tightest.max_current_dbua,
            limitDbuvPerM: c.tightest.limit_dbuv_per_m,
          }
        : null,
      peaksHz: c.radiation_peaks_hz ?? [],
      gridTooCoarse: !!c.grid_too_coarse,
      points: c.points ?? [],
    }))
  return {
    standard: doc.standard_id,
    modelled,
    neverCabled: doc.cables.filter((c) => c.declared_none).map((c) => c.ref),
    unassigned: doc.unassigned.map((u) => ({ ref: u.ref, footprint: u.footprint, reason: u.reason })),
    assumptions: doc.assumptions ?? [],
    notes: doc.notes ?? [],
  }
}

function esdSection(doc: TransientDoc): EsdSection {
  const lines = [...doc.lines]
    .sort((a, b) => `${a.connector.ref}.${a.connector.pad}`.localeCompare(
      `${b.connector.ref}.${b.connector.pad}`, undefined, { numeric: true }))
    .map((l): EsdLineSection => {
      const variants = l.variants ?? []
      const laid = variants.find((v) => v.id === 'as_laid_out')
      const better = variants.find((v) => v.id === 'clamp_at_connector' || v.id === 'reference_clamp')
      return {
        net: l.net,
        connector: `${l.connector.ref}.${l.connector.pad}`,
        x: l.x,
        y: l.y,
        clamp: l.clamp
          ? { ref: `${l.clamp.ref}.${l.clamp.pad}`, part: l.clamp.part, model: l.clamp.model, source: l.clamp.source }
          : null,
        seriesResistor: l.series_resistor ? `${l.series_resistor.ref} (${l.series_resistor.ohm} ohm)` : null,
        ic: l.ic ? `${l.ic.ref}.${l.ic.pad} (${l.ic.supply}, ${l.ic.supply_v} V)` : null,
        peak: peakOf(laid),
        comparison: better
          ? {
              what: better.id === 'reference_clamp' ? 'A clamp at the connector' : 'Clamp moved to the connector',
              volts: peakOf(better).volts,
            }
          : null,
        unclamped: !!laid?.unclamped,
        ...(l.error ? { error: l.error } : {}),
        variants: variants.map((v) => ({
          id: v.id,
          label: v.label,
          ...(num(v.v_pin_peak_v) !== undefined ? { pinPeakV: v.v_pin_peak_v } : {}),
          ...(num(v.v_clamp_peak_v) !== undefined ? { clampPeakV: v.v_clamp_peak_v } : {}),
          connectorPeakV: v.v_connector_peak_v,
          unclamped: v.unclamped,
          worstPolarity: v.worst_polarity,
        })),
        tNs: l.t_ns ?? [],
        waveforms: variants
          .filter((v) => v.v_pin && v.v_pin.length > 0)
          .map((v) => ({ id: v.id, label: v.label, values: v.v_pin! })),
        notes: l.notes ?? [],
      }
    })
  return {
    standard: doc.standard,
    kv: doc.kv,
    level: doc.level,
    polarities: doc.polarities ?? [],
    sourceCheck: doc.source_check ?? [],
    referenceClamp: doc.reference_clamp ? `${doc.reference_clamp.part} (${doc.reference_clamp.model})` : '',
    lines,
    modelsRejected: (doc.models ?? [])
      .filter((m) => m.status === 'rejected')
      .map((m) => ({ part: m.part, reasons: m.reasons })),
    assumptions: doc.assumptions ?? [],
    notes: doc.notes ?? [],
  }
}

/** The number of changed nets a report lists; the rest are counted. */
export const MAX_NET_CHANGES = 25

function changesSection(input: NonNullable<ReportInput['compare']>, after: ReportInput): ChangesSection {
  let findings: ChangesSection['findings'] = null
  if (input.rulesBefore && after.rules) {
    const diff = diffFindings(input.rulesBefore.findings, after.rules.findings)
    const byRule = groupByRule(diff.changes)
      .map(([rule, list]) => ({
        rule,
        ruleTitle: ruleTitle(rule),
        changes: list
          .filter((c): c is FindingChange & { status: 'new' | 'fixed' } => c.status !== 'unchanged')
          .map((c) => {
            const f = (c.after ?? c.before)!
            return { status: c.status, severity: f.severity, title: f.title, ...(f.net ? { net: f.net } : {}) }
          }),
      }))
      .filter((r) => r.changes.length > 0)
    findings = {
      headline: findingsHeadline(diff.counts),
      counts: { fixed: diff.counts.fixed.total, new: diff.counts.new.total, unchanged: diff.counts.unchanged.total },
      byRule,
    }
  }
  const allNets = input.netsBefore && input.netsAfter ? diffNets(input.netsBefore, input.netsAfter) : null
  const cableText = (s?: { cable?: string; note?: string; tightest?: { frequency_hz: number; max_current_dbua: number } | null }) => {
    if (!s) return 'not in this version'
    if (s.note) return s.note
    const t = s.tightest
    return [s.cable, t ? `tightest ${t.max_current_dbua.toFixed(0)} dBuA at ${(t.frequency_hz / 1e6).toFixed(0)} MHz` : '']
      .filter(Boolean).join(', ')
  }
  const cables = input.cablesBefore && after.cables
    ? diffCables(input.cablesBefore, after.cables.doc).map((c) => ({
        ref: c.ref, before: cableText(c.before), after: cableText(c.after),
      }))
    : null
  const esd = input.esdBefore && after.esd
    ? diffEsd(input.esdBefore, after.esd.doc).map((c) => ({
        connector: c.connector, net: c.net,
        ...(c.before?.volts !== undefined ? { beforeV: c.before.volts } : {}),
        ...(c.after?.volts !== undefined ? { afterV: c.after.volts } : {}),
      }))
    : null
  return {
    sinceVersion: input.versionNumber,
    findings,
    nets: allNets ? allNets.slice(0, MAX_NET_CHANGES) : null,
    netsTotal: allNets?.length ?? 0,
    cables,
    esd,
  }
}

function experimentalSection(input: NonNullable<ReportInput['experimental']>): ExperimentalSection {
  const c = input.compliance
  return {
    why:
      'These parts of the analyzer are switched on as experimental features on this installation. ' +
      'None of them has been verified on a real board. Read them as a ranking between layouts, ' +
      'not as levels.',
    compliance: c
      ? {
          standard: c.doc.standard,
          complete: c.doc.complete,
          ...(num(c.doc.margin_db) !== undefined ? { marginDb: c.doc.margin_db } : {}),
          ...(c.doc.worst
            ? {
                worst: {
                  frequencyHz: c.doc.worst.frequency_hz,
                  fieldDbuvPerM: c.doc.worst.field_dbuv_per_m,
                  limitDbuvPerM: c.doc.worst.limit_dbuv_per_m,
                },
              }
            : {}),
          gaps: (c.doc.gaps ?? []).map((g) => g.message),
          notes: c.doc.notes ?? [],
          uncertainty: c.doc.uncertainty_note ?? '',
          why: EXPERIMENTAL.complianceEstimate,
        }
      : null,
    solves: (input.solves ?? []).map((r) => ({
      id: r.id,
      kind: (r.params as { mode?: string } | undefined)?.mode === 'small_part' ? 'Small-part solve' : 'Full-wave solve',
      ...(r.finished_at ? { finishedAt: r.finished_at } : {}),
    })),
  }
}

/** Assemble everything the report says. Sections not asked for are null, not empty. */
export function assembleReport(input: ReportInput): ReportData {
  const want = new Set(input.sections)
  const summary = (input.ingest.summary ?? {}) as Record<string, unknown>
  const notices = collectNotices(input.rules, input.board)
  const { groups, markers } = groupFindings(input.rules?.findings ?? [])
  const experimentalOn = input.features.full_wave || input.features.small_part_solve === true

  const sections = SECTIONS.map((s) => s.id).filter((id) => {
    if (!want.has(id)) return false
    if (id === 'cables') return !!input.cables
    if (id === 'esd') return !!input.esd
    if (id === 'changes') return !!input.compare
    // Experimental results never appear unless their feature is on.
    if (id === 'experimental') {
      const e = input.experimental
      return experimentalOn && !!e && (!!e.compliance || (e.solves?.length ?? 0) > 0)
    }
    if (id === 'findings' || id === 'notes') return !!input.rules || id === 'notes'
    return true
  })
  const has = (id: SectionId) => sections.includes(id)

  return {
    format: REPORT_FORMAT,
    format_version: REPORT_FORMAT_VERSION,
    disclaimer: DISCLAIMER,
    sections,
    meta: {
      board: input.projectName,
      version: input.version.number,
      versions: input.version.count,
      filename: input.version.filename,
      uploadedAt: input.version.uploadedAt,
      ...(input.ingest.finished_at ? { analysedAt: input.ingest.finished_at } : {}),
      generatedAt: input.generatedAt.toISOString(),
      appVersion: input.appVersion || 'not recorded',
      workerVersion: typeof summary.worker_version === 'string' ? summary.worker_version : 'not recorded',
      ...(typeof summary.worker === 'string' ? { worker: summary.worker } : {}),
      ...(input.version.sha256 || typeof summary.input_sha256 === 'string'
        ? { sha256: input.version.sha256 || (summary.input_sha256 as string) }
        : {}),
      ...(num(input.board.kicad?.version) !== undefined ? { kicadVersion: input.board.kicad.version } : {}),
      size: {
        widthMm: input.board.board.width_mm,
        heightMm: input.board.board.height_mm,
        thicknessMm: input.board.board.thickness_mm,
      },
      copperLayers: input.board.layers.length,
      nets: input.board.nets.length,
      features: {
        fullWave: input.features.full_wave,
        smallPartSolve: input.features.small_part_solve === true || input.features.full_wave,
      },
      rulesFile: notices.rulesFile,
      rulesFileError: notices.rulesFileError
        ? `${notices.rulesFileError.name}: ${notices.rulesFileError.reason}`
        : null,
      appSettings: notices.appSettings,
      ...(num(input.rules?.summary.assumed_max_frequency_hz) !== undefined
        ? { assumedMaxFrequencyHz: input.rules!.summary.assumed_max_frequency_hz }
        : {}),
    },
    stackup: input.board.stackup.map((s) => ({
      name: s.name, role: s.role, thickness_mm: s.thickness_mm, material: s.material,
      epsilon_r: s.epsilon_r, from_file: s.from_file,
    })),
    settings: changedSettings(input.rules),
    summary: {
      critical: input.rules?.summary.critical ?? 0,
      warning: input.rules?.summary.warning ?? 0,
      info: input.rules?.summary.info ?? 0,
      suppressed: input.rules?.suppressed ?? 0,
    },
    findings: has('findings') ? groups : [],
    markers: has('board') && has('findings') ? markers : [],
    boardImage: has('board') ? safeImage(input.boardImage) : null,
    notes: has('notes') ? notices.notices : [],
    cables: has('cables') && input.cables ? cableSection(input.cables.doc) : null,
    esd: has('esd') && input.esd ? esdSection(input.esd.doc) : null,
    changes: has('changes') && input.compare ? changesSection(input.compare, input) : null,
    experimental: has('experimental') && input.experimental ? experimentalSection(input.experimental) : null,
  }
}

/** The JSON export: the same data, without the picture. */
export function reportJson(data: ReportData): string {
  return JSON.stringify({ ...data, boardImage: undefined }, null, 2)
}

/** A file name that survives every operating system a download might land on. */
export function reportFileName(board: string, version: number, ext: 'html' | 'json'): string {
  const slug = board.trim().replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || 'board'
  return `${slug}-v${version}-emi-report.${ext}`
}
