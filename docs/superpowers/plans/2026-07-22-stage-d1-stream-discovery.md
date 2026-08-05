# Stage D1 — Stream Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** List a channel's finished streams — title, duration, view count, video ID — with one `yt-dlp` call, and surface them in the studio so an operator picks a stream instead of copying a URL.

**Architecture:** A pure-ish module `youtube.py` runs `yt-dlp --flat-playlist --dump-json` against the channel's `/streams` tab and parses the NDJSON into `Stream` objects, with the subprocess boundary injected for testing. A studio route serves the list, cached in memory for the session.

**Tech Stack:** Python 3 standard library plus the yt-dlp binary already used by `harvest`. FastAPI for the studio route. No new dependencies.

## Global Constraints

- `PYTHONPATH=src` is mandatory. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` — 485 tests pass at the start of this plan.
- **No YouTube Data API key, no OAuth.** Discovery is one `yt-dlp` call; the spec explains why. Do not add a Google API dependency.
- No new Python dependencies. yt-dlp is invoked as a subprocess, exactly as `harvest._query_ytdlp` does.
- **The subprocess boundary is injected** so tests never hit the network — parsing, ordering and error handling run against recorded `yt-dlp --dump-json` output.
- One malformed entry never sinks the list; a yt-dlp failure is an understandable `YouTubeError`, not a raw traceback. Same per-entry isolation the rest of the tool uses.
- **FastAPI stays optional**: nothing outside `src/yt_shorts/studio/` imports it. `youtube.py` must not import FastAPI.
- Tests must not depend on `~/YT-Shorts-Data`; `tests/conftest.py` pins `profile.CHANNELS_DIR` to `tests/fixtures/channels`.
- English only. Imperative commit messages.

---

## Task 1: List a channel's streams

**Files:**
- Create: `src/yt_shorts/youtube.py`
- Test: `tests/test_youtube.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `youtube.Stream` — dataclass: `video_id: str`, `title: str`, `duration_seconds: int | None`, `view_count: int | None`
  - `youtube.YouTubeError` — raised when yt-dlp fails
  - `youtube.list_streams(channel_url: str, *, runner=...) -> list[Stream]`
  - the default runner runs `yt-dlp --flat-playlist --dump-json "<channel_url>/streams"` and returns stdout; `runner(args: list[str]) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_youtube.py`:

```python
import json

import pytest

from yt_shorts.youtube import Stream, YouTubeError, list_streams

CHANNEL = "https://www.youtube.com/channel/UCb3S2oA7lANdg5IS0QtF46w"

# Recorded from a real `yt-dlp --flat-playlist --dump-json .../streams` run,
# trimmed to the fields we read. Newest first, as the /streams tab returns them.
LINES = "\n".join(json.dumps(d) for d in [
    {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
     "duration": 28431, "view_count": 1800, "live_status": "was_live"},
    {"id": "2O_lQrxEHWo", "title": "ERF 24h Nürburgring 2026 | Part 2",
     "duration": 29478, "view_count": 1300, "live_status": "was_live"},
    {"id": "Esm9vv5-PdU", "title": "ERF 24h Nürburgring 2026 | Part 1",
     "duration": 29975, "view_count": 2200, "live_status": "was_live"},
])


def runner_returning(text):
    def run(args):
        return text
    return run


class TestListStreams:
    def test_fields_are_extracted(self):
        streams = list_streams(CHANNEL, runner=runner_returning(LINES))
        assert streams[0] == Stream(
            video_id="xQlD7MkC-Eo",
            title="ERF 24h Nürburgring 2026 | Part 3",
            duration_seconds=28431, view_count=1800)

    def test_order_is_preserved(self):
        streams = list_streams(CHANNEL, runner=runner_returning(LINES))
        assert [s.video_id for s in streams] == [
            "xQlD7MkC-Eo", "2O_lQrxEHWo", "Esm9vv5-PdU"]

    def test_the_runner_is_asked_for_the_streams_tab(self):
        seen = {}

        def run(args):
            seen["args"] = args
            return LINES

        list_streams(CHANNEL, runner=run)
        assert seen["args"][-1] == f"{CHANNEL}/streams"
        assert "--flat-playlist" in seen["args"]
        assert "--dump-json" in seen["args"]

    def test_a_malformed_line_is_skipped_not_fatal(self):
        text = LINES + "\n{not json\n"
        streams = list_streams(CHANNEL, runner=runner_returning(text))
        assert len(streams) == 3

    def test_a_missing_duration_or_view_count_is_tolerated(self):
        text = json.dumps({"id": "x", "title": "T"})
        streams = list_streams(CHANNEL, runner=runner_returning(text))
        assert streams[0] == Stream("x", "T", None, None)

    def test_an_entry_without_an_id_is_skipped(self):
        text = json.dumps({"title": "no id"})
        assert list_streams(CHANNEL, runner=runner_returning(text)) == []

    def test_blank_lines_are_ignored(self):
        text = "\n\n" + LINES + "\n\n"
        assert len(list_streams(CHANNEL, runner=runner_returning(text))) == 3

    def test_empty_output_is_an_empty_list_not_an_error(self):
        assert list_streams(CHANNEL, runner=runner_returning("")) == []


class TestErrors:
    def test_a_runner_failure_becomes_a_youtube_error(self):
        def run(args):
            raise RuntimeError("yt-dlp: channel not found")
        with pytest.raises(YouTubeError) as error:
            list_streams(CHANNEL, runner=run)
        assert "channel not found" in str(error.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_youtube.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.youtube'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/youtube.py`:

