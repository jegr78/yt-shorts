# Stage G2 — Event CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator create, rename, and delete events from the studio's event list — the first writes the studio makes to event directories (G1 was read-only).

**Architecture:** A pure `event_admin.py` module (filesystem ops over the channels dir, no FastAPI, like `workspace_listing.py`) does create/rename/delete, raising a typed `EventAdminError(kind=…)`; rename/delete take the event's `EventLock` first so a running render/detect blocks them. Three channel-scoped studio routes (POST/PATCH/DELETE) map `EventAdminError.kind` to 400/404/409. The event-list screen gains New/Rename/Delete controls, with a typed-name confirmation gating delete.

**Tech Stack:** Python 3 stdlib + `yt_shorts.lock`; FastAPI (studio only); React + Vite + Mantine; Vitest + Playwright.

## Global Constraints

- `PYTHONPATH=src` mandatory for pytest. Full suite is green at the start of this plan (studio + E2E).
- **Write boundary:** the studio may now create/rename/delete event *directories*; INSIDE an event it still writes only `edit.json` — never `clip.json`, `transcript.json`, a rendered short, `sources.json`, or other derived/editorial content.
- `event_admin.py` imports nothing heavy (no FastAPI, no google), like `workspace_listing.py`.
- **Name validation is the security boundary:** an event name becomes a directory name. Allowed = `^[A-Za-z0-9][A-Za-z0-9._-]*$`, length ≤ 100. No `/`, no leading `.`, no `..`. Validate every name the operator supplies AND every `{event}` path segment before touching the filesystem.
- Delete is a hard `rmtree`; the typed-name confirmation is a FRONTEND guard. No soft-delete, no undo. Create makes an EMPTY event (no source seeding).
- Rename/delete acquire the event's `EventLock` and refuse (409) if a live render/detect holds it. Rename releases the lock file that moves with the directory.
- Built `static/` stays committed; English only; imperative commit messages.

---

## Task 1: `event_admin.py` — create/rename/delete (pure, no FastAPI)

**Files:**
- Create: `src/yt_shorts/event_admin.py`, `tests/test_event_admin.py`

**Interfaces:**
- Consumes: `yt_shorts.lock.EventLock`, `yt_shorts.lock.LockError`.
- Produces:
  - `EventAdminError(Exception)` with attribute `kind: str` (`"bad_name"|"not_found"|"exists"|"locked"`)
  - `NAME_PATTERN`, `MAX_NAME_LENGTH = 100`
  - `validate_name(name: str) -> None`
  - `create_event(channels_dir, channel: str, name: str) -> None`
  - `rename_event(channels_dir, channel: str, old: str, new: str) -> None`
  - `delete_event(channels_dir, channel: str, name: str) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_event_admin.py`:

