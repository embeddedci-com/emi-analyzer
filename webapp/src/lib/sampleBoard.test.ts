import { describe, expect, it } from 'vitest'
import { sampleBoardFile } from './sampleBoard'
import fixture from '../../../worker/tests/fixtures/sample.kicad_pcb?raw'

describe('sample board', () => {
  it('matches the worker sample fixture, byte for byte', async () => {
    const file = sampleBoardFile()
    expect(file.name).toBe('sample.kicad_pcb')
    expect(await file.text()).toBe(fixture)
  })
})
