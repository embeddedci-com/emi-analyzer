/**
 * Where to put the excitation.
 *
 * openEMS solves a passive structure, so a solve without a port produces a field of zeros.
 * Choosing that spot is the hardest decision the user makes, and there are two honest ways
 * to help:
 *
 * * **Pick a net.** This is how someone thinks about EMI — "what is radiating?", not
 *   "which pad?" — so it is the default. We place the port on the most plausible driver
 *   pad and show where it landed.
 * * **Click a pad.** For when the guess is wrong, or the user already knows.
 *
 * Both produce explicit coordinates. The worker never guesses: it is handed an x, y and
 * layer, which keeps the thing that determines the physics visible in the run's parameters
 * rather than hidden in a heuristic.
 */

import type { BoardDoc, BoardPad, BoardVia } from './boardTypes'

export interface PortSpec {
  name: string
  x_mm: number
  y_mm: number
  layer: string
  half_width_mm: number
  resistance: number
  excited: boolean
  /** How this port came to be here, so the UI can show it and the user can disagree. */
  origin: 'net' | 'pad' | 'manual'
  net?: string
  padRef?: string
}

/** Pads that are not plausible excitation points. */
const NON_DRIVER_PAD_TYPES = new Set(['np_thru_hole'])

/**
 * Reference-net names. A port needs a signal and a return, and putting the excitation on
 * the return itself is a common way to get a result that means nothing.
 */
const REFERENCE_HINTS = ['gnd', 'ground', 'vss', 'agnd', 'dgnd', 'pgnd', 'earth']

