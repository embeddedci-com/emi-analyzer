import { afterEach, describe, expect, it, vi } from 'vitest'
import { dismiss, isDismissed } from './dismissed'

describe('dismissed', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('remembers a closed hint', () => {
    const store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    })
    expect(isDismissed('what-next')).toBe(false)
    dismiss('what-next')
    expect(isDismissed('what-next')).toBe(true)
  })

  it('shows the hint again rather than failing when storage refuses', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => { throw new Error('SecurityError') },
      setItem: () => { throw new Error('QuotaExceededError') },
    })
    expect(() => dismiss('what-next')).not.toThrow()
    expect(isDismissed('what-next')).toBe(false)
  })

  it('works with no storage at all', () => {
    vi.stubGlobal('localStorage', undefined)
    expect(isDismissed('x')).toBe(false)
    expect(() => dismiss('x')).not.toThrow()
  })
})
