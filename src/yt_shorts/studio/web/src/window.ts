import type { ClipDetail } from './api'

/** PATCH /api/clips/{name} never echoes `detected_window`/`effective_window`
 * back (see api.py's patch_clip route - it returns `_summary` plus only
 * `words`/`conflict`), unlike GET, which always computes them fresh. Every
 * handler that calls patchClip must carry these two fields forward itself
 * rather than trusting the response to have them - this is the one place that
 * happens, so the call sites (save, reset title, change status) can't drift
 * apart on how. `effectiveOverride` is passed only by the handler that
 * actually changed the window (save, when windowDirty; reset, to the detected
 * window); every other handler leaves the window exactly as it was on `base`.
 *
 * Kept in its own module rather than in ClipEditor so it is unit-testable and
 * Vite's fast-refresh boundary stays component-only (same convention as
 * words.ts / format.ts). */
export function withWindow(
  patchResult: ClipDetail,
  base: ClipDetail,
  effectiveOverride?: [number, number],
): ClipDetail {
  return {
    ...patchResult,
    detected_window: base.detected_window,
    effective_window: effectiveOverride ?? base.effective_window,
  }
}
