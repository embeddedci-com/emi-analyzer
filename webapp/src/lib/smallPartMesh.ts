/**
 * How many cells a small part's mesh will have, counted the way the worker builds it.
 *
 * The first estimate was cells per mm² of the region, fitted to five meshed coupons. Against
 * the runs of the September 2026 checks it was 0.45x to 4.4x the real mesh: the mesh is set by
 * the net's copper edges and graded out across empty board, so a big coupon around a straight
 * net (the sample board's DATA, 56 x 52 mm) meshes to a third of what its area says, and a
 * short one with diagonals and arcs to twice. It also refused the sample board's nets on
 * normal, which the worker then accepts.
 *
 * So this lays out grid lines the way `worker/emi_worker/openems/mesh.py` does, from what the
 * browser has: the nets' copper (the viewer's triangles, which carry every track edge, pad and
 * via ring), the ports, and the stackup. Lines closer than half a cell merge, gaps fill with
 * cells growing 1.4x a step up to the band's largest, and each end gets eight absorbing lines.
 * On the fourteen coupons meshed for the checks it came within 0.76x to 1.9x of the real mesh.
 *
 * Traces are read off the triangles' outline: a straight span is two long parallel edges a
 * trace's width apart, however the viewer fanned the triangles inside it. An axis-aligned one
 * that is narrow sets the smallest cell; a diagonal one gets lines along both axes it spans, as
 * the worker gives it. Without those lines the sample board's SPI_CLK counted 1.0 M cells where
 * the worker meshed 2.4 M, and the app offered a normal mesh the worker then refused. The worker
 * publishes the real mesh within seconds.
 */

import type { BoardDoc, GeometryIndex } from './boardTypes'
import type { PortSpec } from './portPlacement'

const C0 = 299_792_458
const RATIO = 1.4
const PML_LINES = 8
const CELLS_PER_WAVELENGTH = 20
/** The small-part mode's merge (stages/small_part.py `MERGE_FRACTION`). */
const MERGE_FRACTION = 0.5
/** Air above and below the board, mm (model.py `DEFAULT_AIR_MM`). */
const AIR_MM = 5
/** Two parallel outline edges this long, at least five times further apart than the floor and
 *  at most five times closer than their length, are a straight trace span. No trace is narrower
 *  than the floor; a cap's chords are. */
const SPAN_MIN_MM = 0.5
const WIDTH_FLOOR_MM = 0.05

export interface MeshCount {
  cells: number
  lines: [number, number, number]
  /** The smallest cell expected, mm: it sets the timestep. */
  min_cell_mm: number
}

type Pt = readonly [number, number]

interface Edge {
  key: string
  a: Pt
  b: Pt
  len: number
  /** Unit direction, pointing to +x (or +y when vertical). */
  u: Pt
}

interface Span {
  /** The trace's centerline and width, mm. */
  a: Pt
  b: Pt
  w: number
  diagonal: boolean
}

function edge(p: Pt, q: Pt): Edge | null {
  const len = Math.hypot(q[0] - p[0], q[1] - p[1])
  if (len < SPAN_MIN_MM) return null
  const [a, b] = p[0] < q[0] || (p[0] === q[0] && p[1] < q[1]) ? [p, q] : [q, p]
  const key = [a[0], a[1], b[0], b[1]].map((c) => c.toFixed(4)).join(',')
  return { key, a, b, len, u: [(b[0] - a[0]) / len, (b[1] - a[1]) / len] }
}

/** Straight trace spans: pairs of long outline edges, parallel and a trace's width apart, of
 *  the same length and side by side. The centerline runs between their matching ends. */
