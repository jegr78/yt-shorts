# Trimming a rendered short in the studio

**Date:** 2026-07-30
**Scope:** cutting seconds off the head and tail of an already-rendered
`short.mp4`, from the studio, without re-downloading, re-transcribing or
re-composing. Re-cutting the SOURCE window (`editorial.Edit.window`) already
exists and is a different feature; this design does not change it.

## Problem

An operator watches a finished short and wants the first two seconds and the
last three gone. Today the only way is an external tool, or shifting
`Edit.window` and paying a full render (download + Whisper + compose, ~2.5
minutes) for a cut that needs neither the source nor the transcript.

The captions are burned into the picture, so a head/tail cut moves nothing
relative to the image. Nothing needs re-deriving. This is purely an edit of
the finished file.

## Measurements (2026-07-30, on `last-gasp-lap-steals-super-pole`, 84 s)

Every number below was measured on this workspace's own clip, not estimated.

| Operation | Cost |
|---|---|
| Full render (download + transcribe + compose) | ~2.5 min |
| Compose alone, from the kept `raw.mp4` | 57 s |
| Cut the finished short, re-encoded (`-crf 18 -preset veryfast`) | 15 s |
| Cut the finished short, stream copy (`-c copy`) | 0.08 s |

The stream copy is **rejected**: cuts land on keyframes, and the keyframe
interval in this project's own output is 4.18 s. A requested cut at 5.0 s
lands at 4.18 s — over four seconds wrong, on a feature whose whole point is
"a few seconds". The re-encode is exact and preserves geometry
(`1080,1920,1:1` verified) and passes the audio through untouched.

## Decisions taken with the operator

1. **Instant preview, deliberate apply.** The player previews the trimmed
   range immediately with no encoding at all; the physical cut happens only
   when the operator asks for it. Rejected: cutting on every save (each
   correction would cost a wait), and cutting only at download/upload time
   (the real result would not be visible until the end).
2. **Delivery is blocked while a trim is pending**, rather than warned about
   or silently auto-applied. An unapplied trim must never reach YouTube.
3. **An untrimmed master file** is kept, so applying again is 15 s rather
   than 72 s and never compounds. Rejected: trimming at compose input
   (would require shifting the subtitle track by the head cut, coupling
   trimming to the caption timeline — the exact silent-misalignment class of
   bug this project has now paid for twice), and re-composing per apply
   (5× slower, and depends on `raw.mp4` still existing, which is true today
   but not guaranteed for older clips).
4. **Two properties, head and tail, both defaulting to 0.**

## Data model

`editorial.Edit` gains one field, beside `window` and `upload`:

```python
trim: tuple[float, float] | None = None      # (head_seconds, tail_seconds)
```

`None` means "no trim", which is the same request as `(0.0, 0.0)` and is what
every existing `edit.json` means today — no migration.

`editorial.load` validates it exactly the way `window` is validated, and
raises `EditError` naming the file otherwise:

- a two-element list of numbers, or absent;
- both values `>= 0` (a negative trim would mean *extending*, which there is
  no material for);
- `head + tail + MIN_REMAINING_SECONDS <= clip duration`.

`editorial.save` writes it back as a two-element list, like `window`.

**`MIN_REMAINING_SECONDS = 3.0`.** Below three seconds there is no short left,
only a mistake. This is a floor on what remains, never a target length —
per the operator's standing rule that the data decides the length, not a
constant.

Duration for that check comes from the clip's own `clip.json` `duration`
(harvested/detected), NOT from probing the mp4: `editorial.load` must stay
free of ffmpeg and must work for a clip that has never been rendered. The
exact rendered length can differ from `clip.json` by a frame or two; the
3-second floor absorbs that without needing precision.

## Files and the one invariant

```
clips/<clip>/short.mp4          the deliverable — ALWAYS embodies the
                                applied trim; this is what the player,
                                the download link and the upload use
clips/<clip>/short.full.mp4     the untrimmed master; exists ONLY while a
                                trim is applied
clips/<clip>/short.trim.json    {"head": 3.0, "tail": 2.0} — which trim
                                short.mp4 currently embodies; exists ONLY
                                while a trim is applied
```

New path helpers in `clipstore.py` beside `short_path`: `short_master_path`,
`short_trim_state_path`.

**The invariant:** `short.mp4` embodies the trim recorded in
`short.trim.json` (or none, when that file is absent).

**Pending vs applied** is therefore a comparison of two recorded values —
`Edit.trim` against `short.trim.json` — and needs no probing of the video.
That matters: `_summary` runs per clip on every list request, and this
project has already had to reject an O(size) computation there.

`short.full.mp4` is derived, not an original: if it is lost, a re-render
recreates it. It is never the file anything downstream reads.

## `trim.py` — the one place a cut happens

