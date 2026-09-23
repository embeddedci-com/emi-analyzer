import { describe, expect, it } from 'vitest'
import { sampleBoardFile } from './sampleBoard'
import fixture from '../../../worker/tests/fixtures/tiny.kicad_pcb?raw'

describe('sample board', () => {
  it('is the worker fixture, byte for byte', async () => {
    const file = sampleBoardFile()
    expect(file.name).toBe('sample.kicad_pcb')
    expect(await file.text()).toBe(fixture)
  })
})
