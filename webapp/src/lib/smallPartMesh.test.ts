/**
 * The counted mesh estimate (`smallPartMesh.ts`). Against real meshes it was checked on the
 * coupons of docs/verification/small-part-solve.md; these pin how it behaves.
 */

import { describe, expect, it } from 'vitest'
import type { BoardDoc } from './boardTypes'
import { countSmallPartMesh, maxCellMm } from './smallPartMesh'

/** Two triangles of a straight trace from (x0, y) to (x1, y), `w` wide, on net CLK. */
function trace(x0: number, x1: number, y: number, w: number): ArrayBuffer {
  const a = [x0, y - w / 2], b = [x1, y - w / 2], c = [x1, y + w / 2], d = [x0, y + w / 2]
  return new Float32Array([...a, ...b, ...c, ...a, ...c, ...d]).buffer
}

/** A trace from (x0, y0) to (x1, y1), `w` wide, as the viewer draws it: one fan of triangles
 *  from a corner, round ends left out. `fan` false gives the rectangle's two triangles. */
function slanted(x0: number, y0: number, x1: number, y1: number, w: number, fan = true): ArrayBuffer {
  const len = Math.hypot(x1 - x0, y1 - y0)
  const nx = (-(y1 - y0) / len) * (w / 2)
  const ny = ((x1 - x0) / len) * (w / 2)
  const a = [x0 + nx, y0 + ny], b = [x1 + nx, y1 + ny], c = [x1 - nx, y1 - ny], d = [x0 - nx, y0 - ny]
  if (!fan) return new Float32Array([...a, ...b, ...c, ...a, ...c, ...d]).buffer
  // A fan from a point on the short side at a, as the viewer's triangulation does.
  const m = [(a[0] + d[0]) / 2, (a[1] + d[1]) / 2]
  return new Float32Array([...m, ...a, ...b, ...m, ...b, ...c, ...m, ...c, ...d]).buffer
}

function doc(): BoardDoc {
  const cu = (name: string) => ({ name, role: 'copper', type: 'copper', thickness_mm: 0.035, material: '', epsilon_r: null, loss_tangent: null, from_file: true, z_bottom_mm: 0, z_top_mm: 0 })
  return {
    layers: [{ name: 'F.Cu', kind: 'signal', index: 0, z_mm: 0.2 }, { name: 'B.Cu', kind: 'signal', index: 1, z_mm: 0 }],
    stackup: [cu('F.Cu'),
      { name: 'core', role: 'dielectric', type: 'core', thickness_mm: 0.2, material: '', epsilon_r: 4.4, loss_tangent: 0.02, from_file: true, z_bottom_mm: 0, z_top_mm: 0 },
      cu('B.Cu')],
    vias: [],
    pads: [],
    nets: [],
    geometry: { file: 'geometry.bin', dtype: 'float32', components: 2, primitive: 'triangles', vertex_count: 6, byte_length: 48, groups: [{ layer: 'F.Cu', net: 'CLK', offset: 0, count: 6 }] },
  } as unknown as BoardDoc
}

const COARSE = { dx: 150, dy: 150, dz: 100 }

describe('the counted mesh', () => {
  it('holds the band to twenty cells a wavelength', () => {
    expect(maxCellMm(doc(), 2e9)).toBeCloseTo(3.57, 2)
  })

  it('grows slowly with the margin: empty board is graded, not gridded', () => {
    const g = trace(5, 25, 10, 0.4)
    const small = countSmallPartMesh(doc(), g, doc().geometry, ['CLK'], [2, 7, 28, 13], [], COARSE, 2e9)
    const big = countSmallPartMesh(doc(), g, doc().geometry, ['CLK'], [-3, 2, 33, 18], [], COARSE, 2e9)
    expect(big.lines[0] - small.lines[0]).toBeLessThan(10)
    expect(big.cells / small.cells).toBeLessThan(1.5)
    expect(small.lines[2]).toBe(big.lines[2])
  })

  it('counts the other nets\' copper only when asked for a region', () => {
    const g = trace(5, 25, 10, 0.4)
    const roi: [number, number, number, number] = [2, 7, 28, 13]
    const none = countSmallPartMesh(doc(), g, doc().geometry, ['DATA'], roi, [], COARSE, 2e9)
    const region = countSmallPartMesh(doc(), g, doc().geometry, null, roi, [], COARSE, 2e9)
    expect(region.lines[1]).toBeGreaterThan(none.lines[1])
  })

  it('lays lines along both axes a diagonal trace spans, however it was triangulated', () => {
    const roi: [number, number, number, number] = [2, 2, 18, 18]
    const count = (g: ArrayBuffer) => {
      const d = doc()
      d.geometry.groups[0].count = new Float32Array(g).length / 2
      return countSmallPartMesh(d, g, d.geometry, ['CLK'], roi, [], COARSE, 2e9)
    }
    // 10 mm along each axis at 0.15 mm apart (half the trace's width, which is the cell here).
    const fan = count(slanted(5, 5, 15, 15, 0.3))
    const rect = count(slanted(5, 5, 15, 15, 0.3, false))
    expect(fan.lines[0]).toBeGreaterThan(66)
    expect(fan.lines[1]).toBeGreaterThan(66)
    // The fan has one vertex more, and its lines.
    expect(Math.abs(fan.lines[0] - rect.lines[0])).toBeLessThan(0.05 * rect.lines[0])
  })

  it('takes half a narrow trace\'s width as its smallest cell', () => {
    const wide = countSmallPartMesh(doc(), trace(5, 25, 10, 0.4), doc().geometry, ['CLK'], [2, 7, 28, 13], [], COARSE, 2e9)
    const narrow = countSmallPartMesh(doc(), trace(5, 25, 10, 0.1), doc().geometry, ['CLK'], [2, 7, 28, 13], [], COARSE, 2e9)
    expect(wide.min_cell_mm).toBeGreaterThanOrEqual(0.075)
    expect(narrow.min_cell_mm).toBeCloseTo(0.05, 3)
  })
})
