# Playlists as a filter, and queueing many streams at once

Date: 2026-08-04
Status: design, approved for planning

## The problem

The Streams tab lists a channel's finished streams as one flat list. Measured
on ERF: **91 streams**. An operator looking for the three parts of one race
weekend scrolls past eighty-eight things they do not want, and then queues each
one with its own click.

Two changes, one screen: **group the list by the channel's playlists**, and
**let several streams be queued in one action**.

## What was measured first

Every number below is from real `yt-dlp` calls against
`UCb3S2oA7lANdg5IS0QtF46w` on 2026-08-04, not from reading documentation. They
decided the design, so they are recorded here rather than summarised.

| Question | Answer |
| --- | --- |
| Streams in the Streams tab | 91, in 1.6 s |
| Playlists on the channel | 17, in 0.8 s |
| One playlist's members | 1.2 s |
| All 17 playlists, 6 threads | **2.5 s, no failures** |
| Streams in no playlist | **0** |
| Videos in more than one playlist | **0** |
| Videos in a playlist but NOT in the Streams tab | **8** |
| Playlist sizes | 13, 10, 10, 10, 8, 7, 7, 6, 6, 5, 4, 4, 2, 2, 2, 2, 1 |

Two of those eight missing videos are real broadcasts (`live_status:
was_live`, 2 h 30 m and 2 h 06 m — "ERF Special Catalunya 6H Part 2",
"ERF Special Event - Monza 8H - Stint 4"): exactly the material this tool
works on, and **unreachable from the studio today**. Four are highlights and
onboard clips (8–23 min). Two have no title and no duration at all — deleted
or private videos still listed in a playlist.

"Every stream is in a playlist" and "no video is in two playlists" are
observations of one channel on one day. The data model must not encode either
as a guarantee.

## Decisions

The operator chose the shape; each choice is recorded with what it rules out.

1. **A filter dropdown above the list**, not collapsible groups and not a
   playlist-first drill-down. The flat "all streams" view stays the default and
   stays one click away.
2. **A selected playlist shows the PLAYLIST's contents**, not the Streams tab
   filtered by playlist membership. This is what surfaces the two missing
   broadcasts. Short videos stay visible — the duration column already tells an
   8-minute onboard apart from a 3-hour broadcast, and a fixed minimum duration
   would decide for the operator what counts as a clip candidate.
3. **Multi-select offers transcribe, detect, and both**, including the chained
   case (transcribe a stream, then detect on it) for several streams at once.
4. **Each row shows what already exists** for that stream (transcript,
   analysis), and a bulk action skips what is already there by default.

### Correction to an assumption made while designing

The chained case was first designed around a new deferral rule in
`Worker._blocked_by`, on the belief that the queue had no dependencies. **It
has.** `Entry.after` exists, `JobQueue._dependency_status` honours it, and
`enqueueJob(kind, params, after)` is already in `api.ts`. A dependent whose
dependency ends without succeeding is FAILED with
`"dependency … ended without succeeding; this entry can never run"`.

That is strictly better than deferring: it cannot wait forever, and it needs no
change to `job_queue.py` or `worker.py`. The deferral rule is not part of this
design.

**But the chain exposes an existing dishonesty that has never been
reachable.** No browser call site sends `after` today — `JobsScreen` only
*displays* it. So `waitNote` has no branch for it: for a `detect` waiting on a
**running** transcription, `ahead` is 0 and it says *"It is next in line, and
starts as soon as the worker has a free slot."* That is false; a free slot does
not help it. This change is the first to make that state reachable, so closing
it belongs here.

## Data layer

### `youtube.py` — the catalogue

Three additions, in the module's existing shape: pure, stdlib-only, the
subprocess boundary injected as `runner`.

