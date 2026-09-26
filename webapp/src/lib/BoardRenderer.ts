/**
 * WebGL2 renderer for a normalised PCB.
 *
 * Framework-free on purpose: React owns the canvas element's lifecycle, this owns
 * everything inside it. That split keeps the renderer testable in a plain HTML harness
 * against real board data, which is how the geometry pipeline gets verified.
 *
 * The performance shape that matters: `geometry.bin` is uploaded **once** as a static
 * vertex buffer. Hiding a layer, highlighting a net and changing the hotspot frequency are
 * all uniform changes plus a different set of draw ranges. A dense 4-layer board is around
 * half a million triangles, which a GPU does not notice — but re-uploading it on every
 * layer toggle would be visible immediately.
 */

import type { BoardDoc, GeometryGroup } from './boardTypes'
import { FieldOverlay, type FieldOverlayData, type OverlayOptions } from './overlay'
import { DEFAULT_MARKER_COLOR, markerBatches, type BoardMarker, type Rgb } from './markers'

const VERT = `#version 300 es
precision highp float;

layout(location = 0) in vec2 a_pos;      // board-space mm, Y up

uniform vec2  u_translate;               // pan, in mm
uniform float u_scale;                   // px per mm
uniform vec2  u_viewport;                // canvas size in px

void main() {
  vec2 px = (a_pos + u_translate) * u_scale;
  // Board space and clip space are both Y-up, so Y needs no flip: board y = 0 maps to
  // clip -1, the bottom of the viewport. Flipping here would mirror the board vertically
  // and put it out of step with toBoard(), which is what the cursor readout, fit() and
  // the focus-on-finding maths all use.
  vec2 clip = vec2(
     (px.x / u_viewport.x) * 2.0 - 1.0,
     (px.y / u_viewport.y) * 2.0 - 1.0
  );
  gl_Position = vec4(clip, 0.0, 1.0);
}`

const FRAG = `#version 300 es
precision highp float;

uniform vec4  u_color;
uniform float u_alpha;

out vec4 outColor;

void main() {
  outColor = vec4(u_color.rgb, u_color.a * u_alpha);
}`

export interface LayerStyle {
  /** Base colour, as 0..1 RGB. */
  color: [number, number, number]
  visible: boolean
  /** 0..1. Lower layers are usually drawn faded so the top layer stays readable. */
  opacity: number
}

export interface ViewState {
  /** Pan offset in mm. */
  tx: number
  ty: number
  /** Zoom, in device pixels per mm. */
  scale: number
}

export interface RendererOptions {
  /** Background, as 0..1 RGBA. */
  background?: [number, number, number, number]
  /** Colour used for the highlighted net, as 0..1 RGB. */
  highlightColor?: [number, number, number]
  /** Board outline colour. */
  outlineColor?: [number, number, number, number]
}

/** Default layer palette: copper tones, front brightest, back coolest. */
export const DEFAULT_LAYER_COLORS: [number, number, number][] = [
  [0.85, 0.55, 0.25], // F.Cu  — copper
  [0.55, 0.65, 0.35], // In1.Cu
  [0.4, 0.6, 0.62], // In2.Cu
  [0.55, 0.45, 0.72], // B.Cu
]

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const sh = gl.createShader(type)
  if (!sh) throw new Error('could not create shader')
  gl.shaderSource(sh, src)
  gl.compileShader(sh)
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(sh)
    gl.deleteShader(sh)
    throw new Error(`shader failed to compile: ${log}`)
  }
  return sh
}

export class BoardRenderer {
  private gl: WebGL2RenderingContext
  private program: WebGLProgram
  private vao: WebGLVertexArrayObject
  private vbo: WebGLBuffer
  private outlineVbo: WebGLBuffer
  private outlineVao: WebGLVertexArrayObject
  private outlineCount = 0

  private uTranslate: WebGLUniformLocation | null
  private uScale: WebGLUniformLocation | null
  private uViewport: WebGLUniformLocation | null
  private uColor: WebGLUniformLocation | null
  private uAlpha: WebGLUniformLocation | null

