# Whole-stream transcription

`stream_transcribe.transcribe_stream(video_id, workspace_dir)` turns a whole
stream into a timed transcript and a kept local audio file, both under
`streams/<video_id>/` in the workspace. It is the input the moment detector will
run on. Because a stream is hours long — **over two hours of Whisper decode for
an 8-hour race**, measured on this machine on 2026-07-31 (an 8 h 19 min stream:
about 2.5 minutes per 10-minute chunk, 50 chunks; the README said "~1 hour"
until then, and the figure is machine-dependent either way) — it works in fixed
chunks: the audio is downloaded once, each chunk is
decoded in a separate killable process, each chunk's words are cached, and the
whole thing is assembled into `transcript.json`. That makes it **resumable** - a
re-run decodes only missing chunks - and lets a hung chunk be timed out without
losing the rest. The downloaded audio is kept because decoding needs it - an
earlier loudness-ranking signal was tried and removed (see
[Not built yet (later)](Home#not-built-yet-later)
and the
[detection-and-providers skill](https://github.com/jegr78/yt-shorts/blob/main/.claude/skills/detection-and-providers/SKILL.md)
for why). Everything
under `streams/<video_id>/` is derived data: delete the folder to re-download
and re-transcribe.

**Who starts one depends on who is asking.** `bin/yt-shorts detect` still
transcribes as its first step (or reuses the cache): a CLI operator is watching
a terminal and can decide to sit through it. The studio does not — its Detect
button *requires* a transcript that already exists and fails the job if there
is none, rather than silently starting two hours of decode nobody is watching.
In the studio you queue a transcription of its own: the **Transcribe** button
on the channel's Streams tab or on the stream view (see "Jobs" under
[Studio](Studio)).

**The glossary (see "Glossary" under [Subtitles](Subtitles)) reaches this
path too, split across the
chunk boundary.** Only `terms` are handed to each chunk's killable worker, as
the decoder bias for that chunk alone — never `replacements`, which are
applied exactly once, at assembly, over the whole stream's words — see the
[detection-and-providers skill](https://github.com/jegr78/yt-shorts/blob/main/.claude/skills/detection-and-providers/SKILL.md)
for why the split is where it is.
The consequence is two-sided. Because `replacements` apply at assembly, a
glossary edit takes effect on the **next assembly with no re-decode** — cheap,
and immediate for anything a `replacements` edit alone can fix. But a `terms`
edit changes what the decoder is BIASED toward, which only affects a chunk
that has not been decoded yet: any chunk already cached under
`streams/<video_id>/chunks/` keeps the OLD decoder bias until that directory
(or the specific chunk) is deleted and re-decoded from scratch.
