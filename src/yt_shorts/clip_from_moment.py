"""Turn an operator-chosen window into a clip entry.

This is the ONLY path on which moment detection ever produces a clip, and it
runs on an explicit request - never as a side effect of scanning a stream.

`moment_url` is moved here unchanged from the retired `moment_entry.py`. Its
one rule still holds: clipid.canonical_url STRIPS the query string, so the
identity lives in the URL PATH. Two windows are two clips; the same window
chosen twice is the same clip.
"""

from __future__ import annotations

from pathlib import Path

from . import clipstore
from .clipid import clip_id

# How close two windows' start/end must be to count as "the same moment"
# rather than a collision. Exact float equality is the wrong test here: a
# window that has been through a JSON round-trip (write, then read back from
# clip.json/edit.json) can pick up float noise in the last bit or two, and a
# value re-derived from a rounded-to-tenths UI slider is not guaranteed to
# reproduce bit-for-bit either. A tenth of what actually matters - the whole
# second the identity is rounded to - is plenty of margin without being loose
# enough to call two genuinely different moments "the same".
WINDOW_TOLERANCE_SECONDS = 0.01


class ClipIdentityCollision(Exception):
    """Two different windows rounded to the same clip identity (path).

    `moment_url` rounds start/end to the nearest second before putting them in
    the URL path, so two windows inside the same rounded second - e.g.
    (10.1, 20.3) and (10.2, 20.4) - produce the identical identity and would
    otherwise land in the identical clip directory. Raised instead of letting
    `clipstore.write_clip` silently overwrite `clip.json` with the second
    window's data while a seeded `edit.json` (an operator's title correction)
    and `transcript.json` (decoded against the FIRST window's audio span) sit
    untouched beside it, now describing a moment that clip.json no longer does.
    """


class ClipIdentityUnreadable(Exception):
    """A directory already occupies this identity, but its clip.json will not parse.

    This is a SIBLING of `ClipIdentityCollision`, deliberately not a reuse of
    it: the two situations look similar from the caller's side (both refuse to
    write) but the operator's situation and remedy differ. An ordinary
    collision means "I compared your window to the neighbour's and they
    genuinely differ" - the fix is to pick a different window. This one means
    "there is a neighbour at this identity and I cannot read it well enough to
    compare at all" - the fix is to go look at that directory, not the window.
    A message-parsing caller could conflate the two; a distinct type lets a
    caller (the studio route, in particular) `except` them separately and show
    the operator the right one of two different explanations.

    Why refuse rather than proceed: proceeding is the bug this closes.
    `_existing_window` used to treat "cannot read this neighbour's clip.json"
    the same as "no neighbour here", so `create_clip` fell through to
    `clipstore.write_clip` - which independently ALSO cannot read the
    corrupted clip.json, and so ALSO fails to recognise the directory as
    already occupying this identity, and mints a brand new directory from the
    new hook instead of touching the corrupted one. The result is two sibling
    directories sharing one 8-character identity suffix, with nothing on
    screen to say a duplicate now exists - exactly the "one clip, one
    directory" invariant this project depends on, broken silently. Refusing
    is recoverable: an operator can inspect or delete the unreadable directory
    and retry, at the cost of the tool asking them to look, once. Proceeding
    would need no such round trip but leaves a landmine no one is told about.
    """


def moment_url(video_id: str, start: float, end: float) -> str:
    return (f"https://www.youtube.com/watch/{video_id}/"
            f"{int(round(start))}-{int(round(end))}")


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= WINDOW_TOLERANCE_SECONDS


