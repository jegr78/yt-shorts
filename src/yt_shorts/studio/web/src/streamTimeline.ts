/**
 * Pure geometry for the stream screen: seconds to pixels and back.
 *
 * No React here, deliberately - the same rule words.ts and format.ts follow, so
 * Vite's fast-refresh boundary stays component-only and this maths is tested
 * without rendering anything.
 *
 * The screen has two lanes because one cannot do the job. An endurance stream
 * is six to eight hours; over a 1200-pixel overview that is ~24 seconds per
 * pixel, so a 20-second clip is under a single pixel and no boundary can be
 * expressed there. The overview LOCATES, the zoom lane EDITS, and zoomAround is
 * the bridge between them.
 */

export interface Zoom {
  start: number
  end: number
}

/** The narrowest zoom window that can still be dragged accurately. */
export const MIN_ZOOM_SECONDS = 10

export function secondsToFraction(seconds: number, zoom: Zoom): number {
  const span = zoom.end - zoom.start
  // A collapsed window would divide by zero and paint NaN into a style
  // attribute, which renders as an element that silently vanishes.
  if (span <= 0) return 0
  return (seconds - zoom.start) / span
}

export function fractionToSeconds(fraction: number, zoom: Zoom): number {
  return zoom.start + fraction * (zoom.end - zoom.start)
}

export function clampZoom(zoom: Zoom, duration: number, minSpan = MIN_ZOOM_SECONDS): Zoom {
  // A stream shorter than the requested span is shown whole rather than
  // padded with time that does not exist.
  if (duration <= minSpan) return { start: 0, end: Math.max(duration, 0) }
  const span = Math.min(Math.max(zoom.end - zoom.start, minSpan), duration)
  const start = Math.min(Math.max(zoom.start, 0), duration - span)
  return { start, end: start + span }
}

export function zoomAround(centre: number, span: number, duration: number): Zoom {
  return clampZoom({ start: centre - span / 2, end: centre + span / 2 }, duration, span)
}

/** One curve value per 60 s - moments.activity_curve's own step. */
export const CURVE_STEP_SECONDS = 60

/**
 * The activity bucket covering a given second - deliberate, tested
 * groundwork, not yet wired into a component, the same status CLAUDE.md
 * documents for `pump_subprocess`/`shorten_urls` in logsetup.py and for
 * `timecode.with_padding`/`render.Source`'s `--download-sections` path: it
 * exists for a later addition (e.g. highlighting the overview bar under the
 * player's current playback position, or under a pointer hovering the
 * strip) rather than for anything StreamTimeline.tsx does today, which maps
 * `activity` to bars directly by INDEX and never needs to go from a second
 * back to a bucket. Do not delete it as dead code; it is covered on its own
 * in streamTimeline.test.ts, including the lower-clamp regression that
 * motivated pinning it in the first place.
 *
 * No `duration` parameter, deliberately: an earlier version took one and
 * never used it (suppressed with a bare `void duration`), because the
 * curve's length is derived from the last WORD, not from the stream's
 * duration - activity_curve appends a trailing bucket when that word ends on
 * an exact multiple of the step, so `duration` was never safe to use for
 * this calculation in the first place and the parameter was pure noise.
 */
export function curveBucket(seconds: number, activity: number[]): number {
  // -1 rather than 0 for an empty curve: 0 is a real bucket, and a caller that
  // cannot tell the two apart draws a bar for a stream with no curve at all.
  if (activity.length === 0) return -1
  const index = Math.floor(seconds / CURVE_STEP_SECONDS)
  // Neither safe to assume length === ceil(duration/step) nor to index past
  // the end - see the module docstring above.
  return Math.min(Math.max(index, 0), activity.length - 1)
}

/**
 * What a zoom-lane drag's current pointer position means for the selection,
 * given where the drag started - `null` when the pointer has not moved off
 * its start point yet, so the caller fires no change at all rather than a
 * zero-width one it would later have to undo.
 *
 * This is where the zero-width guard belongs: a plain click's pointerdown
 * and pointerup land on the exact same pixel, so `now === start` here means
 * nothing has been dragged yet, full stop - there is no selection to make,
 * and therefore nothing to later tell apart from one made somewhere else
 * (e.g. picking a hit-list row). Guarding on RELEASE instead - comparing
 * the release point back to the drag's start - was tried first and was
 * wrong: by release time the guard can no longer tell "this drag never
 * moved" from "the operator already had an unrelated selection and just
 * clicked once", so it cleared both alike.
 */
export function dragSelection(
  start: number, now: number,
): { start: number; end: number } | null {
  if (now === start) return null
  return { start: Math.min(start, now), end: Math.max(start, now) }
}
