# Stage D2a — Whole-stream transcription

**Date:** 2026-07-22
**Scope:** turn one full stream into a cached, timed transcript and a kept local
audio file, so D2b can detect moments in it. This stage produces the artifact;
it does not score or detect anything — that is D2b.

## Problem

Moment detection (D2b) needs two things over a whole stream: a timed transcript
(for the emphasis signal) and the audio itself (for the loudness signal). The
existing `transcribe()` is built for a downloaded clip of a few seconds: it
extracts the whole audio in one ffmpeg pass and decodes it in one unbounded
Whisper call. A stream is not a clip.

## Why this is its own stage, and why chunked

Measured on this machine, not assumed: the project's `transcribe()` decodes at
**0.123× realtime** (38s of wall-clock for 310s of real ERF stream audio, `small`
model, `vad_filter=False`). Extrapolated:

- an 8-hour ERF race stream → **~1 hour of uninterrupted Whisper decode**
- a 2-hour session → ~15 minutes

One hour of decode in a single unbounded call is exactly the failure the
`transcribe.py` "Unbounded decode" note warns about: if it hangs at minute 50, a
whole hour is lost, and no timeout can safely fire inside one giant decode.

Chunking the audio into fixed windows turns that single hour-long risk into a
sequence of ~1–2 minute decodes. Two things follow that a whole-stream decode
cannot offer:

- **A per-chunk timeout becomes feasible.** A chunk that hangs is abandoned
  after a bounded wait; the rest of the stream still transcribes. The
  whole-stream decode could not be timed out at all.
- **The work is resumable.** Each chunk's words are cached as they finish. A
  re-run, or a recovery after an abort, re-decodes only the chunks that are
  missing — never the whole stream again.

This is enough new machinery, with enough of its own risk, to be its own stage.
D2b builds detection on top of the artifact it produces.

## What it does

Given a public stream's `video_id`:

1. **Download the audio once** with yt-dlp (`bestaudio`), into the workspace,
   keyed by `video_id`. yt-dlp's own resume (`--continue`) handles a partial
   download. The audio is **kept**, not deleted — D2b reads it for loudness.
2. **Split into fixed chunks** (default 600 s) by time. No re-download per chunk:
   the single audio file is the source, and each chunk's PCM is extracted from it
   with ffmpeg `-ss`/`-t` (the same ffmpeg extraction `transcribe()` already
   does, bounded window instead of whole file).
3. **Decode each chunk in a killable subprocess** with the same Whisper settings
   the tool already uses — `small`, `vad_filter=False`, the post-decode
   `no_speech_prob > 0.75` drop, the channel glossary as `hotwords` — and shift
   each chunk's word timestamps by the chunk's start offset so they are absolute
   stream time. The subprocess is what makes the per-chunk timeout *real*: as
   `transcribe.py`'s "Unbounded decode" note explains, a hung Whisper decode
   cannot be stopped by a signal (it is inside a C extension) or by a thread
   (Python threads are not killable) — only a separate process can be killed.
   Chunking is what finally makes that restructuring worthwhile: the model is
   loaded once per chunk subprocess rather than once for the whole run, and that
   reload (a few seconds against the measured ~74 s of decode per 600 s chunk) is
   a minor overhead for a timeout that actually works.
4. **Cache each chunk's words** as they finish. A per-chunk timeout abandons a
   hung chunk and records it as failed rather than sinking the run.
5. **Assemble** the present chunks, in order, into one timed word list — the
   stream transcript — cached as a single artifact keyed by `video_id`.

Progress is reported as chunks complete, because this is a minutes-to-an-hour
operation, not the seconds a clip takes.

## Architecture

A new module `src/yt_shorts/stream_transcribe.py`:

- `transcribe_stream(video_id, workspace_dir, *, downloader=..., decoder=...,
  chunk_seconds=600, ffmpeg="ffmpeg", model_name="small", glossary=EMPTY) ->
  StreamTranscript`, where `StreamTranscript` carries the absolute-time word list
  and the path to the kept audio. `downloader` and `decoder` are the injected
  boundaries — the yt-dlp download and the Whisper decode — so chunk-splitting,
  timestamp-offsetting, resumability and assembly are all tested against recorded
  chunk output without the network or a real model, exactly as D1 injects its
  yt-dlp `runner`.
