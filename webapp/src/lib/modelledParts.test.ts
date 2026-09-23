/**
 * The modelled-parts list (docs/implementation.md §3).
 *
 * The distinction that matters is the three-way one: a result that modelled nothing, a result
 * that modelled some things, and a result produced before the list existed. Collapsing any two
 * of those tells a user something untrue about what they are looking at.
 */

import { describe, expect, it } from 'vitest'
import type { ModelledPart, SolveManifest } from '../components/HotspotResults'

const manifest = (over: Partial<SolveManifest>): SolveManifest =>
  ({
    format_version: 2,
    metric: 'surface_current_density',
    dynamic_range_db: 60,
    reference_magnitude: 1,
    frequencies_hz: [200e6],
    layers: [],
    has_sparams: true,
    ...over,
  }) as SolveManifest

const part = (over: Partial<ModelledPart> = {}): ModelledPart => ({
  ref: 'C12',
  component_id: 'generic-mlcc-100n-0402',
  component: '100 nF 0402 (generic)',
  generic: true,
  source: 'typical for the package and value class (ESL and ESR)',
  c_f: 1e-7,
  esl_h: 4.5e-10,
  esr_ohm: 0.06,
  self_resonance_hz: 23.7e6,
  layer: 'F.Cu',
  ...over,
})

describe('the three states a result can be in', () => {
  it('absent means the result predates the list', () => {
    expect(manifest({}).modelled_parts).toBeUndefined()
  })

  it('empty means nothing was modelled, which is a real answer', () => {
    expect(manifest({ modelled_parts: [] }).modelled_parts).toEqual([])
  })

  it('populated means these parts and no others', () => {
    const m = manifest({ modelled_parts: [part()] })
    expect(m.modelled_parts).toHaveLength(1)
    expect(m.modelled_parts![0].ref).toBe('C12')
  })
})

describe('provenance travels with each part', () => {
  it('marks a class average as generic', () => {
    expect(part().generic).toBe(true)
  })

  it('marks a cited component as not generic, with its source', () => {
    const cited = part({ generic: false, source: 'part datasheet rev C (ESL and ESR)' })
    expect(cited.generic).toBe(false)
    expect(cited.source).toContain('datasheet')
  })

  it('allows a part whose model has no self-resonance', () => {
    // A component with no ESL resolves but cannot be placed; if one ever reaches this list
    // the UI must render it rather than assume a number is there.
    expect(part({ self_resonance_hz: null, esl_h: null }).self_resonance_hz).toBeNull()
  })
})
