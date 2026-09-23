/**
 * The browser's half of the shared cost-model contract.
 *
 * Reads the same fixtures as `server/emi/estimate_test.go` and
 * `worker/tests/test_estimate.py`. If these three ever disagree, a user sees one number
 * while committing to a run the worker sizes differently — and finds out hours later.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/estimate_fixtures.json'
import {
  BYTES_PER_CELL,
  estimate,
  EstimateError,
  formatDuration,
  IN_PLANE_MIN_CELL_FRACTION,
  MAX_FILL_FACTOR,
  MESH_MULTIPLIER_FLOOR,
} from './estimate'

interface FixtureCase {
  name: string
  input: Record<string, number>
  expected: {
    cells: number
    ram_bytes: number
    timesteps: number
    dt_seconds: number
    sim_time_seconds: number
    eta_seconds: number
  }
}

describe('cost model matches the shared fixtures', () => {
  it('shares its constants with the worker', () => {
    expect(IN_PLANE_MIN_CELL_FRACTION).toBe(fixtures.in_plane_min_cell_fraction)
    expect(MESH_MULTIPLIER_FLOOR).toEqual(fixtures.mesh_multiplier_floor)
  })

  for (const c of fixtures.cases as FixtureCase[]) {
    it(c.name, () => {
      const got = estimate(c.input as never)
      expect(got.cells).toBe(c.expected.cells)
      expect(got.ram_bytes).toBe(c.expected.ram_bytes)
      expect(got.timesteps).toBe(c.expected.timesteps)
      expect(got.dt_seconds).toBeCloseTo(c.expected.dt_seconds, 20)
      expect(got.sim_time_seconds).toBeCloseTo(c.expected.sim_time_seconds, 15)
      // eta spans many orders of magnitude across the fixtures, so compare relatively.
      expect(got.eta_seconds / c.expected.eta_seconds).toBeCloseTo(1, 10)
    })
  }
})

describe('cost model properties', () => {
  it('the vertical mesh sets the timestep, not the trace width', () => {
    // The trap users fall into: dt keys off the smallest cell in ANY axis. Halving only dz
    // doubles the step count even though the in-plane mesh never changed — and since cells
    // also double, wall clock goes up about 4x.
    const base = {
      roi_x_mm: 20, roi_y_mm: 20, roi_z_mm: 5,
      dx_um: 200, dy_um: 200, dz_um: 40, f_min_hz: 300e6,
    }
    const a = estimate(base)
    const b = estimate({ ...base, dz_um: 20 })

    expect(b.timesteps).toBeGreaterThanOrEqual(2 * a.timesteps - 1)
    expect(b.eta_seconds).toBeGreaterThanOrEqual(3.5 * a.eta_seconds)
  })

  it('grading reduces cells but never the timestep', () => {
    const uniform = {
      roi_x_mm: 24, roi_y_mm: 24, roi_z_mm: 10,
      dx_um: 50, dy_um: 50, dz_um: 25, f_min_hz: 100e6,
    }
    const u = estimate(uniform)
    const g = estimate({ ...uniform, fill_factor: 0.13 })

    expect(g.cells).toBeLessThan(u.cells)
    expect(g.timesteps).toBe(u.timesteps)
    expect(g.dt_seconds).toBe(u.dt_seconds)
  })

  it('ram is 72 bytes per cell', () => {
    const e = estimate({
      roi_x_mm: 10, roi_y_mm: 10, roi_z_mm: 2,
      dx_um: 50, dy_um: 50, dz_um: 25, f_min_hz: 1e9,
    })
    expect(e.ram_bytes).toBe(e.cells * BYTES_PER_CELL)
    expect(BYTES_PER_CELL).toBe(6 * 3 * 4)
  })

  it('reproduces the whole-board figure from the design doc', () => {
    const e = estimate({
      roi_x_mm: 120, roi_y_mm: 100, roi_z_mm: 10,
      dx_um: 50, dy_um: 50, dz_um: 25, f_min_hz: 100e6,
    })
    expect(e.cells).toBe(1_920_000_000)
    expect(e.ram_bytes / 1e9).toBeCloseTo(138.24, 1)
    // 69 days while dt came from the 25 um vertical cell; the mesher's 12.5 um in plane doubles it.
    expect(e.eta_seconds / 86400).toBeCloseTo(138.5, 0)
  })

  it.each([
    ['zero extent', { roi_x_mm: 0 }],
    ['negative extent', { roi_z_mm: -1 }],
    ['zero resolution', { dz_um: 0 }],
    ['zero frequency', { f_min_hz: 0 }],
    ['zero ports', { ports: 0 }],
    // 1.5 used to be rejected. It is an ordinary mesh multiplier: measured values on real
    // boards run 0.70 to 8.75, because dx is a floor on cell size and copper edges force
    // lines much closer than that. Only a value that cannot be a ratio at all is refused.
    ['fill absurdly high', { fill_factor: MAX_FILL_FACTOR + 1 }],
    ['fill negative', { fill_factor: -0.5 }],
  ])('rejects %s', (_label, patch) => {
    const good = {
      roi_x_mm: 10, roi_y_mm: 10, roi_z_mm: 2,
      dx_um: 50, dy_um: 50, dz_um: 25, f_min_hz: 1e8, ports: 1,
    }
    expect(() => estimate({ ...good, ...patch })).toThrow(EstimateError)
  })
})

describe('formatDuration', () => {
  it.each([
    [30, '30 s'],
    [90, '1 m'],
    [3600 * 3 + 60 * 20, '3 h 20 m'],
    [86400 * 2 + 3600 * 4, '2 d 4 h'],
  ])('%i seconds -> %s', (secs, want) => {
    expect(formatDuration(secs)).toBe(want)
  })
})
