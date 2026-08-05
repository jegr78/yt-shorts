---
name: logging-and-observability
description: logsetup.py - why it is stdlib-only, where logs live, the best-effort guarantee, the five places external-tool text is elided before it is written, per-job loggers, the read-only log routes and their traversal guards. Read BEFORE touching logsetup.py or adding any log line that quotes a subprocess.
---

# Logging and observability

Moved here VERBATIM out of the repository-root `CLAUDE.md`. The root file keeps
the secrets-never-reach-a-log prohibition and points here.

**Logging & observability is `logsetup.py`, stdlib-only by design.** Same rule
as `pathnames.py` and `upload_policy.py`: it must never import from this
project or from FastAPI/google, because the CLI configures logging in a venv
that may never have installed either (see "Commands" above on
`faster-whisper` being the one thing that IS required; FastAPI and the google
libraries are not). A future change that makes `logsetup.py` import anything
project-shaped breaks the CLI in exactly the minimal venv this file already
warns about.

Logs live in `<workspace>/logs/` - workspace data, a sibling of `auth/`, not
repository data - with two shapes: a central `yt-shorts.log` the CLI and the
studio both write into (daily midnight rotation to `yt-shorts.log.YYYY-MM-DD`,
then gzipped), and one gzipped log per background job under `logs/jobs/`
(`<kind>-<job id>.log[.gz]`, e.g. `detect-<id>.log`). `prune_old_logs` (30-day
default) is the ONLY deletion authority in the whole subsystem; every handler
is built with `backupCount=0` specifically so the stock `TimedRotatingFileHandler`
never deletes anything on its own and the two paths can never disagree about
what should still exist.

**Logging is best-effort everywhere**, the same guarantee "One failed clip
must never abort a run" already makes for rendering: a failed log write, a
failed rotation or a failed gzip compression warns and carries on. It must
never be the reason a job or a render dies. `_ResilientTimedRotatingFileHandler`
exists specifically to keep a mid-rollover failure from dropping the very
record that triggered it - it re-opens the base file and pushes the next
rollover attempt forward rather than re-raising.

**`pump_subprocess`, `LineThrottle`, `shorten_urls` and friends are
deliberate, tested groundwork, not yet wired into the pipeline.**
`pump_subprocess`, `shorten_urls`, `LineThrottle`, `classify_subproc_line`,
`tag_line` and `normalize_for_dedup` live in `logsetup.py`, fully covered by
`tests/test_logsetup.py`, for a future path that pumps a subprocess's own
stdout/stderr (yt-dlp, ffmpeg) line-by-line into a job's log with throttling
and URL-shortening applied uniformly. Nothing in `stream_transcribe.py`,
`harvest.py` or `render.py` calls `pump_subprocess` yet - this is the same
status `timecode.with_padding` and `render.Source`'s `--download-sections`
path already have (see Stage 2 above): tested groundwork for a later change,
not dead code, do not delete it because nothing calls it today.

**Secrets never reach a log, in the THREE places external-tool text is
actually written today - it was two until the queue added the third.**
`stream_transcribe`'s per-chunk decode-failure
warning, `studio.jobs.Job.record`'s exception `reason` (surfaced when,
for example, yt-dlp raises with a stalled/expired-manifest googlevideo URL
in the message) and `studio.worker.Worker._fail`'s entry reason all run the
message through `logsetup.shorten_urls` before
it is written, which elides a long URL's path and query (where yt-dlp's
googlevideo `sig`/`lsig` tokens live) down to a host-only form, keeping only
the `itag` for diagnostics - see `tests/test_logging_secrets.py` for the
pinned proof that a signed download URL never survives into any of those log
lines. `Worker._fail` is the one that needed a fourth site to go with it: its
reason is not only logged, it is PERSISTED on the queue entry in `jobs.json`
and served to a browser, so `api._entry_json` elides the same field again on
the way out - which is what covers a `jobs.json` this process never wrote
(`test_the_jobs_route_never_serves_a_signed_url_in_an_entry_reason`).
Centralising it inside `_fail` rather than at each `except` that calls it is
deliberate: every path into that method is then covered by construction
rather than by remembering to wrap it. The workspace's `auth/` directory (`client_secret.json`,
`token-<id>.json`, `quota.json`) is the one place a real secret lives, and
it is a SIBLING of `logs/`, never inside it - so even a route that resolved
every name under `logs/` could not reach a token by construction, which
`resolve_archive`'s traversal guard (below) still enforces defensively
anyway.

