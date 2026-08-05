# Two-Class Upload (owned vs. render-only) — Design

**Status:** approved, ready for planning
**Date:** 2026-07-22
**Follows:** Stage E (upload), Stage G1 plan (workspace shell)

## Problem

The YouTube Data API can only `videos.insert` to channels the OAuth identity
**owns** (the personal account plus owned brand accounts like ERF). The operator
also **manages/edits** other channels (Community League Racing, International
Racing Organisation, CTC Livestreams, Race Asylum, Community Team Cup). Those are
a YouTube-Studio-web-only delegation: the Data API cannot upload to them, and
they do not even appear in the OAuth brand-account chooser, so the existing
connect flow's verify-and-refuse guard already refuses a token for them.

Today the tool assumes every channel is API-uploadable. For a manager/editor
channel that means: no way to connect (correctly refused), and an Upload button
that would only ever fail. The operator's real workflow for those channels is
**render locally → download the short → upload it by hand in YouTube Studio**.

## Goal

Give every channel an explicit upload **class** and make the whole tool — CLI and
studio — behave correctly for both:

- **`api`** (owned, the default): unchanged. Connect + API upload as today.
- **`manual`** (render-only): no connect, no API upload; instead the studio
  offers the rendered short as a download and the upload metadata as
  copy-to-paste text for a manual upload in YouTube Studio.

## Non-goals

- No CMS / `onBehalfOfContentOwner` path (needs a YouTube content-owner setup,
  out of scope). `manual` is the supported answer for manager/editor channels.
- No auto-detection of ownership from the network. The class is declared; the
  existing connect guard remains the hard runtime boundary.
- No change to privacy (always `private` on the API path), the re-upload guard,
  or the verify-and-refuse auth check.

## Data model

The class lives inside the **existing** `config["upload"]` block (which already
holds `description`, `tags`, `category_id`, `made_for_kids`), so it deep-merges
event-over-channel like every other profile value.

```jsonc
// channel.json (or brand.json) — the upload block
"upload": {
  "mode": "manual",          // "api" (default) | "manual"
  "description": "...",       // unchanged
  "tags": ["..."],           // unchanged
  "category_id": "17"        // unchanged
}
```

- **Absent `upload` block or absent `mode` ⇒ `"api"`.** ERF and every existing
  channel keep working with no edit.
- **Validation** (`profile.py`): if `upload.mode` is present and not one of
  `"api"` / `"manual"`, raise `ProfileError` — collected together with all other
  profile defects, as `profile.py` already does. No new required field.

## The shared predicate

One small module, imported by both CLI and studio, so the rule lives in exactly
one place (DRY) and is enforced everywhere (defense in depth).

**New file `src/yt_shorts/upload_policy.py`** — imports nothing heavy (no FastAPI,
no google), pure functions over the flat `config` dict:

```python
class RenderOnlyError(Exception):
    """Raised when an API-upload path is reached for a render-only channel."""

def mode(config: dict) -> str:
    """"api" (default) or "manual"; trusts profile validation for the value."""

def is_render_only(config: dict) -> bool:
    return mode(config) == "manual"

def require_api_upload(config: dict) -> None:
    """No-op for api channels; raises RenderOnlyError(message) for manual ones."""
```

The `RenderOnlyError` message is the single operator-facing sentence, reused by
every caller: *"channel is render-only (upload.mode=manual): render the short,
download it, and upload it by hand in YouTube Studio."* (CLI callers may prefix
the channel identifier.)

## Enforcement points (four)

All four consult `upload_policy`; none reimplements the check.

1. **`cmd_auth` (CLI)** — `manual` ⇒ refuse before any consent, print the
   message, exit non-zero. Nothing to connect (a token would be refused anyway).
2. **`cmd_upload` (CLI)** — `manual` ⇒ refuse the whole run before any clip, print
   the message, exit non-zero. No partial API attempt.
3. **`POST /api/auth/connect` (studio)** — `manual` ⇒ HTTP 409 with the message.
4. **`POST /api/clips/{name}/upload` (studio)** — `manual` ⇒ HTTP 409 with the
   message.

`api` channels are unaffected at all four points. The verify-and-refuse guard
inside `authorize` still runs for `api` channels and still refuses a mismatched
channel.

## Studio API surface

