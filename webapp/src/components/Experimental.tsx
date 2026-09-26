/**
 * The mark a feature carries when it works but has not been fully validated.
 *
 * The tool's whole claim is that a number comes with an honest account of how much to trust
 * it, so "experimental" as a bare word would be the wrong kind of hedge: it warns without
 * saying what about, and a user cannot act on it. Every use therefore has to supply `why` —
 * the specific thing that is unproven, in the terms of the measurement that would settle it.
 *
 * This is not the same as an uncertainty figure. σ says how far a number might be off given
 * that the model is right; this says the model itself has not been checked against the case
 * in front of you. A feature can be experimental and precise, or validated and vague.
 */

import { Badge, Tooltip } from '@mantine/core'

export interface ExperimentalProps {
  /**
   * What is unproven and why it matters, in a sentence or two. Written for someone deciding
   * whether to act on the number, not for someone maintaining the code: name the measurement
   * that would settle it, and which way the error is likely to run if it is known.
   */
  why: string
  /** Overrides the label where "experimental" is not the right word (e.g. "provisional"). */
  label?: string
  size?: 'xs' | 'sm'
  mb?: number
}

export function Experimental({ why, label = 'experimental', size = 'xs', mb }: ExperimentalProps) {
  return (
    <Tooltip label={why} withArrow multiline w={340} events={{ hover: true, focus: true, touch: true }}>
      <Badge size={size} variant="light" color="yellow" tt="none" mb={mb} tabIndex={0}
             style={{ cursor: 'help' }}>
        {label}
      </Badge>
    </Tooltip>
  )
}

/**
 * The reasons in use, in one place.
 *
 * Kept together rather than inline at each call site so that the set of things the tool is
 * unsure about can be read in one go — by someone deciding what to validate next, and by the
 * limitations page, which has to list them without going out of date.
 */
export const EXPERIMENTAL = {
  smallPart:
    'Checked on test lines, vias and a synthetic part. Only one real board has been checked '
    + '(docs/verification/small-part-solve.md). The map shows where '
    + 'current flows, relative to the loudest point. The port numbers are what the two ends see '
    + 'with 50 Ω on each. Neighboring nets are left out, so coupling into them is not shown.',

  hotspotMap:
    'Full-wave solving is experimental, and no hotspot map has yet been compared with a '
    + 'near-field scan of the same board. Use the maps to compare layers, frequencies and '
    + 'layouts. A level in dBµA/m is the solve scaled by the driver, and has not been checked '
    + 'against a probe.',

  cableBudget:
    'The antenna model behind this is checked against transmission-line theory, but not yet '
    + 'against a second solver, and the composition it feeds has been compared with a fully coupled '
    + 'simulation on a synthetic board (1.2 dB typical, 2.2 dB worst) but not yet on a real '
    + 'one. Read it as a budget -- how much common-mode current this cable can carry before it '
    + 'reaches the limit -- and compare cables and layouts with it, rather than reading the '
    + 'absolute level as a prediction of what a test house would measure.',

  complianceEstimate:
    'The chain behind this number runs end to end on the small fixture board, and has never '
    + 'been checked against a lab result or a second solver on a real board. The far field '
    + 'matches a second solver on test antennas over the ground plane, not on a board. The '
    + 'uncertainty is uncalibrated. Read the ranking and the contributions before the absolute '
    + 'margin.',

  farField:
    'Checked on test antennas, not on a board: dipoles over the ground plane match a second '
    + 'solver (nec2c) within 0.2 dB from 30 MHz, and within about 1 dB for a resonant one up '
    + 'to its resonance. The top 15 % of the solved band reads up to 2 dB low, so solve past the '
    + 'highest frequency you care about. On a real board the run can stop while the board is '
    + 'still ringing; those frequencies are dropped and marked. No real board has been compared '
    + 'with a measurement. A '
    + 'far-field run is always a long one: the band starts at 30 MHz, which needs 100 ns of '
    + 'simulated time whatever the board is.',

  cableEmissions:
    'The composition behind this — open-circuit voltage at the connector divided by the ' +
    'cable\'s antenna impedance — has been checked against a fully coupled simulation on a ' +
    'synthetic board, where it agreed to 1.2 dB typical and 2.2 dB worst. It has not yet been ' +
    'checked that way on a real board: those runs need the record length the 30 MHz floor ' +
    'implies, and none has been measured. Treat the level as indicative and ' +
    'the ranking between layouts as the useful part. The uncertainty budget carries a ' +
    'deliberately conservative 4.5 dB for this term until real-board residuals replace it.',
} satisfies Record<string, string>

/** What each reason is about, as the limitations page titles it. Typed so none is left out. */
export const EXPERIMENTAL_TITLES: Record<keyof typeof EXPERIMENTAL, string> = {
  smallPart: 'Small-part solve',
  hotspotMap: 'Hotspot maps and levels',
  cableBudget: 'Cable budget',
  complianceEstimate: 'Compliance estimate',
  farField: 'Far field',
  cableEmissions: 'Cable emissions from a solve',
}
