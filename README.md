# YT-Shorts

Turns YouTube live streams from sim-racing leagues into finished
portrait-format shorts that only need to be reviewed and uploaded.
The tool serves multiple channels: brand (colors, fonts, dimensions,
footer) lives per channel in profile files, and the channel-specific
accent element is an optional module.

**Stage 1 (current state):** the source is viewer-created community
clips. The title the clipper gave a clip becomes the short's hook.

## Installing

This project is not yet published to PyPI, so install it from a checkout
(`git clone`, then run one of these from the repo root). Either gives you a
`yt-shorts` command on `PATH` — everywhere else in this README,
`bin/yt-shorts <command>` (this repo's own dev entry point, which re-execs
into the `.venv` beside it, so it runs from any clone that has one) is the
same command as `yt-shorts <command>` once installed one of these ways:

```bash
pipx install .                 # isolated, recommended for everyday use
pip install ".[all]"           # into your own venv; [all] adds transcription
                                # and every cloud moment-detection provider
```

**Both of these need Node on `PATH`.** They install from this checkout, and the
studio's frontend is built rather than committed — so the install runs `npm ci
&& npm run build` on the way in (`hatch_build.py`; an existing
`src/yt_shorts/studio/static/` is reused as is, and a missing `npm` fails the
install with that instruction). This is the price of installing from source; it
does not apply to the binary below, which ships the built page.

There is also a per-OS binary that needs no Python at all: download the
archive built for your OS (see the wiki's
[Building from source](https://github.com/jegr78/yt-shorts/wiki/Building-from-source)),
unpack it, and run the `yt-shorts` (`yt-shorts.exe` on Windows) it contains
the same way. It bundles Python and every Python dependency; it does not
bundle `ffmpeg`, `ffprobe` or `yt-dlp` — see `install-tools` right below. On
macOS it arrives quarantined and unsigned — see the wiki's
[If something goes wrong](https://github.com/jegr78/yt-shorts/wiki/If-something-goes-wrong).

## Requirements

Python 3.12 or newer, and three external tools on your PATH:

| Tool | Why |
|---|---|
| `ffmpeg` | every render — the picture is composited with `overlay`, `boxblur`, `scale` and `setsar` |
| `ffprobe` | ships with ffmpeg; used to verify durations and the rendered result |
| `yt-dlp` | every download, and stream discovery |

The tool installs and updates them for you:

```bash
yt-shorts install-tools            # winget / brew / apt / pacman, whichever this machine has
yt-shorts install-tools --update   # run this before a session — yt-dlp goes stale in weeks
yt-shorts doctor                   # what is installed, what works, what to fix
```

On Linux, `yt-dlp` is a pinned, checksum-verified download into
`<workspace>/.tools/` rather than an apt package: apt's version lags far enough
behind upstream that it no longer passes YouTube's bot check. The CLI puts that
directory on `PATH` itself, so nothing else needs configuring.

## Workflow after a race weekend

Every command takes an identifier of the form `<channel>/<event>`,
e.g. `erf/community-clips-back-catalogue` — `<channel>` is a folder name
under `channels/`, `<event>` a folder name under that channel's `events/`.

### 1. Collect clip titles and addresses

The only step done by hand — YouTube offers no interface to list a
channel's clips.

Scroll to the "Top community clips" row on the `channel_url` recorded in
`channels/<channel>/channel.json`, then in the browser console:

```js
JSON.stringify([...document.querySelectorAll('a[href*="/clip/"]')]
  .map(a => {
    const card = a.closest('ytd-grid-video-renderer, ytd-rich-item-renderer, ytd-video-renderer');
    const title = card?.querySelector('#video-title, h3')?.textContent?.trim();
    return { url: a.href.split('?')[0], hook: title || '' };
  })
  .filter(e => e.hook)
  .filter((e, i, all) => all.findIndex(x => x.url === e.url) === i), null, 2)
