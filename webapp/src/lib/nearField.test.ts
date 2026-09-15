import { describe, expect, it } from 'vitest'
import { parseScan, scanToOverlay } from './nearField'

const place = { offsetX: 0, offsetY: 0, flipY: false, boardHeight: 50, valuesAreDb: true, rangeDb: 40 }

describe('parseScan', () => {
  it('reads a comma file with a header', () => {
    const s = parseScan('x_mm,y_mm,level_dBuV\n0,0,30\n1,0,32\n0,1,31\n1,1,45\n')
    expect(s.points).toHaveLength(4)
    expect(s.valueLabel).toBe('level_dBuV')
    expect(s.looksLikeDb).toBe(true)
  })

  it('reads semicolons with decimal commas, the way European tools write them', () => {
    const s = parseScan('X;Y;Amplitude\n0,5;0,0;12,5\n1,5;0,0;13\n0,5;1,0;14\n1,5;1,0;15\n')
    expect(s.points[0]).toEqual({ x: 0.5, y: 0, v: 12.5 })
  })

  it('reads whitespace-separated numbers with no header, skipping comments', () => {
    const s = parseScan('# scan export\n0 0 -60\n2 0 -55\n0 2 -50\n2 2 -40\n')
    expect(s.points).toHaveLength(4)
    expect(s.looksLikeDb).toBe(true)
  })

  it('finds x, y and the reading by name in any column order', () => {
    expect(parseScan('reading,y,x\n10,0,5\n11,0,6\n12,1,5\n13,1,6\n').points[0]).toEqual({ x: 5, y: 0, v: 10 })
  })

  it('refuses a file with too few readable points', () => {
    expect(() => parseScan('x,y,v\n0,0,1\n')).toThrow(/readable points/)
  })
})

describe('scanToOverlay', () => {
  const scan = parseScan('x,y,dB\n0,0,-50\n1,0,-40\n0,1,-30\n1,1,-20\n')

  it('puts the peak at 0 dB, row 0 at the lowest Y', () => {
    const r = scanToOverlay(scan, place)
    expect([r.width, r.height]).toEqual([2, 2])
    expect(Array.from(r.overlay.values)).toEqual([-30, -20, -10, 0])
    expect(r.peakAt).toEqual({ x: 1, y: 1 })
  })

  it('covers the scan cells, not only their centres', () => {
    expect(scanToOverlay(scan, place).overlay.extent).toEqual([-0.5, -0.5, 1.5, 1.5])
  })

  it('reverses rows when the scanner Y axis points down', () => {
    const r = scanToOverlay(scan, { ...place, flipY: true, boardHeight: 10 })
    expect(Array.from(r.overlay.values)).toEqual([-10, 0, -30, -20])
    expect(r.overlay.extent[1]).toBeCloseTo(8.5)
    expect(r.overlay.extent[3]).toBeCloseTo(10.5)
  })

  it('converts linear readings to dB', () => {
    const lin = parseScan('x,y,volts\n0,0,1\n1,0,10\n0,1,1\n1,1,1\n')
    expect(lin.looksLikeDb).toBe(false)
    const r = scanToOverlay(lin, { ...place, valuesAreDb: false })
    expect(r.overlay.values[1]).toBeCloseTo(0)
    expect(r.overlay.values[0]).toBeCloseTo(-20)
  })

  it('hides cells the probe never visited', () => {
    const sparse = parseScan('x,y,dB\n0,0,-10\n1,0,-10\n2,0,-5\n0,1,-10\n')
    const r = scanToOverlay(sparse, place)
    expect([r.width, r.height]).toEqual([3, 2])
    expect(r.overlay.values[4]).toBeLessThan(r.overlay.floorDb)
    expect(r.overlay.values[5]).toBeLessThan(r.overlay.floorDb)
  })

  it('offsets the scan into board coordinates', () => {
    const r = scanToOverlay(scan, { ...place, offsetX: 10, offsetY: 20 })
    expect(r.overlay.extent).toEqual([9.5, 19.5, 11.5, 21.5])
    expect(r.peakAt).toEqual({ x: 11, y: 21 })
  })
})
