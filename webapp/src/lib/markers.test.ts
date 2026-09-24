import { describe, expect, it } from 'vitest'
import {
  DEFAULT_MARKER_COLOR, FINDING_MARKER_HEX, findingMarkerColor, hexToRgb, markerBatches,
} from './markers'

describe('hexToRgb', () => {
  it('converts #rrggbb to 0..1', () => {
    expect(hexToRgb('#ff0080')).toEqual([1, 0, 128 / 255])
    expect(hexToRgb('00FF00')).toEqual([0, 1, 0])
  })
  it('rejects anything else', () => {
    expect(() => hexToRgb('red')).toThrow()
  })
})

describe('markerBatches', () => {
  it('keeps uncolored markers white, in one batch', () => {
    const b = markerBatches([{ x: 1, y: 1 }, { x: 2, y: 2 }])
    expect(b).toHaveLength(1)
    expect(b[0].color).toEqual(DEFAULT_MARKER_COLOR)
    expect(b[0].markers).toHaveLength(2)
  })
  it('groups by color in order of first appearance', () => {
    const red = findingMarkerColor('new')
    const green = findingMarkerColor('fixed')
    const b = markerBatches([
      { x: 0, y: 0, color: red },
      { x: 1, y: 0 },
      { x: 2, y: 0, color: [...red] },
      { x: 3, y: 0, color: green },
    ])
    expect(b.map((g) => g.markers.map((m) => m.x))).toEqual([[0, 2], [1], [3]])
    expect(b[1].color).toEqual(DEFAULT_MARKER_COLOR)
  })
  it('is empty for no markers', () => {
    expect(markerBatches([])).toEqual([])
  })
})

describe('finding marker colors', () => {
  it('gives every status its own color, none of them the default white', () => {
    const colors = Object.values(FINDING_MARKER_HEX)
    expect(new Set(colors).size).toBe(colors.length)
    for (const s of ['new', 'fixed', 'unchanged'] as const) {
      expect(findingMarkerColor(s)).not.toEqual(DEFAULT_MARKER_COLOR)
    }
  })
})
