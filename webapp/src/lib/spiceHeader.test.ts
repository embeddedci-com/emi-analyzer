import { describe, expect, it } from 'vitest'
import { defaultPinMap, guessSubckt, parseSubckts, sortPadNumbers } from './spiceHeader'

const LIB = `* Vendor ESD library
.SUBCKT USBLC6_2SC6 IO1 GND IO2
+ IO2B VBUS IO1B ; package pin order
D1 IO1 VBUS DS
.ENDS
* a second part
.subckt TVS_X A K PARAMS: VBR=6.8 ITEST=1m
D1 K A DT
.ends TVS_X
`

describe('parseSubckts', () => {
  it('reads names and pins, joining continuation lines and dropping comments', () => {
    expect(parseSubckts(LIB)).toEqual([
      { name: 'USBLC6_2SC6', pins: ['IO1', 'GND', 'IO2', 'IO2B', 'VBUS', 'IO1B'] },
      { name: 'TVS_X', pins: ['A', 'K'] },
    ])
  })

  it('finds nothing in a file with no subcircuit', () => {
    expect(parseSubckts('* just a model\n.model D1 D(IS=1e-14)\n')).toEqual([])
  })
})

describe('guessSubckt', () => {
  const headers = parseSubckts(LIB)
  it('prefers the subcircuit named like the part', () => {
    expect(guessSubckt(headers, 'USBLC6-2SC6_C2687116', 6)?.name).toBe('USBLC6_2SC6')
  })
  it('falls back to the only one with the right pin count', () => {
    expect(guessSubckt(headers, 'SMAJ5.0A', 2)?.name).toBe('TVS_X')
  })
})

describe('pads', () => {
  it('sorts pad numbers the way footprints number them', () => {
    expect(sortPadNumbers(['10', '2', '1', 'B1', 'A2', 'A1', '2'])).toEqual(['1', '2', '10', 'A1', 'A2', 'B1'])
  })
  it('maps pads to pins in order only when the counts agree', () => {
    expect(defaultPinMap(['1', '2'], ['A', 'K'])).toEqual({ 1: 'A', 2: 'K' })
    expect(defaultPinMap(['1', '2', '3'], ['A', 'K'])).toEqual({})
  })
})
