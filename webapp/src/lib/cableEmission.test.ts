/**
 * The browser half of the cable-emission contract.
 *
 * The same fixtures are checked by `worker/tests/test_cable_emission.py`, against
 * `compose_cable`, the composition the compliance estimate uses. If this file and that one
 * disagree, the Cables tab shows a different number from the estimate, and both look equally
 * confident.
 */

import { describe, expect, it } from 'vitest'
import fixtures from '../../../server/emi/testdata/cable_emission_fixtures.json'
import { parseDriverDocument } from './driverDocument'
import {
  composeCable, interpComplex, marginDb, parseCablePorts, portImpedance, portResistance,
  whyNoCableDriver,
  worstPoint,
  type CableAntenna, type CablePort, type PortSpectrum,
} from './cableEmission'

type Case = (typeof fixtures.cases)[number]

const compose = (c: Case) =>
  composeCable(
    c.input.port as CablePort,
    c.input.antenna as CableAntenna,
    c.input.spectrum as PortSpectrum,
    c.input.z_s,
    parseDriverDocument(c.input.driver),
    c.input.standard_id,
  )

const byName = (name: string) => fixtures.cases.find((c) => c.name === name)!

describe('cable emission, against the shared fixtures', () => {
  for (const c of fixtures.cases) {
    it(c.name, () => {
      const got = compose(c)
      const want = c.expected
      expect(got.covered).toEqual(want.covered_hz)
      expect(got.band).toEqual(want.band_hz)
      expect(got.emission.line).toBe(want.line)
      expect(got.emission.points.map((p) => p.frequency_hz)).toEqual(
        want.points.map((p) => p.frequency_hz),
      )
      got.emission.points.forEach((p, k) => {
        const w = want.points[k]
        if (w.current_a === 0) {
          expect(p.current_a).toBe(0)
          return
        }
        expect(p.current_a / w.current_a).toBeCloseTo(1, 12)
        expect(p.field_v_per_m / w.field_v_per_m).toBeCloseTo(1, 12)
      })
      expect([...got.emission.undriven.keys()].sort((a, b) => a - b)).toEqual(
        want.undriven_hz,
      )
    })
  }
})

describe('cable emission', () => {
  it('evaluates every harmonic, not only the grid points', () => {
    // A 25 MHz clock against a six-point grid: 37 harmonics in 30-960 MHz. Composed on the
    // grid, as the chart used to be, it drove only 250 MHz.
    const got = compose(byName('harmonics-between-grid-points'))
    expect(got.emission.line).toBe(true)
    expect(got.emission.points.map((p) => Math.round(p.frequency_hz / 25e6))).toEqual(
      Array.from({ length: 37 }, (_, k) => k + 2),
    )
  })

  it("applies the driver's own source impedance", () => {
    const same = compose(byName('harmonics-between-grid-points')).emission
    const other = compose(byName('driver-source-impedance-replaces-the-ports')).emission
    // 275 MHz, the 11th harmonic (the 10th, on the grid, is a square wave's null). A 10 ohm
    // driver pushes |50 + Z_in| / |10 + Z_in| more, with Z_in read between grid points.
    const f = 275e6
    const at = (e: typeof same) => e.points.find((p) => p.frequency_hz === f)!
    const z = portImpedance(byName('harmonics-between-grid-points').input.spectrum as PortSpectrum)
    const zIn = interpComplex(z.f, z.zr, z.zi, f)!
    const factor = Math.hypot(50 + zIn.re, zIn.im) / Math.hypot(10 + zIn.re, zIn.im)
    expect(at(other).current_a / at(same).current_a).toBeCloseTo(factor, 12)
  })

  it('worst margin is the worst, not the highest field', () => {
    const e = compose(byName('driver-source-impedance-replaces-the-ports')).emission
    const worst = worstPoint(e)
    expect(worst).not.toBeNull()
    for (const p of e.points) {
      if (Number.isFinite(marginDb(p))) expect(marginDb(p)).toBeGreaterThanOrEqual(marginDb(worst!))
    }
  })

  it('names why a point is missing rather than dropping it', () => {
    const e = compose(byName('every-way-a-point-goes-missing')).emission
    expect(e.undriven.get(120e6)).toMatch(/no source energy/)
    expect(e.undriven.get(250e6)).toMatch(/bandwidth/)
  })
})

describe('an older solve', () => {
  const spectrum = byName('harmonics-between-grid-points').input.spectrum as PortSpectrum
  const run = { ports: [{ name: 'p1', resistance_ohm: 33, excited: true }] }

  it('takes a driver when it recorded its ports and their spectra', () => {
    expect(whyNoCableDriver({ format_version: 2, run }, spectrum)).toBeNull()
    expect(portResistance({ run }, 'p1')).toBe(33)
  })

  it('refuses a driver with a re-run message when it predates them', () => {
    expect(whyNoCableDriver({ format_version: 1 }, null)).toMatch(/Re-run this solve/)
    expect(whyNoCableDriver({ format_version: 2, run: {} }, spectrum)).toMatch(/Re-run/)
    expect(whyNoCableDriver({ format_version: 2, run }, null)).toMatch(/Re-run/)
  })
})

describe('reading cable_ports.json', () => {
  // Byte for byte the shape worker/emi_worker/openems/post.py writes: the transfer function is
  // nested under each port, not at the top level. Reading it any other way yields no cables and
  // the chart silently never renders.
  const written = JSON.parse(
    '{"ports":[{"ref":"J1","anchor_mm":[12.5,3.0],"driven_by":"p1","transfer":' +
      '{"frequencies_hz":[1e8,2e8],"h_real":[0.01,0.02],"h_imag":[-0.005,0.0],' +
      '"usable":[true,false]}}]}',
  )

  it('keeps each port with its ref, its driver port and its transfer function', () => {
    expect(parseCablePorts(written)).toEqual([{
      ref: 'J1', driven_by: 'p1',
      transfer: { frequencies_hz: [1e8, 2e8], h_real: [0.01, 0.02], h_imag: [-0.005, 0.0],
        usable: [true, false] },
    }])
  })

  it('reads nothing from a missing or empty file', () => {
    expect(parseCablePorts(null)).toEqual([])
    expect(parseCablePorts({})).toEqual([])
  })
})