```

Save the result to `channels/<channel>/events/<event>/sources.json`.

### 2. Resolve timecodes, render, review

```bash
bin/yt-shorts harvest <channel>/<event>    # sources.json -> clips/<clip>/clip.json with timecodes
bin/yt-shorts render  <channel>/<event>    # clip.json -> clips/<clip>/short.mp4
bin/yt-shorts gallery <channel>/<event>    # the clip store -> index.html
open channels/<channel>/events/<event>/index.html
```

For the ERF channel, for example:

```bash
bin/yt-shorts render erf/community-clips-back-catalogue
```

Then review, discard, upload. The upload is deliberately manual: on a
branded channel, a human signs off.

**Do not chain the three commands with `&&`.** `harvest` and `render`
deliberately return exit code 1 as soon as *one* clip fails — the rest
still run through. Chaining with `&&` would abort the run at exactly
the case the tool is built to survive.

## What the tool guarantees

- **One broken clip never aborts the run.** Errors are isolated per
  entry, collected, and written to the error output with a reason at
  the end. One exception: the Whisper decode behind subtitles has no
  timeout and can in principle hang indefinitely — see the wiki's
  [If something goes wrong](https://github.com/jegr78/yt-shorts/wiki/If-something-goes-wrong)
  for what to do if a run appears stuck.
- **The picture is never cropped.** The timing tower and leaderboard are
  burned into the source material; a center crop would destroy them.
  The picture is instead fitted into a fixed window, with the rest of
  the frame showing the same picture blurred.
- **A second `harvest` run never destroys good data.** Entries already
  resolved without error are carried over unchanged; only missing and
  failed entries are queried again. To force a re-resolve for a clip:
  delete its `clip.json` (its whole directory works too, see "Removing
  a clip" on the wiki's
  [The editorial layer](https://github.com/jegr78/yt-shorts/wiki/The-editorial-layer))
  and run `harvest` again.
- **Removing a clip from `sources.json` does not delete it.** A clip's
  directory is never deleted by any derivation step — only its owner (a
  human) removes it. `harvest` reports a clip that has fallen out of the
  source list as a `NOTE:` instead, naming what to do about it — see
  "Removing a clip" on the wiki's
  [The editorial layer](https://github.com/jegr78/yt-shorts/wiki/The-editorial-layer).
- **No stale material.** Temporary files are removed before loading and
  cleaned up after a successful build. On failure, they are left in
  place for troubleshooting. One exception, deliberate: `render` keeps
  each clip's downloaded `raw.mp4` (see the wiki's
  [Layout](https://github.com/jegr78/yt-shorts/wiki/Layout)) instead of
  deleting it — it is the clean, caption-free frame a local preview draws
  on before a re-render. The disk cost is roughly the size of the
  downloaded source clip, per rendered clip. Deleting a clip's `raw.mp4`
  by hand is always safe: the next `render` run just re-downloads it.
- **A trimmed clip keeps two full renders on disk, the same trade-off as
  `raw.mp4` above.** Cutting a rendered short always reads the untrimmed
  master (`short.full.mp4`), never the current `short.mp4`, so a second
  correction ("trim 3s, then change your mind and trim 5s") lands 5s off
  the original rather than compounding on the first cut. The cost is a
  second full-length copy of the short sitting beside the deliverable for
  as long as a trim is applied; reverting the trim to zero removes it, and
  a re-render replaces it with the fresh composition.
- **An unknown channel or unknown event produces an understandable
  message**, not a raw traceback.

## Development

```bash
PYTHONPATH=src .venv/bin/pytest -q
```

Upload (see the wiki's
[Upload](https://github.com/jegr78/yt-shorts/wiki/Upload)) needs two extra
libraries, optional exactly like
FastAPI — every other command works without them:

```bash
.venv/bin/pip install google-api-python-client google-auth-oauthlib
```

Moment detection (see the wiki's
[Moment detection](https://github.com/jegr78/yt-shorts/wiki/Moment-detection))
needs the SDK of whichever
model provider the channel uses — all three optional, all lazily imported,
none required by anything else in this project:

```bash
.venv/bin/pip install anthropic      # the default provider
.venv/bin/pip install google-genai   # only if a channel uses Gemini
.venv/bin/pip install openai         # only if a channel uses OpenAI
```

Without the SDK (or without a key), detection still runs, but falls back to the
weaker offline lexicon engine instead of failing — see the wiki's
[Moment detection](https://github.com/jegr78/yt-shorts/wiki/Moment-detection)
for how the fallback is reported and
[Model providers](https://github.com/jegr78/yt-shorts/wiki/Model-providers)
for where each key goes.

The test suite never touches your workspace: `tests/conftest.py` points
`profile.CHANNELS_DIR` at `tests/fixtures/channels/` for the whole session,
autouse, so it runs identically whether `~/YT-Shorts-Data` exists or not.
`tests/fixtures/channels/erf/` is the suite's own copy of the ERF channel
(`channel.json`, `brand.json`, `glossary.json`, `layout.py`, `fonts/`, and
the source list for `community-clips-back-catalogue` — nothing derived) and
is unrelated to whatever ERF data lives in your actual workspace.

**The studio's end-to-end tests** (`tests/test_studio_e2e.py`) drive the
real built page in a real Chromium browser via Playwright, against a real
local server seeded with a temporary event — nothing under
`~/YT-Shorts-Data`. They need that browser installed once:

```bash
.venv/bin/pip install pytest-playwright
.venv/bin/python -m playwright install chromium
```

Without it, that file's tests are **skipped** with a clear reason (not
failed) — a fresh clone must not be blocked by a missing browser download.

### Building a wheel or a binary

Both — and what each one needs, and what neither of them bundles — are on
the wiki's
[Building from source](https://github.com/jegr78/yt-shorts/wiki/Building-from-source).

The design and implementation plan live under `docs/superpowers/`.

## Not built yet (later)

Making a video public, scheduling, thumbnails, playlists, and deleting an upload
stay manual in YouTube Studio after you review the private upload. Live-chat
activity as an extra moment signal is a possible later addition; moment detection
currently scores transcript evidence only (by model, or by the offline lexicon
fallback) — an earlier loudness-ranking signal was tried and removed, see the
[detection-and-providers skill](.claude/skills/detection-and-providers/SKILL.md)
for why. The studio picker for turning a
detected moment (or a hand-picked window) into a clip is the stream view — see
the wiki's
[Moment detection](https://github.com/jegr78/yt-shorts/wiki/Moment-detection)
— so that item is done, not outstanding.
