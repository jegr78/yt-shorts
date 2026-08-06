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
  timeout and can in principle hang indefinitely — see "Subtitles" below
  for what to do if a run appears stuck.
- **The picture is never cropped.** The timing tower and leaderboard are
  burned into the source material; a center crop would destroy them.
  The picture is instead fitted into a fixed window, with the rest of
  the frame showing the same picture blurred.
- **A second `harvest` run never destroys good data.** Entries already
  resolved without error are carried over unchanged; only missing and
  failed entries are queried again. To force a re-resolve for a clip:
  delete its `clip.json` (its whole directory works too, see "Removing
  a clip" below) and run `harvest` again.
- **Removing a clip from `sources.json` does not delete it.** A clip's
  directory is never deleted by any derivation step — only its owner (a
  human) removes it. `harvest` reports a clip that has fallen out of the
  source list as a `NOTE:` instead, naming what to do about it — see
  "Removing a clip" below.
- **No stale material.** Temporary files are removed before loading and
  cleaned up after a successful build. On failure, they are left in
  place for troubleshooting. One exception, deliberate: `render` keeps
  each clip's downloaded `raw.mp4` (see the layout tree below) instead of
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

## Layout

The tool's repository holds code; a **workspace** holds channels, events,
clips and everything derived from them — see "Where the data lives" below.

```
YT-Shorts/                      the repository: code only
  bin/yt-shorts              command line
  src/yt_shorts/
    workspace.py               resolves where the data lives
    clipid.py                   a clip's identity: its source URL
    clipstore.py                 one clip, one directory
    editorial.py                  hand-made corrections, additive over derived data
    migrate.py                     copies an old-layout event into a workspace
    timecode.py                     time arithmetic (pure logic)
    harvest.py                       clip addresses -> timecodes (calls yt-dlp)
    render.py                          loads the clip and composes it with the overlay
    overlay.py                          the brand overlay as a PNG (Pillow)
    gallery.py                           overview page for review
    brand.py                              loads a brand.json, makes font paths absolute
    merge.py                               deep_merge: event overrides channel, key by key
    profile.py                              resolves 'channel/event', layers event over channel
    preview.py                               a preview PNG at a timestamp, from raw.mp4
    job_queue.py                             the job queue: order, pools, jobs.json (pure)
    cancel.py                                 a cancellation token, and how a stop reaches a subprocess
    studio/                                   the local editor - see "Studio" below
      api.py                                    FastAPI app: clips, edit.json, render jobs
      jobs.py                                    background jobs the studio starts and polls
      worker.py                                   the one thread that drives the queue
      web/                                       source: React + Vite + Mantine (TypeScript)
      static/                                    BUILT output `npm run build` writes - not committed
  templates/example-channel/     copy into a workspace to start a new channel
    channel.json                   placeholder values, see the template's own README.md
    brand.json                       placeholder values
    README.md                          what to change and where to put fonts
  tests/fixtures/channels/erf/   the ERF channel as a test fixture, owned by the
                                    suite (see tests/conftest.py) - not a channel to
                                    render from; a stand-in the tests own

~/YT-Shorts-Data/                the workspace (or the repository's own
  channels/                        channels/, until a workspace exists -
                                    empty/absent in a fresh checkout)
    erf/
      channel.json             channel ID, handle, language, footer, display name
      brand.json                 colors, fonts, output dimensions
      fonts/                      this channel's fonts
      assets/                      optional: this channel's assets (e.g. a logo)
      layout.py                    optional: the channel-specific accent element
      events/
        community-clips-back-catalogue/
          sources.json        collected by hand (see above)
          clips/
            speedy--a3f19c2b/
              clip.json           derived: URL, timecodes, harvested title
              edit.json           editorial: title, status, corrections - only
                                   once a human has actually touched the clip
              transcript.json      derived (cache)
              raw.mp4               derived (cache)
              short.mp4             derived (output) - always the deliverable,
                                     already cut if a trim is applied
              short.full.mp4        derived (cache) - the untrimmed master,
                                     present only while a trim is applied;
                                     a re-render recreates it
              short.trim.json       derived - which trim short.mp4 currently
                                     embodies (absent means no trim)
          brand.json           optional: partial override, this event only
          fonts/                 optional: additional/overriding fonts
          assets/                optional: this event's assets (e.g. a logo)
          layout.py              optional: this event's own decoration
  streams/                     derived, per stream: the downloaded audio,
    <video-id>/                  chunks/ (decoded chunks), windows/ (scored
                                 windows), transcript.json, moments.json
  auth/                        secrets: the OAuth client secret, one upload
                                 token per channel, the model-provider API keys,
                                 the local quota estimate - never committed
  logs/                        the central yt-shorts.log, its dated archives,
                                 and jobs/<kind>-<job-id>.log per background job
  jobs.json                    the job queue's plan: what is queued, running and
                                 recently finished (see "Jobs" under Studio)
  settings.json                this workspace's own settings - today the job
                                 queue's per-pool limits. Absent means every
                                 setting is at its default
```

Every clip lives under one directory, named from its harvested title and a
short hash of its source URL — the identity that never changes, even when
the title later gets a hand-made correction. Backing up, deleting or
inspecting one clip is one operation on that directory.

## Where the data lives

`workspace.resolve()` picks the data directory in this order, and the tool
prints which one it picked at the start of every command:

1. **`YT_SHORTS_DATA`**, if set. A path that does not exist is an error, not
   a silent fallback to something else.
2. **`~/YT-Shorts-Data`**, if it exists.
3. **the repository's own `channels/`** — the layout every command used
   before a workspace existed.

Creating `~/YT-Shorts-Data` is the entire migration switch — no flag, no
cutover date. `bin/yt-shorts migrate <channel>/<event>` copies one event
from the repository layout into the resolved workspace: it copies (never
moves), verifies every file it copies by checksum before reporting success,
and leaves the repository's originals untouched for the operator to delete
by hand once satisfied.

**Why the typography is done in Pillow and not in ffmpeg:** the ffmpeg
installed here is built without `libfreetype` and `libass` — the
`drawtext` and `subtitles` filters don't exist. ffmpeg is deliberately
*not* reinstalled, because the racecast broadcast project depends on
exactly this binary. All text is therefore drawn as a PNG layer and
placed on top via `overlay`.

### Brand as data, motif as an optional module

`channel.json` describes the channel (who): channel ID, handle,
language of the hooks, footer, display name, origin of the broadcast
assets. `brand.json` describes the appearance (how): colors, font paths
(relative to the channel folder), the output dimensions of the video
window.

`overlay.build_overlay` draws the same darkening base surface and the
same opaque accent-colored edges at the video window for every channel.
The channel-specific accent element — for ERF, the slanted
parallelogram — is NOT wired into `overlay.py`, but an optional function

```python
def decorate(draw, config, window_top, window_bottom) -> None: ...
```

in `channels/<channel>/layout.py`. `profile.py` loads it while building
the brand profile and passes it through to `build_overlay` under
`config["decorate"]` — `build_overlay` itself keeps the signature
`(hook, footer, config)` and knows nothing about `layout.py`; it only
calls `config.get("decorate")` if present. A channel without
`layout.py` automatically gets plain bars.

### Brand is per event, not only per channel

One channel does not necessarily have one brand. ERF's own material makes
the case: its Nürburgring 24h clips carry green 24h branding, its Le Mans
Classic clips look completely different. So besides its channel-wide
defaults, `channels/<channel>/events/<event>/` may optionally carry its
own `brand.json`, `fonts/`, `assets/` and `layout.py` — all of it
optional. An event with none of these files behaves exactly as the
channel does. Resolution order:

```
value        ->  event profile   ->  channel profile  ->  built-in default
font file    ->  event/fonts/    ->  channel/fonts/
layout.py    ->  event/          ->  channel/          ->  plain bars
logo file    ->  event/assets/   ->  channel/assets/    ->  none
```

The merge is a **deep merge per key, event wins**, replacing only the
leaf values it names — an event `brand.json` of

```json
{ "colors": { "accent": "#FF3355" } }
```

changes the accent color and leaves `colors.base`, `colors.text`,
`colors.edge`, the fonts, and the output dimensions exactly as the
channel defines them. Lists would be replaced wholesale rather than
merged, but nothing in the profile format is a list today.

A font (or a logo file) named in the event's `brand.json` is looked up
under `events/<event>/fonts/` (or `assets/`) first, then falls back to
`channels/<channel>/fonts/` (or `assets/`) — so an event can name a
channel font or asset without copying it. `layout.py` resolves the same
way: the event's own `layout.py` wins if present, otherwise the
channel's, otherwise plain bars.

**Logo.** `overlay.build_overlay` can place an image at the top of the
upper band — the one thing it couldn't do before this layer existed:

```json
"logo": { "file": "assets/logo.png", "max_height": 160, "gap": 24 }
```

`max_height` and `gap` default to 160 and 24 if omitted. The logo is
scaled proportionally to fit `max_height` (or the side margins, if that
would otherwise overflow them) and centered at the top; the hook is then
laid out in the remaining height below it. This is exactly where a
naive implementation breaks: the hook's overflow guard (the logic that
shrinks or truncates a too-long hook so it never reaches into the video
window) has to know how much vertical space the logo consumes, or a tall
logo pushes the hook text down into the video window. `build_overlay`
folds the logo's reserved height into the same budget the guard already
uses for the accent-decoration offset, so this holds for every hook
length the guard is tested against, logo or no logo. Without a `logo`
key, nothing changes: the reserved height is exactly 0 and every
formula in the hook layout reduces to its pre-logo form.

