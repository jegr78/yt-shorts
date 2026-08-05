# Event-level brand editing (colors / fonts / logo / output / subtitles) — design

Date: 2026-07-24
Status: approved (design), ready for implementation plan

## Motivation

A channel's brand (`brand.json`: colors, fonts, logo, output geometry,
subtitles) is fully editable in the studio UI — the "Brand" tab in the channel
screen (`BrandEditor.tsx` over `GET/PUT /api/channels/{channel}/brand`,
`POST/DELETE …/fonts/{filename}`, `POST …/brand/preview`). An **event** may
carry its own `brand.json` that `profile.load` **deep-merges leaf-by-leaf over
the channel brand** (`profile._load_brand_optional` + `merge.deep_merge`), but
there is **no UI and no API** to edit it. An operator who wants one event to
look different (a special color, a different logo variant, an event-specific
font) has to hand-edit JSON in the workspace. This closes that gap.

The event case is genuinely different from the channel case: an event
`brand.json` is a **partial override**, not a complete brand. `brand_admin`'s
validation requires a *complete* brand (the same checks `profile.load` runs);
the event editor instead validates the **merged result** and stores **only the
overridden sections**.

## Decided requirements

- **Placement:** a right-hand **Drawer** opened from the event editor's header
  (`App.tsx` AppShell). The editor already has the event's `Profile` and a live
  overlay preview, so this is the natural home; it never disrupts the clip
  workflow. The Drawer content must scroll reliably (project-wide mandatory
  visual-acceptance criterion — see the scroll memory / NavScreen pattern).
- **Override model:** **per section** (Colors, Fonts, Logo, Output, Subtitles).
  Each section has an **"Inherit from channel ⇄ Override"** switch. Inherited =
  the channel's values shown read-only/dimmed, nothing written. Overridden =
  editable fields (seeded from the *effective* value) written into the event
  `brand.json`. Overriding a section writes the **whole section** (all its
  fields), not per-leaf — predictable and matches the section switch.
- **Scope:** everything the channel editor covers **except `upload.mode`**:
  Colors, Fonts, Logo, Output geometry, Subtitles. `upload.mode` is the
  channel's YouTube-account class (owner vs. manager) and is never
  event-overridable.
- **Fonts:** full, including **event-specific font upload**. An event has its
  own `fonts/` directory; `profile._resolve_relative` already resolves a
  `fonts/<name>` ref **event-first**, so an event font shadows a channel font of
  the same name. The font picker shows the **union** of channel + event fonts,
  marking which are event-specific.
- **Validation is on the merged brand.** A partial override that only becomes
  valid once merged is accepted; a merge that produces an invalid brand is
  rejected. Same `profile._validate_*` checks the channel path uses.
- **Fully-inherited event → no override file.** When every section is inherited,
  the event `brand.json` is deleted (the event just uses the channel brand).

## Architecture

### Backend — `event_brand_admin.py` (new, pure, no FastAPI)

Mirrors `brand_admin.py`'s style (paths injected, `*AdminError.kind` mapping),
but with override semantics. Every path segment (`channel`, `event`, font
filename, and the `fonts/<name>` ref stored in the override) is validated via
`pathnames.validate_segment` before any filesystem touch.

- `EVENT_OVERRIDE_SECTIONS = ("colors", "fonts", "logo", "output", "subtitles")`
  — the whitelist. `upload` is deliberately excluded.
- `read_event_brand(channels_dir, channel, event) -> dict` →
  `{ "override": <event brand.json or {}>, "channel": <channel brand>,
     "effective": deep_merge(channel, override),
     "fonts": {"channel": [...], "event": [...]} }`.
  The UI needs all three brand views (to show inherited vs overridden vs the
  effective preview) plus the two font lists.
