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

  it('takes half a narrow trace\'s width as its smallest cell', () => {
    const wide = countSmallPartMesh(doc(), trace(5, 25, 10, 0.4), doc().geometry, ['CLK'], [2, 7, 28, 13], [], COARSE, 2e9)
    const narrow = countSmallPartMesh(doc(), trace(5, 25, 10, 0.1), doc().geometry, ['CLK'], [2, 7, 28, 13], [], COARSE, 2e9)
    expect(wide.min_cell_mm).toBeGreaterThanOrEqual(0.075)
    expect(narrow.min_cell_mm).toBeCloseTo(0.05, 3)
  })
})