```python
@dataclass
class Playlist:
    id: str
    title: str
    count: int          # usable videos
    unavailable: int    # entries dropped for having no title

@dataclass
class Video:            # Stream, plus where it sits
    video_id: str
    title: str
    duration_seconds: int | None
    view_count: int | None
    playlist_ids: list[str]

@dataclass
class FailedPlaylist:
    title: str          # the playlist's own title, so the operator can name it
    reason: str

@dataclass
class Catalogue:
    videos: list[Video]
    playlists: list[Playlist]
    failed_playlists: list[FailedPlaylist]

def list_playlists(channel_url, *, runner=_default_runner) -> list[Playlist]
def list_playlist_videos(playlist_id, *, runner=_default_runner) -> list[Video]
def channel_catalogue(channel_url, *, runner=_default_runner,
                      max_workers=6) -> Catalogue
```

`list_streams` stays exactly as it is, `Stream` included — `channel_catalogue`
calls it and widens each `Stream` into a `Video` with an empty `playlist_ids`,
then fills those in from the playlist fetches. Nothing that reads `Stream`
today has to change.

`channel_catalogue` composes: the Streams tab, the playlist list, and every
playlist's members via a `ThreadPoolExecutor` — each worker calling the same
injected `runner`, which is what keeps the whole thing testable without a
network.

The video list is the **union** of the Streams tab and every playlist's
members: 99 videos on ERF, not 91. `playlist_ids` is a LIST because a video may
belong to several playlists, even though none does on this channel today.

`list_playlists` cannot fill in `count`/`unavailable` on its own — yt-dlp's
`playlist_count` on the `/playlists` tab is the number of PLAYLISTS, identical
on every row (17 on ERF), not the size of that playlist. Both numbers come from
the member fetch, so `list_playlists` returns them as 0 and `channel_catalogue`
is what sets them.

Two honesty rules, both from the measurement above:

- **A playlist entry with no title is dropped and COUNTED.** It is a deleted or
  private video (two of them on ERF). Counted as the playlist's `unavailable`,
  so a dropdown reading "(6)" is not silently a 6 that came from 8. Silently
  shrinking a list is this project's recurring failure mode; see
  `subtitle_pipeline`'s history.
- **A playlist whose fetch fails does not sink the catalogue.** It lands in
  `failed_playlists` with its title and the reason, the rest is served, and the
  UI says so. The same per-entry tolerance `list_streams` already applies to a
  malformed line, and the same "one failed clip must never abort a run" stance
  one layer down.

`max_workers=6` is a starting point for this machine, like
`worker.DEFAULT_LIMITS`, not a measurement of YouTube's tolerance. It is a
parameter so it can be lowered without touching call sites.

### `detect.py` — three small helpers

Two places ask the same question, so it is answered once:

```python
def stream_dir(video_id, workspace_dir) -> Path
def has_cached_transcript(video_id, workspace_dir) -> bool
def has_analysis(video_id, workspace_dir) -> bool
```

Each is a `validate_segment` plus one `Path.exists()`. They live in `detect.py`
because it already owns both filenames (`ANALYSIS_FILENAME`, and the
`transcript.json` path `require_cached_transcript` reads).

## API

**One route, richer payload.** `GET …/streams` keeps its per-channel cache and
its `?refresh=true`.

```json
{ "videos": [ { "video_id": "...", "title": "...",
                "duration_seconds": 29777, "view_count": 2400,
                "playlist_ids": ["PL..."],
                "has_transcript": true, "has_analysis": false } ],
  "playlists": [ { "id": "PL...", "title": "...",
                   "count": 6, "unavailable": 0 } ],
  "failed_playlists": [ { "title": "...", "reason": "..." } ] }
```

**`has_transcript`/`has_analysis` are computed fresh on every response** — two
`stat` calls per video, not measurable at 91 videos — and deliberately NOT
cached with the yt-dlp result. Caching them would leave the list saying "no
transcript" after a transcription finished, until someone pressed refresh. The
expensive part (yt-dlp) stays cached; the cheap, changing part does not.

**Nothing about enqueueing changes server-side.** No new endpoint,
`POST /api/jobs` and `_validate_enqueue` untouched. The client already has
`enqueueJob(kind, params, after)`.

