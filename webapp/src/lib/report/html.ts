/**
 * The report as one self-contained HTML file.
 *
 * Inline CSS, inline SVG, the board image as a data URL, and a Content-Security-Policy that
 * forbids everything else, so the file opens the same offline, attached to an email or on a
 * test lab's machine, and loads nothing from anywhere. A print stylesheet makes "Save as PDF"
 * in any browser the PDF export.
 *
 * Board, net, part and file names are user-controlled text, and so is everything the worker
 * copied out of a board file. Every string goes through {@link esc}; there is no other way
 * into the markup.
 */

import type { BoardDoc } from '../boardTypes'
import { CHART_COLORS, cableBudgetSvg, fmtHz, transientSvg } from './charts'
import { SECTIONS, type ReportData, type SectionId } from './model'

/** Escape text for HTML element content and quoted attribute values alike. */
export function esc(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

const SEVERITY_COLOR = { critical: '#e03131', warning: '#f08c00', info: '#1c7ed6' } as const
const SEVERITY_LABEL = { critical: 'Critical', warning: 'Warning', info: 'Info' } as const
const SOURCE_LABEL: Record<string, string> = {
  default: 'default', project: 'KiCad project', file: 'rules file', run: 'set in the app',
}

const fmt = (v: number | undefined, digits = 1, unit = '') =>
  v === undefined || !Number.isFinite(v) ? '' : `${v.toFixed(digits)}${unit ? ` ${unit}` : ''}`
const fmtV = (v?: number) =>
  v === undefined || !Number.isFinite(v) ? '' : `${Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)} V`
const fmtDate = (iso?: string) => {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC')
}

/** Sentence case for a worker note that starts with a plain lowercase word. */
const tidy = (t: string) => (/^[a-z]+(\s|$)/.test(t) ? t[0].toUpperCase() + t.slice(1) : t)

const list = (items: string[], cls = '') =>
  items.length ? `<ul${cls ? ` class="${cls}"` : ''}>${items.map((i) => `<li>${esc(tidy(i))}</li>`).join('')}</ul>` : ''

/** A CSS string literal's content. Only ever given fixed copy, escaped all the same. */
function cssString(t: string): string {
  return t.replace(/[\\"\n\r<>]/g, (ch) => `\\${ch.charCodeAt(0).toString(16)} `)
}

const row = (k: string, v: string) => (v ? `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>` : '')

const CSS = `
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #f1f3f5; color: #212529;
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 860px; margin: 24px auto; background: #fff; padding: 40px 48px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08); }
h1 { font-size: 28px; margin: 4px 0 2px; overflow-wrap: anywhere; }
h2 { font-size: 20px; margin: 0 0 12px; padding-bottom: 6px; border-bottom: 2px solid #212529; }
h3 { font-size: 15px; margin: 22px 0 8px; }
h4 { font-size: 14px; margin: 16px 0 6px; }
p { margin: 0 0 10px; }
section { margin-top: 40px; }
.eyebrow { text-transform: uppercase; letter-spacing: .08em; font-size: 12px; color: #868e96; margin: 0; }
.sub { color: #495057; margin: 0 0 18px; overflow-wrap: anywhere; }
.muted { color: #868e96; font-size: 12px; }
.disclaimer { border: 2px solid #e03131; background: #fff5f5; border-radius: 6px; padding: 12px 16px; margin: 16px 0 20px; }
.disclaimer strong { display: block; color: #c92a2a; font-size: 15px; margin-bottom: 4px; }
.experimental-banner { border: 2px dashed #f08c00; background: #fff9db; border-radius: 6px; padding: 10px 14px; margin: 0 0 16px; }
.experimental-banner strong { color: #e67700; }
.counts { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 18px; }
.pill { border-radius: 999px; padding: 2px 10px; font-size: 12px; font-weight: 600; color: #fff; }
table { border-collapse: collapse; width: 100%; margin: 0 0 12px; font-size: 13px; }
th, td { text-align: left; vertical-align: top; padding: 4px 8px; border-bottom: 1px solid #e9ecef; }
td { overflow-wrap: anywhere; }
thead th { border-bottom: 1px solid #adb5bd; font-size: 12px; color: #495057; }
table.meta th { width: 34%; color: #495057; font-weight: 500; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.assumed { color: #e67700; font-size: 11px; }
ul { margin: 0 0 10px; padding-left: 20px; }
.rule { margin: 0 0 14px; }
.rule-about { color: #495057; font-size: 12px; margin: 0 0 6px; }
.finding { display: flex; gap: 10px; padding: 6px 0; border-top: 1px solid #f1f3f5; break-inside: avoid; }
.finding .n { flex: none; width: 26px; height: 26px; border-radius: 50%; color: #fff; font-size: 12px;
  font-weight: 700; display: flex; align-items: center; justify-content: center; }
.finding .n.none { background: #dee2e6 !important; color: #495057; }
.ftitle { font-weight: 600; overflow-wrap: anywhere; }
.fdetail { color: #343a40; overflow-wrap: anywhere; }
.floc { color: #868e96; font-size: 12px; overflow-wrap: anywhere; }
.board-figure { margin: 0; }
.board-figure svg { width: 100%; height: auto; max-height: 640px; display: block; background: #0e1211; border-radius: 4px; }
.legend { display: flex; gap: 14px; font-size: 12px; color: #495057; margin-top: 6px; flex-wrap: wrap; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 4px; vertical-align: -1px; }
.swatch { display: inline-block; width: 16px; height: 0; border-top: 2px solid; margin-right: 4px; vertical-align: 3px; }
.swatch.dashed { border-top-style: dashed; }
.card { border: 1px solid #dee2e6; border-radius: 6px; padding: 12px 14px; margin: 0 0 14px; break-inside: avoid; }
.card h4 { margin-top: 0; overflow-wrap: anywhere; }
.chart { width: 100%; height: auto; display: block; margin: 6px 0; }
.note-off { color: #c92a2a; }
.note-warn { color: #e67700; }
.tag { display: inline-block; font-size: 11px; border-radius: 4px; padding: 0 6px; background: #f1f3f5; color: #495057; margin-left: 6px; }
.tag.bad { background: #ffe3e3; color: #c92a2a; }
.tag.new { background: #ffe3e3; color: #c92a2a; }
.tag.fixed { background: #d3f9d8; color: #2b8a3e; }
.in-report { margin: 0 0 10px; }
@media (max-width: 700px) { main { padding: 20px 16px; margin: 0; } }
@page {
  size: A4;
  margin: 14mm 14mm 16mm;
  @bottom-left { content: "__FOOTER__"; font: 8pt sans-serif; color: #868e96; }
  @bottom-right { content: counter(page) " / " counter(pages); font: 8pt sans-serif; color: #868e96; }
}
@media print {
  body { background: #fff; font-size: 10pt; }
  main { max-width: none; margin: 0; padding: 0; box-shadow: none; }
  h1 { font-size: 22pt; }
  table { font-size: 9pt; }
  th, td { padding: 3px 6px; }
  h3 { margin: 14px 0 6px; }
  section { margin-top: 24px; }
  section.page { break-before: page; margin-top: 0; }
  h2, h3, h4 { break-after: avoid; }
  tr, .card, .finding, figure { break-inside: avoid; }
  .board-figure svg { max-height: 230mm; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
`

function titlePage(d: ReportData, omitted: { label: string; reason: string }[]): string {
  const m = d.meta
  const counts = (['critical', 'warning', 'info'] as const)
    .map((s) => `<span class="pill" style="background:${SEVERITY_COLOR[s]}">${esc(d.summary[s])} ${SEVERITY_LABEL[s].toLowerCase()}</span>`)
    .join('')
  const stackup = d.stackup.length
    ? `<table><thead><tr><th>Layer</th><th>Kind</th><th class="num">Thickness</th><th>Material</th><th class="num">Er</th></tr></thead><tbody>${
        d.stackup.map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(s.role)}</td><td class="num">${esc(fmt(s.thickness_mm, 3, 'mm'))}</td><td>${esc(s.material)}</td><td class="num">${
          s.epsilon_r == null ? '' : esc(s.epsilon_r.toFixed(2))}${s.from_file ? '' : ' <span class="assumed">assumed</span>'}</td></tr>`).join('')
      }</tbody></table>`
    : '<p class="muted">The board file carries no stackup.</p>'
  const settings = [
    `<p>${m.rulesFile ? `Rules file: <strong>${esc(m.rulesFile)}</strong>.` : 'No rules file: built-in defaults.'}${
      m.appSettings ? ' Some settings were changed in the app.' : ''}${
      m.assumedMaxFrequencyHz ? ` Checks evaluated up to ${esc(fmtHz(m.assumedMaxFrequencyHz))}.` : ''}</p>`,
    m.rulesFileError ? `<p class="note-off">Not applied: ${esc(m.rulesFileError)}</p>` : '',
    d.settings.length
      ? `<table><thead><tr><th>Setting</th><th>Value</th><th>From</th></tr></thead><tbody>${
          d.settings.map((s) => `<tr><td>${esc(s.what)}</td><td>${esc(s.value)}</td><td>${esc(SOURCE_LABEL[s.source] ?? s.source)}</td></tr>`).join('')
        }</tbody></table>`
      : '<p class="muted">Every check ran with its default settings.</p>',
    d.summary.suppressed ? `<p class="muted">${esc(d.summary.suppressed)} findings hidden by suppressions.</p>` : '',
  ].join('')
  const included = d.sections.map((id) => SECTIONS.find((s) => s.id === id)!.label)

  return `<header class="title-page">
<p class="eyebrow">EMI Analyzer report</p>
<h1>${esc(m.board)}</h1>
<p class="sub">Version ${m.version} of ${m.versions} · ${esc(m.filename)}</p>
<div class="disclaimer" role="note"><strong>${esc(d.disclaimer.title)}</strong>${esc(d.disclaimer.text)}</div>
<div class="counts">${counts}</div>
<table class="meta"><tbody>
${row('Board size', `${fmt(m.size.widthMm, 1)} x ${fmt(m.size.heightMm, 1)} mm, ${fmt(m.size.thicknessMm, 2)} mm thick`)}
${row('Copper layers and nets', `${m.copperLayers} layers, ${m.nets} nets`)}
${row('Uploaded', fmtDate(m.uploadedAt))}
${row('Analyzed', fmtDate(m.analysedAt))}
${row('Report generated', fmtDate(m.generatedAt))}
${row('File SHA-256', m.sha256 ?? 'not recorded')}
${row('KiCad file version', m.kicadVersion ? String(m.kicadVersion) : '')}
${row('App version', m.appVersion)}
${row('Worker version', m.worker ? `${m.workerVersion} (${m.worker})` : m.workerVersion)}
${row('Experimental features', [
    m.features.fullWave ? 'full-wave on' : 'full-wave off',
    m.features.smallPartSolve ? 'small-part solve on' : 'small-part solve off',
  ].join(', '))}
</tbody></table>
<h3>Stackup</h3>
${stackup}
<h3>Settings</h3>
${settings}
<h3>In this report</h3>
<p class="in-report">${esc(included.join(', '))}.</p>
${omitted.length ? `<p class="in-report">Not included: ${omitted.map((o) => `${esc(o.label)} (${esc(o.reason)})`).join(', ')}.</p>` : ''}
</header>`
}

function boardSvg(board: BoardDoc, d: ReportData): string {
  const W = board.board.width_mm
  const H = board.board.height_mm
  if (!(W > 0 && H > 0)) return ''
  const m = Math.max(W, H) * 0.03
  const r = Math.max(W, H) * 0.016
  const img = d.boardImage
    ? `<image href="${esc(d.boardImage.src)}" x="0" y="0" width="${W}" height="${H}" preserveAspectRatio="none"/>`
    : ''
  const outline = board.board.outline
    .filter((ring) => ring.length > 1)
    .map((ring) => `M${ring.map(([x, y]) => `${fmt(x, 3)},${fmt(H - y, 3)}`).join(' L')}`)
    .join(' ')
  // Findings at the same spot (a long net and its plane gap often share a start point) are
  // fanned out sideways so every number stays readable.
  const seen = new Map<string, number>()
  const markers = d.markers.map((mk) => {
    const key = `${Math.round(mk.x / r)},${Math.round(mk.y / r)}`
    const k = seen.get(key) ?? 0
    seen.set(key, k + 1)
    const cx = fmt(mk.x + k * r * 1.9, 3)
    const cy = fmt(H - mk.y, 3)
    return `<g><circle cx="${cx}" cy="${cy}" r="${fmt(r, 3)}" fill="${SEVERITY_COLOR[mk.severity]}" stroke="#fff" stroke-width="${fmt(r * 0.18, 3)}"/>` +
      `<text x="${cx}" y="${cy}" dy="0.35em" text-anchor="middle" font-size="${fmt(r * (mk.n > 99 ? 0.8 : 1.05), 3)}" font-weight="700" fill="#fff" font-family="sans-serif">${mk.n}</text></g>`
  }).join('')
  return `<svg viewBox="${fmt(-m, 3)} ${fmt(-m, 3)} ${fmt(W + 2 * m, 3)} ${fmt(H + 2 * m, 3)}" role="img" aria-label="Board with numbered finding markers">${img}` +
    (outline ? `<path d="${outline}" fill="none" stroke="#a6b3ad" stroke-width="${fmt(Math.max(W, H) * 0.002, 3)}"/>` : '') +
    `${markers}</svg>`
}

function boardSection(board: BoardDoc, d: ReportData): string {
  const legend = (['critical', 'warning', 'info'] as const)
    .map((s) => `<span><span class="dot" style="background:${SEVERITY_COLOR[s]}"></span>${SEVERITY_LABEL[s]}</span>`)
    .join('')
  return `<section class="page" id="board"><h2>Board</h2>
<figure class="board-figure">${boardSvg(board, d)}</figure>
<div class="legend">${legend}<span>Numbers match the findings list. Top view, all copper layers.</span></div>
${d.boardImage ? '' : '<p class="muted">The copper could not be drawn when this report was made; the outline and markers are shown.</p>'}
${d.sections.includes('findings') && d.markers.length === 0 ? '<p class="muted">No finding has a single place on the board.</p>' : ''}
</section>`
}

function findingsSection(d: ReportData): string {
  const withMarkers = d.sections.includes('board')
  if (d.findings.length === 0) {
    return `<section class="page" id="findings"><h2>Findings</h2>
<p>The layout checks found nothing. That is not a clean bill of health: they are geometric checks, not a simulation.</p></section>`
  }
  const body = d.findings.map((g) => `<h3 style="color:${SEVERITY_COLOR[g.severity]}">${SEVERITY_LABEL[g.severity]} (${g.count})</h3>` +
    g.rules.map((r) => `<div class="rule"><h4>${esc(r.ruleTitle)} <span class="tag">${r.findings.length}</span></h4>` +
      (r.about ? `<p class="rule-about">${esc(r.about)}</p>` : '') +
      r.findings.map((f) => {
        const loc = [
          f.net ? `Net ${f.net}` : '',
          f.layer ? `Layer ${f.layer}` : '',
          f.x !== undefined && f.y !== undefined ? `at ${fmt(f.x, 2)}, ${fmt(f.y, 2)} mm` : 'no single location',
        ].filter(Boolean).join(' · ')
        const n = withMarkers && f.marker !== null
          ? `<span class="n" style="background:${SEVERITY_COLOR[f.severity]}">${f.marker}</span>`
          : `<span class="n none">-</span>`
        return `<div class="finding">${n}<div><div class="ftitle">${esc(f.title)}</div>` +
          `<div class="fdetail">${esc(f.detail)}</div><div class="floc">${esc(loc)}</div></div></div>`
      }).join('') + '</div>').join('')).join('')
  return `<section class="page" id="findings"><h2>Findings</h2>
<p class="muted">Geometric checks: they say where to look, not how much the board radiates. Coordinates are in mm from the bottom-left corner of the board, Y up.</p>
${body}</section>`
}

function notesSection(d: ReportData): string {
  const cls = { off: 'note-off', warn: 'note-warn', info: '' }
  const label = { off: 'Skipped', warn: 'Assumed', info: 'Note' }
  return `<section id="notes"><h2>Analysis notes</h2>
<p class="muted">What the analysis assumed, skipped or could not read. A skipped check has no findings, which is not the same as passing.</p>
${d.notes.length
    ? `<ul>${d.notes.map((n) => `<li class="${cls[n.level]}"><strong>${label[n.level]}:</strong> ${esc(n.text)}</li>`).join('')}</ul>`
    : '<p>Nothing was assumed or skipped.</p>'}
</section>`
}

function cablesSection(d: ReportData): string {
  const c = d.cables!
  const cards = c.modelled.map((m) => `<div class="card"><h4>${esc(m.ref)}: ${esc(m.cable)}${m.lengthM !== undefined ? `, ${esc(fmt(m.lengthM, 2, 'm'))}` : ''}</h4>
<p>${m.tightest
    ? `Tightest point: <strong>${esc(fmt(m.tightest.maxCurrentDbua, 1, 'dBuA'))}</strong> at ${esc(fmtHz(m.tightest.frequencyHz))} (limit ${esc(fmt(m.tightest.limitDbuvPerM, 1, 'dBuV/m'))}).`
    : 'No tightest point in the band.'}
${m.farEnd ? ` Far end: ${esc(m.farEnd)}.` : ''}${m.shield ? ` Shield: ${esc(m.shield)}.` : ''}</p>
${cableBudgetSvg(m.points, m.peaksHz)}
<div class="legend"><span><span class="swatch" style="border-color:${CHART_COLORS.budget}"></span>allowed common-mode current</span><span><span class="swatch dashed" style="border-color:${CHART_COLORS.peak}"></span>where it radiates best</span><span>lower is less headroom</span></div>
${m.gridTooCoarse ? '<p class="muted">The frequency grid is too coarse to resolve a peak, so an empty list of peaks says nothing.</p>' : ''}
</div>`).join('')
  return `<section class="page" id="cables"><h2>Cable budgets</h2>
<p>How much common-mode current each connector's cable may carry before it reaches the ${esc(c.standard)} limit. A budget, not a prediction: compare cables and layouts with it.</p>
${cards || '<p class="muted">No connector has a cable modelled.</p>'}
${c.neverCabled.length ? `<p>Declared as never cabled: ${esc(c.neverCabled.join(', '))}.</p>` : ''}
${c.unassigned.length ? `<h3>Not modelled</h3><p class="muted">No cable was set for these connectors, so they are not covered by this section.</p><ul>${
    c.unassigned.map((u) => `<li>${esc(u.ref)}${u.footprint ? ` (${esc(u.footprint)})` : ''}${u.reason ? `: ${esc(u.reason)}` : ''}</li>`).join('')}</ul>` : ''}
${c.assumptions.length ? `<h3>Assumptions</h3>${list(c.assumptions)}` : ''}
${c.notes.length ? `<h3>Notes</h3>${list(c.notes)}` : ''}
</section>`
}

const VARIANT_COLOR: Record<string, string> = {
  as_laid_out: CHART_COLORS.as_laid_out,
  clamp_at_connector: CHART_COLORS.clamp_at_connector,
  ideal_ground: CHART_COLORS.ideal_ground,
  reference_clamp: CHART_COLORS.reference_clamp,
}

function esdSection(d: ReportData): string {
  const e = d.esd!
  const cards = e.lines.map((l) => {
    const status = l.error
      ? '<span class="tag bad">failed</span>'
      : l.unclamped ? '<span class="tag bad">unclamped</span>' : ''
    const waves = l.waveforms.map((w) => ({ color: VARIANT_COLOR[w.id] ?? '#868e96', values: w.values }))
    const chart = transientSvg(l.tNs, waves)
    return `<div class="card"><h4>${esc(l.connector)} · ${esc(l.net)}${status}</h4>
${l.error ? `<p class="note-off">${esc(l.error)}</p>` : `<p>Peak as laid out: <strong>${esc(fmtV(l.peak.volts))}</strong> at the ${esc(l.peak.where)}.${
      l.comparison ? ` ${esc(l.comparison.what)}: <strong>${esc(fmtV(l.comparison.volts))}</strong>.` : ''}</p>`}
<p class="muted">${esc([
      l.clamp ? `Clamp ${l.clamp.ref}, ${l.clamp.part} (${l.clamp.model} model)` : 'No clamp found',
      l.seriesResistor ? `series ${l.seriesResistor}` : '',
      l.ic ? `IC ${l.ic}` : '',
    ].filter(Boolean).join(' · '))}</p>
${l.variants.length ? `<table><thead><tr><th>Variant</th><th class="num">Pin</th><th class="num">Clamp</th><th class="num">Connector</th><th>Worst polarity</th></tr></thead><tbody>${
      l.variants.map((v) => `<tr><td><span class="swatch" style="border-color:${VARIANT_COLOR[v.id] ?? '#868e96'}"></span>${esc(v.label)}${v.unclamped ? '<span class="tag bad">unclamped</span>' : ''}</td><td class="num">${esc(fmtV(v.pinPeakV))}</td><td class="num">${esc(fmtV(v.clampPeakV))}</td><td class="num">${esc(fmtV(v.connectorPeakV))}</td><td>${esc(v.worstPolarity)}</td></tr>`).join('')
    }</tbody></table>` : ''}
${chart}
${l.notes.length ? list(l.notes, 'muted') : ''}
</div>`
  }).join('')
  return `<section class="page" id="esd"><h2>ESD simulation</h2>
<p>${esc(e.standard)} contact discharge at ${esc(fmt(e.kv, 0, 'kV'))}, ${esc(e.polarities.join(' and ') || 'both')} polarity, on every line that leaves the board through an edge connector. Each line is compared with the clamp at the connector. Compare those numbers; the absolute volts are an estimate.</p>
${e.sourceCheck.length ? `<p class="note-warn">The simulated source misses the standard: ${esc(e.sourceCheck.join(' '))}</p>` : ''}
${e.referenceClamp ? `<p class="muted">Reference clamp for lines without one: ${esc(e.referenceClamp)}.</p>` : ''}
${cards || '<p class="muted">No exposed line was found.</p>'}
${e.modelsRejected.length ? `<h3>Vendor models not used</h3><ul>${e.modelsRejected.map((m) => `<li>${esc(m.part)}: ${esc(m.reasons.join(' '))}</li>`).join('')}</ul>` : ''}
${e.assumptions.length ? `<h3>Assumptions</h3>${list(e.assumptions)}` : ''}
${e.notes.length ? `<h3>Notes</h3>${list(e.notes)}` : ''}
</section>`
}

function changesSection(d: ReportData): string {
  const c = d.changes!
  const parts: string[] = []
  if (c.findings) {
    parts.push(`<h3>Findings</h3>
<p>${esc(c.findings.headline)}: ${c.findings.counts.new} new, ${c.findings.counts.fixed} fixed, ${c.findings.counts.unchanged} unchanged.</p>
${c.findings.byRule.map((r) => `<h4>${esc(r.ruleTitle)}</h4><ul>${r.changes.map((ch) => `<li><span class="tag ${ch.status}">${ch.status}</span> ${esc(ch.title)}${ch.net ? ` <span class="muted">(${esc(ch.net)})</span>` : ''}</li>`).join('')}</ul>`).join('')}`)
  } else {
    parts.push('<p class="muted">Findings are not compared: one of the versions has no finished analysis.</p>')
  }
  if (c.nets && c.nets.length) {
    parts.push(`<h3>Nets whose length or vias changed</h3>
<table><thead><tr><th>Net</th><th>Change</th><th class="num">Length before</th><th class="num">Length now</th><th class="num">Vias before</th><th class="num">Vias now</th></tr></thead><tbody>${
      c.nets.map((n) => `<tr><td>${esc(n.net)}</td><td>${esc(n.status)}</td><td class="num">${esc(fmt(n.lengthBefore, 2, 'mm'))}</td><td class="num">${esc(fmt(n.lengthAfter, 2, 'mm'))}</td><td class="num">${esc(n.viasBefore ?? '')}</td><td class="num">${esc(n.viasAfter ?? '')}</td></tr>`).join('')
    }</tbody></table>${c.netsTotal > c.nets.length ? `<p class="muted">The ${c.nets.length} largest of ${c.netsTotal} changes.</p>` : ''}`)
  } else if (c.nets) {
    parts.push('<h3>Nets</h3><p>No net changed length or via count.</p>')
  }
  if (c.cables) {
    parts.push(`<h3>Cable budgets</h3><table><thead><tr><th>Connector</th><th>Before</th><th>Now</th></tr></thead><tbody>${
      c.cables.map((r) => `<tr><td>${esc(r.ref)}</td><td>${esc(r.before)}</td><td>${esc(r.after)}</td></tr>`).join('')}</tbody></table>`)
  }
  if (c.esd && c.esd.length === 0) {
    parts.push('<h3>ESD peak as laid out</h3><p>Neither version has an exposed line.</p>')
  } else if (c.esd) {
    parts.push(`<h3>ESD peak as laid out</h3><table><thead><tr><th>Connector</th><th>Net</th><th class="num">Before</th><th class="num">Now</th></tr></thead><tbody>${
      c.esd.map((r) => `<tr><td>${esc(r.connector)}</td><td>${esc(r.net)}</td><td class="num">${esc(fmtV(r.beforeV))}</td><td class="num">${esc(fmtV(r.afterV))}</td></tr>`).join('')}</tbody></table>`)
  }
  return `<section class="page" id="changes"><h2>Changes since version ${c.sinceVersion}</h2>
<p class="muted">Findings are matched on rule, net, layer and place, within 2 mm. Cables and ESD are compared only where both versions have a run.</p>
${parts.join('\n')}</section>`
}

function experimentalSection(d: ReportData): string {
  const e = d.experimental!
  const c = e.compliance
  return `<section class="page" id="experimental"><h2>Experimental results</h2>
<div class="experimental-banner"><strong>Experimental.</strong> ${esc(e.why)}</div>
${c ? `<h3>Compliance estimate <span class="tag bad">experimental</span></h3>
<p class="muted">${esc(c.why)}</p>
<table class="meta"><tbody>
${row('Standard', c.standard)}
${row('Inputs complete', c.complete ? 'yes' : 'no, so there is no margin')}
${row('Estimated margin', c.marginDb !== undefined ? fmt(c.marginDb, 1, 'dB') : '')}
${row('Worst frequency', c.worst ? `${fmtHz(c.worst.frequencyHz)}: ${fmt(c.worst.fieldDbuvPerM, 1)} against ${fmt(c.worst.limitDbuvPerM, 1)} dBuV/m` : '')}
</tbody></table>
${c.gaps.length ? `<h4>What is missing</h4>${list(c.gaps)}` : ''}
${c.uncertainty ? `<p class="muted">${esc(c.uncertainty)}</p>` : ''}
${c.notes.length ? list(c.notes, 'muted') : ''}` : ''}
${e.solves.length ? `<h3>Solves run on this version <span class="tag bad">experimental</span></h3>
<p class="muted">Their maps are in the app; they are not reproduced here.</p>
<ul>${e.solves.map((s) => `<li>${esc(s.kind)}${s.finishedAt ? `, ${esc(fmtDate(s.finishedAt))}` : ''}</li>`).join('')}</ul>` : ''}
</section>`
}

export interface RenderOptions {
  /** Parts the user asked for that could not be included, and why. Listed on the title page. */
  omitted?: { label: string; reason: string }[]
}

const RENDER: Record<Exclude<SectionId, 'board'>, (d: ReportData) => string> = {
  findings: findingsSection,
  notes: notesSection,
  cables: cablesSection,
  esd: esdSection,
  changes: changesSection,
  experimental: experimentalSection,
}

export function renderReportHtml(d: ReportData, board: BoardDoc, opts: RenderOptions = {}): string {
  const body = d.sections
    .map((id) => (id === 'board' ? boardSection(board, d) : RENDER[id](d)))
    .join('\n')
  const title = `${d.meta.board} v${d.meta.version}: EMI report`
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<meta name="generator" content="EMI Analyzer ${esc(d.meta.appVersion)}">
<title>${esc(title)}</title>
<style>${CSS.replace('__FOOTER__', cssString(d.disclaimer.short))}</style>
</head>
<body>
<main>
${titlePage(d, opts.omitted ?? [])}
${body}
<p class="muted" style="margin-top:32px">${esc(d.disclaimer.short)} Generated by EMI Analyzer ${esc(d.meta.appVersion)} on ${esc(fmtDate(d.meta.generatedAt))}.</p>
</main>
</body>
</html>
`
}
