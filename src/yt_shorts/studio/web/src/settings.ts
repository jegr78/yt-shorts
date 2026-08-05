/** Pure display helper for the Settings screen (see components/SettingsScreen.tsx
 * and GET /api/settings in api.py), kept in its own module - exporting no React -
 * so Vite's fast-refresh boundary stays component-only (same convention as
 * scopedApi.ts/eventAdmin.ts/slug.ts) and the mapping is unit-tested directly. */

/** Turns the workspace's `origin` (see yt_shorts.workspace.resolve, whose
 * three literal return values this switches on) into the sentence the
 * operator actually needs: not just WHICH of the three resolution rules
 * fired, but what to do about it if the data root looks wrong. An origin
 * `resolve()` never actually returns is passed through as-is rather than
 * guessed at, so a future fourth origin still shows something honest
 * instead of a fabricated label. */
export function originLabel(origin: string): string {
  switch (origin) {
    case 'YT_SHORTS_DATA':
      return 'from $YT_SHORTS_DATA'
    case 'default':
      return 'default (~/YT-Shorts-Data)'
    case 'repository':
      return 'repository fallback'
    default:
      return origin
  }
}

/** A cheap, LOCAL hint for the job-queue pool-limit field (Task 11's
 * QueueLimitsPanel in SettingsScreen.tsx) - never the authority. It mirrors
 * only the "whole number, at least 1" half of what the server checks
 * (api.py's `_validated_limits`), so the Save button can disable itself for
 * an obviously-unusable value without the client re-deciding anything the
 * server has not also been asked: whether a pool name is one this
 * workspace actually has is answered only by the response to the PUT, and
 * this function does not attempt it. Returns `null` for anything that is
 * not a positive whole number typed with no extra characters (an empty
 * field mid-edit, "1.5", "-1", "0", "lots") - all of which the server would
 * refuse too, just later and over the network. */
export function parsePoolLimit(raw: string): number | null {
  const trimmed = raw.trim()
  if (!/^[1-9]\d*$/.test(trimmed)) return null
  return Number(trimmed)
}
