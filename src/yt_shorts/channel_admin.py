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
import os
import shutil
from pathlib import Path

from . import atomicwrite, pathnames
from .lock import EventLock, LockError

REQUIRED_FIELDS = ["id", "channel_url", "handle", "display_name", "language", "footer"]

# Equal to templates/example-channel/brand.json - embedded so this module stays
# pure and independent of the repo layout. Points at fonts that do not exist
# yet; G3b's brand/fonts editor is what makes the channel renderable.
#
# The colors are deliberately GREYSCALE, and must stay that way. They used to
# be ERF's petrol/mint pair, which meant every channel created from this
# scaffold shipped looking like ERF - four of the five channels in the
# operator's workspace ended up carrying another channel's brand colour, which
# is the bug palette.py exists to fix. A grey scaffold reads as "not branded
# yet" rather than as a deliberate choice, and the Brand editor's "Derive from
# logo" turns it into the channel's own palette in one click. Never put a hue
# here: whatever it is, it is wrong for every channel but the one it came from.
DEFAULT_BRAND = {
    "colors": {"text": "#FFFFFF", "base": "#101010", "accent": "#2A2A2A", "edge": "#9A9A9A"},
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
    return _within(channels_dir, slug)


def _within(channels_dir, slug: str) -> Path:
    """Belt to _validate_slug's braces: joins, normalises, and refuses a result
    that is not INSIDE channels_dir.

    After _validate_slug this cannot fire - NAME_PATTERN already rejects
    separators, `..` and leading dots, and test_channel_admin.py's own
    traversal test pins that. It is here as a second layer that holds if the
    first is ever loosened, and because the pair (normpath, then a prefix
    check) is the shape a scanner can actually see: CodeQL read the validated
    slug as uncontrolled data all the way into the file write and raised six
    py/path-injection alerts, because it models neither
    pathnames.validate_segment nor pathlib's own refusals."""
    root = os.path.normpath(str(channels_dir))
    candidate = os.path.normpath(os.path.join(root, slug))
    if not candidate.startswith(root + os.sep):
        raise ChannelAdminError(
            f"channel name {slug!r} escapes the channels directory", kind="bad_name")
    return Path(candidate)


def _event_dirs(channel_dir: Path) -> list[Path]:
    events = channel_dir / "events"
    if not events.is_dir():
        return []
    return sorted(p for p in events.iterdir() if p.is_dir())


def _acquire_all_event_locks(channel_dir: Path) -> list[EventLock]:
    """Acquire the EventLock of every event under this channel, so no render can
    start (or be running) while we restructure the channel dir. A read-only
    is_held() check would leave a TOCTOU window in which a render could acquire
    the lock and begin writing between the check and the rmtree/rename, exactly
    the two-writers race EventLock exists to prevent. On any live lock, release
    what we took and refuse (409 'locked')."""
    acquired: list[EventLock] = []
    for event in _event_dirs(channel_dir):
        lock = EventLock(event)
        try:
            lock.acquire()
        except LockError:
            for held in acquired:
                held.release()
            raise ChannelAdminError(
                f"event {event.name!r} of this channel is being rendered - "
                f"wait for it to finish", kind="locked") from None
        acquired.append(lock)
    return acquired


def create_channel(channels_dir, slug: str, fields: dict) -> None:
    base = _channel_dir(channels_dir, slug)
    _validate_fields(fields)
    if base.exists():
        raise ChannelAdminError(f"a channel named {slug!r} already exists", kind="exists")
    base.mkdir(parents=True)
    payload = {field: fields[field] for field in REQUIRED_FIELDS}
    atomicwrite.write_text(
        base / "channel.json", json.dumps(payload, indent=2) + "\n")
    atomicwrite.write_text(
        base / "brand.json", json.dumps(DEFAULT_BRAND, indent=2) + "\n")
    (base / "fonts").mkdir()
    (base / "events").mkdir()


def update_channel(channels_dir, slug: str, fields: dict) -> None:
    base = _channel_dir(channels_dir, slug)
    path = base / "channel.json"
    if not path.exists():
        raise ChannelAdminError(f"unknown channel: {slug!r}", kind="not_found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # A broken stored channel.json is a clean 4xx the operator must fix by
        # hand, not a 500: surface it as bad_field rather than a raw crash.
        raise ChannelAdminError(
            f"channel {slug!r} has an unreadable channel.json ({error}); "
            f"fix it by hand", kind="bad_field") from error
    for field in REQUIRED_FIELDS:
        if field in fields:
            data[field] = fields[field]
    _validate_fields(data)
    atomicwrite.write_text(path, json.dumps(data, indent=2) + "\n")


def rename_channel(channels_dir, old: str, new: str) -> None:
    source = _channel_dir(channels_dir, old)
    target = _channel_dir(channels_dir, new)
    if not source.is_dir():
        raise ChannelAdminError(f"unknown channel: {old!r}", kind="not_found")
    if target.exists():
        raise ChannelAdminError(f"a channel named {new!r} already exists", kind="exists")
    locks = _acquire_all_event_locks(source)
    try:
        source.rename(target)
        # The lock files moved with the dir; remove them from the new location so
        # the renamed channel's events aren't left holding our now-stale locks.
        for event in _event_dirs(target):
            EventLock(event).release()
    finally:
        # Old-path unlink is a no-op after the successful rename (tolerated); on a
        # rename failure it releases the locks we took.
        for lock in locks:
            lock.release()


def delete_channel(channels_dir, slug: str) -> None:
    base = _channel_dir(channels_dir, slug)
    if not base.is_dir():
        raise ChannelAdminError(f"unknown channel: {slug!r}", kind="not_found")
    locks = _acquire_all_event_locks(base)
    try:
        shutil.rmtree(base)
    finally:
        for lock in locks:
            lock.release()  # rmtree already removed the lock files; tolerated
