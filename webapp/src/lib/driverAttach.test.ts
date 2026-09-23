/**
 * The gate on attaching a driver to an existing result (§10).
 *
 * A version-1 solve never recorded complex port spectra, and they cannot be recovered from
 * the artifacts it did write. The UI has to say so rather than offer a control that would
 * quietly do nothing.
 */

import { describe, expect, it } from 'vitest'
import {
  canAttachDriver,
  whyNoDriver,
  type SolveManifest,
} from '../components/HotspotResults'

const manifest = (over: Partial<SolveManifest>): SolveManifest =>
  ({
    format_version: 2,
    metric: 'surface_current_density',
    dynamic_range_db: 60,
    reference_magnitude: 1,
    frequencies_hz: [200e6],
    layers: [],
    has_sparams: true,
    has_port_spectra: true,
    ...over,
  }) as SolveManifest

describe('canAttachDriver', () => {
  it('accepts a version-2 result with port spectra', () => {
    expect(canAttachDriver(manifest({}))).toBe(true)
    expect(whyNoDriver(manifest({}))).toBeNull()
  })

  it('refuses a version-1 result and says to re-run', () => {
    const old = manifest({ format_version: 1, has_port_spectra: undefined })
    expect(canAttachDriver(old)).toBe(false)
    expect(whyNoDriver(old)).toMatch(/Re-run this solve/)
  })

  it('refuses a version-2 result that recorded no excited port', () => {
    const none = manifest({ has_port_spectra: false })
    expect(canAttachDriver(none)).toBe(false)
    expect(whyNoDriver(none)).toMatch(/no excited port/)
  })

  it('refuses a run that hit its timestep limit', () => {
    const capped = manifest({ run: { converged: false, final_energy_db: -24 } })
    expect(canAttachDriver(capped)).toBe(false)
    expect(whyNoDriver(capped)).toMatch(/timestep limit/)
    expect(canAttachDriver(manifest({ run: { converged: true } }))).toBe(true)
  })

  it('accepts a future format version', () => {
    expect(canAttachDriver(manifest({ format_version: 3 }))).toBe(true)
  })
})
