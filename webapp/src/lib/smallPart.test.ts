/**
 * The browser's half of the small-part solve: the same coupon and the same budget as the
 * worker's (`worker/emi_worker/openems/coupon.py`, `stages/small_part.py`).
 */

import { describe, expect, it } from 'vitest'
import type { BoardDoc } from './boardTypes'
import fixtures from '../../../server/emi/testdata/small_part_fixtures.json'
import {
  BANDS, estimateSmallPart, END_CRITERIA_DB, marginFor, MAX_CELL_STEPS, MAX_CELLS, MAX_SIDE_MM,
  MARGIN_HEIGHTS, MIN_MARGIN_MM, MIN_RECORD_S, netEnds, pairOf, planCoupon, PORT_HALF_WIDTH_MM,
  PRESETS, smallPartParams,
} from './smallPart'

function doc(over: Partial<BoardDoc> = {}): BoardDoc {
  const pad = (ref: string, number: string, net: string, x: number, y: number) =>
    ({ ref, number, net, x, y, type: 'smd', drill_mm: 0, layers: ['F.Cu'] })
  return {
    format_version: 1, source: {}, units: 'mm',
    coordinate_system: { x: 'right', y: 'up', z: 'up', origin: 'corner' },
    board: { width_mm: 60, height_mm: 40, thickness_mm: 1.6, outline: [] },
    layers: [
      { name: 'F.Cu', kind: 'signal', index: 0, z_mm: 1.6 },
      { name: 'In1.Cu', kind: 'signal', index: 1, z_mm: 1.4 },
      { name: 'In2.Cu', kind: 'signal', index: 2, z_mm: 0.2 },
      { name: 'B.Cu', kind: 'signal', index: 3, z_mm: 0 },
    ],
    stackup: [
      { name: 'F.Cu', role: 'copper', type: 'copper', thickness_mm: 0.035, material: '', epsilon_r: null, loss_tangent: null, from_file: true, z_bottom_mm: 0, z_top_mm: 0 },
      { name: 'dielectric 1', role: 'dielectric', type: 'prepreg', thickness_mm: 0.2, material: '', epsilon_r: 4.4, loss_tangent: 0.02, from_file: true, z_bottom_mm: 0, z_top_mm: 0 },
      { name: 'In1.Cu', role: 'copper', type: 'copper', thickness_mm: 0.015, material: '', epsilon_r: null, loss_tangent: null, from_file: true, z_bottom_mm: 0, z_top_mm: 0 },
      { name: 'dielectric 2', role: 'dielectric', type: 'core', thickness_mm: 1.0, material: '', epsilon_r: 4.6, loss_tangent: 0.02, from_file: true, z_bottom_mm: 0, z_top_mm: 0 },
      { name: 'In2.Cu', role: 'copper', type: 'copper', thickness_mm: 0.015, material: '', epsilon_r: null, loss_tangent: null, from_file: true, z_bottom_mm: 0, z_top_mm: 0 },
      { name: 'dielectric 3', role: 'dielectric', type: 'prepreg', thickness_mm: 0.2, material: '', epsilon_r: 4.4, loss_tangent: 0.02, from_file: true, z_bottom_mm: 0, z_top_mm: 0 },
      { name: 'B.Cu', role: 'copper', type: 'copper', thickness_mm: 0.035, material: '', epsilon_r: null, loss_tangent: null, from_file: true, z_bottom_mm: 0, z_top_mm: 0 },
    ],
    nets: ['CLK', 'GND', 'USB_D+', 'USB_D-'].map((name, index) =>
      ({ index, name, tracks: 1, vias: 0, pads: 2, length_mm: 20, layers: ['F.Cu'] })),
    vias: [],
    pads: [
      pad('U1', '1', 'CLK', 10, 20), pad('U1', '2', 'GND', 10, 21), pad('U1', '3', '', 10, 22),
      pad('TP1', '1', 'CLK', 20, 20), pad('R1', '1', 'CLK', 30, 20), pad('R1', '2', '', 31, 20),
    ],
    geometry: { file: 'geometry.bin', dtype: 'float32', components: 2, primitive: 'triangles', vertex_count: 0, byte_length: 0, groups: [] },
    kicad: { version: 1, generator: 'test' },
    warnings: [],
    ...over,
  } as unknown as BoardDoc
}