The response shape changes from a bare array, so both callers move with it:
`listStreams` (StreamPanel) and `getStreams` (StreamScreen's title lookup,
which also gains the two previously invisible broadcasts).

## Frontend

### `streams.ts` — a new pure module

Not exported from a component, so Vite's fast-refresh boundary stays
component-only, and unit-tested with Vitest like `words.ts` and `jobs.ts`.

- `playlistOptions(catalogue)` — the dropdown's rows. "All streams (99)" first
  — the count is the UNION the catalogue holds, not the Streams tab's 91, so it
  matches the list the option actually shows — then each playlist with its
  count, then "In no playlist (n)" **only when n > 0**. An always-present empty
  bucket reads as a fault; on ERF today it is simply absent.
- `visibleVideos(catalogue, playlistId)` — the filtered list.
- `bulkPlan(selectedIds, videos, action, force)` — **the heart of it**: what
  will actually be enqueued, what is skipped and why, and which `detect` is
  chained to which `transcribe`.

`bulkPlan` is pure and tested on its own because it is the decision that costs
money and hours.

### The skip rule is symmetric

`transcribe` skips a video with `has_transcript`; `detect` skips one with
`has_analysis`. Each has its own "do it anyway" checkbox.

For `transcribe` the cost is concrete: a re-run **downloads the audio again**
before it can know the chunk count (`stream_transcribe.transcribe_stream`),
which is gigabytes for an 8-hour race, even though every chunk then comes from
the cache. For `detect` the cost is money at the provider. Neither is a good
default for a single click over thirteen rows.

