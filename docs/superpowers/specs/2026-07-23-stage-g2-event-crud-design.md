# Stage G2 — Event CRUD in the Studio — Design

**Status:** approved, ready for planning
**Date:** 2026-07-23
**Follows:** Stage G1 (workspace shell — read-only navigation)

## Problem

G1 made the studio a workspace shell: a start screen of channels → events →
the editor, all **read-only**. An operator still has to drop to a shell to
create a new event directory, rename one, or remove one. G2 adds those three
lifecycle operations to the studio's event list.

## Goal

From the studio's event list for a channel, an operator can:

- **Create** a new (empty) event.
- **Rename** an existing event.
- **Delete** an event (hard delete, guarded by a typed confirmation).

## The write boundary (a deliberate G1 extension)

G1's rule was "the studio writes `edit.json` and nothing else." G2 extends it
**only** to the event LIFECYCLE: the studio may now **create, rename, and
delete event directories** under `channels/<channel>/events/`. Inside an event
it still writes nothing but `edit.json` — it never edits `clip.json`,
`transcript.json`, a rendered short, `sources.json`, or any other derived or
editorial content. Creating an event makes an empty directory; renaming moves
the whole directory; deleting removes the whole directory. This is stated
explicitly in CLAUDE.md alongside the existing boundary.

## Non-goals

- **No source seeding on create.** A new event is an empty directory. It is
  populated the existing ways — the studio's Streams → detect flow (which
  writes clips under the event), or a CLI `harvest`. G2 adds no harvest/source
  editing.
- **No channel CRUD** (that is G3) and **no settings page** (G4).
- **No soft-delete / trash.** Delete is a hard `rmtree`, guarded by the
  frontend's typed confirmation (the operator types the event name).
- **No undo.**

## Backend

### `src/yt_shorts/event_admin.py` (new, pure — no FastAPI)

Filesystem operations over the workspace's channels dir, each raising a typed
`EventAdminError` on bad input. Mirrors `workspace_listing.py`: imports nothing
heavy (no FastAPI, no google); depends only on the stdlib and `lock.EventLock`.

```python
class EventAdminError(Exception):
    """A create/rename/delete request that cannot be honoured (bad name,
    already exists, unknown, or a job is running)."""

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_NAME_LENGTH = 100

def validate_name(name: str) -> None:
    """Reject a name that is not a safe single path segment: empty, too long,
    a leading dot, '..', a slash, or any char outside [A-Za-z0-9._-]. Raises
    EventAdminError. This is the security boundary - an event name becomes a
    directory name, so a '../' or an absolute path must never slip through."""

def create_event(channels_dir, channel: str, name: str) -> None:
    """Validate name; require the channel to exist; refuse if the event already
    exists; then mkdir channels/<channel>/events/<name>/."""

def rename_event(channels_dir, channel: str, old: str, new: str) -> None:
    """Validate new; require old to exist and new not to; acquire the event's
    EventLock (raising if a render/detect is live) so a job cannot be running
    against the directory being moved; then move old -> new."""

def delete_event(channels_dir, channel: str, name: str) -> None:
    """Require the event to exist; acquire its EventLock (raising if a job is
    live); then shutil.rmtree the event directory."""
```

Details:
- **Name validation is the security boundary.** `NAME_PATTERN` allows a single
  safe path segment only: no `/`, no leading `.`, no `..`, length ≤
  `MAX_NAME_LENGTH`. A bad name raises `EventAdminError` before any filesystem
  touch. Both the create and the rename target go through it.