function spans(edges: Edge[]): Span[] {
  const out: Span[] = []
  const used = new Set<number>()
  for (let i = 0; i < edges.length; i++) {
    if (used.has(i)) continue
    const e = edges[i]
    for (let j = i + 1; j < edges.length; j++) {
      if (used.has(j)) continue
      const f = edges[j]
      if (Math.abs(e.u[0] * f.u[1] - e.u[1] * f.u[0]) > 1e-3) continue
      if (Math.abs(e.len - f.len) > 0.01 * e.len) continue
      const ox = f.a[0] - e.a[0]
      const oy = f.a[1] - e.a[1]
      const w = Math.abs(e.u[0] * oy - e.u[1] * ox)
      const along = e.u[0] * ox + e.u[1] * oy
      if (w < WIDTH_FLOOR_MM || w * 5 > e.len || Math.abs(along) > w) continue
      used.add(i)
      used.add(j)
      out.push({
        a: [(e.a[0] + f.a[0]) / 2, (e.a[1] + f.a[1]) / 2],
        b: [(e.b[0] + f.b[0]) / 2, (e.b[1] + f.b[1]) / 2],
        w,
        diagonal: Math.min(Math.abs(e.u[0]), Math.abs(e.u[1])) > 1e-3,
      })
      break
    }
  }
  return out
}

/** The largest cell the band allows, mm (mesh.py `max_cell_for_frequency`). */
export function maxCellMm(doc: BoardDoc, fMaxHz: number): number {
  const er = Math.max(1, ...doc.stackup.filter((s) => s.role === 'dielectric').map((s) => s.epsilon_r ?? 4.4))
  return (C0 / (fMaxHz * Math.sqrt(er))) * 1000 / CELLS_PER_WAVELENGTH
}

function merge(values: number[], tol: number): number[] {
  const s = [...values].sort((a, b) => a - b)
  const out: number[] = []
  for (const v of s) if (!out.length || v - out[out.length - 1] >= tol) out.push(v)
  return out
}

/** Cells a gap of `length` fills with, growing from `start` at `ends` ends up to `maxRes`. */
function fillCount(length: number, start: number, maxRes: number, ends = 2): number {
  let total = 0
  let n = 0
  let step = Math.min(start * RATIO, maxRes)
  // Two series, one from each end, meet in the middle: count cells in pairs.
  while (total < length && n < 4000) {
    total += ends * step
    n += ends
    step = Math.min(step * RATIO, maxRes)
  }
  return Math.max(1, n)
}

/** Lines on one axis: the required ones merged, the gaps graded, and the absorbing padding. */
function axisLines(required: number[], minRes: number, maxRes: number, mergeTol: number): {
  count: number
  smallest: number
} {
  const lines = merge(required, mergeTol)
  if (lines.length < 2) return { count: 2 + 2 * PML_LINES, smallest: minRes }
  let count = 1
  let smallest = Infinity
  const gaps = lines.slice(1).map((v, i) => v - lines[i])
  gaps.forEach((g, i) => {
    const neighbour = Math.min(gaps[i - 1] ?? g, gaps[i + 1] ?? g, g)
    smallest = Math.min(smallest, g)
    count += g <= Math.min(maxRes, neighbour * RATIO) ? 1 : fillCount(g, neighbour, maxRes)
  })
  return { count: count + 2 * PML_LINES, smallest }
}

/** The z axis: every copper layer, the dielectrics sliced at dz, the air graded out. */
function zLines(doc: BoardDoc, dzMm: number, maxRes: number): { count: number; smallest: number } {
  const stack = doc.stackup.filter((s) => s.thickness_mm > 0 || s.role === 'copper')
  let count = doc.layers.length || 2
  let smallest = Infinity
  for (const s of stack) {
    if (s.role !== 'dielectric' || s.thickness_mm <= 0) continue
    const n = Math.max(1, Math.ceil(s.thickness_mm / dzMm))
    count += n - 1
    smallest = Math.min(smallest, s.thickness_mm / n)
  }
  if (!Number.isFinite(smallest)) smallest = dzMm
  count += 2 * fillCount(AIR_MM, smallest, maxRes, 1) + 2 * PML_LINES
  return { count, smallest }
}

/**
 * The mesh a small part gets, counted. `nets` null means a drawn region: all copper in it.
 */