## The editorial layer (`edit.json`)

Every clip's directory may hold an `edit.json` — hand-made corrections, kept
strictly apart from everything derived (`clip.json`, `transcript.json`,
`raw.mp4`, `short.mp4`). No derivation step (`harvest`, `render`,
`transcribe`) ever writes it, and it is never rewritten by anything but a
human. An untouched clip has no `edit.json` at all — the file is created by
the first editorial action, so its mere existence means a human has looked
at this clip.

`render` and `gallery` both read it; a malformed `edit.json` fails only that
one clip (reported with its exception type), not the run.

It is a JSON object with up to three keys, all optional:

```json
{
  "title": "Abschied von Speedy",
  "status": "kept",
  "transcript": {
    "based_on": "sha256:...",
    "words": [{"start": 0.0, "end": 0.5, "text": " hi"}]
  }
}
```

- **`title`** — overrides the harvested hook everywhere it is shown
  (`render`'s overlay, `gallery`'s page). The harvested title in `clip.json`
  is frozen after the clip's first harvest (see "A second `harvest` run
  never destroys good data" above) — fixing a typo in `sources.json` and
  re-running `harvest` does **nothing** to an already-harvested clip; a
  title correction always goes through `edit.json` instead. This is
  currently a silent surprise if you don't know it, which is exactly why it
  is written down here.
- **`status`** — one of three values, default `"candidate"` if the key is
  absent:
  - `"candidate"` — not yet reviewed. The default state; `render` and
    `gallery` treat it exactly like `"kept"`.
  - `"kept"` — reviewed and approved. No behavioural difference from
    `"candidate"` today; it exists for the operator's own review bookkeeping.
  - `"discarded"` — excluded from `render` (skipped, reported as
    `skipped (discarded): <clip>`) and from `gallery`'s page. The clip's
    directory, and everything in it, stays on disk untouched — discarding
    is reversible by editing `status` back, and is a different action from
    deleting the directory outright.
- **`transcript`** — a hand-corrected caption transcript, `{"based_on":
  "<checksum of the words it was corrected from>", "words": [{"start",
  "end", "text"}, ...]}`. A correction **always** wins over a fresh
  transcription, even if re-transcribing itself fails. If the underlying
  transcript has since changed (a different `based_on` checksum than what
  `transcribe` would now produce), the correction is still used — auto-
  merging would silently produce a wrong caption, and dropping the
  correction would destroy hand work — but the mismatch is reported as a
  `NOTE:` on stderr rather than merged silently. `editorial.checksum()`
  computes the checksum from a word list the same way every time
  (normalized number formatting, sorted keys), so an unchanged transcript
  never spuriously reports a conflict.

**Removing a clip.** Deleting an entry from `sources.json` and re-running
`harvest` does **not** remove the clip — no derivation step ever deletes a
clip's directory (see "Removing a clip from `sources.json` does not delete
it" above); `harvest` reports it instead:

```
NOTE: speedy--dde9b753 ('Speedy!') is no longer in the source list. It is
kept as-is, not re-downloaded or re-rendered on its own: delete
.../clips/speedy--dde9b753 yourself to remove it, or set "status":
"discarded" in its edit.json to keep it on disk but exclude it from render
and gallery.
```

Use `"status": "discarded"` to exclude the clip while keeping it (and its
raw download, transcript and any prior short) on disk for reference; delete
the clip's directory yourself when you actually want it gone.

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
clip never aborts the run" and the layout tree below). Selecting a clip
that has never been rendered shows an explanation instead of a broken
image, with what to do about it (render it).

**A conflict is shown, never silently resolved.** If a caption correction
was made against a transcript that has since changed (a re-transcription,
say), the studio still uses the correction — same rule as `render`, see
"The editorial layer" above — but shows a banner naming what happened,
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
the studio at all, see "Moment detection" below), **Detect moments**,
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
model API — see "Model providers"); **upload** spends YouTube API quota; the
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
clone does:

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

Upload (see "Upload" below) needs two extra libraries, optional exactly like
FastAPI — every other command works without them:

```bash
.venv/bin/pip install google-api-python-client google-auth-oauthlib
```

