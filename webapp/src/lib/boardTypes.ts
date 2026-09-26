/**
 * The `board.json` contract, as produced by the worker's KiCad ingest.
 *
 * This file is the single description of that format on the frontend side. It is
 * deliberately a plain type declaration with no runtime code, so it can be shared by the
 * renderer, the rules panel and the setup wizard without any of them importing the others.
 */

export interface BoardCoordinateSystem {
  x: string
  y: string
  z: string
  origin: string
  note?: string
}

export interface StackupEntry {
  name: string
  /** `copper` conducts, `dielectric` is board substrate, `other` is mask/silk/paste. */
  role: 'copper' | 'dielectric' | 'other'
  type: string
  thickness_mm: number
  material: string
  epsilon_r: number | null
  loss_tangent: number | null
  /**
   * False when the worker had to invent this value because the board file did not carry
   * one. The UI must show these differently: a guessed epsilon_r shifts every resonance,
   * and the user is the only one who can correct it.
   */
  from_file: boolean
  z_bottom_mm: number
  z_top_mm: number
}

export interface BoardLayer {
  /** The net whose pours dominate this layer, when one covers enough of it to be a plane. */
  plane_net?: string
  /** Fraction of the board that net's pours cover, 0..1. */
  plane_coverage?: number
  name: string
  kind: string
  index: number
  /** Height of this copper layer above the bottom of the stack, in mm. */
  z_mm: number
}

export interface BoardNet {
  index: number
  name: string
  tracks: number
  vias: number
  pads: number
  length_mm: number
  layers: string[]
}

export interface BoardVia {
  x: number
  y: number
  size_mm: number
  drill_mm: number
  net: string
  layers: string[]
  kind: 'through' | 'blind' | 'micro'
}

export interface BoardPad {
  ref: string
  number: string
  net: string
  x: number
  y: number
  type: string
  drill_mm: number
  layers: string[]
}

/**
 * One contiguous run of vertices in `geometry.bin`, all on one layer and one net.
 *
 * `offset` and `count` are in vertices, not bytes or triangles. Grouping this way is what
 * lets the viewer highlight a net or hide a layer by changing a draw range rather than
 * re-uploading the buffer.
 */
export interface GeometryGroup {
  layer: string
  net: string
  offset: number
  count: number
}

export interface GeometryIndex {
  file: string
  dtype: 'float32'
  components: 2
  primitive: 'triangles'
  vertex_count: number
  byte_length: number
  groups: GeometryGroup[]
}

export interface BoardDoc {
  format_version: number
  source: Record<string, unknown>
  units: 'mm'
  coordinate_system: BoardCoordinateSystem
  board: {
    width_mm: number
    height_mm: number
    thickness_mm: number
    outline: [number, number][][]
  }
  layers: BoardLayer[]
  stackup: StackupEntry[]
  nets: BoardNet[]
  vias: BoardVia[]
  pads: BoardPad[]
  geometry: GeometryIndex
  kicad: { version: number; generator: string }
  warnings: string[]
}

/** A finding from the fast rules tier. */
export interface RuleFinding {
  id: string
  rule: string
  severity: 'critical' | 'warning' | 'info'
  title: string
  detail: string
  net?: string
  layer?: string
  /**
   * Board-space location in mm, so the UI can zoom the viewer to it.
   *
   * Null — not merely absent — for findings that have no single place on the board, such
   * as a summary count. Check with `!= null`.
   */
  x?: number | null
  y?: number | null
  /** Optional bounding box in mm: [minX, minY, maxX, maxY]. */
  bbox?: [number, number, number, number]
}

export interface RulesDoc {
  /** How many findings the project's suppressions hid. Shown, so a gap is explainable. */
  suppressed?: number
  /** What the suppressions hid, and the reason each gave. */
  suppressed_findings?: {
    rule: string
    net: string
    title: string
    reason: string
    source?: import('./rulesSettings').SettingSource
  }[]
  /** Settings that could not be applied — a typo in a rules file, or a newer schema. */
  settings_warnings?: string[]
  /**
   * What came with the board and what did not: the rules file that was read, a missing
   * project file, unfilled zones. Plain sentences from the worker; see lib/notices.ts.
   */
  notes?: string[]
  /** The settings the checks ran with. Absent on boards analysed before it existed. */
  settings?: import('./rulesSettings').RulesSettingsReport
  format_version: number
  findings: RuleFinding[]
  summary: {
    critical: number
    warning: number
    info: number
    /** Frequencies the length-based checks were evaluated against. */
    assumed_max_frequency_hz: number
  }
}

/** One row of nets.json: a net's lengths, delay, and skew against its reference. */
export interface NetRow {
  net: string
  netclass: string
  kind: string
  topology: string
  pads: number | null
  unreachable_pads: number | null
  /** All the copper on the net. */
  copper_mm: number
  /** The longest pad-to-pad route: what a signal actually travels. */
  path_mm: number | null
  path_from: string
  path_to: string
  path_by_layer: string
  path_vias: number | null
  vias_total: number | null
  delay_ps: number | null
  width_min_mm: number | null
  width_max_mm: number | null
  diff_pair_partner: string
  match_group: string
  match_kind: string
  match_reference: string
  reference_delay_ps: number | null
  skew_ps: number | null
  skew_mm: number | null
  tolerance_ps: number | null
  within_tolerance: string
  clock_net: string
  vs_clock_ps: number | null
  vs_clock_mm: number | null
}

export interface NetReport {
  format_version: number
  /** The DDR clock the vs-clock columns compare against, or '' when none was identified. */
  clock_net: string
  epsilon_assumed: boolean
  rows: NetRow[]
  /** Built in the browser from board.json for a board analysed before nets.json existed. */
  partial?: boolean
}
