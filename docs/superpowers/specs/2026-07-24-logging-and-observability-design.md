# Logging & observability (rotating, compressed, studio-viewable) — design

Date: 2026-07-24
Status: approved (design), ready for implementation plan

## Motivation

The project has effectively no logging: a single `logging.getLogger(__name__)`
in `studio/api.py`, no workspace log directory, and background jobs capture no
output. When a background job fails, the operator gets no diagnostic.

This was discovered concretely: a moment-detection run on stream
`V9nVNEQNdR4` produced zero moments. Investigation found the stream's
`transcript.json` had **0 words** — all 10 chunks were recorded in
`missing_chunks` and the `chunks/` directory was empty. The decode pipeline
itself works (a mid-stream 60 s segment decodes cleanly to 74 words incl.
racing speech), so the failure was transient — but `stream_transcribe`'s
per-chunk handler is `except Exception: missing.append(index); continue`,
which **silently swallowed** the real cause. The detect job then wrote 0
moments as if nothing exciting had happened. Nothing was recorded anywhere,
so the actual reason is unrecoverable.

The operator wants a complete logging capability: viewable in the studio,
cleanly rotated and compressed, modeled on the racecast broadcast project
(`/Users/jegr/Documents/github/gt-endurance-racing-broadcast`,
`src/scripts/logsetup.py`).

This is Feature 1 of two. Feature 2 (the additive moments/keyword lexicon UI
— central Settings → per-channel → per-event, additive not override, with a
racing default) is a **separate** spec that follows this one; it is out of
scope here. Logging goes first because it is the debugging foundation and it
unblocks re-running the detection that surfaced the lexicon gap.

## Decided requirements

- **A complete logging subsystem**, not a targeted patch — viewable in the
  studio, daily-rotated, gzip-compressed archives, age-pruned.
- **Log-file model: central + per-job.** One central app log
  (`yt-shorts.log`) for the studio server and short operations, PLUS a
  dedicated log file per heavy background job (detect / render / upload /
  transcribe), so a long (~1 h) transcription does not bloat the central log
  and is debuggable in isolation. The studio job panel links to its job log.
- **Location:** `<workspace>/logs/` (workspace-resolved, gitignored, alongside
  `auth/` and `streams/`), so the studio can read it and it is per-operator.
- **Retention:** 30 days (a batch tool may keep longer than racecast's 7).
- **Studio viewer:** its own workspace-level screen (like Settings), plus a
  per-job link.
- **Racecast as the template:** port `logsetup.py`'s proven pieces (rotation,
  resilient rollover, prune, tail, log-surface helpers with traversal guards,
  subprocess pump + classification + throttle + URL shortening). ADD gzip
  compression, which racecast does not have.
- **Secrets never appear in logs** — the existing invariant extends to this
  new surface.

## Architecture

### New module: `yt_shorts/logsetup.py` (stdlib-only)

Ported and adapted from racecast's `src/scripts/logsetup.py`. Stdlib-only by
design (no project imports, like `pathnames.py`/`upload_policy.py`), so the CLI
can configure logging in a venv that never installed FastAPI/google.

- `configure_logging(name, log_path, *, level=INFO, to_stdout=None) -> Logger`
  — a `Logger` writing timestamped, leveled lines
  (`%(asctime)s %(levelname)s %(message)s`, `%Y-%m-%d %H:%M:%S`) to `log_path`
  via a resilient `TimedRotatingFileHandler` (daily midnight rollover, archive
  suffix `.YYYY-MM-DD`, `backupCount=0` so the handler never deletes —
  `prune_old_logs` is the sole deletion authority). A stdout `StreamHandler` is
  added only on a TTY (or when `to_stdout=True`), so a CLI run echoes to the
  console but a server whose stdout is redirected does not double-write.
  Idempotent per `name` (a `_ytshorts` handler marker, like racecast's
  `_racecast`).
- `_ResilientTimedRotatingFileHandler` — racecast's rollover-never-drops-a-line
  handler, carried over verbatim in spirit (a failed rollover re-opens the base
  file and defers the next attempt). POSIX-only here, but kept for correctness
  and parity.
- **`close_logging(name)`** — detach/close this module's handlers (tests
  pointing a logger at a `TemporaryDirectory` call it before teardown).
- **gzip compression (NEW vs racecast).** On daily rollover the just-closed
  archive `yt-shorts.<date>` is gzipped to `yt-shorts.<date>.gz` and the plain
  archive removed, via the handler's `rotator`/`namer` hooks (or a
  `_compress(path)` invoked from `doRollover`). A per-job log is gzipped once,
  when the job finishes (it is immutable after), by an explicit
  `compress_file(path) -> gz_path`. Compression is best-effort: a failure logs
  a warning and leaves the plain file (never loses the log).
