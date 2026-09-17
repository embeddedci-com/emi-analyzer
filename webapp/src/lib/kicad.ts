/**
 * The KiCad plugin, seen from the page.
 *
 * The plugin (kicad-plugin/) shows these very pages in a window beside pcbnew, served by the
 * same local app a browser tab would load them from. The one thing the page cannot do on its
 * own there is point at the board in the PCB Editor, so the plugin injects a small object
 * that can, and this is the only place that touches it.
 *
 * Absent everywhere else. A browser tab, the hosted site and the desktop app all get null
 * from `useKiCad()`, and the cross-probe affordances are simply not rendered.
 */

import { useEffect, useState } from 'react'

/** Injected by kicad-plugin/emi_analyzer/bridge.py. Keep in step with BRIDGE_VERSION. */
export interface KiCadBridge {
  version: number
  /** Select these nets' copper in the PCB Editor and zoom to it. Resolves with how many items. */
  select(nets: string[]): Promise<number>
  /** Select every net a check flagged. Resolves with how many items. */
  selectAttention(): Promise<number>
}

/** Fired once the plugin has finished wiring its channel up. */
const READY_EVENT = 'kicad-bridge-ready'

declare global {
  interface Window {
    kicadBridge?: KiCadBridge
  }
}

export function kicadBridge(): KiCadBridge | null {
  const bridge = typeof window === 'undefined' ? undefined : window.kicadBridge
  return bridge && typeof bridge.select === 'function' ? bridge : null
}

/**
 * The bridge, once it exists.
 *
 * The plugin sets it up asynchronously, so a page that rendered first would decide there was
 * no KiCad and never look again. It listens for the event instead.
 */
export function useKiCad(): KiCadBridge | null {
  const [bridge, setBridge] = useState<KiCadBridge | null>(kicadBridge)
  useEffect(() => {
    if (bridge) return
    const onReady = () => setBridge(kicadBridge())
    window.addEventListener(READY_EVENT, onReady)
    return () => window.removeEventListener(READY_EVENT, onReady)
  }, [bridge])
  return bridge
}