Moment detection (see "Moment detection" below) needs the SDK of whichever
model provider the channel uses — all three optional, all lazily imported,
none required by anything else in this project:

```bash
.venv/bin/pip install anthropic      # the default provider
.venv/bin/pip install google-genai   # only if a channel uses Gemini
.venv/bin/pip install openai         # only if a channel uses OpenAI
```

Without the SDK (or without a key), detection still runs, but falls back to the
weaker offline lexicon engine instead of failing — see "Moment detection" for
how the fallback is reported and "Model providers" for where each key goes.

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

## Setting up a new channel

Every `channels/<channel>/...` path below is relative to wherever
`workspace.resolve()` lands (see "Where the data lives" above) — the
repository's own `channels/` only if no workspace exists yet, which is not
the common case: `channels/` is not part of the repository (it isn't
tracked, and the repository does not ship one). Once a workspace exists
(the common case), that means `~/YT-Shorts-Data/channels/<channel>/...`,
or wherever `YT_SHORTS_DATA` points.

1. Copy `templates/example-channel/` into your workspace as
   `channels/<channel>/` — it ships `channel.json` and `brand.json` with
   placeholder values, plus its own short README:
   ```bash
   cp -r templates/example-channel <workspace>/channels/<channel>
   ```
2. Edit the copied `channels/<channel>/channel.json`:
   ```json
   {
     "id": "<YouTube channel ID>",
     "channel_url": "https://www.youtube.com/channel/<YouTube channel ID>",
     "handle": "@Example",
     "display_name": "Example Racing League",
     "language": "en",
     "footer": "EXAMPLE | @Example",
     "assets": { "runtime": "...", "standby": "..." }
   }
   ```
   `assets` is optional and purely documentary, in case the channel is
   modeled on a racecast broadcast runtime like ERF is.
3. Edit the copied `channels/<channel>/brand.json` — colors, font paths
   (relative to `channels/<channel>/`, usually `fonts/...`) and the output
   dimensions:
   ```json
   {
     "colors": { "text": "#FFFFFF", "base": "#101010", "accent": "#2A2A2A", "edge": "#9A9A9A" },
     "fonts": { "hook": "fonts/My-Font-Bold.ttf", "small": "fonts/My-Font-Bold.ttf" },
     "output": { "width": 1080, "height": 1920, "video_width": 1080, "video_height": 608, "video_y": 600 }
   }
   ```
   `colors.text`, `colors.base`, `colors.accent`, and `colors.edge` are
   all mandatory (the base surface, accent element, and opaque edges
   are drawn for every channel). `colors.accent` is also what a custom
   `layout.py` typically draws with.
4. Create `channels/<channel>/fonts/` and put font files in it (the
   template doesn't ship any — fonts are channel-specific).
5. **Optional:** a custom accent element via `channels/<channel>/layout.py`
   with a function `decorate(draw, config, window_top, window_bottom)` —
   see the `decorate` signature and example under "Brand as data, motif as
   an optional module" above. If this element draws into the upper bar,
   `brand.json` should state the vertical space it takes up under
   `output.accent_offset`, so the hook text doesn't run into it (default 0
   if omitted). Without `layout.py`, it stays at plain bars.
6. Create the event folder: `channels/<channel>/events/<event>/` with
   `sources.json` (see step 1 above, using the `channel_url` from
   `channel.json`).
7. **Optional:** give the event its own brand, fonts, assets or layout —
   e.g. because one event's clips carry different branding than the rest
   of the channel. Add whichever of these the event needs under
   `channels/<channel>/events/<event>/`:
   - `brand.json` — only the keys that differ from the channel, e.g.
     `{ "colors": { "accent": "#FF3355" } }`. Everything unnamed is kept
     from the channel (see "Brand is per event, not only per channel"
     above for the full resolution order and merge rules).
   - `fonts/` — additional or overriding font files, referenced from the
     event's `brand.json` the same way the channel's are.
   - `assets/` — event-specific assets, e.g. a `logo.png` referenced
     under `brand.json`'s `logo.file`.
   - `layout.py` — an event-specific `decorate(draw, config, window_top,
     window_bottom)`, overriding the channel's for this event only.
   - `glossary.json` — an event-specific glossary (see "Glossary" under
     "Subtitles" below). Unlike `brand.json`'s deep merge, this ADDS to the
     channel's `glossary.json` entry by entry: the event's own value for a
     term or replacement wins that entry, and everything it doesn't name is
     inherited from the channel — and, below that, from the workspace, the
     event's own selected circuit pack, and the (now empty) built-in default
     (see "Glossary" below for the full five-layer rule).
8. Run `bin/yt-shorts harvest <channel>/<event>` and
   `bin/yt-shorts render <channel>/<event>`.

## Subtitles

Subtitles are off by default. Switch them on per channel or per event in
`brand.json`:

```json
"subtitles": { "enabled": true, "max_words": 4, "max_seconds": 3.0, "size": 78, "y": 1290 }
```

`max_words` and `max_seconds` are the values shown above unless a profile
overrides them. Tighter grouping was tried and reads worse: at 3 words /
1.6 seconds the grouping cuts phrases into single words that then stand
alone for a long time. Measured on the real `speedy` transcript, that
setting leaves "by" on screen for 2.08 seconds and "originally" for 2.00,
with four captions running past their own `max_seconds`; at 4 / 3.0 the
same transcript keeps phrases intact and no caption exceeds it. Note that
`max_seconds` is not a hard bound - a single word whose own duration is
longer will still be shown for that long.

The commentary of each clip is transcribed locally with faster-whisper and
cached under the event's `transcripts/`. **The first run downloads the model
(464 MB on disk, `models--Systran--faster-whisper-small`)** and therefore
takes noticeably longer than later ones. A clip with no speech, or any
failure anywhere in the subtitle pipeline (transcription, caption grouping,
building the track), simply gets no subtitles; every such case is reported
on stderr, not treated as a failure - the clip itself still renders and the
run's exit code is unaffected. Only a failure of the render itself (the
clip's actual download or composition) still fails that clip.

**The Whisper decode itself has no timeout.** The transcription step is
bounded against a hung audio extraction, but not against a hung decode -
that call can in principle run forever. If a run appears stuck, interrupt
it with Ctrl-C first; if that doesn't respond within a few seconds, kill
the process outright (`kill -9 <pid>`) and re-run. The transcript cache
means clips already transcribed are not re-transcribed, so nothing already
finished is lost by killing it.

A segment Whisper transcribed but scored as likely non-speech is dropped
rather than shown as a caption, and reported as
`NOTE: <clip>: dropped N segment(s) above no_speech_prob threshold 0.75 (...)`
on stderr. The threshold (0.75) was picked empirically - the highest
no_speech_prob measured on any segment of real commentary across the
project's reference clips, versus the lowest measured on a hallucinated
segment from silent audio - not a proven bound, so it is not guaranteed to
be right for every clip. A drop means exactly one thing: that stretch of
the short has no caption, because the segment covering it was judged too
likely to be non-speech to show.

