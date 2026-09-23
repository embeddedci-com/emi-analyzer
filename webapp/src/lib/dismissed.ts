/**
 * Hints and notes a user has closed, remembered in this browser.
 *
 * Storage can be missing or refuse (a private window, a sandboxed webview, a full quota), and
 * none of that may break the page: a hint that comes back is the worst case.
 */

const PREFIX = 'emi.dismissed.'

export function isDismissed(key: string): boolean {
  try {
    return globalThis.localStorage?.getItem(PREFIX + key) === '1'
  } catch {
    return false
  }
}

export function dismiss(key: string): void {
  try {
    globalThis.localStorage?.setItem(PREFIX + key, '1')
  } catch {
    // Not remembered; it shows again next time.
  }
}
