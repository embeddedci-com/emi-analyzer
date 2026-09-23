/**
 * React wrapper around BoardRenderer.
 *
 * React owns the element's lifecycle; the renderer owns everything inside it. Nothing that
 * changes per frame goes through React state — pan, zoom and the redraw flag all live in
 * refs, because a 60 Hz setState during a drag is the difference between smooth and
 * unusable on a board with half a million triangles.
 */

import { useCallback, useEffect, useRef } from 'react'
import { BoardRenderer } from '../lib/BoardRenderer'
import type { FieldOverlayData, OverlayOptions } from '../lib/overlay'
import type { BoardDoc } from '../lib/boardTypes'
import { anchorNear, type PortAnchor } from '../lib/portPlacement'

/**
 * What a drag does.
 *
 * `pan` is the default. `roi` drags out the region a solve will cover. `pick-pad` turns a
 * click into a port placement. Modes rather than modifier keys because placing a port is a
 * deliberate step in a wizard, not an expert shortcut.
 */
export type CanvasMode = 'pan' | 'roi' | 'pick-pad'

/** How far, in CSS pixels, a press may wander and still count as a click. */
const CLICK_SLOP_PX = 4

export interface BoardCanvasProps {
  doc: BoardDoc | null
  geometry: ArrayBuffer | null
  mode?: CanvasMode
  /** Region of interest in board mm: [minX, minY, maxX, maxY]. */
  roi?: [number, number, number, number] | null
  onRoiChange?: (roi: [number, number, number, number]) => void
  /** Port positions to mark. */
  markers?: { x: number; y: number; label?: string }[]
  /** Called with the nearest pad or via, or null when the click found nothing. */
  onPadPick?: (anchor: PortAnchor | null) => void
  /** Field-magnitude map to draw over the copper. */
  overlay?: FieldOverlayData | null
  overlayOptions?: OverlayOptions
  /** Layer name -> visible. Layers absent from the map are visible. */
  layerVisibility?: Record<string, boolean>
  highlightNet?: string | null
  /** Board-space mm to centre on, e.g. when a rule finding is clicked. */
  focus?: { x: number; y: number; zoom?: number } | null
  onCursorMove?: (pt: { x: number; y: number } | null) => void
  onReady?: (renderer: BoardRenderer) => void
  onError?: (err: Error) => void
  className?: string
  style?: React.CSSProperties
}

