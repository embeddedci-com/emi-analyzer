/**
 * The hotspot overlay: a field-magnitude grid drawn over the board.
 *
 * Kept separate from BoardRenderer because it is a second GL program with its own texture
 * lifecycle, and because the data conversion below is the part most likely to need tuning.
 *
 * Field data arrives as float32 dB, already normalised by the worker against a reference
 * shared across every layer and frequency — so a quiet frequency looks quiet rather than
 * being stretched to fill the colour scale. Here it is quantised to 8 bits and uploaded as
 * an R8 texture: 60 dB across 256 levels is 0.23 dB per step, far finer than the eye can
 * read off a colour ramp, and R8 with linear filtering works everywhere without the float
 * texture extensions.
 */

const OVERLAY_VERT = `#version 300 es
precision highp float;

layout(location = 0) in vec2 a_pos;      // board-space mm

uniform vec2  u_translate;
uniform float u_scale;
uniform vec2  u_viewport;
uniform vec4  u_extent;                  // minX, minY, maxX, maxY in board mm

out vec2 v_uv;

void main() {
  v_uv = (a_pos - u_extent.xy) / (u_extent.zw - u_extent.xy);
  vec2 px = (a_pos + u_translate) * u_scale;
  gl_Position = vec4((px / u_viewport) * 2.0 - 1.0, 0.0, 1.0);
}`

const OVERLAY_FRAG = `#version 300 es
precision highp float;

uniform sampler2D u_field;
uniform float u_floorDb;     // most negative dB the ramp covers, e.g. -60
uniform float u_gateDb;      // below this, draw nothing
uniform float u_opacity;

in vec2 v_uv;
out vec4 outColor;

// A dark-to-hot ramp. Deliberately not a rainbow: rainbow ramps invent visual edges where
// the data is smooth, which on a current-density map reads as structure that is not there.
vec3 ramp(float t) {
  const vec3 c0 = vec3(0.05, 0.03, 0.20);   // deep indigo
  const vec3 c1 = vec3(0.45, 0.10, 0.45);   // magenta
  const vec3 c2 = vec3(0.85, 0.30, 0.25);   // red
  const vec3 c3 = vec3(0.99, 0.72, 0.20);   // amber
  const vec3 c4 = vec3(1.00, 0.99, 0.85);   // near white
  if (t < 0.25) return mix(c0, c1, t / 0.25);
  if (t < 0.50) return mix(c1, c2, (t - 0.25) / 0.25);
  if (t < 0.75) return mix(c2, c3, (t - 0.50) / 0.25);
  return mix(c3, c4, (t - 0.75) / 0.25);
}

void main() {
  if (v_uv.x < 0.0 || v_uv.x > 1.0 || v_uv.y < 0.0 || v_uv.y > 1.0) discard;

  float unit = texture(u_field, v_uv).r;          // 0..1
  float db = u_floorDb * (1.0 - unit);            // back to dB

  if (db < u_gateDb) discard;                     // quiet areas stay out of the way

  float t = clamp((db - u_floorDb) / (0.0 - u_floorDb), 0.0, 1.0);
  // Fade in over the first part of the range so the map does not end in a hard edge where
  // it crosses the gate.
  float a = smoothstep(0.0, 0.18, t) * u_opacity;
  outColor = vec4(ramp(t), a);
}`

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const sh = gl.createShader(type)
  if (!sh) throw new Error('could not create shader')
  gl.shaderSource(sh, src)
  gl.compileShader(sh)
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(sh)
    gl.deleteShader(sh)
    throw new Error(`overlay shader failed to compile: ${log}`)
  }
  return sh
}

export interface FieldOverlayData {
  /** dB values, row-major, length = width * height. */
  values: Float32Array
  width: number
  height: number
  /** Board-space extent the grid covers: [minX, minY, maxX, maxY] in mm. */
  extent: [number, number, number, number]
  /** Most negative dB the colour ramp covers. Matches the worker's dynamic range. */
  floorDb: number
}

export interface OverlayOptions {
  /** Values below this many dB are not drawn at all. */
  gateDb?: number
  opacity?: number
}

export class FieldOverlay {
  private gl: WebGL2RenderingContext
  private program: WebGLProgram
  private vao: WebGLVertexArrayObject
  private vbo: WebGLBuffer
  private texture: WebGLTexture | null = null

  private uTranslate: WebGLUniformLocation | null
  private uScale: WebGLUniformLocation | null
  private uViewport: WebGLUniformLocation | null
  private uExtent: WebGLUniformLocation | null
  private uField: WebGLUniformLocation | null
  private uFloorDb: WebGLUniformLocation | null
  private uGateDb: WebGLUniformLocation | null
  private uOpacity: WebGLUniformLocation | null

  private extent: [number, number, number, number] = [0, 0, 0, 0]
  private floorDb = -60
  private gateDb = -45
  private opacity = 0.85
  private ready = false

