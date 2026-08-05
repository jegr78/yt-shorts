# A job queue for the studio: schedule, run, pause, stop

**Date:** 2026-07-31
**Scope:** turning the studio's fire-and-forget background jobs into a managed
queue with a limit on what runs at once, a persisted plan that survives a
restart, cooperative stopping, and a Jobs screen. Splitting stream
transcription out of moment detection into a job of its own, because the
resource model does not work otherwise.

## Problem

Transcribing an eight-hour endurance stream takes over two hours on this
machine — measured 2026-07-31 on `Esm9vv5-PdU` (8 h 19 min): about 2.5 minutes
per 10-minute chunk, 50 chunks. The note in `CLAUDE.md` claiming "~1h of
Whisper decode for an 8h race" does not hold here and should be re-measured.

At that length the studio's current model breaks down in four ways:

1. **A job cannot be stopped.** `studio.jobs._spawn` starts a daemon thread and
   nothing can ask it to stop. A two-hour transcription started by mistake runs
   to the end or the operator kills the studio.
2. **There is no list.** `GET /api/jobs/{id}` needs an id you already have.
   Nothing shows what is running, and nothing shows a queue because there is no
   queue — every start runs immediately, however many are already going.
3. **Nothing survives a restart.** `JobStore` is an in-memory dict, bounded at
   `MAX_JOBS = 200`, evicting terminal jobs. A restart loses the plan.
4. **Nothing bounds concurrency by cost.** Two transcriptions started at once
   are not twice as fast; they are each half as fast and fight for memory.
   Meanwhile two detections could happily run together — they wait on a network.

The operator's own words: several concurrent jobs that can be scheduled,
started, paused and stopped, from a Jobs page in the studio.

## Decisions taken

Each closes a question with a defensible opposite answer:

1. **"Schedule" means a queue, not a clock.** Jobs are appended, run in order,
   bounded by how many may run at once. No wall-clock start times, no recurring
   jobs. The studio is a local tool the operator launches — it is not a service,
   so a clock would only work while the window happens to be open.
2. **The queue is persisted; interrupted jobs never restart themselves.** The
   pending plan lives in a file and comes back after a restart. A job that was
   running when the studio died shows as `interrupted` with a button. It does
   **not** resume automatically, because a detection run spends real money and a
   job that quietly starts on launch and bills $0.80 is a bad surprise.
3. **Stopping is cooperative by default, with a hard stop only where it is
   provably safe.** The default is a clean halt at the next safe point, with the
   UI naming that point in advance. A "cancel now" exists for the kinds where it
   cannot damage anything, and does not exist at all for upload.

## The finding that shapes the design

**Moment detection currently transcribes.** `detect.detect_moments` calls
`transcribe_stream` itself. So a detect job on an eight-hour stream is
CPU-bound for two hours and then network-bound for five minutes — which makes
any single resource classification of that job wrong.

**Stream transcription therefore becomes a job kind of its own.** That is also
what the operator wants: queue five streams to transcribe overnight without
ordering five paid detections at the same time. The seam already exists —
`detect_moments` takes its `transcriber` as a parameter, so a detect job can be
given one that requires a cached transcript and refuses otherwise.

## Architecture

### Modules

| File | Responsibility |
|---|---|
| `src/yt_shorts/job_queue.py` | the queue, the pools, the state file, the transitions. Pure — no FastAPI, like `brand_admin`/`upload_policy` |
| `src/yt_shorts/studio/jobs.py` | unchanged in kind; gains a per-kind descriptor and threads a `CancelToken` through |
| `src/yt_shorts/studio/api.py` | thin routes over `job_queue`, as for every other admin module |
| `src/yt_shorts/studio/web/src/jobs.ts` | pure display/permission logic for the screen |

`job_queue.py` must stay importable in a venv that never installed FastAPI, the
same rule `subtitle_pipeline.py` and `upload_policy.py` follow.

### Two pools, not one number

Job kinds load different resources, so a single "max N jobs" is the wrong dial:

- **cpu** — `transcribe`, `render`, `trim`. Default limit **1**.
- **net** — `detect`, `upload`. Default limit **3**.

Both configurable at workspace level (not per channel — the queue belongs to the
workspace). The effect worth having: stream A can be detected while stream B is
still transcribing, and two transcriptions never collide.

**`connect` is never queued.** It opens the operator's browser and waits for a
consent they must give in person; queueing it behind a two-hour transcription
is meaningless. It keeps its current immediate path and its existing duplicate
guard (`JobStore.begin_connect`).

`copy` (workspace copy) is **io** and runs outside both pools, unbounded as
today — it is neither CPU- nor network-bound and has its own semantics.

