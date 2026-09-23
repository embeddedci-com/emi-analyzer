import { describe, expect, it } from 'vitest'
import type { Run } from './emiApi'
import { complianceParams, runPhase } from './complianceRun'

const run = (over: Partial<Run>): Run => ({
  id: 'r1', project_id: 'p1', kind: 'compliance', status: 'new',
  created_at: '', updated_at: '', ...over,
})

describe('runPhase', () => {
  it('is running while queued, however long that takes', () => {
    // The tab used to call this a worker failure after thirty seconds.
    expect(runPhase(run({ status: 'new' }))).toEqual({
      phase: 'running', message: 'Waiting for a worker',
    })
    expect(runPhase(run({ status: 'in_progress', progress: { message: 'combining paths' } as Run['progress'] })).message)
      .toBe('combining paths')
  })

  it('shows the worker\'s own error when the run failed', () => {
    const p = runPhase(run({ status: 'failed', error: 'the attached driver could not be read' }))
    expect(p).toEqual({ phase: 'failed', message: 'the attached driver could not be read' })
    expect(runPhase(run({ status: 'timed_out' })).message).toBe('The run ended as timed out.')
  })

  it('is done only when the run is', () => {
    expect(runPhase(run({ status: 'done' })).phase).toBe('done')
    expect(runPhase(undefined).phase).toBe('idle')
  })
})

describe('complianceParams', () => {
  it('names the solve and driver and sends nothing the gate derives', () => {
    const p = complianceParams({
      standardId: 'fcc-15b-radiated-3m', solveRunId: 's1', driverId: 'd1',
      cableAssignments: { J1: { type: 'none' } }, power: 'dc', enclosure: 'plastic',
    })
    expect(p).toMatchObject({ solve_run_id: 's1', driver_id: 'd1', power: 'dc' })
    for (const key of ['paths', 'driven_ports', 'far_field_refs', 'connectors',
      'solved_f_max_hz', 'power_entry_found']) {
      expect(p).not.toHaveProperty(key)
    }
  })
})