```python
"""List a channel's streams with yt-dlp.

The tool already depends on yt-dlp for every download, and one
`yt-dlp --flat-playlist --dump-json <channel>/streams` call returns a
channel's finished streams with the fields this needs - id, title, duration,
view count - newest first. No YouTube Data API key, no quota, no OAuth: see
the stage D1 design for why the API-key path was dropped.

The subprocess call is injected as `runner` so parsing and error handling are
tested against recorded output without the network, mirroring how `harvest`
isolates its own yt-dlp call.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


class YouTubeError(Exception):
    """Understandable message about a failed yt-dlp discovery call."""


@dataclass
class Stream:
    video_id: str
    title: str
    duration_seconds: int | None
    view_count: int | None


def _default_runner(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()[-1] if result.stderr \
            else "yt-dlp failed"
        raise RuntimeError(message)
    return result.stdout


def list_streams(channel_url: str, *, runner=_default_runner) -> list[Stream]:
    """Returns a channel's finished streams, newest first.

    A malformed line is skipped rather than sinking the list, an entry with no
    id is skipped, and a missing duration or view count is tolerated - the same
    per-entry tolerance the rest of the tool applies to yt-dlp output. A yt-dlp
    failure is raised as YouTubeError.
    """
    args = ["yt-dlp", "--flat-playlist", "--dump-json",
            f"{channel_url}/streams"]
    try:
        output = runner(args)
    except Exception as error:
        raise YouTubeError(f"Could not list streams for {channel_url}: {error}") \
            from error

    streams: list[Stream] = []
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
        streams.append(Stream(
            video_id=entry["id"],
            title=entry.get("title", ""),
            duration_seconds=_int_or_none(entry.get("duration")),
            view_count=_int_or_none(entry.get("view_count")),
        ))
    return streams


def _int_or_none(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_youtube.py -q`
Expected: PASS, 9 tests

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 494 passed (485 + 9)

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/youtube.py tests/test_youtube.py
git commit -m "List a channel's streams with one yt-dlp call"
```

---

## Task 2: Serve the stream list from the studio

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_api.py` (add one class)

