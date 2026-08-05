# Stage G3b — Brand & Fonts Editor in the Studio — Design

**Status:** approved, ready for planning
**Date:** 2026-07-23
**Follows:** Stage G3a (channel CRUD). Completes the channel-onboarding arc.

## Problem

A channel created in G3a is not renderable: its scaffolded `brand.json` points
at font files that do not exist, so `profile.load` fails and its events cannot
be opened. G3b adds the editor that makes a channel renderable — upload fonts,
assign them, set colors and the subtitles toggle — with a live preview of the
brand overlay.

## Goal

From a channel, an operator can:

- **Upload** `.ttf`/`.otf` font files to the channel and **assign** them to the
  `hook` and `small` roles; **delete** an unused font.
- **Edit** the channel's `brand.json` colors (`text`/`base`/`accent`/`edge`) and
  the `subtitles.enabled` toggle.
- See a **live preview** of the brand overlay (`overlay.build_overlay`) that
  updates as they edit, so they can tune colors and fonts against a real render.

Once a font is uploaded and assigned, the channel loads and renders.

## Scope / non-goals

- **Output dimensions are NOT editable** (kept at the scaffold defaults —
  1080×1920 portrait). No caption-box geometry form.
- **Channel-level brand only.** Per-event brand overrides (the event `brand.json`
  merge that `profile.load` supports) are not edited here.
- **No logo / `layout.py` (`decorate`) editing** — those stay file/code-based.
- The live preview renders the **overlay only** (`build_overlay`: base veil,
  edges, hook, footer). It does NOT draw captions — `build_overlay` never has;
  the subtitles toggle only sets the flag used at render time, without a caption
  preview.
- No new heavy dependency: font upload uses the raw request body (bytes), not
  multipart (`python-multipart` is not installed).

## Backend

### `font_admin.py` (new — uses PIL to validate, no FastAPI)

```python
class FontAdminError(Exception):    # kind: "bad_name" | "bad_type" | "too_big" | "invalid" | "not_found" | "in_use"
    def __init__(self, message, kind): ...

FONT_EXTENSIONS = (".ttf", ".otf")
MAX_FONT_BYTES = 10 * 1024 * 1024   # 10 MB

def list_fonts(channels_dir, channel: str) -> list[str]:
    """Sorted .ttf/.otf filenames in channels/<channel>/fonts/ (empty if none)."""

def save_font(channels_dir, channel: str, filename: str, data: bytes) -> None:
    """Validate filename (pathnames.validate_segment) and its extension
    (FONT_EXTENSIONS, case-insensitive); reject data over MAX_FONT_BYTES; reject
    data PIL cannot load as a font (ImageFont.truetype(BytesIO(data)) raises) -
    so build_overlay can never fail on it later; require the channel to exist;
    write channels/<channel>/fonts/<filename>."""

def delete_font(channels_dir, channel: str, filename: str) -> None:
    """Validate filename; require it to exist; refuse (kind="in_use") if the
    channel's brand.json fonts.hook or fonts.small references it; else delete."""
```

- **Filename is a safe segment** (`pathnames.validate_segment`) — the same
  boundary as slugs; a `..`/slash filename can never write outside `fonts/`.
- **The font must load.** `ImageFont.truetype(io.BytesIO(data))` is the gate:
  a non-font or corrupt file is rejected (`kind="invalid"`) before it is written,
  so a saved font is always renderable.

### `brand_admin.py` (new — no FastAPI)

```python
class BrandAdminError(Exception):   # kind: "not_found" | "bad_color" | "bad_font" | "bad_subtitles"
    def __init__(self, message, kind): ...

def read_brand(channels_dir, channel: str) -> dict:
    """The channel's raw brand.json (for the editor to prefill). 404-kind if the
    channel or its brand.json is missing/unreadable."""

def update_brand(channels_dir, channel: str, patch: dict) -> None:
    """Apply colors / fonts / subtitles from `patch` onto the channel's brand.json
    (output and any other keys kept), validate, and write. Validation:
    - colors: text/base/accent/edge each present and accepted by
      PIL.ImageColor.getrgb (a bad hex -> kind="bad_color").
    - fonts: hook and small each a "fonts/<name>" whose file exists under the
      channel's fonts/ dir (-> kind="bad_font" otherwise).
    - subtitles: if present, `enabled` must be a bool (-> kind="bad_subtitles").
    Output dimensions are never taken from `patch`."""
```

- Validation mirrors `profile.REQUIRED_COLOR_KEYS`/`REQUIRED_FONT_KEYS` and the
  font-file-exists rule of `profile._validate_brand`, so a brand this accepts is
  one `profile.load` will accept (fonts resolvable, colors drawable).

### Studio API routes (channel-scoped, in `studio/api.py`)

- `GET  /api/channels/{channel}/brand` → `{"brand": <brand.json>, "fonts": [names]}`
  (from `brand_admin.read_brand` + `font_admin.list_fonts`).
- `PUT  /api/channels/{channel}/brand` body `{colors, fonts, subtitles}` →
  `brand_admin.update_brand`; 200 `{"brand": <updated>}`; 400 (bad_color/font/
  subtitles), 404 (unknown channel).
- `POST /api/channels/{channel}/fonts/{filename}` — the **raw request body** is
  the font bytes (`await request.body()`) → `font_admin.save_font`; 201
  `{"fonts": [names]}`; 400 (bad_name/bad_type/too_big/invalid), 404 (unknown
  channel).
- `DELETE /api/channels/{channel}/fonts/{filename}` → `font_admin.delete_font`;
  200 `{"fonts": [names]}`; 404, 409 (in_use).