**Forcing a re-transcribe.** A cached transcript in a clip's own
`transcript.json` is reused on every later render, keyed to the clip's own
source URL rather than its position in `sources.json` - reordering that
file no longer makes a different clip inherit a stale cache. To get a fresh
transcription of a clip (e.g. after upgrading the model, or to see if a
different result comes out this time), delete that clip's
`clips/<clip>/transcript.json`; the next render transcribes it again from
scratch. Nothing else needs to change - a re-order of `sources.json` is not
a reason to delete anything here. A hand-corrected transcript in the same
clip's `edit.json` is unaffected either way - it always wins over a fresh
transcription (see "The editorial layer" above).

**Glossary.** Whisper doesn't know a sim-racing league's own proper nouns -
on a real ERF clip it transcribed "Rei Racing" as "very, very". Up to five
layers feed one glossary, least to most specific: an empty built-in default
(shipped in code, not a file), the circuit pack an event selects (see
"Circuit vocabulary" below), the workspace's own `glossary.json`
(`$YT_SHORTS_DATA/glossary.json`, or `~/YT-Shorts-Data/glossary.json`),
`channels/<channel>/glossary.json`, then `events/<event>/glossary.json`.
Every layer is optional, and they are ADDITIVE, entry by entry — not a
wholesale replacement: the most specific layer that names a given term or
replacement wins that entry, and anything a layer doesn't name is inherited
from the layer below it. Each of the three file-backed layers looks like:

```json
{
  "terms": ["Rei Racing", "Team Fullsend", "Nordschleife"],
  "replacements": { "very very": "Rei Racing" }
}
```

To DISABLE an entry a less specific layer contributed — rather than simply
leaving it unmentioned, which inherits it — name it with a falsy value:
`false` for a term, `null` for a replacement, e.g.
`{"terms": {"Nordschleife": false}, "replacements": {"very very": null}}`.
An empty-string replacement is refused outright, not treated as "delete
these words" — that would be indistinguishable from a typo; use `null` to
disable instead. The studio has an editor for the three writable layers
(workspace, channel, event) that shows every entry alongside the layer that
set it, including an entry a more specific layer disabled — struck through,
not hidden.

**Circuit vocabulary.** An event may name the circuit it races at with
`"track": "<id>"` in its own `glossary.json` — the corner names and any
measured mis-hearings for that one venue then apply to that event only, on
top of the layers above. Pick the track in the studio's Event glossary
editor (the Circuit selector) rather than typing the id by hand; the full
list of ids and their display names lives there, or in
`src/yt_shorts/tracks.py` if you're reading the source.

Both keys are optional and work independently. `terms` are handed to the
Whisper decoder as a bias ("hotwords") - this only has a chance to help on
a FRESH transcription; a term added to the glossary cannot retroactively
change how an already-cached transcript was decoded. `replacements` are
applied to the decoded words afterwards, correcting whatever the decoder
still got wrong (case-, punctuation- and whitespace-insensitively, but
never across a sentence boundary), and are written into the cache already
corrected - so the extra work happens once per clip, not once per render.

**Nothing invalidates a cached transcript when the glossary changes** -
this is deliberate for this pre-alpha stage, not an oversight. If a
`transcript.json` was cached before a glossary existed, or before it named
the term that would have fixed it, editing `glossary.json` alone changes
nothing on that clip's next render. Delete that clip's
`clips/<clip>/transcript.json` (see "Forcing a re-transcribe" above) to
re-derive it against the current glossary.

## Whole-stream transcription

`stream_transcribe.transcribe_stream(video_id, workspace_dir)` turns a whole
stream into a timed transcript and a kept local audio file, both under
`streams/<video_id>/` in the workspace. It is the input the moment detector will
run on. Because a stream is hours long — **over two hours of Whisper decode for
an 8-hour race**, measured on this machine on 2026-07-31 (an 8 h 19 min stream:
about 2.5 minutes per 10-minute chunk, 50 chunks; this README said "~1 hour"
until then, and the figure is machine-dependent either way) — it works in fixed
chunks: the audio is downloaded once, each chunk is
decoded in a separate killable process, each chunk's words are cached, and the
whole thing is assembled into `transcript.json`. That makes it **resumable** - a
re-run decodes only missing chunks - and lets a hung chunk be timed out without
losing the rest. The downloaded audio is kept because decoding needs it - an
earlier loudness-ranking signal was tried and removed (see "Not built yet
(later)" below and CLAUDE.md's "Moment detection" section for why). Everything
under `streams/<video_id>/` is derived data: delete the folder to re-download
and re-transcribe.

**Who starts one depends on who is asking.** `bin/yt-shorts detect` still
transcribes as its first step (or reuses the cache): a CLI operator is watching
a terminal and can decide to sit through it. The studio does not — its Detect
button *requires* a transcript that already exists and fails the job if there
is none, rather than silently starting two hours of decode nobody is watching.
In the studio you queue a transcription of its own: the **Transcribe** button
on the channel's Streams tab or on the stream view (see "Jobs" above).

**The glossary (see "Glossary" above) reaches this path too, split across the
chunk boundary.** Only `terms` are handed to each chunk's killable worker, as
the decoder bias for that chunk alone — never `replacements`, which are
applied exactly once, at assembly, over the whole stream's words. That split
is deliberate: a decoded chunk is cached as-is, so if a replacement were
applied inside the worker it would get baked into that cache in already-
corrected form, defeating a correction whose key spans a chunk boundary and
making a plain re-assembly unable to tell what the decoder actually heard.
The consequence is two-sided. Because `replacements` apply at assembly, a
glossary edit takes effect on the **next assembly with no re-decode** — cheap,
and immediate for anything a `replacements` edit alone can fix. But a `terms`
edit changes what the decoder is BIASED toward, which only affects a chunk
that has not been decoded yet: any chunk already cached under
`streams/<video_id>/chunks/` keeps the OLD decoder bias until that directory
(or the specific chunk) is deleted and re-decoded from scratch.

## Moment detection

Given a whole-stream transcript (see "Whole-stream transcription"), the tool
scans it for moments worth clipping and writes what it finds to an analysis
file. **It does not create clips on its own:**

```bash
bin/yt-shorts detect <channel>/<event> <video-id>   # -> streams/<video-id>/moments.json
```

or, in the studio, **queue a transcription first and then detect**: open the
channel's Streams panel, press **Transcribe** on the stream (that enqueues a
`transcribe` job — follow it on the Jobs screen; an 8-hour race takes over two
hours), and once it is `done` press **Detect moments** on the same stream.
Detection in the studio never transcribes on its own — a stream with no
transcript fails the detect job with a message saying to transcribe it first —
so the two steps are planned separately: you can queue five streams to
transcribe overnight without ordering five paid detections at the same time.
Detection itself runs as a background job, and the panel reports which engine
ran and how many moments it found once it finishes. From the CLI it is still
one command: `detect` transcribes and then scores.