```python
import os

import pytest

from yt_shorts import event_admin
from yt_shorts.event_admin import EventAdminError
from yt_shorts.lock import LOCK_NAME


def _channel(tmp_path, name="erf", events=()):
    channel_dir = tmp_path / "channels" / name
    (channel_dir / "events").mkdir(parents=True)
    for event in events:
        (channel_dir / "events" / event).mkdir()
    return tmp_path / "channels"


class TestValidateName:
    @pytest.mark.parametrize("good", ["race-1", "Round_2", "a", "a.b-c_d", "2026-07"])
    def test_accepts_a_safe_slug(self, good):
        event_admin.validate_name(good)  # must not raise

    @pytest.mark.parametrize("bad", ["", ".hidden", "..", "a/b", "/abs", "a b", "a" * 101, "-x"])
    def test_rejects_unsafe_names(self, bad):
        with pytest.raises(EventAdminError) as error:
            event_admin.validate_name(bad)
        assert error.value.kind == "bad_name"


class TestCreate:
    def test_creates_an_empty_event_directory(self, tmp_path):
        channels = _channel(tmp_path)
        event_admin.create_event(channels, "erf", "round-1")
        made = channels / "erf" / "events" / "round-1"
        assert made.is_dir()
        assert list(made.iterdir()) == []          # empty - no seeding

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = tmp_path / "channels"
        with pytest.raises(EventAdminError) as error:
            event_admin.create_event(channels, "nope", "round-1")
        assert error.value.kind == "not_found"

    def test_existing_event_is_a_conflict(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        with pytest.raises(EventAdminError) as error:
            event_admin.create_event(channels, "erf", "round-1")
        assert error.value.kind == "exists"

    def test_bad_name_is_rejected_before_touching_disk(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(EventAdminError) as error:
            event_admin.create_event(channels, "erf", "../escape")
        assert error.value.kind == "bad_name"
        assert not (channels.parent / "escape").exists()


class TestRename:
    def test_moves_the_directory_and_its_contents(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        (channels / "erf" / "events" / "round-1" / "marker").write_text("x")
        event_admin.rename_event(channels, "erf", "round-1", "round-2")
        assert not (channels / "erf" / "events" / "round-1").exists()
        assert (channels / "erf" / "events" / "round-2" / "marker").read_text() == "x"

    def test_unknown_event_is_not_found(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(EventAdminError) as error:
            event_admin.rename_event(channels, "erf", "ghost", "round-2")
        assert error.value.kind == "not_found"

    def test_target_existing_is_a_conflict(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1", "round-2"])
        with pytest.raises(EventAdminError) as error:
            event_admin.rename_event(channels, "erf", "round-1", "round-2")
        assert error.value.kind == "exists"

    def test_a_live_lock_blocks_the_rename(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        # A live lock = a lock file naming a running pid (this test process).
        (channels / "erf" / "events" / "round-1" / LOCK_NAME).write_text(str(os.getpid()))
        with pytest.raises(EventAdminError) as error:
            event_admin.rename_event(channels, "erf", "round-1", "round-2")
        assert error.value.kind == "locked"
        assert (channels / "erf" / "events" / "round-1").exists()    # untouched

    def test_rename_leaves_no_live_lock_behind(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        event_admin.rename_event(channels, "erf", "round-1", "round-2")
        # The lock this rename acquired must not survive into the renamed event,
        # or the next render would see a live-pid lock and refuse.
        assert not (channels / "erf" / "events" / "round-2" / LOCK_NAME).exists()


class TestDelete:
    def test_removes_the_event_directory(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        (channels / "erf" / "events" / "round-1" / "clips").mkdir()
        event_admin.delete_event(channels, "erf", "round-1")
        assert not (channels / "erf" / "events" / "round-1").exists()

    def test_unknown_event_is_not_found(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(EventAdminError) as error:
            event_admin.delete_event(channels, "erf", "ghost")
        assert error.value.kind == "not_found"

    def test_a_live_lock_blocks_the_delete(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        (channels / "erf" / "events" / "round-1" / LOCK_NAME).write_text(str(os.getpid()))
        with pytest.raises(EventAdminError) as error:
            event_admin.delete_event(channels, "erf", "round-1")
        assert error.value.kind == "locked"
        assert (channels / "erf" / "events" / "round-1").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_event_admin.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.event_admin'`.

- [ ] **Step 3: Implement `src/yt_shorts/event_admin.py`**

