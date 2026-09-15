/**
 * Read the subcircuit names and pins out of a SPICE library, in the browser.
 *
 * Only so the user can map the part's pads onto the model's pins before uploading. It
 * decides nothing about whether the file is safe or even valid: the worker vets every model
 * before ngspice sees it, and rejects what this happily lists.
 */

export interface SubcktHeader {
  name: string
  pins: string[]
}

/** Logical lines: '+' continuations joined, full-line and inline comments removed. */
function logicalLines(text: string): string[] {
  const out: string[] = []
  for (const raw of text.split(/\r?\n/)) {
    const stripped = raw.trim()
    if (!stripped || stripped.startsWith('*')) continue
    let body = stripped.split(';', 1)[0]
    const dollar = body.search(/\s\$/)
    if (dollar >= 0) body = body.slice(0, dollar)
    body = body.trimEnd()
    if (!body) continue
    if (body.startsWith('+')) {
      if (out.length) out[out.length - 1] += ' ' + body.slice(1).trim()
    } else {
      out.push(body)
    }
  }
  return out
}

export function parseSubckts(text: string): SubcktHeader[] {
  const found: SubcktHeader[] = []
  for (const line of logicalLines(text)) {
    const tokens = line.split(/\s+/)
    if (tokens[0].toLowerCase() !== '.subckt' || tokens.length < 3) continue
    const pins: string[] = []
    for (const tok of tokens.slice(2)) {
      if (/^params?:$/i.test(tok) || tok.includes('=')) break
      pins.push(tok)
    }
    if (pins.length) found.push({ name: tokens[1], pins })
  }
  return found
}

/**
 * The subcircuit most likely meant for a part: its name matches the part value, or it is the
 * only one whose pin count matches the footprint.
 */
export function guessSubckt(headers: SubcktHeader[], part: string, padCount: number): SubcktHeader | null {
  const norm = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]/g, '')
  const p = norm(part)
  const byName = headers.find((h) => p.startsWith(norm(h.name)) || norm(h.name).startsWith(p))
  if (byName) return byName
  const byCount = headers.filter((h) => h.pins.length === padCount)
  return byCount.length === 1 ? byCount[0] : headers[0] ?? null
}

/** Pad numbers in footprint order: "1" < "2" < "10", "A1" < "A2" < "B1". */
export function sortPadNumbers(numbers: string[]): string[] {
  return [...new Set(numbers)].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
}

/** Pads mapped to pins in order, when the counts agree — the usual vendor convention. */
export function defaultPinMap(pads: string[], pins: string[]): Record<string, string> {
  if (pads.length !== pins.length) return {}
  return Object.fromEntries(pads.map((pad, i) => [pad, pins[i]]))
}
