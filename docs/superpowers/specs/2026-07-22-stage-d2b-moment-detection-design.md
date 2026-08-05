# Stage D2b — Moment detection

**Date:** 2026-07-22
**Scope:** find the moments worth clipping in a stream's transcript and audio, and
let an operator curate them in the studio — review candidates, adjust each
window, keep or discard — then render them through the existing pipeline. This is
the stage that turns a whole stream into shorts without a human scrubbing hours of
video. It builds on D1 (which stream) and D2a (its transcript and audio).

## Problem

The community-clips flow needs a human to find each moment by hand. A race stream
is 2–8 hours; nobody watches all of it to find the ten seconds worth posting. D2a
produced the transcript and audio; this stage reads them, proposes the moments,
and gives the operator a fast way to accept or reject each one.

## What it does, end to end

1. **Pick a stream** (D1's `/api/streams`) and **transcribe it** (D2a) — run as one
   background job from the studio, the way renders already run.
2. **Score emphasis** across the transcript: per-channel **lexicon** hits plus
   **speech-rate** spikes, combined and normalised. A threshold picks candidate
   peaks; peaks closer than a configurable gap merge into one, so a single
   incident is one candidate, not twenty.
3. **Rank by loudness**: for each candidate, measure the audio loudness in its
   window from D2a's kept audio, and order the candidates by that peak. Transcript
   emphasis decides *which* moments; loudness decides *which of those* rank
   highest. Take the top N.
4. **Write candidate clip entries** into the event — each with the stream's
   `video_id`, a start/end window around the peak, the stream title, and a
   provisional hook drawn from the transcript at that point. They arrive as
   `candidate` status, the editorial default.
5. **Curate in the studio**: a candidate list, a window control per candidate
   (nudge the lead-in and lead-out), and keep/discard. Keeping and rendering use
   the pipeline that already exists — moment clips render through `render.Source`'s
   `video_id` + time-range path.

## Detection

A new `moments.py`, pure logic over a word list plus an injected loudness
measurement, so the whole thing tests without audio or ffmpeg.

### Emphasis score

Two signals, each normalised to 0–1, then summed:

- **Lexicon hits.** The channel's excitement markers (see below) matched against
  the transcript, case-insensitively and phrase-aware, the same punctuation-tolerant
  matching `glossary.apply` uses. Each hit contributes at the time it occurs.
- **Speech-rate.** Words per second in a sliding window. Excited commentary comes
  fast; a spike above the stream's own baseline rate is the signal, so a
  slow-talking stream and a fast one are each judged against themselves rather than
  an absolute words-per-second number.

The two are summed into an emphasis value along the timeline (evaluated in small
steps). Local maxima above a configurable threshold are candidate peaks. Peaks
within a configurable minimum gap collapse to the strongest one.

### Loudness ranking

For each candidate window, an injected `measure_loudness(audio_path, start, end)
-> float` returns the window's loudness (production: ffmpeg EBU R128 / peak; tests:
a stub). Candidates sort by this value, descending; the top N become clip entries.
This is the "transcript filters, loudness orders" decision made during design:
loudness never introduces a candidate the transcript did not, it only ranks the
ones it did.

### The window

A candidate's peak is the anchor. Its window is `[peak - preroll, peak + postroll]`,
with `preroll`/`postroll` defaults from the channel profile (e.g. 8 s / 4 s). The
window is what the operator adjusts in the studio; detection only sets the initial
one.

## The lexicon (`moments.json` per channel)

A new per-channel file, loaded by `profile.py` exactly as `glossary.json` is
(channel-level, optionally replaced wholesale by an event-level file). It is
**separate from `glossary.json` on purpose**: the glossary corrects proper nouns
the decoder mishears; the lexicon names the words that mark an exciting moment
(*crash, overtake, safety car, P1, "oh my", contact, incident*). Different job,
different file. Shape: a list of marker phrases, optionally weighted; a missing or
empty file means no lexicon signal (speech-rate still works), never an error —
the same tolerance `glossary` has.

## Moment → clip entry

A detected moment becomes a normal clip entry so the rest of the pipeline needs no
special case:

- **Identity must be distinct per `(video_id, start, end)`** — two moments in one
  stream are two clips. The entry carries the `video_id` and the window directly
  (what `render.Source` needs), and its clip identity is derived so that the same
  moment re-detected lands on the same clip while a different window is a different
  clip. The exact identity string (a canonical `watch?v=…` URL that encodes the
  range, versus a dedicated moment id) is settled in the plan against
  `clipid.canonical_url`'s actual behaviour; the requirement is distinctness per
  window.
