# Playlists and Bulk Queueing Implementation Plan

> **STATUS: COMPLETE, merged into `master` as 11aa00a.** The
> unchecked `- [ ]` boxes below are the execution skill's per-step
> artefact and were never ticked; they are NOT open work. The
> authority on what was done is `git log` plus the ledger at
> `.superpowers/sdd/progress.md`. Every task landed; the Minor findings it left behind were closed by the plan beside this one.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group the studio's stream list by the channel's YouTube playlists, show per-stream what already exists, and let an operator queue transcription and detection for several streams in one action.

**Architecture:** `youtube.py` grows a catalogue built from the Streams tab plus every playlist's members (fetched in parallel through the injected `runner`). `GET …/streams` serves it as one richer payload with two freshly-stat'd flags per video. The frontend gains one pure module (`streams.ts`) for the filter and the bulk decision, a multi-entry polling hook, and a rewired `StreamPanel`. **No change to `job_queue.py`, `worker.py` or `_validate_enqueue`** — the queue's existing `Entry.after` already chains a detect behind its transcription.

**Tech Stack:** Python 3 (stdlib + `concurrent.futures`), FastAPI, pytest, React + Mantine + TypeScript, Vitest, Playwright (inside pytest).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Full suite: `PYTHONPATH=src .venv/bin/pytest -q`.
- `python3 tools/lint.py` must exit 0 before every commit. **Lint is not optional before trusting a green suite** — a duplicate test-class name is dropped silently by pytest and only ruff's `F811` catches it.
- **Bare `npx tsc --noEmit` is INERT here.** `npm run build` (which runs `tsc -b`) is the real type-check. Run it from `src/yt_shorts/studio/web/`.
- The built frontend at `src/yt_shorts/studio/static/` is **committed**. Every task that changes `web/src/` ends with `npm run build` and commits `static/` — the Playwright E2E serves from `static/`, so a stale build makes later tasks test the old page.
- `npm test` (Vitest, jsdom) runs from `src/yt_shorts/studio/web/`.
- Pure modules (`youtube.py`, `detect.py`, `streams.ts`, `jobs.ts`) must not import FastAPI. Frontend pure logic lives in its own module, never exported from a component file — Vite's fast-refresh boundary stays component-only.
- **No silent shrinking.** Anything dropped from a list is counted and reported. This project's recurring failure mode is a degraded result that looks like a healthy one.
- Every number quoted in a comment must be labelled as a measurement of one channel on one day, never as a guarantee.

---

## File Structure

**Create:**
- `src/yt_shorts/studio/web/src/streams.ts` — the list's pure rules (filter options, visible list, bulk decision)
- `src/yt_shorts/studio/web/src/streams.test.ts` — its Vitest suite
- `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts` — poll the plan once, follow N entries
- `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.test.tsx` — its Vitest suite

**Modify:**
- `src/yt_shorts/youtube.py` — playlists, playlist members, the catalogue
- `tests/test_youtube.py` — its tests
- `src/yt_shorts/detect.py` — `stream_dir`, `has_cached_transcript`, `has_analysis`
- `tests/test_detect.py` — their tests
- `src/yt_shorts/studio/api.py:1874-1935` — the streams route's payload, and `post_detect`'s title lookup
- `tests/test_studio_api.py:524-574` — `TestStreamsRoute`
- `src/yt_shorts/studio/web/src/api.ts:715-746` — the `Stream`/catalogue types and both callers
- `src/yt_shorts/studio/web/src/jobs.ts` — `waitNote`'s dependency branch, `batchNotice`
- `src/yt_shorts/studio/web/src/jobs.test.ts` — their tests
- `src/yt_shorts/studio/web/src/hooks/useQueuedJob.ts` — re-expressed on `useQueuedEntries`
- `src/yt_shorts/studio/web/src/components/StreamPanel.tsx` — filter, markers, selection, bulk bar
- `src/yt_shorts/studio/web/src/components/StreamScreen.tsx:107-115` — the title lookup
- `src/yt_shorts/studio/web/src/App.tsx:75-90, 358-412` — one tracking map instead of four state variables
- `tests/test_studio_e2e.py` — the new journeys
- `CLAUDE.md`, `README.md` — what changed and why

---

### Task 1: The channel catalogue in `youtube.py`

**Files:**
- Modify: `src/yt_shorts/youtube.py`
- Test: `tests/test_youtube.py`

**Interfaces:**
- Consumes: the existing `list_streams(channel_url, *, runner)` and `Stream`, unchanged.
- Produces: `Playlist(id, title, count, unavailable)`, `Video(video_id, title, duration_seconds, view_count, playlist_ids)`, `FailedPlaylist(title, reason)`, `PlaylistContents(videos, unavailable)`, `Catalogue(videos, playlists, failed_playlists)`, and `list_playlists(channel_url, *, runner)`, `list_playlist_videos(playlist_id, *, runner)`, `channel_catalogue(channel_url, *, runner, max_workers=6)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_youtube.py` (keep the existing `CHANNEL`, `LINES` and `runner_returning` at the top of the file; add the new imports to the existing import line):

```python
# Recorded from a real `yt-dlp --flat-playlist --dump-json .../playlists`
# run on 2026-08-04, trimmed to the fields we read. `playlist_count` is
# deliberately included and deliberately ignored: on the /playlists tab it
# is the number of PLAYLISTS (identical on every row), not the size of that
# playlist - see list_playlists' own docstring.
PLAYLIST_LINES = "\n".join(json.dumps(d) for d in [
    {"_type": "url", "id": "PLaaa", "title": "2026 Nürburgring 24 Hour",
     "playlist_count": 2},
    {"_type": "url", "id": "PLbbb", "title": "Bathurst 12 Hour 2025",
     "playlist_count": 2},
])

# One playlist's members. The third entry is a deleted or private video -
# yt-dlp reports it with a null title and no duration, and two such entries
# really do sit in ERF's playlists.
MEMBER_LINES = "\n".join(json.dumps(d) for d in [
    {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
     "duration": 28431, "view_count": 1800},
    {"id": "newvid0001", "title": "ERF Special Catalunya 6H Part 2",
     "duration": 8983, "view_count": 400},
    {"id": "goneforever", "title": None, "duration": None, "view_count": None},
])


def runner_for(mapping, default=""):
    """A runner that answers by which URL it is asked for.

    Called from several THREADS by channel_catalogue, so it records into a
    list (append is atomic under the GIL) and the tests compare the record
    as a SET - the order threads finish in is not a property worth pinning.
    """
    calls = []

    def run(args):
        calls.append(args[-1])
        for fragment, text in mapping.items():
            if fragment in args[-1]:
                return text
        return default

    run.calls = calls
    return run


class TestListPlaylists:
    def test_the_playlists_tab_is_asked_for(self):
        runner = runner_for({"/playlists": PLAYLIST_LINES})
        list_playlists(CHANNEL, runner=runner)
        assert runner.calls == [f"{CHANNEL}/playlists"]

    def test_id_and_title_are_read(self):
        runner = runner_for({"/playlists": PLAYLIST_LINES})
        playlists = list_playlists(CHANNEL, runner=runner)
        assert [(p.id, p.title) for p in playlists] == [
            ("PLaaa", "2026 Nürburgring 24 Hour"),
            ("PLbbb", "Bathurst 12 Hour 2025")]

    def test_the_sizes_are_not_taken_from_the_tab(self):
        """yt-dlp's `playlist_count` on this tab is the number of playlists,
        identical on every row - reading it as the playlist's own size is
        the mistake this pins shut. channel_catalogue fills these in."""
        runner = runner_for({"/playlists": PLAYLIST_LINES})
        playlists = list_playlists(CHANNEL, runner=runner)
        assert [(p.count, p.unavailable) for p in playlists] == [(0, 0), (0, 0)]

    def test_a_failure_is_a_youtube_error(self):
        def boom(args):
            raise RuntimeError("yt-dlp is not installed")
        with pytest.raises(YouTubeError, match="yt-dlp is not installed"):
            list_playlists(CHANNEL, runner=boom)


class TestListPlaylistVideos:
    def test_members_are_read_with_no_playlist_ids_yet(self):
        """The `playlist_ids` field has exactly ONE owner - the catalogue -
        so this returns it empty rather than half-filling it."""
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(MEMBER_LINES))
        assert [v.video_id for v in contents.videos] == [
            "xQlD7MkC-Eo", "newvid0001"]
        assert all(v.playlist_ids == [] for v in contents.videos)

    def test_a_titleless_entry_is_dropped_and_counted(self):
        """A deleted or private video. Dropped, because it can never be
        transcribed - and COUNTED, so a "(2)" in the dropdown is not
        silently a 2 that came from 3."""
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(MEMBER_LINES))
        assert len(contents.videos) == 2
        assert contents.unavailable == 1

    @pytest.mark.parametrize("bad", ["PL&list=other", "../etc", "PL id", ""])
    def test_an_unsafe_playlist_id_never_reaches_the_runner(self, bad):
        def boom(args):
            raise AssertionError("runner should not run for a bad id")
        with pytest.raises(ValueError, match="playlist id"):
            list_playlist_videos(bad, runner=boom)


class TestChannelCatalogue:
    def _runner(self):
        return runner_for({
            "/streams": LINES,
            "/playlists": PLAYLIST_LINES,
            "list=PLaaa": MEMBER_LINES,
            "list=PLbbb": "",
        })

    def test_every_playlist_is_fetched(self):
        runner = self._runner()
        channel_catalogue(CHANNEL, runner=runner)
        assert set(runner.calls) == {
            f"{CHANNEL}/streams",
            f"{CHANNEL}/playlists",
            "https://www.youtube.com/playlist?list=PLaaa",
            "https://www.youtube.com/playlist?list=PLbbb",
        }

    def test_the_video_list_is_the_union_streams_first(self):
        """A playlist may hold a broadcast the Streams tab does not list -
        measured: 8 such videos on ERF, two of them multi-hour races that
        were unreachable from the studio before this. They are APPENDED, so
        the order the Streams tab already had is untouched."""
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        assert [v.video_id for v in catalogue.videos] == [
            "xQlD7MkC-Eo", "2O_lQrxEHWo", "Esm9vv5-PdU", "newvid0001"]

    def test_membership_is_recorded_on_the_video(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        by_id = {v.video_id: v for v in catalogue.videos}
        assert by_id["xQlD7MkC-Eo"].playlist_ids == ["PLaaa"]
        assert by_id["newvid0001"].playlist_ids == ["PLaaa"]
        assert by_id["2O_lQrxEHWo"].playlist_ids == []

    def test_playlist_sizes_come_from_the_members(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        sizes = {p.id: (p.count, p.unavailable) for p in catalogue.playlists}
        assert sizes == {"PLaaa": (2, 1), "PLbbb": (0, 0)}

    def test_one_failing_playlist_does_not_sink_the_catalogue(self):
        """The same per-entry tolerance list_streams already applies to a
        malformed line. A half catalogue that looks whole is the failure
        mode this project keeps paying for, so the loss is REPORTED."""
        def run(args):
            url = args[-1]
            if "list=PLbbb" in url:
                raise RuntimeError("HTTP Error 404: Not Found")
            if "/streams" in url:
                return LINES
            if "/playlists" in url:
                return PLAYLIST_LINES
            return MEMBER_LINES

        catalogue = channel_catalogue(CHANNEL, runner=run)
        assert [p.id for p in catalogue.playlists] == ["PLaaa"]
        assert len(catalogue.failed_playlists) == 1
        assert catalogue.failed_playlists[0].title == "Bathurst 12 Hour 2025"
        assert "404" in catalogue.failed_playlists[0].reason

    def test_a_failing_playlist_tab_still_serves_the_streams(self):
        """A channel with no playlists tab, or a failure fetching it, must
        leave the list exactly as useful as it was before this feature."""
        def run(args):
            if "/playlists" in args[-1]:
                raise RuntimeError("HTTP Error 404: Not Found")
            return LINES

        catalogue = channel_catalogue(CHANNEL, runner=run)
        assert [v.video_id for v in catalogue.videos] == [
            "xQlD7MkC-Eo", "2O_lQrxEHWo", "Esm9vv5-PdU"]
        assert catalogue.playlists == []
        assert len(catalogue.failed_playlists) == 1

    def test_a_failing_streams_tab_still_raises(self):
        """The one failure that is NOT tolerated: without the Streams tab
        there is no list at all, and the route turns this into a 502."""
        def run(args):
            raise RuntimeError("yt-dlp is not installed")
        with pytest.raises(YouTubeError):
            channel_catalogue(CHANNEL, runner=run)
```

Update the import line at the top of the file to:

```python
from yt_shorts.youtube import (
    Stream, YouTubeError, channel_catalogue, list_playlist_videos,
    list_playlists, list_streams,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_youtube.py -q`
Expected: FAIL at collection — `ImportError: cannot import name 'channel_catalogue'`.

- [ ] **Step 3: Implement the catalogue**

In `src/yt_shorts/youtube.py`, extend the module docstring with a paragraph after the existing one:

