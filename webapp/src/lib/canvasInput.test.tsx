import { describe, expect, it } from 'vitest'
import { renderToString } from 'react-dom/server'
import { BoardCanvas } from '../components/BoardCanvas'
import {
  DOM_DELTA_LINE, DOM_DELTA_PAGE, DOM_DELTA_PIXEL, FrameScheduler, KEY_PAN_PX, KEY_ZOOM,
  LINE_PX, keyAction, watchContextLoss, wheelPixels, wheelZoomFactor,
} from './canvasInput'
import { createCursorStore } from './cursorStore'

describe('wheel zoom', () => {
  it('reads line and page deltas as the distance they mean', () => {
    expect(wheelPixels(100, DOM_DELTA_PIXEL, 800)).toBe(100)
    expect(wheelPixels(3, DOM_DELTA_LINE, 800)).toBe(3 * LINE_PX)
    expect(wheelPixels(1, DOM_DELTA_PAGE, 800)).toBe(800)
  })

  it('zooms as far per Firefox notch as per Chrome notch', () => {
    // Firefox reports one notch as 3 lines; Chrome as about 100 px. Read as pixels, the
    // Firefox notch zoomed 0.3 %.
    const firefox = wheelZoomFactor(3, DOM_DELTA_LINE, 800)
    const chrome = wheelZoomFactor(100, DOM_DELTA_PIXEL, 800)
    expect(Math.abs(Math.log(firefox) - Math.log(chrome))).toBeLessThan(0.02)
    expect(firefox).toBeLessThan(0.96)
  })

  it('caps one event, so a page-mode wheel does not jump', () => {
    expect(wheelZoomFactor(1, DOM_DELTA_PAGE, 5000)).toBeCloseTo(wheelZoomFactor(400, 0, 0))
    expect(wheelZoomFactor(-1, DOM_DELTA_PAGE, 5000)).toBeCloseTo(1 / wheelZoomFactor(400, 0, 0))
  })
})

describe('keyboard', () => {
  it('pans with the arrows, further with shift', () => {
    expect(keyAction('ArrowLeft')).toEqual({ kind: 'pan', dx: KEY_PAN_PX, dy: 0 })
    expect(keyAction('ArrowRight')).toEqual({ kind: 'pan', dx: -KEY_PAN_PX, dy: 0 })
    expect(keyAction('ArrowUp')).toEqual({ kind: 'pan', dx: 0, dy: KEY_PAN_PX })
    expect(keyAction('ArrowDown', true)).toEqual({ kind: 'pan', dx: 0, dy: -4 * KEY_PAN_PX })
  })

  it('zooms with + and -, and fits with 0', () => {
    expect(keyAction('+')).toEqual({ kind: 'zoom', factor: KEY_ZOOM })
    expect(keyAction('=')).toEqual({ kind: 'zoom', factor: KEY_ZOOM })
    expect(keyAction('-')).toEqual({ kind: 'zoom', factor: 1 / KEY_ZOOM })
    expect(keyAction('0')).toEqual({ kind: 'fit' })
    expect(keyAction('a')).toBeNull()
    expect(keyAction('Tab')).toBeNull()
  })

  it('the canvas is labelled and can take focus', () => {
    const html = renderToString(<BoardCanvas doc={null} geometry={null} />)
    expect(html).toContain('tabindex="0"')
    expect(html).toContain('aria-label="Board view. Arrow keys pan, plus and minus zoom, 0 fits the board."')
  })
})

describe('frame scheduling', () => {
  function fakeFrames() {
    const queue = new Map<number, () => void>()
    let next = 1
    return {
      raf: (cb: () => void) => { queue.set(next, cb); return next++ },
      caf: (id: number) => { queue.delete(id) },
      flush() {
        const cbs = [...queue.values()]
        queue.clear()
        cbs.forEach((cb) => cb())
      },
      get size() { return queue.size },
    }
  }

  it('draws once for many requests, and nothing when idle', () => {
    const frames = fakeFrames()
    let draws = 0
    const s = new FrameScheduler(() => { draws++ }, frames.raf, frames.caf)
    s.request()
    s.request()
    s.request()
    expect(frames.size).toBe(1)
    frames.flush()
    expect(draws).toBe(1)
    // Idle: no frame is waiting. The old loop re-queued itself every frame to poll resize().
    expect(frames.size).toBe(0)
    frames.flush()
    expect(draws).toBe(1)
  })

  it('cancels a pending frame', () => {
    const frames = fakeFrames()
    let draws = 0
    const s = new FrameScheduler(() => { draws++ }, frames.raf, frames.caf)
    s.request()
    s.cancel()
    frames.flush()
    expect(draws).toBe(0)
    expect(s.scheduled).toBe(false)
  })
})

describe('lost GL context', () => {
  it('asks the browser to restore it, and rebuilds when it does', () => {
    const canvas = new EventTarget()
    const seen: string[] = []
    const stop = watchContextLoss(canvas, () => seen.push('lost'), () => seen.push('restored'))

    const lost = new Event('webglcontextlost', { cancelable: true })
    canvas.dispatchEvent(lost)
    // Without preventDefault the browser never fires the restore event.
    expect(lost.defaultPrevented).toBe(true)
    canvas.dispatchEvent(new Event('webglcontextrestored'))
    expect(seen).toEqual(['lost', 'restored'])

    stop()
    canvas.dispatchEvent(new Event('webglcontextlost', { cancelable: true }))
    expect(seen).toEqual(['lost', 'restored'])
  })
})

describe('cursor store', () => {
  it('notifies only its subscribers, and only on a visible change', () => {
    const store = createCursorStore()
    let calls = 0
    const unsubscribe = store.subscribe(() => { calls++ })
    store.set({ x: 1, y: 2 })
    store.set({ x: 1.001, y: 2.001 }) // under the readout's 0.01 mm resolution
    expect(calls).toBe(1)
    expect(store.get()).toEqual({ x: 1, y: 2 })
    store.set(null)
    store.set(null)
    expect(calls).toBe(2)
    unsubscribe()
    store.set({ x: 5, y: 5 })
    expect(calls).toBe(2)
  })
})