- `POST /api/channels/{channel}/brand/preview` body `{colors, fonts, subtitles}`
  → a PNG (`image/png`) of `overlay.build_overlay(<display_name>, <footer>,
  config)` where config is `{colors, fonts (resolved to absolute paths under the
  channel's fonts/ dir), output (the channel's current output)}`. The sample
  hook is the channel's `display_name` and the footer its `channel.json`
  `footer`. If a selected font file does not exist, 409 with a message telling
  the operator to upload/select a font. This is the live preview — a read, like
  the clip `POST …/preview`; it writes nothing.

Each `*AdminError.kind` maps to its status via a small mapper (bad_*→400,
not_found→404, in_use→409). Kinds and mapping stay per module, as in G2/G3a.

The write boundary in CLAUDE.md is extended: the studio may now write a
channel's `brand.json` and add/remove files under its `fonts/` dir. It still
never edits an event's derived/editorial content, and `auth/` only via connect.

## Frontend

### The `/{channel}` screen becomes a tabbed `ChannelScreen`

`EventsScreen` becomes `ChannelScreen` with two Mantine tabs — **Events** (the
existing event list plus the G2 controls, moved verbatim into the tab panel) and
**Brand** (the new editor). No new route (so it cannot collide with
`/{channel}/{event}`); `/{channel}` still resolves here.

### `BrandEditor` (the Brand tab) — two columns

- **Left (form):**
  - **Fonts:** an upload control (`<input type=file accept=".ttf,.otf">` → read
    the file as bytes → `POST …/fonts/<sanitised-name>`); the list of uploaded
    fonts, each with a delete button (a 409 in_use surfaces its message); two
    selects assigning `hook` and `small` from the uploaded fonts.
  - **Colors:** four Mantine `ColorInput`s (`text`/`base`/`accent`/`edge`),
    prefilled from `brand.json`.
  - **Subtitles:** a `Switch` bound to `subtitles.enabled`.
  - **Save** (`PUT …/brand`), disabled until the brand is valid to save (all
    colors valid, hook and small assigned) and something changed.
- **Right (live preview):** an `<img>` whose `src` is the object URL from
  `POST …/brand/preview` with the current form values, re-fetched (debounced
  ~300 ms) on every change. A preview that 409s (no font yet) shows a hint
  ("upload and assign a font to see the preview") instead of a broken image.
- Loading/empty/error states; a failed request surfaces the server `detail`
  inline. After a successful save, the preview and the fonts list reflect the
  saved state.

### Pure logic (`brand.ts`, Vitest-tested, not exported from a component)

- `isValidHexColor(value)` — accepts `#RGB`/`#RRGGBB` (what `ColorInput` emits
  and `ImageColor.getrgb` accepts).
- `brandReadyToSave(form)` — all four colors valid and both font roles assigned.
- `fontFilename(name)` — sanitise an uploaded file's name to a safe segment plus
  a lowercased `.ttf`/`.otf` extension (reject if it has no font extension).

### `api.ts`

`getBrand(channel)`, `saveBrand(channel, patch)`, `uploadFont(channel, filename,
bytes)`, `deleteFont(channel, filename)`, `brandPreview(channel, patch)` (returns
an object URL like the existing `fetchPreview`, or throws on 409).

## Testing

- **`font_admin.py`** (tmp dirs; use a real fixture `.ttf` for valid bytes —
  `tests/fixtures/channels/erf/fonts/BarlowCondensed-Bold.ttf`): save a valid
  font (it appears in `list_fonts`); reject a bad extension (`bad_type`), non-font
  bytes (`invalid`), over-size data (`too_big`), a traversal filename
  (`bad_name`, nothing escapes `fonts/`); delete removes it, and refuses
  (`in_use`) a font referenced by `brand.json`.
- **`brand_admin.py`** (tmp dirs, a channel scaffolded by `channel_admin` + a
  real font copied into `fonts/`): read returns the brand; update applies colors/
  fonts/subtitles and keeps `output`; rejects a bad hex (`bad_color`), a font not
  present under `fonts/` (`bad_font`), a non-bool `subtitles.enabled`
  (`bad_subtitles`); an accepted brand then loads via `profile.load` (assert the
  round-trip: after update with a real uploaded font, `profile.load("<ch>/<ev>")`
  succeeds for a seeded event).
- **Studio API** (G1 copytree fixture): GET brand returns brand+fonts; PUT
  validates and persists; POST font (raw body of a real `.ttf`) saves and lists
  it; DELETE font removes it and 409s when in use; POST preview returns a
  non-empty `image/png` with a real assigned font and 409s when the font is
  missing. A traversal `{channel}`/`{filename}` is rejected (400) — nothing
  escapes.
- **Vitest**: `brand.ts` (`isValidHexColor`, `brandReadyToSave`, `fontFilename`).
- **E2E** (real Chromium): open a channel's **Brand** tab, upload a font (bytes
  from a fixture `.ttf`), assign it to hook and small, change a color and see the
  preview `<img>` update, save; reload and confirm the saved brand and fonts.
  Reuse the E2E's server/seeding/on-disk assertions.

## Files

- **Create:** `src/yt_shorts/font_admin.py`, `src/yt_shorts/brand_admin.py`,
  `tests/test_font_admin.py`, `tests/test_brand_admin.py`,
  `src/yt_shorts/studio/web/src/brand.ts` (+ its Vitest), `BrandEditor.tsx` (and
  any small sub-components).
- **Modify:** `studio/api.py` (five routes + a preview builder), `studio/web/src/
  api.ts` (five calls), `studio/web/src/components/EventsScreen.tsx` → a tabbed
  `ChannelScreen.tsx` (Events + Brand), `Root.tsx`/wherever `/{channel}` renders
  (point it at `ChannelScreen`), `tests/test_studio_api.py`,
  `tests/test_studio_e2e.py`, `static/` (rebuilt), `CLAUDE.md`, `README.md`.