  constructor(gl: WebGL2RenderingContext) {
    this.gl = gl

    const vs = compile(gl, gl.VERTEX_SHADER, OVERLAY_VERT)
    const fs = compile(gl, gl.FRAGMENT_SHADER, OVERLAY_FRAG)
    const program = gl.createProgram()
    if (!program) throw new Error('could not create overlay program')
    gl.attachShader(program, vs)
    gl.attachShader(program, fs)
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`overlay program failed to link: ${gl.getProgramInfoLog(program)}`)
    }
    gl.deleteShader(vs)
    gl.deleteShader(fs)
    this.program = program

    this.uTranslate = gl.getUniformLocation(program, 'u_translate')
    this.uScale = gl.getUniformLocation(program, 'u_scale')
    this.uViewport = gl.getUniformLocation(program, 'u_viewport')
    this.uExtent = gl.getUniformLocation(program, 'u_extent')
    this.uField = gl.getUniformLocation(program, 'u_field')
    this.uFloorDb = gl.getUniformLocation(program, 'u_floorDb')
    this.uGateDb = gl.getUniformLocation(program, 'u_gateDb')
    this.uOpacity = gl.getUniformLocation(program, 'u_opacity')

    const vao = gl.createVertexArray()
    const vbo = gl.createBuffer()
    if (!vao || !vbo) throw new Error('could not allocate overlay buffers')
    this.vao = vao
    this.vbo = vbo
    gl.bindVertexArray(vao)
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo)
    gl.enableVertexAttribArray(0)
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0)
    gl.bindVertexArray(null)
  }

  setOptions(opts: OverlayOptions): void {
    if (opts.gateDb !== undefined) this.gateDb = opts.gateDb
    if (opts.opacity !== undefined) this.opacity = opts.opacity
  }

  /** Upload a field grid. Pass null to clear the overlay. */
  setField(data: FieldOverlayData | null): void {
    const gl = this.gl
    if (!data) {
      this.ready = false
      return
    }

    const { values, width, height, extent, floorDb } = data
    if (values.length < width * height) {
      throw new Error(
        `field grid is ${values.length} values, expected ${width * height}`,
      )
    }

    // dB -> 0..255. floorDb maps to 0, 0 dB maps to 255.
    const bytes = new Uint8Array(width * height)
    const span = 0 - floorDb
    for (let i = 0; i < bytes.length; i++) {
      const t = (values[i] - floorDb) / span
      bytes[i] = t <= 0 ? 0 : t >= 1 ? 255 : (t * 255) | 0
    }

    if (!this.texture) this.texture = gl.createTexture()
    gl.bindTexture(gl.TEXTURE_2D, this.texture)
    // Rows are tightly packed and width is arbitrary, so the default 4-byte row alignment
    // would shear every grid whose width is not a multiple of four.
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1)
    gl.texImage2D(
      gl.TEXTURE_2D, 0, gl.R8, width, height, 0, gl.RED, gl.UNSIGNED_BYTE, bytes,
    )
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)

    // A quad covering exactly the grid's extent.
    const [x0, y0, x1, y1] = extent
    gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo)
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([x0, y0, x1, y0, x1, y1, x0, y0, x1, y1, x0, y1]),
      gl.STATIC_DRAW,
    )

    this.extent = extent
    this.floorDb = floorDb
    this.ready = true
  }

  hasField(): boolean {
    return this.ready
  }

  draw(translate: [number, number], scale: number, viewport: [number, number]): void {
    if (!this.ready || !this.texture) return
    const gl = this.gl

    gl.useProgram(this.program)
    gl.uniform2f(this.uTranslate, translate[0], translate[1])
    gl.uniform1f(this.uScale, scale)
    gl.uniform2f(this.uViewport, viewport[0], viewport[1])
    gl.uniform4f(this.uExtent, this.extent[0], this.extent[1], this.extent[2], this.extent[3])
    gl.uniform1f(this.uFloorDb, this.floorDb)
    gl.uniform1f(this.uGateDb, this.gateDb)
    gl.uniform1f(this.uOpacity, this.opacity)

    gl.activeTexture(gl.TEXTURE0)
    gl.bindTexture(gl.TEXTURE_2D, this.texture)
    gl.uniform1i(this.uField, 0)

    gl.bindVertexArray(this.vao)
    gl.drawArrays(gl.TRIANGLES, 0, 6)
    gl.bindVertexArray(null)
  }

  dispose(): void {
    const gl = this.gl
    if (this.texture) gl.deleteTexture(this.texture)
    gl.deleteBuffer(this.vbo)
    gl.deleteVertexArray(this.vao)
    gl.deleteProgram(this.program)
    this.ready = false
  }
}

/** Colour stops matching the shader ramp, for drawing a legend in the DOM. */
export const RAMP_STOPS: [number, string][] = [
  [0.0, 'rgb(13, 8, 51)'],
  [0.25, 'rgb(115, 26, 115)'],
  [0.5, 'rgb(217, 77, 64)'],
  [0.75, 'rgb(252, 184, 51)'],
  [1.0, 'rgb(255, 252, 217)'],
]

export function rampCss(direction = 'to right'): string {
  const stops = RAMP_STOPS.map(([t, c]) => `${c} ${(t * 100).toFixed(0)}%`).join(', ')
  return `linear-gradient(${direction}, ${stops})`
}
