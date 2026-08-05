/** Tabular time formatters shared by the clip list and the stream panel.
 * Kept in their own module rather than exported from a component file so
 * Vite's fast-refresh boundary stays component-only (same convention as
 * words.ts), and so they can be unit-tested directly. */

/** A clip's duration as minutes:seconds.tenths, zero-padded seconds. */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '--:--'
  const whole = Math.floor(seconds)
  const minutes = Math.floor(whole / 60)
  const secs = whole % 60
  const tenths = Math.round((seconds - whole) * 10)
  return `${minutes}:${String(secs).padStart(2, '0')}.${tenths}`
}

/** A stream's duration as hours:minutes:seconds (hours dropped when zero),
 * without a tenths digit - a stream is hours long, tenths would be noise. */
export function formatStreamDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '--:--'
  const whole = Math.floor(seconds)
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const secs = whole % 60
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
  }
  return `${minutes}:${String(secs).padStart(2, '0')}`
}
