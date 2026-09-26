/**
 * Markers drawn over the board. Most call sites mark ports and give no color, so the default
 * stays the renderer's white. The compare view colors each finding by what happened to it,
 * so new and fixed findings can be told apart on the board and not only by the toggle.
 */

import type { FindingStatus } from './compare'

/** RGB in 0..1, as the renderer's color uniform takes it. */
export type Rgb = [number, number, number]

export interface BoardMarker {
  x: number
  y: number
  label?: string
  /** Defaults to {@link DEFAULT_MARKER_COLOR}. */
  color?: Rgb
}

export const DEFAULT_MARKER_COLOR: Rgb = [1, 1, 1]

/** "#rrggbb" to 0..1 RGB. */
export function hexToRgb(hex: string): Rgb {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex)
  if (!m) throw new Error(`not a #rrggbb color: ${hex}`)
  return [parseInt(m[1], 16) / 255, parseInt(m[2], 16) / 255, parseInt(m[3], 16) / 255]
}

/**
 * Marker color per finding status, as CSS for the legend. Mantine's red.6, green.6 and
 * gray.5, so the board matches the badges beside it.
 */
export const FINDING_MARKER_HEX: Record<FindingStatus, string> = {
  new: '#fa5252',
  fixed: '#40c057',
  unchanged: '#adb5bd',
}

/**
 * A small-part result's loudest spots: Mantine's grape.6, the color the result panel numbers
 * them in. Orange was tried first and vanished on the copper, which the viewer draws orange;
 * this is also apart from the white ports, the findings' red and green and the map's yellows.
 */
export const HOTSPOT_MARKER_HEX = '#be4bdb'

export function findingMarkerColor(status: FindingStatus): Rgb {
  return hexToRgb(FINDING_MARKER_HEX[status])
}

/**
 * Markers grouped by color, in order of first appearance, so the renderer sets its color
 * uniform once per group rather than once per marker.
 */
export function markerBatches(markers: BoardMarker[]): { color: Rgb; markers: BoardMarker[] }[] {
  const byKey = new Map<string, { color: Rgb; markers: BoardMarker[] }>()
  for (const m of markers) {
    const color = m.color ?? DEFAULT_MARKER_COLOR
    const key = color.join(',')
    let batch = byKey.get(key)
    if (!batch) {
      batch = { color, markers: [] }
      byKey.set(key, batch)
    }
    batch.markers.push(m)
  }
  return [...byKey.values()]
}