**A per-job logger must not propagate into the central log.** Every job
logger built by `_open_job_log` sets `propagate = False`, so a one-hour
stream transcription's per-chunk narrative lands only in that job's own file,
never flooding `yt-shorts.log` with content an operator reading the central
log has no reason to wade through.

**The two silent-failure gaps this subsystem closes:** `stream_transcribe`
no longer swallows a chunk decode failure - it logs the cause and still
records the chunk's index, so a transcript with a hole in it says why rather
than just stopping short with no trace. `detect.detect_moments` reports LOUDLY
(logged, not just an empty result) when a stream's transcript comes back with
0 words, naming the failed-chunk count, and its own final log line always
reports the words/moments/engine/failed-window counts - a run that silently
produced nothing used to look identical to a run that legitimately found no
moments. Both route their diagnosis through an INJECTED `logger`
(`detect_moments`'s own `logger` kwarg) so a studio-started detect job's
per-chunk and per-window narrative lands in that job's own log file, not
only wherever the process's central log happens to be.

**The studio's log routes are read-only and resolve strictly inside
`<workspace>/logs/`.** `GET /api/logs` lists the central log, its archive
dates and every job log; `GET /api/logs/{name}` and `GET /api/jobs/{id}/log`
serve content. Every name arrives from an HTTP path or query, so
`logsetup.resolve_archive` (the archive case) and `api.py`'s own `_resolve_log`
(the plain and job-log cases) both realpath the candidate and require it stay
under the realpath of `logs/` via `os.path.commonpath` - a symlink planted
inside `logs/` pointing out at `auth/` is refused the same as a literal `../`
would be. These routes are registered BEFORE the SPA fallback (`GET
/{full_path}`), like every other `/api` route (see "The studio app is
workspace-level" above) - the fallback would otherwise happily serve
`index.html` for a near-miss path instead of ever reaching the log route or
its 404.

**Test isolation for the studio's workspace resolution has a sharp edge that
has already cost a Critical once.** `create_app()` resolves the workspace at
construction time via `workspace.resolve()`, but `studio/api.py` and
`studio/jobs.py` each do `from ..workspace import resolve as _resolve_workspace`
- a from-import copies the function object into that module's own namespace,
so patching the attribute on the `workspace` module afterwards does not reach
either already-bound name. `tests/conftest.py`'s autouse
`_isolated_resolved_workspace` fixture patches all THREE names
(`workspace.resolve`, `studio.api._resolve_workspace`,
`studio.jobs._resolve_workspace`) to the same fixed, session-scoped root -
missing any one of them leaves that module still resolving through the real
`YT_SHORTS_DATA`/`~/YT-Shorts-Data`/repository chain. Do not "simplify" this
by setting `YT_SHORTS_DATA` for isolation instead: the workspace switch/create
routes deliberately 409 while `workspace.resolve().origin == "YT_SHORTS_DATA"`
(re-rooting is meaningless while an env var pins the root), so pinning it
session-wide would trip a guard several tests exist specifically to exercise.

**The same hazard from the other direction: a from-import of an EXCEPTION
class is a trap here, and it has already cost one silent defect.** An
`except SomeError` clause holds the class object the module was handed at
import time, while the raising module resolves that name from its own globals
at call time - and `importlib.reload`, which `tests/test_profile.py` performs,
mints a NEW class object in place. `studio/worker.py` bound `ProfileError` by
name, so after that reload the `isinstance` check behind its `except` no longer
matched what `profile.load` raised: the handler missed, the exception escaped
`_start`, and it killed the worker's whole pass - a failure that depends on
test ORDER and therefore looks like flakiness rather than a bug. So `lock`,
`profile`, `cancel` and `job_queue` are imported as MODULES there and the
classes are looked up as `profile.ProfileError`, `lock.LockError`,
`cancel.Stopped`, `job_queue.QueueError`, at `except`-evaluation time. Two of
those would have been worse than the one that was caught: a missed
`lock.LockError` would fall through to the blanket handler and FAIL an entry
whose event lock is merely held (violating the queue's first rule with no noise
at all), and a missed `cancel.Stopped` would relabel an operator's own stop a
failure. Catch through the module, or import inside the function.