```python
"""...existing docstring...

`channel_catalogue` composes three reads - the Streams tab, the channel's
playlist list, and every playlist's members - into one answer, so the studio
can group a long stream list by playlist. Measured on ERF (2026-08-04, one
channel on one day, not a guarantee): 91 streams, 17 playlists, 99 distinct
videos, all 17 member fetches in 2.5s at six threads against 20s sequential.
The eight videos in a playlist but NOT in the Streams tab include two
multi-hour broadcasts, so the union is what makes them reachable at all.

Concurrency lives here and nowhere else in this module: each worker calls the
same injected `runner`, so the whole thing still tests without a network.
"""
```

Add the imports and dataclasses:

```python
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# A YouTube playlist id, as it goes into a URL we hand to a subprocess.
# Validated rather than trusted: an id carrying '&' would append query
# parameters of its own to that URL.
_PLAYLIST_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")


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
```

Add the three functions after `list_streams`:

```python
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
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        playlists.append(Playlist(id=entry["id"],
                                  title=entry.get("title") or ""))
    return playlists


def list_playlist_videos(playlist_id: str,
                         *, runner=_default_runner) -> PlaylistContents:
    """One playlist's videos, in playlist order.

    An entry with no title is a deleted or private video: it is dropped
    (nothing can ever be transcribed from it) and COUNTED, so a size shown
    to an operator is never quietly smaller than the playlist itself.

    `playlist_ids` is returned EMPTY on every video: the catalogue is the
    one owner of that field, and half-filling it here would give it two.
    """
    if not _PLAYLIST_ID.match(playlist_id or ""):
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

    videos: list[Video] = []
    unavailable = 0
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if not entry.get("title"):
            unavailable += 1
            continue
        videos.append(Video(
            video_id=entry["id"],
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
        failed.append(FailedPlaylist(
            title="the channel's playlist list", reason=str(error)))
        return Catalogue(videos=list(videos.values()), playlists=[],
                         failed_playlists=failed)

    kept: list[Playlist] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(list_playlist_videos, playlist.id, runner=runner)
                   for playlist in playlists]
        for playlist, future in zip(playlists, futures):
            try:
                contents = future.result()
            except Exception as error:
                failed.append(FailedPlaylist(
                    title=playlist.title or playlist.id, reason=str(error)))
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_youtube.py -q`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/youtube.py tests/test_youtube.py
git commit -m "feat(youtube): a channel catalogue with playlist membership

Measured on ERF: 91 streams, 17 playlists, 99 distinct videos. Eight
videos live in a playlist and not in the Streams tab, two of them
multi-hour broadcasts that were unreachable from the studio.

Per-playlist failure is tolerated and REPORTED; the Streams tab's own
failure still raises, because without it there is no list at all."
```

---

### Task 2: `detect.py` answers "what already exists"

**Files:**
- Modify: `src/yt_shorts/detect.py`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `detect.ANALYSIS_FILENAME`, `pathnames.validate_segment` (already imported there as `validate_segment`).
- Produces: `TRANSCRIPT_FILENAME`, `stream_dir(video_id, workspace_dir) -> Path`, `has_cached_transcript(video_id, workspace_dir) -> bool`, `has_analysis(video_id, workspace_dir) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_detect.py`:

```python
class TestWhatAStreamAlreadyHas:
    """The two questions the studio's stream list asks per row, and the
    worker asks about a queued detect. One `Path.exists` each, so the list
    route can ask them for 99 videos on every response."""

    def test_nothing_exists_for_an_untouched_stream(self, tmp_path):
        assert detect.has_cached_transcript("vid123", tmp_path) is False
        assert detect.has_analysis("vid123", tmp_path) is False

    def test_a_written_transcript_is_seen(self, tmp_path):
        directory = tmp_path / "streams" / "vid123"
        directory.mkdir(parents=True)
        (directory / "transcript.json").write_text("{}", encoding="utf-8")
        assert detect.has_cached_transcript("vid123", tmp_path) is True
        assert detect.has_analysis("vid123", tmp_path) is False

    def test_a_written_analysis_is_seen(self, tmp_path):
        directory = tmp_path / "streams" / "vid123"
        directory.mkdir(parents=True)
        (directory / detect.ANALYSIS_FILENAME).write_text("{}", encoding="utf-8")
        assert detect.has_analysis("vid123", tmp_path) is True

    def test_a_directory_in_place_of_the_file_is_not_a_transcript(self, tmp_path):
        """`is_file`, not `exists`: a directory named transcript.json is not
        a transcript, and reporting it as one would offer to detect on it."""
        (tmp_path / "streams" / "vid123" / "transcript.json").mkdir(parents=True)
        assert detect.has_cached_transcript("vid123", tmp_path) is False

    def test_an_unsafe_video_id_answers_false_rather_than_raising(self, tmp_path):
        """These two are asked once per video over a whole catalogue, and
        the ids come from yt-dlp. An id that is not a safe segment names no
        file this workspace could hold - answering False is right, and one
        odd id must not 500 a list of 99 videos. `stream_dir` itself still
        raises, which is what `require_cached_transcript` needs."""
        assert detect.has_cached_transcript("../etc", tmp_path) is False
        assert detect.has_analysis("../etc", tmp_path) is False
        with pytest.raises(ValueError):
            detect.stream_dir("../etc", tmp_path)

    def test_stream_dir_is_where_the_stream_lives(self, tmp_path):
        assert detect.stream_dir("vid123", tmp_path) == \
            tmp_path / "streams" / "vid123"
```

Check the top of `tests/test_detect.py` for how it imports; if it does not already, add `from yt_shorts import detect` and `import pytest`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_detect.py::TestWhatAStreamAlreadyHas -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.detect' has no attribute 'has_cached_transcript'`.

- [ ] **Step 3: Implement the helpers**

In `src/yt_shorts/detect.py`, beside `ANALYSIS_FILENAME`:

```python
TRANSCRIPT_FILENAME = "transcript.json"


def stream_dir(video_id: str, workspace_dir) -> Path:
    """Where a stream's derived data lives. Raises ValueError for an id
    that is not one safe path segment - the same guard every other write
    path in this project applies before touching the filesystem."""
    validate_segment(video_id, what="video id")
    return Path(workspace_dir) / "streams" / video_id


def has_cached_transcript(video_id: str, workspace_dir) -> bool:
    """Whether a `transcribe` job has already produced this stream's
    transcript. `is_file`, not `exists`: a directory of that name is not a
    transcript. An id that is not a safe segment answers False rather than
    raising - this is asked once per video over a whole catalogue, and one
    odd id must not sink a list of 99."""
    return _has(video_id, workspace_dir, TRANSCRIPT_FILENAME)


def has_analysis(video_id: str, workspace_dir) -> bool:
    """Whether a `detect` job has already written this stream's moments.
    Same tolerance as has_cached_transcript, for the same reason."""
    return _has(video_id, workspace_dir, ANALYSIS_FILENAME)


def _has(video_id: str, workspace_dir, filename: str) -> bool:
    try:
        directory = stream_dir(video_id, workspace_dir)
    except ValueError:
        return False  # names no file this workspace could hold
    return (directory / filename).is_file()
```

Then change `require_cached_transcript`'s path line from

```python
    validate_segment(video_id, what="video id")
    path = Path(workspace_dir) / "streams" / video_id / "transcript.json"
```

to

```python
    path = stream_dir(video_id, workspace_dir) / TRANSCRIPT_FILENAME
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_detect.py -q`
Expected: PASS — including the existing `require_cached_transcript` tests, which must be unaffected by the refactor.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/detect.py tests/test_detect.py
git commit -m "feat(detect): ask what a stream already has, cheaply

