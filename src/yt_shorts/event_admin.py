"""Create, rename and delete an event directory for the studio (stage G2).

Pure filesystem operations over the workspace's channels dir - no FastAPI - so
the studio's routes stay a thin layer over this (mirrors workspace_listing.py).
An event is a directory channels/<channel>/events/<name>/; this module manages
that directory's LIFECYCLE and nothing inside it (the studio still writes only
edit.json within an event). Rename and delete take the event's EventLock first,
so they cannot run against an event a render or detect is using.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import pathnames
from .lock import EventLock, LockError


class EventAdminError(Exception):
    """A create/rename/delete request that cannot be honoured. `kind` lets the
    studio route map it to an HTTP status without string-sniffing:
    "bad_name" -> 400, "not_found" -> 404, "exists"/"locked" -> 409."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def validate_name(name: str, *, what: str = "event name") -> None:
    """Reject anything that is not one safe path segment (see
    pathnames.validate_segment). This is the security boundary: a name here
    becomes a directory name, so '..', a slash or a leading dot must never
    reach the filesystem. Used for both the event name and (via _events_dir)
    the channel segment - the latter is just as much a path component, so a
    traversal channel like '..' cannot make a create or delete act outside
    channels/."""
    try:
        pathnames.validate_segment(name, what=what)
    except ValueError as error:
        raise EventAdminError(str(error), kind="bad_name") from error


def _events_dir(channels_dir, channel: str) -> Path:
    # Validate the channel segment BEFORE joining it: it is a path component
    # just like an event name, so a '..' here would otherwise let events/ reach
    # outside channels/ (a create/delete escape).
    validate_name(channel, what="channel name")
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
