/**
 * Input handling for the board canvas that does not need a DOM: what a wheel tick or a key
 * press does to the view. Kept apart from the component so it can be tested in Node.
 */

/** WheelEvent.deltaMode values. */
export const DOM_DELTA_PIXEL = 0
export const DOM_DELTA_LINE = 1
export const DOM_DELTA_PAGE = 2

/**
 * Pixels per wheel "line". Firefox reports a mouse-wheel notch as 3 lines where Chrome reports
 * about 100 px, so a line is taken as a third of that.
 */
export const LINE_PX = 33

/** Zoom per pixel of wheel travel. 100 px, one notch in Chrome, is about 10 %. */
const ZOOM_PER_PX = 0.999

/** The largest single-event step, so a page-mode wheel or a flung trackpad cannot jump 100x. */
const MAX_WHEEL_PX = 400

/** Wheel travel in pixels, whatever unit the browser reported it in. */
export function wheelPixels(deltaY: number, deltaMode: number, pagePx: number): number {
  switch (deltaMode) {
    case DOM_DELTA_LINE:
      return deltaY * LINE_PX
    case DOM_DELTA_PAGE:
      return deltaY * pagePx
    default:
      return deltaY
  }
}

/**
 * The zoom factor for one wheel event. Reading deltaY as pixels made Firefox, which reports
 * a notch as 3 lines, zoom 0.3 % per notch instead of 10 %.
 */
export function wheelZoomFactor(deltaY: number, deltaMode: number, pagePx: number): number {
  const px = wheelPixels(deltaY, deltaMode, pagePx)
  return Math.pow(ZOOM_PER_PX, Math.max(-MAX_WHEEL_PX, Math.min(MAX_WHEEL_PX, px)))
}

/** How far one arrow key press pans, in CSS pixels; Shift pans further. */
export const KEY_PAN_PX = 40
/** One + or - press. */
export const KEY_ZOOM = 1.25

export type KeyAction =
  | { kind: 'pan'; dx: number; dy: number }
  | { kind: 'zoom'; factor: number }
  | { kind: 'fit' }

/**
 * What a key does to the view, or null for keys the canvas leaves alone.
 *
 * Arrows move the view the way a drag would move the board: the right arrow shows more of
 * what is to the right, so the board moves left.
 */
export function keyAction(key: string, shift = false): KeyAction | null {
  const step = KEY_PAN_PX * (shift ? 4 : 1)
  switch (key) {
    case 'ArrowLeft':
      return { kind: 'pan', dx: step, dy: 0 }
    case 'ArrowRight':
      return { kind: 'pan', dx: -step, dy: 0 }
    case 'ArrowUp':
      return { kind: 'pan', dx: 0, dy: step }
    case 'ArrowDown':
      return { kind: 'pan', dx: 0, dy: -step }
    case '+':
    case '=':
      return { kind: 'zoom', factor: KEY_ZOOM }
    case '-':
    case '_':
      return { kind: 'zoom', factor: 1 / KEY_ZOOM }
    case '0':
      return { kind: 'fit' }
    default:
      return null
  }
}

/**
 * Draws at most once per animation frame, and only when asked.
 *
 * The canvas used to run a requestAnimationFrame loop for its whole life and ask the renderer
 * whether the element had changed size on every frame, so an idle board still woke the page
 * sixty times a second. A frame is now requested when something changes, and a resize is
 * reported by a ResizeObserver rather than polled.
 */
export class FrameScheduler {
  private pending = 0

  constructor(
    private readonly draw: () => void,
    private readonly raf: (cb: () => void) => number = (cb) => requestAnimationFrame(cb),
    private readonly caf: (id: number) => void = (id) => cancelAnimationFrame(id),
  ) {}

  /** Ask for a frame. Any number of requests before it runs make one draw. */
  request(): void {
    if (this.pending) return
    this.pending = this.raf(() => {
      this.pending = 0
      this.draw()
    })
  }

  get scheduled(): boolean {
    return this.pending !== 0
  }

  cancel(): void {
    if (this.pending) this.caf(this.pending)
    this.pending = 0
  }
}

/**
 * Keeps a WebGL canvas usable across a lost context.
 *
 * A browser drops a page's GL context when the GPU resets, the driver updates or too many
 * tabs hold contexts. Without a handler the canvas stays blank until a reload. Calling
 * preventDefault on the loss is what tells the browser the page will rebuild, so it fires
 * the restore event; the caller then builds a new renderer and uploads the board again.
 */
export function watchContextLoss(
  target: EventTarget,
  onLost: () => void,
  onRestored: () => void,
): () => void {
  const lost = (e: Event) => {
    e.preventDefault()
    onLost()
  }
  const restored = () => onRestored()
  target.addEventListener('webglcontextlost', lost)
  target.addEventListener('webglcontextrestored', restored)
  return () => {
    target.removeEventListener('webglcontextlost', lost)
    target.removeEventListener('webglcontextrestored', restored)
  }
}
