# Setting up a new channel

Every `channels/<channel>/...` path below is relative to wherever
`workspace.resolve()` lands (see [Where the data lives](Where-the-data-lives)) — the
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
   see the `decorate` signature and example under
   [Brand as data, motif as an optional module](Where-the-data-lives#brand-as-data-motif-as-an-optional-module).
   If this element draws into the upper bar,
   `brand.json` should state the vertical space it takes up under
   `output.accent_offset`, so the hook text doesn't run into it (default 0
   if omitted). Without `layout.py`, it stays at plain bars.
6. Create the event folder: `channels/<channel>/events/<event>/` with
   `sources.json` (see
   [Collect clip titles and addresses](https://github.com/jegr78/yt-shorts/blob/main/README.md#1-collect-clip-titles-and-addresses),
   using the `channel_url` from `channel.json`).
7. **Optional:** give the event its own brand, fonts, assets or layout —
   e.g. because one event's clips carry different branding than the rest
   of the channel. Add whichever of these the event needs under
   `channels/<channel>/events/<event>/`:
   - `brand.json` — only the keys that differ from the channel, e.g.
     `{ "colors": { "accent": "#FF3355" } }`. Everything unnamed is kept
     from the channel (see
     [Brand is per event, not only per channel](Where-the-data-lives#brand-is-per-event-not-only-per-channel)
     for the full resolution order and merge rules).
   - `fonts/` — additional or overriding font files, referenced from the
     event's `brand.json` the same way the channel's are.
   - `assets/` — event-specific assets, e.g. a `logo.png` referenced
     under `brand.json`'s `logo.file`.
   - `layout.py` — an event-specific `decorate(draw, config, window_top,
     window_bottom)`, overriding the channel's for this event only.
   - `glossary.json` — an event-specific glossary (see "Glossary" under
     [Subtitles](https://github.com/jegr78/yt-shorts/blob/main/README.md#subtitles)).
     Unlike `brand.json`'s deep merge, this ADDS to the
     channel's `glossary.json` entry by entry: the event's own value for a
     term or replacement wins that entry, and everything it doesn't name is
     inherited from the channel — and, below that, from the workspace, the
     event's own selected circuit pack, and the (now empty) built-in default
     (see the same section for the full five-layer rule).
8. Run `bin/yt-shorts harvest <channel>/<event>` and
   `bin/yt-shorts render <channel>/<event>`.