- The Whisper decode settings are shared with `transcribe.py`, not
  re-implemented: the one decode core (`vad_filter=False`, `no_speech_prob` drop,
  glossary application) is the single place those settings live, so a clip and a
  stream chunk decode identically. Extracting that core from `transcribe()` is
  part of this stage. D2a invokes it inside the killable subprocess described
  above; `transcribe()` keeps invoking it in-process for a short clip, where a
  seconds-long decode needs no timeout.
- The injected `decoder` boundary is that subprocess call: in production it spawns
  the killable process and returns the chunk's words (or a timeout/failure); in
  tests it returns canned words, so every behaviour below is exercised without a
  real model or a real subprocess.

### Workspace layout (runtime data, never the repo)

Under the resolved workspace (`YT_SHORTS_DATA` → `~/YT-Shorts-Data` → repo
`channels/`, per `workspace.py`):

```
streams/<video_id>/
  audio.<ext>            downloaded once, kept for D2b's loudness signal
  chunks/000.json        per-chunk cached words (absolute stream time)
  chunks/001.json
  ...
  transcript.json        assembled full-stream transcript (the artifact)
```

`<video_id>` is a clean, stable key (the same id D1 lists and D2b's clip entries
carry). Everything here is derived, re-derivable data: deleting `streams/<id>/`
re-downloads and re-transcribes. Nothing here is editorial, so nothing here is
precious — the same rule the rest of the workspace follows.

## Caching and resumability

- **Chunk cache** is source-matched the way `transcribe()`'s clip cache is: a
  chunk file records what stream and what time window it is for, and a mismatch
  is re-decoded rather than trusted. A present, matching chunk file is returned
  as-is; only missing chunks are decoded.
- **The assembled transcript** is rebuilt from whatever chunks are present. If
  some chunks failed (timed out or errored), the transcript has gaps there and
  says which chunks are missing — it does not pretend to be complete, and a later
  re-run fills the gaps.
- **No glossary re-invalidation**, same as `transcribe()`: a cached chunk is not
  re-decoded because the glossary changed. Delete the chunk (or the stream dir)
  to re-derive. This is the established pre-alpha rule, not a new exception.

## Failure isolation

Consistent with the tool's hard rule that one failure never aborts a run:

- A single chunk that hangs is abandoned after the per-chunk timeout and recorded
  as failed; the remaining chunks still decode. The timeout is set generously
  relative to the measured decode cost (~74 s for a 600 s chunk), not near it —
  the same reasoning `transcribe.py` gives for having no clip timeout at all: a
  timeout that misfires on a slow-but-healthy chunk aborts good work, which is
  worse than the hang it guards against, so it must only ever fire on a genuine
  hang (a small multiple of the expected decode, e.g. 600 s for a 600 s chunk).
- A single chunk that errors (bad audio window, decoder exception) is recorded
  with its reason; the run continues.
- The download failing, or yt-dlp being absent, is a clear `StreamTranscribeError`
  naming what happened — not a raw traceback.

## Testing

- Chunk arithmetic: a stream of a given length splits into the expected windows,
  the last (short) chunk included, off-by-one at the boundary covered.
- Timestamp offsetting: a decoder returning words at 0–30 s inside chunk 3
  (start 1800 s) yields words at 1830 s in the assembly.
- Resumability: with chunks 000–002 already cached, a run decodes only the
  missing chunks and assembles all of them; present chunks are not re-decoded.
- Failure isolation: one chunk's decoder raising (or timing out) leaves a gap and
  a recorded failure, the other chunks assemble, the run does not abort.
- Source-mismatch: a chunk file recorded for a different stream is re-decoded, not
  trusted.
- Error path: the injected downloader failing becomes `StreamTranscribeError`.
- All of the above run against an injected decoder and downloader — no network, no
  real Whisper, no multi-hour test.

## Not in scope

- Emphasis scoring, loudness, candidate ranking, windows — all D2b.
- A user-facing trigger (CLI command or studio button) to start a stream
  transcription — it lands with D2b, which is what has a reason to start one. This
  stage is the library capability and its cache, tested directly, the same way D1
  shipped `list_streams` and deferred the picker to D2.
- Unlisted/private streams (yt-dlp cookies) — a later addition if needed, as in
  D1.
- Re-using YouTube's own auto-captions — deliberately not chosen: this stage
  decodes locally with Whisper for accuracy and independence from YouTube.