Two Path.is_file checks, so the studio's stream list can carry them per
row for a whole catalogue. An unsafe id answers False rather than
raising: one odd id must not 500 a list of 99 videos - stream_dir still
raises, which is what require_cached_transcript needs."
```

---

### Task 3: The streams route serves the catalogue

**Files:**
- Modify: `src/yt_shorts/studio/api.py:1874-1935`
- Test: `tests/test_studio_api.py:524-574` (`TestStreamsRoute`)

**Interfaces:**
- Consumes: `youtube.channel_catalogue`, `detect.has_cached_transcript`, `detect.has_analysis`, the existing `_resolve_workspace`.
- Produces: `GET …/streams` answering `{videos: [...], playlists: [...], failed_playlists: [...]}`.

- [ ] **Step 1: Write the failing tests**

Replace the body of `class TestStreamsRoute` in `tests/test_studio_api.py` with:

```python
class TestStreamsRoute:
    def _catalogue(self, **overrides):
        from yt_shorts.youtube import Catalogue, Playlist, Video
        base = {
            "videos": [Video("aaa", "Race Part 1", 29975, 2200, ["PLaaa"]),
                       Video("bbb", "Special", 8983, 400, [])],
            "playlists": [Playlist("PLaaa", "2026 Season", 1, 2)],
            "failed_playlists": [],
        }
        base.update(overrides)
        return Catalogue(**base)

    def test_videos_carry_their_playlists_and_what_exists(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "channel_catalogue",
                            lambda url, **k: self._catalogue())
        r = client.get(f"{EVENT_PREFIX}/streams")
        assert r.status_code == 200
        assert r.json()["videos"][0] == {
            "video_id": "aaa", "title": "Race Part 1",
            "duration_seconds": 29975, "view_count": 2200,
            "playlist_ids": ["PLaaa"],
            "has_transcript": False, "has_analysis": False}

    def test_playlists_carry_their_size_and_what_is_unavailable(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "channel_catalogue",
                            lambda url, **k: self._catalogue())
        r = client.get(f"{EVENT_PREFIX}/streams")
        assert r.json()["playlists"] == [
            {"id": "PLaaa", "title": "2026 Season", "count": 1, "unavailable": 2}]

    def test_a_failed_playlist_is_reported_rather_than_hidden(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        from yt_shorts.youtube import FailedPlaylist
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: self._catalogue(
                failed_playlists=[FailedPlaylist("Bathurst", "HTTP 404")]))
        r = client.get(f"{EVENT_PREFIX}/streams")
        assert r.json()["failed_playlists"] == [
            {"title": "Bathurst", "reason": "HTTP 404"}]

    def test_what_exists_is_read_fresh_not_cached_with_the_yt_dlp_answer(
            self, client, monkeypatch, workspace_root):
        """The expensive half (yt-dlp) is cached for the session; these two
        flags are not. Caching them would leave the list saying "no
        transcript" after a transcription finished, until someone pressed
        refresh - and the whole point of the marker is to be true."""
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "channel_catalogue",
                            lambda url, **k: self._catalogue())
        first = client.get(f"{EVENT_PREFIX}/streams")
        assert first.json()["videos"][0]["has_transcript"] is False

        directory = workspace_root / "streams" / "aaa"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "transcript.json").write_text("{}", encoding="utf-8")

        second = client.get(f"{EVENT_PREFIX}/streams")   # NO refresh
        assert second.json()["videos"][0]["has_transcript"] is True

    def test_the_catalogue_is_cached_within_a_session(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        calls = {"n": 0}

        def fake(url, **k):
            calls["n"] += 1
            return self._catalogue()

        monkeypatch.setattr(api, "channel_catalogue", fake)
        client.get(f"{EVENT_PREFIX}/streams")
        client.get(f"{EVENT_PREFIX}/streams")
        assert calls["n"] == 1

    def test_a_refresh_re_fetches(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        calls = {"n": 0}

        def fake(url, **k):
            calls["n"] += 1
            return self._catalogue()

        monkeypatch.setattr(api, "channel_catalogue", fake)
        client.get(f"{EVENT_PREFIX}/streams")
        client.get(f"{EVENT_PREFIX}/streams?refresh=true")
        assert calls["n"] == 2

    def test_a_youtube_error_is_a_502_with_a_message(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        from yt_shorts.youtube import YouTubeError

        def fail(url, **k):
            raise YouTubeError("yt-dlp is not installed")

        monkeypatch.setattr(api, "channel_catalogue", fail)
        r = client.get(f"{EVENT_PREFIX}/streams")
        assert r.status_code == 502
        assert "yt-dlp" in r.json()["detail"]
```

`workspace_root` is the fixture the existing detect-route tests in this file already use (see `test_a_stopped_detect_writes_no_analysis`, which asserts on `workspace_root / "streams" / video_id / "moments.json"`). Reuse it; do not add a second one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py::TestStreamsRoute -q`
Expected: FAIL — `AttributeError: <module 'yt_shorts.studio.api'> does not have the attribute 'channel_catalogue'`.

- [ ] **Step 3: Rewrite the route**

In `src/yt_shorts/studio/api.py`, change the import at line 171 from

```python
from ..youtube import YouTubeError, list_streams
```

to

```python
from ..youtube import YouTubeError, channel_catalogue
from ..detect import has_analysis, has_cached_transcript
```

**These two `detect` functions are imported by NAME on purpose.** A module import bound as `detect` would be shadowed by the local variable `detect` this file already uses at line ~1602 (the brand's detect section), which is the kind of silent capture that is very hard to see in review. They are plain functions, never patched by a test and never rebuilt by `importlib.reload`, so none of the from-import hazards `worker.py` documents apply.

Replace lines 1874-1894 with:

```python
    # ---- Streams: the channel's catalogue, cached per channel ----
    # Cache lives in this closure so it dies with the app - the same
    # in-memory, single-session limit the render job store has. Keyed by
    # channel so a multi-channel session does not serve one channel's
    # catalogue for another. What is cached is the EXPENSIVE half only: the
    # yt-dlp reads. `has_transcript`/`has_analysis` are stat'd fresh on
    # every response, because a cached "no transcript" would survive a
    # finished transcription until someone pressed refresh - and a marker
    # that is not true is worse than no marker.
    streams_cache: dict[str, Catalogue] = {}

    @app.get(EV + "/streams")
    def get_streams(channel: str, event: str, refresh: bool = False) -> dict:
        profile = _load_profile(channel, event)
        if channel not in streams_cache or refresh:
            try:
                streams_cache[channel] = channel_catalogue(
                    profile.channel["channel_url"])
            except YouTubeError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
        catalogue = streams_cache[channel]
        root = _resolve_workspace().root
        return {
            "videos": [
                {"video_id": video.video_id, "title": video.title,
                 "duration_seconds": video.duration_seconds,
                 "view_count": video.view_count,
                 "playlist_ids": list(video.playlist_ids),
                 "has_transcript": has_cached_transcript(video.video_id, root),
                 "has_analysis": has_analysis(video.video_id, root)}
                for video in catalogue.videos],
            "playlists": [
                {"id": playlist.id, "title": playlist.title,
                 "count": playlist.count, "unavailable": playlist.unavailable}
                for playlist in catalogue.playlists],
            "failed_playlists": [
                {"title": failure.title, "reason": failure.reason}
                for failure in catalogue.failed_playlists],
        }
```

Add `Catalogue` to the youtube import line so the annotation resolves:

```python
from ..youtube import Catalogue, YouTubeError, channel_catalogue
```

In `post_detect` (line ~1916), the title lookup reads the cache, which now holds a `Catalogue` rather than a list of dicts. Change

```python
        for stream in streams_cache.get(channel, []):
            if stream["video_id"] == video_id:
                title = stream["title"]
                break
```

to

```python
        catalogue = streams_cache.get(channel)
        if catalogue is not None:
            for video in catalogue.videos:
                if video.video_id == video_id:
                    title = video.title
                    break
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q`
Expected: PASS. The detect-route tests exercise `post_detect`'s title lookup, so a broken conversion shows up here.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "feat(studio-api): serve the catalogue, with what each stream has

One route, richer payload: playlist membership per video, sizes per
playlist, failed playlists named rather than hidden.

has_transcript/has_analysis are stat'd FRESH on every response while the
yt-dlp reads stay cached - a cached 'no transcript' would survive a
finished transcription until someone pressed refresh."
```

---

### Task 4: The client's types follow the payload

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts:715-746`
- Modify: `src/yt_shorts/studio/web/src/components/StreamScreen.tsx:107-115`

**Interfaces:**
- Consumes: the payload from Task 3.
- Produces: `StreamVideo`, `StreamPlaylist`, `FailedPlaylist`, `StreamCatalogue`, `listStreams(refresh?) => Promise<StreamCatalogue>`, `getStreams(channel, event) => Promise<StreamCatalogue>`.

- [ ] **Step 1: Replace the types and both callers**

In `api.ts`, replace the `Stream` interface and both functions (lines 715-746) with:

```ts
/** GET /api/streams (see api.py) - a channel's CATALOGUE: the Streams tab
 * and every playlist's members, unioned. Cached server-side for the
 * session; pass `refresh: true` to force a re-fetch of the yt-dlp half.
 *
 * The list is the union, so it holds videos the Streams tab does not -
 * measured on ERF: 8 of them, two multi-hour broadcasts that no screen in
 * this studio could reach before. */
export interface StreamVideo {
  video_id: string
  title: string
  duration_seconds: number | null
  view_count: number | null
  /** Every playlist this video sits in. A LIST: a video may be in several,
   * even though none is on ERF today. */
  playlist_ids: string[]
  /** Whether a transcribe job has already produced this stream's
   * transcript, and a detect job its moments. Read fresh by the server on
   * every response - never cached with the yt-dlp answer - so a marker
   * cannot outlive the fact it reports. */
  has_transcript: boolean
  has_analysis: boolean
}

export interface StreamPlaylist {
  id: string
  title: string
  /** Usable videos in it. */
  count: number
  /** Entries dropped for being deleted or private. Carried so a "(6)" is
   * never silently a 6 that came from 8. */
  unavailable: number
}

/** A playlist whose fetch failed. Rendered, never swallowed: a half
 * catalogue that looks whole is this project's recurring failure mode. */
export interface FailedPlaylist {
  title: string
  reason: string
}

export interface StreamCatalogue {
  videos: StreamVideo[]
  playlists: StreamPlaylist[]
  failed_playlists: FailedPlaylist[]
}

export function listStreams(refresh = false): Promise<StreamCatalogue> {
  return fetch(`${eventScope()}/streams${refresh ? '?refresh=true' : ''}`)
    .then(asJson<StreamCatalogue>)
}

/** GET .../streams for an EXPLICIT channel/event, unlike listStreams above.
 * The stream screen mounts as a sibling of App (see getStreamAnalysis's own
 * comment on why channel/event are parameters there) - the module scope
 * listStreams relies on is only ever set by App, so a scoped call here would
 * throw on mount instead of fetching. Used only to look up a stream's real
 * title for the heading (see StreamScreen): the analysis's cached
 * `stream_title` is often empty ("" - a cold cache, see api.py's post_detect),
 * and this is the same catalogue the event's Streams tab already shows - which
 * since the playlist change also holds streams the Streams tab itself omits,
 * so a title is found for those too. Not cached client-side; the server's own
 * per-channel cache absorbs repeat calls within a session. Can be slow on a
 * cold cache (several yt-dlp calls) - callers must not await this before
 * rendering. */
export function getStreams(channel: string, event: string): Promise<StreamCatalogue> {
  return fetch(`${eventBase(channel, event)}/streams`).then(asJson<StreamCatalogue>)
}
```

In `StreamScreen.tsx`, change the title lookup at lines 107-111 from

```ts
    getStreams(channel, event)
      .then((streams) => {
        const found = streams.find((stream) => stream.video_id === videoId)
        if (found) setStreamListTitle(found.title)
      })
```

to

```ts
    getStreams(channel, event)
      .then((catalogue) => {
        const found = catalogue.videos.find((video) => video.video_id === videoId)
        if (found) setStreamListTitle(found.title)
      })
```

- [ ] **Step 2: Run the type-check to see what else must move**

Run: `cd src/yt_shorts/studio/web && npm run build`
Expected: FAIL, with errors in `components/StreamPanel.tsx` — it still expects `Stream[]`. That is Task 8's job; this step exists to see the whole blast radius at once.

- [ ] **Step 3: Keep StreamPanel compiling with the new shape**

In `StreamPanel.tsx`, change the import and the single fetch so the file compiles unchanged otherwise — the filter and selection arrive in Tasks 8 and 9:

```ts
import { ApiError, listStreams, type StreamVideo } from '../api'
```

```ts
  const [streams, setStreams] = useState<StreamVideo[] | null>(null)
```

```ts
      const catalogue = await listStreams(refresh)
      setStreams(catalogue.videos)
```

- [ ] **Step 4: Build to verify it type-checks**

Run: `cd src/yt_shorts/studio/web && npm run build`
Expected: PASS, no TypeScript errors.

- [ ] **Step 5: Commit, with the built output**

```bash
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "refactor(studio-web): the client reads a catalogue, not a list

Types follow the route. StreamPanel still shows the flat list - the
filter and the selection land in later tasks - but it now shows the
UNION, so the streams that live only in a playlist are visible."
```

---

### Task 5: `streams.ts`, the list's pure rules

**Files:**
- Create: `src/yt_shorts/studio/web/src/streams.ts`
- Create: `src/yt_shorts/studio/web/src/streams.test.ts`

**Interfaces:**
- Consumes: `StreamVideo`, `StreamCatalogue` from `api.ts`.
- Produces: `ALL_STREAMS`, `NO_PLAYLIST`, `PlaylistOption`, `playlistOptions(catalogue)`, `visibleVideos(catalogue, playlistId)`, `BulkAction`, `BulkPlan`, `BulkStep`, `bulkPlan(selectedIds, videos, action, force)`, `selectionNote(selectedIds, visibleIds)`.

- [ ] **Step 1: Write the failing tests**

Create `src/yt_shorts/studio/web/src/streams.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import type { StreamCatalogue, StreamVideo } from './api'
import {
  ALL_STREAMS, NO_PLAYLIST, bulkPlan, playlistOptions, selectionNote,
  visibleVideos,
} from './streams'

function video(overrides: Partial<StreamVideo> = {}): StreamVideo {
  return {
    video_id: 'v1',
    title: 'Race Part 1',
    duration_seconds: 29975,
    view_count: 2200,
    playlist_ids: [],
    has_transcript: false,
    has_analysis: false,
    ...overrides,
  }
}

function catalogue(overrides: Partial<StreamCatalogue> = {}): StreamCatalogue {
  return {
    videos: [
      video({ video_id: 'a', playlist_ids: ['PL1'] }),
      video({ video_id: 'b', playlist_ids: ['PL1'] }),
      video({ video_id: 'c', playlist_ids: [] }),
    ],
    playlists: [{ id: 'PL1', title: '2026 Season', count: 2, unavailable: 0 }],
    failed_playlists: [],
    ...overrides,
  }
}

describe('playlistOptions', () => {
  it('counts "all" from the union, not from any one playlist', () => {
    // The whole reason playlist contents are shown rather than the Streams
    // tab filtered: the union is bigger. A count that disagreed with the
    // list it labels would hide exactly that.
    const [all] = playlistOptions(catalogue())
    expect(all).toEqual({ value: ALL_STREAMS, label: 'All streams', count: 3 })
  })

  it('offers each playlist with its own size', () => {
    const options = playlistOptions(catalogue())
    expect(options[1]).toEqual({ value: 'PL1', label: '2026 Season', count: 2 })
  })

  it('offers the leftovers bucket when something is in no playlist', () => {
    const options = playlistOptions(catalogue())
    expect(options[options.length - 1]).toEqual({
      value: NO_PLAYLIST, label: 'In no playlist', count: 1,
    })
  })

  it('omits the leftovers bucket entirely when it would be empty', () => {
    // Measured on ERF: every one of its 91 streams is in a playlist, so
    // this is the ordinary case. An always-present empty row reads as a
    // fault in the catalogue.
    const every = catalogue({
      videos: [video({ video_id: 'a', playlist_ids: ['PL1'] })],
    })
    expect(playlistOptions(every).map((o) => o.value)).toEqual([ALL_STREAMS, 'PL1'])
  })
})

describe('visibleVideos', () => {
  it('shows everything for the "all" selection', () => {
    expect(visibleVideos(catalogue(), ALL_STREAMS).map((v) => v.video_id))
      .toEqual(['a', 'b', 'c'])
  })

  it('shows a playlist\'s own members', () => {
    expect(visibleVideos(catalogue(), 'PL1').map((v) => v.video_id))
      .toEqual(['a', 'b'])
  })

  it('shows what is in no playlist at all', () => {
    expect(visibleVideos(catalogue(), NO_PLAYLIST).map((v) => v.video_id))
      .toEqual(['c'])
  })
})

describe('bulkPlan', () => {
  const videos = [
    video({ video_id: 'a' }),
    video({ video_id: 'b', has_transcript: true }),
    video({ video_id: 'c', has_transcript: true, has_analysis: true }),
  ]
  const nothingForced = { transcribe: false, detect: false }

  it('follows list order, not the order rows were ticked', () => {
    const plan = bulkPlan(['c', 'a'], videos, 'transcribe', { transcribe: true, detect: false })
    expect(plan.steps.map((s) => s.videoId)).toEqual(['a', 'c'])
  })

  it('skips a stream that already has a transcript', () => {
    const plan = bulkPlan(['a', 'b'], videos, 'transcribe', nothingForced)
    expect(plan.steps).toEqual([{ videoId: 'a', transcribe: true, detect: false }])
    expect(plan.skippedTranscribe).toEqual(['b'])
  })

  it('re-transcribes when the operator forces it', () => {
    const plan = bulkPlan(['a', 'b'], videos, 'transcribe', { transcribe: true, detect: false })
    expect(plan.steps.map((s) => s.videoId)).toEqual(['a', 'b'])
    expect(plan.skippedTranscribe).toEqual([])
  })

  it('skips a stream that already has an analysis', () => {
    // Symmetric with the transcript rule, and not out of tidiness: a
    // re-detection spends real money at the provider, so thirteen of them
    // must not be the default reading of one click.
    const plan = bulkPlan(['a', 'c'], videos, 'detect', nothingForced)
    expect(plan.steps.map((s) => s.videoId)).toEqual(['a'])
    expect(plan.skippedDetect).toEqual(['c'])
  })

  it('chains both, so the detect can name its transcribe', () => {
    const plan = bulkPlan(['a'], videos, 'both', nothingForced)
    expect(plan.steps).toEqual([{ videoId: 'a', transcribe: true, detect: true }])
  })

  it('queues the detect alone when the transcript is already there', () => {
    // Nothing to wait for: the caller enqueues this one with no `after`.
    const plan = bulkPlan(['b'], videos, 'both', nothingForced)
    expect(plan.steps).toEqual([{ videoId: 'b', transcribe: false, detect: true }])
  })

  it('contributes nothing for a stream that has both already', () => {
    const plan = bulkPlan(['c'], videos, 'both', nothingForced)
    expect(plan.steps).toEqual([])
    expect(plan.skippedEntirely).toEqual(['c'])
  })

  it('says what will happen before the click', () => {
    // Names the LEG, never the video: under 'both' a video can have its
    // transcription skipped while its detection IS queued, so a sentence
    // about the video would contradict what actually happens.
    const plan = bulkPlan(['a', 'b'], videos, 'transcribe', nothingForced)
    expect(plan.note).toBe('1 transcription skipped: already transcribed.')
  })

  it('says nothing when nothing is skipped', () => {
    const plan = bulkPlan(['a'], videos, 'transcribe', nothingForced)
    expect(plan.note).toBeNull()
  })

  it('is empty, and says so, when everything is skipped', () => {
    // The bar disables the button on this and shows the reason. A click
    // that silently does nothing is the same lying control as a spinner
    // that never moves.
    const plan = bulkPlan(['b'], videos, 'transcribe', nothingForced)
    expect(plan.steps).toEqual([])
    expect(plan.note).toBe('1 transcription skipped: already transcribed.')
  })

  it('ignores a selected id the catalogue no longer holds', () => {
    // A refresh can drop a video while its row is ticked.
    const plan = bulkPlan(['a', 'ghost'], videos, 'transcribe', nothingForced)
    expect(plan.steps.map((s) => s.videoId)).toEqual(['a'])
  })
})

describe('selectionNote', () => {
  it('is silent when everything selected is on screen', () => {
    expect(selectionNote(['a', 'b'], ['a', 'b', 'c'])).toBeNull()
  })

  it('says how many are selected outside this view', () => {
    // Selection deliberately survives a filter change - a race weekend
    // split across two playlists is an ordinary case - so the count has to
    // say when it reaches past what is on screen.
    expect(selectionNote(['a', 'x', 'y'], ['a', 'b'])).toBe('2 not in this view')
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd src/yt_shorts/studio/web && npm test -- streams.test.ts`
Expected: FAIL — cannot resolve `./streams`.

- [ ] **Step 3: Implement the module**

Create `src/yt_shorts/studio/web/src/streams.ts`:

```ts
/**
 * What the stream list shows, and what a bulk action will actually do.
 *
 * Pure, and NOT exported from a component file, for the reason every other
 * module beside it exists (see words.ts, jobs.ts, streamTimeline.ts): Vite's
 * fast-refresh boundary stays component-only and these rules are unit-tested
 * without rendering anything.
 *
 * `bulkPlan` is the one that matters. It decides what gets queued for a
 * selection of streams, and each of the two things it skips by default costs
 * something real if it does not: a re-transcription DOWNLOADS THE STREAM'S
 * AUDIO AGAIN before it can even count its chunks (gigabytes for an 8-hour
 * race, even though every chunk then comes from the cache), and a
 * re-detection spends money at the model provider. Neither is a good reading
 * of one click over thirteen ticked rows - so both are skipped unless the
 * operator says otherwise, and the bar says so BEFORE the click.
 */
import type { StreamCatalogue, StreamVideo } from './api'

/** The filter's two synthetic selections. Neither can collide with a real
 * playlist id, which is letters, digits, '-' and '_' only. */
export const ALL_STREAMS = '*all*'
export const NO_PLAYLIST = '*none*'

export interface PlaylistOption {
  value: string
  label: string
  count: number
}

/**
 * The filter's rows: everything, then each playlist, then the leftovers.
 *
 * "All streams" counts the UNION the catalogue holds, not the Streams tab's
 * own total - that difference (99 against 91 on ERF) is the entire reason
 * playlist contents are shown rather than the Streams tab filtered, and a
 * count that disagreed with its own list would hide it.
 *
 * The leftovers row is omitted when it would be empty rather than shown as
 * "(0)": on ERF every stream is in a playlist, so an always-present empty
 * row would be the ordinary case and would read as a fault.
 */
export function playlistOptions(catalogue: StreamCatalogue): PlaylistOption[] {
  const options: PlaylistOption[] = [
    { value: ALL_STREAMS, label: 'All streams', count: catalogue.videos.length },
  ]
  for (const playlist of catalogue.playlists) {
    options.push({ value: playlist.id, label: playlist.title, count: playlist.count })
  }
  const loose = catalogue.videos.filter((video) => video.playlist_ids.length === 0).length
  if (loose > 0) {
    options.push({ value: NO_PLAYLIST, label: 'In no playlist', count: loose })
  }
  return options
}

/** The videos the current filter shows, in catalogue order. */
export function visibleVideos(
  catalogue: StreamCatalogue, playlistId: string,
): StreamVideo[] {
  if (playlistId === ALL_STREAMS) return catalogue.videos
  if (playlistId === NO_PLAYLIST) {
    return catalogue.videos.filter((video) => video.playlist_ids.length === 0)
  }
  return catalogue.videos.filter((video) => video.playlist_ids.includes(playlistId))
}

export type BulkAction = 'transcribe' | 'detect' | 'both'

/** One video's share of a bulk action. `transcribe` and `detect` are what
 * will actually be enqueued for it - a step with `transcribe: false,
 * detect: true` is a stream whose transcript already exists, so its detect
 * has nothing to wait for and is enqueued with no `after`. */
export interface BulkStep {
  videoId: string
  transcribe: boolean
  detect: boolean
}

export interface BulkPlan {
  steps: BulkStep[]
  skippedTranscribe: string[]
  skippedDetect: string[]
  /** Videos this action would do nothing at all for. */
  skippedEntirely: string[]
  /** The sentence the bar shows before the click, or null when nothing is
   * skipped. */
  note: string | null
}

/**
 * What a bulk action will really queue.
 *
 * Steps follow CATALOGUE order, not the order rows were ticked: the plan an
 * operator reads on the Jobs screen should match the list they were looking
 * at. The caller enqueues them in this order, sequentially - parallel
 * requests would scramble the queue's own order.
 */
export function bulkPlan(
  selectedIds: string[],
  videos: StreamVideo[],
  action: BulkAction,
  force: { transcribe: boolean; detect: boolean },
): BulkPlan {
  const selected = new Set(selectedIds)
  const steps: BulkStep[] = []
  const skippedTranscribe: string[] = []
  const skippedDetect: string[] = []
  const skippedEntirely: string[] = []

  for (const video of videos) {
    if (!selected.has(video.video_id)) continue
    const wantsTranscribe = action === 'transcribe' || action === 'both'
    const wantsDetect = action === 'detect' || action === 'both'
    let transcribe = false
    let detect = false

    if (wantsTranscribe) {
      if (video.has_transcript && !force.transcribe) skippedTranscribe.push(video.video_id)
      else transcribe = true
    }
    if (wantsDetect) {
      if (video.has_analysis && !force.detect) skippedDetect.push(video.video_id)
      else detect = true
    }

    if (transcribe || detect) steps.push({ videoId: video.video_id, transcribe, detect })
    else skippedEntirely.push(video.video_id)
  }

  // The sentence names the LEG that was skipped, never the video, and that
  // is not a wording preference: under 'both' a video whose transcript
  // already exists has its transcription skipped while its DETECTION is
  // queued for it. "1 already has a transcript and will be skipped" - the
  // first version of this - said the video was skipped while a paid job was
  // being queued for it, which is exactly the note-disagrees-with-the-action
  // case this function's own docstring warns about.
  const parts: string[] = []
  if (skippedTranscribe.length > 0) {
    parts.push(`${skippedTranscribe.length} ` +
      `${skippedTranscribe.length === 1 ? 'transcription' : 'transcriptions'} ` +
      `skipped: already transcribed.`)
  }
  if (skippedDetect.length > 0) {
    parts.push(`${skippedDetect.length} ` +
      `${skippedDetect.length === 1 ? 'detection' : 'detections'} ` +
      `skipped: already analysed.`)
  }
  return {
    steps,
    skippedTranscribe,
    skippedDetect,
    skippedEntirely,
    note: parts.length > 0 ? parts.join(' ') : null,
  }
}

/**
 * How much of the selection is off screen, or null when none of it is.
 *
 * Selection is by video id and deliberately SURVIVES a filter change - a
 * race weekend split across two playlists is an ordinary case - so the bar
 * has to say when the count it shows reaches past what is visible. Without
 * this, "6 selected" over a view holding four rows reads as a bug.
 */
export function selectionNote(
  selectedIds: string[], visibleIds: string[],
): string | null {
  const visible = new Set(visibleIds)
  const hidden = selectedIds.filter((id) => !visible.has(id)).length
  return hidden > 0 ? `${hidden} not in this view` : null
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd src/yt_shorts/studio/web && npm test -- streams.test.ts`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/streams.ts src/yt_shorts/studio/web/src/streams.test.ts
git commit -m "feat(studio-web): the stream list's pure rules

playlistOptions, visibleVideos and bulkPlan, unit-tested without
rendering. bulkPlan is the one that matters: it skips a re-transcription
(which re-downloads the audio before it can count chunks) and a
re-detection (which spends money) unless the operator forces them, and
says so before the click."
```

---

### Task 6: `jobs.ts` — the dependency wait, and one notice per batch

**Files:**
- Modify: `src/yt_shorts/studio/web/src/jobs.ts:564-591` (`waitNote`), and append `batchNotice`
- Test: `src/yt_shorts/studio/web/src/jobs.test.ts`

**Interfaces:**
- Consumes: the existing `activity`, `findEntry`, `stateColor`, `endedNotice`, `EndedNotice`.
- Produces: `waitNote`'s new `after` branch; `batchNotice(what, kind, outcomes) => EndedNotice`.

- [ ] **Step 1: Write the failing tests**

Append to `src/yt_shorts/studio/web/src/jobs.test.ts` (reuse the `entry()`/`plan()` builders already in that file; if they are named differently there, use the file's own):

```ts
describe('waitNote, for an entry that depends on another', () => {
  it('names the dependency instead of promising a free slot', () => {
    // The gap this closes was UNREACHABLE until the Streams tab began
    // chaining a detect behind its transcription: no browser call site
    // sent `after` at all (JobsScreen only displayed it). For a detect
    // waiting on a RUNNING transcription, `ahead` is 0, so this used to
    // say "It is next in line, and starts as soon as the worker has a free
    // slot" - and a free slot is exactly what does not start it.
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'running' })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1' })
    const note = waitNote(plan({ running: [dependency], queued: [dependent] }), dependent)
    expect(note).toContain('transcribe')
    expect(note).not.toContain('next in line')
  })

  it('waits on a dependency that has not started either', () => {
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'queued', position: 0 })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1', position: 1 })
    const note = waitNote(plan({ queued: [dependency, dependent] }), dependent)
    expect(note).toContain('transcribe')
  })

  it('says nothing about a dependency that is done', () => {
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'done' })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1' })
    const note = waitNote(plan({ finished: [dependency], queued: [dependent] }), dependent)
    expect(note).toBe('It is next in line, and starts as soon as the worker has a free slot.')
  })

  it('says nothing about a dependency the plan no longer holds', () => {
    // `_trim_finished` ages a long-since-done dependency out of the plan,
    // and `job_queue._dependency_status` treats an absent one as
    // SATISFIED. Saying it was still waiting would contradict the queue.
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'gone' })
    const note = waitNote(plan({ queued: [dependent] }), dependent)
    expect(note).toBe('It is next in line, and starts as soon as the worker has a free slot.')
  })

  it('still puts a stopped worker first', () => {
    // A dependency is temporary; a stopped worker is a dead end, and it is
    // the more useful thing to be told.
    const dependency = entry({ id: 'tr1', kind: 'transcribe', state: 'running' })
    const dependent = entry({ id: 'de1', kind: 'detect', state: 'queued', after: 'tr1' })
    const note = waitNote(
      plan({ running: [dependency], queued: [dependent], worker_running: false }), dependent)
    expect(note).toContain('worker is not running')
  })
})