```python
"""Create, rename and delete an event directory for the studio (stage G2).

Pure filesystem operations over the workspace's channels dir - no FastAPI - so
the studio's routes stay a thin layer over this (mirrors workspace_listing.py).
An event is a directory channels/<channel>/events/<name>/; this module manages
that directory's LIFECYCLE and nothing inside it (the studio still writes only
edit.json within an event). Rename and delete take the event's EventLock first,
so they cannot run against an event a render or detect is using.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .lock import EventLock, LockError

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_NAME_LENGTH = 100


class EventAdminError(Exception):
    """A create/rename/delete request that cannot be honoured. `kind` lets the
    studio route map it to an HTTP status without string-sniffing:
    "bad_name" -> 400, "not_found" -> 404, "exists"/"locked" -> 409."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def validate_name(name: str) -> None:
    """Reject anything that is not one safe path segment. This is the security
    boundary: an event name becomes a directory name, so '..', a slash or a
    leading dot must never reach the filesystem."""
    if not name or len(name) > MAX_NAME_LENGTH or not NAME_PATTERN.match(name):
        raise EventAdminError(
            f"not a valid event name: {name!r} (use letters, digits, '.', '-', "
            f"'_'; no slashes, no leading dot, max {MAX_NAME_LENGTH} chars)",
            kind="bad_name")


def _events_dir(channels_dir, channel: str) -> Path:
    channel_dir = Path(channels_dir) / channel
    if not channel_dir.is_dir():
        raise EventAdminError(f"unknown channel: {channel!r}", kind="not_found")
    return channel_dir / "events"


def create_event(channels_dir, channel: str, name: str) -> None:
    validate_name(name)
    target = _events_dir(channels_dir, channel) / name
    if target.exists():
        raise EventAdminError(f"an event named {name!r} already exists", kind="exists")
    target.mkdir(parents=True)


def rename_event(channels_dir, channel: str, old: str, new: str) -> None:
    validate_name(old)
    validate_name(new)
    events = _events_dir(channels_dir, channel)
    source = events / old
    if not source.is_dir():
        raise EventAdminError(f"unknown event: {old!r}", kind="not_found")
    target = events / new
    if target.exists():
        raise EventAdminError(f"an event named {new!r} already exists", kind="exists")
    lock = EventLock(source)
    try:
        lock.acquire()
    except LockError as error:
        raise EventAdminError(str(error), kind="locked") from error
    try:
        source.rename(target)
    except OSError:
        lock.release()
        raise
    # The lock file moved with the directory and records this (long-lived studio)
    # process's pid; release it there so the renamed event is not left holding a
    # live-pid lock the next render would refuse.
    EventLock(target).release()


def delete_event(channels_dir, channel: str, name: str) -> None:
    validate_name(name)
    target = _events_dir(channels_dir, channel) / name
    if not target.is_dir():
        raise EventAdminError(f"unknown event: {name!r}", kind="not_found")
    try:
        EventLock(target).acquire()
    except LockError as error:
        raise EventAdminError(str(error), kind="locked") from error
    shutil.rmtree(target)
```

- [ ] **Step 4: Run tests + no-FastAPI check**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_event_admin.py -q` → PASS.
Run: `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.event_admin; assert 'fastapi' not in sys.modules; print('clean')"` → prints `clean`.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/event_admin.py tests/test_event_admin.py
git commit -m "Add event_admin: create, rename and delete an event directory"
```

---

## Task 2: Studio API — create/rename/delete routes

**Files:**
- Modify: `src/yt_shorts/studio/api.py` (add `event_admin` import, an `EventNameBody`, an `_admin_status` mapper, an `_event_entry` helper, and three routes near `get_events`)
- Test: `tests/test_studio_api.py` (a `TestEventAdmin` class; the file already has the `client`/`event_dir`/`studio_profile` copytree fixtures and imports `clipstore`, `json`, `os` may need adding)

**Interfaces:**
- Consumes: `event_admin.create_event/rename_event/delete_event`, `event_admin.EventAdminError` (Task 1); `list_events` (already imported).
- Produces routes:
  - `POST   /api/channels/{channel}/events`  body `{"name": str}` → 201, the created event's listing entry
  - `PATCH  /api/channels/{channel}/events/{event}`  body `{"name": str}` → 200, the renamed event's listing entry
  - `DELETE /api/channels/{channel}/events/{event}` → 200 `{"deleted": event}`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_studio_api.py` (add `import os` at the top if not present). The `studio_profile` fixture copies the `erf` fixture into a tmp `CHANNELS_DIR` and makes the `studio-test` event; these tests act on that channel/dir. `CHANNELS = "erf"`, `EVENT = "studio-test"` constants already exist in the file — reuse them.