describe('constants', () => {
  it('match the worker', () => {
    const c = fixtures.constants
    expect(MIN_MARGIN_MM).toBe(c.min_margin_mm)
    expect(MARGIN_HEIGHTS).toBe(c.margin_heights)
    expect(MAX_SIDE_MM).toBe(c.max_side_mm)
    expect(PORT_HALF_WIDTH_MM).toBe(c.port_half_width_mm)
    expect(MAX_CELLS).toBe(c.max_cells)
    expect(MAX_CELL_STEPS).toBe(c.max_cell_steps)
    expect(MIN_RECORD_S).toBe(c.min_record_s)
    expect(END_CRITERIA_DB).toBe(c.end_criteria_db)
    expect([BANDS[0].lo, BANDS[0].hi]).toEqual(c.band_hz)
  })
})

describe('the coupon', () => {
  it('takes five heights of the outer dielectric, or 3 mm', () => {
    expect(marginFor(doc())).toBe(3)
  })

  it('puts ports on the two furthest pads, the many-pin part driven', () => {
    const ends = netEnds(doc(), 'CLK')
    expect(ends.map((p) => `${p.ref}.${p.number}`)).toEqual(['U1.1', 'R1.1'])
    const plan = planCoupon(doc(), null, null, ['CLK'])
    expect(plan.error).toBeNull()
    expect(plan.ports.map((p) => [p.padRef, p.excited, p.half_width_mm]))
      .toEqual([['U1.1', true, 0.2], ['R1.1', false, 0.2]])
    expect(plan.roi).toEqual([7, 17, 33, 23])
  })

  it('refuses a ground net and a part past the largest side', () => {
    expect(planCoupon(doc(), null, null, ['GND']).error).toMatch(/ground/)
    const far = doc({ pads: [...doc().pads, { ref: 'J9', number: '1', net: 'CLK', x: 90, y: 20, type: 'smd', drill_mm: 0, layers: ['F.Cu'] }] })
    expect(planCoupon(far, null, null, ['CLK']).error).toMatch(/stop at 60 mm/)
  })

  it('finds a pair by its name', () => {
    expect(pairOf(doc(), 'USB_D+')).toBe('USB_D-')
    expect(pairOf(doc(), 'CLK')).toBeNull()
  })
})

describe('the estimate', () => {
  it('prices a small coupon and refuses a large one before the worker does', () => {
    const ok = estimateSmallPart([0, 0, 20, 8], PRESETS[1], BANDS[0], doc())
    expect(ok.refused).toBeNull()
    expect(ok.worst_seconds).toBeGreaterThanOrEqual(ok.typical_seconds)
    expect(ok.record_s).toBeGreaterThanOrEqual(MIN_RECORD_S)
    const big = estimateSmallPart([0, 0, 60, 60], PRESETS[1], BANDS[0], doc())
    expect(big.refused).not.toBeNull()
  })

  it('sends what the worker reads', () => {
    const plan = planCoupon(doc(), null, null, ['CLK'])
    const p = smallPartParams({
      roi: plan.roi!, ports: plan.ports, nets: ['CLK'], band: BANDS[0], preset: PRESETS[1],
      frequencies_hz: [50e6, 300e6],
    })
    expect(p.mode).toBe('small_part')
    expect(p.frequencies_hz).toEqual([300e6])
    expect(p.coupon).toEqual({ nets: ['CLK'] })
    expect(p.ports[0]).toMatchObject({ pad: 'U1.1', excited: true, net: 'CLK' })
  })
})
