# Shared job state: one event stream instead of per-tab polling

## Why

Starting a render spawns a background thread and hands the client a job id.
The client then polls `GET /api/jobs/{job_id}` every 750ms
(`hooks/useJobPolling.ts`) until the job leaves `running`, and `App.tsx`
refreshes the clip list and the open clip when it does. That works — for the
one tab that started the job.

Three gaps follow from the job id living in React state and `JobStore` being
readable only by id:

- **A reload forgets a running render.** The id is gone, nothing polls, the UI
  shows an idle studio while a render is in fact running, and when it finishes
  nothing refreshes.
- **Every other tab is blind.** Same reason.
- **There is no way to ask what is running.** `GET /api/jobs/{job_id}` is the
  only read; a job whose id you do not have is unreachable.

## What this does NOT fix, stated up front

A render started by the CLI is a different process. `JobStore` never sees it,
and no pub/sub inside the studio can — whatever transport is chosen. That case
is covered separately by the focus-refetch in
`2026-07-26-stale-short-design.md`, and the two designs are complementary
rather than alternatives.

## Decisions

1. **The stream polls `JobStore` in-process; the browser stops polling
   entirely.** `GET /api/jobs/stream` is an `async def` route returning
   `text/event-stream` from an async generator that reads the store every
   `STREAM_INTERVAL` (400ms), emits an event only when the payload changed, and
   emits a heartbeat comment every `HEARTBEAT_SECONDS` (15s) otherwise.

   The alternative — a subscriber registry woken by the job threads, i.e. real
   pub/sub — is rejected deliberately. Jobs mutate from worker threads
   (`jobs._spawn`), so waking an async subscriber needs a thread-to-loop
   bridge, a registry, and leak handling on disconnect. Polling a
   lock-protected dict inside the generator needs none of it: `asyncio.sleep`
   plus a microsecond dict read. It holds no threadpool thread per connection
   (which a blocking sync generator would, out of the same pool that serves
   every other route in this app), has no registry to leak, and notices a
   disconnected client within one tick. The cost is up to 400ms of latency —
   better than the 750ms it replaces — and one wakeup per connected tab while
   idle. The wire format is unaffected if the internals are ever upgraded.

2. **`async def` is deliberate, and this is the first such route here.** Every
   other route in `studio/api.py` is a sync `def`, which Starlette runs in a
   threadpool. A streaming route must not: a long-lived sync generator occupies
   one of those threads for the entire connection, and the pool is shared with
   every other request. Async is what makes the connection cost nothing but a
   coroutine.

3. **Full state, not diffs.** Each event carries the complete set of jobs the
   connection is reporting on. A reconnecting or late-joining tab needs no
   replay logic, events are idempotent and order-independent, and
   `EventSource`'s automatic reconnect stops being a special case.

4. **The payload keeps the exact shape `getJob` returns today**, `log`
   included. That is what lets the client hook stay signature-compatible (7),
   which in turn keeps the change at the five call sites down to an import.
   Trading payload thrift for that is the right way round: the log of a render
   is a line per clip.

5. **The stream reports running jobs, plus jobs that finished while this
   connection was open.** On connect: the running ones, which is exactly what
   reload-adoption (8) needs. As a job finishes it stays in that connection's
   payload so the panel can show its results. It does NOT replay the store's
   whole history — `MAX_JOBS` is 200, and a payload proportional to a session's
   entire past would be sent 2.5 times a second. The client retains the last
   snapshot it saw for an id, so a job dropping out of the payload never blanks
   a panel.

6. **Jobs learn their scope, and what they are about.** `Job` gains optional
   `channel` and `event`, set by the `start_*` functions that already receive a
   `Profile`, and emitted by `snapshot()`. Without this a workspace-level job
   list cannot tell a tab whether a render is its own event's, and "every tab
   sees every job" would mean a tab showing another event's render as its own.
   Connect jobs are channel-only; a workspace copy job has neither. Both stay
   `null`.

   `Job` also gains an optional `target`: the video id for a detect job, the
   clip name for an upload. A tab adopting a job it did not start (8) needs to
   know what the job is *about*, not merely that it exists — the studio tracks
   exactly that today in `activeDetectVideoId` and `uploadingClipName`. The
   alternative, reading it back out of `results`, does not work: `results` is
   keyed by clip name but is EMPTY until the job records its first outcome, so
   a freshly started job would be adopted with no target at all.

7. **`useJob(jobId)` replaces `useJobPolling(jobId)` with an identical
   signature.** A provider at `Root.tsx` owns one `EventSource` and the job
   map; the hook selects from it. All four job kinds (render, detect, upload,
   connect) move at once, because the change at each of the five call sites is
   the import and nothing else — migrating one kind and leaving three on
   polling would mean two mechanisms in the tree for no gain.

   One rule the polling hook did not need: **"absent" must not mean "failed"
   until a first snapshot has arrived.** Absent before any data is *loading*
   (`null`). Absent after a snapshot has been received is the case the old hook
   met as a 404 — the studio restarted, or the job was evicted — and resolves
   to the same synthetic failed job, with the same wording it uses today, so
   the consumer's status effects still fire and the UI leaves its running
   state.