- `prune_old_logs(log_dir, keep_days=30, now_ts=None) -> list[str]` — delete
  files whose mtime is older than `keep_days` (covers `yt-shorts.log`
  rotations, `.gz` archives, and everything under `jobs/`). Best-effort;
  injectable clock; the ONLY deletion path. Recurses into `jobs/`.
- Log-surface helpers for the viewer: `list_logs(dir)` (regular files,
  newest-first), `newest_log(dir)`, `read_new_lines(path, pos) ->
  (lines, new_pos)` (re-opens each call, handles rotation/truncation, holds a
  partial trailing line), `archive_dates(dir, basenames)`, and
  `resolve_archive(dir, basename, date)` — traversal-guarded realpath (date is
  exactly `YYYY-MM-DD`, basename carries no separators, resolved path stays
  inside `dir`). These back the studio routes.
- Subprocess pump: `pump_subprocess(stream, logger, tag, on_line=None)`,
  `classify_subproc_line(line) -> level`, `LineThrottle` (dedup consecutive
  normalized-equal lines + rate-limit per window, with periodic
  `(last line repeated ×N)` / `(suppressed N lines)` summaries), and
  `shorten_urls(text)` / `normalize_for_dedup(text)`. This turns a chunk
  decode's or yt-dlp's stderr into classified, throttled, secret-safe log
  lines. `shorten_urls` elides signed-URL query strings, which is exactly
  where yt-dlp's googlevideo tokens live.

### Workspace layout

```
<workspace>/logs/
  yt-shorts.log              # central app log (server + CLI + short ops)
  yt-shorts.2026-07-24.gz    # gzipped daily archives
  jobs/
    detect-<jobid>.log       # per heavy background job; → .log.gz on completion
    render-<jobid>.log
    upload-<jobid>.log
    transcribe-<jobid>.log
```

- `workspace.py` gains a `logs_dir(workspace)` resolver (mirrors how `auth/`
  and `streams/` are located) so both the CLI and the studio agree on the path.
  `logs/` is created on demand.
- `.gitignore` already ignores workspace data; confirm `logs/` is covered (add
  an explicit entry if the workspace lives inside the repo's `channels/`
  fallback).

### Integration points

- **`yt_shorts/studio/jobs.py`** — the `Job` dataclass gains a `log_path` (and
  the studio exposes it). The job runner opens a per-job logger via
  `configure_logging(f"ytshorts.job.{id}", jobs_dir/f"{kind}-{id}.log")` before
  the background thread starts, logs start / key steps / a result summary /
  any exception, and calls `compress_file` on the job log when the job reaches
  a terminal state. Each job also writes one summary line to the central log.
- **`stream_transcribe.py`** — the swallowing handler
  (`except Exception: missing.append(index); continue`) now logs the real
  exception at WARNING with the chunk index before recording it missing. The
  long-running decode/extract subprocess calls stream their stderr through
  `pump_subprocess` (Popen) instead of a silent `capture_output` run, so a
  decode failure's cause is visible. The assembled result logs a summary
  (`decoded N/M chunks, K words, missing=[…]`).
- **`detect.py`** — logs `words=N, candidates=M, written=K`; if the transcript
  has 0 words or any missing chunks, it logs a clear WARNING and the studio
  detect job surfaces it (a `"detected 0 moments — transcript empty (N/M
  chunks failed); see the job log"` message rather than a silent empty
  success).
- **`harvest.py`** and **`render.py`** — their yt-dlp / ffmpeg calls log
  through the pump for long-running ones; short `run(capture_output=True)`
  calls log captured stderr on failure at ERROR. (The per-clip / per-entry
  failure isolation already in place is unchanged — logging is additive to it.)
- **CLI (`bin/yt-shorts`)** — configures the central logger at startup
  (`to_stdout` on a TTY), so every command writes to `yt-shorts.log` and still
  prints to the console. Backend route changes still need a studio restart
  (unchanged); this note stays in CLAUDE.md.
- **`studio/api.py`** — replaces the lone bare `getLogger` with the configured
  central logger at app creation; request/job lifecycle logs there.

### Studio log viewer (workspace-level)

Routes (thin, over `logsetup` + `workspace.logs_dir`):
- `GET /api/logs` — list log files (central + `jobs/`) newest-first, plus
  `archive_dates` for the central log. Booleans/paths/dates only.
- `GET /api/logs/{name}` — the log's current content or a tail
  (`?after=<pos>` using `read_new_lines` for live-tail); `?date=YYYY-MM-DD`
  serves a rotated archive resolved via `resolve_archive` (404 on a bad/escaping
  name or date — the traversal guard). A `.gz` archive is decompressed for
  viewing server-side.
- `GET /api/jobs/{id}/log` — the job's own log (current or gzipped), same
  guarding.
- The `{name}` and any date go through `resolve_archive` / a safe-segment check
  before any filesystem touch, exactly like every other studio write/read op.

