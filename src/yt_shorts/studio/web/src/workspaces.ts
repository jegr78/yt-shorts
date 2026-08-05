/** Pure helpers for the workspace manager, kept out of components so Vite's
 * fast-refresh boundary stays component-only and each rule is unit-tested. */

const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

/** Mirrors pathnames.validate_segment - the server is still the boundary.
 * JS's `$` can match before a trailing line terminator in some contexts,
 * unlike Python's `\Z`; reject any embedded CR/LF explicitly so this stays
 * in parity with the backend rather than relying on that subtlety. */
export function isValidWorkspaceName(name: string): boolean {
  return NAME_PATTERN.test(name) && !/[\r\n]/.test(name)
}

/** POSIX-join a parent directory and a child name. */
export function joinPath(parent: string, name: string): string {
  return `${parent.replace(/\/+$/, '')}/${name}`
}
