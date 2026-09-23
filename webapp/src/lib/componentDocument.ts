/**
 * The `emi-component` document — the browser's copy (docs/implementation.md §3).
 *
 * The worker holds the other implementation (`worker/emi_worker/components/document.py`) and
 * both are checked against `server/emi/testdata/component_fixtures.json`. The browser previews
 * a component's impedance while someone types it and the worker then places that same
 * component in a solve, so a disagreement is a user shown a self-resonance the solve does not
 * model.
 *
 * **Built-in numbers are cited, never remembered.** A model giving an ESL or ESR without a
 * matching `sources` entry is refused here as it is there. 0.4 nH and 0.6 nH for the same
 * 0402 move the self-resonance by 20 %, and nobody can tell which was meant unless the
 * document says.
 */

export class ComponentError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ComponentError'
  }
}

export const COMPONENT_FORMAT = 'emi-component'
export const COMPONENT_VERSION = 1

export const KINDS = ['capacitor'] as const
export type Kind = (typeof KINDS)[number]

/**
 * Where a component's numbers come from, which is a different question from which document
 * they are written in. It decides what the UI may claim about them — in particular, a
 * `generic` entry is a class average and is never attributed to a manufacturer, because it
 * describes none.
 */
export const PROVENANCE = ['generic', 'vendor', 'measured', 'user'] as const
export type Provenance = (typeof PROVENANCE)[number]

export const RESOLVERS = ['series_rlc', 'mlcc_family'] as const
export const FUTURE_RESOLVERS = [
  'touchstone', 'ferrite', 'regulator_output', 'connector', 'package',
] as const

export interface Source {
  doc: string
  rev?: string
  what?: string
}

export interface SeriesRLC {
  c_f: number
  esl_h: number | null
  esr_ohm: number | null
  esl_includes_mount: boolean
}

export interface MLCCFamily {
  eslByPackage: Record<string, number>
  /** (capacitance, ESR) in increasing capacitance, interpolated log-log. */
  esrTable: [number, number][]
}

export interface Component {
  id: string
  kind: Kind
  name: string
  match: Record<string, unknown>
  modelType: string
  model: Record<string, unknown>
  sources: Source[]
  validHz: [number, number] | null
  eslIncludesMount: boolean
  provenance: Provenance
}

export function describeSource(s: Source): string {
  const parts = [s.doc]
  if (s.rev) parts.push(`rev ${s.rev}`)
  return parts.join(' ') + (s.what ? ` (${s.what})` : '')
}

export function cites(c: Component, what: string): Source | null {
  for (const s of c.sources) {
    if (!s.what || s.what.toLowerCase().includes(what.toLowerCase())) return s
  }
  return null
}

export const isGeneric = (c: Component): boolean => c.provenance === 'generic'

/** One line for the UI. Never names a manufacturer for a generic entry. */
export function describeProvenance(c: Component): string {
  if (c.provenance === 'generic') {
    return 'generic — typical for this package and value, not a specific part'
  }
  if (c.provenance === 'vendor') {
    const s = cites(c, 'ESL') ?? cites(c, '')
    return `from the part datasheet${s ? ` (${describeSource(s)})` : ''}`
  }
  if (c.provenance === 'measured') {
    const s = cites(c, '')
    return `measured${s ? ` (${describeSource(s)})` : ''}`
  }
  return 'entered by hand'
}

const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function optNumber(v: unknown, where: string): number | null {
  if (v === null || v === undefined) return null
  if (!num(v)) throw new ComponentError(`${where}: expected a number or null, got ${typeof v}`)
  return v
}

export function seriesRLC(c: Component): SeriesRLC {
  if (c.modelType !== 'series_rlc') {
    throw new ComponentError(`${c.id} is a ${c.modelType} model, not a series R-L-C`)
  }
  return {
    c_f: Number(c.model.c_f),
    esl_h: optNumber(c.model.esl_h, 'esl_h'),
    esr_ohm: optNumber(c.model.esr_ohm, 'esr_ohm'),
    esl_includes_mount: c.eslIncludesMount,
  }
}

export function mlccFamily(c: Component): MLCCFamily {
  if (c.modelType !== 'mlcc_family') {
    throw new ComponentError(`${c.id} is a ${c.modelType} model, not an MLCC family`)
  }
  const packages = (c.model.esl_h_by_package ?? {}) as Record<string, number>
  const table = ((c.model.esr_ohm_by_c ?? []) as [number, number][])
    .map(([cf, r]) => [Number(cf), Number(r)] as [number, number])
    .sort((a, b) => a[0] - b[0])
  return { eslByPackage: packages, esrTable: table }
}

/** Interpolated ESR, clamped at both ends rather than extrapolated. */
export function esrFor(family: MLCCFamily, cF: number): number | null {
  const t = family.esrTable
  if (t.length === 0 || !(cF > 0)) return null
  if (cF <= t[0][0]) return t[0][1]
  if (cF >= t[t.length - 1][0]) return t[t.length - 1][1]
  for (let i = 0; i < t.length - 1; i++) {
    const [c0, r0] = t[i]
    const [c1, r1] = t[i + 1]
    if (c0 <= cF && cF <= c1) {
      const k = (Math.log10(cF) - Math.log10(c0)) / (Math.log10(c1) - Math.log10(c0))
      return 10 ** (Math.log10(r0) + k * (Math.log10(r1) - Math.log10(r0)))
    }
  }
  return null
}