Frontend: a new top-level **Logs** screen (client-side route, like Settings),
listing logs with a viewer pane that live-tails the active file (polling
`?after=`), lets the operator pick a rotated date, and download an archive.
Each job panel (render/detect/upload) gets a "View log" link to its job log.
Pure logic (tail-merge, byte-offset tracking, date formatting) lives in its own
`.ts` module, Vitest-tested, like the other studio screens.

### Security / correctness

- **No secrets in logs.** `client_secret.json`, `token-<id>.json`,
  `quota.json` contents are never logged; the upload path logs only the video
  id/url and resulting privacy status (unchanged). `shorten_urls` strips
  signed-URL query strings (yt-dlp googlevideo sig/lsig tokens). A test asserts
  a representative signed URL is elided and that no auth-file content reaches a
  log. The log routes never serve anything outside `<workspace>/logs/`
  (traversal-guarded).
- **Logging never breaks a run.** The pump and throttle swallow their own
  errors (best-effort, like racecast); a logging or compression failure warns
  and continues, never aborts a job or a render. This preserves the "one failed
  clip must never abort a run" invariant.
- **The unbounded-decode invariant is unchanged** — logging observes the decode,
  it does not add or change a timeout (`stream_transcribe`'s per-chunk
  subprocess timeout stays as-is; the clip-path decode stays untimed per the
  documented decision).

## Testing

- **`logsetup` (stdlib, ported + adapted):** timestamped line written; no stdout
  handler off-TTY; idempotent (no duplicate handlers); rollover survives a
  failed rename with no line lost; `prune_old_logs` deletes only aged files and
  recurses into `jobs/`; `read_new_lines` handles append / rotation / partial
  line; `resolve_archive` rejects traversal / bad date / cross-dir; the pump
  classifies + throttles + shortens; `LineThrottle` dedup/rate-limit summaries.
- **gzip (new):** a rolled archive is gzipped and the plain file removed; a
  finished job log is gzipped; a compression failure leaves the plain file and
  warns (best-effort).
- **workspace:** `logs_dir` resolves under the same base as `auth/`/`streams/`;
  created on demand; identical whether `~/YT-Shorts-Data` exists or not (the
  suite's `conftest` fixture discipline).
- **Integration (no network/model):** a stubbed failing chunk decode makes
  `stream_transcribe` log the real cause and record the chunk missing (not
  swallow it); `detect` logs the `words/candidates/written` summary and warns on
  an empty transcript; a background `Job` gets a `log_path`, writes start/result,
  and is gzipped on completion — all with the existing injected seams
  (`transcriber`/`decoder`/`measure_loudness`/`detect_fn`), no real model or
  ffmpeg.
- **Studio API:** `GET /api/logs` lists; `GET /api/logs/{name}` tails and serves
  an archive by date; a traversal / bad-date / cross-dir name is refused (404);
  `GET /api/jobs/{id}/log` returns the job log; no secret is ever served. With
  the FastAPI TestClient, as today.
- **Vitest:** the pure tail/offset/format helpers of the Logs screen.
- **Playwright E2E:** open the Logs screen, assert the central log lists and
  its content renders; run a stubbed detect job, follow its "View log" link,
  assert the job log shows the run summary.
- Full pytest suite green, `npm test` green, `python3 tools/lint.py` green,
  `npm run build` committed (`static/`).

## Out of scope (explicitly)

- **The moments/keyword lexicon UI** (Feature 2) — the additive
  Settings→channel→event lexicon with a racing default. Separate spec next.
- **Re-running the actual V9nVNEQNdR4 transcription** — the operator triggers
  that once logging makes the outcome visible (the model is now cached; a full
  ~1 h run is theirs to start).
- **A live WebSocket log stream** — polling `?after=<pos>` tailing is enough;
  no new transport dependency.
- **App-wide DEBUG-level instrumentation of every function** — logging attaches
  at the operation and subprocess boundaries that matter (jobs, CLI commands,
  yt-dlp/ffmpeg/decode), not every internal call.
- **Log shipping / external aggregation** (syslog, cloud) — local files only.
- **Changing any pipeline timeout or the unbounded-decode decision.**

## Notable risks / decisions carried forward

- **The silent `except Exception` in `stream_transcribe` is the root cause the
  operator hit** — making it log the real error is the single highest-value
  change and must land exactly (a swallow that only appends to `missing` is
  what this feature exists to end; the lint suite's empty-except guard already
  polices this class elsewhere).
- **Secrets discipline is the highest-risk item** — yt-dlp URLs and OAuth
  material must never reach a log; the URL-shortening elision and an explicit
  no-secret test are the guardrail, reviewed as such.
- **Best-effort logging** — every logging/compression path fails safe (warn +
  continue) so it can never abort a render/job; this is a hard constraint, not
  a nicety.
- **Central + per-job** keeps the central log readable while isolating a long
  transcription's voluminous output; the trade-off is two log surfaces the
  viewer must present coherently (the job link solves this).