- **Fields:** `video_id`, `start`, `end`, `duration`, `source_title` (the stream
  title), and a **provisional hook** — a short transcript excerpt at the peak (or
  the matched marker), a starting point the operator will almost always rewrite,
  not a finished caption.
- Written through `clipstore.write_clip`, so a moment clip sits in the event
  beside community clips and is indistinguishable to `render` and the studio.

## Editorial window override

Adjusting a window in the studio is an **operator decision about a derived
artifact**, so it lives in `edit.json`, never rewritten into the detected entry —
the same rule that keeps a title correction out of `clip.json`. `editorial.Edit`
gains an optional window override (a `start`/`end` pair), following the existing
`title` pattern:

- Absent → the detected window is used.
- Present → it wins, and `render`/preview use the effective start/end (editorial
  over detected), exactly as the effective title already layers.
- Written only by the explicit studio action, only into `edit.json`.

Keep/discard needs nothing new: `status` already carries `candidate` (the
default detection produces), `kept`, and `discarded`.

## Orchestration (studio background job)

Detecting moments means running D2a (minutes to an hour) then D2b (seconds). That
is a background job, like a render:

- `POST /api/streams/{video_id}/detect` starts a job that transcribes the stream
  (D2a) then detects moments (D2b) and writes the candidate entries. It reports
  progress (D2a's chunk progress, then detection), refuses to start a second job
  for the same stream while one runs, and survives neither a restart (in-memory,
  the same deliberate limit render jobs have) nor pretends to.
- `GET /api/jobs/{id}` reports its state, reusing the existing job surface.
- A stream already transcribed skips straight to detection — D2a's cache is why.

## Studio UI

Added to the existing studio, in its neutral timing-tower style (not ERF's
colours):

- **Stream list** (D1's `/api/streams`, still unbuilt as UI) becomes real here:
  pick a stream, start a detect job, watch progress.
- **Candidate list**: the detected moments as dense rows — rank, window, duration,
  loudness, the provisional hook — in the same timing-tower vocabulary as the clip
  list, with status markers (candidate amber, kept green, discarded dimmed).
- **Window control** per candidate: nudge lead-in and lead-out; the preview and
  the effective window update, and saving writes the editorial override. Saving
  stays explicit, as everywhere in the studio.
- Keep/discard, then the existing render flow renders the kept moments.

## Configuration

Per channel (profile), all with sane defaults so a new channel needs none:

- `preroll` / `postroll` seconds (window around a peak)
- `min_gap` seconds (candidate merge distance)
- `top_n` (how many candidates to keep)
- emphasis `threshold`
- the lexicon file (`moments.json`)

## Testing

- Emphasis scoring: lexicon hits and speech-rate spikes each produce candidates;
  the two combine; a below-threshold stretch produces none; nearby peaks merge to
  one. All against a hand-built word list, no audio.
- Loudness ranking: with a stub `measure_loudness`, candidates order by loudness,
  not by emphasis; the top-N cut is honoured.
- Window: a candidate's entry spans `[peak - preroll, peak + postroll]`; the
  defaults come from the profile.
- Clip entries: a moment writes a clip entry with the video id and window; two
  different windows get distinct identities; re-running detection is idempotent on
  the same moment.
- Editorial window override: absent uses detected, present wins, `render`/preview
  use the effective window, and it is written only to `edit.json`.
- The detect job: transcribe-then-detect orchestration with D2a and detection
  stubbed; a second job refused while one runs; an already-transcribed stream
  skips to detection.
- Studio routes and page: candidate list, window edit persists to `edit.json`,
  keep/discard — driven in a real browser (the studio's Playwright E2E), as the
  studio's other flows are.
- Lexicon loading: channel-level, event override, missing file tolerated —
  mirroring the glossary tests.

## Not in scope

- Upload, OAuth, account switching — stage E.
- Non-loudness ranking signals (live chat activity was noted as a future signal;
  not this stage).
- Auto-writing a finished hook — detection proposes a provisional one; a good
  caption is the operator's, via the editorial title override that already exists.
- Cookies for unlisted/private streams — carried over from D1/D2a as a later
  addition.