  private doc: BoardDoc | null = null
  /** Draw ranges bucketed by layer, so a layer is one pass over contiguous groups. */
  private byLayer = new Map<string, GeometryGroup[]>()

  private styles = new Map<string, LayerStyle>()
  private highlightNet: string | null = null
  private view: ViewState = { tx: 0, ty: 0, scale: 4 }
  private opts: Required<RendererOptions>
  private disposed = false

  private overlay: FieldOverlay | null = null
  /** Region of interest in board mm: [minX, minY, maxX, maxY]. */
  private roi: [number, number, number, number] | null = null
  /** Port markers in board mm. */
  private markers: BoardMarker[] = []
  private annotationVbo: WebGLBuffer
  private annotationVao: WebGLVertexArrayObject

  constructor(canvas: HTMLCanvasElement, opts: RendererOptions = {}) {
    const gl = canvas.getContext('webgl2', {
      antialias: true,
      alpha: false,
      // The board is opaque and repainted on demand; keeping the drawing buffer lets a
      // screenshot or a thumbnail capture read back a frame that was drawn earlier.
      preserveDrawingBuffer: true,
    })
    if (!gl) throw new Error('WebGL2 is not available in this browser')
    this.gl = gl

    this.opts = {
      background: opts.background ?? [0.06, 0.07, 0.07, 1],
      highlightColor: opts.highlightColor ?? [1.0, 0.95, 0.5],
      outlineColor: opts.outlineColor ?? [0.65, 0.7, 0.68, 1],
    }

    const vs = compile(gl, gl.VERTEX_SHADER, VERT)
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG)
    const program = gl.createProgram()
    if (!program) throw new Error('could not create program')
    gl.attachShader(program, vs)
    gl.attachShader(program, fs)
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`program failed to link: ${gl.getProgramInfoLog(program)}`)
    }
    gl.deleteShader(vs)
    gl.deleteShader(fs)
    this.program = program

    this.uTranslate = gl.getUniformLocation(program, 'u_translate')
    this.uScale = gl.getUniformLocation(program, 'u_scale')
    this.uViewport = gl.getUniformLocation(program, 'u_viewport')
    this.uColor = gl.getUniformLocation(program, 'u_color')
    this.uAlpha = gl.getUniformLocation(program, 'u_alpha')

    const vbo = gl.createBuffer()
    const vao = gl.createVertexArray()
    const ovbo = gl.createBuffer()
    const ovao = gl.createVertexArray()
    const avbo = gl.createBuffer()
    const avao = gl.createVertexArray()
    if (!vbo || !vao || !ovbo || !ovao || !avbo || !avao) {
      throw new Error('could not allocate GL buffers')
    }
    this.vbo = vbo
    this.vao = vao
    this.outlineVbo = ovbo
    this.outlineVao = ovao
    this.annotationVbo = avbo
    this.annotationVao = avao

    for (const [v, b] of [
      [vao, vbo],
      [ovao, ovbo],
      [avao, avbo],
    ] as const) {
      gl.bindVertexArray(v)
      gl.bindBuffer(gl.ARRAY_BUFFER, b)
      gl.enableVertexAttribArray(0)
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0)
    }
    gl.bindVertexArray(null)

    gl.enable(gl.BLEND)
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)
  }

  /** Upload a board. `geometry` is the raw bytes of geometry.bin. */
  setBoard(doc: BoardDoc, geometry: ArrayBuffer): void {
    const gl = this.gl
    this.doc = doc

    const verts = new Float32Array(geometry)
    const expected = doc.geometry.vertex_count * 2
    if (verts.length < expected) {
      throw new Error(
        `geometry.bin has ${verts.length / 2} vertices, board.json expects ${doc.geometry.vertex_count}`,
      )
    }

    gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo)
    gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW)

    this.byLayer.clear()
    for (const grp of doc.geometry.groups) {
      const list = this.byLayer.get(grp.layer)
      if (list) list.push(grp)
      else this.byLayer.set(grp.layer, [grp])
    }

    // Board outline, drawn as line segments over the copper.
    const outline: number[] = []
    for (const ring of doc.board.outline) {
      for (let i = 0; i + 1 < ring.length; i++) {
        outline.push(ring[i][0], ring[i][1], ring[i + 1][0], ring[i + 1][1])
      }
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.outlineVbo)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(outline), gl.STATIC_DRAW)
    this.outlineCount = outline.length / 2

    // Default styling: front layer solid, inner layers progressively faded, so a
    // four-layer board reads as depth rather than as mud.
    this.styles.clear()
    doc.layers.forEach((layer, i) => {
      this.styles.set(layer.name, {
        color: DEFAULT_LAYER_COLORS[i % DEFAULT_LAYER_COLORS.length],
        visible: true,
        opacity: i === 0 ? 1.0 : 0.55,
      })
    })
  }

  setLayerStyle(name: string, patch: Partial<LayerStyle>): void {
    const cur = this.styles.get(name)
    if (cur) this.styles.set(name, { ...cur, ...patch })
  }

  getLayerStyle(name: string): LayerStyle | undefined {
    return this.styles.get(name)
  }

  /** Highlight one net across every visible layer. Pass null to clear. */
  setHighlightNet(net: string | null): void {
    this.highlightNet = net
  }

  getHighlightNet(): string | null {
    return this.highlightNet
  }

  /**
   * Show a field-magnitude map over the board.
   *
   * The overlay is created lazily: a board that is only being inspected never pays for the
   * second GL program or its texture.
   */
  setFieldOverlay(data: FieldOverlayData | null, opts?: OverlayOptions): void {
    if (!this.overlay) {
      if (!data) return
      this.overlay = new FieldOverlay(this.gl)
    }
    if (opts) this.overlay.setOptions(opts)
    this.overlay.setField(data)
  }

  setOverlayOptions(opts: OverlayOptions): void {
    this.overlay?.setOptions(opts)
  }

  hasOverlay(): boolean {
    return this.overlay?.hasField() ?? false
  }

  /** Outline the region a solve will cover. Pass null to clear. */
  setRoi(roi: [number, number, number, number] | null): void {
    this.roi = roi
  }

  getRoi(): [number, number, number, number] | null {
    return this.roi
  }

  /** Port positions, drawn as crosshairs. */
  setMarkers(markers: BoardMarker[]): void {
    this.markers = markers
  }

  /**
   * Called whenever the view changes, so the owner can draw a frame. The canvas draws on
   * request rather than in a loop, and a caller such as a "Fit board" button changes the
   * view without going through it.
   */
  onInvalidate: (() => void) | null = null

  setView(view: Partial<ViewState>): void {
    this.view = { ...this.view, ...view }
    this.onInvalidate?.()
  }

  getView(): ViewState {
    return { ...this.view }
  }

  /** Fit the whole board in the canvas with a small margin. */
  fit(marginFraction = 0.04): void {
    if (!this.doc) return
    // Sync the drawing buffer to the element first. Fitting means "fit what is on screen
    // now", and a canvas that has not been sized yet still reports the 300x150 default --
    // which puts the board in a postage stamp in the corner.
    this.resize()
    const { width_mm, height_mm } = this.doc.board
    const w = this.gl.drawingBufferWidth
    const h = this.gl.drawingBufferHeight
    if (w === 0 || h === 0 || width_mm <= 0 || height_mm <= 0) return

    const scale = Math.min(w / width_mm, h / height_mm) * (1 - marginFraction * 2)
    // Centre the board: translate is applied before scaling, so it is in mm.
    this.view = {
      scale,
      tx: (w / scale - width_mm) / 2,
      ty: (h / scale - height_mm) / 2,
    }
    this.onInvalidate?.()
  }

  /** Canvas pixel coordinates to board-space mm. */
  toBoard(px: number, py: number): { x: number; y: number } {
    const { tx, ty, scale } = this.view
    return {
      x: px / scale - tx,
      // Canvas Y is down, board Y is up.
      y: (this.gl.drawingBufferHeight - py) / scale - ty,
    }
  }

  /** Zoom about a fixed canvas point, so the board does not slide under the cursor. */
  zoomAt(px: number, py: number, factor: number, min = 0.5, max = 400): void {
    const before = this.toBoard(px, py)
    const scale = Math.max(min, Math.min(max, this.view.scale * factor))
    this.view.scale = scale
    const after = this.toBoard(px, py)
    this.view.tx += after.x - before.x
    this.view.ty += after.y - before.y
    this.onInvalidate?.()
  }

  panBy(dxPx: number, dyPx: number): void {
    this.view.tx += dxPx / this.view.scale
    this.view.ty -= dyPx / this.view.scale
    this.onInvalidate?.()
  }

  /** Match the drawing buffer to the element's displayed size. Returns true if it changed. */
  resize(): boolean {
    const gl = this.gl
    const canvas = gl.canvas as HTMLCanvasElement
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const w = Math.max(1, Math.floor(canvas.clientWidth * dpr))
    const h = Math.max(1, Math.floor(canvas.clientHeight * dpr))
    if (canvas.width === w && canvas.height === h) return false
    canvas.width = w
    canvas.height = h
    return true
  }

  render(): void {
    if (this.disposed) return
    const gl = this.gl
    const [br, bg, bb, ba] = this.opts.background

    gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight)
    gl.clearColor(br, bg, bb, ba)
    gl.clear(gl.COLOR_BUFFER_BIT)

    if (!this.doc) return

    gl.useProgram(this.program)
    gl.uniform2f(this.uTranslate, this.view.tx, this.view.ty)
    gl.uniform1f(this.uScale, this.view.scale)
    gl.uniform2f(this.uViewport, gl.drawingBufferWidth, gl.drawingBufferHeight)

    gl.bindVertexArray(this.vao)

    // Bottom layer first so the front layer lands on top, matching how the board is read.
    const order = [...this.doc.layers].reverse()
    const highlighted: GeometryGroup[] = []

    for (const layer of order) {
      const style = this.styles.get(layer.name)
      const groups = this.byLayer.get(layer.name)
      if (!style || !style.visible || !groups) continue

      gl.uniform4f(this.uColor, style.color[0], style.color[1], style.color[2], 1)
      gl.uniform1f(this.uAlpha, style.opacity)

      for (const grp of groups) {
        if (this.highlightNet && grp.net === this.highlightNet) {
          // Deferred to a second pass so the highlight is never painted over by a layer
          // drawn later.
          highlighted.push(grp)
          continue
        }
        gl.drawArrays(gl.TRIANGLES, grp.offset, grp.count)
      }
    }

    if (highlighted.length) {
      const [hr, hg, hb] = this.opts.highlightColor
      gl.uniform4f(this.uColor, hr, hg, hb, 1)
      gl.uniform1f(this.uAlpha, 1)
      for (const grp of highlighted) {
        gl.drawArrays(gl.TRIANGLES, grp.offset, grp.count)
      }
    }

    if (this.outlineCount > 0) {
      gl.bindVertexArray(this.outlineVao)
      const [or_, og, ob, oa] = this.opts.outlineColor
      gl.uniform4f(this.uColor, or_, og, ob, oa)
      gl.uniform1f(this.uAlpha, 1)
      gl.drawArrays(gl.LINES, 0, this.outlineCount)
    }

    gl.bindVertexArray(null)

    // The field map sits above the copper: it is the answer, and the copper is context.
    if (this.overlay?.hasField()) {
      this.overlay.draw(
        [this.view.tx, this.view.ty],
        this.view.scale,
        [gl.drawingBufferWidth, gl.drawingBufferHeight],
      )
    }

    this.drawAnnotations()
  }

  /**
   * Region rectangle and markers, drawn last so they are never hidden.
   *
   * Marker size is computed in board units from the current zoom, so a marker stays the same
   * size on screen rather than shrinking to nothing when the user zooms out to place it. It is
   * sized in CSS pixels: it was "8 device pixels" of hairline cross, which on a 2x screen is a
   * 4 px speck, and a colored marker could not be told apart from the copper under it. Each
   * marker is now a filled diamond in its color with a dark outline and a cross through it.
   */
  private drawAnnotations(): void {
    const gl = this.gl
    const lines: number[] = []
    const tris: number[] = []
    type Draw = { mode: number; first: number; count: number; color: Rgb; alpha: number }
    const lineDraws: Omit<Draw, 'mode'>[] = []
    const triDraws: Omit<Draw, 'mode'>[] = []

    if (this.roi) {
      const [x0, y0, x1, y1] = this.roi
      lines.push(x0, y0, x1, y0, x1, y0, x1, y1, x1, y1, x0, y1, x0, y1, x0, y0)
      lineDraws.push({ first: 0, count: lines.length / 2, color: DEFAULT_MARKER_COLOR, alpha: 0.9 })
    }

    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const r = (9 * dpr) / this.view.scale // 9 CSS pixels, in mm
    const d = r * 0.7
    for (const batch of markerBatches(this.markers)) {
      const tFirst = tris.length / 2
      const oFirst = lines.length / 2
      for (const m of batch.markers) {
        // Filled diamond: two triangles.
        tris.push(m.x - d, m.y, m.x, m.y + d, m.x + d, m.y, m.x - d, m.y, m.x + d, m.y, m.x, m.y - d)
        // Outline and a cross through it, so the exact point is still readable.
        lines.push(
          m.x - d, m.y, m.x, m.y + d, m.x, m.y + d, m.x + d, m.y,
          m.x + d, m.y, m.x, m.y - d, m.x, m.y - d, m.x - d, m.y,
          m.x - r, m.y, m.x + r, m.y, m.x, m.y - r, m.x, m.y + r,
        )
      }
      triDraws.push({ first: tFirst, count: tris.length / 2 - tFirst, color: batch.color, alpha: 0.95 })
      lineDraws.push({ first: oFirst, count: lines.length / 2 - oFirst, color: [0.08, 0.08, 0.1], alpha: 0.9 })
    }

    if (lines.length === 0 && tris.length === 0) return

    gl.useProgram(this.program)
    gl.uniform2f(this.uTranslate, this.view.tx, this.view.ty)
    gl.uniform1f(this.uScale, this.view.scale)
    gl.uniform2f(this.uViewport, gl.drawingBufferWidth, gl.drawingBufferHeight)
    gl.bindVertexArray(this.annotationVao)
    gl.bindBuffer(gl.ARRAY_BUFFER, this.annotationVbo)

    // Fills first, then outlines on top, from one buffer: triangles, then lines after them.
    const all = new Float32Array(tris.length + lines.length)
    all.set(tris, 0)
    all.set(lines, tris.length)
    gl.bufferData(gl.ARRAY_BUFFER, all, gl.DYNAMIC_DRAW)
    const lineBase = tris.length / 2
    const draws: Draw[] = [
      ...triDraws.map((x) => ({ ...x, mode: gl.TRIANGLES })),
      ...lineDraws.map((x) => ({ ...x, first: x.first + lineBase, mode: gl.LINES })),
    ]
    for (const { mode, first, count, color, alpha } of draws) {
      if (count === 0) continue
      gl.uniform1f(this.uAlpha, alpha)
      gl.uniform4f(this.uColor, color[0], color[1], color[2], 1.0)
      gl.drawArrays(mode, first, count)
    }
    gl.bindVertexArray(null)
  }

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    const gl = this.gl
    this.overlay?.dispose()
    gl.deleteBuffer(this.annotationVbo)
    gl.deleteVertexArray(this.annotationVao)
    gl.deleteBuffer(this.vbo)
    gl.deleteBuffer(this.outlineVbo)
    gl.deleteVertexArray(this.vao)
    gl.deleteVertexArray(this.outlineVao)
    gl.deleteProgram(this.program)
  }
}