**Why it stops at an analysis.** An earlier version of this wrote clip
directories the moment it found something, and the result was unusable: every
suggestion was 12 seconds long, torn out of context, and seemingly arbitrary.
Now detection only writes `streams/<video_id>/moments.json` — a scored list of
candidates (start, end, category, a one-sentence reason, an optional on-screen
hook suggestion) plus a stream-activity overview — and it may be generous,
because a weak suggestion in that file costs you a glance rather than a clip
you have to clean up. Turning one of those candidates into an actual clip
(`clip_from_moment.create_clip`) is a separate, explicit step, and the studio's
**stream view** is where you take it.

**The operator's flow.** Open an event, click its **Streams** tab, and click a
stream — that opens `/{channel}/{event}/streams/{video_id}`, one level deeper
than the editor. You get a searchable transcript, a small player, an overview
strip over the whole stream and, below it, a zoom lane for setting a clip's
exact window (the overview only locates — click it to re-centre the zoom lane
— an eight-hour stream is far too coarse over one strip to set a boundary on
directly; the zoom lane is what you actually drag). Read the transcript,
search it for what you remember being said, and either click a detected
moment in the hit list to jump the player and the zoom lane to it, or drag a
window on the zoom lane yourself. Either way, type a hook and press **Make a
clip** to write a real clip directory from that window.

**None of this needs detection to have run, or any API key at all.** A
transcript alone is enough to open the screen, search it, drag a window and
make a clip — detection only adds the ranked hit list on top, and the screen
says plainly when a stream "has not been analysed yet" rather than hiding the
zoom lane or erroring. If you do run detection, the hit list also tells you
which **engine** produced it (the model, or the weaker offline lexicon
fallback) and names any **window that failed** to scan — both surfaced
directly rather than left for you to notice their absence, the same reason
"reduced quality" gets logged below.

**How a moment is scored.** By default, an hour of transcript at a time is
sent to Claude, which is asked to pick out moments by category — the race
start/finish, incidents (crash, spin, contact...), highlights (overtakes,
fastest laps, pole...), race control (safety car, penalties...) and
commentator reactions — using the channel's own excitement lexicon (see
below) as a vocabulary hint. This needs the optional `anthropic` package:

```bash
.venv/bin/pip install anthropic
```

and an API key at `<workspace>/auth/anthropic.json`, mode 600, gitignored and
never committed — either a bare `sk-ant-...` string or `{"api_key":
"sk-ant-..."}`.

Anthropic is only the **default**. A channel can be pointed at Google Gemini or
OpenAI instead — see "Model providers" below for how to choose one, where each
one's key goes, and how far each has actually been measured.

**Without a key, or if the model can't be reached, detection falls back
automatically** to a weaker, fully offline engine that scores marker hits from
the same excitement lexicon, amplified by how much faster the commentary is
running than the stream's own baseline. This never fails outright — a stream
with no lexicon and no key still finishes, it just finds nothing — but the
fallback is always announced: the job's log (and the CLI's own output) say
which engine actually ran, so a quietly worse result is never mistaken for a
normal one.

**The excitement lexicon** (`moments.json`, additive across the workspace,
channel and event, the same layering the glossary uses) is a weighted marker
list, not a flat one — `crash` counts for more than `pole`, because an
unweighted list flags nearly every mention of a word commentators say
constantly. Editing a marker's weight changes what the offline fallback finds
immediately; it only changes what the model is told to look for when the
edit enables or disables a marker outright (crossing zero), since the model
is only ever given the marker names, never their numbers.

A moment, once it exists as a clip, renders through the same pipeline as a
community clip: it carries the stream's video id and a time range, so
`render.Source`'s `--download-sections` path fetches exactly its window, and
nudging that window afterwards is an **editorial** decision stored in
`edit.json` — the render always downloads the effective window, your
override if you set one, the detected one otherwise.

## Model providers

Moment detection is the only part of this tool that talks to a commercial model
API, and which vendor answers is a choice. Three providers ship —
**Anthropic**, **Google Gemini** and **OpenAI** — behind one seam, so switching
is a config key rather than a rebuild. Nothing else in the project changes:
harvesting, rendering, transcription, subtitles and upload never touch a model
API at all.

**Choosing one.** In the studio, open a channel's **Brand editor** and use its
*Moment detection* section. That writes a `detect` block into the channel's
`brand.json`, which you can equally well type by hand:

```json
{
  "detect": {
    "provider": "gemini",
    "model": "gemini-3.6-flash"
  }
}
```

Both keys are optional, and so is the whole block: an absent (or explicitly
`null`) `provider` means Anthropic, and an absent (or `null`) `model` means
that provider's own default model — which is what every profile written before
this existed gets. An **unknown provider name is a reported profile defect**,
not a silent fall back to the default: a typo that quietly ran a different
vendor than you asked for is exactly the kind of silent substitution this
project refuses. The model name, by contrast, is deliberately **not** checked
against the vendor's catalogue, and what a wrong one does is worth knowing
before you type one: it does **not** fall back to the lexicon. Nothing reads
the name until the first request, so the run has already committed to
`"engine": "model:<whatever-you-typed>"`; every window then fails, each with
its cause logged and its index recorded in `missing_windows`, and you get a
finished analysis with **zero moments**. That is loud rather than silent — the
stream view's hit list flags missing windows — but it is an empty result, not a
weaker one. The lexicon engine takes over only when no caller can be built at
all: no key file, that provider's SDK not installed, or the service unreachable
when the client is constructed.

`detect` is **channel-level only**. An event's `brand.json` override may not
set it (the studio refuses it by name rather than dropping it silently),
because it decides whose bill a run spends — a property of the channel, not of
one event's look.

**Where the keys live.** One file per provider, in the same workspace
directory as the YouTube credentials, mode 600, gitignored and never logged:

```
<workspace>/auth/anthropic.json
<workspace>/auth/gemini.json
<workspace>/auth/openai.json
```

Each accepts two shapes — a bare key string, or `{"api_key": "..."}` — because
the file as you first create it usually holds the raw key despite its
extension. Each provider also needs its own optional SDK, lazily imported and
required by nothing else:

```bash
.venv/bin/pip install anthropic      # Anthropic
.venv/bin/pip install google-genai   # Google Gemini
.venv/bin/pip install openai         # OpenAI
```

**Setting one from the studio.** The **Settings** page has a *Model providers*
block with one row per provider. It shows whether that provider's SDK is
installed (with the exact command if it is not), whether a key is stored, and
lets you paste a new key or forget the stored one. A stored key is never shown
again and never returned by the server — a row carries only booleans, shipped
constants (the default model, the install command, the price table) and the
provider's public id. Keys
are workspace-wide; the *channel* rows further down show which provider each
channel is currently set to use, read-only, since that is the Brand editor's
job. Removing a key is reversible by pasting it again, and makes detection for
every channel using that provider fall back to the offline lexicon engine.