export function countSmallPartMesh(
  doc: BoardDoc,
  geometry: ArrayBuffer | null,
  index: GeometryIndex | null,
  nets: string[] | null,
  roi: [number, number, number, number],
  ports: PortSpec[],
  cell: { dx: number; dy: number; dz: number },
  fMaxHz: number,
): MeshCount {
  const [x0, y0, x1, y1] = roi
  const inside = (x: number, y: number) => x >= x0 && x <= x1 && y >= y0 && y <= y1
  const want = nets ? new Set(nets) : null
  const xs: number[] = [x0, x1]
  const ys: number[] = [y0, y1]
  const dx = cell.dx / 1000
  const dy = cell.dy / 1000
  let narrowest = Infinity
  const diagonals: Span[] = []

  if (geometry && index) {
    const v = new Float32Array(geometry)
    for (const g of index.groups) {
      if (want && !want.has(g.net)) continue
      // The group's outline: an edge two triangles share is inside the copper (null here).
      const edges = new Map<string, Edge | null>()
      for (let t = g.offset; t + 2 < g.offset + g.count && 2 * (t + 2) + 1 < v.length; t += 3) {
        const p = [0, 1, 2].map((k) => [v[2 * (t + k)], v[2 * (t + k) + 1]] as const)
        if (!p.some(([x, y]) => inside(x, y))) continue
        for (const [x, y] of p) {
          xs.push(x)
          ys.push(y)
        }
        for (const [i, j] of [[0, 1], [1, 2], [2, 0]] as const) {
          const e = edge(p[i], p[j])
          if (e) edges.set(e.key, edges.has(e.key) ? null : e)
        }
      }
      // A trace narrower than two cells sets a finer cell (model.py `THIRDS_RULE`).
      for (const span of spans([...edges.values()].filter((e): e is Edge => !!e))) {
        if (span.diagonal) diagonals.push(span)
        else narrowest = Math.min(narrowest, span.w)
      }
    }
  }
  // Lines along both axes a diagonal spans, no further apart than 0.7 of its width and no
  // closer than half of it or the preset's cell (model.py `_copper_features`).
  const edgeCell = Math.min(dx, dy)
  for (const { a, b, w } of diagonals) {
    const step = Math.min(Math.max(edgeCell, w / 2), 0.7 * w)
    for (const [lo, hi, axis] of [[Math.min(a[0], b[0]), Math.max(a[0], b[0]), xs],
      [Math.min(a[1], b[1]), Math.max(a[1], b[1]), ys]] as const) {
      const n = Math.ceil((hi - lo) / step)
      for (let k = 1; k < n; k++) axis.push(lo + ((hi - lo) * k) / n)
    }
  }
  for (const via of doc.vias) {
    if ((want && !want.has(via.net)) || !inside(via.x, via.y)) continue
    const r = (via.drill_mm || via.size_mm) / 2
    xs.push(via.x - r, via.x + r)
    ys.push(via.y - r, via.y + r)
  }
  for (const p of ports) {
    xs.push(p.x_mm - p.half_width_mm, p.x_mm, p.x_mm + p.half_width_mm)
    ys.push(p.y_mm - p.half_width_mm, p.y_mm, p.y_mm + p.half_width_mm)
  }

  const maxRes = maxCellMm(doc, fMaxHz)
  const inRoi = (vals: number[], lo: number, hi: number) => vals.filter((v) => v >= lo && v <= hi)
  const ax = axisLines(inRoi(xs, x0, x1), dx, maxRes, dx * MERGE_FRACTION)
  const ay = axisLines(inRoi(ys, y0, y1), dy, maxRes, dy * MERGE_FRACTION)
  const az = zLines(doc, cell.dz / 1000, maxRes)
  // A trace narrower than two cells is ruled at half its width (model.py `THIRDS_RULE`).
  const inPlane = Math.min(Math.max(ax.smallest, dx * MERGE_FRACTION),
    Math.max(ay.smallest, dy * MERGE_FRACTION),
    narrowest <= 2 * Math.min(dx, dy) ? narrowest / 2 : Infinity)
  return {
    cells: ax.count * ay.count * az.count,
    lines: [ax.count, ay.count, az.count],
    min_cell_mm: Math.min(inPlane, az.smallest),
  }
}
