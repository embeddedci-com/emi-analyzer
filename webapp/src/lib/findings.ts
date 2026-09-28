/**
 * The order findings are listed in, their numbers, and the one line that says what to do.
 *
 * The list and the board share this: a marker numbered 3 on the board is the third finding in
 * the list, so both have to come from one ordering rather than two that happen to agree.
 */

import type { RuleFinding } from './boardTypes'

const RANK: Record<RuleFinding['severity'], number> = { critical: 0, warning: 1, info: 2 }

/**
 * Findings grouped by rule, the rule with the worst finding first, and within a rule the worst
 * first. Ties keep the worker's order, so the numbering is stable across reloads.
 */
export function groupFindings(findings: RuleFinding[]): [string, RuleFinding[]][] {
  const byRule = new Map<string, RuleFinding[]>()
  for (const f of findings) {
    const list = byRule.get(f.rule)
    if (list) list.push(f)
    else byRule.set(f.rule, [f])
  }
  const worst = (list: RuleFinding[]) => Math.min(...list.map((f) => RANK[f.severity] ?? 2))
  return [...byRule.entries()]
    .map(([rule, list]): [string, RuleFinding[]] => [
      rule,
      list.map((f, i) => ({ f, i }))
        .sort((a, b) => (RANK[a.f.severity] ?? 2) - (RANK[b.f.severity] ?? 2) || a.i - b.i)
        .map(({ f }) => f),
    ])
    .map((g, i) => ({ g, i }))
    .sort((a, b) => worst(a.g[1]) - worst(b.g[1]) || a.i - b.i)
    .map(({ g }) => g)
}

/** Finding id to its 1-based number in {@link groupFindings} order. */
export function numberFindings(findings: RuleFinding[]): Map<string, number> {
  const out = new Map<string, number>()
  for (const [, list] of groupFindings(findings)) {
    for (const f of list) out.set(f.id, out.size + 1)
  }
  return out
}

/** One short line per check: what to change. */
const ACTION: Record<string, string> = {
  'plane-gap': 'Route over solid plane, or bridge the gap with a stitching capacitor.',
  'return-via': 'Add a ground via next to the layer change.',
  'via-stub': 'Change layers nearer the via end, or backdrill it.',
  'test-point-stub': 'Put the test point on the trace, not at the end of a branch.',
  radiator: 'Shorten the trace, or route it on an inner layer between planes.',
  'edge-proximity': 'Move the copper away from the board edge.',
  'ddr-skew': 'Add length to the short traces in the group.',
  impedance: 'Adjust the trace width for the target impedance.',
  'pair-coupling': 'Keep both halves together, with the same clearance to any pour.',
  decoupling: 'Move the capacitor next to the pin, with its own ground via.',
  stitching: 'Add vias to tie the planes together here.',
  'edge-stitching': 'Add a row of ground vias along this edge.',
  stackup: 'Put a solid plane next to each signal layer.',
  'copper-island': 'Tie the copper to ground with vias, or remove it.',
  crystal: 'Keep the crystal next to its IC, over solid ground.',
  antenna: 'Keep every layer clear under the antenna.',
  'esd-protection': 'Put a TVS clamp at the connector, before any IC.',
  'connector-shield': 'Tie the connector shield to ground.',
  'reset-filter': 'Add a small capacitor to ground at the reset pin.',
  'input-filter': 'Add a capacitor or filter at the power input.',
  'switch-node': 'Keep the switch node copper small and tight.',
  'power-neck': 'Widen the trace to match the copper on either side, or pour it.',
  'cable-resonance': 'Filter the lines at the connector.',
}

/**
 * What to do about a finding, in one line. A check this list does not know yet falls back to
 * the last sentence of the finding's own text, which is where the worker puts the fix.
 */
export function findingAction(f: Pick<RuleFinding, 'rule' | 'detail' | 'action'>): string {
  // The worker's own line wins: one rule can find different problems with different fixes.
  if (f.action) return f.action
  const known = ACTION[f.rule]
  if (known) return known
  const sentences = f.detail.trim().split(/(?<=\.)\s+/)
  return sentences[sentences.length - 1] ?? ''
}