```python
class TestEventAdmin:
    def test_create_makes_an_empty_event_and_returns_its_entry(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR   # the tmp copy the fixture set
        response = client.post(f"/api/channels/{CHANNEL}/events", json={"name": "round-9"})
        assert response.status_code == 201
        assert response.json()["name"] == "round-9"
        assert (channels / "erf" / "events" / "round-9").is_dir()

    def test_create_rejects_a_bad_name_400(self, client):
        response = client.post(f"/api/channels/{CHANNEL}/events", json={"name": "../escape"})
        assert response.status_code == 400

    def test_create_on_existing_event_409(self, client):
        response = client.post(f"/api/channels/{CHANNEL}/events", json={"name": EVENT})
        assert response.status_code == 409

    def test_create_on_unknown_channel_404(self, client):
        response = client.post("/api/channels/nope/events", json={"name": "round-9"})
        assert response.status_code == 404

    def test_rename_moves_the_event(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.patch(f"/api/channels/{CHANNEL}/events/{EVENT}",
                                json={"name": "renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "renamed"
        assert (channels / "erf" / "events" / "renamed").is_dir()
        assert not (channels / "erf" / "events" / EVENT).exists()

    def test_rename_unknown_event_404(self, client):
        response = client.patch(f"/api/channels/{CHANNEL}/events/ghost", json={"name": "x"})
        assert response.status_code == 404

    def test_delete_removes_the_event(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.delete(f"/api/channels/{CHANNEL}/events/{EVENT}")
        assert response.status_code == 200
        assert response.json()["deleted"] == EVENT
        assert not (channels / "erf" / "events" / EVENT).exists()

    def test_delete_unknown_event_404(self, client):
        response = client.delete(f"/api/channels/{CHANNEL}/events/ghost")
        assert response.status_code == 404

    def test_a_live_lock_makes_delete_and_rename_409(self, client, studio_profile):
        from yt_shorts.lock import LOCK_NAME
        channels = profile_module.CHANNELS_DIR
        (channels / "erf" / "events" / EVENT / LOCK_NAME).write_text(str(os.getpid()))
        assert client.delete(f"/api/channels/{CHANNEL}/events/{EVENT}").status_code == 409
        assert client.patch(f"/api/channels/{CHANNEL}/events/{EVENT}",
                            json={"name": "x"}).status_code == 409
```

Note: `profile_module` is `from yt_shorts import profile as profile_module`, already imported in `tests/test_studio_api.py` (the `studio_profile` fixture monkeypatches `profile_module.CHANNELS_DIR` to the tmp copy). Use it for the on-disk assertions above; the event dirs live under `profile_module.CHANNELS_DIR / "erf" / "events"`.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py::TestEventAdmin -q`
Expected: FAIL — the routes 405/404 (not defined yet).

- [ ] **Step 3: Implement in `src/yt_shorts/studio/api.py`**

Add the import with the other `from .. import` lines:

```python
from .. import event_admin
```

Add a request body near the other `BaseModel` classes (e.g. after `ConnectBody`):

```python
class EventNameBody(BaseModel):
    """Body for creating or renaming an event: the new event name (a slug;
    validated server-side by event_admin.validate_name)."""
    name: str
```

Inside `create_app`, next to `get_events` (which already has `channels_dir` in scope), add the mapper, the entry helper, and the three routes:

```python
    def _admin_status(error: event_admin.EventAdminError) -> int:
        return {"bad_name": 400, "not_found": 404,
                "exists": 409, "locked": 409}.get(error.kind, 400)

    def _event_entry(channel: str, name: str) -> dict:
        for entry in list_events(channels_dir, channel):
            if entry.name == name:
                return vars(entry)
        # Just created/renamed but not found in the listing (should not happen)
        # - return a zero-count entry rather than 500 the successful mutation.
        return {"name": name, "clip_count": 0, "kept_count": 0, "rendered_count": 0}

    @app.post(CH + "/events", status_code=201)
    def create_event(channel: str, body: EventNameBody) -> dict:
        try:
            event_admin.create_event(channels_dir, channel, body.name)
        except event_admin.EventAdminError as error:
            raise HTTPException(status_code=_admin_status(error), detail=str(error))
        return _event_entry(channel, body.name)

    @app.patch(EV)
    def rename_event(channel: str, event: str, body: EventNameBody) -> dict:
        try:
            event_admin.rename_event(channels_dir, channel, event, body.name)
        except event_admin.EventAdminError as error:
            raise HTTPException(status_code=_admin_status(error), detail=str(error))
        return _event_entry(channel, body.name)

    @app.delete(EV)
    def delete_event(channel: str, event: str) -> dict:
        try:
            event_admin.delete_event(channels_dir, channel, event)
        except event_admin.EventAdminError as error:
            raise HTTPException(status_code=_admin_status(error), detail=str(error))
        return {"deleted": event}
