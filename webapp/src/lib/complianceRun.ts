/**
 * The Compliance tab's decisions, kept out of the component so they can be tested.
 *
 * Two things went wrong here before. The tab gave up on a run after thirty seconds of failed
 * artifact fetches and called it "a worker failure", when the run was simply still queued; and
 * it sent `driven_ports: ['p1']` and an empty `paths` list, inputs the gate must never take from
 * a client. The phase now comes from the run's status, and the params carry only names and
 * the user's own declarations.
 */

import { TERMINAL_STATUSES, type ComplianceParams, type Run } from './emiApi'

export type CompliancePhase = 'idle' | 'running' | 'done' | 'failed'

export function runPhase(run: Run | undefined): { phase: CompliancePhase; message: string } {
  if (!run) return { phase: 'idle', message: '' }
  if (run.status === 'done') return { phase: 'done', message: '' }
  if (TERMINAL_STATUSES.includes(run.status)) {
    // The worker's own words: "the attached driver could not be read", a refused standard.
    return {
      phase: 'failed',
      message: run.error || `The run ended as ${run.status.replace('_', ' ')}.`,
    }
  }
  return {
    phase: 'running',
    message: run.progress?.message || (run.status === 'new' ? 'Waiting for a worker' : 'Running'),
  }
}

export interface ComplianceChoices {
  standardId: string
  solveRunId?: string
  driverId?: string | null
  cableAssignments: Record<string, { type: string | null; length_m?: number }>
  power: string
  enclosure: string
  findings?: Record<string, unknown>[]
}

/** The request body's params: names and declarations, nothing the gate derives. */
export function complianceParams(c: ComplianceChoices): ComplianceParams {
  return {
    standard_id: c.standardId,
    solve_run_id: c.solveRunId,
    driver_id: c.driverId ?? undefined,
    cable_assignments: c.cableAssignments,
    power: (c.power || '') as ComplianceParams['power'],
    enclosure: (c.enclosure || 'none') as ComplianceParams['enclosure'],
    findings: c.findings ?? [],
  }
}