A new module, stdlib + subprocess only. It imports `clipstore` and
`editorial` but **not** FastAPI — same rule as `subtitle_pipeline.py`, and
for the same reason: `bin/yt-shorts` must import it in a venv that never
installed FastAPI.

```python
def ensure_applied(directory: Path, edit: editorial.Edit, *,
                   ffmpeg: str = "ffmpeg") -> bool
```

Idempotent. Returns whether it changed anything.

1. Read the desired trim from `edit.trim` (absent → `(0.0, 0.0)`).
2. Read the applied trim from `short.trim.json` (absent → `(0.0, 0.0)`).
3. Equal, or no `short.mp4` at all → return `False`, do nothing.
4. Otherwise **cut from the master**: if `short.full.mp4` does not exist yet,
   promote the current `short.mp4` to it first (a rename, not a copy). Cutting
   always from the master is what stops repeated corrections from compounding.
5. Desired is `(0, 0)` → move the master back to `short.mp4`, delete the
   master and the state file, return `True`.
6. Otherwise run
   `ffmpeg -v error -y -ss <head> -to <duration-tail> -i <master>
    -c:v libx264 -crf 18 -preset veryfast -c:a copy <scratch>`
   and `Path.replace` the scratch into `short.mp4`, then write the state file.

**`-ss` AND `-to` must both come BEFORE `-i`.** Measured on this project's own
short, cutting head 5 s from an 84.27 s file:

```
ffmpeg -ss 5 -to 79 -i short.mp4 -c copy out.mp4   ->  74.08 s   correct
ffmpeg -ss 5 -i short.mp4 -to 79 -c copy out.mp4   ->  79.10 s   WRONG
```

As an input option `-to` is a position in the input's own timeline, so
`duration - tail` means what it says. Moved after `-i` it becomes an output
option, the seek has already reset timestamps to zero, and it is read as a
LENGTH — the tail cut silently does not happen and the head cut is the only
one applied. Five seconds of error, no error message. A test must pin the
resulting duration, not merely that ffmpeg exited 0.

**The master's duration is probed with ffprobe**, not taken from
`clip.json`: `-to` needs the real length of the rendered file, which differs
from the harvested duration. `stream_transcribe.ytdlp_downloader` already
shells to ffprobe for exactly this, and the same treatment applies here — the
subprocess boundary is injected (`runner`), so every branch tests without
ffmpeg, ffprobe or a real video. A probe that fails, or returns something
unparseable, leaves the trim pending and reports why; it never guesses a
duration.

**The scratch file goes before the extension** (`short.trim-part.mp4`, not
`short.mp4.part`): ffmpeg picks its muxer from the extension and refuses to
write to an unknown one. This is not a hypothesis — `TestComposeIsAtomic`
pins exactly this for `render.compose`, where it was found the hard way.

**Write-aside-then-replace is required, not tidiness.** From the moment a
naive in-place cut starts, `short.mp4` exists but is incomplete, its
`(mtime, size)` version token matches, and `GET …/short` would hand out
`immutable` for bytes still being written — the incident `render.compose`
already documents. The scratch file is cleaned up in a `finally`.

**Order of operations on the state file:** the state file is written only
AFTER the replace succeeds. A failed cut therefore leaves the previous,
consistent pair (file + state) untouched; the trim simply stays pending.

## Call sites

Three, all calling `ensure_applied`:

- the studio's apply job (below);
- `cmd_render`, after `build_short` returns for a clip;
- `studio.jobs._render_one`, likewise.

The last two exist so the invariant holds again after every render: a render
writes an untrimmed `short.mp4`, and without this the operator's trim would
silently vanish.

**The render path must call `trim.forget_applied(directory)` FIRST, and that
call must delete the stale master as well as the state file.** This is the
subtle one. After a render, `short.mp4` is a freshly composed, untrimmed file
— it IS the new master. A `short.full.mp4` left over from before the render
is the OLD composition; `ensure_applied` cuts from the master, so leaving it
in place would produce a short built from stale material while `short.mp4`
sat beside it unused. Deleting only the state file (an earlier draft of this
paragraph said exactly that, and it was wrong) leaves the same stale master
in place for the promote step to skip. Both go.

Order therefore is: render writes `short.mp4` → `forget_applied` removes
master and state → `ensure_applied` promotes the fresh `short.mp4` to master
and cuts. A test must pin that a re-render followed by a trim uses the NEW
composition, not the old master.

**`render.py` is not touched.** It knows nothing of `clipstore` or editorial
data and must keep it that way; the cut is applied by the caller afterwards,
exactly as the subtitle pipeline is wired in from outside.

## Studio API

**`POST …/clips/{name}/trim`** starts a background job (`kind="trim"`), taking
the same `EventLock` a render takes — applying a trim writes `short.mp4`, and
so does a render; they must not race. The job gets its own log under
`logs/jobs/trim-<id>.log`, like every other job. Returns the job payload the
render route already returns. `409` when no short exists yet, with a message
saying the trim will be applied by the next render.

