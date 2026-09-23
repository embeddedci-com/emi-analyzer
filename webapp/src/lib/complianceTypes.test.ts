/**
 * The one contract this file exists to hold: a number appears only when the inputs are whole.
 *
 * §17.3 is enforced twice, in the worker and here, because they fail differently. The worker
 * omits the field; this is what stops a UI from treating a missing field as zero -- which is
 * exactly what `d.margin_db ?? 0` would do, and it would render as a perfect 0.0 dB margin.
 */

import { describe, expect, it } from 'vitest'
import { driverProvenance, fmtHz, hasMargin, type ComplianceDoc } from './complianceTypes'

const base: ComplianceDoc = {
  format: 'emi-compliance',
  format_version: 1,
  standard_id: 'fcc-15b-radiated-3m',
  standard: 'FCC Part 15 Class B',
  distance_m: 3,
  scan: 'radiated',
  complete: true,
  gaps: [],
  spectrum: [],
  paths: [],
  uncertainty_note: 'placeholders',
  margin_db: 5.1,
}

describe('hasMargin', () => {
  it('is true only when complete and a margin is present', () => {
    expect(hasMargin(base)).toBe(true)
  })

  it('is false when the document is incomplete, even if a margin somehow appears', () => {
    expect(hasMargin({ ...base, complete: false })).toBe(false)
  })

  it('is false when the margin is absent, which is how an incomplete run arrives', () => {
    const { margin_db, ...withoutMargin } = base
    void margin_db
    expect(hasMargin(withoutMargin as ComplianceDoc)).toBe(false)
  })

  it('treats a zero margin as a real number, not as missing', () => {
    // The failure this guards: `margin_db ?? 0` renders an absent field as a perfect 0.0 dB.
    expect(hasMargin({ ...base, margin_db: 0 })).toBe(true)
  })
})

describe('fmtHz', () => {
  it('switches to GHz above a gigahertz', () => {
    expect(fmtHz(1.2e9)).toBe('1.20 GHz')
  })

  it('keeps a decimal place where one megahertz matters', () => {
    expect(fmtHz(1.5e6)).toBe('1.5 MHz')
    expect(fmtHz(300e6)).toBe('300 MHz')
  })
})

describe('driverProvenance', () => {
  const inputs = (driver: unknown) =>
    ({
      driver, connectors: [], modelled_cables: {}, excited_ports: [], far_field_ports: [],
      covered_hz: {}, required_hz: null, declared: {},
    }) as unknown as ComplianceDoc['inputs']

  it('says an assumed driver is assumed, with its sigma, even without a margin', () => {
    const d: ComplianceDoc = {
      ...base, complete: false, margin_db: undefined,
      inputs: inputs({
        id: 'd', name: 'clk', kind: 'trapezoid', net: null,
        weakest_source: 'assumed', sigma_db: 6, assumed: ['source_impedance_ohm'],
      }),
    }
    expect(driverProvenance(d)).toEqual({
      name: 'clk', source: 'assumed', sigmaDb: 6, assumed: ['source_impedance_ohm'],
    })
  })

  it('is null for a result from before provenance was recorded', () => {
    const d: ComplianceDoc = {
      ...base, inputs: inputs({ id: 'd', name: 'clk', kind: 'trapezoid', net: null }),
    }
    expect(driverProvenance(d)).toBeNull()
  })
})