- **`GET /api/auth`** gains one field: `"upload_mode": "api" | "manual"`. This is
  the single signal the frontend reads to choose which controls to render. The
  rest of the response (connected, remaining_uploads, …) is unchanged and, for a
  `manual` channel, simply reflects "not connected" (which is correct).
- **`GET /api/clips/{name}/upload-preview`** is **unchanged and open for both
  modes** — it is `build_metadata` (pure, no auth, no network) and is the source
  for the copy-to-paste metadata in manual mode. It is NOT guarded.
- **`GET /api/clips/{name}/short`** is **unchanged** — it already streams
  `short.mp4` as a `FileResponse` with `filename=…`, i.e. a download. Manual mode
  reuses it; no new endpoint.

## Studio frontend

The frontend reads `upload_mode` from `/api/auth` and picks controls accordingly.
The selection/formatting logic goes into a pure module `upload.ts` (not exported
from a component, to keep Vite's fast-refresh boundary component-only) and is
unit-tested with Vitest.

**`api` mode:** unchanged — connect dialog, upload confirmation with the metadata
preview, force-auth, etc.

**`manual` mode — per kept, rendered clip:**
- **No** connect button, **no** upload button.
- A **"Download short"** action pointing at `GET /api/clips/{name}/short`.
- A **"Metadata to copy"** panel sourced from `GET /api/clips/{name}/upload-preview`:
  - **Title** — copy button.
  - **Description** — copy button.
  - **Tags** — formatted as a **comma-separated** string (as YouTube Studio's tag
    field expects); this formatting is the pure, Vitest-tested function in
    `upload.ts`. Copy button.
  - **Category** — shown for reference (copy optional).
  - A **"copy all"** convenience.
  - A one-line note: *privacy and "made for kids" are set by you in YouTube
    Studio* — these are deliberately **not** presented as copy values, because
    `build_metadata`'s `privacy=private` governs the API path, not a manual upload.

## Error handling

`RenderOnlyError` carries the one operator-facing message. CLI callers print it
and exit non-zero; studio routes map it to HTTP 409 with the same text. A `manual`
channel therefore cannot trigger an API upload even via a hand-crafted request —
the guard, not the hidden button, is the real boundary.

## Testing

Backend inline/TDD; frontend via a dispatched subagent. Controller verifies the
full `pytest` suite and `npm test` at the end.

- **`profile.py`:** `upload.mode` `"api"`/`"manual"` accepted; unknown value
  rejected (collected in `ProfileError`); absent `mode`/`upload` block ⇒ `"api"`.
- **`upload_policy.py`:** `mode()` default and both values; `require_api_upload`
  is a no-op for `api` and raises `RenderOnlyError` for `manual`;
  `is_render_only` matches.
- **CLI:** `cmd_auth` and `cmd_upload` refuse a `manual` channel (netless,
  injected `oauth`, no clips touched) and exit non-zero; the `api` path is
  unchanged and still green.
- **Studio (Starlette TestClient):** `/api/auth` reports `upload_mode` for both a
  fixture `api` channel and a fixture `manual` channel; `/api/auth/connect` and
  `/api/clips/{name}/upload` return 409 for `manual`; `upload-preview` and
  `short` work for `manual`; the `api` path is unchanged.
- **E2E:** a `manual` fixture channel shows Download + copy-metadata and neither a
  connect nor an upload control; an `api` channel is unchanged.
- **Vitest:** the control-selection helper (given `upload_mode`) and the
  tag-formatting function in `upload.ts`.

A small `manual`-mode channel fixture is added under
`tests/fixtures/channels/` so the studio and CLI tests have a render-only profile
to exercise. It must not perturb the pinned overlay hashes (it is a separate
channel, not an ERF edit).

## Files

- **Create:** `src/yt_shorts/upload_policy.py`, its test, a `manual`-mode channel
  fixture, `yt_shorts/studio/web/src/upload.ts` + its Vitest test.
- **Modify:** `profile.py` (validate `upload.mode`), `bin/yt-shorts` (`cmd_auth`,
  `cmd_upload`), `studio/api.py` (`/api/auth` field; guard `connect` + `upload`),
  the studio frontend component(s) for the manual-mode controls, and `static/`
  (rebuilt). Docs: a line in `CLAUDE.md`'s upload section and `README.md`.
```