describe('batchNotice', () => {
  it('says exactly what endedNotice says for a batch of one', () => {
    // A single-row action IS a batch of one, so there is one code path and
    // not two. If these two ever disagree, the same entry gets two
    // different stories depending on which button queued it.
    const outcomes = [{ outcome: 'failed', reason: 'yt-dlp exited 1' }]
    expect(batchNotice('Transcription', 'transcribe', outcomes))
      .toEqual(endedNotice('Transcription', 'transcribe', 'failed', 'yt-dlp exited 1'))
  })

  it('is green when every one of them finished', () => {
    const outcomes = [
      { outcome: 'done', reason: null }, { outcome: 'done', reason: null }]
    const notice = batchNotice('Transcriptions', 'transcribe', outcomes)
    expect(notice.color).toBe(stateColor('done'))
    expect(notice.message).toContain('2 finished')
  })

  it('is the failure colour when any of them failed', () => {
    const outcomes = [
      { outcome: 'done', reason: null }, { outcome: 'failed', reason: 'nope' }]
    const notice = batchNotice('Transcriptions', 'transcribe', outcomes)
    expect(notice.color).toBe(stateColor('failed'))
    expect(notice.message).toContain('1 failed')
    expect(notice.message).toContain('1 finished')
  })

  it('never reports a stop in the failure colour', () => {
    // The rule this queue restates most often, and the one three separate
    // components each got wrong: an operator's own "stop this" must not
    // come back looking like a crash.
    const outcomes = [
      { outcome: 'done', reason: null }, { outcome: 'stopped', reason: null }]
    const notice = batchNotice('Transcriptions', 'transcribe', outcomes)
    expect(notice.color).toBe(stateColor('stopped'))
    expect(notice.color).not.toBe(stateColor('failed'))
    expect(notice.message).toContain('1 stopped')
  })
})
```

Add `batchNotice` and `endedNotice` to the file's import from `./jobs`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd src/yt_shorts/studio/web && npm test -- jobs.test.ts`
Expected: FAIL — `batchNotice` is not exported, and the dependency cases get "next in line".