**Interfaces:**
- Consumes: `youtube.list_streams`, `youtube.Stream`, `youtube.YouTubeError`; `Profile` (already passed to `create_app`), whose `.channel["channel_url"]` gives the channel URL
- Produces: `GET /api/streams` returning a list of `{video_id, title, duration_seconds, view_count}`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_studio_api.py`. **Use the file's existing fixtures**: `client` (a `TestClient` over `create_app(studio_profile)`) and `studio_profile` (a `Profile` whose `.channel["channel_url"]` is the ERF channel URL). The route looks up `list_streams` as a module global at request time, so `monkeypatch.setattr(api, "list_streams", …)` in the test body takes effect even though the `client` fixture already built the app — no need to rebuild it.

```python
class TestStreamsRoute:
    def test_streams_are_listed(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        from yt_shorts.youtube import Stream
        monkeypatch.setattr(api, "list_streams",
                            lambda url, **k: [Stream("aaa", "Race Part 1", 29975, 2200)])
        r = client.get("/api/streams")
        assert r.status_code == 200
        assert r.json() == [{"video_id": "aaa", "title": "Race Part 1",
                             "duration_seconds": 29975, "view_count": 2200}]

    def test_the_list_is_cached_within_a_session(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        from yt_shorts.youtube import Stream
        calls = {"n": 0}

        def fake(url, **k):
            calls["n"] += 1
            return [Stream("aaa", "T", 1, 1)]

        monkeypatch.setattr(api, "list_streams", fake)
        client.get("/api/streams")
        client.get("/api/streams")
        assert calls["n"] == 1

    def test_a_refresh_re_fetches(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        from yt_shorts.youtube import Stream
        calls = {"n": 0}

        def fake(url, **k):
            calls["n"] += 1
            return [Stream("aaa", "T", 1, 1)]

        monkeypatch.setattr(api, "list_streams", fake)
        client.get("/api/streams")
        client.get("/api/streams?refresh=true")
        assert calls["n"] == 2

    def test_a_youtube_error_is_a_502_with_a_message(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        from yt_shorts.youtube import YouTubeError

        def fail(url, **k):
            raise YouTubeError("yt-dlp is not installed")

        monkeypatch.setattr(api, "list_streams", fail)
        r = client.get("/api/streams")
        assert r.status_code == 502
        assert "yt-dlp" in r.json()["detail"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k Streams`
Expected: FAIL — 404, the route does not exist

- [ ] **Step 3: Write the implementation**

At the top of `src/yt_shorts/studio/api.py`, with the other imports:

```python
from ..youtube import YouTubeError, list_streams
```

Inside `create_app`, alongside the other routes, add a session cache in the
closure and the route:

```python
    streams_cache: list[dict] | None = None

    @app.get("/api/streams")
    def get_streams(refresh: bool = False) -> list[dict]:
        nonlocal streams_cache
        if streams_cache is None or refresh:
            try:
                found = list_streams(profile.channel["channel_url"])
            except YouTubeError as error:
                raise HTTPException(status_code=502, detail=str(error))
            streams_cache = [
                {"video_id": s.video_id, "title": s.title,
                 "duration_seconds": s.duration_seconds,
                 "view_count": s.view_count}
                for s in found
            ]
        return streams_cache
```

`HTTPException` is already imported in this file (the other routes use it); if
not, add `from fastapi import HTTPException`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q`
Expected: PASS (all studio API tests, including the four new ones)

- [ ] **Step 5: Confirm FastAPI is still optional**

`youtube.py` must not import FastAPI, and the CLI must still run without it.

Run: `PYTHONPATH=src .venv/bin/python -c "import yt_shorts.youtube"` with FastAPI importable — it must not pull FastAPI in. Then confirm the whole suite:

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 498 passed (494 + 4)

- [ ] **Step 6: Update the documentation**

In `README.md`, add a short note under the studio section: the studio can list
a channel's streams (from its `channel_url`), and this uses yt-dlp — no API key.
In `CLAUDE.md`, one line: stream discovery is `youtube.list_streams`, a yt-dlp
call with an injected subprocess boundary, and it must not import FastAPI.

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py README.md CLAUDE.md
git commit -m "Serve a channel's streams from the studio"
```

---

## Note on the frontend

This plan stops at the API. The stream list needs a small piece of studio UI —
a panel that calls `GET /api/streams`, shows title/duration/view count in the
timing-tower style, and records a selection. That UI, and what "record a
selection" writes, are deliberately deferred: the selection only becomes
meaningful once D2 exists to consume a chosen stream. Building the picker before
its consumer would freeze a shape D2 might want changed. The API and the
discovery module are the testable, self-contained deliverable of D1; the picker
lands with D2, when there is something to pick *for*.

## Self-review notes

Checked against the spec:
- yt-dlp not API — Task 1, and the Global Constraints forbid a Google dependency
- fields id/title/duration/view_count — Task 1's `Stream` and tests
- newest-first order preserved — Task 1 `test_order_is_preserved`
- per-entry tolerance, YouTubeError not traceback — Task 1
- injected subprocess boundary — `runner` parameter, all tests use it
- `/api/streams` with in-session cache and deliberate refresh — Task 2
- yt-dlp-unavailable state — Task 2 `test_a_youtube_error_is_a_502`
- FastAPI stays optional — Task 2 Step 5
- ownership/multi-channel — no code; the channel URL comes from the profile
  already selected, which is the folder the operator is in

Deferred with reason (not gaps): the picker UI and what selection persists —
both belong with D2, noted above.
