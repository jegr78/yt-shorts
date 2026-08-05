# Stage G3a — Channel CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator create, edit, rename, and delete channels from the studio's start screen — the channel-identity layer G3b's brand/fonts editor builds on.

**Architecture:** A shared `pathnames.validate_segment` (the one safe-path-segment rule, extracted from `event_admin`) guards every slug. A pure `channel_admin.py` (like `event_admin.py`) does create/edit/rename/delete over `channel.json` and the channel directory, raising a typed `ChannelAdminError(kind=…)`; rename/delete refuse (409) when any of the channel's events holds a live `EventLock` (new read-only `EventLock.is_held()`). Four routes (`POST /api/channels`, `PATCH`/`DELETE /api/channels/{channel}`, `POST …/rename`) map the kind to 400/404/409. The start screen gains New/Edit/Rename/Delete controls.

**Tech Stack:** Python 3 stdlib + `yt_shorts.lock`; FastAPI (studio only); React + Vite + Mantine; Vitest + Playwright.

## Global Constraints

- `PYTHONPATH=src` mandatory for pytest. Full suite green at the start of this plan.
- **Security — every slug is one safe path segment** (`^[A-Za-z0-9][A-Za-z0-9._-]*\Z`, length ≤ 100; no `/`, leading `.`, `..`, trailing newline), validated BEFORE any filesystem touch, for the create slug, the edit/rename/delete `{channel}` segment, and both `old`/`new` in rename. A traversal slug must never make an op act outside `channels/`.
- `pathnames.py`/`channel_admin.py` import nothing heavy (no FastAPI, no google).
- **A created channel is INCOMPLETE until G3b** (its scaffold `brand.json` points at non-existent fonts; its events cannot be opened until G3b adds fonts). G3a adds no "incomplete" marker.
- Delete is a hard `rmtree` (removes all the channel's events), guarded by a typed-slug confirmation in the UI. No soft-delete, no undo. Delete does NOT touch `<workspace>/auth/token-<id>.json` (outside the channel dir; the studio never writes `auth/` except via connect).
- Required `channel.json` fields (non-empty): `id`, `channel_url`, `handle`, `display_name`, `language`, `footer` (matches `profile.REQUIRED_CHANNEL_FIELDS`).
- Built `static/` stays committed; English only; imperative commits.

---

## Task 1: `pathnames.py` — shared segment validation; refactor `event_admin`

**Files:**
- Create: `src/yt_shorts/pathnames.py`, `tests/test_pathnames.py`
- Modify: `src/yt_shorts/event_admin.py` (use `pathnames.validate_segment`)

**Interfaces:**
- Produces: `pathnames.NAME_PATTERN`, `pathnames.MAX_NAME_LENGTH = 100`, `pathnames.validate_segment(value: str, *, what: str) -> None` (raises `ValueError`).
- `event_admin.validate_name` keeps its signature and `EventAdminError(kind="bad_name")` behaviour (now delegating to `validate_segment`).

- [ ] **Step 1: Write the failing test** — `tests/test_pathnames.py`:

```python
import pytest

from yt_shorts import pathnames


class TestValidateSegment:
    @pytest.mark.parametrize("good", ["race-1", "Round_2", "a", "a.b-c_d", "erf", "2026-07"])
    def test_accepts_a_safe_segment(self, good):
        pathnames.validate_segment(good, what="event name")   # must not raise

    @pytest.mark.parametrize("bad", ["", ".hidden", "..", "a/b", "/abs", "a b",
                                     "a" * 101, "-x", "x\n", "a\nb"])
    def test_rejects_unsafe_segments(self, bad):
        with pytest.raises(ValueError) as error:
            pathnames.validate_segment(bad, what="channel name")
        assert "channel name" in str(error.value)   # the label appears in the message
```

- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src .venv/bin/pytest tests/test_pathnames.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `src/yt_shorts/pathnames.py`:

```python
"""The one 'safe single path segment' rule, shared by event_admin and
channel_admin. A name here becomes a directory name, so the same validation
guards both the event name and the channel slug (and every {event}/{channel}
URL segment): '..', a slash or a leading dot must never reach the filesystem.
No FastAPI, no heavy imports."""

from __future__ import annotations

import re

# \Z (not $) so a trailing newline is rejected: Python's $ matches just before
# a final '\n', which would let "round-1\n" through.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MAX_NAME_LENGTH = 100


def validate_segment(value: str, *, what: str) -> None:
    """Raise ValueError (naming `what`) if `value` is not one safe path segment:
    empty, > MAX_NAME_LENGTH, a leading dot, '..', a slash, or any char outside
    [A-Za-z0-9._-]."""
    if not value or len(value) > MAX_NAME_LENGTH or not NAME_PATTERN.match(value):
        raise ValueError(
            f"not a valid {what}: {value!r} (use letters, digits, '.', '-', "
            f"'_'; no slashes, no leading dot, max {MAX_NAME_LENGTH} chars)")
```

- [ ] **Step 4: Refactor `event_admin.py`** to delegate. Replace its `NAME_PATTERN`/`MAX_NAME_LENGTH` module constants and the body of `validate_name` with a delegation to `pathnames`:

```python
from . import pathnames
# ... (drop the local NAME_PATTERN / MAX_NAME_LENGTH definitions)

def validate_name(name: str) -> None:
    """Reject anything that is not one safe path segment (see pathnames)."""
    try:
        pathnames.validate_segment(name, what="event name")
    except ValueError as error:
        raise EventAdminError(str(error), kind="bad_name") from error
```

Keep everything else in `event_admin.py` unchanged.

- [ ] **Step 5: Run tests** — `PYTHONPATH=src .venv/bin/pytest tests/test_pathnames.py tests/test_event_admin.py -q` → all PASS (event_admin's own 30 tests still green, including the traversal and `\n` cases). Also `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.pathnames, yt_shorts.event_admin; assert 'fastapi' not in sys.modules; print('clean')"`.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/pathnames.py tests/test_pathnames.py src/yt_shorts/event_admin.py
git commit -m "Extract the safe-path-segment rule into pathnames; reuse in event_admin"
```

---

## Task 2: `EventLock.is_held()` — read-only lock check

**Files:**
- Modify: `src/yt_shorts/lock.py` (add `is_held`)
- Test: `tests/test_lock.py` (add cases; if the file does not exist, create it)

**Interfaces:**
- Produces: `EventLock.is_held() -> bool`.

- [ ] **Step 1: Write the failing test** — add to `tests/test_lock.py` (create it if absent, with `import os`, `from yt_shorts.lock import EventLock, LOCK_NAME`):

```python
class TestIsHeld:
    def test_a_live_pid_lock_is_held(self, tmp_path):
        (tmp_path / LOCK_NAME).write_text(str(os.getpid()))
        assert EventLock(tmp_path).is_held() is True

    def test_no_lock_file_is_not_held(self, tmp_path):
        assert EventLock(tmp_path).is_held() is False

    def test_a_stale_dead_pid_lock_is_not_held(self, tmp_path):
        # PID 2^31-1 is not a running process on this machine.
        (tmp_path / LOCK_NAME).write_text("2147483647")
        assert EventLock(tmp_path).is_held() is False

    def test_an_empty_or_garbage_lock_is_not_held(self, tmp_path):
        (tmp_path / LOCK_NAME).write_text("not-a-pid")
        assert EventLock(tmp_path).is_held() is False
```

- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src .venv/bin/pytest tests/test_lock.py::TestIsHeld -q` → `AttributeError: 'EventLock' object has no attribute 'is_held'`.

- [ ] **Step 3: Implement** — add to `EventLock` in `src/yt_shorts/lock.py` (it already has `_read_holder_pid` and the module-level `_process_is_alive`):

```python
    def is_held(self) -> bool:
        """True iff the lock file exists and names a live process - a read-only
        check that never creates or takes over a lock (unlike acquire). A stale
        (dead-pid), empty, or absent lock is not held. Used by channel_admin to
        refuse renaming/deleting a channel while any of its events is rendering."""
        pid = self._read_holder_pid()
        return pid is not None and _process_is_alive(pid)
```

- [ ] **Step 4: Run tests** — `PYTHONPATH=src .venv/bin/pytest tests/test_lock.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/lock.py tests/test_lock.py
git commit -m "Add EventLock.is_held: a read-only 'is a render live?' check"
```

---

## Task 3: `channel_admin.py` — create/edit/rename/delete (pure, no FastAPI)

**Files:**
- Create: `src/yt_shorts/channel_admin.py`, `tests/test_channel_admin.py`

**Interfaces:**
- Consumes: `pathnames.validate_segment` (Task 1), `EventLock.is_held` (Task 2).
- Produces:
  - `ChannelAdminError(Exception)` with `kind: str` (`"bad_name"|"bad_field"|"not_found"|"exists"|"locked"`)
  - `REQUIRED_FIELDS`, `DEFAULT_BRAND`
  - `create_channel(channels_dir, slug: str, fields: dict) -> None`
  - `update_channel(channels_dir, slug: str, fields: dict) -> None`
  - `rename_channel(channels_dir, old: str, new: str) -> None`
  - `delete_channel(channels_dir, slug: str) -> None`

- [ ] **Step 1: Write the failing test** — `tests/test_channel_admin.py`:

```python
import json
import os

import pytest

from yt_shorts import channel_admin
from yt_shorts.channel_admin import ChannelAdminError
from yt_shorts.lock import LOCK_NAME

FIELDS = {"id": "UCabc", "channel_url": "https://www.youtube.com/channel/UCabc",
          "handle": "@demo", "display_name": "Demo League", "language": "en",
          "footer": "DEMO | @demo"}


def _channels(tmp_path):
    d = tmp_path / "channels"
    d.mkdir()
    return d


class TestCreate:
    def test_creates_channel_json_and_scaffold(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        base = channels / "demo"
        assert json.loads((base / "channel.json").read_text())["display_name"] == "Demo League"
        assert (base / "brand.json").is_file()          # default brand scaffold
        assert (base / "fonts").is_dir()
        assert (base / "events").is_dir()

    def test_rejects_a_traversal_slug_and_nothing_escapes(self, tmp_path):
        channels = _channels(tmp_path)
        outside = tmp_path / "pwned"
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.create_channel(channels, "..", {**FIELDS})
        assert error.value.kind == "bad_name"
        assert not outside.exists()

    def test_existing_channel_is_a_conflict(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.create_channel(channels, "demo", FIELDS)
        assert error.value.kind == "exists"

    @pytest.mark.parametrize("missing", ["id", "handle", "display_name", "footer",
                                         "language", "channel_url"])
    def test_missing_required_field_is_bad_field(self, tmp_path, missing):
        channels = _channels(tmp_path)
        fields = {**FIELDS, missing: ""}
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.create_channel(channels, "demo", fields)
        assert error.value.kind == "bad_field"
        assert not (channels / "demo").exists()


class TestUpdate:
    def test_merges_fields_into_channel_json(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        channel_admin.update_channel(channels, "demo", {"display_name": "Renamed League"})
        data = json.loads((channels / "demo" / "channel.json").read_text())
        assert data["display_name"] == "Renamed League"
        assert data["handle"] == "@demo"               # untouched field kept

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = _channels(tmp_path)
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.update_channel(channels, "ghost", {"footer": "x"})
        assert error.value.kind == "not_found"

    def test_blanking_a_required_field_is_bad_field(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.update_channel(channels, "demo", {"footer": ""})
        assert error.value.kind == "bad_field"


class TestRename:
    def test_moves_the_directory_and_its_events(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        (channels / "demo" / "events" / "round-1").mkdir()
        channel_admin.rename_channel(channels, "demo", "demo2")
        assert not (channels / "demo").exists()
        assert (channels / "demo2" / "events" / "round-1").is_dir()

    def test_target_existing_is_a_conflict(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        channel_admin.create_channel(channels, "demo2", {**FIELDS, "id": "UCxyz"})
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.rename_channel(channels, "demo", "demo2")
        assert error.value.kind == "exists"

    def test_a_live_event_lock_blocks_rename(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        event = channels / "demo" / "events" / "round-1"
        event.mkdir()
        (event / LOCK_NAME).write_text(str(os.getpid()))
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.rename_channel(channels, "demo", "demo2")
        assert error.value.kind == "locked"
        assert (channels / "demo").exists()


class TestDelete:
    def test_removes_the_channel_directory(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        channel_admin.delete_channel(channels, "demo")
        assert not (channels / "demo").exists()

    def test_a_live_event_lock_blocks_delete(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        event = channels / "demo" / "events" / "round-1"
        event.mkdir()
        (event / LOCK_NAME).write_text(str(os.getpid()))
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.delete_channel(channels, "demo")
        assert error.value.kind == "locked"
        assert (channels / "demo").exists()

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = _channels(tmp_path)
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.delete_channel(channels, "ghost")
        assert error.value.kind == "not_found"
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `src/yt_shorts/channel_admin.py`:

```python
"""Create, edit, rename and delete a channel directory for the studio (stage
G3a). Pure filesystem ops over the workspace's channels dir - no FastAPI, like
event_admin.py. Manages channel.json identity and the channel directory's
lifecycle; branding (brand.json colors/output) and fonts are stage G3b. Rename
and delete refuse while any of the channel's events holds a live EventLock.

A created channel is scaffolded with a DEFAULT_BRAND brand.json (pointing at
fonts that do not exist yet) plus empty fonts/ and events/ dirs - it is not
renderable until G3b provides fonts.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import pathnames
from .lock import EventLock

REQUIRED_FIELDS = ["id", "channel_url", "handle", "display_name", "language", "footer"]

# Equal to templates/example-channel/brand.json - embedded so this module stays
# pure and independent of the repo layout. Points at fonts that do not exist
# yet; G3b's brand/fonts editor is what makes the channel renderable.
DEFAULT_BRAND = {
    "colors": {"text": "#FFFFFF", "base": "#101010", "accent": "#144E53", "edge": "#B8F5CA"},
    "fonts": {"hook": "fonts/YourFont-Bold.ttf", "small": "fonts/YourFont-Bold.ttf"},
    "output": {"width": 1080, "height": 1920, "video_width": 1080,
               "video_height": 608, "video_y": 600},
    "subtitles": {"enabled": False},
}


class ChannelAdminError(Exception):
    """A channel create/edit/rename/delete that cannot be honoured. `kind` maps
    to HTTP status: "bad_name"/"bad_field" -> 400, "not_found" -> 404,
    "exists"/"locked" -> 409."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _validate_slug(slug: str) -> None:
    try:
        pathnames.validate_segment(slug, what="channel name")
    except ValueError as error:
        raise ChannelAdminError(str(error), kind="bad_name") from error


def _validate_fields(fields: dict) -> None:
    for field in REQUIRED_FIELDS:
        if not str(fields.get(field, "")).strip():
            raise ChannelAdminError(
                f"channel field {field!r} must not be empty", kind="bad_field")


def _channel_dir(channels_dir, slug: str) -> Path:
    _validate_slug(slug)
    return Path(channels_dir) / slug


def _locked_event(channel_dir: Path) -> str | None:
    events = channel_dir / "events"
    if not events.is_dir():
        return None
    for event in sorted(p for p in events.iterdir() if p.is_dir()):
        if EventLock(event).is_held():
            return event.name
    return None


def create_channel(channels_dir, slug: str, fields: dict) -> None:
    base = _channel_dir(channels_dir, slug)
    _validate_fields(fields)
    if base.exists():
        raise ChannelAdminError(f"a channel named {slug!r} already exists", kind="exists")
    base.mkdir(parents=True)
    payload = {field: fields[field] for field in REQUIRED_FIELDS}
    (base / "channel.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (base / "brand.json").write_text(
        json.dumps(DEFAULT_BRAND, indent=2) + "\n", encoding="utf-8")
    (base / "fonts").mkdir()
    (base / "events").mkdir()


def update_channel(channels_dir, slug: str, fields: dict) -> None:
    base = _channel_dir(channels_dir, slug)
    path = base / "channel.json"
    if not path.exists():
        raise ChannelAdminError(f"unknown channel: {slug!r}", kind="not_found")
    data = json.loads(path.read_text(encoding="utf-8"))
    for field in REQUIRED_FIELDS:
        if field in fields:
            data[field] = fields[field]
    _validate_fields(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def rename_channel(channels_dir, old: str, new: str) -> None:
    source = _channel_dir(channels_dir, old)
    target = _channel_dir(channels_dir, new)
    if not source.is_dir():
        raise ChannelAdminError(f"unknown channel: {old!r}", kind="not_found")
    if target.exists():
        raise ChannelAdminError(f"a channel named {new!r} already exists", kind="exists")
    locked = _locked_event(source)
    if locked is not None:
        raise ChannelAdminError(
            f"event {locked!r} of this channel is being rendered - "
            f"wait for it to finish", kind="locked")
    source.rename(target)


def delete_channel(channels_dir, slug: str) -> None:
    base = _channel_dir(channels_dir, slug)
    if not base.is_dir():
        raise ChannelAdminError(f"unknown channel: {slug!r}", kind="not_found")
    locked = _locked_event(base)
    if locked is not None:
        raise ChannelAdminError(
            f"event {locked!r} of this channel is being rendered - "
            f"wait for it to finish", kind="locked")
    shutil.rmtree(base)
```

- [ ] **Step 4: Run tests + no-FastAPI check** — `PYTHONPATH=src .venv/bin/pytest tests/test_channel_admin.py -q` → PASS; `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.channel_admin; assert 'fastapi' not in sys.modules; print('clean')"`.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/channel_admin.py tests/test_channel_admin.py
git commit -m "Add channel_admin: create, edit, rename and delete a channel"
```

---

## Task 4: Studio API — channel create/edit/rename/delete routes

**Files:**
- Modify: `src/yt_shorts/studio/api.py` (import `channel_admin`, a `ChannelCreateBody`/`ChannelFieldsBody`/reuse `EventNameBody`, a `_channel_status` mapper, a `_channel_entry` helper, four routes near `get_channels`)
- Test: `tests/test_studio_api.py` (a `TestChannelAdmin` class)

**Interfaces:**
- Consumes: `channel_admin.*`, `channel_admin.ChannelAdminError` (Task 3); `list_channels` (already imported).
- Produces routes:
  - `POST   /api/channels`  body `{slug, id, channel_url, handle, display_name, language, footer}` → 201, `ChannelInfo`
  - `PATCH  /api/channels/{channel}`  body `{<channel.json fields>}` → 200, `ChannelInfo`
  - `POST   /api/channels/{channel}/rename`  body `{name}` → 200, `ChannelInfo` (new slug)
  - `DELETE /api/channels/{channel}` → 200 `{"deleted": slug}`

- [ ] **Step 1: Write the failing tests** — add to `tests/test_studio_api.py` (`profile_module`, `CHANNEL`, `os`, `json` are imported; the `client`/`studio_profile` fixtures give the tmp `CHANNELS_DIR` with the `erf` channel):

```python
class TestChannelAdmin:
    FIELDS = {"id": "UCnew", "channel_url": "https://www.youtube.com/channel/UCnew",
              "handle": "@new", "display_name": "New League", "language": "en",
              "footer": "NEW | @new"}

    def test_create_makes_a_channel_and_returns_its_entry(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.post("/api/channels", json={"slug": "newchan", **self.FIELDS})
        assert response.status_code == 201
        assert response.json()["name"] == "newchan"
        assert response.json()["display_name"] == "New League"
        assert (channels / "newchan" / "channel.json").is_file()
        assert (channels / "newchan" / "brand.json").is_file()

    def test_create_bad_slug_400(self, client):
        response = client.post("/api/channels", json={"slug": "../x", **self.FIELDS})
        assert response.status_code == 400

    def test_create_missing_field_400(self, client):
        response = client.post("/api/channels", json={"slug": "newchan", **{**self.FIELDS, "footer": ""}})
        assert response.status_code == 400

    def test_create_existing_channel_409(self, client):
        response = client.post("/api/channels", json={"slug": CHANNEL, **self.FIELDS})
        assert response.status_code == 409

    def test_a_traversal_channel_segment_is_400_not_escaped(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        outside = channels.parent / "victim"
        outside.mkdir()
        (outside / "keep").write_text("x")
        assert client.delete("/api/channels/%2e%2e").status_code == 400
        assert (outside / "keep").read_text() == "x"

    def test_edit_updates_channel_json(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.patch(f"/api/channels/{CHANNEL}", json={"display_name": "ERF Renamed"})
        assert response.status_code == 200
        assert json.loads((channels / "erf" / "channel.json").read_text())["display_name"] == "ERF Renamed"

    def test_rename_moves_the_channel(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.post(f"/api/channels/{CHANNEL}/rename", json={"name": "erf2"})
        assert response.status_code == 200
        assert response.json()["name"] == "erf2"
        assert (channels / "erf2").is_dir()
        assert not (channels / "erf").exists()

    def test_delete_removes_the_channel(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.delete(f"/api/channels/{CHANNEL}")
        assert response.status_code == 200
        assert response.json()["deleted"] == CHANNEL
        assert not (channels / "erf").exists()

    def test_a_live_event_lock_makes_rename_and_delete_409(self, client, studio_profile):
        from yt_shorts.lock import LOCK_NAME
        channels = profile_module.CHANNELS_DIR
        (channels / "erf" / "events" / EVENT / LOCK_NAME).write_text(str(os.getpid()))
        assert client.post(f"/api/channels/{CHANNEL}/rename", json={"name": "erf2"}).status_code == 409
        assert client.delete(f"/api/channels/{CHANNEL}").status_code == 409
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement in `studio/api.py`.** Add the import with the others:

```python
from .. import channel_admin
```

Add bodies near the other `BaseModel`s:

```python
class ChannelCreateBody(BaseModel):
    slug: str
    id: str
    channel_url: str
    handle: str
    display_name: str
    language: str
    footer: str


class ChannelFieldsBody(BaseModel):
    """A partial channel.json edit - only the fields present are applied."""
    id: str | None = None
    channel_url: str | None = None
    handle: str | None = None
    display_name: str | None = None
    language: str | None = None
    footer: str | None = None
```

Inside `create_app`, next to `get_channels` (which already has `channels_dir`), add the mapper, entry helper, and four routes:

```python
    def _channel_status(error: channel_admin.ChannelAdminError) -> int:
        return {"bad_name": 400, "bad_field": 400, "not_found": 404,
                "exists": 409, "locked": 409}.get(error.kind, 400)

    def _channel_entry(slug: str) -> dict:
        for entry in list_channels(channels_dir):
            if entry.name == slug:
                return vars(entry)
        return {"name": slug, "display_name": "", "handle": "",
                "event_count": 0, "error": None}

    @app.post("/api/channels", status_code=201)
    def create_channel(body: ChannelCreateBody) -> dict:
        fields = body.model_dump()
        slug = fields.pop("slug")
        try:
            channel_admin.create_channel(channels_dir, slug, fields)
        except channel_admin.ChannelAdminError as error:
            raise HTTPException(status_code=_channel_status(error), detail=str(error))
        return _channel_entry(slug)

    @app.patch(CH)
    def edit_channel(channel: str, body: ChannelFieldsBody) -> dict:
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            channel_admin.update_channel(channels_dir, channel, fields)
        except channel_admin.ChannelAdminError as error:
            raise HTTPException(status_code=_channel_status(error), detail=str(error))
        return _channel_entry(channel)

    @app.post(CH + "/rename")
    def rename_channel(channel: str, body: EventNameBody) -> dict:
        try:
            channel_admin.rename_channel(channels_dir, channel, body.name)
        except channel_admin.ChannelAdminError as error:
            raise HTTPException(status_code=_channel_status(error), detail=str(error))
        return _channel_entry(body.name)

    @app.delete(CH)
    def delete_channel(channel: str) -> dict:
        try:
            channel_admin.delete_channel(channels_dir, channel)
        except channel_admin.ChannelAdminError as error:
            raise HTTPException(status_code=_channel_status(error), detail=str(error))
        return {"deleted": channel}
```

`CH` is the existing `"/api/channels/{channel}"` constant. `PATCH CH` / `DELETE CH` add methods on the bare channel path (auth is `CH+"/auth"`, events `CH+"/events"`, and G2's event `PATCH`/`DELETE` are on `EV = CH+"/events/{event}"` — all deeper, no collision). `POST CH+"/rename"` is a distinct sub-path. `EventNameBody` (from G2, `{name: str}`) is reused for rename.

- [ ] **Step 4: Run the studio tests + no-google check** — `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q` → PASS; `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.studio.api as a; a.create_app; assert 'googleapiclient' not in sys.modules; print('clean')"`.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "Studio API: create, edit, rename and delete channels"
```

---

## Task 5: Frontend — channel admin controls (dialogs) + E2E

**Files:**
- Create: `src/yt_shorts/studio/web/src/slug.ts` + `src/slug.test.ts`, the channel dialog component(s).
- Modify: `src/api.ts` (four calls + a create payload type), `src/eventAdmin.ts` (reuse the shared slug rule), `src/components/ChannelsScreen.tsx` (controls), `tests/test_studio_e2e.py`, rebuild `../static/`.

Dispatched to a focused frontend agent with the Task-4 API contract.

**API contract (Task 4, already built):**
- `POST /api/channels` body `{slug, id, channel_url, handle, display_name, language, footer}` → 201, a `ChannelInfo` (`{name, display_name, handle, event_count, error}`); 400 bad slug/field, 409 exists.
- `PATCH /api/channels/{channel}` body `{<any of id, channel_url, handle, display_name, language, footer>}` → 200, `ChannelInfo`; 400/404.
- `POST /api/channels/{channel}/rename` body `{name}` → 200, `ChannelInfo` (new slug); 400/404/409 (409 = target exists OR an event is being rendered).
- `DELETE /api/channels/{channel}` → 200 `{deleted}`; 404, 409 (an event is being rendered).
- Errors are FastAPI `{detail}` — surface via the existing `ApiError.message`.

**What to build (follow the G2 EventsScreen patterns exactly — this mirrors them):**
1. `src/slug.ts` — pure, not exported from a component: `isValidSlug(name: string): boolean` (rule `^[A-Za-z0-9][A-Za-z0-9._-]*$`, length 1..100). Move the shared rule here and have `eventAdmin.ts`'s `isValidEventName` delegate to `isValidSlug` (keep `isValidEventName` exported for its existing test/callers, or repoint them — but do NOT change behaviour). Add `src/slug.test.ts` (accept/reject table). Keep `deleteConfirmed` where it is (reuse it).
2. `src/api.ts` — add, reusing `ChannelInfo`, `asJson`, and absolute `/api/channels` paths (channel create/list are NOT event-scoped):
```ts
export interface ChannelCreatePayload {
  slug: string; id: string; channel_url: string; handle: string
  display_name: string; language: string; footer: string
}
export function createChannel(payload: ChannelCreatePayload): Promise<ChannelInfo> {
  return fetch('/api/channels', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }).then(asJson<ChannelInfo>)
}
export function updateChannel(channel: string, fields: Partial<Omit<ChannelCreatePayload, 'slug'>>): Promise<ChannelInfo> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}`, { method: 'PATCH',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fields),
  }).then(asJson<ChannelInfo>)
}
export function renameChannel(channel: string, name: string): Promise<ChannelInfo> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}/rename`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  }).then(asJson<ChannelInfo>)
}
export function deleteChannel(channel: string): Promise<{ deleted: string }> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}`, { method: 'DELETE' })
    .then(asJson<{ deleted: string }>)
}
```
3. `src/components/ChannelsScreen.tsx` — keep the existing list; add:
   - A **"New channel"** button → `Modal` with a **slug** field (validated live with `isValidSlug`) and the six fields; `channel_url` auto-fills from `id` as `https://www.youtube.com/channel/<id>` (editable — only overwrite it while the operator has not hand-edited it). Create disabled until the slug is valid and all six fields are non-empty. On success refetch + close; show `ApiError.message` inline on failure.
   - Per channel row a `⋯` `Menu` (sibling of the navigating button, NOT nested; `stopPropagation`; `withinPortal`; `aria-label`) with **Edit**, **Rename**, **Delete**:
     - **Edit** → `Modal` pre-filled with the channel's fields (fetch is not needed if the row lacks them — the list only carries `display_name`/`handle`; fetch the full `channel.json`? No dedicated GET exists, so Edit collects the fields the list has plus lets the operator fill the rest; simplest: PATCH only sends changed fields, and the dialog is seeded from `display_name`/`handle` which the list provides — leave `id`/`channel_url`/`language`/`footer` editable, empty-unless-typed, and send only non-empty changed fields). → `updateChannel` → refetch + close.
     - **Rename** → `Modal` with a new **slug** field (validated) → `renameChannel` → on success `navigate` is not required (staying on the channel list), just refetch the list so the new slug row appears.
     - **Delete** → `Modal` requiring the operator to **type the slug** (`deleteConfirmed`), warning that all `event_count` events are removed → `deleteChannel` → refetch + close.
   - Disable a dialog's submit + show loading while its request is in flight; surface `ApiError.message` inline.
   - A channel with a non-null `error` (unreadable channel.json) keeps its existing "not openable" treatment; its menu should still allow Delete (so a broken channel can be removed).
4. Match the existing dark Mantine styling. Rebuild `../static/` (`npm run build`).

**Note on Edit prefill:** the channel list (`GET /api/channels`) only carries `name`, `display_name`, `handle`, `event_count`, `error` — not the full `channel.json`. So the Edit dialog can prefill only `display_name`/`handle`; `id`/`channel_url`/`language`/`footer` are not in the list. Two acceptable options — pick one and note it: (a) Edit sends only the fields the operator changes (prefill `display_name`/`handle`, leave the rest blank = "unchanged"), relying on the backend's partial merge; (b) add a `GET /api/channels/{channel}` returning the full `channel.json` and prefill everything. **Prefer (a)** for G3a (no new route; the backend `update_channel` already merges only the fields sent). Make the Edit dialog's empty fields mean "leave unchanged" and only send non-empty changed values.

**E2E (`tests/test_studio_e2e.py`, real Chromium):** add a test that, from the start screen, creates a channel (assert it appears AND `channels/<slug>/channel.json` exists on disk under the fixture's tmp channels dir), edits its display name (assert the row/JSON updates), renames the slug (assert the new-slug row appears and the directory moved), deletes it via the typed-slug confirmation (assert it disappears and the dir is gone). Reuse the E2E's existing server/seeding/on-disk assertions.

**Verify:** `npm test -- --run` (Vitest green incl. `slug.test.ts`); `npm run build` (typecheck clean, `../static/` rebuilt); `PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q` (green, real Chromium). Drive the real page and confirm the four dialogs before reporting.

- [ ] Build to the above; rebuild+commit `static/`; commit — `Add channel create/edit/rename/delete controls to the studio`.

---

## Task 6: Documentation

**Files:** `CLAUDE.md`, `README.md`.

- [ ] **Step 1: CLAUDE.md** — extend the write-boundary note added in G2. State that the studio now also creates/edits/renames/deletes CHANNELS via `channel_admin` (channel.json + a scaffold brand.json; the same segment validation via `pathnames.validate_segment`; rename/delete refuse while any of the channel's events holds a live `EventLock`, checked by the new read-only `EventLock.is_held()`). Note a created channel is incomplete until G3b provides fonts. The `edit.json`-only rule for event CONTENTS is unchanged; delete never touches `auth/`.

- [ ] **Step 2: README.md** — in the Studio section, note the start screen can now add a channel (a slug + the channel.json identity fields), edit those fields, rename its slug (the URL moves), and delete a channel (typing the slug to confirm; removes all its events). Note a new channel needs branding and fonts before its events can be opened (a later stage), and that renaming/deleting is refused while one of its events is rendering.

- [ ] **Step 3: Commit** — `Document studio channel CRUD`.

---

## Verification for the branch

- Full `pytest` suite green, E2E included; `npm test` green; `static/` rebuilt.
- Create/edit/rename/delete work end-to-end from the start screen (E2E), and each refusal (bad slug/field 400, unknown 404, exists/locked 409) is exercised.
- A held event lock blocks channel rename AND delete (tested at the `channel_admin` and route levels).
- A traversal slug / `{channel}` segment is rejected (400) before any filesystem touch, at both the `channel_admin` and HTTP levels — nothing escapes `channels/`.
- `pathnames.py`/`channel_admin.py` import no FastAPI; `create_app()` still pulls no google at module scope; `event_admin`'s own tests stay green after the refactor.

## Self-review notes

- `pathnames.validate_segment` is now the single segment rule; the reviewer should confirm `event_admin` still raises `EventAdminError(kind="bad_name")` (its tests pin this) and that the `\Z`/traversal behaviour is unchanged.
- The channel routes reuse `CH` so they cannot drift from the scoped tree; `PATCH`/`DELETE CH` and `POST CH+"/rename"` do not collide with the auth/events/clip routes (all deeper) or the last-registered SPA fallback.
- The lock check spans all of a channel's events (`_locked_event`) via the read-only `is_held` — a small TOCTOU window remains (documented, acceptable for a local single-user tool).
- Deferred with reason: brand editing, font upload, a channel "incomplete/complete" marker (all G3b); slug rename of *events* (already in G2); channel `assets` field (racecast-documentary, not collected).
