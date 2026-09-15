/**
 * Limit tables — the browser's reader.
 *
 * The tables are imported straight from the worker's copy
 * (`worker/emi_worker/compliance/limits/fcc.json`), not regenerated into the webapp. There
 * is exactly one file holding every limit value in this system, so the number on a chart and
 * the number behind a margin cannot disagree.
 *
 * Two subtleties live here rather than in the data, and both matter:
 *
 * - **Band edges take the tighter limit.** At 88 MHz exactly, FCC Class B is 100 µV/m, not
 *   150. A half-open interval would pick whichever segment was written first, and could
 *   draw a limit line a device passes when it does not.
 * - **Conducted segments slope**, linearly in log frequency. §15.107's 0.15–0.5 MHz band
 *   falls 66 → 56 dBµV, so the limit at the geometric mean of the endpoints is their
 *   arithmetic mean: 61 dBµV at 0.274 MHz.
 */

import table from '../../../worker/emi_worker/compliance/limits/fcc.json'

export class LimitError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'LimitError'
  }
}

export interface LimitSegment {
  f_lo_hz: number
  f_hi_hz: number
  level_db: number
  /** Level at `f_hi_hz` when the segment slopes; absent for a flat one. */
  level_db_hi?: number
  microvolts_per_m?: number
}

export interface Standard {
  id: string
  name: string
  clause: string
  scan: 'radiated' | 'conducted'
  class?: string
  distance_m?: number
  detector: string
  unit: string
  verified: 'standard text' | 'secondary sources'
  source: string
  segments: LimitSegment[]
}

interface ScanRange {
  clause: string
  source: string
  rules: { highest_below_hz: number; scan_to_hz: number }[]
  above: { harmonic: number; cap_hz: number }
}

const doc = table as unknown as {
  format: string
  version: number
  standards: Standard[]
  scan_range: ScanRange
}

export const STANDARDS: Standard[] = doc.standards
export const SCAN_RANGE: ScanRange = doc.scan_range

const byId = new Map(STANDARDS.map((s) => [s.id, s]))

export function standard(id: string): Standard {
  const found = byId.get(id)
  if (!found) {
    throw new LimitError(
      `unknown standard '${id}'; known: ${[...byId.keys()].sort().join(', ')}`,
    )
  }
  return found
}

function covers(seg: LimitSegment, frequencyHz: number): boolean {
  // Inclusive at both ends, so a band edge belongs to both of its segments.
  return seg.f_lo_hz <= frequencyHz && frequencyHz <= seg.f_hi_hz
}

function levelAt(seg: LimitSegment, frequencyHz: number): number {
  if (seg.level_db_hi === undefined || seg.level_db_hi === null) return seg.level_db
  if (frequencyHz <= seg.f_lo_hz) return seg.level_db
  if (frequencyHz >= seg.f_hi_hz) return seg.level_db_hi
  const span = Math.log10(seg.f_hi_hz / seg.f_lo_hz)
  const along = Math.log10(frequencyHz / seg.f_lo_hz) / span
  return seg.level_db + along * (seg.level_db_hi - seg.level_db)
}

/** The limit at one frequency, in the standard's own unit. */
export function limitAt(standardId: string, frequencyHz: number): number {
  const std = standard(standardId)
  const levels = std.segments.filter((s) => covers(s, frequencyHz)).map((s) => levelAt(s, frequencyHz))
  if (levels.length === 0) {
    const lo = std.segments[0].f_lo_hz / 1e6
    const hi = std.segments[std.segments.length - 1].f_hi_hz / 1e6
    throw new LimitError(
      `${std.id} covers ${lo}-${hi} MHz and says nothing at ${frequencyHz / 1e6} MHz. ` +
        `Reporting a margin here would be inventing one`,
    )
  }
  return Math.min(...levels)
}

/**
 * Points for drawing a limit line, including both sides of every band edge.
 *
 * A chart that sampled on its own grid would round the 88 MHz step into a ramp. Emitting
 * the segment endpoints explicitly keeps the step vertical and truthful.
 */
export function limitLine(standardId: string): { frequency_hz: number; level_db: number }[] {
  const std = standard(standardId)
  const out: { frequency_hz: number; level_db: number }[] = []
  for (const seg of std.segments) {
    out.push({ frequency_hz: seg.f_lo_hz, level_db: seg.level_db })
    out.push({
      frequency_hz: seg.f_hi_hz,
      level_db: seg.level_db_hi ?? seg.level_db,
    })
  }
  return out
}

/** How far up a radiated scan must go, per 47 CFR §15.33(b). */
export function scanToHz(highestFrequencyHz: number): number {
  if (!(highestFrequencyHz > 0)) {
    throw new LimitError('the highest frequency in use must be positive')
  }
  for (const rule of SCAN_RANGE.rules) {
    if (highestFrequencyHz < rule.highest_below_hz) return rule.scan_to_hz
  }
  return Math.min(highestFrequencyHz * SCAN_RANGE.above.harmonic, SCAN_RANGE.above.cap_hz)
}
