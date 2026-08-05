# Stage 2a — Subtitles from the commentary track

**Date:** 2026-07-20
**Scope:** subtitles for existing shorts. Moment detection from full streams
(the rest of stage 2) is a separate design.

## Problem

Shorts are watched muted more often than not. The commentary on these clips
carries the moment — without subtitles it is lost.

## Measurements (2026-07-20, the basis for the decisions below)

Three real drafts were transcribed before any design work:

| Clip | Commentary |
|---|---|
| rei got sliced | "…and oh no he's gone flying off into the shadow realm" |
| WHAT IS HAPPENING?!? | "No, what is happening to the Porsche over there? No, no, no…" |
| Jegr and the Barbie | "oh my god yeah I know oh my god oh my god oh my god" |

Three findings:

1. **The commentary carries.** This is usable short material, not filler.
2. **The clip title is quoted from the commentary.** The clipper of
   "WHAT IS HAPPENING?!?" wrote down what the commentator shouted. That is the
   strongest available evidence that moment detection will find something in the
   transcript later: viewers have already demonstrated it.
3. **Speed:** 15–26 s of audio in 3–5 s, on CPU, with the `small` model.

And one finding that shapes the design:

```
[0.00-14.62]  "It drives quite slow and it's very easy to pick up a penalty
               as that is Ray Ray so is he going to go for the double overtake
               again and oh no he's gone flying off into the shadow realm"
```

A single 14.6 s segment of 33 words — useless as a subtitle. Word-level
timestamps are available (40 words, median word duration 0.26 s), but the gaps
between words are all 0.00 s, so pauses cannot be used to split. Grouping has to
run on word count, elapsed time and punctuation.

## Constraints

- ffmpeg here is built without `libfreetype` and `libass`: no `drawtext`, no
  `subtitles` filter. It is not reinstalled — racecast depends on this binary.
  Verified available: `overlay` (timeline-capable), `concat` and `image2`
  demuxers, `qtrle` with `argb` for an alpha video track.
- Subtitles are off by default and switchable per channel and per event.
- The six existing shorts must stay byte-identical while subtitles are off.

## Architecture

Subtitles enter the picture as their own timed layer, alongside the brand
overlay rather than inside it.

```
1. TRANSCRIBE   audio of the finished excerpt -> words with timestamps, cached
2. GROUP        words -> groups of 2-4 words          (pure logic)
3. BUILD TRACK  one PNG per group -> concat -> .mov with alpha
4. COMPOSE      background -> sharp picture -> brand overlay -> subtitle track
```

**Grouping is pure logic with no I/O.** It is where the quality is decided, so it
has to be testable without Whisper, without ffmpeg and without network, using
invented word lists — the same reasoning that made `timecode.py` a separate
module.

**Appearance comes from the profile**, like everything else, and inherits the
existing event-over-channel layering for free.

## Components

### ① `transcribe.py` — video file → words with timestamps
Extracts the audio via ffmpeg (16 kHz mono, what Whisper expects), runs
`faster-whisper` with word timestamps, writes `transcripts/<slug>.json`. Cached:
once per clip, so re-rendering costs no transcription.
**Depends on:** ffmpeg, faster-whisper.

### ② `captions.py` — words → caption groups
Pure logic. Three rules: at most N words, at most T seconds, and a sentence-ending
punctuation mark closes a group early.
**Depends on:** nothing.

### ③ `subtitle_track.py` — caption groups → transparent track
Draws one PNG per group and builds an alpha `.mov` from them via `concat` with
per-image durations. Calls `overlay.build_caption` for the typography so font
logic lives in one place.
**Depends on:** Pillow, ffmpeg.

### ④ `overlay.build_caption(text, config) -> Image`
A 1080x1920 RGBA image, transparent everywhere except the caption in the lower
band. Reuses the existing wrapping and size-search used for the hook, with a
different target area. `build_overlay` is unchanged.

### ⑤ `render.compose` accepts an optional subtitle track
One more `overlay` step at the end of the chain, after the brand overlay and
**before** `setsar=1`. With no track the chain is character-for-character the one
in use today.

### Profile

```json
"subtitles": {
  "enabled": false,
  "max_words": 3,
  "max_seconds": 1.6,
  "size": 78,
  "y": 1290
}
```

Colour and font come from the existing `colors.text` and `fonts.hook` — no second
set of brand values that can drift apart.

## Choices and the alternatives rejected

**Transparent track, not one `overlay` per group.** `overlay` is timeline-capable,
so `enable='between(t,a,b)'` per group would work — but a 60 s short with
three-word groups produces 60–80 chained filters, and the chain grows with clip
length. The track keeps the chain as short as it is today regardless, and can
carry any timed graphic later, not just text.

**`faster-whisper`, not `mlx-whisper`.** The stage 1 design named mlx-whisper.
It pulls in torch (~2 GB). `faster-whisper` was measured on the real clips at
3–5 s for 15–26 s of audio on CPU, which is ample at clip length. When full
8-hour streams need transcribing, `whisper.cpp` via Homebrew is the upgrade:
Metal-accelerated, no Python dependencies at all.

**Transcribe the excerpt, not the stream.** The stage 1 design assumed the
8-hour track because it treated subtitles and moment detection as one job. For
subtitles alone the 15–60 s already on disk are enough.

**Word groups, not karaoke highlighting.** Karaoke needs one image per word
rather than per group, and a recognition error stays on screen longer. Groups
are also more robust: a wrong word passes by.

## Error handling

- **No speech detected** — not an error. The short is produced without subtitles
  and this is reported. For an engine-noise-only clip that is the correct result.
- **Transcription fails** — affects that one candidate; the others still run.
  Same isolation as everywhere else in the tool.
- **Model missing on first run** — `faster-whisper` downloads it once (~150 MB
  for `small`). This belongs in the README, or the first slow run is a surprise.

## Testing

| Unit | How |
|---|---|
| `captions.py` | Fully, with invented words: empty list, one very long word, punctuation mid-group, a group breaking at the time limit |
| `build_caption` | Pixel measurement: no text pixel inside the video window, none in the footer, none beyond the side margins |
| Track | `ffprobe`: alpha present, total duration equals the last group boundary |
| End to end | One real clip; extract a frame at a timestamp with known text and assert white pixels in the caption area |
| Regression | The six existing shorts stay byte-identical while subtitles are off |

## Build order

**② first.** It is the only component fully testable without Whisper, ffmpeg or
network, and the only one whose rules should be argued over at the result before
the rest is built on top. Then ④, ③, ①, ⑤.

## Not in scope

Moment detection from full streams, and the hooks that would come from it. That
needs the 8-hour transcription path and its own design. This design deliberately
builds the transcription and caption machinery it will reuse.