def _existing_window(event_dir: str | Path, url: str) -> tuple[float, float] | Path | None:
    """What (if anything) already occupies this identity, in one of THREE states.

    Matches directories the same way `clipstore._existing_dir` names them -
    by the identity suffix, since two different hooks/titles produce two
    different slugs but the same suffix. The three states, distinguished by
    return type so a caller need not parse anything:

    - `None`: no directory at all carries this identity. Nothing to compare.
    - `(start, end)`: a neighbour exists and its clip.json parsed cleanly -
      the window to compare the new one against.
    - the neighbour's `Path`: a directory carries this identity but its
      clip.json could NOT be parsed, OR parsed but is missing `start`/`end`.
      This used to collapse into the `None` case (skipped, "no neighbour to
      compare"), which is exactly what let `create_clip` fall through to
      `clipstore.write_clip` - which is just as unable to read the same file,
      and so mints a SECOND, differently-named directory for the same
      identity instead of recognising the first one. Returning the Path
      instead lets `create_clip` refuse before that happens, rather than
      silently reproducing the corrupted-neighbour bug one function further
      down the call chain. A clip.json that parses as valid JSON but lacks
      `start`/`end` is the SAME situation as invalid JSON from this
      function's point of view - there is no window to compare against
      either way - and it used to fall through to `(None, None)` instead,
      which `_close`/`create_clip` then subtracted `float` from, raising a
      bare `TypeError` in place of the typed `ClipIdentityUnreadable` this
      case is supposed to raise.
    """
    identity = clip_id(url)
    for directory in clipstore.iter_clip_dirs(event_dir):
        if directory.name == identity or directory.name.endswith(f"--{identity}"):
            try:
                entry = clipstore.read_clip(directory)
            except clipstore.ClipStoreError:
                return directory
            start, end = entry.get("start"), entry.get("end")
            if start is None or end is None:
                return directory
            return start, end
    return None


def create_clip(event_dir: str | Path, *, video_id: str, start: float, end: float,
                hook: str, source_title: str) -> Path:
    """Writes one clip directory for this window and returns it.

    `moment_url` rounds start/end to the nearest second before encoding them
    into the URL path that becomes the clip's identity (see its own
    docstring on why the path, not the query). That rounding means two
    windows inside the same rounded second are, as far as the clip store is
    concerned, ONE identity - and this is a reproduced case, not a
    theoretical one: (10.1, 20.3) and (10.2, 20.4) both round to `.../10-20`.

    Calling this again with the SAME window (within `WINDOW_TOLERANCE_SECONDS`)
    is an ordinary, idempotent re-pick of the same moment and succeeds,
    updating the directory's clip.json in place exactly as `write_clip`
    always has. Calling it with a genuinely DIFFERENT window that happens to
    collide on the rounded identity is refused with `ClipIdentityCollision`
    instead of silently overwriting clip.json: `edit.json` (a human's title
    correction) and `transcript.json` (decoded against the FIRST window's
    audio span) would survive untouched beside a clip.json that now claims
    the SECOND window's start/end/hook, leaving the three files silently
    describing three different accounts of which moment this directory is -
    a mismatch that would surface only as a published short with the wrong
    title, long after the cause.

    A third case is refused with `ClipIdentityUnreadable` rather than either
    of the above: a directory already carries this identity, but its
    clip.json is corrupted (invalid JSON) and cannot be read at all, so there
    is nothing to compare the new window against - not "no neighbour", not "a
    known different neighbour", but "unknown". Proceeding here would not
    overwrite that directory (`clipstore.write_clip` cannot recognise it
    either, for the same reason) - it would mint a SECOND, freshly-named
    directory sharing the identical identity suffix, silently, with no
    on-screen sign a duplicate now exists. Refusing is recoverable: an
    operator can inspect or remove the unreadable directory and retry.
    """
    if end <= start:
        raise ValueError(f"window end ({end}) must be after start ({start})")
    url = moment_url(video_id, start, end)

    existing = _existing_window(event_dir, url)
    if isinstance(existing, Path):
        raise ClipIdentityUnreadable(
            f"video {video_id}: a clip directory already occupies the "
            f"identity for window ({start}, {end}) - {existing} - but its "
            f"clip.json cannot be read, so it is impossible to tell whether "
            f"creating this clip would collide with whatever that directory "
            f"holds. Inspect or remove {existing} and retry."
        )
    if existing is not None:
        existing_start, existing_end = existing
        if not (_close(existing_start, start) and _close(existing_end, end)):
            raise ClipIdentityCollision(
                f"video {video_id}: window ({start}, {end}) collides with the "
                f"rounded clip identity of an existing window "
                f"({existing_start}, {existing_end}) - both round to the same "
                f"path and would overwrite that clip's directory."
            )

    entry = {
        "url": url,
        "video_id": video_id,
        "hook": hook,
        "source_title": source_title,
        "start": start,
        "end": end,
        "duration": end - start,
    }
    return clipstore.write_clip(event_dir, entry)
