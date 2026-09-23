/**
 * The board behind "Try the sample board": the worker's own tiny test fixture, bundled as
 * text so it opens with no network and no file on disk. A copy rather than an import from
 * worker/, because some builds of this webapp see only webapp/; sampleBoard.test.ts fails
 * when the two drift.
 */

import text from '../assets/sample.kicad_pcb?raw'

export const SAMPLE_BOARD_NAME = 'Sample board'

/** A File, so it goes through exactly the upload path a user's own board does. */
export function sampleBoardFile(): File {
  return new File([text], 'sample.kicad_pcb', { type: 'application/octet-stream' })
}