8. **Reload survival is one step on top — for render and detect only.** On the
   first snapshot, a tab holding no render job id that sees a running render job
   for its own channel/event adopts that id. The existing completion effect in
   `App.tsx` then behaves exactly as it does for a job the tab started itself.
   Detect adopts the same way, taking `activeDetectVideoId` from the job's
   `target`.

   Upload and connect do NOT adopt, and that is a decision rather than an
   omission. Both are modal, operator-initiated flows: adopting a connect would
   put a tab that never opened the dialog into "waiting for consent" for an
   OAuth flow happening in a browser window it has nothing to do with, and
   adopting an upload would surface a confirmation-driven action as though this
   tab had asked for it. Neither is something the reported problem needs. They
   still stream and display normally in the tab that started them — they simply
   are not claimed by tabs that did not.

9. **A stream that cannot reconnect must say so.** `EventSource` retries on its
   own, silently and forever. After `MAX_CONSECUTIVE_ERRORS` (5, the same
   number the polling hook used) consecutive `error` events with no successful
   message in between, the provider surfaces a lost-contact state rather than
   leaving the UI frozen in a state it can no longer verify.

10. **`GET /api/jobs` stays, as the plain read beside the stream.** It returns
    every job in the store, unfiltered; a client filters by scope. It is what
    makes the state testable without opening a stream, debuggable with curl,
    and assertable in an E2E — and it is a handful of lines. The stream is the
    live mechanism; this is the snapshot.

11. **Route order matters and gets a test.** `GET /api/jobs/stream` must be
    registered BEFORE `GET /api/jobs/{job_id}`, or `stream` binds as a job id
    and the route 404s as an unknown job. This is the same ordering discipline
    already documented for the SPA fallback and the `/api/{full_path}`
    catch-all. A test asserts the stream route answers as a stream — it fails
    if the order regresses, and it must assert more than a status code, since
    the wrong route also answers.

## What changes

**Server** (`src/yt_shorts/studio/jobs.py`, `src/yt_shorts/studio/api.py`):
- `Job.__init__` takes optional `channel`/`event`; `snapshot()` emits them
- `JobStore` gains a method returning snapshots of all jobs, and one for the
  running ones (both under its existing lock)
- the `start_*` functions pass the profile's channel/event through
- `GET /api/jobs` — every job, unfiltered
- `GET /api/jobs/stream` — the SSE route, registered before `/api/jobs/{job_id}`

The event-payload assembly (which jobs a connection reports, and whether the
payload changed since the last send) is pure and lives beside the store, not
inside the route: it is the part with the actual rules, and it should be
testable without HTTP. `jobs.py` must not import FastAPI, as today.

**Client** (`src/yt_shorts/studio/web/src/`):
- a provider owning one `EventSource`, the job map, and the lost-contact state
- `useJob(jobId)` with `useJobPolling`'s signature; `useJobPolling` is removed,
  and its test file is replaced rather than dropped
- the five call sites change their import
- adoption of running jobs for the tab's own scope on the first snapshot
- `Job` type gains `channel`/`event`

## Testing

- **The payload rules, as pure functions.** Which jobs a connection reports
  (running, plus finished-while-connected, never the whole history) and the
  changed/unchanged decision, tested directly with hand-built jobs — no HTTP,
  no sleeping.
- **Scope and target reach the snapshot.** A render job started for
  `erf/studio-test` reports that channel and event; a connect job reports the
  channel and a null event; a copy job reports neither. A detect job reports its
  video id as `target` and an upload job its clip name — asserted BEFORE the job
  has recorded any result, which is the case that rules out deriving the target
  from `results`.
- **`GET /api/jobs`** lists a running job and a finished one, and reflects a
  job started through the render route.
- **The stream, without hanging the suite.** Open it, read a bounded number of
  events, assert the first carries the running job, then close. A test that
  iterates an endless stream wedges the whole run — every stream test reads a
  fixed count and closes, and the run is bounded with `gtimeout` as well.
  Assert the media type AND the parsed payload, so the route-ordering test in
  decision 11 cannot pass against the `{job_id}` route answering instead.
- **The heartbeat** appears on an idle stream, without waiting 15 real seconds:
  the interval and heartbeat period are module constants the test overrides, in
  the same spirit as this project's injected `runner`/`now`/`decoder` seams.
- **Client, Vitest:** the "absent before first snapshot is loading, absent
  after is failed" rule; retaining a job that drops out of the payload;
  lost-contact after five consecutive errors; adoption picking a running job of
  the right kind and scope, ignoring another event's, and NOT adopting an
  upload or a connect (decision 8 — a test, so a later "consistency" change
  cannot quietly start claiming modal flows across tabs).
- **E2E, the guard that matters.** Start a render, reload the page mid-render,
  and assert the UI still shows it running and still refreshes the clip list
  when it completes. Then a second page in the same context: assert it sees the
  render the first one started. Assert on rendered state and on `edit.json`/the
  clip list — not on a value minted per response, which this project has
  shipped as an unfailable guard twice (see CLAUDE.md).

## Out of scope

- **File-version events** — the stream reporting that a clip's rendered short
  changed on disk, which would cover the CLI render without a focus event. A
  real option, deliberately deferred: it adds a periodic stat sweep over an
  event's clips to a design that currently only reads an in-memory dict.
- **Removing the focus-refetch** from the stale-short design. It is the CLI
  answer and this is not.
- **Any cross-process notification from the CLI to a running studio.** The two
  deliberately do not know about each other; they share the `EventLock` and the
  files on disk, and that stays true.
- **WebSockets.** Nothing here needs a client-to-server channel; SSE over the
  existing HTTP app is the smaller mechanism.
- **Persisting jobs across a studio restart.** `JobStore` stays in-memory, and
  a restart still resolves a tracked id to the same "no longer available"
  state it does today.
