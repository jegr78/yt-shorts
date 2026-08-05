"""List a workspace's channels and a channel's events for the studio's start
screen (see the stage G1 design). Pure filesystem reads over the resolved
workspace's channels dir - no FastAPI, no profile loading, cheap counts only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import clipstore, editorial
from .pathnames import validate_segment


@dataclass
class ChannelInfo:
    name: str
    display_name: str
    handle: str
    event_count: int
    error: str | None = None


@dataclass
class EventInfo:
    name: str
    clip_count: int
    kept_count: int
    rendered_count: int


def list_channels(channels_dir) -> list[ChannelInfo]:
    root = Path(channels_dir)
    if not root.is_dir():
        return []
    out: list[ChannelInfo] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        events_dir = entry / "events"
        event_count = sum(1 for e in events_dir.iterdir() if e.is_dir()) \
            if events_dir.is_dir() else 0
        try:
            data = json.loads((entry / "channel.json").read_text(encoding="utf-8"))
            out.append(ChannelInfo(
                name=entry.name,
                display_name=data.get("display_name", ""),
                handle=data.get("handle", ""),
                event_count=event_count))
        except (OSError, json.JSONDecodeError) as error:
            out.append(ChannelInfo(name=entry.name, display_name="", handle="",
                                   event_count=event_count,
                                   error=f"channel.json unreadable: {error}"))
    return out


def list_events(channels_dir, channel) -> list[EventInfo]:
    # channel is a caller-supplied path segment; validate it before enumerating,
    # so a '../..' cannot list an events/-shaped tree outside the channels dir.
    try:
        validate_segment(channel, what="channel name")
    except ValueError:
        return []
    events_dir = Path(channels_dir) / channel / "events"
    if not events_dir.is_dir():
        return []
    out: list[EventInfo] = []
    for entry in sorted(p for p in events_dir.iterdir() if p.is_dir()):
        clip_count = kept_count = rendered_count = 0
        for directory in clipstore.iter_clip_dirs(entry):
            clip_count += 1
            try:
                if editorial.load(directory).status == editorial.KEPT:
                    kept_count += 1
            except editorial.EditError:
                pass  # unreadable edit.json: count the clip, just not as kept
            if clipstore.short_path(directory).exists():
                rendered_count += 1
        out.append(EventInfo(name=entry.name, clip_count=clip_count,
                            kept_count=kept_count, rendered_count=rendered_count))
    return out