### How far each provider has actually been measured

All three have now been measured against their real service, on the **same
stream**, so what follows is a comparison rather than a gradation of
confidence. That stream is this workspace's own 98-minute qualifying (5574
transcript words, 2 scan windows); Anthropic's run was on **2026-07-29**,
Gemini's and OpenAI's on **2026-07-31**.

| provider / model | moments | agreement | measured cost |
| --- | --- | --- | --- |
| `claude-opus-5` *(the default)* | 10–11 | reference | **$0.1362** |
| `gemini-3.6-flash` | 6 | 6/6, both key moments | $0.0590 |
| `gpt-5.6-terra` | 5 | 5/5, both key moments | $0.0326 |
| `gpt-5.6-luna` | 7 | 6/7, splits the pole lap | $0.0041 |

*Agreement* counts a moment as agreeing when it overlaps one of Anthropic's by
more than half of the shorter window. Anthropic's run is the **reference, not
an answer key** — it is the production analysis this workspace already had, not
a ground truth anyone checked the others against. The costs are computed from
each API's own reported token counts at the rates in that provider's price
table; **none of them was read off an invoice.** The input token counts differ
per vendor for the same text (15369 / 13052 / 11047), because the tokenizers
differ — so the table compares **cost**, not tokens.

**Every figure here is a single sample, and the reference itself moves.** The
same `claude-opus-5` over the same stream has been observed to return **7, 10
and 11** moments on three separate runs (2026-07-29 twice, 2026-07-31 once).
What varies is the **borderline tail**, not the result: the two logged runs
agree on 9 of 10, differing by two weak moments in one (scored 5.0 and 4.5) and
one in the other (scored 7.0). The strong moments — including both the pole lap
and the Speed Hunter lap every provider is scored on — are stable across all of
them. So read the moment counts as approximate, and read the *agreement* column
as approximate too: "6/6" is agreement against **one** run of a reference that
would have offered a slightly different list on a different day, not a
precision claim. Only the Opus row has more than one run behind it; the other
three are one run each, so their spread is unknown rather than zero.

The Opus cost here (`$0.1362`, from the API's own reported counts on
2026-07-31: 2 calls, 15369 input + 2374 output tokens) replaces a `~$0.062`
that this table used to carry and that was never a measurement — it came from
the character-counting estimate `estimate.py` documents as circular. The
correction changes what the table *says*, not just a decimal: Opus is **more
than twice** Gemini Flash rather than about the same as it, and over forty
times `gpt-5.6-luna`. The Anthropic bake-off's own `~$0.0xx` figures below are
that same estimate and are labelled as such.

Read the whole table as **one stream, one qualifying session, two windows, one
run per model — except Opus, which has three.** That caveat used to end with
"re-measure before treating any of it as settled", and the re-measurement has
since happened. It changed the conclusion.

### The same three providers over an eight-hour race

The qualifying is not this project's workload. On **2026-07-31** all three ran
over `Esm9vv5-PdU`, *"ERF 24h Nürburgring 2026 | The Race | Part 1"* — **8 h 19
min**, 41 925 transcript words, 50 decoded chunks with none missing, **9 scan
windows** against the qualifying's two.

| provider / model | moments | ≥ 7.0 | ≥ 8.0 | failed windows | measured cost | runtime |
| --- | --- | --- | --- | --- | --- | --- |
| `claude-opus-5` | 39 | 13 | 2 | 0 | $0.7603 | 130 s |
| `gemini-3.6-flash` | 30 | 21 | 5 | **1** | $0.3347 | 146 s |
| `gpt-5.6-terra` | 33 | 24 | 8 | 0 | **$0.2140** | 78 s |

**Agreement collapsed.** Over the qualifying, Gemini and OpenAI proposed only
moments Anthropic had also found (6/6 and 5/5). Over the race, pairwise
agreement is **38 % to 67 %**:

| | vs `opus` | vs `gemini` | vs `terra` |
| --- | --- | --- | --- |
| `claude-opus-5` (39) | — | 15 (38 %) | 22 (56 %) |
| `gemini-3.6-flash` (30) | 15 (50 %) | — | 16 (53 %) |
| `gpt-5.6-terra` (33) | 22 (67 %) | 16 (48 %) | — |

**No model's list is a subset of another's.** Each proposes strong moments the
others miss entirely — two of Gemini's five ≥ 8.0 moments appear in neither
other list, and one of OpenAI's eight. Their stated reasons read plausibly in
every case (a three-wide fight for the lead, a spin on cold tyres, a crash on
the restart), so this is divergence, not one model hallucinating.

**Do not read the ≥ 7.0 and ≥ 8.0 columns across rows.** Each model scores on
its own scale, and this run shows how far apart those scales are: Anthropic
returned the *most* moments (39) and the *fewest* strong ones (2 at ≥ 8.0),
OpenAI the reverse. An 8.0 from one is not a claim about the same thing as an
8.0 from another. This is the first measured support for the "one engine per
run" rule `moment_scan.scan` documents — mixing two scales in one hit list
would produce a ranking nobody could interpret.

**Gemini lost a window** — one of nine, an hour of the race that nothing
looked at. Recorded in `missing_windows` and flagged in the stream view's hit
list, which is exactly what those exist for; noted here because it is the first
time it has happened on a real run rather than in a test.

