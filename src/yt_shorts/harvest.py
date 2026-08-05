"""Harvests YouTube community clips at timecodes.

Two sources: the hook comes from the HTML of the channel page (assigned by
the clipper), the timecodes come from yt-dlp. yt-dlp returns the source
video title under 'title', not the clip title — that's why the hook is
passed in from outside.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from . import clipid

TIMEOUT_SECONDS = 120


@dataclass
class ClipEntry:
    url: str
    hook: str
    source_title: str
    start: float
    end: float
    duration: float
    error: str | None = None


def read_metadata(raw_json: dict, url: str, hook: str) -> ClipEntry:
    """Shapes a yt-dlp response into a ClipEntry. No network access.

    Never raises: unexpected types/values in the yt-dlp response end up in
    the ``error`` field instead of crashing the caller.
    """
    missing = [f for f in ("section_start", "section_end") if f not in raw_json]
    if missing:
        return ClipEntry(
            url=url, hook=hook, source_title=raw_json.get("title", ""),
            start=0.0, end=0.0, duration=0.0,
            error=f"Fields missing in the yt-dlp response: {', '.join(missing)}",
        )
    try:
        start = float(raw_json["section_start"])
        end = float(raw_json["section_end"])
        raw_duration = raw_json.get("duration")
        # "duration" missing -> compute from start/end. "duration": 0 is a
        # present value and must not be treated like a missing one.
        duration = float(raw_duration) if raw_duration is not None else (end - start)
    except (TypeError, ValueError) as error:
        return ClipEntry(
            url=url, hook=hook, source_title=raw_json.get("title", ""),
            start=0.0, end=0.0, duration=0.0,
            error=f"{type(error).__name__}: {error}",
        )
    # An out-of-order or negative window is malformed input: catch it here with a
    # clear reason (like timecode.with_padding does) rather than letting a
    # negative/incoherent window flow downstream as "valid" and fail far later in
    # ffmpeg's --download-sections with a much less clear error.
    if end < start or duration < 0:
        return ClipEntry(
            url=url, hook=hook, source_title=raw_json.get("title", ""),
            start=0.0, end=0.0, duration=0.0,
            error=f"clip window is not valid: start={start}, end={end}, "
                  f"duration={duration}",
        )
    return ClipEntry(
        url=url, hook=hook, source_title=raw_json.get("title", ""),
        start=start, end=end, duration=duration, error=None,
    )


def _query_ytdlp(url: str, ytdlp: str) -> dict:
    clipid.require_http_url(url)  # http(s) only; a bad URL is isolated per-entry
    result = subprocess.run(
        [ytdlp, "-J", "--no-warnings", "--", url],
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr else "yt-dlp failed")
    return json.loads(result.stdout)


def harvest(inputs: list[dict], ytdlp: str = "yt-dlp") -> list[ClipEntry]:
    """Queries yt-dlp for each input {url, hook}. A single failure never stops the run.

    Every input is handled in isolation: whatever exception occurs (missing
    yt-dlp binary, broken input, malformed JSON, an unexpected exception
    from yt-dlp itself, ...), it must never take down the whole run. The
    reason lands with exception type and message in that entry's ``error``
    field instead of being swallowed.
    """
    entries: list[ClipEntry] = []
    for input_ in inputs:
        url = input_.get("url", "")
        hook = input_.get("hook", "")
        try:
            if "url" not in input_:
                raise KeyError("url")
            entries.append(read_metadata(_query_ytdlp(url, ytdlp), url, hook))
        except Exception as error:
            entries.append(ClipEntry(
                url=url, hook=hook, source_title="",
                start=0.0, end=0.0, duration=0.0, error=f"{type(error).__name__}: {error}",
            ))
    return entries
