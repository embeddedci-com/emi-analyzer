import { describe, expect, it } from 'vitest'
import {
  DEFAULT_MARKER_COLOR, FINDING_MARKER_HEX, SEVERITY_MARKER_HEX, findingMarkerColor, findingMarkers,
  hexToRgb, markerAt, markerBatches, severityMarkerColor,
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

describe('finding markers', () => {
  const finding = (id: string, severity: 'critical' | 'warning' | 'info', x: number | null = 1) =>
    ({ id, rule: 'r', severity, title: '', detail: '', x, y: x })

  it('colors by severity: critical red, warning orange, info gray', () => {
    expect(SEVERITY_MARKER_HEX).toEqual({ critical: '#fa5252', warning: '#fd7e14', info: '#868e96' })
    expect(severityMarkerColor('warning')).toEqual(hexToRgb('#fd7e14'))
  })

  it('labels each marker with its number and skips findings with no place', () => {
    const numbers = new Map([['a', 2], ['b', 1], ['c', 3]])
    const m = findingMarkers(
      [finding('a', 'warning'), finding('b', 'critical'), finding('c', 'info', null)], numbers)
    expect(m.map((x) => [x.id, x.label])).toEqual([['a', '2'], ['b', '1']])
    // Critical last, so it is drawn on top.
    expect(m[m.length - 1].color).toEqual(severityMarkerColor('critical'))
  })

  it('finds the nearest marker under a click', () => {
    const toScreen = (x: number, y: number) => ({ x: x * 10, y: y * 10 })
    const ms = [{ x: 1, y: 1, id: 'a' }, { x: 2, y: 1, id: 'b' }]
    expect(markerAt(ms, toScreen, 18, 10)?.id).toBe('b')
    expect(markerAt(ms, toScreen, 50, 50)).toBeNull()
  })
})
