# Overlay colour control: logo-derived palettes and per-event band opacity

## Why

Two separate defects in how a short's colours are decided today.

**The channel palettes are wrong.** Four of the five channels in the operator's
workspace carry byte-identical colours — `base #101010`, `accent #144E53`,
`edge #B8F5CA` — and the last two of those are ERF's green. They are a
placeholder that was never revisited. ERF's own palette is no better: its
`base #004625` green came from the racecast ERF NLS assets in the first
single-channel version of this tool, where it was correct. It is not correct
now: ERF's logo is dark blue (`#012269` over 91% of its opaque pixels), and
the green appears nowhere in racecast's ERF profile either (that profile is
greys and whites). Every channel currently renders in another channel's
colours.

**The bands cannot be turned down.** `overlay.ALPHA_BASE = 150` and
`ALPHA_OPAQUE = 255` are module constants. There is no way to render a short
whose upper and lower thirds are just the blurred backdrop of the clip itself,
and no way to soften them for one event without changing them for every event
of that channel.

Both are colour decisions an operator should be able to make per channel and
per event, in the studio, with the preview they already have.

## Decisions

Settled in brainstorming, with the reasoning that decided each.

1. **Build the derivation, then migrate with it.** A one-off correction of the
   five files would leave the next channel in the same state. The migration is
   also the derivation's first real test.
2. **Opacity affects SURFACES only** — the veil, the channel decoration and the
   edge accent. The hook, the footer and the logo keep rendering at full
   strength on top of whatever is left. At opacity 0 a band is the blurred
   backdrop with the text still legible on it.
3. **One slider per band, not one per surface.** `0.0`–`1.0`, applied to every
   surface in that band, preserving their existing relationship (veil 150/255,
   edge fully opaque). Two controls, not four, and no third decision about
   which of them a channel decoration follows.
4. **`1.0` is exactly today.** The multiplier at 1.0 is skipped entirely rather
   than applied as an identity, so the six pinned overlay hashes stay
   byte-identical. `CLAUDE.md` requires this of any change not meant to alter
   appearance, and those hashes are the only guard that catches a subtle
   regression the assertions miss.
5. **ERF is migrated like every other channel.** Confirmed by the operator: the
   green was a first-pass choice from the racecast assets, correct while this
   was a single-channel tool and wrong now that channels and events exist.

## Data model

A new brand section, alongside `colors`, `fonts`, `output`, `subtitles`,
`logo` and `upload`:

```json
"bands": { "top": 1.0, "bottom": 1.0 }
```

Both keys are floats in `[0.0, 1.0]`. The section is **optional at every
layer**: an absent `bands`, an absent key within it, and the value `1.0` all
mean the same thing, so no existing `brand.json` needs touching and a profile
written before this feature keeps rendering identically. `profile.load`
normalises the merged result to a full `{"top": float, "bottom": float}` dict
so `overlay.build_overlay` never has to reason about absence.

Layering needs no new machinery. `profile.py` already deep-merges the event's
`brand.json` over the channel's leaf by leaf, and `event_brand_admin`'s
`OVERRIDE_SECTIONS` already models "this event overrides that section". `bands`
joins both lists and inherits the whole mechanism — including the editor's
existing per-section override/inherit switch.

