# Stage G3a — Channel CRUD in the Studio — Design

**Status:** approved, ready for planning
**Date:** 2026-07-23
**Follows:** Stage G2 (event CRUD). **Precedes:** G3b (brand & fonts editor).

## Problem

G2 let the studio create/rename/delete *events*. Adding or removing a whole
*channel* still means editing `channel.json` and directories by hand. G3a adds
channel-identity lifecycle to the start screen. Branding (colors/output/fonts)
and font upload — what actually makes a channel renderable — are the separate
G3b stage; G3a is the directory + `channel.json` identity layer it builds on.

## Goal

From the studio's channel list (the start screen), an operator can:

- **Create** a channel: a directory slug + the six `channel.json` identity
  fields, scaffolded with a default `brand.json` and empty `fonts/`/`events/`.
- **Edit** a channel's `channel.json` fields (display name, handle, footer, id,
  channel_url, language).
- **Rename** a channel: move its directory slug (all its events move with it),
  refused while any of its events is being rendered/detected.
- **Delete** a channel (hard delete, typed-slug confirmation).

## Scope / non-goals

- **A created channel is INCOMPLETE until G3b.** Its scaffolded `brand.json`
  points at font files that do not exist, so `profile.load` fails and its events
  cannot be opened in the editor until G3b adds fonts/branding. G3a does NOT add
  an "incomplete" marker to the channel list — that belongs to G3b, where a
  channel becomes complete.
- **No brand editing, no font upload** (G3b).
- **No soft-delete/undo.** Delete is a hard `rmtree`, guarded by the frontend's
  typed-slug confirmation. Deleting a channel removes all its events with it.
- The channel's OAuth token (`<workspace>/auth/token-<id>.json`, keyed by the
  YouTube channel id, not the directory) is **not** touched by delete — it lives
  outside the channel directory and the studio never writes `auth/` except via
  the connect flow. A deleted channel may leave an orphaned token; that is
  acceptable and noted.

## Backend

### Shared path-segment validation — `src/yt_shorts/pathnames.py` (new)

The G2 final review found a path-traversal from an unvalidated segment. Both
event and channel admin need the identical "one safe path segment" rule, so it
moves to one place:

```python
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")   # \Z, not $ (no trailing newline)
MAX_NAME_LENGTH = 100

def validate_segment(value: str, *, what: str) -> None:
    """Raise ValueError (with a message naming `what`) if `value` is not one
    safe path segment: empty, > MAX_NAME_LENGTH, a leading dot, '..', a slash,
    or any char outside [A-Za-z0-9._-]."""
```

`validate_segment` raises a plain `ValueError`. Each admin module wraps it into
its own typed error so HTTP mapping stays per-module:
`event_admin.validate_name(name)` calls `validate_segment(name, what="event
name")` inside a `try/except ValueError` that re-raises
`EventAdminError(str(e), kind="bad_name")` — keeping event_admin's public API
and tests unchanged; `channel_admin` does the same, raising
`ChannelAdminError(str(e), kind="bad_name")`.

### `EventLock.is_held()` — `src/yt_shorts/lock.py` (new method)

A read-only check (does NOT create a lock file), so a channel operation can ask
"is any event under me being rendered?" without acquiring N locks:

```python
def is_held(self) -> bool:
    """True iff the lock file exists and names a live process (see
    _process_is_alive). A stale (dead-pid) or absent lock is not held."""
```

### `channel_admin.py` (new, pure — no FastAPI, like `event_admin.py`)