> **Dated amendment, 2026-08-04, after implementation.** The claim above -
> that a re-transcription "downloads the audio again" - does not hold.
> `stream_transcribe.ytdlp_downloader` asks yt-dlp for the stream's true
> duration (metadata only) and probes any existing `streams/<video_id>/
> audio.*` with ffprobe; when the two agree, which is the ordinary case for a
> stream that already has a transcript (that transcript could not exist
> without that audio already on disk), it reuses the file and downloads
> nothing. The ordinary re-run is a metadata call and an ffprobe, not a
> download - it costs a real re-download only when the workspace's own
> `audio.*` was cleared, is short/partial, or fails to probe. The skip
> itself is still the right default (see CLAUDE.md's own correction of this
> same claim, and the two skips' full mechanism there) - both stay skipped
> by default so an operator does not pay for the ordinary case thirteen
> times over on one bulk click, even though the ordinary case is cheap. Only
> the COST CLAIM this paragraph opened with was wrong; follow CLAUDE.md for
> the corrected mechanism rather than silently rewriting this paragraph, the
> same way the job-queue design records its own `copy`/`io` amendment rather
> than editing the original claim out of existence.

### The bar says what will happen BEFORE the click

"3 selected · 2 transcriptions skipped: already transcribed · 1 will be
queued." If nothing is left, the button is **disabled with the reason** — a
click that silently does nothing is the same lying control as a spinner that
never moves.

**The sentence names the LEG that is skipped, never the video**, and that was
a correction rather than a first draft. Under "Transcribe + detect", a video
whose transcript already exists has its TRANSCRIPTION skipped while its
DETECTION is queued for it — so "2 already have a transcript and will be
skipped" said the video was skipped at the moment a paid job was being queued
for it. It is the note-disagrees-with-the-action case this section itself
warns about, found by review before the bar was ever built on the string.

### The chain breaks per video, not per batch

Enqueued sequentially, in list order — sequential is not laziness: parallel
requests would scramble the queue order, and the plan should match the order
the operator ticked.

Per selected video: enqueue `transcribe` (unless skipped), keep the entry id,
then enqueue `detect` with `after` set to it (or with no `after` when the
transcribe was skipped because a transcript already exists).

**If the `POST` for video B's transcribe fails, B's detect is NOT enqueued.** A
detect without its `after` would quietly run on an untranscribed stream. The
notification says "5 queued, 1 refused: …" — never a bare "queued".

### Selection survives a filter change

Selection is by video id. The bar says "6 selected (2 not in this view)".
Clearing on filter change would be simpler but makes "two playlists at once"
impossible, which a race weekend split across two playlists makes plausible.

### Tracking: one map instead of four state variables

`App.tsx` holds `detectEntryId`, `transcribeEntryId` and two `activeVideoId`
today. That becomes a `Map<videoId, {transcribe?: string, detect?: string}>` in
the same place — App, so it survives switching the navbar's tabs, the same
reason the detect entry is hoisted there now.

`useQueuedEntries(ids: string[])` is new; **`useQueuedJob` is re-expressed on
top of it** rather than sitting beside it. The plan is a single
`GET /api/jobs`, so thirteen hooks would fetch it thirteen times — and a second
hook would be a second copy of the `seen` race guard, the error budget, and the
rule that `error` set means `pending` and `running` are both false. This
project has already paid for three copies of a colour ternary.

Each row shows its own badge. Above the list, a summary: "6 queued: 1 running,
3 waiting, 2 finished", with a link to the Jobs screen.

> **Dated amendment, 2026-08-04, after implementation.** The per-batch
> summary line above the list was never built. Each row carries its own
> "Jobs" anchor instead (`TrackedEntry` in `StreamPanel.tsx`), which is what
> actually shipped and is what `tests/test_studio_e2e.py` exercises. This is
> a fine decision on its own merits - a per-row link needs no aggregation
> logic and cannot go stale relative to the rows it sits beside - but the
> design promised the summary and nothing recorded that it was dropped until
> this note. Do not add it later on the assumption this document describes
> the shipped behaviour; it does not, here.

### One notification per batch, not thirteen

`jobs.ts` gains `batchNotice(...)` beside `endedNotice`, firing when the last
entry of a batch reaches a terminal state, with the same colour rules — **a
stop is never reported in the failure colour**. A single-row action is a batch
of one, so there is one code path, not two.

### The `waitNote` correction

A branch for `entry.after`: if the dependency is in the plan and not `done`,
say so ("waiting for this stream's transcription") instead of "next in line".
If the dependency is not in the plan at all, it is satisfied
(`_dependency_status` treats it so) and the branch stays quiet.

## Testing

- `tests/test_youtube.py` — catalogue composition against an injected
  `runner`. It is called from threads, so recorded calls are compared as a
  **set**, never by order. Titleless entries are dropped and counted; a failing
  playlist does not sink the catalogue.
- `tests/test_studio_api.py` — the new payload; `has_transcript` is **fresh**
  (write a transcript after the first call, and the second call reports it
  without `refresh`).
- Vitest — `streams.test.ts` (`bulkPlan` across every skip and chain case),
  `jobs.test.ts` (the new `waitNote` branch, `batchNotice`),
  `useQueuedEntries.test.tsx` (both stop reasons, for several ids).
- `tests/test_studio_e2e.py` — against a studio whose worker is deliberately
  NOT running, like `TestTheOtherButtonsGoThroughTheQueueToo`: filter by
  playlist, tick three rows, press "both", and assert the plan holds each
  `detect` with `after` pointing at its own `transcribe`, and that each row
  shows its wait reason.
- **Reachability at a short viewport is an acceptance criterion, not a
  nicety**: the action bar must not eat the list. Checked with
  `_wheel_scroll_until_visible`, never `scroll_into_view_if_needed()` — which
  was proven on this branch to pass on a broken build.
- `python3 tools/lint.py` must stay green, and lint is not optional before
  trusting a green suite: a duplicate test-class name is dropped silently by
  pytest and only `F811` catches it.

## Explicitly out of scope

- No new enqueue endpoint.
- No change to `job_queue.py`, `worker.py`, or `_validate_enqueue`.
- No deferral rule for a missing transcript — `after` already covers it.
- No minimum-duration filter on playlist contents.
- No change to `list_streams` itself.