/** The frequency above which a capacitor is an inductor. Null without an ESL. */
export function selfResonanceHz(rlc: SeriesRLC): number | null {
  if (rlc.esl_h === null || !(rlc.esl_h > 0) || !(rlc.c_f > 0)) return null
  return 1 / (2 * Math.PI * Math.sqrt(rlc.esl_h * rlc.c_f))
}

export function impedanceAt(rlc: SeriesRLC, frequencyHz: number): { re: number; im: number } {
  if (rlc.esl_h === null || rlc.esr_ohm === null) {
    throw new ComponentError(
      'impedance needs both an ESL and an ESR; this model carries neither a measured nor a ' +
        'cited value for at least one of them',
    )
  }
  const w = 2 * Math.PI * frequencyHz
  return { re: rlc.esr_ohm, im: w * rlc.esl_h - 1 / (w * rlc.c_f) }
}

export const isComplete = (rlc: SeriesRLC): boolean =>
  rlc.esl_h !== null && rlc.esr_ohm !== null

export function parseComponent(doc: unknown): Component {
  if (typeof doc !== 'object' || doc === null || Array.isArray(doc)) {
    throw new ComponentError('a component document must be a JSON object')
  }
  const d = doc as Record<string, unknown>
  if (d.format !== COMPONENT_FORMAT) {
    throw new ComponentError(`not a component document: format=${JSON.stringify(d.format)}`)
  }
  const version = d.version
  if (typeof version !== 'number' || !Number.isInteger(version) || version < 1) {
    throw new ComponentError(`version must be a whole number from 1, got ${JSON.stringify(version)}`)
  }
  if (version > COMPONENT_VERSION) {
    throw new ComponentError(
      `this component is version ${version} and this build understands version ` +
        `${COMPONENT_VERSION}. Update the analyzer rather than reading the parts it recognises`,
    )
  }
  for (const required of ['id', 'kind', 'name'] as const) {
    const v = d[required]
    if (typeof v !== 'string' || v.trim() === '') {
      throw new ComponentError(`a component needs a ${required}`)
    }
  }
  if (!(KINDS as readonly string[]).includes(d.kind as string)) {
    throw new ComponentError(
      `unknown component kind ${JSON.stringify(d.kind)}; this build models ${KINDS.join(', ')}`,
    )
  }

  const model = d.model
  if (typeof model !== 'object' || model === null || typeof (model as Record<string, unknown>).type !== 'string') {
    throw new ComponentError('a component needs a model with a type')
  }
  const modelType = (model as Record<string, unknown>).type as string
  if ((FUTURE_RESOLVERS as readonly string[]).includes(modelType)) {
    throw new ComponentError(
      `'${modelType}' is a model type this build does not resolve yet. The document is ` +
        `valid; the resolver ships in a later phase`,
    )
  }
  if (!(RESOLVERS as readonly string[]).includes(modelType)) {
    throw new ComponentError(`unknown model type '${modelType}'`)
  }

  const provenance = (d.provenance ?? 'user') as string
  if (!(PROVENANCE as readonly string[]).includes(provenance)) {
    throw new ComponentError(
      `unknown provenance '${provenance}'; use one of ${PROVENANCE.join(', ')}`,
    )
  }

  let validHz: [number, number] | null = null
  if (d.valid_hz !== undefined && d.valid_hz !== null) {
    const v = d.valid_hz
    if (!Array.isArray(v) || v.length !== 2 || !v.every(num)) {
      throw new ComponentError('valid_hz must be a [low, high] pair in hertz')
    }
    if (v[0] >= v[1]) throw new ComponentError('valid_hz must be increasing')
    validHz = [v[0], v[1]]
  }

  const sources: Source[] = ((d.sources ?? []) as unknown[])
    .filter((s): s is Record<string, unknown> => typeof s === 'object' && s !== null)
    .map((s) => ({
      doc: String(s.doc ?? ''), rev: String(s.rev ?? ''), what: String(s.what ?? ''),
    }))

  const c: Component = {
    id: (d.id as string).trim(),
    kind: d.kind as Kind,
    name: (d.name as string).trim(),
    match: (d.match ?? {}) as Record<string, unknown>,
    modelType,
    model: model as Record<string, unknown>,
    sources,
    validHz,
    eslIncludesMount: Boolean(d.esl_includes_mount),
    provenance: provenance as Provenance,
  }

  if (modelType === 'series_rlc') {
    const rlc = seriesRLC(c)
    for (const [value, what] of [[rlc.esl_h, 'ESL'], [rlc.esr_ohm, 'ESR']] as const) {
      if (value !== null && cites(c, what) === null) {
        throw new ComponentError(
          `${c.id} gives an ${what} of ${value} but says nothing about where it came from. ` +
            `Add a sources entry — for a generic figure that is simply what it is, and for a ` +
            `vendor part it is the datasheet`,
        )
      }
    }
  } else {
    const fam = mlccFamily(c)
    if (Object.keys(fam.eslByPackage).length === 0) {
      throw new ComponentError(`${c.id} is an MLCC family with no packages`)
    }
    if (cites(c, 'ESL') === null) {
      throw new ComponentError(
        `${c.id} gives per-package ESL values but says nothing about where they came from`,
      )
    }
  }
  if (provenance === 'vendor' && sources.length === 0) {
    throw new ComponentError(`${c.id} claims to come from a vendor datasheet but cites none`)
  }
  return c
}