```python
class ChannelAdminError(Exception):
    # kind: "bad_name" | "bad_field" | "not_found" | "exists" | "locked"
    def __init__(self, message, kind): ...

REQUIRED_FIELDS = ["id", "channel_url", "handle", "display_name", "language", "footer"]

DEFAULT_BRAND = { ...the templates/example-channel brand.json, embedded... }

def create_channel(channels_dir, slug: str, fields: dict) -> None:
    """Validate slug (segment) and the six required fields (non-empty); refuse
    if the channel dir exists; write channel.json (the given fields), scaffold a
    default brand.json (DEFAULT_BRAND), and mkdir fonts/ and events/."""

def update_channel(channels_dir, slug: str, fields: dict) -> None:
    """Validate slug; require the channel to exist; merge the provided fields
    into channel.json (partial - only the keys present); reject if any REQUIRED
    field ends up empty; write channel.json."""

def rename_channel(channels_dir, old: str, new: str) -> None:
    """Validate both slugs; require old to exist and new not to; refuse (locked)
    if ANY event under old holds a live EventLock; move old -> new."""

def delete_channel(channels_dir, slug: str) -> None:
    """Validate slug; require it to exist; refuse (locked) if ANY event holds a
    live EventLock; shutil.rmtree the channel dir."""
```

Details:
- **Every slug is validated before any filesystem touch** (create/update/
  rename/delete, and both `old` and `new` in rename) via `validate_segment` — a
  `..`/slash/leading-dot slug can never make an op act outside `channels/`. This
  is the same boundary G2 has, applied to the channel segment (the exact gap
  the G2 review caught).
- **Required-field validation** matches `profile.REQUIRED_CHANNEL_FIELDS`
  (non-empty). A missing/blank required field → `ChannelAdminError(kind="bad_field")`.
  (`assets` is optional and not collected — it is racecast-documentary.)
- **The lock check spans all events.** `rename_channel`/`delete_channel` iterate
  `channels/<slug>/events/*/` and refuse (kind="locked") if any
  `EventLock(event_dir).is_held()`. This is a check (a small TOCTOU window
  remains — acceptable for a local single-user tool, as the operator will not
  render and restructure a channel simultaneously); it is NOT an acquire-all.
- **Scaffold on create:** `channel.json` (the fields), `brand.json`
  (`DEFAULT_BRAND`), empty `fonts/`, empty `events/`. `DEFAULT_BRAND` is an
  embedded constant equal to `templates/example-channel/brand.json`, so the
  module stays pure and workspace/repo-layout-independent.

### Studio API routes (in `studio/api.py`)

- `POST   /api/channels`  body `{slug, id, channel_url, handle, display_name, language, footer}`
  → `create_channel`. 201 with the new channel's `ChannelInfo` listing entry.
- `PATCH  /api/channels/{channel}`  body `{<any channel.json fields>}`
  → `update_channel`. 200 with the channel's `ChannelInfo`.
- `POST   /api/channels/{channel}/rename`  body `{name}`
  → `rename_channel`. 200 with the renamed channel's `ChannelInfo` (new slug).
- `DELETE /api/channels/{channel}`
  → `delete_channel`. 200 `{"deleted": "<slug>"}`.

A `_channel_status(kind)` maps `bad_name`/`bad_field`→400, `not_found`→404,
`exists`/`locked`→409. `POST /api/channels` shares the path of the existing
`GET /api/channels` (different method); `PATCH`/`DELETE` on the bare
`/api/channels/{channel}` path are new (auth is `…/auth`, events `…/events` —
no collision); `rename` is an explicit sub-action to stay unambiguous against
the field-editing `PATCH`.

Read-only-of-contents still holds elsewhere: the only new writes are
`channel.json` (create/edit), the scaffold, and whole-directory rename/delete —
nothing edits an event's clip/transcript/short data, and the write boundary
note in CLAUDE.md is extended to name channel identity + scaffold.

## Frontend

### `ChannelsScreen.tsx` gains controls

- A **"New channel"** button opens a dialog with a **slug** field (validated
  live) and the six `channel.json` fields; `channel_url` auto-fills from `id`
  (`https://www.youtube.com/channel/<id>`, editable). Create is disabled until
  the slug is valid and every required field is non-empty. On success the
  channel appears (refetch) and the dialog closes.
