/** Pure client-side guards for the event-admin dialogs (New / Rename /
 * Delete), kept in their own module - exporting no React - so Vite's
 * fast-refresh boundary stays component-only (same convention as
 * words.ts/format.ts/scopedApi.ts) and each rule is unit-tested directly.
 *
 * The name rule mirrors the backend's security boundary exactly
 * (yt_shorts.event_admin.validate_name -> pathnames.validate_segment): an event
 * name becomes a directory name, so the dialog rejects a bad name before
 * sending rather than round-tripping to a 400. The server still validates -
 * this is a convenience, not the boundary. The rule itself lives in slug.ts
 * (the ONE segment rule, shared with the channel-admin dialogs, just as the
 * backend shares pathnames.validate_segment across event_admin and
 * channel_admin); this module delegates to it so the two cannot drift. */

import { isValidSlug, MAX_SLUG_LENGTH } from './slug'

export const MAX_EVENT_NAME_LENGTH = MAX_SLUG_LENGTH

/** The same slug rule the backend enforces (event_admin.validate_name), so the
 * dialog can reject a bad name before sending. Delegates to the shared
 * isValidSlug - identical behaviour, one rule. */
export function isValidEventName(name: string): boolean {
  return isValidSlug(name)
}

/** The delete gate: the operator must type the event's exact name. Trailing
 * whitespace is tolerated (trimmed) so a stray space does not block a correct
 * confirmation, but the name itself must match exactly. */
export function deleteConfirmed(typed: string, eventName: string): boolean {
  return typed.trim() === eventName
}
