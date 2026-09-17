import { afterEach, describe, expect, it } from 'vitest'
import { kicadBridge, type KiCadBridge } from './kicad'

const bridge: KiCadBridge = {
  version: 1,
  select: async () => 3,
  selectAttention: async () => 7,
}

afterEach(() => {
  delete (globalThis as { window?: unknown }).window
})

function withWindow(value: unknown) {
  ;(globalThis as { window?: unknown }).window = value
}

describe('the KiCad bridge', () => {
  it('is absent in an ordinary browser tab', () => {
    withWindow({})
    expect(kicadBridge()).toBeNull()
  })

  it('is the object the plugin injected', () => {
    withWindow({ kicadBridge: bridge })
    expect(kicadBridge()).toBe(bridge)
  })

  // The page is served over http://127.0.0.1 by the local app, so anything at all could have
  // set window.kicadBridge. Something without the calls is not the plugin.
  it('is not just anything called kicadBridge', () => {
    withWindow({ kicadBridge: { version: 1 } })
    expect(kicadBridge()).toBeNull()
  })
})