- **`EventLock` closes the race.** `rename_event`/`delete_event` call
  `EventLock(event_dir).acquire()` (from `yt_shorts.lock`). If a live render or
  detect holds the lock, `acquire()` raises `LockError` → surfaced as an
  `EventAdminError` (mapped to 409). Holding the lock also prevents a *new*
  render from starting mid-operation (no TOCTOU window). A stale lock from a
  crashed run is taken over, exactly as a render would — a crash must not make
  an event undeletable.
  - **Delete:** `shutil.rmtree(event_dir)` removes the directory *and* the lock
    file with it — nothing to release.
  - **Rename:** the acquired lock file (`.render.lock`, recording the
    long-lived studio process's pid) moves with the directory, so after the
    move `rename_event` must **release the moved lock** — remove
    `<new_dir>/.render.lock` (via `EventLock(new_dir).release()`). Skipping this
    would leave the renamed event holding a *live*-pid lock and make the next
    render refuse it. If the move itself fails, the lock on the original
    directory is released in a `finally`.
- **Refusals are typed and specific:** unknown channel, unknown event, target
  exists, bad name, and "a job is running" each raise `EventAdminError` with a
  message the route passes through.

### Studio API routes (channel-scoped, in `studio/api.py`)

Added next to G1's listing routes; all resolve `channels_dir` the same way
(`_profile_module.CHANNELS_DIR`, read live).

- `POST   /api/channels/{channel}/events`  body `{"name": "<slug>"}`
  → `event_admin.create_event`. 200 with the created event's listing entry;
  400 (bad name), 404 (unknown channel), 409 (already exists).
- `PATCH  /api/channels/{channel}/events/{event}`  body `{"name": "<slug>"}`
  → `event_admin.rename_event`. 200 with the renamed event's listing entry;
  400 (bad name), 404 (unknown), 409 (target exists or a job is running).
- `DELETE /api/channels/{channel}/events/{event}`
  → `event_admin.delete_event`. 200 `{"deleted": "<event>"}`; 404 (unknown),
  409 (a job is running).

The route layer maps `EventAdminError` to the right 4xx: a not-found message →
404, an exists/locked message → 409, a bad-name message → 400. To keep the
mapping unambiguous rather than string-sniffing, `EventAdminError` carries a
`kind` field (`"not_found" | "exists" | "locked" | "bad_name"`) the route
switches on. The typed **delete confirmation lives in the frontend**; the
backend deletes on the local call (single-user tool), guarded only by the lock.

Read-only routes and the `edit.json`-only content rule are unchanged; these
three are the only writes G2 adds, and they act on directories, never on an
event's derived/editorial files.

## Frontend

### `EventsScreen.tsx` gains controls

- A **"New event"** button opens a small dialog with a name field, validated
  client-side against the same slug rule (inline error), then `POST`s. On
  success the new event appears in the list (and the dialog closes).
- Each event row gets a **"⋯" menu** (or two icon buttons) with **Rename** and
  **Delete**:
  - **Rename** opens a dialog pre-filled with the current name → `PATCH` → the
    row updates.
  - **Delete** opens a dialog that requires the operator to **type the event
    name exactly** to enable the destructive button; it shows a warning when
    the event is non-empty (from the listing's `clip_count`, and calls out
    `rendered_count` since those shorts are lost) → `DELETE` → the row is
    removed.
- While a mutation is in flight the row's controls disable and show a loading
  state; a failed request shows the server's message inline ("an event named X
  already exists", "a job is running for this event", "not a valid event name").

### `eventAdmin.ts` (new, pure — Vitest-tested, not exported from a component)

- `isValidEventName(name: string): boolean` — the same slug rule as the
  backend, so the dialog can validate before sending.
- `deleteConfirmed(typed: string, eventName: string): boolean` — the
  confirmation gate (`typed.trim() === eventName`), the single source of the
  "type the name" rule the delete button is disabled behind.

`api.ts` gains `createEvent(channel, name)`, `renameEvent(channel, event,
name)`, `deleteEvent(channel, event)` targeting the routes above (unscoped
channel-level, like `getEvents`).

## Testing

- **`event_admin.py`** (pure, tmp dirs, no FastAPI): create/rename/delete happy
  paths; every refusal — bad name (`../x`, `/x`, `.hidden`, empty, over-length,
  a slash), unknown channel, unknown event, target already exists, and a held
  `EventLock` (seed a live lock file with the current pid) makes rename/delete
  raise. Assert the `kind` on each raised `EventAdminError`.
- **Studio API** (copytree fixture from G1): each route's success (the event
  dir really appears/moves/disappears on disk) and each failure code
  (400/404/409). One test that a `.render.lock` held by this process makes
  DELETE and PATCH return 409.
- **Vitest**: `isValidEventName` (accept/reject table) and `deleteConfirmed`
  (exact-match gate, trimming, mismatch).
- **E2E** (real Chromium): from the start screen, open a channel, create an
  event (it appears), rename it (the row updates and the URL still works),
  delete it with the typed confirmation (it disappears). Assert against the
  real on-disk directory via the same seeding the E2E already uses.

## Files

- **Create:** `src/yt_shorts/event_admin.py`, `tests/test_event_admin.py`,
  `src/yt_shorts/studio/web/src/eventAdmin.ts` + its Vitest test, and the
  create/rename/delete dialog components (or one small `EventAdminDialogs.tsx`).
- **Modify:** `studio/api.py` (three routes), `studio/web/src/api.ts` (three
  calls + types), `studio/web/src/components/EventsScreen.tsx` (the controls),
  `tests/test_studio_api.py` and `tests/test_studio_e2e.py` (new tests),
  `static/` (rebuilt), `CLAUDE.md` (the extended write boundary), `README.md`
  (the studio now creates/renames/deletes events).