```

`CH` and `EV` are the existing path-prefix constants in `create_app` (`CH = "/api/channels/{channel}"`, `EV = CH + "/events/{event}"`). `PATCH EV` / `DELETE EV` add new methods on the bare event path (the clip routes are all `EV + "/..."`, so there is no collision), and `POST CH + "/events"` shares the path of the existing `GET CH + "/events"` with a different method.

- [ ] **Step 4: Run the studio tests + confirm no google at import**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q` → PASS (TestEventAdmin green, the rest still green).
Run: `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.studio.api as a; a.create_app; assert 'googleapiclient' not in sys.modules; print('clean')"` → `clean`.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "Studio API: create, rename and delete events"
```

---

## Task 3: Frontend — event admin controls (dialogs) + E2E

**Files:**
- Create: `src/yt_shorts/studio/web/src/eventAdmin.ts` + `src/eventAdmin.test.ts`
- Modify: `src/api.ts` (three calls + reuse `EventInfo`), `src/components/EventsScreen.tsx` (New/Rename/Delete controls + dialogs), `tests/test_studio_e2e.py` (a navigation-CRUD test), and rebuild `../static/`.

This is a frontend task, dispatched to a focused agent with the Task-2 API contract.

**API contract (already built in Task 2):**
- `POST /api/channels/{channel}/events` body `{name}` → 201, an `EventInfo` (`{name, clip_count, kept_count, rendered_count}`); 400 bad name, 404 unknown channel, 409 exists.
- `PATCH /api/channels/{channel}/events/{event}` body `{name}` → 200, an `EventInfo`; 400/404/409 (409 = target exists OR a job is running).
- `DELETE /api/channels/{channel}/events/{event}` → 200 `{deleted}`; 404, 409 (a job is running).
- Error responses carry `{detail: "<message>"}` (FastAPI); show `detail` as-is.

**What to build:**
1. `src/eventAdmin.ts` — pure, NOT exported from a component (fast-refresh boundary), Vitest-tested:

```ts
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
export const MAX_EVENT_NAME_LENGTH = 100

/** The same slug rule the backend enforces (event_admin.validate_name), so the
 * dialog can reject a bad name before sending. */
export function isValidEventName(name: string): boolean {
  return name.length > 0 && name.length <= MAX_EVENT_NAME_LENGTH && NAME_RE.test(name)
}