**What this changes.** The qualifying supported an argument that Anthropic's
extra moments were noise — procedural announcements and unreproducible
low-scored items. **That does not generalise.** Over eight hours the three
models genuinely disagree, and picking one means accepting the moments the
others would have found. `gpt-5.6-terra` remains the recommended choice on this
material — cheapest by a factor of 3.6, fastest, no lost windows, and the
highest confirmation rate of any pair (67 % of its moments also appear in
Anthropic's) — but on those grounds, not on "the others only add noise".

Everything above is still **one race, one run per model.** No provider's spread
over an eight-hour stream has been measured even once.

**Anthropic — the default, and measured.** A bake-off over this workspace's own
98-minute qualifying stream on **2026-07-29**, all three models, the same
corrected prompt, scored against the operator's four known-good moments:

| model | moments found | of the 4 known-good | cost *(estimate, not measured)* |
| --- | --- | --- | --- |
| `claude-haiku-4-5` | 2 | 0 | ~$0.012 |
| `claude-sonnet-5` | 7 | 1 | ~$0.037 |
| `claude-opus-5` | 7 | 3 | ~$0.062 |

The moment counts and the known-good hits in that table are observations; the
**cost column is not**. All three came from the character-counting estimate,
and it runs low — the one row since measured against the API's own token counts
is `claude-opus-5`, at **$0.1362**, more than double what its `~$0.062` says.
Nobody has re-measured Haiku or Sonnet, so those two rows still hold only an
estimate.

`claude-opus-5` is the default because it alone found the purple flying lap and
"what a lap", and gave the most specific reasons. The price gap is a few cents
per stream, which does not buy back a detector that misses the moments the
channel exists to publish. This measurement overturned the plan's own starting
assumption that the cheapest model would do; treat "the detector only ranks a
transcript it has already been handed, so it can be cheap" as an argument that
has already been refuted once.

The `7` in the Opus row is one run of a count that moves — see the variance
note above the comparison table, which also records how the 7-versus-11
discrepancy between this bake-off and the workspace's stored analysis was
settled.

**Gemini — measured.** One paid detection run on **2026-07-31**, one model.
Gemini found a **strict subset**: all 6 of its moments overlap an Anthropic
moment by more than half of the shorter window, 3 of the 6 exactly, and the top
three scores are identical on identical windows. It disagreed with Opus about
nothing — it found less, including both of the moments the Anthropic bake-off
credits to Opus alone. It is **not as cheap as a Flash model sounds**, because
it spends most of its answer thinking (5262 output tokens where the local
preview predicts 1000) — $0.0590 for this stream. Against Opus's *measured*
$0.1362 that is still well under half, so it is the cheaper option; it is not
the near-tie this paragraph claimed while the Opus figure was the ~$0.062
estimate. What separates the two here is recall as well as price, which is why
Anthropic remains the default and this is what a channel gets when it asks for
Gemini.

`gemini-3.5-flash` was not measured at all; it costs more per output token
($9.00 against $7.50), which on a model that thinks this much is the expensive
direction, and whether it buys the missing recall back is unknown. Two ids that
used to be priced here — `gemini-2.5-flash` and `gemini-2.5-pro` — were
**removed** from `gemini_api.PRICES` on 2026-07-31: both answer `NotFoundError`
on the Interactions API this project uses, existing only on the older
`generate_content` surface. Naming one in the `model` field is not a cheap run
and is not a lexicon one either: every window fails into `missing_windows` and
the analysis comes out empty (see "Choosing one" above).

On Gemini's **free tier**: it exists on the Flash models, but Google's terms
restrict free-tier use for the **EEA, Switzerland and the UK**, where billing
must be enabled on the project even for models that are otherwise free-tier
eligible (verified 2026-07-31 at `ai.google.dev/gemini-api/terms`). If you are
in one of those regions, expect to enable billing before the first call
succeeds — a quota or permission error on your first run is that, not a bug in
this tool. Concretely, as observed on 2026-07-31 before credits were added to
the project: every call comes back `429 "Your prepayment credits are
depleted"`, and this project handles that the way it handles any other failing
window — the window is recorded in `missing_windows`, the engine is named, the
cause is logged loudly — so the symptom is a **finished run with an empty
analysis**, not a crash or an error page. There is one genuine upside to the
same rule: in those regions the
**paid-tier data terms** apply across the services, so content you submit is
not used to improve Google's models.

**OpenAI — measured, and the one result that changed a decision.** Two paid
runs on **2026-07-31**, `gpt-5.6-terra` and `gpt-5.6-luna`, 0 failed windows
each. `gpt-5.6-terra` stays the default, and the reason is subtler than
"cheaper is worse": `gpt-5.6-luna` finds *more* moments and costs an eighth as
much, but it **cuts the pole lap in half** — proposing two windows that overlap
the reference moment by 49% and 33%, where `gpt-5.6-terra` and
`gemini-3.6-flash` each return one window at 86%. For a Shorts pipeline the
deliverable *is* the window: a split moment is two clips that each begin or end
in the wrong place, not one good one. Until this run, `gpt-5.6-terra` was
picked by **analogy** with the Anthropic bake-off — a reasonable argument from
another vendor's measurement, and now replaced by this vendor's own.

All five ids in `openai_api.PRICES` answered a 16-token ping on the same date,
so unlike Gemini's table this one has no entries the API declines to serve. The
other three (`gpt-5.6-sol`, `gpt-5.4`, `gpt-5.4-mini`) were **not run** and
have no numbers at all.

**An open question nobody has answered.** On identical input, Anthropic found
11 moments (10 on the re-run — see the variance note above), Gemini 6 and
OpenAI 5 — with perfect agreement wherever they overlap: every non-Anthropic
moment lands on an Anthropic one. Nobody has checked whether the other two
engines **scored** the moments only Opus found below threshold, or never
**proposed** them at all. It is not a cap being hit
(`moment_scan.MAX_PER_WINDOW` is 12 across 2 windows, nowhere near reached), so
"lower recall" currently describes the counts rather than explaining them. Part
of the gap is a tail that is not stable even within one model, which narrows
the question without answering it: the moments Opus alone found include the
weak ones its own runs disagree about. It is the most interesting thing the
measurement did not settle.

Prices shown anywhere in the studio are a **dated per-million-token rate and a
floor**, not a bill: each provider's table is a flat two-number snapshot that
cannot express batch, cached, long-context or service-tier pricing, and this
project's endurance-stream windows are long enough to reach the tiers that cost
more. The studio's cost preview on the stream screen is likewise local and
approximate (characters divided by four, no network, no key) — it answers
"cents or euros?", not "what will the invoice say".

### Adding a fourth provider

A provider is one module in `src/yt_shorts/providers/`, and registering it is
**two edits in `providers/__init__.py`, not one**: the module-scope `from
. import …` at the top, and the `_MODULES` tuple below it. `PROVIDERS` is a
comprehension over `_MODULES`, so inserting into that dict directly leaves you a
provider `ordered()` never returns — and `ordered()` is what both the Settings
payload and every parameterised contract test are built from, so the provider
would be registered and untested at the same time. Measured, not assumed: a
fourth provider added to `PROVIDERS` alone fails
`test_the_registry_is_the_three_modules_default_first` and is silently absent
from all thirty of the per-provider contract cases.

It must expose exactly the **eight names** in `providers.CONTRACT` and nothing
else:

| name | what it is |
| --- | --- |
| `PROVIDER_ID` | the id used in `brand.json`, in URLs and as the registry key |
| `KEY_FILENAME` | its key file under `<workspace>/auth/` — must be `<PROVIDER_ID>.json` |
| `DEFAULT_MODEL` | the model used when a channel names none |
| `PRICES` | `model -> (USD per 1M input tokens, USD per 1M output tokens)`, dated |
| `PACKAGE` | the importable SDK package name, for the "is it installed?" check |
| `INSTALL` | the exact command that installs it |
| `VERIFIED` | whether it has been measured against the real service |
| `make_caller` | `make_caller(api_key, *, model, max_tokens, sdk, usage) -> call(system, user, schema) -> dict` |

Two shapes the contract enforces that the table above cannot show, both easy to
get wrong once and then puzzling:

- **every rate in `PRICES` must be a `float`.** `(1, 5)` is rejected and
  `(1.00, 5.00)` accepted — an int is the shape a typo takes, and the numbers
  are dollars.
- **all four of `make_caller`'s keyword arguments need defaults**
  (`model=DEFAULT_MODEL`, `max_tokens=4096`, `sdk=None`, `usage=None`).
  Production calls it as `make_caller(key, model=…, usage=…)` and the suite as
  `make_caller(KEY, sdk=…)`; one required keyword anywhere fails all nine
  behavioural cases with a `TypeError` that names the argument rather than the
  rule.

`tests/test_provider_contract.py` is the bar, and it is worth reading before
writing the module rather than after. It holds **every registered provider** to
the same nine behavioural properties, parameterised over the registry, so a
fourth provider inherits all of them the moment it enters `_MODULES`: the
three key-secrecy wraps (building the client, sending the request, **reading
the response**), a non-JSON answer becoming a `ModelError`, accepting
`moment_scan`'s own schema and returning its answer, recording usage *before*
the response is read, accumulating the API's own token counts, surviving usage
that cannot be read, and working with no `usage` argument at all. Plus the key
file's own rules (0600, atomic write, every unusable key reported as
`MissingKey` and never quoted back) and a check that importing the package
pulls in no vendor SDK. Add your provider's fake SDK to that file's `FAKES`
table and nothing else *there* changes.

