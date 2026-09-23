/**
 * The small-part solve: one net (or a pair) cut out over its planes, or a small drawn region.
 *
 * This is the browser's half of `worker/emi_worker/stages/small_part.py` and
 * `worker/emi_worker/openems/coupon.py`. The browser plans the coupon (its region and its
 * ports) and says what it will cost; the worker cuts the copper, meshes, and enforces the
 * budget against the real mesh. The two share their constants by hand, and a test pins them
 * (`smallPart.test.ts` against `server/emi/testdata/small_part_fixtures.json`).
 *
 * The cost shown here is an estimate from the region's size, fitted to coupons that were
 * actually meshed (docs/verification/small-part-solve.md). The worker publishes the real
 * mesh within seconds of starting and refuses a part its budget does not cover.
 */

import type { BoardDoc, BoardPad, GeometryIndex } from './boardTypes'
import { isReferenceNet, padLayer, type PortSpec } from './portPlacement'

export const SMALL_PART_MODE = 'small_part'

/** Mirrors of the worker's constants. Pinned by the shared fixture. */
export const MIN_MARGIN_MM = 3.0
export const MARGIN_HEIGHTS = 5.0
export const MAX_SIDE_MM = 60.0
export const PORT_HALF_WIDTH_MM = 0.2
export const MAX_CELLS = 3_000_000
export const MAX_CELL_STEPS = 1.5e11
export const MIN_RECORD_S = 10e-9
export const END_CRITERIA_DB = -50

/**
 * Cells per mm² of region by preset, and how much each copper layer past two adds, fitted to
 * meshed coupons; and the simulated time a terminated coupon took to ring down. Pinned by the
 * shared fixture and measured by `worker/research/sp_real_coupons.py`.
 */
export const CELL_FIT: Record<string, { per_mm2: number; per_layer: number }> = {
  coarse: { per_mm2: 500, per_layer: 0.8 },
  normal: { per_mm2: 1200, per_layer: 0.8 },
}
export const TYPICAL_RECORD_S = 5e-9

/** The worker's pessimistic throughput, MC/s. */
export const ASSUMED_MCELLS_S = 150

/**
 * Bands a user can pick. The first is the default: it keeps a terminated part's record to a
 * few nanoseconds while reaching past the fifth harmonic of most board clocks.
 */
export const BANDS = [
  { value: '100M-2G', label: '100 MHz to 2 GHz', lo: 100e6, hi: 2e9 },
  { value: '200M-1G', label: '200 MHz to 1 GHz', lo: 200e6, hi: 1e9 },
  { value: '300M-3G', label: '300 MHz to 3 GHz', lo: 300e6, hi: 3e9 },
] as const

export const PRESETS = [
  { value: 'coarse', label: 'Coarse', dx: 150, dy: 150, dz: 100 },
  { value: 'normal', label: 'Normal', dx: 75, dy: 75, dz: 50 },
] as const

export type Roi = [number, number, number, number]

/** Copper kept around the net, mm: max(3 mm, 5 heights of the outer dielectric). */
export function marginFor(doc: BoardDoc): number {
  const stack = doc.stackup.filter((s) => s.thickness_mm > 0 || s.role === 'copper')
  const copper = stack.map((s, i) => (s.role === 'copper' ? i : -1)).filter((i) => i >= 0)
  const outer: number[] = []
  if (copper.length) {
    for (const [k, step] of [[copper[0], 1], [copper[copper.length - 1], -1]] as const) {
      const j = k + step
      if (j >= 0 && j < stack.length && stack[j].role === 'dielectric') outer.push(stack[j].thickness_mm)
    }
  }
  const h = outer.length ? Math.max(...outer) : doc.board.thickness_mm
  return Math.max(MIN_MARGIN_MM, MARGIN_HEIGHTS * h)
}

/** The extent of some nets' copper, from the viewer's geometry plus pads and vias. */
export function netExtent(
  doc: BoardDoc, geometry: ArrayBuffer | null, index: GeometryIndex | null, nets: string[],
): Roi | null {
  const want = new Set(nets)
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  const add = (x: number, y: number) => {
    if (x < x0) x0 = x
    if (x > x1) x1 = x
    if (y < y0) y0 = y
    if (y > y1) y1 = y
  }
  if (geometry && index) {
    const v = new Float32Array(geometry)
    for (const g of index.groups) {
      if (!want.has(g.net)) continue
      for (let k = g.offset; k < g.offset + g.count && 2 * k + 1 < v.length; k++) add(v[2 * k], v[2 * k + 1])
    }
  }
  for (const p of doc.pads) if (want.has(p.net)) add(p.x, p.y)
  for (const via of doc.vias) {
    if (!want.has(via.net)) continue
    const r = via.size_mm / 2
    add(via.x - r, via.y - r)
    add(via.x + r, via.y + r)
  }
  return Number.isFinite(x0) ? [x0, y0, x1, y1] : null
}

