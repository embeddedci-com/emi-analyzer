/**
 * The board as a PNG for the report, drawn from geometry.bin with a 2D canvas.
 *
 * Not a capture of the viewer: the viewer's canvas shows whatever the user last panned to,
 * with their layers hidden and a net highlighted, and a WebGL canvas cannot be read back once
 * it has presented a frame. This draws the whole board the way the viewer first shows it,
 * bottom layer first and the front layer solid on top, into a fixed size.
 *
 * Board space is mm with Y up and the origin at the bottom-left corner of the board extent
 * (board.json coordinate_system), so the image spans exactly [0, width] x [0, height].
 */

import type { BoardDoc } from '../boardTypes'
import { DEFAULT_LAYER_COLORS } from '../BoardRenderer'
import type { BoardImage } from './model'

const BACKGROUND = '#0e1211'

/**
 * Render the board, or null where there is no 2D canvas (or it refuses). The report then
 * shows the outline and the markers without the copper, and says so.
 */
export function renderBoardImage(doc: BoardDoc, geometry: ArrayBuffer, maxPx = 1600): BoardImage | null {
  const W = doc.board.width_mm
  const H = doc.board.height_mm
  if (!(W > 0 && H > 0) || typeof document === 'undefined') return null
  const s = maxPx / Math.max(W, H)
  const widthPx = Math.max(1, Math.round(W * s))
  const heightPx = Math.max(1, Math.round(H * s))

  const canvas = document.createElement('canvas')
  canvas.width = widthPx
  canvas.height = heightPx
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  ctx.fillStyle = BACKGROUND
  ctx.fillRect(0, 0, widthPx, heightPx)

  const v = new Float32Array(geometry)
  const byLayer = new Map<string, { offset: number; count: number }[]>()
  for (const g of doc.geometry.groups) {
    const list = byLayer.get(g.layer)
    if (list) list.push(g)
    else byLayer.set(g.layer, [g])
  }

  const px = (x: number) => x * s
  const py = (y: number) => (H - y) * s
  // Bottom layer first so the front layer lands on top, as the viewer draws it.
  const order = doc.layers.map((layer, i) => ({ layer, i })).reverse()
  for (const { layer, i } of order) {
    const groups = byLayer.get(layer.name)
    if (!groups) continue
    const [r, g, b] = DEFAULT_LAYER_COLORS[i % DEFAULT_LAYER_COLORS.length]
    ctx.fillStyle = `rgba(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)},${i === 0 ? 1 : 0.55})`
    // One path per layer. Each triangle is wound the same way, so under the nonzero rule an
    // overlap (a pad over a track) adds up instead of cancelling into a hole.
    ctx.beginPath()
    for (const grp of groups) {
      const end = Math.min(grp.offset + grp.count, v.length / 2)
      for (let k = grp.offset; k + 3 <= end; k += 3) {
        const ax = px(v[2 * k]); const ay = py(v[2 * k + 1])
        let bx = px(v[2 * k + 2]); let by = py(v[2 * k + 3])
        let cx = px(v[2 * k + 4]); let cy = py(v[2 * k + 5])
        if ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0) {
          [bx, cx] = [cx, bx];
          [by, cy] = [cy, by]
        }
        ctx.moveTo(ax, ay)
        ctx.lineTo(bx, by)
        ctx.lineTo(cx, cy)
        ctx.closePath()
      }
    }
    ctx.fill('nonzero')
  }

  try {
    const src = canvas.toDataURL('image/png')
    return src.startsWith('data:image/png;base64,') ? { src, widthPx, heightPx } : null
  } catch {
    return null
  }
}
