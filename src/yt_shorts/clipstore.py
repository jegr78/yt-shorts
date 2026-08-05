"""One clip, one directory.

The previous layout scattered a clip across three folders - drafts/x.mp4,
raw/x.raw.mp4, transcripts/x.json - all keyed on a slug derived from the
title. Backing up, deleting or inspecting one clip meant touching three
places and hoping the names still lined up; they stopped lining up as soon
as a title changed or the source list was reordered.

Here everything belonging to a clip sits under one directory named by
clipid.directory_name, so those operations become one operation on one path.
The cost, deliberately accepted: listing "all finished shorts" is no longer
`ls drafts/` and needs the tool.
"""

from __future__ import annotations

import json
from pathlib import Path

from .clipid import canonical_url, clip_id, directory_name
from .pathnames import validate_segment

CLIPS_DIRNAME = "clips"
CLIP_FILENAME = "clip.json"


class ClipStoreError(Exception):
    """Understandable error message about the clip store on disk."""


def clips_dir(event_dir: str | Path) -> Path:
    return Path(event_dir) / CLIPS_DIRNAME


def clip_dir_by_name(event_dir: str | Path, name: str) -> Path:
    """The directory of the clip called `name` - the ONE place a
    caller-supplied clip NAME becomes a path, and the one place that name is
    validated as a single safe path segment. Raises ValueError for anything
    else, BEFORE any filesystem touch.

    It exists because the rule was kept on one half of this project and not
    the other. A clip name arrives from an operator two ways: as a URL path
    segment (`GET …/clips/{name}`, which validated it) and inside a request
    BODY - a render's `clips` list, a queue entry's `clip` - which did not.
    Measured, not feared: `POST …/render {"clips": ["../../../../../OUTSIDE"]}`
    answered 200, and `render.build_short` was handed
    `<event>/clips/../../../../../OUTSIDE/short.mp4` as its target and that
    same directory as its work dir - so a short, a raw download and an overlay
    PNG were written outside the event entirely. A queued `trim` naming the
    same thing reached `trim.ensure_applied`, which renames `short.mp4` aside
    and re-encodes it.

    Built here rather than at each call site for the reason `detect.stream_dir`
    gives for the identical move: three copies of one rule is three chances for
    one of them to drift, and a caller that simply forgot is exactly how this
    gap appeared in the first place. `clips_dir(...) / name` still exists and
    is still right for a name this project produced ITSELF (`clipid.
    directory_name`, or a directory name read back off disk); this is the one
    to use for any name that came from outside.
    """
    validate_segment(name, what="clip name")
    return clips_dir(event_dir) / name


def clip_dir(event_dir: str | Path, url: str, harvested_title: str) -> Path:
    return clips_dir(event_dir) / directory_name(url, harvested_title)


def _existing_dir(event_dir: str | Path, url: str) -> Path | None:
    """Finds a clip's directory by its IDENTITY, not by its current name.

    This is the whole point of the id suffix. Looking a clip up by
    recomputing its name from the title would fail for exactly the case the
    layout exists to handle: a retitled clip's recomputed name is new, so the
    lookup misses and a second directory appears, orphaning everything under
    the old one. Verified against that case before this plan was written.

    When a name match is found, the directory's clip.json is read to verify
    it actually belongs to this clip (by comparing stored URL to the incoming
    one). This prevents silent merging if the directory has no clip.json or
    if it contains a different clip (identity collision).

    The comparison is on CANONICAL URLs, not raw strings: clip_id() already
    strips a query string (a YouTube share parameter, say), a fragment and a
    trailing slash before hashing two URLs into the same identity, so a
    directory match on that identity must accept the same variation - a
    stored URL and an incoming one that canonicalize to the same thing ARE
    the same clip. Comparing the raw strings instead reported that case as
    an "Identity collision" against itself, which is actively false.
    """
    identity = clip_id(url)
    directory = clips_dir(event_dir)
    if not directory.is_dir():
        return None
    for candidate in sorted(directory.iterdir()):
        if candidate.is_dir() and (candidate.name == identity
                                   or candidate.name.endswith(f"--{identity}")):
            # Verify this directory actually belongs to this clip by checking clip.json
            clip_file = candidate / CLIP_FILENAME
            if not clip_file.exists():
                # No clip.json, not our directory
                continue
            try:
                payload = json.loads(clip_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    stored_url = payload.get("url")
                    if (isinstance(stored_url, str)
                            and canonical_url(stored_url) == canonical_url(url)):
                        # Same clip (possibly a share-parameter variant of
                        # the same URL) - this is our directory.
                        return candidate
                    # Canonically different - this is a genuine identity
                    # collision.
                    raise ClipStoreError(
                        f"Identity collision: two different clips share directory {candidate.name}\n"
                        f"  Stored URL: {stored_url if stored_url is not None else '<no url>'}\n"
                        f"  Incoming URL: {url}"
                    )
            except json.JSONDecodeError:
                # clip.json is corrupted, skip this directory
                continue
    return None


def write_clip(event_dir: str | Path, entry: dict) -> Path:
    """Writes the derived facts about a clip and returns its directory.

    The directory is named from the URL and the title *as harvested*. Calling
    this again with a different title updates the file in place and keeps the
    directory, because the URL - not the label - is the identity.
    """
    url = entry.get("url")
    if not url:
        raise ClipStoreError(f"Clip entry has no url: {entry!r}")

    try:
        directory = _existing_dir(event_dir, url) or clip_dir(
            event_dir, url, entry.get("hook", ""))
    except ValueError as error:
        raise ClipStoreError(f"Invalid URL: {url!r}\n{error}") from error
    directory.mkdir(parents=True, exist_ok=True)
    (directory / CLIP_FILENAME).write_text(
        json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return directory


def read_clip(clip_dir_: str | Path) -> dict:
    path = Path(clip_dir_) / CLIP_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ClipStoreError(f"Clip file is unreadable: {path}\n{error}") from error
    if not isinstance(payload, dict):
        raise ClipStoreError(f"Clip file must hold an object: {path}")
    return payload


def iter_clip_dirs(event_dir: str | Path) -> list[Path]:
    """Every clip directory of an event, in a stable order."""
    directory = clips_dir(event_dir)
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir()
                   if p.is_dir() and (p / CLIP_FILENAME).exists()),
                  key=lambda p: p.name)


def transcript_path(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "transcript.json"


def raw_path(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "raw.mp4"


def short_path(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "short.mp4"


def short_master_path(clip_dir_: str | Path) -> Path:
    """The untrimmed render, kept only while a trim is applied."""
    return Path(clip_dir_) / "short.full.mp4"


def short_trim_state_path(clip_dir_: str | Path) -> Path:
    """Which trim short.mp4 currently embodies. Absent means none."""
    return Path(clip_dir_) / "short.trim.json"


def subs_track_path(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "subs.mov"


def subs_work_dir(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "subs"
