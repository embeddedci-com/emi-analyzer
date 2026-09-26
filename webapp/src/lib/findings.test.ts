import { describe, expect, it } from 'vitest'
import type { RuleFinding } from './boardTypes'
import { findingAction, groupFindings, numberFindings } from './findings'

const f = (id: string, rule: string, severity: RuleFinding['severity'], x: number | null = 1): RuleFinding =>
  ({ id, rule, severity, title: id, detail: 'Why it matters. Do this.', x, y: x })

describe('groupFindings', () => {
  it('puts the rule with the worst finding first, and the worst first within a rule', () => {
    const groups = groupFindings([
      f('a', 'radiator', 'warning'),
      f('b', 'return-via', 'warning'),
      f('c', 'return-via', 'critical'),
      f('d', 'stackup', 'info'),
    ])
    expect(groups.map(([rule]) => rule)).toEqual(['return-via', 'radiator', 'stackup'])
    expect(groups[0][1].map((x) => x.id)).toEqual(['c', 'b'])
  })

  it('keeps the worker order between equals', () => {
    const groups = groupFindings([f('a', 'x', 'warning'), f('b', 'y', 'warning'), f('c', 'x', 'warning')])
    expect(groups.map(([rule, list]) => [rule, list.map((x) => x.id)])).toEqual([
      ['x', ['a', 'c']], ['y', ['b']],
    ])
  })
})

describe('numberFindings', () => {
  it('numbers in list order, from 1, including findings with no position', () => {
    const n = numberFindings([
      f('a', 'radiator', 'warning'), f('b', 'return-via', 'critical'), f('c', 'radiator', 'info', null),
    ])
    expect([...n.entries()]).toEqual([['b', 1], ['a', 2], ['c', 3]])
  })
})

describe('findingAction', () => {
  it('has a line for a known check', () => {
    expect(findingAction({ rule: 'return-via', detail: '' })).toBe('Add a ground via next to the layer change.')
  })
  it('falls back to the last sentence of the detail', () => {
    expect(findingAction({ rule: 'new-check', detail: 'Why it matters. Do this.' })).toBe('Do this.')
  })
})
