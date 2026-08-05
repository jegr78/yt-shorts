# Stage D1 — Stream discovery

**Date:** 2026-07-22
**Scope:** list a channel's streams so an operator can pick one to work with. No
transcription, no moment detection — that is D2. This stage only answers "which
stream", replacing a manual copy of a URL.

## Problem

Two of the tool's entry points make a human find a video by hand. The
community-clips flow copies clip addresses from the channel page in the browser
console. The moment-detection flow that D2 will add needs a stream's video ID.
Neither has a way to see what a channel has published without leaving the tool.

## Why yt-dlp, not the YouTube Data API

An earlier draft of this design reached for a YouTube Data API key. That was
wrong, and measuring it made that obvious. The tool already depends on yt-dlp
for every download, and a single yt-dlp call lists a channel's streams with
everything this stage needs. Verified against the real ERF channel:

```
$ yt-dlp --flat-playlist --dump-json ".../channel/<id>/streams"
{'id': 'xQlD7MkC-Eo', 'title': 'ERF 24h Nürburgring 2026 | Part 3',
 'duration': 28431, 'view_count': 1800, 'live_status': 'was_live'}
```

- `id`, `title`, `duration` (seconds, no ISO-8601 parsing) — exactly the fields
  the list needs.
- `live_status: was_live` distinguishes a finished stream from a normal video,
  the filter an API approach would have needed extra work for.
- `view_count` comes for free, and view counts are the metric this whole
  project set out to improve — useful when deciding which stream to mine.

Against the API-key alternative, yt-dlp wins on every axis that matters here: no
Google Cloud project, no key to store, no daily quota to watch, one dependency
instead of two, consistent with racecast (which already uses yt-dlp with
cookies), and — with cookies — it would even see unlisted or private uploads,
which an API key cannot. This project's streams are public, so cookies are not
required, but the door is not closed the way the API-key path would close it.

The one thing lost: `--flat-playlist` does not reliably return an upload date.
The `/streams` tab is already newest-first, so ordering does not need it, and a
precise date is not worth resolving each video individually (which
`--flat-playlist` exists to avoid).

## What it does

Given a channel, run one `yt-dlp --flat-playlist` against its `/streams` tab and
return the streams — id, title, duration, view count — in the tab's natural
newest-first order, so an operator recognises a stream and selects it.

## Architecture

A new module `src/yt_shorts/youtube.py`:

- `list_streams(channel_url, *, runner=...) -> list[Stream]`, where `Stream`
  carries `video_id`, `title`, `duration_seconds`, `view_count`. `runner` is
  the subprocess boundary, injected so the JSON parsing, field extraction and
  ordering are tested against recorded `yt-dlp --dump-json` output without the
  network.
- The channel URL comes from `channel.json`'s existing `channel_url` field, with
  `/streams` appended. No new configuration.
- One malformed JSON line does not sink the list — the same per-entry isolation
  the rest of the tool uses. A yt-dlp failure (channel gone, network down) is
  reported as an understandable `YouTubeError`, not a raw traceback.
- Parsing is defensive about missing fields: a stream without a duration or view
  count still lists, with that field blank, rather than being dropped.

## Studio integration

- `GET /api/streams` — the list from `list_streams`, cached in memory for the
  session so paging the UI does not re-run yt-dlp. A refresh control re-fetches
  deliberately.
- The page shows streams as a dense list — title, duration and view count in
  tabular figures — consistent with the clip list. Selecting a stream records
  the choice into the event so D2 has an input and the choice survives a reload.
  What "records the choice" feeds into is D2's concern.
- yt-dlp missing or failing shows an explanatory state, not a broken panel.

## Multiple channels and ownership

An operator works with several channels — their own, managed ones, a brand
channel. For this stage they are identical, and none of it depends on owning
the channel:

- **Downloading and listing do not depend on ownership.** yt-dlp reads any
  *public* channel's streams and downloads any public video, regardless of whose
  channel it is — which is why the community-clips flow already works on videos
  the operator does not own. Ownership matters only for upload (stage E).
- Each channel is its own `channels/<name>/` folder with its `channel_url`.
  Channel selection is the folder you are already working in
  (`bin/yt-shorts ... erf/...`); there is no flat list of channels to filter.
- **This project's streams are public.** Unlisted/private streams would need
  yt-dlp cookies; that is a small future addition (a cookies path yt-dlp
  already supports), not a redesign, and out of scope here.
- The "switch account" experience from YouTube Studio is an OAuth concept and
  belongs to stage E, where upload requires OAuth anyway.

## Testing

- `list_streams` against recorded `yt-dlp --dump-json` output through the
  injected `runner`: fields extracted correctly, newest-first order preserved,
  a malformed line skipped without sinking the rest, a missing duration/view
  count tolerated.
- Error paths: yt-dlp exiting non-zero, and an empty result — each an
  understandable outcome, none a raw traceback.
- The `/api/streams` route: success against a stubbed `list_streams`, the
  yt-dlp-unavailable state, and the in-session cache not re-running yt-dlp.
- Nothing else in the pipeline changes; this stage only adds a capability.

## Not in scope

- Transcription, loudness, moment detection, windows — all D2.
- Cookies for unlisted/private streams — a later addition if needed.
- OAuth and the account-switch experience — stage E.
- Listing regular videos as well as streams — the `/streams` tab is what this
  stage needs; a `/videos` variant is a trivial later addition if wanted.