- `update_event_brand(channels_dir, channel, event, patch) -> None`. `patch` is
  the desired **override** object: it contains exactly the sections that should
  be overridden (each a full section), and omits the sections that should be
  inherited. Implementation:
  1. Reject any key outside `EVENT_OVERRIDE_SECTIONS` (esp. `upload`) →
     `bad_field`.
  2. Compute `merged = deep_merge(channel_brand, patch)`.
  3. Validate `merged` with the same validators `brand_admin._validate` uses
     (`profile._validate_subtitles/_validate_brand/_validate_logo`), resolving
     font refs against the event dir **then** the channel dir (event-first, as
     `profile` does). Font refs in the override must resolve to an existing file
     in either the event or channel `fonts/`.
  4. Write `patch` (the override) to the event `brand.json`. If `patch` is empty
     (all inherited), **delete** the event `brand.json` instead.
- `set`-style helpers are not needed (no event `upload.mode`).

### Backend — event fonts (mirror `font_admin` at event scope)

- `add_event_font` / `remove_event_font` write under `<event>/fonts/`. Same
  rules as the channel path: a `.ttf`/`.otf` that `PIL.ImageFont.truetype` can
  load, ≤ 10 MB, safe-segment filename, raw request body (no `python-multipart`).
- **Delete guard:** a font currently assigned as the event override's
  `fonts.hook`/`small` is refused deletion (409 `in_use`) until reassigned —
  same guard `font_admin` applies at the channel level. (A channel font is not
  deletable through the event route at all; the event route only manages the
  event's own `fonts/`.)
- `list_event_fonts` returns the event's own font files; `read_event_brand`
  surfaces both channel and event lists so the picker shows the union.

### Backend — routes (thin layer in `studio/api.py`)

Under the existing `EV = "/api/channels/{channel}/events/{event}"` prefix,
resolving the `Profile` from the path exactly like the other event routes:

- `GET  EV + "/brand"` → `read_event_brand(...)`.
- `PUT  EV + "/brand"` → `update_event_brand(...)`, body = the override object
  (a `BrandPatchBody`-shaped payload restricted to the five sections).
- `POST EV + "/fonts/{filename}"` (201) / `DELETE EV + "/fonts/{filename}"` —
  event font add/remove (raw body on add), mapping `in_use` → 409.
- `POST EV + "/brand/preview"` → render `overlay.build_overlay` on
  `deep_merge(channel_brand, <edited override>)` (fonts/logo resolved
  event-first) and return a PNG; any render failure → 409, like the channel
  preview and the clip preview.

`*AdminError.kind` → HTTP: `bad_name`→400, `not_found`→404, `bad_color`/
`bad_font`/`bad_subtitles`/`bad_brand`/`bad_field`→400, `in_use`→409.

### Frontend — Drawer + `EventBrandEditor`

- **Trigger:** a button in the editor header (`App.tsx` AppShell.Header, e.g.
  "Event branding") opens a Mantine `Drawer` (position right, size ~`lg`). The
  Drawer body is a scroll container (`overflow-y: auto`, bounded height) so
  every control is reachable — verified at a short viewport per the scroll
  mandate.
- **`EventBrandEditor` (new component):** event-scoped. For each of the five
  sections, an **"Inherit from channel / Override"** control (`SegmentedControl`
  or `Switch`):
  - *Inherit:* render the channel's values read-only/dimmed; the section is
    omitted from the saved override.
  - *Override:* render the editable field widgets, **seeded from the effective
    value**; the section is written into the override.
  - The field widgets (color inputs, logo card, output geometry, subtitles) are
    **reused from the existing `BrandEditor`/`brand.ts`** (extracted into shared
    pieces if needed) rather than duplicated — DRY.
- **Fonts section (override):** a picker over the **union** of channel + event
  fonts (event fonts badged), with **event font upload/delete** (the new event
  font routes). Assigning `hook`/`small` stores a `fonts/<name>` ref.
- **Live preview:** the Drawer shows a merged-brand preview via
  `POST …/events/{event}/brand/preview`, updating on edits (debounced like the
  channel brand preview).
- **Save:** `PUT …/events/{event}/brand` with the override object; on success
  the editor's own profile-derived preview (clip `PreviewPane`) reflects the new
  effective brand on the next render/preview. Pure helpers (which sections are
  overridden, seeding a section from effective, building the override payload)
  live in a non-component `.ts` module (Vite fast-refresh boundary) and are
  Vitest-tested, like `brand.ts`.
- **Scoped URLs:** built via the existing `scopedApi.ts` event-scoped base, same
  as the other event routes the editor already calls.

### Persistence & merge semantics

- The event `brand.json` holds **only overridden sections**. `deep_merge` fills
  the rest from the channel. Overriding a section stores the full section
  (seeded from effective), so there is no partial-within-section ambiguity.
- Inheriting a section removes it from the override; an empty override deletes
  the file.
- This is derived-vs-editorial-clean: `brand.json` is channel/event
  configuration a human edits, not derived data and not per-clip `edit.json`.
  The event brand write is a **new** studio write path outside `edit.json`,
  analogous to the existing channel-level brand/admin writes (G3a/G3b) — the
  studio may write channel- and event-level *configuration*, never an event's
  derived `clip.json`/`transcript.json`/short.

### Security

- Every segment (`channel`, `event`, font filename, stored `fonts/<name>` ref)
  is `pathnames.validate_segment`-checked before any filesystem touch, so
  `..`/slash/leading-dot can never reach disk — the same rule every other write
  op follows. The `fonts/<name>` ref is validated so a `fonts/../..` cannot
  smuggle a traversal into a later `profile.load`.
- Writes go through the existing studio CSRF/origin guard (same as channel
  brand/admin writes). No new secret surface: brand/fonts are appearance config,
  never tokens.

## Testing

- **`event_brand_admin` (pure):** override round-trip (write only overridden
  sections; inherit removes a section; all-inherited deletes the file); merged
  validation (a partial override valid only after merge is accepted; an override
  that makes the merge invalid is rejected); `upload` in the patch is rejected
  (`bad_field`); font ref resolves event-first; bad segment rejected. Paths
  injected.
- **event fonts (pure):** add/remove under the event `fonts/`; reject
  non-font/oversized/bad-name; `in_use` refusal when assigned as event
  hook/small.
- **Studio API:** `GET/PUT EV/brand` (happy path + merged-validation 400 +
  unknown channel/event 404 + `upload` rejected); event fonts CRUD (201/anything
  → 409 `in_use`); `POST EV/brand/preview` (200 PNG, 409 on render failure).
  In-process `TestClient`, same pattern as the channel brand API tests.
- **Vitest:** the pure frontend helpers (overridden-section detection, override
  payload builder, seeding a section from effective).
- **Playwright E2E (in pytest):** open the editor, open the Event-branding
  Drawer, override **Colors** with a new value, save, and assert (a) the event
  `brand.json` on disk contains **only** the `colors` section, (b) the merged
  effective brand reflects it (e.g. via `GET EV/brand`'s `effective`), and (c)
  the Drawer scrolls at a short viewport. Reuse the live-server harness.
- Full pytest suite green, `npm test` green, `python3 tools/lint.py` green,
  `npm run build` committed (`static/`). No overlay-hash regression (this adds a
  new render path but does not change `overlay.build_overlay`).

## Out of scope (explicitly)

- Inheritance beyond one level (only channel → event).
- Overriding `upload.mode` at the event level.
- Editing an event's `layout.py` (decorate), `glossary.json`, or `moments.json`
  — those are not brand and follow different (non-deep-merge) rules.
- Multi-event bulk brand edits.
- Deleting/renaming a channel font through the event route (the event route
  manages only the event's own `fonts/`).

## Notable risks / decisions carried forward

- **Partial override vs. complete-brand validation:** the event path must
  validate the **merged** brand, never the partial override alone — reusing the
  channel validators on `deep_merge(channel, override)`. Getting this wrong
  would either reject valid overrides or accept a broken merge.
- **Event-first font resolution:** an event font shadows a same-named channel
  font. The picker must make event-specific fonts visible so this is intentional,
  not surprising.
- **Whole-section override:** chosen over per-leaf to match the per-section
  switch and keep the merge predictable; a future per-leaf refinement stays
  compatible (deep_merge already merges per key).
- **Scroll:** the Drawer is a new full-height surface — it must own its scroll
  (mandatory visual-acceptance criterion), verified at a short viewport.
