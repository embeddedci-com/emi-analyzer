import { describe, expect, it } from 'vitest'
import { assessGrid, convergence, suggestedGate } from './solveQuality'

function grid(width: number, height: number, fill: (r: number, c: number) => number) {
  const v = new Float32Array(width * height)
  for (let r = 0; r < height; r++) for (let c = 0; c < width; c++) v[r * width + c] = fill(r, c)
  return v
}

describe('assessGrid', () => {
  it('calls a shielded layer residue, not a hotspot', () => {
    // As measured on a real solve: 99.7% at the floor, "peak" -56.7 dB at the region corner.
    const v = grid(20, 20, (r, c) => (r === 0 && c === 0 ? -56.7 : -60))
    const q = assessGrid(v, 20, 20, -60, -45)
    expect(q.belowNoise).toBe(true)
    expect(q.truncated).toBe(false)
    expect(q.visibleShare).toBe(0)
  })

  it('flags strong field at the boundary as a cut structure', () => {
    // A trace running out of the region: loud at the centre and still loud at one edge.
    const v = grid(20, 20, (r, c) => (r === 10 ? -2 * Math.abs(c - 10) / 10 : -35))
    const q = assessGrid(v, 20, 20, -60, -45)
    expect(q.truncated).toBe(true)
  })

  it('does not flag a field that falls away before the boundary', () => {
    const v = grid(20, 20, (r, c) => -3 * Math.hypot(r - 10, c - 10))
    const q = assessGrid(v, 20, 20, -60, -45)
    expect(q.truncated).toBe(false)
    expect(q.belowNoise).toBe(false)
  })

  it('reports how much of the map a gate shows', () => {
    const v = grid(10, 10, (r) => -10 * r)
    expect(assessGrid(v, 10, 10, -100, -45).visibleShare).toBeCloseTo(0.5)
  })
})

describe('suggestedGate', () => {
  it('shows the loud part of a map that is near-field everywhere', () => {
    // Every cell above -45 dB: a -45 dB gate would paint the whole region.
    const v = grid(10, 10, (r, c) => -(r * 10 + c) * 0.4)
    const gate = suggestedGate(v, -60)
    expect(gate).toBeGreaterThan(-45)
    // Math.abs: -20 % 5 is -0, which is a real zero but not Object.is(0).
    expect(Math.abs(gate % 5)).toBe(0)
  })

  it('stays within sensible bounds', () => {
    expect(suggestedGate(new Float32Array(100).fill(0), -60)).toBe(-10)
    expect(suggestedGate(new Float32Array(100).fill(-60), -60)).toBe(-20)
  })
})

describe('convergence', () => {
  it('treats a run that barely decayed as unusable, whatever its flag says', () => {
    expect(convergence(-4.53, false)).toBe('unusable')
    expect(convergence(-4.53, true)).toBe('unusable')
  })
  it('separates partly settled from settled', () => {
    expect(convergence(-25, true)).toBe('partial')
    expect(convergence(-25, undefined)).toBe('partial')
    expect(convergence(-42.28, true)).toBe('converged')
  })
  it('treats a run that hit its timestep limit as unusable, however far it decayed', () => {
    expect(convergence(-42.28, false)).toBe('unusable')
    expect(convergence(-35, false)).toBe('unusable')
    expect(convergence(null, false)).toBe('unusable')
  })
  it('falls back to the flag when there is no energy', () => {
    expect(convergence(undefined, true)).toBe('converged')
    expect(convergence(undefined, undefined)).toBe('unknown')
  })
})