/** The other half of a differential pair, if the net's name says it has one. */
export function pairOf(doc: BoardDoc, net: string): string | null {
  const names = new Set(doc.nets.map((n) => n.name))
  for (const [a, b] of [['+', '-'], ['_P', '_N'], ['_p', '_n'], ['P', 'N']]) {
    for (const [from, to] of [[a, b], [b, a]]) {
      if (!net.endsWith(from)) continue
      const other = net.slice(0, -from.length) + to
      if (names.has(other)) return other
    }
  }
  return null
}

function pinCounts(doc: BoardDoc): Map<string, number> {
  const out = new Map<string, number>()
  for (const p of doc.pads) if (p.ref) out.set(p.ref, (out.get(p.ref) ?? 0) + 1)
  return out
}

/** The net's two ends: the pads furthest apart, the one on the part with most pins first. */
export function netEnds(doc: BoardDoc, net: string): BoardPad[] {
  const pads = doc.pads.filter((p) => p.net === net && p.type !== 'np_thru_hole')
  if (pads.length <= 1) return pads
  let best: [number, BoardPad, BoardPad] = [-1, pads[0], pads[1]]
  for (let i = 0; i < pads.length; i++) {
    for (let j = i + 1; j < pads.length; j++) {
      const d = Math.hypot(pads[i].x - pads[j].x, pads[i].y - pads[j].y)
      if (d > best[0]) best = [d, pads[i], pads[j]]
    }
  }
  const pins = pinCounts(doc)
  const key = (p: BoardPad) => [-(pins.get(p.ref) ?? 0), p.ref, p.number] as const
  return [best[1], best[2]].sort((a, b) => {
    const ka = key(a), kb = key(b)
    return ka[0] - kb[0] || ka[1].localeCompare(kb[1]) || ka[2].localeCompare(kb[2])
  })
}

export interface CouponPlan {
  nets: string[]
  roi: Roi | null
  ports: PortSpec[]
  margin_mm: number
  notes: string[]
  /** Why there is no coupon, in the user's words. */
  error: string | null
}

/** The coupon for these nets: its region and its ports, as the worker's `coupon.plan` makes it. */
export function planCoupon(
  doc: BoardDoc, geometry: ArrayBuffer | null, index: GeometryIndex | null, nets: string[],
): CouponPlan {
  const margin = marginFor(doc)
  const none = (error: string): CouponPlan => ({ nets, roi: null, ports: [], margin_mm: margin, notes: [], error })
  if (nets.length === 0) return none('Pick a net.')
  const ref = nets.find(isReferenceNet)
  if (ref) return none(`${ref} is a ground net. Pick the signal or supply that returns through it.`)
  const ext = netExtent(doc, geometry, index, nets)
  if (!ext) return none(`${nets.join(', ')} has no copper on this board.`)
  const roi: Roi = [ext[0] - margin, ext[1] - margin, ext[2] + margin, ext[3] + margin]
  const side = Math.max(roi[2] - roi[0], roi[3] - roi[1])
  if (side > MAX_SIDE_MM) {
    return none(
      `${nets.join(', ')} spans ${(side - 2 * margin).toFixed(0)} mm, a ${side.toFixed(0)} mm ` +
      `part with its planes. Small-part solves stop at ${MAX_SIDE_MM} mm. Pick a shorter net, ` +
      `or draw a region around the part you care about.`,
    )
  }
  const ports: PortSpec[] = []
  const notes: string[] = []
  for (const net of nets) {
    const ends = netEnds(doc, net)
    if (ends.length === 0) {
      notes.push(`${net} has no pads, so it has no port.`)
      continue
    }
    for (const pad of ends) {
      ports.push({
        name: `p${ports.length + 1}`, x_mm: pad.x, y_mm: pad.y, layer: padLayer(doc, pad),
        half_width_mm: PORT_HALF_WIDTH_MM, resistance: 50, excited: ports.length === 0,
        origin: 'net', net, padRef: pad.ref ? `${pad.ref}.${pad.number}` : undefined,
      })
    }
    if (ends.length === 1) notes.push(`${net} has one pad, so its far end is open.`)
  }
  if (ports.length === 0) return none(`${nets.join(', ')} has no pads to put a port on.`)
  return { nets, roi, ports, margin_mm: margin, notes, error: null }
}