**Amended after implementation (2026-08-01).** `copy` turned out not to be
queueable at all, for the same reason `connect` is not: `start_copy_job` takes
an `on_done` callback that re-roots the LIVE app onto the copy, and no state
file can hold a callback. It keeps its own route (`POST /api/workspaces/copy`),
so the `io` pool is unreachable through the queue in production. The
unlimited-pool rule this paragraph describes is unchanged and still pinned
(`test_io_runs_outside_both_pools` builds its own kinds table to exercise it);
what is gone is the shipped kind that used it. `api._queue_pools` derives the
settable pools from the queueable kinds for the same reason — a limit for `io`
would be a control that does nothing.

### The worker

One thread pulls the head of the queue whose pool has a free slot, and calls the
existing `start_*_job` function. It does not reimplement any job; every job
keeps its current body, its logging, its `EventLock` acquisition and its URL
redaction.

### Persistence

`<workspace>/jobs.json` — workspace data, a sibling of `logs/` and `auth/`,
never in the repository, added to `.gitignore` the way those are.

It holds pending entries with their parameters, and the last **50** finished
ones (the screen shows a recent history; the durable record is each job's own
log under `logs/jobs/`, which `prune_old_logs` already ages out — this file must
not become a second, unpruned copy of it). It **never holds an API key**: a
detect entry stores channel, event and video id, and the key is read from
`auth/` when the job starts, exactly as today.

Writes go through the same write-aside-then-`os.replace` mechanic
`providers.save_api_key` and `render.compose` use, so a crash mid-write cannot
leave a half-parsed queue. A `jobs.json` that cannot be parsed is renamed aside
and reported, never silently discarded — losing a plan quietly is worse than
starting empty loudly.

### States

`queued` → `running` → `done` / `failed` / `stopped`

plus `paused` (keeps its place in the queue), `stopping` (asked to stop, not yet
at its safe point), and `interrupted` (was running when the process died —
resumable by an explicit click, never automatically).

### One dependency edge, and only one

Ordering a detection for a stream with no transcript enqueues **two** entries:
a `transcribe`, and a `detect` that waits for it. That is the only dependency
this design has — not a general graph. A general dependency system is where
queue designs go to become unmaintainable, and nothing else here needs one.

## Stopping

### The safety property everything rests on