- [ ] **Step 3: Implement both**

In `jobs.ts`, inside `waitNote`, insert after the `if (entry.reason) return entry.reason` line:

```ts
  // A dependency the queue is holding this entry back for (`Entry.after`,
  // checked by job_queue._dependency_status). Below `entry.reason`, because
  // a recorded reason is the more specific fact, and below the
  // worker_running check, because a stopped worker is a dead end while this
  // is temporary.
  //
  // Without this branch the answer was actively FALSE: a dependent whose
  // dependency is RUNNING has nothing queued in front of it, so `ahead` is
  // 0 and the last line below promised it starts "as soon as the worker has
  // a free slot" - and a free slot is precisely what will not start it.
  // Unreachable from the browser until the Streams tab began chaining a
  // detect behind its transcription; JobsScreen only ever DISPLAYED `after`.
  //
  // A dependency the plan no longer holds is SATISFIED, not missing -
  // `_trim_finished` ages a long-since-done one out, and the queue treats
  // absence as met - so this stays quiet for it.
  if (entry.after) {
    const dependency = findEntry(plan, entry.after)
    if (dependency !== null && activity(dependency) !== 'terminal') {
      return `It waits for the ${dependency.kind} job it depends on to finish ` +
        `first, so a free worker slot will not start it yet.`
    }
  }
```

Append after `endedNotice`:

```ts
/**
 * One notification for a whole batch, rather than one per entry.
 *
 * A bulk action over thirteen streams would otherwise raise thirteen
 * toasts. A SINGLE-row action is a batch of one and goes through here too,
 * so there is one code path and not two - and for that case this must say
 * exactly what `endedNotice` says, which `jobs.test.ts` pins.
 *
 * The colour comes from `stateColor` via the worst outcome present, in the
 * order failed > interrupted > stopped > done. `stopped` sits BELOW
 * `interrupted` and nowhere near `failed` on purpose: a stop is what the
 * operator asked for, and reporting it in the failure colour sends them
 * looking for a cause that does not exist.
 */
export function batchNotice(
  what: string, kind: string,
  outcomes: { outcome: string; reason: string | null }[],
): EndedNotice {
  if (outcomes.length === 1) {
    const only = outcomes[0]
    if (only.outcome !== 'done') {
      return endedNotice(what, kind, only.outcome, only.reason)
    }
    return {
      title: `${what} finished`,
      message: only.reason ?? 'It is done.',
      color: stateColor('done'),
    }
  }
  const counts: Record<string, number> = {}
  for (const { outcome } of outcomes) counts[outcome] = (counts[outcome] ?? 0) + 1
  const worst = ['failed', 'interrupted', 'stopped', 'done']
    .find((state) => (counts[state] ?? 0) > 0) ?? 'done'
  const parts: string[] = []
  for (const state of ['done', 'failed', 'stopped', 'interrupted']) {
    const count = counts[state] ?? 0
    if (count > 0) parts.push(`${count} ${state === 'done' ? 'finished' : state}`)
  }
  return {
    title: `${what}: ${outcomes.length} ended`,
    message: `${parts.join(', ')}. The Jobs screen has each one.`,
    color: stateColor(worst),
  }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd src/yt_shorts/studio/web && npm test -- jobs.test.ts`
Expected: PASS, including every pre-existing case in the file.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/jobs.ts src/yt_shorts/studio/web/src/jobs.test.ts
git commit -m "fix(studio-web): waitNote no longer promises a free slot to a dependent

An entry waiting on Entry.after has nothing queued in front of it, so
waitNote said 'next in line, and starts as soon as the worker has a free
slot' - which a free slot does not do. Unreachable until the Streams tab
began chaining a detect behind its transcription.

batchNotice beside it: one notification per bulk action, delegating to
endedNotice for a batch of one so both cannot drift, and never reporting
a stop in the failure colour."
```

---

### Task 7: `useQueuedEntries`, and `useQueuedJob` on top of it

**Files:**
- Create: `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts`
- Create: `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.test.tsx`
- Modify: `src/yt_shorts/studio/web/src/hooks/useQueuedJob.ts`

**Interfaces:**
- Consumes: `listJobs`, `findEntry`, `activity`, `waitNote`, `useJobPolling`.
- Produces: `TrackedWork {entry, pending, running, outcome, waiting, error}`, `QueuedEntries {plan, byId: Record<string, TrackedWork>}`, `useQueuedEntries(ids: string[])`. `useQueuedJob(entryId)` keeps its exact existing `QueuedWork` return shape.

- [ ] **Step 1: Write the failing tests**

Create `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.test.tsx`:

```tsx
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { JobEntry, JobPlan } from '../jobs'
import { useQueuedEntries } from './useQueuedEntries'

function entry(overrides: Partial<JobEntry> = {}): JobEntry {
  return {
    id: 'e1', kind: 'transcribe', state: 'queued', params: {}, reason: null,
    progress: null, created_at: 0, after: null, job_id: null, position: 0,
    pool: 'cpu', stop_point: 'the end of the current chunk',
    hard_stop_allowed: true, stoppable: true, ...overrides,
  }
}

function plan(overrides: Partial<JobPlan> = {}): JobPlan {
  return {
    running: [], queued: [], finished: [], limits: {},
    worker_running: true, load_error: null, ...overrides,
  }
}

