/** Pure helpers for the Logs screen (see components/LogsScreen.tsx and the
 * /api/logs routes in api.py), in their own module - exporting no React - so
 * Vite's fast-refresh boundary stays component-only, the same convention as
 * settings.ts/uploadMeta.ts, and the parsing is unit-tested directly. */

export type LogLevel = 'ERROR' | 'WARNING' | 'INFO' | 'OTHER'

/** The tail keeps at most this many lines in memory: a one-hour transcription
 * writes a lot, and an unbounded array would grow until the tab dies. */
export const MAX_LINES = 5000

const LINE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (ERROR|WARNING|INFO|DEBUG|CRITICAL) (.*)$/

/** Split one log line into timestamp / level / message. A line the backend did
 * not write (a traceback's continuation, say) is returned whole at OTHER rather
 * than being dropped - a stack trace is exactly what an operator needs to see. */
export function parseLine(line: string): { timestamp: string; level: LogLevel; message: string } {
  const match = LINE.exec(line)
  if (!match) return { timestamp: '', level: 'OTHER', message: line }
  const level = match[2]
  const known: LogLevel = level === 'ERROR' || level === 'CRITICAL'
    ? 'ERROR'
    : level === 'WARNING' ? 'WARNING' : level === 'INFO' ? 'INFO' : 'OTHER'
  return { timestamp: match[1], level: known, message: match[3] }
}

/** Append newly-tailed lines, capped at `max` by dropping the oldest. Returns
 * the original array untouched when nothing arrived, so React can skip a
 * re-render. */
export function appendLines(existing: string[], incoming: string[],
                            max: number = MAX_LINES): string[] {
  if (incoming.length === 0) return existing
  const combined = [...existing, ...incoming]
  return combined.length <= max ? combined : combined.slice(combined.length - max)
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} kB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** The job kind a job-log filename encodes (`detect-<id>.log[.gz]`), or null
 * when the name is not a job log. */
export function jobKindFromLogName(name: string): string | null {
  const match = /^(render|detect|upload|connect|copy)-[^.]+\.log(\.gz)?$/.exec(name)
  return match ? match[1] : null
}

/** Strips a trailing `.gz`. A job's `log_name` (see api.ts's `Job`, sourced
 * from jobs.py's `Job.snapshot`) is always the bare `<kind>-<id>.log` name,
 * even once `finish_job_log` has rotated the file to `.log.gz` - so the
 * Logs screen's list (GET /api/logs, reporting whatever is actually on disk
 * right now) can show that entry as `.log.gz` while a "View log" link still
 * names it without the suffix. Comparing through this function is what
 * keeps the list's selection highlight matching either spelling of the same
 * file; readLog's own server-side fallback already resolves either form, so
 * this is never needed to build a request, only to compare two names. */
export function logBaseName(name: string): string {
  return name.endsWith('.gz') ? name.slice(0, -3) : name
}

/** Reads the Logs screen's `?file=` query parameter (e.g. `/logs?file=
 * render-abc123.log`) - the per-job "View log" links preselect a file this
 * way, since the router's `parseRoute` (scopedApi.ts) reads only
 * `window.location.pathname` and never the query string. Returns null when
 * absent or blank. */
export function initialLogFileFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get('file')
  return value && value.trim() !== '' ? value : null
}

/** A sensible filename for a client-side download of a log's lines. `GET
 * /api/logs/<name>` returns a JSON body ({"lines": [...], ...}), not text -
 * so the "Download" link cannot just point at it (that used to hand the
 * operator a JSON blob instead of a log). Downloading is therefore built
 * client-side from the lines already fetched (see LogsScreen's
 * downloadLogText below), which needs no new backend route or traversal
 * guard - and needs its own filename, since there is no server response to
 * take one from. Strips a trailing `.gz` (there is nothing left to gzip -
 * the content is already decompressed text) and appends `.txt` so the
 * browser's default file-type association is plain text. An explicit
 * archive date is inserted before the extension so downloading a chosen
 * date never collides with the live log's own filename. */
export function downloadFilename(name: string, date: string | null): string {
  const base = logBaseName(name)
  return `${date ? `${base}.${date}` : base}.txt`
}

/** Joins the currently-loaded lines into one text blob, newline-terminated
 * (like the file on disk) when non-empty. Pure so it is unit-testable
 * without a real Blob/anchor-click, which is exercised only by LogsScreen
 * itself, at the DOM boundary. */
export function downloadLogText(lines: string[]): string {
  return lines.length === 0 ? '' : `${lines.join('\n')}\n`
}
