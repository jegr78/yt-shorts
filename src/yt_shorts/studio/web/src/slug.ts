/** The one client-side "safe path segment" rule, shared by the event-admin and
 * channel-admin dialogs, kept in its own module - exporting no React - so Vite's
 * fast-refresh boundary stays component-only (same convention as
 * words.ts/format.ts/scopedApi.ts) and the rule is unit-tested directly.
 *
 * It mirrors the backend's single security boundary exactly
 * (yt_shorts.pathnames.validate_segment / NAME_PATTERN): an event name and a
 * channel slug each become a directory name, so the dialog rejects a bad name
 * before sending rather than round-tripping to a 400. The server still
 * validates - this is a convenience, not the boundary. */

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
export const MAX_SLUG_LENGTH = 100

/** The same slug rule the backend enforces (pathnames.validate_segment), so a
 * dialog can reject a bad slug before sending. */
export function isValidSlug(name: string): boolean {
  return name.length > 0 && name.length <= MAX_SLUG_LENGTH && NAME_RE.test(name)
}