export function isReferenceNet(net: string): boolean {
  const low = net.toLowerCase().replace(/^\//, '')
  return REFERENCE_HINTS.some((h) => low.includes(h))
}

function padsOnNet(doc: BoardDoc, net: string): BoardPad[] {
  return doc.pads.filter(
    (p) => p.net === net && !NON_DRIVER_PAD_TYPES.has(p.type) && p.layers.length > 0,
  )
}

/** The copper layer a pad's port should sit on. */
export function padLayer(doc: BoardDoc, pad: BoardPad): string {
  const copper = new Set(doc.layers.map((l) => l.name))
  const own = pad.layers.find((l) => copper.has(l))
  if (own) return own
  // A through-hole pad lists "*.Cu"; anchor it to the top layer, which is where a part is.
  return doc.layers[0]?.name ?? ''
}

function inside(roi: [number, number, number, number] | null, x: number, y: number): boolean {
  if (!roi) return true
  return x >= roi[0] && x <= roi[2] && y >= roi[1] && y <= roi[3]
}

export interface PlacementResult {
  port: PortSpec | null
  /** Why this spot, or why none — shown to the user rather than kept internal. */
  reason: string
  /** Other pads on the net, so the UI can offer them without re-deriving. */
  alternatives: BoardPad[]
}

/**
 * Place a port on a net.
 *
 * Preference order, and the reasoning behind it:
 *
 * 1. A pad inside the region of interest — a port outside the meshed region excites
 *    geometry that is not being simulated.
 * 2. A two-pin part's pad over a many-pin part's, since a series element or a terminator
 *    is a better-defined driving point than one pin of a 100-ball BGA.
 * 3. Otherwise the first pad, so the answer is at least deterministic.
 */
export function placePortOnNet(
  doc: BoardDoc,
  net: string,
  roi: [number, number, number, number] | null,
  index = 0,
): PlacementResult {
  if (isReferenceNet(net)) {
    return {
      port: null,
      alternatives: [],
      reason:
        `${net} looks like a reference net. A port drives a signal against its return, so ` +
        `exciting the return itself gives a result that is not about your board. Pick the ` +
        `signal net instead.`,
    }
  }

  const all = padsOnNet(doc, net)
  if (all.length === 0) {
    return {
      port: null,
      alternatives: [],
      reason: `${net} has no pads, so there is nowhere obvious to drive it. Click a pad instead.`,
    }
  }

  const withinRoi = all.filter((p) => inside(roi, p.x, p.y))
  const candidates = withinRoi.length > 0 ? withinRoi : all

  const pinCount = new Map<string, number>()
  for (const p of doc.pads) {
    if (p.ref) pinCount.set(p.ref, (pinCount.get(p.ref) ?? 0) + 1)
  }
  const ranked = [...candidates].sort(
    (a, b) => (pinCount.get(a.ref) ?? 99) - (pinCount.get(b.ref) ?? 99),
  )
  const chosen = ranked[0]

  const note =
    withinRoi.length === 0 && roi
      ? ` No pad on this net is inside the region, so the port sits outside it — widen the region or move the port.`
      : ''

  return {
    port: {
      name: `p${index + 1}`,
      x_mm: chosen.x,
      y_mm: chosen.y,
      layer: padLayer(doc, chosen),
      half_width_mm: 0.2,
      resistance: 50,
      excited: index === 0,
      origin: 'net',
      net,
      padRef: chosen.ref ? `${chosen.ref}.${chosen.number}` : undefined,
    },
    alternatives: ranked.slice(1, 8),
    reason:
      `Placed on ${chosen.ref || 'a pad'}${chosen.number ? `.${chosen.number}` : ''} ` +
      `at ${chosen.x.toFixed(2)}, ${chosen.y.toFixed(2)} mm.${note}`,
  }
}

/** Place a port on a specific pad. */
export function placePortOnPad(doc: BoardDoc, pad: BoardPad, index = 0): PortSpec {
  return {
    name: `p${index + 1}`,
    x_mm: pad.x,
    y_mm: pad.y,
    layer: padLayer(doc, pad),
    half_width_mm: 0.2,
    resistance: 50,
    excited: index === 0,
    origin: 'pad',
    net: pad.net || undefined,
    padRef: pad.ref ? `${pad.ref}.${pad.number}` : undefined,
  }
}

/** Something clickable on the board that a port can be attached to. */
export type PortAnchor =
  | { kind: 'pad'; pad: BoardPad }
  | { kind: 'via'; via: BoardVia }

/**
 * The nearest pad or via to a board-space point, within ``maxDistanceMm``.
 *
 * Vias count. Zoomed in, the obvious round targets on a board are mostly vias, and a via on
 * a signal net is a perfectly good place to inject — it is exactly where that signal
 * changes layer. Searching only pads meant clicking the most clickable-looking thing on the
 * screen silently did nothing.
 *
 * The tolerance comes from the current zoom so the target stays a constant size on screen:
 * a fixed millimetre tolerance is unusable zoomed out and needlessly precise zoomed in.
 */
export function anchorNear(
  doc: BoardDoc,
  x: number,
  y: number,
  maxDistanceMm: number,
): PortAnchor | null {
  let best: PortAnchor | null = null
  let bestD = maxDistanceMm

  for (const pad of doc.pads) {
    const d = Math.hypot(pad.x - x, pad.y - y)
    if (d < bestD) {
      bestD = d
      best = { kind: 'pad', pad }
    }
  }
  for (const via of doc.vias) {
    // Slight bias toward pads at equal distance: a pad is where a part drives, a via is
    // only where the signal passes through.
    const d = Math.hypot(via.x - x, via.y - y) * 1.05
    if (d < bestD) {
      bestD = d
      best = { kind: 'via', via }
    }
  }
  return best
}

/** Backwards-compatible pad-only lookup. */
export function padNear(
  doc: BoardDoc,
  x: number,
  y: number,
  maxDistanceMm: number,
): BoardPad | null {
  const a = anchorNear(doc, x, y, maxDistanceMm)
  return a?.kind === 'pad' ? a.pad : null
}

/** Place a port on whichever anchor was clicked. */
export function placePortOnAnchor(
  doc: BoardDoc,
  anchor: PortAnchor,
  index = 0,
): PortSpec {
  if (anchor.kind === 'pad') return placePortOnPad(doc, anchor.pad, index)

  const via = anchor.via
  const copper = doc.layers.map((l) => l.name)
  const layer = via.layers.find((l) => copper.includes(l)) ?? copper[0] ?? ''
  return {
    name: `p${index + 1}`,
    x_mm: via.x,
    y_mm: via.y,
    layer,
    half_width_mm: Math.max(0.15, (via.size_mm || via.drill_mm || 0.4) / 2),
    resistance: 50,
    excited: index === 0,
    origin: 'pad',
    net: via.net || undefined,
    padRef: 'via',
  }
}

/**
 * A region of interest around a set of ports, clamped to the board.
 *
 * Offered as a starting point so the user adjusts a sensible rectangle rather than drawing
 * one from nothing. The margin is generous because a trace leaving the region still carries
 * current back into it.
 */
export function suggestRoi(
  doc: BoardDoc,
  ports: PortSpec[],
  marginMm = 6,
): [number, number, number, number] {
  const { width_mm, height_mm } = doc.board
  if (ports.length === 0) {
    // No ports yet: propose a modest square in the middle rather than the whole board,
    // which would be far too expensive to solve.
    const side = Math.min(20, width_mm * 0.5, height_mm * 0.5)
    const cx = width_mm / 2
    const cy = height_mm / 2
    return [cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2]
  }
  const xs = ports.map((p) => p.x_mm)
  const ys = ports.map((p) => p.y_mm)
  return [
    Math.max(0, Math.min(...xs) - marginMm),
    Math.max(0, Math.min(...ys) - marginMm),
    Math.min(width_mm, Math.max(...xs) + marginMm),
    Math.min(height_mm, Math.max(...ys) + marginMm),
  ]
}

/** Harmonics of a fundamental, up to a ceiling — the usual way clock frequencies are named. */
export function harmonics(fundamentalHz: number, count: number, maxHz: number): number[] {
  const out: number[] = []
  for (let n = 1; out.length < count; n += 2) {
    // Odd harmonics: a square-wave clock has essentially no even ones, so offering them
    // would spend solver time on frequencies the source does not produce.
    const f = fundamentalHz * n
    if (f > maxHz) break
    out.push(f)
  }
  return out
}

export function formatHz(hz: number): string {
  if (hz >= 1e9) return `${(hz / 1e9).toFixed(hz % 1e9 === 0 ? 0 : 2)} GHz`
  if (hz >= 1e6) return `${(hz / 1e6).toFixed(hz % 1e6 === 0 ? 0 : 1)} MHz`
  if (hz >= 1e3) return `${(hz / 1e3).toFixed(0)} kHz`
  return `${hz} Hz`
}
