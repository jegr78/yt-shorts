# Where the data lives

`workspace.resolve()` picks the data directory in this order, and the tool
prints which one it picked at the start of every command:

1. **`YT_SHORTS_DATA`**, if set. A path that does not exist is an error, not
   a silent fallback to something else.
2. **the workspace you last selected** in the studio's settings, if it still
   exists. Unlike the env var this one is skipped silently when it is gone —
   it is a remembered choice, not an instruction, and a stale one must not
   stop the tool from starting.
3. **`~/YT-Shorts-Data`**, if it exists.
4. **the repository's own `channels/`** — the layout every command used
   before a workspace existed.

Creating `~/YT-Shorts-Data` is the entire migration switch — no flag, no
cutover date. `bin/yt-shorts migrate <channel>/<event>` copies one event
from the repository layout into the resolved workspace: it copies (never
moves), verifies every file it copies by checksum before reporting success,
and leaves the repository's originals untouched for the operator to delete
by hand once satisfied.

**Every glyph on screen is drawn as a PNG layer, not by ffmpeg.** This
ffmpeg has no `drawtext` and no `subtitles` filter, so all text comes from
Pillow and is composited on top of the picture. Do not reinstall or upgrade
ffmpeg to get those filters back — see
[Hard constraints](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md#hard-constraints).

## Brand as data, motif as an optional module

![The overlay on its own: the veil, the opaque edges, the hook and the footer,
transparent where the video window goes](images/overlay.png)

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
`config["decorate"]`. A channel without `layout.py` automatically gets
plain bars, and a new channel needs no code anywhere — see
[Architecture](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md#architecture)
for why the overlay knows nothing channel-specific.

## Brand is per event, not only per channel

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
