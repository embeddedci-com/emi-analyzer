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
 * It leaves out the worker's lines along diagonal traces: read off the triangles they doubled
 * the count, since a triangle cannot say which of its sides is a trace's. The worker publishes
 * the real mesh within seconds.
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
/** A right triangle with a side this long along an axis, over five times its shortest, is half
 *  a straight trace span. No trace is narrower than the floor; a cap's chords are. */
const SPAN_MIN_MM = 0.5
const WIDTH_FLOOR_MM = 0.05

export interface MeshCount {
  cells: number
  lines: [number, number, number]
  /** The smallest cell expected, mm: it sets the timestep. */
  min_cell_mm: number
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

  if (geometry && index) {
    const v = new Float32Array(geometry)
    for (const g of index.groups) {
      if (want && !want.has(g.net)) continue
      for (let t = g.offset; t + 2 < g.offset + g.count && 2 * (t + 2) + 1 < v.length; t += 3) {
        const p = [0, 1, 2].map((k) => [v[2 * (t + k)], v[2 * (t + k) + 1]] as const)
        if (!p.some(([x, y]) => inside(x, y))) continue
        for (const [x, y] of p) {
          xs.push(x)
          ys.push(y)
        }
        // Half of a straight trace span is a long, thin right triangle: its shortest side is
        // the trace's width, and its middle one runs along an axis. A trace narrower than two
        // cells sets a finer cell (model.py `THIRDS_RULE`).
        const sides = [[p[0], p[1]], [p[1], p[2]], [p[2], p[0]]]
          .map(([a, b]) => ({ dx: Math.abs(b[0] - a[0]), dy: Math.abs(b[1] - a[1]) }))
          .map((e) => ({ ...e, len: Math.hypot(e.dx, e.dy) }))
          .sort((a, b) => a.len - b.len)
        const [short, along, hyp] = sides
        const right = Math.abs(hyp.len ** 2 - short.len ** 2 - along.len ** 2) < 1e-3 * hyp.len ** 2
        if (right && along.len >= SPAN_MIN_MM && short.len >= WIDTH_FLOOR_MM
            && short.len * 5 <= along.len && Math.min(along.dx, along.dy) < 1e-3) {
          narrowest = Math.min(narrowest, short.len)
        }
      }
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
