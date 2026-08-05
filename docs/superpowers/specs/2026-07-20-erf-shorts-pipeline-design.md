> **Historical record.** This document describes the state of the project on
> 2026-07-20, when it was still called `ERF-Shorts`, lived under
> `~/ERF-Shorts`, served a single channel, and used German identifiers
> throughout. Since then the project has been renamed to YT-Shorts and moved
> to `/Users/jegr/Documents/github/YT-Shorts`, rebuilt for multi-channel
> operation with `channels/<name>/`, and fully converted to English. The
> decisions, measurements, and rationale below are left as they were recorded.

# ERF Shorts — Design

**Date:** 2026-07-20
**Channel:** Endurance Racing Federation (@ERFofficial), UCb3S2oA7lANdg5IS0QtF46w

## Problem

The channel has 1.03k subscribers and 93 videos, almost all of them 8-hour
live VODs. The Videos tab contains only two real uploads; Shorts don't exist
at all. Live VODs are barely surfaced by the algorithm to non-subscribers —
the channel therefore essentially only reaches people who already know it.

The goal is a repeatable way to produce shorts from the existing raw material
(100+ hours). The channel pursues all three purposes at once: new drivers for
the league, viewers for the live streams, general channel growth.

## Constraints

- Source material is exclusively the public YouTube VODs. No local raw
  recordings, no sim replays.
- Operated as semi-automatic, ongoing: the tool produces drafts, a human
  reviews and uploads. Target 3–10 shorts per event at ~30 min effort.
- Picture format: letterbox. 16:9 picture unchanged, centered, hook line on
  top, ERF branding at the bottom. No cropping, because the timing tower and
  leaderboard are burned into the picture and a center crop would destroy
  them.
- Hardware: Apple M3, 16 GB. ffmpeg, yt-dlp, Python 3 available.

## Measurements (2026-07-20, basis for the decisions)

Checked before the design, to rule out assumptions:

| Signal | Result | Consequence |
|---|---|---|
| Live chat spikes | 462 messages over 8h20 = 0.9/min; 57% of all minutes empty | Discarded — noise, no signal |
| "Most replayed" heatmap | Not delivered for the VODs | Discarded |
| Community clips | 6 findable, yt-dlp resolves exact sections (example: 2923.28s → 2948.805s) | Used |
| Comment track | Consistently present, one English audio track | Used |
| Chapters in the VOD | None | — |

Additional finding: `yt-dlp --download-sections` only downloads the desired
time range. A 20-second clip from an 8-hour stream costs seconds and a few
MB. Full downloads are never necessary.

## Architecture

Four decoupled steps:

1. **Harvest** — two independent sources deliver timecodes: community clips
   (human-curated) and the comment transcript.
2. **Review** — judgment step, runs in the Claude Code session, no script.
   Result is `kandidaten.json`.
3. **Render** — for each candidate, load the clip and prepare it as
   1080×1920.
4. **Accept** — HTML overview of all drafts for review.

Load-bearing decision: **the machine does the mechanical work, judgment stays
with the human.** Hence no API key, no service, no ongoing costs.

Second load-bearing decision: **harvest and render don't know about each
other.** Only `kandidaten.json` sits between them. That makes it possible to
later attach a third harvest source (visual director-cut detection) or a
different output type (highlight video) without changing existing parts.

## Building blocks

### ① Clip harvest
**Task:** clip URLs → `clips.json` with timecodes and titles.
**Dependency:** yt-dlp.
**Open question:** yt-dlp reliably returns start/end, but not directly the ID
of the source video. Irrelevant for rendering (the clip URL is loaded
directly); only extending a too-short clip needs an additional mapping. To be
clarified during the build.

### ② Transcription
**Task:** video ID → text with word-level timecodes.
Loads only the audio track (~150 MB), transcribes locally via mlx-whisper
(Metal). Result is cached: once per stream, evaluable any number of times
afterwards.
**Dependency:** yt-dlp, mlx-whisper (the only new install).

### ③ Review
**Task:** transcript → `kandidaten.json`. No script.
Format — the only interface between harvest and render:

```json
{ "quelle": "Esm9vv5-PdU", "start": "03:18:42", "ende": "03:19:04",
  "hook": "24 Stunden. Zwei Sekunden Vorsprung.",
  "saeule": "spannung", "untertitel": true }
```

### ④ Renderer
**Task:** one candidate → one finished short (1080×1920).
Loads only the clip plus lead-in/lead-out via `--download-sections`, places
the 16:9 picture unchanged in the middle, hook on top, branding at the
bottom, subtitles burned in. Knows neither Whisper nor clips.
**Dependency:** yt-dlp, ffmpeg.

### ⑤ Review overview
**Task:** drafts → one HTML page to click through.

### Storage layout
```
~/ERF-Shorts/
  marke/            logo, font, colors
  events/<event>/   clips.json, transkripte/, kandidaten.json, entwuerfe/
```

## Brand

Source is the broadcast runtime `/Users/jegr/racecast/runtime/erf-nls`, not
reinvented. The shorts are meant to look like the stream they come from.

**Colors** (read from `graphics/Standby.png`):

| Hex | Role |
|---|---|
| `#FFFFFF` | text and logo, dominant |
| `#004625` | ERF green, deep |
| `#144E53` | petrol dark tone, surfaces |
| `#B8F5CA` | mint, accent |

**Motif:** slanted parallelograms at the picture edges — the recognizable
mark of the broadcast graphics. Picked up in the portrait-format bars at the
top and bottom.

**Font:** library under `/Users/jegr/racecast/runtime/fonts/`.
The broadcast graphics' display font is not included there (presumably
licensed); the closest candidates are `Oswald-Bold` and
`BarlowCondensed-Bold`. The choice is made on the rendered picture, not on
paper.
The files are in `.woff2`, ffmpeg needs `.ttf`/`.otf` — a one-time conversion
is needed.

**Further assets:** `graphics/*.png` (1920×1080 broadcast graphics),
`brands/*.png` (manufacturer logos), `media/intro.mp4`, `media/outro.mp4`.

**Language:** hooks and subtitles consistently in English — matching the
comment track and the reach.

Subtitles are not optional: shorts are watched mostly muted.

## Error handling

- **yt-dlp fails** (video private, rate limiting, format change): the
  affected candidate is skipped and reported collectively at the end. One
  broken candidate must never abort a run over ten others.
- **Clip not resolvable:** noted with a reason in `clips.json` instead of
  silently omitted.
- **ffmpeg error:** the command line and error output of the affected
  candidate are preserved, so the call can be traced by hand.
- **Transcription aborts:** save the partial result; resuming must not start
  from hour zero.

## Testing

Little can be checked automatically here — the result is a video a human has
to judge. Hence:

- **Reference candidate:** a fixed clip with known timecodes as a test case.
  The renderer must reproducibly produce the same file from it (dimensions
  1080×1920, expected duration, audio present).
- **Timecode arithmetic** (lead-in/lead-out, format conversion, edge cases at
  video start and end) is pure logic and is tested on its own.
- **Visual acceptance** via the overview page from ⑤, not via assertions in
  code.

## Build order

1. **①+④** — produce six finished shorts from the six existing community
   clips. The fastest honest test of whether the format lands, without any
   Whisper at all. At this stage there are no subtitles yet (those come from
   ②); the hook is the title the clipper gave the clip, touched up by hand if
   needed. The renderer therefore treats subtitles as switchable from the
   start.
2. **②+③** — transcription and review, so the candidates no longer depend on
   the random set of existing community clips.

## Out of scope here

- Visual director-cut/replay detection (Option B). Deferred: high effort,
  calibration per season layout, many false positives. Attachable via
  `kandidaten.json`.
- Automatic upload. Deliberately not — on a branded channel a human must sign
  off.
- Highlight videos for the Videos tab. A separate effort, but draws on the
  same candidate list.

## Two notes outside the technical scope

1. **The strongest lever isn't technical:** actively ask the community to
   clip (stream, Discord, video description). Then the audience curates, and
   ① just harvests. 6 clips a year can become 30 per event.
2. **Shorts views count separately** and, by experience, convert weakly into
   long-form viewers. For channel growth they work immediately; for live
   stream viewers only indirectly. The more direct lever for that would be
   the practically empty Videos tab.