**But the test work is not only that file.** Two assertions elsewhere pin the
registry as it stands today, and a fourth provider trips them:

- `TestProviderKeys.test_settings_lists_every_provider_with_its_state` in
  `tests/test_studio_api.py` asserts the settings payload's provider ids are
  exactly `{"anthropic", "gemini", "openai"}` — add yours;
- `TestProviderKeys.test_the_verified_flag_is_each_modules_own`, in the same
  class, asserts every row's `verified` is `True`, which a new provider trips
  for as long as it is honestly `VERIFIED = False` — exempt yours until you
  have measured it.

Both are deliberate pins, not oversights: the registry is small enough to state
exactly, and stating it exactly is what catches a provider that quietly stops
being served. Expect to widen them, rather than to be surprised by them. With
those two edits and the `FAKES` entry, nothing else in the suite needs touching
— verified by writing a throwaway fourth provider, following this recipe
literally, and getting a clean full run (and `python3 tools/lint.py` green) with
it registered.

Two rules that are easy to get wrong and that the suite enforces: the SDK must
be imported **lazily**, inside the module, so a venv without it still starts and
renders; and **no exception escaping the SDK may carry its own message** —
wrap it in `ModelError` built from the exception's type name only, at all three
entry points, because a vendor's message can quote the request and the request
carries the API key.

## Upload

Upload a rendered short to the right YouTube channel as a **private** video, from
the studio or the CLI. This is the only step that writes to YouTube, so it needs
OAuth — unlike everything else, there is no yt-dlp path.

**One-time setup (yours to do).** Uploading uses the YouTube Data API, which needs
a Google Cloud project and an OAuth client:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   project, enable the **YouTube Data API v3**, and create an **OAuth client ID**
   of type *Desktop app*.
2. Download its `client_secret.json` and place it in your workspace under
   `auth/` (i.e. `$YT_SHORTS_DATA/auth/client_secret.json`, or
   `~/YT-Shorts-Data/auth/`). **Never move it into the repository** — it is a
   credential; the repo's `.gitignore` guards against it, but keep it in the
   workspace regardless.

**Connect a channel** (browser consent — the tool never sees your password).
Either from the studio — a "Connect channel" action opens your browser to
Google's consent screen, with the channel id pre-filled (editable, so you can
connect another channel you manage) — or from the CLI:

```bash
bin/yt-shorts auth <channel>          # e.g. erf
```

Either way opens your browser to Google's consent screen. After you approve, only
a refresh token is stored, in `auth/token-<channelid>.json`, keyed by the
channel's YouTube id. To upload for a channel you manage, connect it while signed
in to the Google account that owns it — that is all "switching account" means
here.

**Render-only channels.** The YouTube Data API can only upload to a channel your
Google account **owns** (your personal channel and owned brand accounts). A channel
you only **manage or edit** cannot be uploaded to via the API — it does not even
appear in Google's consent chooser. For such a channel, set
`"upload": { "mode": "manual" }` in its `brand.json`: the tool then never offers a
connect or an API upload for it (both are refused with a clear message), and the
studio instead shows a **Download short** button and the prepared
title/description/tags to copy, so you upload the short by hand in YouTube Studio.
Owned channels need no flag — `mode` defaults to `api`.

**Upload:**

```bash
bin/yt-shorts upload <channel>/<event>   # uploads every kept, rendered, not-yet-uploaded short
```

Or from the studio: a kept, rendered clip shows an Upload action that displays the
exact metadata for you to confirm, then uploads and shows the resulting private
video's URL.

- **Private by default, always.** A short is never uploaded public; you review it
  in YouTube Studio and make it public there yourself. This tool never publishes.
- **Re-upload guard.** A successful upload writes `upload.json` in the clip's
  directory; the tool then shows "uploaded" and refuses a second upload unless you
  explicitly ask for it (studio: a re-upload confirmation; the CLI skips
  already-uploaded clips).
- **Quota.** `videos.insert` costs ~1600 of a default 10,000 units/day, so about
  **6 uploads a day**. The tool keeps a local per-day estimate (resetting at
  midnight Pacific, when YouTube's quota resets) and warns as you approach it; a
  `quotaExceeded` from the API is reported plainly. The estimate only warns — the
  API is the authority.

**Metadata** comes from an optional `upload` block in the channel's `brand.json`
(an event may override it), all with defaults:

```json
{
  "upload": {
    "description": "Clip from {source_title}.",
    "tags": ["simracing", "endurance"],
    "category_id": "20",
    "made_for_kids": false
  }
}
```

`{source_title}` and `{title}` interpolate from the clip. The **title is not
configured here** — it is the clip's effective hook, the same text burned into the
short, so what a viewer reads matches what they see. `category_id` defaults to
`"20"` (Gaming); `made_for_kids` is always sent (YouTube requires the
declaration) and defaults to `false`.

The Google libraries are an **optional dependency**, like FastAPI — install them
only if you upload (see "Development"). Everything else works without them.

## Not built yet (later)

Making a video public, scheduling, thumbnails, playlists, and deleting an upload
stay manual in YouTube Studio after you review the private upload. Live-chat
activity as an extra moment signal is a possible later addition; moment detection
currently scores transcript evidence only (by model, or by the offline lexicon
fallback) — an earlier loudness-ranking signal was tried and removed, see
CLAUDE.md's "Moment detection" section for why. The studio picker for turning a
detected moment (or a hand-picked window) into a clip is the stream view — see
"Moment detection" above — so that item is done, not outstanding.