/** The delete gate: the operator must type the event's exact name. */
export function deleteConfirmed(typed: string, eventName: string): boolean {
  return typed.trim() === eventName
}
```
   Vitest (`src/eventAdmin.test.ts`): an accept/reject table for `isValidEventName` (accept `race-1`, `Round_2`, `a.b-c`; reject ``, `.hidden`, `..`, `a/b`, `a b`, 101 chars, `-x`), and `deleteConfirmed` (exact match true; trailing space trimmed true; mismatch false).

2. `src/api.ts` — add (reusing the existing `EventInfo` type, `channelBase` from `scopedApi`, and `asJson`):
```ts
export function createEvent(channel: string, name: string): Promise<EventInfo> {
  return fetch(`${channelBase(channel)}/events`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then(asJson<EventInfo>)
}
export function renameEvent(channel: string, event: string, name: string): Promise<EventInfo> {
  return fetch(`${channelBase(channel)}/events/${encodeURIComponent(event)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then(asJson<EventInfo>)
}
export function deleteEvent(channel: string, event: string): Promise<{ deleted: string }> {
  return fetch(`${channelBase(channel)}/events/${encodeURIComponent(event)}`, {
    method: 'DELETE',
  }).then(asJson<{ deleted: string }>)
}
```

3. `src/components/EventsScreen.tsx` — keep the existing list; add:
   - A **"New event"** button in the screen header/top of the list. Opens a Mantine `Modal` with a `TextInput` (validated live with `isValidEventName`, an inline error + a disabled Create button when invalid). On submit call `createEvent(channel, name)`; on success prepend/refetch the list and close; on `ApiError` show `error.message` (the server `detail`) inline.
   - Per event row, a **Menu** (Mantine `Menu` with a `⋯` `ActionIcon`) with **Rename** and **Delete**:
     - **Rename** → `Modal` pre-filled with the current name, same validation, calls `renameEvent`; on success update the row (refetch) and close.
     - **Delete** → `Modal` that shows the event name and, when `clip_count > 0`, a warning naming `clip_count`/`rendered_count` (those rendered shorts are lost); a `TextInput` where the operator must type the name; the destructive "Delete" button is disabled until `deleteConfirmed(typed, event.name)`. Calls `deleteEvent`; on success remove the row and close.
   - While any mutation is in flight, disable that dialog's submit and show a loading state; surface `ApiError.message` inline (e.g. "a job is running for this event").
   - Do NOT let the row's menu click navigate into the editor (stop propagation on the menu control), since the row itself is a navigation button.

4. Rebuild `../static/` (`npm run build`).

**E2E (`tests/test_studio_e2e.py`, real Chromium):** add a test that, from the start screen, opens the `erf` channel, creates a new event (assert it appears in the list AND the directory exists on disk under the fixture's tmp channels dir), renames it (assert the new name appears and the dir moved), then deletes it with the typed confirmation (assert it disappears and the dir is gone). Reuse the file's existing server/seeding fixtures and the on-disk assertions it already makes.

**Verify:** `npm test -- --run` (Vitest green incl. `eventAdmin.test.ts`); `npm run build` (typecheck clean, `../static/` rebuilt); `PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q` (green, real Chromium). Drive the real page and confirm the three dialogs before reporting.

- [ ] Build to the above; rebuild+commit `static/`; commit — `Add event create/rename/delete controls to the studio`.

---

## Task 4: Documentation

**Files:** `CLAUDE.md`, `README.md`.

- [ ] **Step 1: CLAUDE.md** — extend the studio boundary note. In the paragraph that begins "**The studio app is workspace-level (stage G1).**", change the read-only sentence to state that G2 lets the studio create/rename/delete event *directories* (via `event_admin`, guarded by name validation and the event's `EventLock`), while inside an event it still writes only `edit.json`. Add one line: rename/delete refuse (409) while a render/detect holds the event lock; delete is a hard `rmtree` guarded by a typed-name confirmation in the UI.

- [ ] **Step 2: README.md** — in the Studio section, note that the start screen's event list can now create a new (empty) event, rename an event, and delete one (typing its name to confirm); a new event is populated the usual ways (Streams → detect, or CLI `harvest`).

- [ ] **Step 3: Commit** — `Document studio event CRUD`.

---

## Verification for the branch

- Full `pytest` suite green, E2E included; `npm test` green; `static/` rebuilt and committed.
- Create/rename/delete work end-to-end from the start screen (driven in E2E), and each refusal (bad name 400, unknown 404, exists/locked 409) is exercised.
- A held `EventLock` blocks rename and delete (tested at both the `event_admin` and route levels).
- Name validation blocks `..`/slash/leading-dot before any filesystem touch (tested).
- `event_admin.py` imports no FastAPI; `create_app()` still pulls no google at module scope.
- Read-only-of-contents holds: the only new writes are whole-directory create/rename/delete; nothing edits an event's `clip.json`/`transcript.json`/short/`sources.json`.

## Self-review notes

- The three routes reuse G1's `CH`/`EV` path constants, so they cannot drift from the scoped tree. `POST` shares `GET`'s `/events` path (different method); `PATCH`/`DELETE` add methods on the bare `EV` path (the clip routes are `EV + "/..."`, no collision).
- The lock is the subtle piece: rename must RELEASE the lock file that moves with the directory (Task 1 Step 3 does, and `test_rename_leaves_no_live_lock_behind` guards it), or the renamed event would be permanently "locked" by the live studio pid.
- Deferred with reason (not gaps): channel CRUD (G3), the settings page (G4), source seeding/harvest from the studio — out of scope for G2.
