/**
 * What the worker said about a board while analysing it, sorted by how much it matters.
 *
 * The notes come from three places: rules.json `notes` (sidecar files, the stackup, checks
 * that could not run), rules.json `settings_warnings` (a rules file with a typo), and
 * board.json `warnings` (what the parser had to guess). They are plain sentences, so the
 * level is read from the wording. Patterns match both today's sentences and the older ones
 * still in rules.json on boards analysed before they were rewritten, and anything
 * unrecognised is a warning: a note nobody classified is never hidden.
 */

import type { BoardDoc, RulesDoc } from './boardTypes'

/**
 * `off`: a check was switched off, or your settings were not used. The findings list is
 * incomplete and says nothing about it, so these stand out.
 * `warn`: something was assumed; results carry that assumption.
 * `info`: worth knowing, nothing to fix.
 */
export type NoticeLevel = 'off' | 'warn' | 'info'

export interface Notice {
  text: string
  level: NoticeLevel
}

export interface AnalysisNotices {
  notices: Notice[]
  /** The rules file that was applied, or null for built-in defaults. */
  rulesFile: string | null
  /** A rules file that was found but refused, with the reason. */
  rulesFileError: { name: string; reason: string } | null
  /** Settings edited in this app took part in the analysis. */
  appSettings: boolean
}

const OFF: RegExp[] = [
  /not filled, so plane checks skip/i,
  /no filled copper (was found )?on .* not checked/i,
  /check failed to run/i,
  /could not be read.*built-in defaults/i,
  /return-via check cannot run/i,
]

const INFO: RegExp[] = [
  /^rules read from /i,
  /^no \.kicad_pro in the upload/i,
  /^permittivity (set|forced) to/i,
  /hidden by suppressions/i,
  /custom shape/i,
  /aperture.macro/i,
  /14-character/i,
]

export function classifyNotice(text: string): NoticeLevel {
  if (OFF.some((re) => re.test(text))) return 'off'
  if (INFO.some((re) => re.test(text))) return 'info'
  return 'warn'
}

const ORDER: Record<NoticeLevel, number> = { off: 0, warn: 1, info: 2 }

/**
 * Sentence case, so an older lowercase note reads like the rest. Only for a plain word: a
 * note that opens with a file name ("emi.rules.yaml could not be read") keeps its spelling.
 */
function tidy(text: string): string {
  const t = text.trim()
  return /^[a-z]+(\s|$)/.test(t) ? t[0].toUpperCase() + t.slice(1) : t
}

export function collectNotices(
  rules: Pick<RulesDoc, 'notes' | 'settings_warnings' | 'settings'> | null | undefined,
  board?: Pick<BoardDoc, 'warnings'> | null,
): AnalysisNotices {
  const raw = [
    ...(rules?.notes ?? []),
    ...(rules?.settings_warnings ?? []),
    ...(board?.warnings ?? []),
  ]

  // Which rules file was applied. Said once, as its own line, rather than as a note too.
  let rulesFile = rules?.settings?.file || null
  let rulesFileError: AnalysisNotices['rulesFileError'] = null
  if (rules?.settings?.file_error) {
    const note = raw.find((n) => /could not be read.*built-in defaults/i.test(n))
    rulesFileError = {
      name: note?.split(' could not be read')[0] ?? 'The rules file',
      reason: rules.settings.file_error,
    }
  }
  if (!rules?.settings) {
    // Older rules.json: the note is the only record of the file.
    const read = raw.map((n) => /^rules read from (.+?)\.?$/i.exec(n.trim())).find(Boolean)
    if (read) rulesFile = read[1]
  }

  const seen = new Set<string>()
  const notices: Notice[] = []
  for (const n of raw) {
    const text = tidy(n)
    if (!text || /^rules read from /i.test(text)) continue
    // Unfilled zones are reported with the board and again with the findings they silence.
    const key = text.toLowerCase().replace(/\.$/, '')
    if (seen.has(key)) continue
    seen.add(key)
    notices.push({ text, level: classifyNotice(text) })
  }
  notices.sort((a, b) => ORDER[a.level] - ORDER[b.level])

  const applied = rules?.settings?.applied
  const appSettings = !!applied && (
    Object.values(applied.board).some((v) => v.source === 'run')
    || Object.values(applied.rules).some((r) =>
      r.enabled_source === 'run' || r.severity_source === 'run'
      || Object.values(r.params).some((v) => v.source === 'run'))
  )

  return { notices, rulesFile, rulesFileError, appSettings }
}

/** The most serious level present, for the indicator on the tab. */
export function worstLevel(notices: Notice[]): NoticeLevel | null {
  return notices.reduce<NoticeLevel | null>(
    (worst, n) => (worst === null || ORDER[n.level] < ORDER[worst] ? n.level : worst), null)
}