**Validation** mirrors the existing pattern: `profile._validate_brand` collects
a defect per bad value rather than raising on the first, and
`brand_admin`/`event_brand_admin` apply the same rule so a brand the studio
accepts is one `profile.load` accepts. The rules are: `bands` must be a dict;
each present key must be `top` or `bottom`; each value must be a real number
(explicitly not a bool — `True` is an `int` in Python, the same trap
`REQUIRED_OUTPUT_KEYS`' check already guards) within `[0.0, 1.0]`.

## Rendering

The multiplier must reach every surface, including a channel decoration this
code has never seen — `overlay.py` deliberately knows nothing channel-specific
and calls whatever `config["decorate"]` holds. Threading an opacity parameter
into that call would push the responsibility into every channel's `layout.py`,
where it would be silently forgotten.

Instead `build_overlay` composes in two passes:

1. **Surface pass.** A transparent full-frame RGBA layer receives the base
   veil, then `decorate(...)`, then the edge accents — the same three
   operations in the same order as today, drawn onto a layer instead of the
   image. Row ranges `[0, window_top)` and `[window_bottom, height)` then have
   their alpha channel scaled by the top and bottom factor respectively. The
   video window's own rows are untouched: they are alpha 0 already, and the
   invariant that the window stays exactly transparent is what keeps the sharp
   picture unobscured.
2. **Content pass.** The layer is composited onto the image, and the logo, hook
   and footer are drawn on top exactly as today, at full strength.

Because the edge accents sit at `window_top - 6 … window_top - 1` and
`window_bottom … window_bottom + 5`, each falls inside its own band's row range
and follows that band's slider — which is what decision 3 promises.

**The byte-identical guarantee is structural, not incidental.** When a factor
is `1.0` the alpha scaling for that band is skipped, so the pixels are those
today's code produces, not those a round-trip through a multiply-by-one
happens to produce. `tests/test_event_layer_no_regression.py`'s six SHA-256
hashes are the proof, and they must not be re-pinned.

## Palette derivation

`palette.py` — a new pure module, no FastAPI, no studio import, like
`pathnames.py` and `upload_policy.py`. It takes an image path and returns a
proposal plus the swatches it came from:

1. Load the logo, drop every pixel with alpha ≤ 200. A logo is mostly
   transparent padding; including it biases every result toward one colour.
2. Quantise the remaining pixels to at most 8 colours (Pillow's
   `FASTOCTREE`, already a dependency) and order them by share.
3. Assign roles:
   - `base` — the darkest swatch holding at least 5% of the opaque pixels. The
     veil sits behind white text; a light base destroys legibility, so darkness
     outranks prominence here. The 5% floor keeps an anti-aliasing artefact —
     a handful of near-black edge pixels on an otherwise light mark — from
     being chosen as the channel's ground colour.
   - `edge` — the most saturated swatch that is not the base. This is the
     crispest brand element and the one that reads as "the channel's colour".
   - `accent` — the next most saturated, else a darkened `edge`. Only channel
     decorations use it, so a reasonable fallback beats failing.
   - `text` — `#FFFFFF` or `#111111`, whichever has more contrast against the
     chosen `base` (WCAG relative luminance).
4. Return both the four assigned roles and the full ordered swatch list.

**The proposal is a starting point, never an automatic overwrite.** The button
fills the editor's existing colour fields; nothing is written until the
operator saves, and the swatches render as clickable chips so any role can be
reassigned by hand. A logo that yields fewer than two usable swatches (a
single-colour mark) returns what it found and leaves the rest untouched rather
than inventing colours.

Derivation is a **read**: it needs the logo file the brand already names, and
it writes nothing.

## Studio

- `GET /api/channels/{channel}/brand/palette` — derive from the channel's
  configured logo. A read, like the existing preview routes; 409 when the brand
  names no logo or the file cannot be opened, matching how
  `POST …/brand/preview` reports a render it cannot perform.
- The channel Brand editor (`BrandEditor.tsx`) gains the "Derive from logo"
  button, the swatch chips, and two sliders for `bands.top`/`bands.bottom`.
  The sliders step in 5% increments and are labelled as percentages; the
  stored value stays a float, so `55%` is `0.55` on disk.
- The event Brand editor (`EventBrandEditor.tsx`) gains the same two sliders
  under a `bands` override section, using the override/inherit control it
  already has for the other five sections. It does **not** get the derive
  button: a palette comes from the channel's logo, and an event that wants
  different colours overrides `colors` directly, as it can today.
- Both editors' live previews already re-render on change; the sliders and the
  derived palette flow through them with no new preview plumbing.

## Migration

After the feature is in, apply it to the five workspace channels — derive,
inspect the preview, save. This is workspace data, not repository data.

`tests/fixtures/channels/erf/brand.json` is **not** migrated. It is the
suite's own copy and the six pinned hashes are computed from it; changing it
would break exactly the guard decision 4 exists to protect.

## Testing

- **Byte-identical output.** The six pinned overlay hashes, unchanged, with no
  `bands` key present — the default path.
- **The multiplier reaches every surface.** Pixel assertions in the style this
  suite already uses (measuring alpha at a point, not asserting a function
  returned): at `top: 0.0` the veil, a decoration pixel and the edge above the
  window are all alpha 0, while a hook pixel is unchanged; at `0.5` each is
  half its full-strength alpha; the bottom band is unaffected by the top
  factor and vice versa.
- **The window stays transparent** at every factor, including `1.0` and `0.0`.
- **Validation** rejects a non-dict, an unknown key, a bool, a string, and a
  value outside `[0.0, 1.0]`, and reports every defect at once.
- **Layering**: an event's `bands` overrides the channel's; an event without
  one inherits; a channel without one yields `1.0`.
- **Derivation** against real fixtures: a synthetic two-colour logo yields a
  predictable assignment; a fully transparent image is refused; a
  single-colour mark returns a partial proposal without inventing colours.
- **E2E**: set an event's band opacity in the studio, save, confirm the file
  records it and the preview changes.

## Out of scope

- Per-surface opacity (decision 3).
- Fading the hook, footer or logo (decision 2).
- Deriving a palette from anything but the channel logo — no event-level
  derivation, no derivation from a clip frame.
- Changing what the bands *are*: geometry stays `output`'s business.
