"""List a channel's streams with yt-dlp.

The tool already depends on yt-dlp for every download, and one
`yt-dlp --flat-playlist --dump-json <channel>/streams` call returns a
channel's finished streams with the fields this needs - id, title, duration,
view count - newest first. No YouTube Data API key, no quota, no OAuth: see
the stage D1 design for why the API-key path was dropped.

The subprocess call is injected as `runner` so parsing and error handling are
tested against recorded output without the network, mirroring how `harvest`
isolates its own yt-dlp call.

`channel_catalogue` composes three reads - the Streams tab, the channel's
playlist list, and every playlist's members - into one answer, so the studio
can filter a long stream list by playlist. Measured on ERF (2026-08-04, one
channel on one day, not a guarantee): 91 streams, 17 playlists, 99 distinct
videos, all 17 member fetches in 2.5s at six threads - both measured
directly, and each playlist fetch on its own costs about 1.2s, also
measured. "20s sequential" is not a third measurement: it is 17 x 1.2s
extrapolated from the per-playlist figure, not a run that was actually
timed one playlist after another.
The eight videos in a playlist but NOT in the Streams tab include two
multi-hour broadcasts, so the union is what makes them reachable at all.

Concurrency lives here and nowhere else in this module: each worker calls the
same injected `runner`, so the whole thing still tests without a network.
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import clipid
from .logsetup import shorten_urls

# A YouTube playlist id, as it goes into a URL we hand to a subprocess.
# Validated rather than trusted: an id carrying '&' would append query
# parameters of its own to that URL.
_PLAYLIST_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")


class YouTubeError(Exception):
    """Understandable message about a failed yt-dlp discovery call."""


@dataclass
class Stream:
    video_id: str
    title: str
    duration_seconds: int | None
    view_count: int | None


@dataclass
class Playlist:
    """One playlist of a channel. `count`/`unavailable` are 0 as
    `list_playlists` returns it and are filled in by `channel_catalogue`
    from the member fetch - see `list_playlists` on why they cannot come
    from the playlists tab itself."""
    id: str
    title: str
    count: int = 0
    unavailable: int = 0


@dataclass
class Video:
    """A `Stream` plus where it sits. `playlist_ids` is a LIST because a
    video may belong to several playlists - none does on ERF today, which
    is an observation of one channel on one day, not a guarantee."""
    video_id: str
    title: str
    duration_seconds: int | None
    view_count: int | None
    playlist_ids: list[str] = field(default_factory=list)


@dataclass
class PlaylistContents:
    """What one playlist holds: the usable videos, and how many entries were
    dropped for having no title (a deleted or private video). The count is
    carried rather than discarded so a displayed size is never silently
    smaller than the playlist really is."""
    videos: list[Video]
    unavailable: int


@dataclass
class FailedPlaylist:
    """A playlist whose fetch failed. Named, so the operator learns WHICH
    part of the catalogue is missing instead of reading a short list as a
    complete one."""
    title: str
    reason: str


@dataclass
class Catalogue:
    videos: list[Video]
    playlists: list[Playlist]
    failed_playlists: list[FailedPlaylist]


def _default_runner(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()[-1] if result.stderr \
            else "yt-dlp failed"
        raise RuntimeError(message)
    return result.stdout


def _entries(output: str):
    """Yield one usable yt-dlp `--dump-json` object per line.

    Three drops, and the rule about WHICH of them get counted lives one layer
    up rather than here: a blank line, a line that is not JSON, and an entry
    that is not an object or carries no `id` are yt-dlp OUTPUT defects, so
    they are skipped silently by all three callers. What must never be
    silently dropped is a well-formed entry describing a video the caller
    cannot have - a deleted or private one, which arrives with a null title -
    and that is `list_playlist_videos`'s own business to count as
    `unavailable`. See its docstring, and CLAUDE.md's "two drops, two
    different answers".

    One generator rather than three copies of the loop, for the reason
    `detect.stream_dir` gives for the same move: three copies of one rule is
    three chances for one of them to drift.
    """
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("id"):
            yield entry


def list_streams(channel_url: str, *, runner=_default_runner) -> list[Stream]:
    """Returns a channel's finished streams, newest first.

    A malformed line is skipped rather than sinking the list, an entry with no
    id is skipped, and a missing duration or view count is tolerated - the same
    per-entry tolerance the rest of the tool applies to yt-dlp output. A yt-dlp
    failure is raised as YouTubeError.
    """
    clipid.require_http_url(channel_url)  # http(s) only, no '-'-leading flag value
    args = ["yt-dlp", "--flat-playlist", "--dump-json", "--",
            f"{channel_url}/streams"]
    try:
        output = runner(args)
    except Exception as error:
        raise YouTubeError(f"Could not list streams for {channel_url}: {error}") \
            from error

    streams: list[Stream] = []
    for entry in _entries(output):
        streams.append(Stream(
            video_id=entry["id"],
            title=entry.get("title", ""),
            duration_seconds=_int_or_none(entry.get("duration")),
            view_count=_int_or_none(entry.get("view_count")),
        ))
    return streams


def _int_or_none(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def list_playlists(channel_url: str, *, runner=_default_runner) -> list[Playlist]:
    """The channel's playlists, in the order the tab returns them.

    `count` and `unavailable` come back 0 and are NOT read from yt-dlp's
    `playlist_count`: on the /playlists tab that field is the number of
    PLAYLISTS on the channel, identical on every row (17 on ERF), not the
    size of the playlist the row describes. The real sizes come from the
    member fetch, which is why `channel_catalogue` is what sets them.
    """
    clipid.require_http_url(channel_url)
    args = ["yt-dlp", "--flat-playlist", "--dump-json", "--",
            f"{channel_url}/playlists"]
    try:
        output = runner(args)
    except Exception as error:
        raise YouTubeError(
            f"Could not list playlists for {channel_url}: {error}") from error

    playlists: list[Playlist] = []
    for entry in _entries(output):
        playlists.append(Playlist(id=entry["id"],
                                  title=entry.get("title") or ""))
    return playlists


def list_playlist_videos(playlist_id: str,
                         *, runner=_default_runner) -> PlaylistContents:
    """One playlist's videos, in playlist order.

    An id is consumed once, no matter how many times it appears - group by
    id, and the BEST occurrence wins. If any occurrence of an id carries a
    title, the video is offered (built from the FIRST titled occurrence) and
    it is never counted as unavailable, even when a titleless line for the
    same id sits elsewhere in the playlist. Only an id that is titleless on
    EVERY occurrence is a reported loss, and it is counted once no matter how
    many titleless lines repeat it. The two outcomes are different in kind: a
    video titleless everywhere is one the operator cannot have, which is a
    loss worth reporting, while a repeat - titleless, titled, or one of
    each - costs them nothing once any occurrence proves it is available.
    Counting a duplicate would make the dropdown report a loss that did not
    happen; letting a titleless repeat inflate the count, or letting an
    available video hide behind an earlier titleless line, would both be
    that same dishonesty, just pointed different ways.

    `playlist_ids` is returned EMPTY on every video: the catalogue is the
    one owner of that field, and half-filling it here would give it two.
    """
    # `isinstance` before the match, same rule and for the same reason as
    # `pathnames.validate_segment`: `None` and `""` were refused as ValueError
    # by the `or ""` while a TRUTHY non-string (an int, a list) went straight
    # into `re.match` and left as a TypeError - one defect with two answers,
    # decided by the value. `channel_catalogue` catches Exception per playlist
    # and reports it in `failed_playlists`, so this was never harmful here;
    # it is fixed because the shape is the one this project has now paid for
    # twice, not because it was reachable.
    if not isinstance(playlist_id, str) or not _PLAYLIST_ID.match(playlist_id):
        raise ValueError(
            f"not a usable playlist id: {playlist_id!r} - a playlist id is "
            f"letters, digits, '-' and '_' only")
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    args = ["yt-dlp", "--flat-playlist", "--dump-json", "--", url]
    try:
        output = runner(args)
    except Exception as error:
        raise YouTubeError(
            f"Could not list playlist {playlist_id}: {error}") from error

    # id -> the first occurrence of it that carried a title, or None while
    # every occurrence seen so far was titleless. ONE map rather than three
    # parallel structures: a dict keeps its insertion order, so this is also
    # the playlist's own first-appearance order, and "is this id available?"
    # is simply whether its value is None - not a second dict that has to be
    # kept in agreement with this one.
    best: dict[str, dict | None] = {}
    for entry in _entries(output):
        video_id = entry["id"]
        if best.get(video_id) is None:
            # Either the first sight of this id, or one only ever seen
            # titleless: a titled occurrence supersedes it, a titleless one
            # leaves it exactly as it was while still recording the id, and
            # with it the place in playlist order this id will keep. An id
            # that already holds a titled entry is left alone - the FIRST
            # title wins.
            best[video_id] = entry if entry.get("title") else None

    videos: list[Video] = []
    unavailable = 0
    for video_id, entry in best.items():
        if entry is None:
            unavailable += 1
            continue
        videos.append(Video(
            video_id=video_id,
            title=entry["title"],
            duration_seconds=_int_or_none(entry.get("duration")),
            view_count=_int_or_none(entry.get("view_count")),
        ))
    return PlaylistContents(videos=videos, unavailable=unavailable)


def channel_catalogue(channel_url: str, *, runner=_default_runner,
                      max_workers: int = 6) -> Catalogue:
    """The channel's streams, its playlists, and which video sits where.

    The video list is the UNION of the Streams tab and every playlist's
    members, streams first in their existing order and playlist-only videos
    appended - so nothing an operator already knew moves, and the eight
    videos ERF keeps only in playlists become reachable.

    Failure is tolerated per playlist, never for the Streams tab: without
    that there is no list at all, so its `YouTubeError` propagates (the
    studio's route turns it into a 502). A playlist that fails - including
    the playlists tab itself - is recorded in `failed_playlists` and the
    rest is served, because a half catalogue that looks whole is exactly
    the silent degradation this project keeps paying for.

    `max_workers` is a starting point for this machine, like
    `worker.DEFAULT_LIMITS` - not a measurement of what YouTube tolerates.
    """
    streams = list_streams(channel_url, runner=runner)
    videos: dict[str, Video] = {
        s.video_id: Video(video_id=s.video_id, title=s.title,
                          duration_seconds=s.duration_seconds,
                          view_count=s.view_count, playlist_ids=[])
        for s in streams
    }
    failed: list[FailedPlaylist] = []
    try:
        playlists = list_playlists(channel_url, runner=runner)
    except YouTubeError as error:
        # `shorten_urls`, like every other place this project writes text that
        # came out of an external tool (stream_transcribe's chunk warning,
        # jobs.Job.record, worker._fail, api._entry_json). It is the FIFTH
        # such site and the only one whose text is rendered in a BROWSER
        # rather than written to a log, which makes skipping it the wrong way
        # round: yt-dlp's message is quoted verbatim, and the rule here has
        # never been "wrap it where a secret is proven" - it is that every
        # such site wraps, so the next reader counting them finds no
        # exception.
        failed.append(FailedPlaylist(
            title="the channel's playlist list", reason=shorten_urls(str(error))))
        return Catalogue(videos=list(videos.values()), playlists=[],
                         failed_playlists=failed)

    kept: list[Playlist] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(list_playlist_videos, playlist.id, runner=runner)
                   for playlist in playlists]
        for playlist, future in zip(playlists, futures, strict=True):
            try:
                contents = future.result()
            except Exception as error:
                failed.append(FailedPlaylist(     # elided, as just above
                    title=playlist.title or playlist.id,
                    reason=shorten_urls(str(error))))
                continue
            playlist.count = len(contents.videos)
            playlist.unavailable = contents.unavailable
            kept.append(playlist)
            for video in contents.videos:
                known = videos.get(video.video_id)
                if known is None:
                    known = video
                    videos[video.video_id] = known
                if playlist.id not in known.playlist_ids:
                    known.playlist_ids.append(playlist.id)

    return Catalogue(videos=list(videos.values()), playlists=kept,
                     failed_playlists=failed)
