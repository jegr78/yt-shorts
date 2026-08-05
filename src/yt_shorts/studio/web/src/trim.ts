/**
 * The arithmetic behind trimming a rendered short.
 *
 * Pure, and in its own module rather than inside a component, so Vite's
 * fast-refresh boundary stays component-only and these rules are unit-tested
 * without rendering anything - same arrangement as words.ts and window.ts.
 */

export type Trim = { head: number; tail: number }

/** Converts the wire shape (a `[head, tail]` tuple or null - see
 * ClipSummary.trim/trim_applied in api.ts) into this module's own `Trim`
 * shape. Kept here rather than repeated at each of isPending's three call
 * sites (ClipEditor, UploadPanel, ManualUploadPanel). */
export function fromPair(pair: [number, number] | null): Trim | null {
  return pair === null ? null : { head: pair[0], tail: pair[1] }
}

/** The server's own floor (editorial.MIN_REMAINING_SECONDS). Kept in step so
 * the operator is told before the 422, not by it. */
export const MIN_REMAINING_SECONDS = 3

/** Float noise from a JSON round trip must not read as a change. Kept in
 * step with trim.py's own TOLERANCE_SECONDS. */
const TOLERANCE_SECONDS = 0.001

export function remainingSeconds(duration: number, trim: Trim | null): number {
  if (!trim) return duration
  return Math.max(0, duration - trim.head - trim.tail)
}

export function isPending(desired: Trim | null, applied: Trim | null): boolean {
  const a = desired ?? { head: 0, tail: 0 }
  const b = applied ?? { head: 0, tail: 0 }
  return (Math.abs(a.head - b.head) > TOLERANCE_SECONDS
    || Math.abs(a.tail - b.tail) > TOLERANCE_SECONDS)
}

/**
 * Whether "Apply trim" must stay reachable - either because the staged
 * values genuinely differ from what short.mp4 currently embodies
 * (`pending`), or because the server cannot tell what short.mp4 embodies
 * at all (`unknown` - ClipSummary.trim_unknown, see trim.py's
 * `is_unknown`). The unknown case needs the exact same button: clicking it
 * re-PATCHes the operator's current head/tail (0/0 when nothing was ever
 * staged) and starts the same apply job, which is exactly what
 * `ensure_applied`'s own `unknown_but_mastered` branch already knows how
 * to repair. Without folding `unknown` in here, a clip whose desired trim
 * happens to equal its last-known-applied value (the ordinary (0, 0) vs
 * null case) read `isPending` as false and disabled the one control that
 * could reach the repair - the button said "Trim applied" for a file that
 * might not be.
 */
export function trimNeedsAction(pending: boolean, unknown: boolean): boolean {
  return pending || unknown
}

/** Button copy for the three states `trimNeedsAction` distinguishes: an
 * ordinary pending cut, an unknown state needing repair, or nothing to do. */
export function trimActionLabel(pending: boolean, unknown: boolean): string {
  if (pending) return 'Apply trim'
  if (unknown) return 'Repair trim'
  return 'Trim applied'
}

export function trimProblems(duration: number, trim: Trim | null): string[] {
  if (!trim) return []
  const problems: string[] = []
  if (trim.head < 0 || trim.tail < 0) {
    problems.push('A trim cannot be negative - there is no material to add back.')
  }
  if (remainingSeconds(duration, trim) < MIN_REMAINING_SECONDS) {
    problems.push(`Less than ${MIN_REMAINING_SECONDS}s would remain.`)
  }
  return problems
}

/**
 * Reconstructs the untrimmed master's real length from the ALREADY-CUT
 * short's own duration plus whatever trim is currently applied to it.
 *
 * `remainingSeconds`/`trimProblems` above must be checked against this,
 * never against a rendered short's `video.duration` directly, once any
 * trim is applied: `video.duration` is short.mp4's duration, which is
 * already missing `applied.head + applied.tail` seconds the moment a cut
 * has landed, while the operator's staged `trimHead`/`trimTail` are always
 * ABSOLUTE cuts from the untrimmed master (see trim.py's own module
 * docstring - cutting always reads the master, never the current
 * short.mp4, so a second correction does not compound on the first).
 * Measured in a real browser without this: a 10s master trimmed head 2 /
 * tail 3 (a 5.04s short.mp4) read "0:00 after the cut" instead of the true
 * 5.04s, a healthy clip was painted red with "Less than 3s would remain",
 * and staging a SECOND, LARGER trim - "trim 3s, then change your mind and
 * trim 5s", the whole reason the master mechanism exists - was blocked by
 * a false floor error the master itself would have accommodated.
 */
export function masterDuration(shortDuration: number, applied: Trim | null): number {
  const a = applied ?? { head: 0, tail: 0 }
  return shortDuration + a.head + a.tail
}

/**
 * The staged trim expressed relative to the file the player actually has
 * loaded (short.mp4, already cut by `applied`) rather than to the master -
 * for seeking/pausing the free preview only, never for validation (use
 * `masterDuration` for that).
 *
 * Can go negative on either end when the operator stages LESS of a cut
 * than is already applied - undoing part of a previous trim, whose
 * content is not back in short.mp4 until the next apply re-cuts from the
 * master. Callers must clamp a negative `head` before using it as a seek
 * target (a negative `currentTime` is not a valid position); a negative
 * `tail` needs no clamping - comparing `video.currentTime >=
 * video.duration - tail` already just stops firing early, which is
 * correct: there is nothing staged to skip.
 */
export function previewOffsets(trim: Trim, applied: Trim | null): Trim {
  const a = applied ?? { head: 0, tail: 0 }
  return { head: trim.head - a.head, tail: trim.tail - a.tail }
}
