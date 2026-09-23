import { describe, expect, it } from 'vitest'
import { classifyNotice, collectNotices, worstLevel } from './notices'
import { defaultSnapshot } from './rulesSettings'

describe('classifyNotice', () => {
  it('puts what switches a check off first', () => {
    for (const n of [
      '3 zones not filled, so plane checks skip them. Refill zones in KiCad (B) and save before exporting.',
      'No filled copper on In1.Cu, so traces over it were not checked for plane gaps. Refill zones in KiCad (B) and save.',
      // The wording on boards analysed before the notes were rewritten.
      'no filled copper was found on In1.Cu, so traces over it were not checked for plane gaps',
      'the stitching check failed to run; other checks are unaffected',
      'emi.rules.yaml could not be read (expected a mapping on line 2), so built-in defaults were used. Fix the file and upload again.',
      'No drill file in the upload, so vias are missing and the return-via check cannot run. Add the drill file to the zip.',
    ]) expect(classifyNotice(n), n).toBe('off')
  })

  it('keeps what needs nothing done quiet', () => {
    for (const n of [
      'Rules read from emi.rules.yaml.',
      'No .kicad_pro in the upload, so net classes and pairs were guessed from net names. Upload the zipped project folder to use them.',
      'no .kicad_pro in the upload, so netclasses and differential-pair geometry are unavailable',
      'Permittivity set to 4.2 by your settings.',
      '2 findings hidden by suppressions in your rules file.',
      '4 pads with a custom shape (U1.1, U1.2, U1.3, U1.4) were drawn as a rectangle, so copper there is approximate.',
    ]) expect(classifyNotice(n), n).toBe('info')
  })

  it('treats anything else as a warning, so nothing unclassified is hidden', () => {
    expect(classifyNotice('No stackup in the board file, so 1.6 mm FR-4 with 35 um copper was assumed.')).toBe('warn')
    expect(classifyNotice('Rules file: unknown rule \'x\'')).toBe('warn')
    expect(classifyNotice('something new the worker learned to say')).toBe('warn')
  })
})

describe('collectNotices', () => {
  const zones = '2 zones not filled, so plane checks skip them. Refill zones in KiCad (B) and save before exporting.'

  it('merges rules notes, settings warnings and board warnings, worst first, once each', () => {
    const out = collectNotices(
      {
        notes: ['Rules read from emi.rules.yaml.', 'No .kicad_pro in the upload, so net classes were guessed.', zones],
        settings_warnings: ['Rules file: unknown rule \'nope\''],
      },
      { warnings: [zones, 'no Edge.Cuts outline found; the board extent was inferred from the copper'] },
    )
    expect(out.notices.map((n) => n.level)).toEqual(['off', 'warn', 'warn', 'info'])
    expect(out.notices[0].text).toBe(zones)
    // An older lowercase note is shown as a sentence.
    expect(out.notices.some((n) => n.text.startsWith('No Edge.Cuts outline found'))).toBe(true)
    // The rules file is its own line, not a note as well.
    expect(out.rulesFile).toBe('emi.rules.yaml')
    expect(out.notices.some((n) => /rules read from/i.test(n.text))).toBe(false)
    expect(worstLevel(out.notices)).toBe('off')
  })

  it('reads the rules file from the settings report, and why one was refused', () => {
    const base = defaultSnapshot()
    const ok = collectNotices({ notes: [], settings: { file: '.emi.yaml', file_error: '', applied: base, base } })
    expect(ok.rulesFile).toBe('.emi.yaml')
    expect(ok.appSettings).toBe(false)

    const bad = collectNotices({
      notes: ['emi.rules.yml could not be read (found a tab on line 3), so built-in defaults were used. Fix the file and upload again.'],
      settings: { file: '', file_error: 'found a tab on line 3', applied: base, base },
    })
    expect(bad.rulesFile).toBeNull()
    expect(bad.rulesFileError).toEqual({ name: 'emi.rules.yml', reason: 'found a tab on line 3' })
    expect(bad.notices[0].level).toBe('off')
    // A file name is not a word to capitalize.
    expect(bad.notices[0].text.startsWith('emi.rules.yml could not be read')).toBe(true)
  })

  it('says when settings from the app took part', () => {
    const base = defaultSnapshot()
    const applied = structuredClone(base)
    applied.rules.radiator.enabled = false
    applied.rules.radiator.enabled_source = 'run'
    expect(collectNotices({ settings: { file: '', file_error: '', applied, base } }).appSettings).toBe(true)
  })

  it('is empty for a board with nothing to say', () => {
    expect(collectNotices(null, null)).toEqual({
      notices: [], rulesFile: null, rulesFileError: null, appSettings: false,
    })
    expect(worstLevel([])).toBeNull()
  })
})
