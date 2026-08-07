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
pip install ".[all]"           # into your own venv; [all] adds transcription,
                               # every cloud moment-detection provider, and
                               # the libraries YouTube upload needs
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

**Every path below is inside your workspace, not this repository.** The
workspace is the data directory the tool resolves and prints on stderr as each
command starts: `~/YT-Shorts-Data` by default, or wherever `YT_SHORTS_DATA`
points. A fresh clone has no `channels/` of its own and does not need one — see
[Where the data lives](https://github.com/jegr78/yt-shorts/wiki/Where-the-data-lives)
for the full resolution order and what goes in each directory.

Every command takes an identifier of the form `<channel>/<event>`,
e.g. `erf/community-clips-back-catalogue` — `<channel>` is a folder name
under the workspace's `channels/`, `<event>` a folder name under that
channel's `events/`.

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

## Documentation

The manual is the [wiki](https://github.com/jegr78/yt-shorts/wiki): setup, the
profile format, the studio, subtitles, whole-stream moment detection, upload,
and a catalogue of what can go wrong. These are the pages a new operator wants
first:

- [Setting up a new channel](https://github.com/jegr78/yt-shorts/wiki/Setting-up-a-new-channel)
  — from the template to a first render.
- [Where the data lives](https://github.com/jegr78/yt-shorts/wiki/Where-the-data-lives)
  — the workspace outside this repository, and the profile format a channel
  and an event are made of.
- [Studio](https://github.com/jegr78/yt-shorts/wiki/Studio) — the local
  editor: review a clip, correct its title and captions, render it, upload it.
- [Subtitles](https://github.com/jegr78/yt-shorts/wiki/Subtitles) — the
  caption layer, the transcript cache and the glossary.
- [If something goes wrong](https://github.com/jegr78/yt-shorts/wiki/If-something-goes-wrong)
  — the failure catalogue: what you see, and what to do about it.

When you move on from community clips to whole broadcasts, read
[Whole-stream transcription](https://github.com/jegr78/yt-shorts/wiki/Whole-stream-transcription)
and
[Moment detection](https://github.com/jegr78/yt-shorts/wiki/Moment-detection)
next; [Upload](https://github.com/jegr78/yt-shorts/wiki/Upload) covers sending
a finished short to YouTube.

Changing the code rather than running it is a different subject:
[CONTRIBUTING.md](CONTRIBUTING.md) for the setup and the checks,
[CLAUDE.md](CLAUDE.md) for the constraints that are expensive to violate, and
[docs/superpowers/](docs/superpowers/) for the design and implementation plan
behind each stage.
