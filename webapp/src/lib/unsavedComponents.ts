/**
 * Components a signed-out visitor has made (docs/implementation.md §3).
 *
 * They are usable immediately — sent inline with a run, so a visitor gets the same modelling
 * a signed-in user does — but they live in `sessionStorage` and are gone when the tab closes.
 * That is the honest bargain: there is no account to file them under, so there is nothing that
 * could bring them back, and pretending otherwise would lose someone's work silently.
 *
 * sessionStorage rather than localStorage on purpose. localStorage would survive the tab and
 * imply a persistence that does not exist; a visitor who closes the tab and returns would find
 * components attached to a cookie they may no longer have.
 */

const KEY = 'emi.unsavedComponents.v1'

export function readUnsaved(): Record<string, unknown>[] {
  try {
    const raw = sessionStorage.getItem(KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    // A private window, cleared storage, or a browser refusing it entirely. An empty list is
    // the right answer everywhere: the caller renders "none yet" and nothing breaks.
    return []
  }
}

export function writeUnsaved(components: Record<string, unknown>[]): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(components))
  } catch {
    /* storage refused; the components still work for this page view */
  }
}

export function addUnsaved(component: Record<string, unknown>): Record<string, unknown>[] {
  const next = [...readUnsaved(), component]
  writeUnsaved(next)
  return next
}

export function removeUnsaved(id: string): Record<string, unknown>[] {
  const next = readUnsaved().filter((c) => c.id !== id)
  writeUnsaved(next)
  return next
}

export function clearUnsaved(): void {
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    /* nothing to do */
  }
}