**`GET …/clips/{name}/short`** gains an optional `as=download` query
parameter. Only with it set does the pending-trim guard apply (`409`); the
plain URL keeps streaming for the player.

This is an honest boundary — previewing is not delivering — and its limit is
stated rather than glossed over: someone who calls the plain URL by hand gets
the untrimmed file. What is hard-guarded server-side is the path that reaches
the channel.

**`POST …/clips/{name}/upload`** refuses with `409` while a trim is pending,
before any upload job starts — the same shape as the existing `manual`-channel
refusal.

**`_summary`** gains `trim` (the desired pair, or `null`) and
`trim_applied` (the applied pair, or `null`). Two recorded values, one extra
small file read per clip — no probing.

## Studio UI

In `ClipEditor`, beside the player:

- two number inputs, "Head" and "Tail", in seconds, both defaulting to `0`,
  saved with the rest of the editorial correction — no separate save path;
- the resulting duration, computed and shown as the values change;
- a button "Apply trim", showing its state: *applied* or *pending*. Disabled
  when desired equals applied, and when no short exists yet (with the reason
  named).

**The preview costs nothing.** On `loadedmetadata` the player seeks to
`head`; a `timeupdate` handler pauses at `duration - tail`. No encoding, no
request, no new file. This is what makes "nudge until it looks right" free.

The download link in `ManualUploadPanel` appends `as=download` and is
disabled while a trim is pending, naming the reason. The upload panel does
the same.

All arithmetic (clamping, remaining duration, pending comparison) goes in a
pure module `trim.ts` beside `words.ts` and `window.ts`, unit-tested with
Vitest — not in the component, so Vite's fast-refresh boundary stays
component-only.

## Error handling

- ffmpeg fails → the job fails with the cause in its log and the clip's
  `reason`; `short.mp4` and the state file are untouched, so the trim stays
  pending and nothing on disk is inconsistent.
- `short.full.mp4` missing while `short.trim.json` exists (a half-deleted
  directory) → treat as "no master": promote the current `short.mp4` and
  re-apply from it. This loses the already-cut seconds, so it is reported as
  a note rather than done silently.
- An `edit.json` whose `trim` fails validation is an `EditError` like any
  other, naming the file — the whole clip refuses to load, exactly as a bad
  `window` does today.

## Testing

pytest, no network, no real render:

- `editorial`: round-trip, every validation branch, and that an absent
  `trim` still loads (no migration).
- `trim.ensure_applied`: idempotent (second call returns `False`); cuts from
  the master, so two successive different trims both measure from the
  original; reverting to `(0, 0)` restores the master and removes both extra
  files; a failing ffmpeg leaves file and state consistent and the scratch
  file removed; the scratch name keeps its `.mp4` extension.
- One real-ffmpeg test on a tiny generated clip asserting the resulting
  DURATION (not merely that ffmpeg exited 0 — the `-to` placement trap above
  produces a wrong length and a clean exit) and that geometry survives:
  width, height and SAR unchanged from its input, checked with ffprobe, never
  with an extracted still.
- `forget_applied` removes the master as well as the state file, pinned by a
  test that re-renders (a stubbed compose writing different bytes) and then
  trims, asserting the result derives from the NEW composition.
- Studio: `409` on upload while pending, `409` on `?as=download` while
  pending, plain `GET …/short` still `200` while pending (the preview must
  not break), the job takes the event lock.
- Vitest for `trim.ts`.
- One E2E through the studio: set values, see the duration update, apply,
  see the state flip to *applied*.

## Out of scope

- Cutting pauses out of the MIDDLE of a short. That touches the caption
  timeline, the audio and the window logic at once and deserves its own
  design.
- Any change to `Edit.window`, the source-window trim.
- Re-encoding settings as a configurable — `-crf 18 -preset veryfast` is
  fixed here, measured at 15 s for 84 s of 1080x1920.

## Constraints this must not violate

- ffmpeg here has no `libfreetype`/`libass`; nothing in this feature needs
  them. Do not reinstall or upgrade ffmpeg.
- The six pinned overlay hashes in
  `tests/test_event_layer_no_regression.py` must not move — nothing here
  touches `overlay.py`.
- `setsar=1` stays at the end of the compose chain; the cut re-encodes an
  already-composed file and must not reintroduce a non-square SAR. Verify
  with `ffprobe`, never with an extracted still.
- No test may hit the network, run a real Whisper decode, or spend money.
- The studio's write boundary: this adds `short.full.mp4` and
  `short.trim.json` as new DERIVED outputs of a render the operator asked
  for. It redefines no window, no transcript, and no clip identity.