export interface SmallPartEstimate {
  cells: number
  /** Smallest cell the mesh is expected to reach, mm, and the timestep it sets. */
  dt_seconds: number
  /** The timestep cap the worker will give it: three periods of the band's lowest frequency,
   *  or what the budget affords. */
  cap_steps: number
  /** Simulated time the cap allows, s. */
  record_s: number
  /** Wall time if the part rings down as the measured coupons did, and at the cap, s. */
  typical_seconds: number
  worst_seconds: number
  /** Why the worker would refuse it, or null. */
  refused: string | null
}

/**
 * What a part will cost. Cells come from a fit to meshed coupons (per preset, per mm² of
 * region, and per copper layer past two), and the timestep from the smallest cell the mesher
 * will make for a small part.
 */
export function estimateSmallPart(
  roi: Roi, preset: (typeof PRESETS)[number], band: (typeof BANDS)[number], doc: BoardDoc,
): SmallPartEstimate {
  const fit = CELL_FIT[preset.value]
  const area = Math.max(0, roi[2] - roi[0]) * Math.max(0, roi[3] - roi[1])
  const layers = doc.layers.length || 2
  const cells = Math.ceil(fit.per_mm2 * area * (1 + fit.per_layer * (layers - 2)))
  // The smallest cell: half of dx in plane (copper lines closer than that merge, and a routed
  // board always has some that close), or the thinnest slice of a dielectric at dz.
  const slices = doc.stackup
    .filter((s) => s.role === 'dielectric' && s.thickness_mm > 0)
    .map((s) => (s.thickness_mm * 1000) / Math.ceil((s.thickness_mm * 1000) / preset.dz))
  const dMin = Math.min(preset.dx / 2, preset.dz, ...slices) * 1e-6
  const dt = dMin / (299_792_458 * Math.sqrt(3))
  const needed = Math.ceil(3 / band.lo / dt)
  const affordable = Math.floor(MAX_CELL_STEPS / Math.max(cells, 1))
  const cap = Math.min(needed, affordable)
  const perStep = cells / (ASSUMED_MCELLS_S * 1e6)
  const typicalSteps = Math.min(cap, Math.ceil(TYPICAL_RECORD_S / dt))
  let refused: string | null = null
  if (cells > MAX_CELLS) {
    refused = `About ${fmtM(cells)} cells, over the ${fmtM(MAX_CELLS)} a small-part solve allows.`
  } else if (cap * dt < MIN_RECORD_S) {
    refused = `At about ${fmtM(cells)} cells the budget buys ${(cap * dt * 1e9).toFixed(1)} ns of ` +
      `simulated time; a part needs at least ${MIN_RECORD_S * 1e9} ns to ring down.`
  }
  return {
    cells, dt_seconds: dt, cap_steps: cap, record_s: cap * dt,
    typical_seconds: typicalSteps * perStep, worst_seconds: cap * perStep, refused,
  }
}

function fmtM(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : `${Math.round(n / 1e3)} k`
}

/** What the app sends for a small-part solve. The worker validates all of it again. */
export function smallPartParams(opts: {
  roi: Roi
  ports: PortSpec[]
  nets: string[]
  band: (typeof BANDS)[number]
  preset: (typeof PRESETS)[number]
  frequencies_hz: number[]
}) {
  const { roi, ports, nets, band, preset } = opts
  return {
    mode: SMALL_PART_MODE,
    roi: { min_x_mm: roi[0], min_y_mm: roi[1], max_x_mm: roi[2], max_y_mm: roi[3] },
    band_hz: [band.lo, band.hi],
    frequencies_hz: opts.frequencies_hz.filter((f) => f >= band.lo && f <= band.hi),
    mesh: { dx_um: preset.dx, dy_um: preset.dy, dz_um: preset.dz },
    ports: ports.map((p) => ({
      name: p.name, x_mm: p.x_mm, y_mm: p.y_mm, layer: p.layer,
      half_width_mm: p.half_width_mm, resistance: p.resistance, excited: p.excited,
      ...(p.net ? { net: p.net } : {}), ...(p.padRef ? { pad: p.padRef } : {}),
    })),
    ...(nets.length ? { coupon: { nets } } : {}),
  }
}

export function isSmallPartRun(params: unknown): boolean {
  return (params as { mode?: string } | undefined)?.mode === SMALL_PART_MODE
}