export function BoardCanvas({
  doc,
  geometry,
  mode = 'pan',
  roi = null,
  onRoiChange,
  markers,
  onPadPick,
  overlay = null,
  overlayOptions,
  layerVisibility,
  highlightNet = null,
  focus = null,
  onCursorMove,
  onReady,
  onError,
  className,
  style,
}: BoardCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const rendererRef = useRef<BoardRenderer | null>(null)
  const dirtyRef = useRef(true)
  const rafRef = useRef(0)
  // x0/y0 is where the press started, to tell a click from a pan on release.
  const dragRef = useRef<{ id: number; x: number; y: number; x0: number; y0: number } | null>(null)
  const roiDragRef = useRef<{ id: number; x0: number; y0: number } | null>(null)
  // Props the pointer handlers read. Kept in a ref so the handlers never need to be
  // recreated, which would otherwise re-register listeners on every render.
  const propsRef = useRef({ mode, onRoiChange, onPadPick, doc })
  propsRef.current = { mode, onRoiChange, onPadPick, doc }

  const markDirty = useCallback(() => {
    dirtyRef.current = true
  }, [])

  // Held in a ref so no effect has to take a callback as a dependency. The page passes
  // inline arrows, which are a new identity on every render.
  const onErrorRef = useRef(onError)
  useEffect(() => {
    onErrorRef.current = onError
  }, [onError])

  // Create the renderer once, and keep it for the element's lifetime.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    let renderer: BoardRenderer
    try {
      renderer = new BoardRenderer(canvas)
    } catch (err) {
      onError?.(err as Error)
      return
    }
    rendererRef.current = renderer
    onReady?.(renderer)

    const loop = () => {
      if (renderer.resize()) dirtyRef.current = true
      if (dirtyRef.current) {
        renderer.render()
        dirtyRef.current = false
      }
      rafRef.current = requestAnimationFrame(loop)
    }
    rafRef.current = requestAnimationFrame(loop)

    return () => {
      cancelAnimationFrame(rafRef.current)
      renderer.dispose()
      rendererRef.current = null
    }
    // Deliberately runs once: recreating the GL context on a prop change would drop the
    // uploaded geometry and force a full re-upload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Upload geometry whenever the board changes -- and only then.
  //
  // This effect resets the camera, so anything that makes it run again throws away the
  // user's pan and zoom. It used to list onError in its dependencies; the page passes that
  // as an inline arrow, so it was a new function on every render, and the page re-renders
  // on every pointer move to show the cursor position. The result was that zooming in
  // snapped straight back out, because the wheel moved the pointer, which re-rendered,
  // which re-fitted.
  //
  // Two guards, because either alone is a trap waiting to be re-sprung: the callback is
  // held in a ref so it can never be a dependency, and the upload is skipped unless the
  // board really is a different one.
  //
  // "Already loaded" has to mean loaded into *this* renderer. StrictMode disposes the
  // renderer and builds a fresh one while refs survive, and the page only mounts the canvas
  // once the board has arrived — so a guard on the board alone skipped the upload into the
  // new renderer, which then drew an empty background forever.
  const loadedRef = useRef<{
    renderer: BoardRenderer | null
    doc: BoardDoc | null
    geometry: ArrayBuffer | null
  }>({ renderer: null, doc: null, geometry: null })
  useEffect(() => {
    const renderer = rendererRef.current
    if (!renderer || !doc || !geometry) return
    const loaded = loadedRef.current
    if (loaded.renderer === renderer && loaded.doc === doc && loaded.geometry === geometry) return
    try {
      renderer.setBoard(doc, geometry)
      renderer.fit()
      loadedRef.current = { renderer, doc, geometry }
      markDirty()
    } catch (err) {
      onErrorRef.current?.(err as Error)
    }
  }, [doc, geometry, markDirty])

  useEffect(() => {
    const renderer = rendererRef.current
    if (!renderer || !doc) return
    for (const layer of doc.layers) {
      renderer.setLayerStyle(layer.name, { visible: layerVisibility?.[layer.name] ?? true })
    }
    markDirty()
  }, [layerVisibility, doc, markDirty])

  useEffect(() => {
    rendererRef.current?.setHighlightNet(highlightNet)
    markDirty()
  }, [highlightNet, markDirty])

  useEffect(() => {
    rendererRef.current?.setRoi(roi)
    markDirty()
  }, [roi, markDirty])

  useEffect(() => {
    rendererRef.current?.setMarkers(markers ?? [])
    markDirty()
  }, [markers, markDirty])

  // Same reasoning as the upload effect: overlayOptions arrives as an inline object, so
  // comparing it by value is what stops a re-render from re-uploading the field texture.
  const gate = overlayOptions?.gateDb
  useEffect(() => {
    const renderer = rendererRef.current
    if (!renderer) return
    try {
      renderer.setFieldOverlay(overlay, { gateDb: gate })
    } catch (err) {
      onErrorRef.current?.(err as Error)
    }
    markDirty()
  }, [overlay, gate, markDirty])

  useEffect(() => {
    const renderer = rendererRef.current
    if (!renderer || !focus) return
    renderer.resize()
    const canvas = canvasRef.current
    if (!canvas) return
    const scale = focus.zoom ?? Math.max(renderer.getView().scale, 20)
    renderer.setView({
      scale,
      tx: canvas.width / (2 * scale) - focus.x,
      ty: canvas.height / (2 * scale) - focus.y,
    })
    markDirty()
  }, [focus, markDirty])

  const dpr = () => Math.min(window.devicePixelRatio || 1, 2)

  const boardPointFromEvent = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const renderer = rendererRef.current
    if (!renderer) return null
    const rect = e.currentTarget.getBoundingClientRect()
    return renderer.toBoard((e.clientX - rect.left) * dpr(), (e.clientY - rect.top) * dpr())
  }

  const handlePointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId)

    if (propsRef.current.mode === 'roi') {
      const p = boardPointFromEvent(e)
      if (p) roiDragRef.current = { id: e.pointerId, x0: p.x, y0: p.y }
      return
    }
    dragRef.current = { id: e.pointerId, x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY }
  }

  const handlePointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.releasePointerCapture(e.pointerId)

    const { mode, onPadPick, doc: currentDoc } = propsRef.current

    // A pan in pick mode ends with a release too; only a press that stayed put is a pick.
    const drag = dragRef.current
    const panned = !!drag && drag.id === e.pointerId &&
      Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) > CLICK_SLOP_PX

    if (mode === 'pick-pad' && currentDoc && onPadPick && !panned) {
      const renderer = rendererRef.current
      const p = boardPointFromEvent(e)
      if (p && renderer) {
        // A constant on-screen target: 14 device pixels converted to mm at the current
        // zoom. A fixed millimetre tolerance would be unusable zoomed out and needlessly
        // precise zoomed in.
        const tol = 14 / renderer.getView().scale
        // Report misses too: clicking into silence is how a user concludes the tool is
        // broken rather than that they missed by two millimetres.
        onPadPick(anchorNear(currentDoc, p.x, p.y, tol))
      }
    }

    if (roiDragRef.current?.id === e.pointerId) {
      roiDragRef.current = null
    }
    if (dragRef.current?.id === e.pointerId) {
      dragRef.current = null
    }
  }

  const handlePointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const renderer = rendererRef.current
    if (!renderer) return

    if (onCursorMove) {
      const rect = e.currentTarget.getBoundingClientRect()
      onCursorMove(
        renderer.toBoard((e.clientX - rect.left) * dpr(), (e.clientY - rect.top) * dpr()),
      )
    }

    const roiDrag = roiDragRef.current
    if (roiDrag && roiDrag.id === e.pointerId) {
      const p = boardPointFromEvent(e)
      if (p) {
        propsRef.current.onRoiChange?.([
          Math.min(roiDrag.x0, p.x), Math.min(roiDrag.y0, p.y),
          Math.max(roiDrag.x0, p.x), Math.max(roiDrag.y0, p.y),
        ])
      }
      return
    }

    const drag = dragRef.current
    if (!drag || drag.id !== e.pointerId) return
    renderer.panBy((e.clientX - drag.x) * dpr(), (e.clientY - drag.y) * dpr())
    drag.x = e.clientX
    drag.y = e.clientY
    markDirty()
  }

  // Wheel is bound imperatively because React's synthetic wheel listener is passive, and a
  // passive listener cannot preventDefault — so the page would scroll while zooming.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const onWheel = (e: WheelEvent) => {
      const renderer = rendererRef.current
      if (!renderer) return
      e.preventDefault()
      const rect = canvas.getBoundingClientRect()
      renderer.zoomAt(
        (e.clientX - rect.left) * dpr(),
        (e.clientY - rect.top) * dpr(),
        Math.pow(0.999, e.deltaY),
      )
      markDirty()
    }
    canvas.addEventListener('wheel', onWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', onWheel)
  }, [markDirty])

  return (
    <canvas
      ref={canvasRef}
      className={className}
      style={{
        display: 'block',
        width: '100%',
        height: '100%',
        cursor: mode === 'pan' ? 'grab' : 'crosshair',
        ...style,
      }}
      onPointerDown={handlePointerDown}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      onPointerMove={handlePointerMove}
      onPointerLeave={() => onCursorMove?.(null)}
    />
  )
}
