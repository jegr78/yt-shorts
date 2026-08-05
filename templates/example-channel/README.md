# Example channel

A starting point for a new channel, not a working one — every value in
`channel.json` and `brand.json` here is a placeholder. Copy this directory
into your workspace and edit it there; see `README.md`'s "Where the data
lives" section (in the repository root) for where your workspace is.

## Use it

```bash
cp -r templates/example-channel <workspace>/channels/<channel>
```

`<workspace>` is `~/YT-Shorts-Data` (or wherever `YT_SHORTS_DATA` points),
`<channel>` is whatever short, URL-safe name you want to identify this
channel by (e.g. `erf`).

## What to change

- **`channel.json`** — `id` and `channel_url` are the YouTube channel's own
  ID (from its URL); `handle` and `display_name` are how it's named;
  `footer` is the text drawn at the bottom of every short. `assets` is
  optional and purely documentary — only relevant if this channel is
  modeled on a separate broadcast runtime.
- **`brand.json`** — `colors.text`, `colors.base`, `colors.accent` and
  `colors.edge` are all mandatory. `fonts.hook` and `fonts.small` must
  name files that actually exist once you've added fonts (see below).
  `output` sets the video window's size and position within the
  1080x1920 frame; the values here are a reasonable default. Leave
  `subtitles.enabled` at `false` until you're ready for them (see
  "Subtitles" in the repository's `README.md`).

## What to add

- **`fonts/`** — this directory doesn't exist yet; create it and put your
  `.ttf` files there, then point `brand.json`'s `fonts.hook`/`fonts.small`
  at them (paths are relative to the channel folder, e.g.
  `fonts/YourFont-Bold.ttf`).
- **`events/<event>/`** — create this per event, with a `sources.json`
  collected by hand (see "Collect clip titles and addresses" in the
  repository's `README.md`).

## Optional

`layout.py` (a custom accent element), `glossary.json` (proper nouns
Whisper doesn't know) and `assets/` (e.g. a logo) are all optional and
therefore not included here — see "Setting up a new channel" in the
repository's `README.md` for the full format of each.
