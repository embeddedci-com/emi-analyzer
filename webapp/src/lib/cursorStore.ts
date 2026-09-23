/**
 * The pointer's position over the board, outside React state.
 *
 * It changes on every pointer move. Held in the project page's state, each move re-rendered
 * the whole page: every kept-mounted tab panel and the findings list, for one line of text.
 * Only the readout subscribes to this, so only the readout re-renders.
 */

import { useSyncExternalStore } from 'react'

export type Point = { x: number; y: number }

export interface CursorStore {
  get(): Point | null
  set(p: Point | null): void
  subscribe(listener: () => void): () => void
}

export function createCursorStore(): CursorStore {
  let current: Point | null = null
  const listeners = new Set<() => void>()
  return {
    get: () => current,
    set(p) {
      // The readout shows hundredths of a millimetre; a move smaller than that is no change.
      if (p === current) return
      if (p && current && Math.abs(p.x - current.x) < 0.005 && Math.abs(p.y - current.y) < 0.005) return
      current = p
      for (const l of listeners) l()
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}

export function useCursor(store: CursorStore): Point | null {
  return useSyncExternalStore(store.subscribe, store.get, store.get)
}