describe('useQueuedEntries', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('reads the plan ONCE for many entries', async () => {
    // The reason this hook exists rather than N useQueuedJob instances:
    // the plan is a single GET, and thirteen followers would fetch it
    // thirteen times a second.
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(
      plan({ queued: [entry({ id: 'a' }), entry({ id: 'b' })] }))
    renderHook(() => useQueuedEntries(['a', 'b']))
    await act(async () => { await Promise.resolve() })
    expect(listJobs).toHaveBeenCalledTimes(1)
  })

  it('follows each id separately', async () => {
    vi.spyOn(api, 'listJobs').mockResolvedValue(plan({
      running: [entry({ id: 'a', state: 'running' })],
      queued: [entry({ id: 'b' })],
    }))
    const { result } = renderHook(() => useQueuedEntries(['a', 'b']))
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.running).toBe(true)
    expect(result.current.byId.b.pending).toBe(true)
  })

  it('reports an entry that left the plan, and stops keeping it pending', async () => {
    // `allowedActions` offers `remove` on a queued entry, so this is an
    // ordinary supported flow. A panel keyed on `pending` would otherwise
    // sit disabled for the life of the screen, still claiming work was
    // queued - the same lie as a button that claims to have started
    // something, pointed the other way.
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValueOnce(plan({ queued: [entry({ id: 'a' })] }))
    listJobs.mockResolvedValue(plan({ queued: [] }))
    const { result } = renderHook(() => useQueuedEntries(['a']))
    await act(async () => { await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(result.current.byId.a.error).toContain('no longer in the plan')
    expect(result.current.byId.a.entry).toBeNull()
    expect(result.current.byId.a.pending).toBe(false)
  })

  it('does not call an entry gone before it was ever seen', async () => {
    // A poll that raced the enqueue's own write. Retried, not reported.
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValueOnce(plan({ queued: [] }))
    listJobs.mockResolvedValue(plan({ queued: [entry({ id: 'a' })] }))
    const { result } = renderHook(() => useQueuedEntries(['a']))
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.error).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(result.current.byId.a.entry?.id).toBe('a')
  })

  it('gives up after the error budget and says so, keeping the entry', async () => {
    // Unlike a removal, a failed read says nothing about whether the entry
    // is still there - so `entry` is KEPT and the sentence says as much.
    const listJobs = vi.spyOn(api, 'listJobs')
    listJobs.mockResolvedValueOnce(plan({ queued: [entry({ id: 'a' })] }))
    listJobs.mockRejectedValue(new api.ApiError(503, 'queue unavailable'))
    const { result } = renderHook(() => useQueuedEntries(['a']))
    await act(async () => { await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(result.current.byId.a.error).toContain('Lost contact')
    expect(result.current.byId.a.entry?.id).toBe('a')
    expect(result.current.byId.a.pending).toBe(false)
  })

  it('stops polling once every tracked entry is terminal', async () => {
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(plan({
      finished: [entry({ id: 'a', state: 'done' }), entry({ id: 'b', state: 'failed' })],
    }))
    renderHook(() => useQueuedEntries(['a', 'b']))
    await act(async () => { await Promise.resolve() })
    const after = listJobs.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(listJobs.mock.calls.length).toBe(after)
  })

  it('keeps following when one is terminal and another is not', async () => {
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(plan({
      finished: [entry({ id: 'a', state: 'done' })],
      queued: [entry({ id: 'b' })],
    }))
    renderHook(() => useQueuedEntries(['a', 'b']))
    await act(async () => { await Promise.resolve() })
    const after = listJobs.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(listJobs.mock.calls.length).toBeGreaterThan(after)
  })

  it('carries the queue\'s own wait reason through', async () => {
    vi.spyOn(api, 'listJobs').mockResolvedValue(plan({
      queued: [entry({ id: 'a', reason: 'waiting for the event lock on erf/x' })],
    }))
    const { result } = renderHook(() => useQueuedEntries(['a']))
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.waiting).toContain('event lock')
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd src/yt_shorts/studio/web && npm test -- useQueuedEntries.test.tsx`
Expected: FAIL — cannot resolve `./useQueuedEntries`.

- [ ] **Step 3: Implement the hook**

Create `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts`:

```ts
import { useEffect, useRef, useState } from 'react'
import { ApiError, listJobs, type JobEntry, type JobPlan } from '../api'
import { activity, findEntry, waitNote } from '../jobs'

const POLL_INTERVAL_MS = 750
const MAX_CONSECUTIVE_ERRORS = 5

/** What this hook says when a row it was following has left the plan. Not
 * a read failure - the plan WAS read, and the entry is not in it. */
const GONE =
  'This entry is no longer in the plan - it was removed on the Jobs screen, ' +
  'or the plan was rewritten. Nothing has been started for it here; the Jobs ' +
  'screen is where to see what became of it.'

/** What it says when it gave up reading the plan. The entry is deliberately
 * NOT called gone: nothing was learned about it either way. */
const LOST =
  'Lost contact with the studio while following this entry - it may well ' +
  'still be in the plan; the Jobs screen is where to check.'

/** One tracked entry. Same fields `useQueuedJob` has always exposed, minus
 * the `job` and `plan` that only a single-entry follower needs. */
export interface TrackedWork {
  entry: JobEntry | null
  pending: boolean
  running: boolean
  outcome: string | null
  waiting: string | null
  /** Why this hook STOPPED following, or null. Whenever it is set,
   * `pending` and `running` are BOTH false - a control disabled for good
   * while the panel still claims work is queued is the same lie as a
   * button that claims to have started something. */
  error: string | null
}

export interface QueuedEntries {
  plan: JobPlan | null
  byId: Record<string, TrackedWork>
}

const IDLE: TrackedWork = {
  entry: null, pending: false, running: false, outcome: null,
  waiting: null, error: null,
}

/**
 * Follows SEVERAL queue entries an operator just put in the plan.
 *
 * One `GET /api/jobs` answers for all of them - the plan is a single
 * document, and a bulk action over thirteen streams followed by thirteen
 * copies of `useQueuedJob` would fetch it thirteen times a second.
 * `useQueuedJob` is now this hook with one id, so the rules below exist
 * once: the `seen` race guard (a row absent BEFORE it was ever seen raced
 * the enqueue's own write and must be retried; one absent AFTER aged out or
 * was removed), the error budget, and the rule that a stop which is not a
 * terminal state must never leave a panel pending.
 *
 * Polling stops when EVERY tracked id has reached a terminal state or
 * stopped being followed. The caller clears the ids it passed in when it is
 * done with the results.
 */
export function useQueuedEntries(ids: string[]): QueuedEntries {
  const [plan, setPlan] = useState<JobPlan | null>(null)
  const [rows, setRows] = useState<Record<string, JobEntry | null>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const seen = useRef<Record<string, boolean>>({})
  // The effect keys on the id SET, not the array identity: a caller
  // building `ids` inline would otherwise restart the poll on every render.
  const key = ids.join(',')
  const latest = useRef(ids)
  latest.current = ids

  useEffect(() => {
    seen.current = {}
    setRows({})
    setErrors({})
    setPlan(null)
    if (latest.current.length === 0) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const settled = new Set<string>()

    async function poll() {
      try {
        const next = await listJobs()
        if (cancelled) return
        failures = 0
        setPlan(next)
        for (const id of latest.current) {
          if (settled.has(id)) continue
          const row = findEntry(next, id)
          if (row === null) {
            if (seen.current[id]) {
              settled.add(id)
              setRows((held) => ({ ...held, [id]: null }))
              setErrors((held) => ({ ...held, [id]: GONE }))
            }
            continue
          }
          seen.current[id] = true
          setRows((held) => ({ ...held, [id]: row }))
          if (activity(row) === 'terminal') settled.add(id)
        }
        if (settled.size >= latest.current.length) return
      } catch (err) {
        if (cancelled) return
        failures += 1
        if (failures >= MAX_CONSECUTIVE_ERRORS) {
          const detail = err instanceof ApiError ? err.message : String(err)
          const message = `${LOST} (${detail})`
          setErrors((held) => {
            const next = { ...held }
            for (const id of latest.current) {
              if (!settled.has(id)) next[id] = message
            }
            return next
          })
          return
        }
      }
      timer = setTimeout(poll, POLL_INTERVAL_MS)
    }
    poll()

    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [key])

  const byId: Record<string, TrackedWork> = {}
  for (const id of ids) {
    const entry = rows[id] ?? null
    const error = errors[id] ?? null
    const following = error === null
    const state = entry === null ? null : activity(entry)
    byId[id] = {
      ...IDLE,
      entry,
      pending: following && state === 'pending',
      running: following && state === 'active',
      outcome: state === 'terminal' && entry !== null ? entry.state : null,
      waiting: following && entry !== null && plan !== null ? waitNote(plan, entry) : null,
      error,
    }
  }
  return { plan, byId }
}
```

- [ ] **Step 4: Re-express `useQueuedJob` on it**

Replace the body of `useQueuedJob` in `hooks/useQueuedJob.ts` (keep the file's whole docstring, and add a paragraph to it) with:

```ts
export function useQueuedJob(entryId: string | null): QueuedWork {
  // One id through the multi-entry follower, so the `seen` race guard, the
  // error budget and the "error set means neither pending nor running" rule
  // exist in ONE place. A second copy of those three is exactly the
  // duplication this project has already paid for once, with three copies
  // of a colour ternary in three components.
  const ids = entryId ? [entryId] : EMPTY_IDS
  const tracked = useQueuedEntries(ids)
  const work = entryId ? tracked.byId[entryId] : undefined
  // Null until the worker claims the entry - which is exactly the state
  // this hook exists to make visible, so it is passed straight through
  // rather than faked with the entry's own id.
  const job = useJobPolling(work?.entry?.job_id ?? null)
  if (!entryId || work === undefined) return IDLE
  return { ...work, plan: tracked.plan, job }
}

/** A stable empty array, so `useQueuedEntries`'s effect key does not churn
 * on every render while no entry is being followed. */
const EMPTY_IDS: string[] = []
```

Delete the now-unused imports (`useRef`, `useState`, `useEffect`, `listJobs`, `findEntry`, `activity`, `waitNote`, `GONE`, `LOST`, `MAX_CONSECUTIVE_ERRORS`, `POLL_INTERVAL_MS`) from that file, keeping `useJobPolling`, `useQueuedEntries` and the `QueuedWork`/`IDLE` definitions.

- [ ] **Step 5: Run both hook suites to verify they pass**

Run: `cd src/yt_shorts/studio/web && npm test -- useQueuedEntries.test.tsx useQueuedJob.test.tsx`
Expected: PASS. **`useQueuedJob.test.tsx` must pass entirely unchanged** — it is the proof that re-expressing the hook kept every rule it had.

- [ ] **Step 6: Build and commit**

```bash
cd src/yt_shorts/studio/web && npm run build && cd -
python3 tools/lint.py
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "refactor(studio-web): follow many queue entries with one poll

useQueuedEntries reads the plan once for N entries; useQueuedJob is now
that hook with one id, so the seen race guard, the error budget and the
'error set means neither pending nor running' rule exist in one place.

useQueuedJob.test.tsx passes unchanged, which is the point."
```

---

### Task 8: The playlist filter and the per-row markers

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/StreamPanel.tsx`

**Interfaces:**
- Consumes: `playlistOptions`, `visibleVideos`, `ALL_STREAMS` from `streams.ts`; `StreamCatalogue` from `api.ts`.
- Produces: no new exports — `StreamPanel`'s props are unchanged in this task.

- [ ] **Step 1: Hold the catalogue, not just its videos**

In `StreamPanel.tsx`, replace the state and loader:

```ts
import { ALL_STREAMS, playlistOptions, visibleVideos } from '../streams'
import { Select } from '@mantine/core'   // add to the existing @mantine/core import
```

```ts
  const [catalogue, setCatalogue] = useState<StreamCatalogue | null>(null)
  const [playlist, setPlaylist] = useState<string>(ALL_STREAMS)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function load(refresh = false) {
    setLoading(true)
    try {
      const next = await listStreams(refresh)
      setCatalogue(next)
      // A refresh can drop the playlist that was selected. Falling back to
      // "all" is the only option that cannot show an empty list with no
      // explanation.
      setPlaylist((current) =>
        current === ALL_STREAMS || next.playlists.some((p) => p.id === current)
          ? current
          : ALL_STREAMS)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }
```

and derive the list:

```ts
  const options = catalogue ? playlistOptions(catalogue) : []
  const streams = catalogue ? visibleVideos(catalogue, playlist) : null
```

- [ ] **Step 2: Draw the filter and the failed-playlist alert**

Replace the header `<Group>` at the top of the returned `<Stack>` with:

```tsx
      <Group justify="space-between" wrap="nowrap" gap="xs">
        <Select
          size="xs"
          style={{ flex: 1 }}
          aria-label="Filter by playlist"
          value={playlist}
          onChange={(value) => setPlaylist(value ?? ALL_STREAMS)}
          data={options.map((option) => ({
            value: option.value,
            label: `${option.label} (${option.count})`,
          }))}
          allowDeselect={false}
          comboboxProps={{ withinPortal: true }}
        />
        <Tooltip label="Re-fetch the stream list and playlists from YouTube">
          <ActionIcon
            variant="default"
            size="sm"
            onClick={() => load(true)}
            disabled={loading}
            aria-label="Refresh streams"
          >
            {loading ? <Loader size={12} color="steel" /> : '⟲'}
          </ActionIcon>
        </Tooltip>
      </Group>

      {catalogue && catalogue.failed_playlists.length > 0 && (
        // Never swallowed: a catalogue missing a playlist looks exactly
        // like a complete one, and an operator would read a stream's
        // absence as "not published" rather than "not fetched".
        <Alert color="yellow" title="Some playlists could not be loaded" p="xs">
          <Text size="xs">
            {catalogue.failed_playlists.map((f) => f.title).join(', ')} —
            their streams may be missing from this list unless another
            playlist also holds them. Refresh to try again.
          </Text>
        </Alert>
      )}
```

- [ ] **Step 3: Show what each row already has**

Inside the row's metadata `<Group gap="md" wrap="nowrap">`, after the views text:

```tsx
                      {stream.has_transcript && (
                        <Badge size="xs" variant="dot" color="teal">
                          Transcript
                        </Badge>
                      )}
                      {stream.has_analysis && (
                        <Badge size="xs" variant="dot" color="grape">
                          Analysis
                        </Badge>
                      )}
```

And change the count line so an empty filtered view explains itself rather than looking broken — replace the `!error && streams && streams.length === 0` block with:

```tsx
      {!error && streams && streams.length === 0 && (
        <Text size="sm" c="dimmed" p="xs">
          {playlist === ALL_STREAMS
            ? 'No finished streams found for this channel yet.'
            : 'This playlist holds no usable videos - its entries may be ' +
              'deleted or private. Pick another playlist, or "All streams".'}
        </Text>
      )}
```

- [ ] **Step 4: Build to verify it type-checks**

Run: `cd src/yt_shorts/studio/web && npm run build`
Expected: PASS.

- [ ] **Step 5: Commit with the built output**

```bash
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "feat(studio-web): filter the stream list by playlist

91 streams in one flat list is what made the Streams tab unusable on a
channel with a back catalogue. The dropdown counts the union it shows,
offers 'In no playlist' only when something is in none, and names any
playlist whose fetch failed rather than serving a short list as a whole
one.

Each row now says whether it already has a transcript and an analysis."
```

---

### Task 9: Multi-select and the bulk bar

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/StreamPanel.tsx`
- Modify: `src/yt_shorts/studio/web/src/App.tsx:75-90, 358-412, 663-674`

**Interfaces:**
- Consumes: `bulkPlan`, `selectionNote` from `streams.ts`; `useQueuedEntries` from the hook; `batchNotice` from `jobs.ts`; `enqueueJob(kind, params, after)` from `api.ts`.
- Produces: `StreamPanel`'s new props — `entries: Record<string, StreamEntryIds>`, `work: QueuedEntries`, `busyVideoIds: string[]`, `onQueue(action, videoIds, force)`. `StreamEntryIds = {transcribe?: string; detect?: string}` is exported from `App.tsx`'s own module? **No** — declare it in `streams.ts` so no component file exports a type.

- [ ] **Step 1: Add the shared type to `streams.ts`**

Append to `src/yt_shorts/studio/web/src/streams.ts`:

```ts
/** The queue entries a bulk action created for one video. Declared here
 * rather than in a component, so no component file exports a type and
 * Vite's fast-refresh boundary stays component-only. */
export interface StreamEntryIds {
  transcribe?: string
  detect?: string
}
```

- [ ] **Step 2: Rewire `App.tsx` to one tracking map**

Replace the four state variables at lines 75-90 with:

```ts
  // Every queue entry the Streams tab has created, by video. One map
  // instead of the four variables this used to be (a detect entry id, a
  // transcribe entry id and an "active video" for each): a bulk action
  // creates several at once, and four variables can hold one. It lives
  // HERE, not in StreamPanel, so a row's live state survives switching the
  // navbar's tabs - the same reason the single detect entry was hoisted.
  const [streamEntries, setStreamEntries] =
    useState<Record<string, StreamEntryIds>>({})
  // Videos whose enqueue POSTs are still in flight. Distinct from "queued":
  // this is the brief window before the plan knows about them at all.
  const [queueingVideos, setQueueingVideos] = useState<string[]>([])
  const streamEntryIds = useMemo(
    () => Object.values(streamEntries)
      .flatMap((ids) => [ids.transcribe, ids.detect])
      .filter((id): id is string => id !== undefined),
    [streamEntries])
  const streamWork = useQueuedEntries(streamEntryIds)
```

Replace `handleStartDetect` and `handleStartTranscribe` (lines 358-412) with one handler:

```ts
  /**
   * Queue work for one or many streams.
   *
   * A single row's button is a batch of ONE through this same path, so
   * there is one set of rules and not two.
   *
   * Enqueued SEQUENTIALLY, in the order `bulkPlan` returns (catalogue
   * order): parallel requests would scramble the queue's own order, and the
   * plan an operator reads should match the list they were looking at.
   *
   * The chain breaks PER VIDEO. If a video's transcribe POST fails, its
   * detect is not enqueued at all - a detect without its `after` would
   * quietly run on an untranscribed stream and fail with
   * TranscriptNotCached, which reads as a bug in detection rather than as
   * the refused request it is.
   */
  async function handleQueueStreams(
    action: BulkAction,
    videoIds: string[],
    force: { transcribe: boolean; detect: boolean },
    videos: StreamVideo[],
  ) {
    const plan = bulkPlan(videoIds, videos, action, force)
    if (plan.steps.length === 0) return
    setQueueingVideos(plan.steps.map((step) => step.videoId))
    const titles = new Map(videos.map((video) => [video.video_id, video.title]))
    const created: Record<string, StreamEntryIds> = {}
    const refusals: string[] = []

    for (const step of plan.steps) {
      const ids: StreamEntryIds = {}
      try {
        if (step.transcribe) {
          const { entry } = await enqueueJob('transcribe', {
            channel, event, video_id: step.videoId,
          })
          ids.transcribe = entry.id
        }
      } catch (error) {
        refusals.push(error instanceof ApiError ? error.message : String(error))
        continue          // no transcript coming, so no detect for this one
      }
      try {
        if (step.detect) {
          const { entry } = await enqueueJob('detect', {
            channel, event, video_id: step.videoId,
            stream_title: titles.get(step.videoId) ?? '',
          }, ids.transcribe ?? null)
          ids.detect = entry.id
        }
      } catch (error) {
        refusals.push(error instanceof ApiError ? error.message : String(error))
      }
      created[step.videoId] = ids
    }

    setStreamEntries((held) => ({ ...held, ...created }))
    setQueueingVideos([])
    const queued = Object.keys(created).length
    if (refusals.length === 0) {
      notifications.show({
        title: `${queued} queued`,
        message: 'Queued, not started - the Streams tab shows each one\'s ' +
          'state, and the Jobs screen has the whole plan.',
        color: 'steel',
      })
    } else {
      // Never a bare "queued" when part of it was refused.
      notifications.show({
        title: `${queued} queued, ${refusals.length} refused`,
        message: refusals[0],
        color: 'red',
      })
    }
  }
```

`stream_title` is passed explicitly for the same reason it always was: the direct route read it from the studio's process-lifetime cache, which an entry running hours later cannot rely on.

Replace the two finish effects (the transcribe one at lines 419-435 and the detect one beside it) with one that reports a whole batch:

```ts
  // One notification per batch, not one per entry: a bulk action over
  // thirteen streams would otherwise raise thirteen toasts. batchNotice
  // delegates to endedNotice for a batch of one, so a single-row action
  // says exactly what it always said - and a STOP is never red.
  const settledRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    const ended: { outcome: string; reason: string | null }[] = []
    // The KIND comes from the entries themselves, and only matters for a
    // batch of one: that is the case batchNotice delegates to endedNotice,
    // whose KEPT_AFTER_A_STOP sentence is per kind ("chunks already decoded
    // stay cached" for a transcribe, "every window it had already scored"
    // for a detect). Hard-coding one would tell a stopped detection what a
    // stopped transcription keeps.
    let kind = 'transcribe'
    for (const [id, work] of Object.entries(streamWork.byId)) {
      if (work.outcome === null || settledRef.current.has(id)) continue
      settledRef.current.add(id)
      if (work.entry) kind = work.entry.kind
      ended.push({ outcome: work.outcome, reason: work.entry?.reason ?? null })
    }
    if (ended.length === 0) return
    notifications.show(batchNotice(
      ended.length === 1 ? 'The stream job' : 'Stream jobs', kind, ended))
  }, [streamWork])
```

Update the `<StreamPanel …>` element (lines 663-674) to:

```tsx
            <StreamPanel
              channel={channel}
              event={event}
              entries={streamEntries}
              work={streamWork}
              busyVideoIds={queueingVideos}
              onQueue={handleQueueStreams}
            />
```

Add the imports: `useMemo`, `useRef` from react; `bulkPlan`, `type BulkAction`, `type StreamEntryIds` from `./streams`; `useQueuedEntries` from `./hooks/useQueuedEntries`; `batchNotice` from `./jobs`; `type StreamVideo` from `./api`.

- [ ] **Step 3: Give `StreamPanel` its new props and the bar**

Replace `StreamPanelProps` with:

```ts
interface StreamPanelProps {
  channel: string
  event: string
  /** Every queue entry created here, by video - owned by App so a row's
   * live state survives switching tabs. */
  entries: Record<string, StreamEntryIds>
  /** Those entries' live state, from ONE poll of the plan. */
  work: QueuedEntries
  /** Videos whose enqueue POST is still in flight - the brief window before
   * the plan knows about them at all. Distinct from "queued", which is a
   * state the plan can report. */
  busyVideoIds: string[]
  onQueue: (
    action: BulkAction,
    videoIds: string[],
    force: { transcribe: boolean; detect: boolean },
    videos: StreamVideo[],
  ) => void
}
```

Add selection state and the derived plan:

```ts
  const [selected, setSelected] = useState<string[]>([])
  const [forceTranscribe, setForceTranscribe] = useState(false)
  const [forceDetect, setForceDetect] = useState(false)
  const force = { transcribe: forceTranscribe, detect: forceDetect }
  const visibleIds = (streams ?? []).map((video) => video.video_id)
  const allVideos = catalogue?.videos ?? []
  const hidden = selectionNote(selected, visibleIds)
```

Add a checkbox to each row (before the title `<Text>`, wrapping the row's header in a `<Group>`):

```tsx
                  <Group gap="xs" wrap="nowrap" align="flex-start">
                    <Checkbox
                      size="xs"
                      mt={4}
                      aria-label={`Select ${stream.title}`}
                      checked={selected.includes(stream.video_id)}
                      onChange={(changeEvent) => setSelected((held) =>
                        changeEvent.currentTarget.checked
                          ? [...held, stream.video_id]
                          : held.filter((id) => id !== stream.video_id))}
                    />
                    <Text …the existing title Text… />
                  </Group>
```

Add the bar as the LAST child of the outer `<Stack>`, after the `<ScrollArea>` — a fixed footer inside the panel, so the list keeps its `flex: 1` and every row stays reachable at a short viewport:

```tsx
      {selected.length > 0 && (
        <Stack gap={6} p="xs" style={{
          borderTop: '1px solid var(--mantine-color-dark-6)',
        }}>
          <Group justify="space-between" wrap="wrap" gap="xs">
            <Text size="xs" c="dimmed">
              {selected.length} selected{hidden ? ` (${hidden})` : ''}
            </Text>
            <Group gap={6} wrap="nowrap">
              <Button size="xs" variant="default"
                      onClick={() => setSelected(visibleIds)}>
                Select all shown
              </Button>
              <Button size="xs" variant="subtle" onClick={() => setSelected([])}>
                Clear
              </Button>
            </Group>
          </Group>

          {/* What WILL happen, before the click. A button that silently
              does nothing is the same lying control as a spinner that
              never moves - so when everything is skipped, the button is
              disabled and this line is the reason. */}
          {(['transcribe', 'detect', 'both'] as BulkAction[]).map((action) => {
            const planned = bulkPlan(selected, allVideos, action, force)
            return (
              <Group key={action} justify="space-between" wrap="nowrap" gap="xs">
                <Text size="xs" c="dimmed">
                  {planned.note ?? `${planned.steps.length} will be queued`}
                </Text>
                <Button
                  size="xs"
                  variant="default"
                  disabled={planned.steps.length === 0}
                  onClick={() => onQueue(action, selected, force, allVideos)}
                >
                  {/* Deliberately NOT "Transcribe"/"Detect moments"/"Transcribe
                      + detect": Playwright's get_by_role matches a name as a
                      SUBSTRING by default, so a bar button whose name contains
                      a row button's name makes every non-exact lookup in the
                      E2E suite ambiguous the moment a row is ticked. These
                      three share no substring with the row buttons. */}
                  {action === 'both'
                    ? 'Queue transcription and detection for selected'
                    : action === 'transcribe'
                      ? 'Queue transcription for selected'
                      : 'Queue detection for selected'}
                </Button>
              </Group>
            )
          })}

          <Group gap="md" wrap="wrap">
            <Checkbox size="xs" label="Re-transcribe anyway"
                      checked={forceTranscribe}
                      onChange={(e) => setForceTranscribe(e.currentTarget.checked)} />
            <Checkbox size="xs" label="Re-detect anyway"
                      checked={forceDetect}
                      onChange={(e) => setForceDetect(e.currentTarget.checked)} />
          </Group>
        </Stack>
      )}
```

Replace the per-row buttons' state derivation with a read of the tracking map, and keep the single-row buttons as batches of one:

```tsx
                      {(() => {
                        const ids = entries[stream.video_id] ?? {}
                        const busy = busyVideoIds.includes(stream.video_id)
                        const transcribeWork = ids.transcribe
                          ? work.byId[ids.transcribe] : undefined
                        const detectWork = ids.detect ? work.byId[ids.detect] : undefined
                        return (
                          <Group gap={6} wrap="nowrap">
                            <Button
                              size="xs"
                              variant={transcribeWork?.running ? 'light' : 'default'}
                              color={transcribeWork?.running ? 'steel' : undefined}
                              disabled={busy || transcribeWork?.pending}
                              loading={transcribeWork?.running}
                              onClick={() => onQueue(
                                'transcribe', [stream.video_id], force, allVideos)}
                            >
                              {transcribeWork?.running ? 'Transcribing…'
                                : busy || transcribeWork?.pending ? 'Queued…' : 'Transcribe'}
                            </Button>
                            <Button
                              size="xs"
                              variant={detectWork?.running ? 'light' : 'default'}
                              color={detectWork?.running ? 'steel' : undefined}
                              disabled={busy || detectWork?.pending}
                              loading={detectWork?.running}
                              onClick={() => onQueue(
                                'detect', [stream.video_id], force, allVideos)}
                            >
                              {detectWork?.running ? 'Detecting…'
                                : busy || detectWork?.pending ? 'Queued…' : 'Detect moments'}
                            </Button>
                          </Group>
                        )
                      })()}
```

Keep `TrackedEntry` and render it per row, under the buttons, for whichever of the two entries exists — that is what carries the badge, the log link, `waitNote` and the two stop reasons. Change its prop type from `QueuedWork` to `TrackedWork` and drop the `plan` it never read.

- [ ] **Step 4: Build and run every Vitest suite**

Run: `cd src/yt_shorts/studio/web && npm run build && npm test`
Expected: PASS both.

- [ ] **Step 5: Commit with the built output**

```bash
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "feat(studio-web): queue several streams at once

A checkbox per row and a bar that says what WILL happen before the click:
what is skipped because it already exists, and how much is left. When
nothing is left the button is disabled with the reason, rather than
clicking into silence.

'Transcribe + detect' chains each detect behind its own transcription
with the queue's existing Entry.after - no new mechanism. The chain
breaks per video: a refused transcribe means no detect for that stream,
because a detect without its after would quietly run untranscribed.

One tracking map replaces four state variables, and one notification per
batch replaces one per entry."
```

---

### Task 10: The journeys, end to end

**Files:**
- Modify: `tests/test_studio_e2e.py`

**Interfaces:**
- Consumes: the whole feature. Uses the file's existing `live_queue_server`, `page`, `CHANNEL`, `EVENT`, `_wheel_scroll_until_visible`, `_within_viewport`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_studio_e2e.py`:

```python
class TestPlaylistFilterAndBulkQueueing:
    """The Streams tab with a channel's back catalogue in it.

    Every test here runs against a studio whose worker is deliberately NOT
    running, like TestTheOtherButtonsGoThroughTheQueueToo: a click must
    leave an honest, explained queue entry, not a spinner.
    """

    def _catalogue(self):
        from yt_shorts.youtube import Catalogue, Playlist, Video
        return Catalogue(
            videos=[
                Video("vid-a", "ERF Race Part 1", 29975, 2200, ["PL1"]),
                Video("vid-b", "ERF Race Part 2", 29478, 1300, ["PL1"]),
                # In a playlist and NOT in the Streams tab - the case that
                # made two multi-hour ERF broadcasts unreachable.
                Video("vid-c", "ERF Special Catalunya 6H", 8983, 400, ["PL2"]),
                Video("vid-d", "ERF Loose Stream", 3600, 100, []),
            ],
            playlists=[Playlist("PL1", "2026 Season", 2, 0),
                       Playlist("PL2", "ERF Specials", 1, 2)],
            failed_playlists=[])

    def _serve(self, monkeypatch):
        import yt_shorts.studio.api as api
        catalogue = self._catalogue()
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: catalogue)

    def _wait_for_entries(self, server, count, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(server.queue.list()) >= count:
                break
            time.sleep(0.05)
        return server.queue.list()

    def test_the_filter_narrows_the_list_to_a_playlist(
            self, live_queue_server, page, monkeypatch):
        self._serve(monkeypatch)
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("ERF Race Part 1").wait_for()
        # The union, not the Streams tab's own total.
        expect(page.get_by_text("All streams (4)")).to_be_visible()

        page.get_by_label("Filter by playlist").click()
        page.get_by_role("option", name="ERF Specials (1)").click()

        expect(page.get_by_text("ERF Special Catalunya 6H")).to_be_visible()
        expect(page.get_by_text("ERF Race Part 1")).not_to_be_visible()

    def test_a_bulk_transcribe_queues_one_entry_per_stream_in_list_order(
            self, live_queue_server, page, monkeypatch):
        self._serve(monkeypatch)
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_label("Select ERF Race Part 2").check()
        page.get_by_label("Select ERF Race Part 1").check()
        page.get_by_role("button", name="Queue transcription for selected").click()

        entries = self._wait_for_entries(server, 2)
        assert [e.kind for e in entries] == ["transcribe", "transcribe"]
        # Catalogue order, NOT the order the boxes were ticked: the plan
        # should match the list the operator was looking at.
        assert [e.params["video_id"] for e in entries] == ["vid-a", "vid-b"]

    def test_transcribe_and_detect_chains_each_detect_behind_its_own_transcribe(
            self, live_queue_server, page, monkeypatch):
        """The whole point of the chained action, and the reason it needed
        no new queue mechanism: `Entry.after` already holds a dependent back
        until its dependency is done, and FAILS it if that never succeeds.
        A detect whose `after` pointed at the wrong transcribe - or at
        nothing - would run on an untranscribed stream."""
        self._serve(monkeypatch)
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_label("Select ERF Race Part 1").check()
        page.get_by_label("Select ERF Race Part 2").check()
        page.get_by_role("button", name="Queue transcription and detection for selected").click()

        entries = self._wait_for_entries(server, 4)
        by_id = {e.id: e for e in entries}
        detects = [e for e in entries if e.kind == "detect"]
        assert len(detects) == 2
        for detect_entry in detects:
            dependency = by_id[detect_entry.after]
            assert dependency.kind == "transcribe"
            assert dependency.params["video_id"] == detect_entry.params["video_id"], (
                "a detect was chained behind another stream's transcription")

    def test_a_queued_dependent_says_what_it_waits_for(
            self, live_queue_server, page, monkeypatch):
        """`waitNote`'s dependency branch, in a browser. Before it, a detect
        waiting on a transcription was told it was "next in line, and starts
        as soon as the worker has a free slot" - which a free slot does not
        do."""
        self._serve(monkeypatch)
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_label("Select ERF Race Part 1").check()
        page.get_by_role("button", name="Queue transcription and detection for selected").click()
        self._wait_for_entries(server, 2)

        expect(page.get_by_text(re.compile(
            r"waits for the transcribe job it depends on", re.I))).to_be_visible()

    def test_a_stream_that_already_has_a_transcript_is_skipped_and_said_so(
            self, live_queue_server, page, monkeypatch, _fixed_workspace_root):
        """A re-transcription re-downloads the stream's audio before it can
        even count its chunks - gigabytes for an 8-hour race. The bar says
        so before the click rather than after the download.

        `_fixed_workspace_root` (tests/conftest.py) is the root every app in
        this suite resolves to, and it is SESSION-scoped - so this seeds a
        UNIQUE video id rather than a fixed one, the same reason the other
        tests in this file mint ids with uuid4. A fixed `vid-a` here would
        leave a transcript on disk for every test that collected after it.
        """
        from yt_shorts.youtube import Catalogue, Playlist, Video
        import yt_shorts.studio.api as api
        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: Catalogue(
            videos=[Video(video_id, "ERF Race Part 1", 29975, 2200, ["PL1"])],
            playlists=[Playlist("PL1", "2026 Season", 1, 0)],
            failed_playlists=[]))
        directory = _fixed_workspace_root / "streams" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "transcript.json").write_text("{}", encoding="utf-8")

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        expect(page.get_by_text("Transcript")).to_be_visible()
        page.get_by_label("Select ERF Race Part 1").check()

        expect(page.get_by_text(re.compile(
            r"1 transcription skipped: already transcribed", re.I))).to_be_visible()
        # And the button refuses rather than clicking into silence.
        transcribe = page.get_by_role("button", name="Queue transcription for selected")
        expect(transcribe).to_be_disabled()

    def test_a_failed_playlist_is_named_rather_than_silently_missing(
            self, live_queue_server, page, monkeypatch):
        from yt_shorts.youtube import Catalogue, FailedPlaylist, Video
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: Catalogue(
            videos=[Video("vid-a", "ERF Race Part 1", 29975, 2200, [])],
            playlists=[],
            failed_playlists=[FailedPlaylist("Bathurst 12 Hour", "HTTP 404")]))
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        expect(page.get_by_text("Bathurst 12 Hour")).to_be_visible()

    def test_the_bulk_bar_never_hides_the_last_row(
            self, live_queue_server, page, monkeypatch):
        """Reachability at a short viewport is an acceptance criterion here,
        not a nicety: the bar is a footer inside the panel, and a footer
        that grows can push the list's last rows out of reach.

        Driven with a real mouse WHEEL, never scroll_into_view_if_needed():
        that call was proven on this branch to pass on a broken build, by
        setting scrollLeft/scrollTop on an overflow:hidden ancestor no real
        wheel could ever move.
        """
        from yt_shorts.youtube import Catalogue, Video
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: Catalogue(
            videos=[Video(f"vid-{n}", f"ERF Race Part {n}", 3600, 10, [])
                    for n in range(30)],
            playlists=[], failed_playlists=[]))
        server = live_queue_server
        viewport = {"width": 1280, "height": 520}
        page.set_viewport_size(viewport)
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_label("Select ERF Race Part 0").check()   # the bar appears

        last = page.get_by_text("ERF Race Part 29")
        anchor = page.get_by_text("ERF Race Part 0")
        box = _wheel_scroll_until_visible(page, last, anchor, dy=250)
        assert _within_viewport(box, viewport), box
```

Add `import re`, `import time` and `uuid` only if the file does not already import them (it does — check before adding, a duplicate import is what `F811` exists for).

`live_queue_server` yields `SimpleNamespace(url, app, queue)` and exposes **no** workspace root — the root is `tests/conftest.py`'s session-scoped `_fixed_workspace_root`, which every app in this suite resolves to (see `_isolated_resolved_workspace`). Request that fixture by name, and because it is session-scoped, seed a `uuid4`-derived video id rather than a fixed one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py::TestPlaylistFilterAndBulkQueueing -q`
Expected: FAIL — the filter, the checkboxes and the bar do not exist in the built `static/` unless Tasks 8 and 9 were built and committed. If they fail with "element not found" while the source looks right, the build was not re-run.

- [ ] **Step 3: Fix whatever they catch**

These tests exercise the wiring nothing else does. Expect to fix accessible names (`aria-label`), the `.last` disambiguation between a row's Transcribe and the bar's, and the bar's own layout. Change `StreamPanel.tsx`, rebuild (`npm run build`), and re-run.

- [ ] **Step 4: Run the whole suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS, with the collected count no lower than before this branch. **A lower count is not a smaller test run, it is a silently dropped class** — run `python3 tools/lint.py` and read the `F811` findings.

- [ ] **Step 5: Commit**

```bash
python3 tools/lint.py
git add tests/test_studio_e2e.py src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "test(studio-e2e): the playlist filter and the bulk queue, in a browser

Against a studio whose worker is deliberately not running, so a click
has to leave an honest queue entry rather than a spinner.

Pins the two things nothing else can: that each detect is chained behind
its OWN stream's transcription, and that the bulk bar - a footer that
grows - never pushes the list's last row out of reach at a short
viewport, checked with a real mouse wheel."
```

---

### Task 11: Say what changed, where the next author reads it

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Update `CLAUDE.md`**

In the section beginning **"Stream discovery is `youtube.list_streams`, not a YouTube Data API call"**, append:

```markdown
**And the studio now reads a CATALOGUE, not just that tab.**
`youtube.channel_catalogue` composes three reads - the Streams tab, the
channel's playlist list, and every playlist's members (parallel, six
threads, each worker calling the same injected `runner`) - into one answer
the Streams tab groups by playlist. Measured on ERF, 2026-08-04, and every
one of these is a reading of one channel on one day rather than a property
of YouTube: 91 streams, 17 playlists, 99 distinct videos, 2.5s for all 17
member fetches against 20s sequential.

**The union is the point, not a side effect.** Eight ERF videos live in a
playlist and NOT in the Streams tab, two of them multi-hour broadcasts
(`was_live`, 2h30 and 2h06) - which means they were unreachable from the
studio entirely. That is why a selected playlist shows the PLAYLIST's
contents rather than the Streams tab filtered by membership: the filtered
reading cannot show a video the tab never listed, and would present a
partly-empty playlist with no explanation.

Two losses are counted rather than swallowed, for the reason this file
records everywhere else: a playlist entry with no title is a deleted or
private video (two on ERF) and is dropped but counted as that playlist's
`unavailable`, so a displayed "(6)" is never silently a 6 that came from 8;
and a playlist whose fetch fails is named in `failed_playlists` while the
rest is served. The Streams tab's OWN failure still raises - without it
there is no list at all.

`playlist_ids` is a LIST on every video. No ERF video is in two playlists
today; that is an observation, not a guarantee, and the field must not be
narrowed to a single id on the strength of it.

**`GET …/streams` caches the expensive half only.** The yt-dlp reads are
cached per channel for the session, as they always were. `has_transcript`
and `has_analysis` (`detect.has_cached_transcript`/`has_analysis`, one
`Path.is_file` each) are stat'd FRESH on every response: a cached "no
transcript" would survive a finished transcription until someone pressed
refresh, and a marker that outlives the fact it reports is worse than no
marker. Both answer False for an id that is not a safe segment rather than
raising - they are asked once per video over a whole catalogue, and one odd
id must not 500 a list of 99. `detect.stream_dir` still raises, which is
what `require_cached_transcript` needs.
```

In the section on the Jobs screen and `waitNote`, append:

```markdown
**A dependent entry is no longer told a free slot will start it.**
`Entry.after` has existed since the queue shipped, but no browser call site
ever SENT one - `JobsScreen` merely displayed it - so `waitNote` had no
branch for it: a `detect` waiting on a RUNNING transcription has nothing
queued in front of it, so `ahead` was 0 and it answered "It is next in
line, and starts as soon as the worker has a free slot", which is false in
the one way that matters. The Streams tab's "Transcribe + detect" is what
made that reachable, so the branch landed with it. A dependency the plan no
longer holds stays quiet: `_trim_finished` ages a long-since-done one out
and `_dependency_status` treats absence as SATISFIED, so saying otherwise
would contradict the queue.

**The chained bulk action needed no new queue mechanism, and an early
design of it wanted one.** The first draft added a `defer` reason to
`Worker._blocked_by` for a detect whose transcript was missing. `after` is
strictly better and already there: it cannot wait forever, and a dependent
whose dependency ends without succeeding is FAILED with a reason naming it.
Do not add the deferral rule back.

**A bulk action's rules are `streams.ts`, and its skips are not tidiness.**
`bulkPlan` skips a stream that already has a transcript, and one that
already has an analysis, unless the operator ticks the matching "anyway"
box. Both cost something concrete: a re-transcription DOWNLOADS THE AUDIO
AGAIN before it can count its chunks (`stream_transcribe.transcribe_stream`
- gigabytes for an 8-hour race, even though every chunk then comes from the
cache), and a re-detection spends money at the provider. The bar says what
will be skipped BEFORE the click, and disables the button when nothing is
left - a control that clicks into silence is the same lie as a spinner that
never moves.

The chain breaks PER VIDEO, not per batch: if a video's `transcribe` POST is
refused, its `detect` is not enqueued at all, because a detect without its
`after` would quietly run on an untranscribed stream and fail with
`TranscriptNotCached` - which reads as a bug in detection rather than as the
refused request it is. Entries are enqueued SEQUENTIALLY in catalogue order;
parallel requests would scramble the queue's order away from the list the
operator was looking at.

**`useQueuedJob` is now `useQueuedEntries` with one id.** The plan is a
single `GET /api/jobs`, so a bulk action followed by thirteen copies of the
old hook would have fetched it thirteen times a second - and a second hook
beside it would have been a second copy of the `seen` race guard, the error
budget, and the rule that `error` set means `pending` and `running` are both
false. `useQueuedJob.test.tsx` passes unchanged across that move, which is
what makes it a refactor rather than a rewrite. `jobs.ts`'s `batchNotice`
is the same argument one layer up: one notification per batch, delegating to
`endedNotice` for a batch of one so a single-row action cannot drift from a
bulk one, and never reporting a stop in the failure colour.
```

Also update the sentence naming the frontend's pure modules to include `streams.ts`, and the one naming `words.ts`, `format.ts`, `window.ts`, `scopedApi.ts`, `streamTimeline.ts`, `providers.ts`, `jobs.ts`.

- [ ] **Step 2: Update `README.md`**

In the section describing the studio's Streams tab, add a paragraph:

```markdown
The Streams tab groups a channel's streams by its YouTube playlists. The
list is the union of the channel's Streams tab and every playlist's
contents, so a broadcast that lives only in a playlist is reachable too -
on the ERF channel that is eight videos, two of them multi-hour races.
Each row shows whether that stream already has a transcript and an
analysis.

Tick several rows and queue them in one action: **Transcribe**, **Detect
moments**, or **Transcribe + detect**, which chains each detection behind
its own transcription so it starts only once there is something to score.
A stream that already has what the action would produce is skipped, and
the bar says how many before you click - re-transcribing re-downloads the
stream's audio, and re-detecting spends money at the model provider. Tick
"anyway" to override either.

Nothing starts on the click: everything goes into the queue, and the Jobs
screen is where the whole plan lives.
```

- [ ] **Step 3: Run the full suite and the linter one last time**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
cd src/yt_shorts/studio/web && npm test && npm run build && cd -
git status --short          # static/ must be clean, i.e. already committed
```

Expected: pytest PASS with no collection drop, lint exit 0, Vitest PASS, build PASS, and `git status` showing no uncommitted `static/` churn.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: playlists, bulk queueing, and the two corrections they forced

CLAUDE.md records what the measurement decided (the union, not the
filtered Streams tab), what is counted rather than swallowed, why
has_transcript is not cached with the yt-dlp answer, and the two things
this change corrected: waitNote's missing dependency branch, and the
deferral rule an early design wanted before Entry.after was noticed."
```

---

## Self-Review

**Spec coverage.** Every section of the design maps to a task: the catalogue and its two honesty rules → Task 1; `detect.py`'s helpers → Task 2; the route and its fresh flags → Task 3; the client types → Task 4; `playlistOptions`/`visibleVideos`/`bulkPlan`/`selectionNote` → Task 5; the `waitNote` correction and `batchNotice` → Task 6; `useQueuedEntries` with `useQueuedJob` re-expressed → Task 7; the filter, markers and the "playlist contents" decision → Task 8; multi-select, the symmetric skip rule, the per-video chain break, the surviving selection and the one tracking map → Task 9; the E2E journeys and the short-viewport reachability → Task 10; the docs → Task 11. The "explicitly out of scope" list is honoured: no task touches `job_queue.py`, `worker.py` or `_validate_enqueue`.

**Type consistency.** `StreamVideo`/`StreamPlaylist`/`FailedPlaylist`/`StreamCatalogue` are defined in Task 4 and used under those names in Tasks 5, 8 and 9. `StreamEntryIds` is defined in `streams.ts` (Task 9, Step 1) and consumed in `App.tsx` and `StreamPanel.tsx`. `TrackedWork`/`QueuedEntries` are defined in Task 7 and consumed in Task 9 — `TrackedEntry` in `StreamPanel.tsx` takes `TrackedWork`, not the old `QueuedWork`. Python-side, `Catalogue`/`Video`/`Playlist`/`FailedPlaylist`/`PlaylistContents` are defined in Task 1 and used by name in Tasks 3 and 10.

**Two things a later task would break silently, called out where they land:** the built `static/` must be committed by every frontend task or Task 10's Playwright tests exercise the old page; and a drop in pytest's collected count is a silently shadowed test class, not a smaller run.