- Each channel row gets a **⋯ menu** (a sibling of the navigating button — not
  nested — with `stopPropagation` and a portalled dropdown, exactly as G2's
  EventsScreen does) with **Edit**, **Rename**, **Delete**:
  - **Edit** → a dialog pre-filled with the channel's `channel.json` fields →
    `PATCH` → the row updates.
  - **Rename** → a dialog with a new **slug** field (validated) → `POST
    …/rename` → on success navigate to the new slug's URL (its events came along).
  - **Delete** → a dialog requiring the operator to **type the slug** to enable
    the destructive button, warning that all N events of the channel are removed
    → `DELETE` → the row disappears.
- Mutations disable their dialog's submit and show a loading state; a failed
  request surfaces the server `detail` inline ("a channel named X already
  exists", "an event of this channel is being rendered", "not a valid …").

### Pure logic (Vitest-tested, not exported from a component)

- `isValidSlug(name)` — the same slug rule as the backend segment (shared with
  events; the existing `eventAdmin.ts` rule is generalised into a `slug.ts` and
  both use it). `deleteConfirmed` is reused.
- `requiredChannelFields(fields)` / a small "are all required fields non-empty"
  helper for the create/edit dialogs.
- `api.ts`: `createChannel(payload)`, `updateChannel(channel, fields)`,
  `renameChannel(channel, name)`, `deleteChannel(channel)`.

## Testing

- **`pathnames.py`**: `validate_segment` accept/reject table (accept `race-1`,
  `Round_2`, `a.b-c`; reject ``, `.hidden`, `..`, `a/b`, `/x`, `a b`, 101 chars,
  `-x`, `x\n`).
- **`channel_admin.py`** (pure, tmp dirs): create writes `channel.json` +
  `brand.json` + `fonts/` + `events/`; create rejects a missing required field
  (`bad_field`), an existing channel (`exists`), a traversal slug (`bad_name`,
  nothing escapes `channels/`); update merges fields and rejects blanking a
  required one; rename moves the dir and its events, rejects a target that
  exists, and rejects when an event holds a live lock (seed a `.render.lock`
  with this pid) — asserting the source is untouched; delete removes the dir and
  rejects a live event lock; a traversal `{channel}` in rename/delete raises
  `bad_name`.
- **`lock.py`**: `EventLock.is_held()` — a live-pid lock → True, a stale
  (impossible-pid) or absent lock → False.
- **Studio API** (G1 copytree fixture): each of the four routes' success (the
  channel dir really appears/updates/moves/disappears) and each failure code
  (400/404/409); a percent-encoded `..` channel is 400 (not an escape); a held
  event lock makes rename and delete 409.
- **Vitest**: `isValidSlug` and the required-fields helper.
- **E2E** (real Chromium): from the start screen, create a channel (it appears
  AND its directory exists on disk under the fixture's tmp channels dir), edit a
  field (the row updates), rename its slug (the row/URL and the directory move),
  delete it with the typed-slug confirmation (it disappears and the dir is
  gone). Reuse the E2E's existing server/seeding/on-disk assertions.

## Files

- **Create:** `src/yt_shorts/pathnames.py`, `src/yt_shorts/channel_admin.py`,
  `tests/test_pathnames.py`, `tests/test_channel_admin.py`,
  `src/yt_shorts/studio/web/src/slug.ts` (+ its Vitest), the channel dialog
  component(s).
- **Modify:** `src/yt_shorts/event_admin.py` (use `pathnames.validate_segment`),
  `src/yt_shorts/lock.py` (`is_held`), `tests/test_lock.py` (is_held),
  `studio/api.py` (four routes), `studio/web/src/api.ts` (four calls),
  `studio/web/src/eventAdmin.ts` (reuse the shared slug rule),
  `studio/web/src/components/ChannelsScreen.tsx` (controls),
  `tests/test_studio_api.py`, `tests/test_studio_e2e.py`, `static/` (rebuilt),
  `CLAUDE.md` (write boundary + G3a note), `README.md`.