**A hard stop never kills the Python thread — only the subprocess it is waiting
on.** The thread then sees a non-zero exit, runs its own cleanup (the `finally`
blocks that, for example, remove `trim.py`'s scratch file), and reports
`stopped`. This is why a cancel cannot leave a half-written artifact: the code
that prevents that still runs.

Python threads are not killable, so there is no other honest option — and this
one is better than a kill would be.

### The mechanism

A `CancelToken` with two levels (`stop_requested`, `kill_requested`), passed
into the work as a parameter — the same injection style as `runner`, `logger`,
`on_note` and `caller` elsewhere in this project. No global state, and testable
with a fake job that counts its checkpoints.

### Per kind

| Kind | Checks between | What a stop costs | Hard stop |
|---|---|---|---|
| `transcribe` | chunks | **nothing** — finished chunks stay cached | yes |
| `detect` | windows | **nothing** — scored windows stay cached | yes (the in-flight window's tokens are still billed) |
| `render` | clips | the clip in flight; the existing short is untouched | yes |
| `trim` | — | the cut in flight; the master is untouched | yes |
| `upload` | — | — | **no** |

`upload` has no stop at any level. A half-finished upload to YouTube is worse
than waiting, and the operator is told so rather than offered a button that
lies.

### Stopping a detection costs nothing, because its windows are cached

An earlier draft of this spec said the opposite: that stopping after window 3
of 9 discards three paid windows, and that the UI must warn before the click.
That was a workaround for a constraint this project does not actually have.
"`moment_scan.py` must not change" was the model-provider branch's *proof* that
the caller seam carried three vendors without widening — it is not a standing
prohibition, and the operator said so plainly.

A warning is the consolation prize. Not losing the windows is the fix.

`moment_scan.scan` gains two additive, injected seams — the style `runner`,
`logger`, `on_note` and `caller` already use here:

- **`should_stop: Callable[[], bool] | None`**, checked at the top of the window
  loop, **outside** the `try`. This placement is the whole point: `scan` catches
  `Exception` per window and records it in `missing_windows`, so a stop signalled
  as an exception from inside the injected `caller` would be swallowed and the
  scan would carry on. Measured, not assumed — see `scan`'s loop.
- **`window_cache`**, an object with `get(index)` and `put(index, moments)`. A
  hit skips the API call entirely.

**`moment_scan.py` stays filesystem-free.** The disk-backed cache is supplied by
`detect.py` and lives at `streams/<video_id>/windows/`, exactly symmetric with
the chunk cache at `streams/<video_id>/chunks/` that already makes transcription
free to stop and resume.

What this removes: the pre-click cost warning, the special case that a detect
retry pays for everything again, and an asymmetry between job kinds that existed
only because one of them had a cache and the other did not.

**What protects the change:** `tests/test_moment_scan.py` is 43 tests across
seven classes (line grouping, window splitting, rendering, validation, merging,
prompt building, and `scan` itself). Both new parameters default to `None`, so
every existing caller and every existing test exercises the unchanged path.

### `stopping` is a real state, not a cosmetic one

After the click the job shows `stopping`, not `stopped`. That is the honest
intermediate state and it prevents the obvious misuse — clicking again because
nothing appears to happen.

**A job that never reaches its safe point keeps its slot.** The queue must not
declare it finished and hand the slot to the next job: the thread is still
running and would fight the newcomer for the same CPU. A visibly stuck job is
better than a queue that silently oversubscribes.

## Routes

`GET /api/jobs/{id}` and `…/log` are unchanged. New:

| Route | Purpose |
|---|---|
| `GET /api/jobs` | the listing that does not exist today: queued, running, recently finished |
| `POST /api/jobs` | enqueue (kind plus parameters) |
| `POST /api/jobs/{id}/stop` | graceful; `?force=true` only where the kind allows it, else 409 |
| `POST /api/jobs/{id}/pause`, `…/resume` | keeps its place in the queue |
| `POST /api/jobs/{id}/move` | reorder — "schedule" without reordering is half a feature |
| `DELETE /api/jobs/{id}` | remove a **queued** job; 409 on a running one, pointing at `stop` |
| `POST /api/jobs/{id}/retry` | for `interrupted` and `failed` |

`retry` re-enqueues the same entry; it does not resume from a saved position,
because the queue holds no such position — and it does not need one. Both long
kinds are cheap to retry for the same reason: a `transcribe` restarts at the
first missing chunk and a `detect` at the first unscored window, because each
caches its own unit of work. That symmetry is deliberate, and it is why the UI
needs no per-kind warning about what a retry will cost.

Two different things (dropping a plan, halting work) get two different buttons.
The pool limits join the workspace-level settings.

## The Jobs screen

The router's seventh screen, at workspace level beside `/settings` and `/logs` —
not in the channel/event chain, because a queue belongs to the workspace.

Three sections: **running** (progress, safe stop point, the buttons),
**queued** (reorderable, removable), **recently finished** (with a jump into the
job log). Pure logic — state labels, progress text, which button is legal when —
lives in `jobs.ts`, not in the component, so Vite's fast-refresh boundary stays
component-only and the rules are unit-tested without rendering.

**Progress is what makes this screen worth having.** "Running" on a two-hour job
is useless. Every kind reports `{done, total, unit}`: chunk 20 of 50, window 3
of 9, clip 2 of 6. The channel already exists — `moment_scan.scan` takes a
`progress` callback today.

**Scrolling is a mandatory acceptance criterion in this project.** Every row and
control must be reachable at a short viewport, verified by driving a real mouse
wheel — `scroll_into_view_if_needed()` was proven here to pass on a broken build.

## Testing

`job_queue.py` is pure, so ordering, pool limits, state transitions and
restart recovery are unit-tested with no server and no real work. Cancellation
is tested with a fake job that counts its checkpoints, which is what makes
"the stop took effect at the intended point" checkable rather than assumed.

No test transcribes, renders, uploads, reaches the network, reads a real API
key or spends money.

## What deliberately does not change

- **`moment_scan.py`'s scoring, validation and merging.** The file gains two
  optional parameters and nothing else; `validate_moment`'s never-raises
  contract, the line-number protocol and `MAX_PER_WINDOW` are untouched.
- **`EventLock` stays, and the pools do not replace it.** They are different
  mechanisms: the pools bound load, the lock stops two runs from tearing up one
  event. Two different events may run at once; the same event never may.
- **The CLI gets no queue.** `bin/yt-shorts` is a different process and keeps
  starting work directly. That a CLI render and a studio job cannot collide is
  already `EventLock`'s job — the incident it was built for.
- Job logs, the `shorten_urls` redaction in `Job.record`, and the studio
  boundary rules.

## Out of scope

- Wall-clock and recurring schedules (decision 1).
- Automatic resume of interrupted jobs (decision 2).
- Reworking how `scan` scores or merges — the two new parameters are additive
  seams, not a rewrite of its logic.
- Running jobs while the studio is closed. That is a service, and this is a
  local tool.

## Risks

- **A job kind that ignores its `CancelToken`** stalls its pool. Mitigated by
  keeping it visible and refusing to reuse the slot, not by pretending it
  stopped.
- **The two-pool default may be wrong on other hardware.** 1 CPU job is right
  for this machine; both limits are configurable, and the defaults are a
  starting point rather than a measurement.
- **Splitting `transcribe` out of `detect` changes an existing path.** The
  detect job stops transcribing implicitly, which is a behaviour change for
  anyone who relied on ordering a detection and getting a transcript for free.
  The paired-enqueue keeps that convenience, but the CLI's `detect` must decide
  the same question explicitly.
