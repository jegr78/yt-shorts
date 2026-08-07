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
archive built for your OS (see "Building a binary" under Development),
unpack it, and run the `yt-shorts` (`yt-shorts.exe` on Windows) it contains
the same way. It bundles Python and every Python dependency; it does not
bundle `ffmpeg`, `ffprobe` or `yt-dlp` — see `install-tools` right below.

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
  [Subtitles](https://github.com/jegr78/yt-shorts/wiki/Subtitles)
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

## Studio

A local, single-user web editor for reviewing a batch of clips after
`harvest` and `render` have run: fix a caption Whisper misheard, correct a
title, mark a clip kept or discarded, and see the result before uploading
anything. Your corrections go into `edit.json`; the studio never edits a
clip's `transcript.json`, and never redefines an existing clip's window in
`clip.json`. Two things it writes besides, both only when you ask: the short
from a render you start (re-rendering replaces that clip's previous short,
which is the point of re-rendering), and a clip made from a window you pick
in the stream view (below) — creating a new one, or, if you re-pick the exact
same window (say, to fix the hook), updating that clip's `clip.json` in
place; it refuses to redefine an existing clip with a genuinely different,
colliding window.

```bash
bin/yt-shorts studio                              # start screen: pick a channel, then an event
bin/yt-shorts studio erf                          # deep-link straight to a channel's event list
bin/yt-shorts studio erf/community-clips-back-catalogue   # deep-link straight into the editor
```

Launch it with no argument to open a **start screen** that lists your
workspace's channels; pick one to see its events, pick an event to open the
editor. A channel or `channel/event` argument is a convenience deep link to
that screen.

On the start screen you can **add a channel** (a directory slug plus its
`channel.json` identity fields — id, channel_url, handle, display name, language,
footer), **edit** those fields, **rename** its slug (the URL moves, and all its
events come along), and **delete** a channel (you type the slug to confirm — this
removes the channel and all its events). Renaming or deleting a channel is refused
while one of its events is rendering.

Opening a channel shows two tabs: its **events** (below) and a **Brand** tab. On
the Brand tab you upload the channel's `.ttf`/`.otf` fonts, assign one to the
**hook** and one to the **small** text, edit the four brand colors (text, base,
accent, edge) and the subtitles on/off toggle, and see a **live preview** of the
overlay as you change them. A newly added channel is incomplete until a font is
uploaded and assigned here — that is what makes its events renderable. Output
dimensions stay at the portrait default and are not editable here; a font that is
currently assigned in the brand cannot be deleted until you reassign it.

An optional **band opacity** section sets how solid the overlay's upper and
lower thirds are — `1` (or omitted) is the usual full-strength look, `0`
leaves nothing there but the clip's own blurred backdrop, with the hook and
footer still drawn on top. Like every other brand setting it can be set
channel-wide and overridden per event, and both the channel's Brand tab and
the event's own brand drawer show a slider for each third. The channel's
Brand tab also has a **"Derive from logo"** button, which reads the
channel's `assets/logo.png` and proposes the four brand colors from it —
a starting point you still review and adjust before clicking Save brand.

On a channel's event list you can **create** a new (empty) event, **rename** an
event, and **delete** one (you type its name to confirm — delete is permanent).
A new event is populated the usual ways: the studio's Streams → detect flow, or
a CLI `harvest`. Renaming or deleting an event that a render or detect is
currently using is refused until that finishes. Inside an event the studio
writes `edit.json`; besides that it writes only a short (from a render you
start, replacing that clip's previous one) and a clip from a window you pick
in the stream view — a new clip directory, or, on an exact re-pick of the
same window, an in-place update of that clip's `clip.json` (a hook/title
correction, never its window). It never edits a clip's `transcript.json` or
`sources.json`, and never redefines an existing clip's window.

A **Settings** page (reached from the start screen) shows a workspace panel — where
your data lives and where it was resolved from (`$YT_SHORTS_DATA`, the default
`~/YT-Shorts-Data`, or the repository fallback), and whether the optional upload
libraries are installed — plus a per-channel connection overview: for each owned
channel you can **connect**, **switch account**, or **disconnect**; render-only
channels are shown as such. Disconnect forgets the local token only (you type the
channel id to confirm); fully revoking the grant stays a manual step in your
Google account settings. The same page holds the workspace's **model provider**
API keys — one row per provider, paste or forget, never shown back — see "Model
providers" below.

Prints the URL and opens it in your default browser. It serves on
`http://127.0.0.1:8765/` when that port is free; if it is busy (a studio you
left running, say) it moves to a free port, prints which one, and opens that
instead of failing. Needs FastAPI and uvicorn, which are **not** required by any other
command — `harvest`, `render`, `gallery` and `migrate` all work in a venv
that never installed them; running `studio` without them prints what to
install (`.venv/bin/pip install fastapi uvicorn`) instead of a traceback.

**The preview needs `raw.mp4`.** The clip's downloaded, caption-free video
is what a live preview is drawn on; it only exists once the clip has been
rendered at least once (`render` keeps it by default — see "One broken
clip never aborts the run" and the wiki's
[Layout](https://github.com/jegr78/yt-shorts/wiki/Layout)). Selecting a clip
that has never been rendered shows an explanation instead of a broken
image, with what to do about it (render it).

**A conflict is shown, never silently resolved.** If a caption correction
was made against a transcript that has since changed (a re-transcription,
say), the studio still uses the correction — same rule as `render`, see the
wiki's [The editorial layer](https://github.com/jegr78/yt-shorts/wiki/The-editorial-layer)
— but shows a banner naming what happened,
because only a human should decide whether it still applies.

**Listing a channel's streams.** `GET /api/streams` returns the channel's
catalogue — its finished streams (title, duration, view count, video id) plus
its playlists, composed by `yt_shorts.youtube.channel_catalogue` from several
`yt-dlp` calls (the streams tab, the playlist list, and every playlist's own
members, fetched in parallel); **no YouTube Data API key** and no quota are
involved (yt-dlp is already the tool's downloader). The catalogue is fetched
once per studio session and cached; `?refresh=true` re-fetches. A failed
playlist fetch is named rather than silently dropped and the rest of the
catalogue is still served; the streams tab's own failure returns a 502 with
an explanation rather than a broken panel, since without it there is no list
at all. Picking a stream to work with lands with moment detection (D2), which
is what a chosen stream feeds into.

The Streams tab filters a channel's streams by its YouTube playlists (a
dropdown, not collapsible groups — every stream still lists in one flat
table, narrowed to the picked playlist). The list is the union of the
channel's Streams tab and every playlist's contents, so a broadcast that
lives only in a playlist is reachable too — on the ERF channel that is eight
videos, two of them multi-hour races. Each row shows whether that stream
already has a transcript and an analysis, and a playlist with dropped
(deleted or private) members says so in the filter itself, e.g. "ERF
Specials (1 + 2 unavailable)", rather than showing a plain count that quietly
excludes them.

Tick several rows and queue them in one action: **Transcribe**, **Detect
moments**, or **Transcribe + detect**, which chains each detection behind
its own transcription so it starts only once there is something to score.
A stream that already has what the action would produce is skipped, and the
bar says how many before you click. That is not because a re-run is
expensive by nature — re-transcribing a stream that already has one
ordinarily reuses its downloaded audio and every cleanly-decoded chunk, so
it costs a metadata call and a re-assembly rather than a re-download, and
re-detecting one that already has a model-backed analysis ordinarily costs
nothing at the model provider, because a scored window is cached too. The
skip exists because neither is *always* cheap — a workspace whose audio or
cached chunks were cleared really does re-download and re-decode, a changed
provider, model or marker set really does spend money again, a re-decoded
transcript (say, after a glossary edit) makes every previously-scored window
a miss, and an analysis produced with no model available at the time (the
lexicon fallback) cached no windows at all, so the first model-backed detect
over it re-scores the whole stream — and a bulk click over many rows should
not gamble on which case it is. Tick "anyway" to override either. A per-row
**Transcribe**/**Detect moments** click, by contrast, always does the work
for that one stream regardless — the skip is the bulk bar's protection
against many rows at once, not a rule that also applies to a single
deliberate click.

Nothing starts on the click: everything goes into the queue, and the Jobs
screen is where the whole plan lives.

**Jobs: the queue and the Jobs screen.** The studio keeps a queue of planned
work in `<workspace>/jobs.json`, and the **Jobs** screen (from the start
screen, or `/jobs`) shows it in three sections: what is running, what is
queued, and what recently finished. The plan survives a restart; what was
running when the studio stopped comes back as `interrupted` and waits for you.

**One studio per workspace.** Two of them share one `jobs.json` and would
overwrite each other's plan, so a second `bin/yt-shorts studio` on the same
workspace refuses and says who holds it. (It picks a free port when 8765 is
busy, so without this it would simply start.) A studio that crashed leaves its
lock behind; the next one takes it over and says so, the same way a stale
render lock is handled.

**Four of the five buttons queue their work now.** Transcribe (a channel's
Streams tab and the stream view — still the only way to get a transcript from
the studio at all, see the wiki's
[Moment detection](https://github.com/jegr78/yt-shorts/wiki/Moment-detection)),
**Detect moments**,
**Render** and **Apply trim** all write an entry into this plan instead of
starting a job on the click, so each of them can be scheduled, reordered,
paused and stopped, and each shows up here.

The consequence to expect: **a click no longer starts anything.** The panel you
clicked in says "Queued — not started yet" and names why — the worker is not
running, another job holds that event's lock, or (for a chained "Transcribe +
detect") the detect entry is still waiting on its own transcription to finish
— instead of showing a spinner for work that has not begun. Queuing itself is
never refused: it takes no lock, so a render can be planned while a detection
is running, and the worker waits for the event rather than failing the entry.

**Upload is the one that still starts directly**, deliberately. It cannot be
stopped at any level, and a non-private or scheduled upload needs a
confirmation given per upload — which an entry written now and run hours later
from a state file cannot carry (`POST /api/jobs` refuses a queued upload that
is not private for exactly that reason). The three direct routes the other
buttons used to call still exist for anything that is not the browser.

How many run at once is per **pool**, not one number: `cpu` (transcribe,
render, trim) defaults to **1** and `net` (detect, upload) to **3**, because a
transcription pins the processor for hours while a detection mostly waits on a
model API. The Jobs screen shows the current limits read-only; the Settings
screen's own "Job queue limits" panel is where you change them, one pool at a
time, and it says what the number does right above the fields rather than in a
tooltip: a limit is **per workspace** — not per event, not per channel — and
raising `cpu` past your machine's own core count makes everything slower, not
faster, because renders and transcriptions start fighting each other for the
same cores instead of running one at a time. Saving writes
`<workspace>/settings.json` **and** re-points the live queue immediately, no
restart needed. Lowering a limit below what is already running never kills or
double-counts anything — the running work finishes normally, and the pool
simply claims no new work until it is back under the new limit.

What each state means:

| state | what it means |
|---|---|
| `queued` | waiting its turn. Its `reason`, if it has one, says what for — most often "waiting for the event lock", i.e. a CLI render or another job is using that event. Normal and temporary |
| `paused` | you paused it before it started; it keeps its place in line, and cannot be reordered until you resume it |
| `running` | started, with a job log of its own under `logs/jobs/` |
| `stopping` | you asked it to stop and it has not reached its safe point yet. It still holds its pool slot, because the work is still using the machine |
| `done` | finished. Drop it from the list when you like |
| `failed` | finished badly, with the reason on the row — **Retry** puts it back in the queue |
| `stopped` | you stopped it. Not a failure — and **Retry** re-queues it, resuming from the first chunk or window nobody reached |
| `interrupted` | it was running when the studio died. It never restarts by itself — only **Retry** re-queues it, because a detection run spends real money |

**Stopping is an ask, not a switch.** A stop takes effect at the kind's own
safe point — after the current chunk, clip, cut or window — which for a long
chunk can be minutes away, and the screen says so before you click. A **hard
stop** is offered where it is safe: it terminates the subprocess the work is
waiting on, never the Python thread, so the job still runs its own cleanup and
reports "stopped" — that is what stops a cancel from leaving a half-written
short behind. **An upload cannot be stopped at any level** and no button is
offered for one: a half-finished upload to YouTube is worse than waiting.

**What costs money, and what a stop costs.** Only **detect** spends money (the
model API — see the wiki's
[Model providers](https://github.com/jegr78/yt-shorts/wiki/Model-providers));
**upload** spends YouTube API quota; the
rest cost time and CPU. Stopping either long kind costs nothing to re-run:
transcription resumes at the first missing chunk and detection at the first
unscored window, because both cache their own unit of work. What a stopped
detection does lose is the *rest of the stream* — nothing is scanned after the
stop, and no analysis file is written at all until a run completes, so re-run
it when you want the whole picture.

Two things worth knowing. The queue only moves while `bin/yt-shorts studio` is
running (any other way of building the app leaves the worker stopped, and the
screen says so rather than looking idle). And **progress is reported by the
three long kinds and by no others**: a running transcription says "chunk 20 of
50", a detection "window 3 of 9" and a render "clip 2 of 6", each advancing as
its own unit of work finishes. A trim is a single cut and an upload's bytes go
somewhere the tool does not watch, so those two rows show nothing at all rather
than an invented "1 of 1" — and no row shows a reading before its first unit is
done, or after it has stopped running. Use a job's log for anything finer.

Only one job (studio- or CLI-started) can work on an event at a time — the
same `EventLock` `render` itself uses. A queued entry whose event is locked
waits and says so; it is never failed for it. The *plan* is what survives a
restart, not a running job's own progress record: a job's own record (its
per-clip results) lives in memory and is gone when the studio process is,
though its log under `logs/jobs/` stays and the entry itself is still in the
plan afterwards. An upload, which starts directly, has no entry at all — close
the studio mid-upload and there is nothing but its log.

**The frontend is built, not committed.** `src/yt_shorts/studio/web/` is
a React + Vite + Mantine (TypeScript) project; `src/yt_shorts/studio/api.py`
serves its *built* output from `src/yt_shorts/studio/static/`. That directory
is git-ignored: the release binary and a `pip`-installed wheel each build it
on the way in, so an operator never needs Node — a developer working from a
clone needs `^22.22.2 || ^24.15.0 || >=26.0.0` of it (see CONTRIBUTING.md):

```bash
cd src/yt_shorts/studio/web
npm install
npm run build          # typechecks, then writes into ../static/
npm test               # Vitest unit tests (see below)
```

The frontend has unit tests (Vitest, jsdom) covering its pure logic — the
duration formatters (`format.ts`), the effective-window reconstruction
(`window.ts`), word equality (`words.ts`), upload-url extraction, the
brand form's hex/ready-to-save/font-filename rules (`brand.ts`), the Jobs
screen's state labels, allowed actions and stop warnings (`jobs.ts`), and the
job-polling hook. Run them with `npm test`. They are a **required check before
committing a frontend change**, alongside `npm run build`, and are **separate**
from the Python `pytest` suite (a JS runner is not folded into it — the same way
`npm run build` is separate). The integrated flows stay covered by the Playwright
E2E tests inside the `pytest` suite; Vitest complements those, it does not replace
them.

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

### Building a binary

```bash
.venv/bin/pip install pyinstaller
PYTHONPATH=src .venv/bin/python tools/build-binary.py --version v0.1.0-local
```

Produces `dist/bin/yt-shorts/` — a directory, not a single file. The bundle
carries ctranslate2, onnxruntime, av and numpy, and a one-file build would
unpack all of it to a temp directory on every invocation.

It builds the studio's frontend first if `studio/static/` is empty, so this
needs Node; an existing build is reused as is, which is how CI hands the same
bundle to every job.

The build is not finished until its own smoke test passes: the binary must
report its version, run `doctor` and `install-tools` without raising, and serve
the studio's SPA plus one API route. That last check is what catches a
`studio/static/` missing from the bundle — otherwise the studio serves a blank
page and says nothing about why.

`ffmpeg`, `yt-dlp` and the Whisper models are **not** bundled. Run
`yt-shorts install-tools` on the target machine.

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
