# Studio Stream View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator a screen that shows a stream's transcript, activity curve and detected moments, and lets them pick a window and turn it into a clip — the half of the moment-detection rebuild that Plan A deliberately left unbuilt.

**Architecture:** A fourth client-side route level, `/{channel}/{event}/streams/{video_id}`, served by the existing SPA fallback with no backend routing change. Three new read routes serve the analysis, the stream transcript and a cost estimate; one new write route creates a clip from a chosen window. All pure geometry and formatting lives in new non-component modules (`streamTimeline.ts`, `momentList.ts`) so Vite's fast-refresh boundary stays component-only and the maths is unit-tested without rendering.

**Tech Stack:** FastAPI (studio only), React 19 + Mantine 9.4.2 + TypeScript, Vitest (jsdom) for pure modules, Playwright inside pytest for the integrated flow.

## Global Constraints

- **The screen must be fully useful with NO API key and NO detection run.** Transcript, search, curve, manual window selection and clip creation all work without a single model call. Detection only adds markers to a screen that already functions. A task that makes any of those five depend on `moments.json` existing is wrong.
- **Every screen must scroll to every element at a short viewport.** Hit list, zoom lane, transcript and player all reachable at 1280x600. `index.css` sets `body { overflow: hidden }`, so **every new full-height pane owns its own scroll container** — there is no document scroll to fall back on. Verified in a real browser before sign-off, not only in tests.
- **No fixed cap on how many moments a stream yields.** The operator rejected that explicitly. Filtering and sorting are the operator's tools; truncation is not.
- **`{video_id}` is validated by `pathnames.validate_segment(video_id, what="video id")` before any filesystem touch — provably, on every path.** The check may live in the route's own body OR as the unconditional first act of the specific function the route calls (the pattern `font_admin`, `channel_admin` and `stream_analysis` already follow, with the route left as a thin mapping of error kinds to status codes). What is forbidden is safety that holds only as an ACCIDENT OF CALL ORDER between unrelated functions — which is exactly what `POST …/streams/{video_id}/detect` relies on today, being safe only because `stream_transcribe._stream_dir` happens to run first. If a route calls something that does not validate — `clip_from_moment.create_clip`, for one — the route validates in its own body. Prove whichever you rely on with a removal check, not by reading.
- **Every new `/api` route is registered BEFORE the SPA fallback** (`api.py`'s `@app.get("/{full_path:path}")`) and before the four non-GET catch-alls that follow it. A route registered after them is unreachable and 404s instead of executing.
- **`api.py`'s module-docstring route table is hand-maintained and currently accurate.** Every new route is added to it in the same commit.
- **Two things are called `moments.json` and they are unrelated.** `<workspace|channel|event>/moments.json` is the excitement LEXICON (`lexicon_admin.py`). `<workspace>/streams/<video_id>/moments.json` is the detection ANALYSIS (`detect.ANALYSIS_FILENAME`). `/api/moments` is already the lexicon's. Never name a new symbol so the two can be confused: the analysis is "the analysis", and its component is `StreamScreen`/`HitList`, never `MomentsPanel` (there is already a `MomentsEditor.tsx` and it edits the lexicon).
- **`npm test` AND `npm run build` before every frontend commit**, and the regenerated `src/yt_shorts/studio/static/` is committed. `static/` is served to the operator and to every Playwright test; an unrebuilt change leaves both stale.
- `PYTHONPATH=src` is mandatory for every Python invocation. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` (~150 s). Run it in the FOREGROUND with a generous timeout.
- `python3 tools/lint.py` (NO `PYTHONPATH`) prints `All checks passed!` before every commit.
- The full suite is green before every commit.
- **No test may hit the network, read a real API key, import `anthropic`, or run a real Whisper decode or render.** `anthropic` IS installed in this venv now, so a test that wrongly reaches the real SDK would pass silently instead of failing loudly. A test that would spend money is a defect.
- The six SHA-256 hashes in `tests/test_event_layer_no_regression.py` must never be re-pinned; nothing here touches overlay rendering.
- Nothing outside `yt_shorts/studio/` may import FastAPI; no module-scope `import anthropic`.
- Docstrings and comments state *why*. Never claim a measurement that was not made.

## Two decisions this plan makes explicit rather than assumes

**1. The studio's "writes `edit.json` and nothing else" rule gains a second, documented carve-out.** `CLAUDE.md` states in three places that the only write path in `yt_shorts/studio/` is `editorial.save`, with one existing exception: a render the operator explicitly starts (`studio.jobs`). Creating a clip from a chosen window writes a new clip directory and a new `clip.json`, which is outside that boundary as written. The spec approved this route, so the rule is amended rather than violated — and the amendment is narrow: the studio may CREATE a clip the operator explicitly asked for, and still may never EDIT an existing event's `clip.json`, `transcript.json`, `sources.json` or rendered short. Task 8 writes that amendment into `CLAUDE.md` in the same commit as the route.

**2. The analysis stays workspace-scoped, and the route stays event-scoped.** `<workspace>/streams/<video_id>/moments.json` carries no channel or event, so two events analysing the same stream share one file. That is deliberate: a transcript and its analysis are properties of the STREAM, and re-deriving them per event would throw away an hour of Whisper decode for nothing. The event in the URL is how the operator navigates and which `EventLock` a write takes — not a scope on the data. Any task tempted to "fix" this by moving the analysis under the event is wrong.

---

## File structure

| File | Responsibility |
|---|---|
| `src/yt_shorts/studio/web/src/scopedApi.ts` | MODIFY — a fourth route level and its `Route` field |
| `src/yt_shorts/studio/web/src/streamTimeline.ts` | CREATE — pure geometry: seconds ↔ pixels, the zoom window, curve bucket lookup |
| `src/yt_shorts/studio/web/src/momentList.ts` | CREATE — pure list logic: sorting, category filtering, the label for a category |
| `src/yt_shorts/studio/web/src/components/StreamScreen.tsx` | CREATE — the screen: layout, data loading, the panes below |
| `src/yt_shorts/studio/web/src/components/HitList.tsx` | CREATE — the left pane |
| `src/yt_shorts/studio/web/src/components/StreamTimeline.tsx` | CREATE — overview strip + zoom lane |
| `src/yt_shorts/studio/web/src/components/TranscriptPane.tsx` | CREATE — transcript with search |
| `src/yt_shorts/studio/web/src/components/StreamPlayer.tsx` | CREATE — small player, expandable to an overlay |
| `src/yt_shorts/studio/web/src/Root.tsx` | MODIFY — dispatch the new screen |
| `src/yt_shorts/studio/web/src/api.ts` | MODIFY — clients for the four new routes |
| `src/yt_shorts/studio/api.py` | MODIFY — four new routes in the Streams group |
| `src/yt_shorts/stream_analysis.py` | CREATE — pure reader for the analysis and the stream transcript (no FastAPI), so the routes stay thin |
| `src/yt_shorts/estimate.py` | CREATE — pure token/cost estimation for a stream (no FastAPI, no `anthropic` at module scope) |

---

## Task 1: A fourth route level

**Files:**
- Modify: `src/yt_shorts/studio/web/src/scopedApi.ts`
- Test: `src/yt_shorts/studio/web/src/scopedApi.test.ts`

**Interfaces:**
- Produces: `Screen` gains `'stream'`; `Route` gains `videoId?: string`; `parseRoute('/erf/ev/streams/vid123')` → `{ screen: 'stream', channel: 'erf', event: 'ev', videoId: 'vid123' }`; `routePath({screen:'stream', channel, event, videoId})` → `/{channel}/{event}/streams/{videoId}` with each segment encoded.

`parseRoute`'s last line is an unconditional catch-all that returns the editor for ANY path of two or more segments, silently discarding segments 3+. The new branch must come BEFORE it.

- [ ] **Step 1: Write the failing tests**

Append to `src/yt_shorts/studio/web/src/scopedApi.test.ts`:

```ts
describe('parseRoute: the stream screen', () => {
  it('reads channel, event and video id from a four-segment path', () => {
    expect(parseRoute('/erf/n24-2026/streams/V9nVNEQNdR4')).toEqual({
      screen: 'stream', channel: 'erf', event: 'n24-2026', videoId: 'V9nVNEQNdR4',
    })
  })

  it('is not confused by a third segment that is not "streams"', () => {
    // The editor catch-all owns everything else, and must keep owning it.
    expect(parseRoute('/erf/n24-2026/something/else')).toEqual({
      screen: 'editor', channel: 'erf', event: 'n24-2026',
    })
  })

  it('falls back to the editor when the video id is missing', () => {
    expect(parseRoute('/erf/n24-2026/streams')).toEqual({
      screen: 'editor', channel: 'erf', event: 'n24-2026',
    })
  })

  it('decodes each segment', () => {
    expect(parseRoute('/erf/n24%202026/streams/vid%2D1')).toEqual({
      screen: 'stream', channel: 'erf', event: 'n24 2026', videoId: 'vid-1',
    })
  })

  it('round-trips through routePath', () => {
    const route = {
      screen: 'stream' as const, channel: 'erf', event: 'n24 2026', videoId: 'V9n',
    }
    expect(parseRoute(routePath(route))).toEqual(route)
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run, from `src/yt_shorts/studio/web/`: `npm test`
Expected: FAIL — the stream cases return `{ screen: 'editor', … }`.

- [ ] **Step 3: Implement**

In `scopedApi.ts`, extend the type and the two functions:

```ts
export type Screen = 'channels' | 'events' | 'editor' | 'settings' | 'logs' | 'stream'

export interface Route {
  screen: Screen
  channel?: string
  event?: string
  videoId?: string
}
```

In `parseRoute`, immediately BEFORE the final `return { screen: 'editor', … }`:

```ts
  // Before the editor catch-all, which owns every other path of two or more
  // segments: a fourth level only exists when the third segment is literally
  // "streams" AND a video id follows. `/a/b/streams` with nothing after it is
  // not a stream screen - there is no stream to show - so it falls through to
  // the editor rather than rendering a screen with an undefined video id.
  if (segments.length >= 4 && segments[2] === 'streams') {
    return {
      screen: 'stream', channel: segments[0], event: segments[1], videoId: segments[3],
    }
  }
```

In `routePath`, before its final editor return:

```ts
  if (route.screen === 'stream') {
    return (
      `/${encodeSegment(route.channel ?? '')}/${encodeSegment(route.event ?? '')}` +
      `/streams/${encodeSegment(route.videoId ?? '')}`
    )
  }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npm test`
Expected: all pass, including the existing `scopedApi` cases.

- [ ] **Step 5: Commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run build && cd -
git add src/yt_shorts/studio/web/src/scopedApi.ts \
        src/yt_shorts/studio/web/src/scopedApi.test.ts \
        src/yt_shorts/studio/static
git commit -m "feat(studio-web): a fourth route level for a stream"
```

---

## Task 2: Reading an analysis and a stream transcript

**Files:**
- Create: `src/yt_shorts/stream_analysis.py`
- Test: `tests/test_stream_analysis.py`

**Interfaces:**
- Consumes: `detect.analysis_path(workspace_dir, video_id) -> Path`, `pathnames.validate_segment(value, what=...)`.
- Produces: `stream_analysis.AnalysisError(RuntimeError)` with a `kind: str` attribute (`"not_found"` or `"unreadable"`); `stream_analysis.read_analysis(workspace_dir, video_id) -> dict`; `stream_analysis.read_transcript(workspace_dir, video_id) -> dict`; `stream_analysis.TRANSCRIPT_FILENAME = "transcript.json"`.

Pure, no FastAPI — like `workspace_listing.py` and the other admin modules. The routes in Task 3 are a thin mapping of `kind` to a status code.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stream_analysis.py`:

```python
import json

import pytest

from yt_shorts import stream_analysis


def write(root, video_id, name, payload):
    directory = root / "streams" / video_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


class TestReadAnalysis:
    def test_returns_the_written_payload(self, tmp_path):
        write(tmp_path, "vid123", "moments.json",
              {"video_id": "vid123", "engine": "lexicon", "moments": []})
        assert stream_analysis.read_analysis(tmp_path, "vid123")["engine"] == "lexicon"

    def test_a_missing_analysis_is_not_found_not_a_crash(self, tmp_path):
        # The screen must work before detection has ever run, so "no analysis
        # yet" is an ordinary answer the caller renders as an empty hit list -
        # not an error page.
        with pytest.raises(stream_analysis.AnalysisError) as caught:
            stream_analysis.read_analysis(tmp_path, "vid123")
        assert caught.value.kind == "not_found"

    def test_a_corrupt_analysis_is_distinguishable_from_a_missing_one(self, tmp_path):
        directory = tmp_path / "streams" / "vid123"
        directory.mkdir(parents=True)
        (directory / "moments.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(stream_analysis.AnalysisError) as caught:
            stream_analysis.read_analysis(tmp_path, "vid123")
        assert caught.value.kind == "unreadable"

    def test_a_traversing_video_id_is_refused_before_any_read(self, tmp_path):
        with pytest.raises(ValueError):
            stream_analysis.read_analysis(tmp_path, "../../auth")


class TestReadTranscript:
    def test_returns_the_words(self, tmp_path):
        write(tmp_path, "vid123", "transcript.json",
              {"video_id": "vid123", "duration_seconds": 60.0,
               "words": [{"start": 0.0, "end": 0.5, "text": " go"}],
               "missing_chunks": []})
        data = stream_analysis.read_transcript(tmp_path, "vid123")
        assert data["words"][0]["text"] == " go"

    def test_a_missing_transcript_is_not_found(self, tmp_path):
        with pytest.raises(stream_analysis.AnalysisError) as caught:
            stream_analysis.read_transcript(tmp_path, "vid123")
        assert caught.value.kind == "not_found"

    def test_a_traversing_video_id_is_refused_before_any_read(self, tmp_path):
        with pytest.raises(ValueError):
            stream_analysis.read_transcript(tmp_path, "../../auth")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_stream_analysis.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.stream_analysis'`

- [ ] **Step 3: Implement**

Create `src/yt_shorts/stream_analysis.py`:

```python
"""Read a stream's derived files - the analysis and the transcript.

Pure and FastAPI-free, like `workspace_listing.py` and the `*_admin` modules:
the studio's routes are a thin mapping of `AnalysisError.kind` onto a status
code, and nothing here knows that HTTP exists.

Both files live under `<workspace>/streams/<video_id>/`, NOT under an event.
That is deliberate and is not a scoping oversight: a transcript and the
analysis derived from it are properties of the STREAM, so two events that
look at the same stream share them rather than each paying an hour of Whisper
decode for a second copy. The event in the studio's URL is how the operator
navigates and which EventLock a write takes - it is not a scope on this data.

A MISSING file and an UNREADABLE one are different answers, which is why
`kind` exists. The screen is designed to work before detection has ever run,
so "no analysis yet" is an ordinary state the caller renders as an empty hit
list; a corrupt file is a real fault the operator has to be told about.
"""

from __future__ import annotations

import json
from pathlib import Path

from .detect import ANALYSIS_FILENAME, analysis_path
from .pathnames import validate_segment

TRANSCRIPT_FILENAME = "transcript.json"


class AnalysisError(RuntimeError):
    """A stream's derived file could not be read. `kind` says which way."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _read(path: Path, what: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AnalysisError("not_found", f"no {what} for this stream yet") from None
    except OSError as error:
        raise AnalysisError("unreadable", f"cannot read the {what}: {error}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise AnalysisError("unreadable", f"the {what} is not valid JSON: {error}") from None


def read_analysis(workspace_dir: str | Path, video_id: str) -> dict:
    """The detection analysis, `streams/<video_id>/moments.json`.

    `analysis_path` validates the video id itself; this call is what makes the
    validation happen BEFORE any filesystem touch here too.
    """
    return _read(analysis_path(workspace_dir, video_id), "analysis")


def read_transcript(workspace_dir: str | Path, video_id: str) -> dict:
    """The whole stream transcript, `streams/<video_id>/transcript.json`.

    Served whole rather than paged: ~2 MB for eight hours over localhost, which
    makes client-side search instant and paging a complication that buys the
    operator nothing.
    """
    validate_segment(video_id, what="video id")
    path = Path(workspace_dir) / "streams" / video_id / TRANSCRIPT_FILENAME
    return _read(path, "transcript")


__all__ = ["ANALYSIS_FILENAME", "TRANSCRIPT_FILENAME", "AnalysisError",
           "read_analysis", "read_transcript"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_stream_analysis.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, full suite, commit**

```bash
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q
git add src/yt_shorts/stream_analysis.py tests/test_stream_analysis.py
git commit -m "feat(studio): read a stream's analysis and transcript"
```

---

## Task 3: The two read routes

**Files:**
- Modify: `src/yt_shorts/studio/api.py` (the `# ---- Streams` group, and the module docstring's route table)
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Consumes: `stream_analysis.read_analysis`, `stream_analysis.read_transcript`, `stream_analysis.AnalysisError`.
- Produces: `GET /api/channels/{channel}/events/{event}/streams/{video_id}/moments` → the analysis dict, or `{"video_id": …, "engine": null, "moments": [], "activity": [], "missing_windows": [], "missing_chunks": [], "duration_seconds": 0.0, "stream_title": "", "created_at": null}` with **200** when none exists yet; `GET …/streams/{video_id}/transcript` → the transcript dict, **404** when none exists.

The two absent-file cases are deliberately different, and this is the task's one real decision. **A missing ANALYSIS is 200 with an empty shape**, because the screen is specified to work before detection has ever run and a 404 would make the client treat an ordinary state as a failure. **A missing TRANSCRIPT is 404**, because without it the screen genuinely cannot show its main pane, and the client must say so rather than render an empty document as though the stream were silent.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_studio_api.py`:

```python
class TestStreamAnalysisRoutes:
    def _write(self, root, video_id, name, payload):
        directory = root / "streams" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_serves_a_written_analysis(self, client, workspace_root):
        self._write(workspace_root, "vid123", "moments.json",
                    {"video_id": "vid123", "engine": "lexicon", "moments": [],
                     "activity": [0.5], "missing_windows": [], "missing_chunks": [],
                     "duration_seconds": 60.0, "stream_title": "Race",
                     "created_at": "2026-07-29T10:00:00+00:00"})
        response = client.get(f"{EV}/streams/vid123/moments")
        assert response.status_code == 200
        assert response.json()["engine"] == "lexicon"

    def test_no_analysis_yet_is_an_empty_analysis_not_a_404(self, client):
        # The screen works before detection has ever run; a 404 here would make
        # the client render a failure for an ordinary state.
        response = client.get(f"{EV}/streams/never-detected/moments")
        assert response.status_code == 200
        body = response.json()
        assert body["moments"] == [] and body["engine"] is None

    def test_a_corrupt_analysis_is_a_500_not_an_empty_one(self, client, workspace_root):
        directory = workspace_root / "streams" / "vid123"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "moments.json").write_text("{not json", encoding="utf-8")
        assert client.get(f"{EV}/streams/vid123/moments").status_code == 500

    def test_serves_the_transcript(self, client, workspace_root):
        self._write(workspace_root, "vid123", "transcript.json",
                    {"video_id": "vid123", "duration_seconds": 60.0,
                     "words": [{"start": 0.0, "end": 0.5, "text": " go"}],
                     "missing_chunks": []})
        response = client.get(f"{EV}/streams/vid123/transcript")
        assert response.status_code == 200
        assert response.json()["words"][0]["text"] == " go"

    def test_a_missing_transcript_is_a_404(self, client):
        # Unlike the analysis: without a transcript the screen's main pane has
        # nothing to show, and rendering an empty document would look like a
        # silent stream rather than a missing file.
        assert client.get(f"{EV}/streams/vid123/transcript").status_code == 404

    def test_a_traversing_video_id_is_refused(self, client):
        assert client.get(f"{EV}/streams/..%2F..%2Fauth/moments").status_code == 400
        assert client.get(f"{EV}/streams/..%2F..%2Fauth/transcript").status_code == 400
```

If `tests/test_studio_api.py` has no `workspace_root` fixture, add one returning the same session-scoped root `tests/conftest.py`'s `_isolated_resolved_workspace` pins, and say in a comment that it must be that root and not a fresh `tmp_path`, or the routes will read a different workspace than the test writes to.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k StreamAnalysisRoutes`
Expected: FAIL — 404 from the SPA fallback, because the routes do not exist.

- [ ] **Step 3: Implement**

In `src/yt_shorts/studio/api.py`, inside the `# ---- Streams` group and BEFORE the SPA fallback, after `post_detect`:

```python
    @app.get(EV + "/streams/{video_id}/moments")
    def get_stream_analysis(channel: str, event: str, video_id: str) -> dict:
        """The detection analysis, or an EMPTY one when detection never ran.

        Deliberately not a 404 for the absent case: this screen is specified to
        be useful with no API key and no detection at all, so "not analysed
        yet" is an ordinary state the client renders as an empty hit list. A
        404 would push the client into its error path for a normal condition.
        A corrupt file IS a fault and does not get the same treatment.
        """
        _load_profile(channel, event)
        root = _resolve_workspace().root
        try:
            return stream_analysis.read_analysis(root, video_id)
        except ValueError as error:            # a bad path segment
            raise HTTPException(status_code=400, detail=str(error)) from error
        except stream_analysis.AnalysisError as error:
            if error.kind == "not_found":
                return {"video_id": video_id, "stream_title": "", "engine": None,
                        "created_at": None, "duration_seconds": 0.0, "activity": [],
                        "moments": [], "missing_windows": [], "missing_chunks": []}
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.get(EV + "/streams/{video_id}/transcript")
    def get_stream_transcript(channel: str, event: str, video_id: str) -> dict:
        """The whole stream transcript. 404 when there is none.

        The opposite of the analysis route above, on purpose: without a
        transcript this screen's main pane has nothing to show, and an empty
        document would read as a silent stream rather than a missing file.
        """
        _load_profile(channel, event)
        root = _resolve_workspace().root
        try:
            return stream_analysis.read_transcript(root, video_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except stream_analysis.AnalysisError as error:
            status = 404 if error.kind == "not_found" else 500
            raise HTTPException(status_code=status, detail=str(error)) from error
```

Add `from .. import stream_analysis` to the imports at the top of `api.py`, and add both routes to the module docstring's route table beside the existing `…/streams` entries.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, full suite, commit**

```bash
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "feat(studio): serve a stream's analysis and transcript"
```

---

## Task 4: The pure logic behind the screen

**Files:**
- Create: `src/yt_shorts/studio/web/src/streamTimeline.ts`
- Create: `src/yt_shorts/studio/web/src/momentList.ts`
- Test: `src/yt_shorts/studio/web/src/streamTimeline.test.ts`
- Test: `src/yt_shorts/studio/web/src/momentList.test.ts`

**Interfaces:**
- Produces, from `streamTimeline.ts`:
  - `export interface Zoom { start: number; end: number }`
  - `export function clampZoom(zoom: Zoom, duration: number, minSpan?: number): Zoom`
  - `export function secondsToFraction(seconds: number, zoom: Zoom): number`
  - `export function fractionToSeconds(fraction: number, zoom: Zoom): number`
  - `export function zoomAround(centre: number, span: number, duration: number): Zoom`
  - `export function curveBucket(seconds: number, activity: number[], duration: number): number`
- Produces, from `momentList.ts`:
  - `export interface Moment { start: number; end: number; category: string; score: number; reason: string; hook_suggestion: string }`
  - `export const CATEGORY_LABELS: Record<string, string>`
  - `export type SortKey = 'score' | 'time'`
  - `export function sortMoments(moments: Moment[], key: SortKey): Moment[]`
  - `export function filterMoments(moments: Moment[], categories: Set<string>): Moment[]`
  - `export function categoryLabel(category: string): string`

Both are non-component modules for the reason `CLAUDE.md` already gives for `words.ts`/`format.ts`: Vite's fast-refresh boundary stays component-only, and the maths is unit-tested without rendering anything.

**The screen's whole spatial problem, and why `zoomAround` exists.** An endurance stream is six to eight hours. Over a 1200-pixel-wide overview that is ~24 seconds per pixel, so a 20-second clip is under one pixel — an overview alone cannot express a boundary. The overview locates, the zoom lane edits, and `zoomAround` is what carries a click on the first into a window on the second.

- [ ] **Step 1: Write the failing tests**

Create `src/yt_shorts/studio/web/src/streamTimeline.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import {
  clampZoom, curveBucket, fractionToSeconds, secondsToFraction, zoomAround,
} from './streamTimeline'

describe('secondsToFraction / fractionToSeconds', () => {
  it('maps the zoom window onto 0..1', () => {
    expect(secondsToFraction(150, { start: 100, end: 200 })).toBeCloseTo(0.5)
    expect(fractionToSeconds(0.5, { start: 100, end: 200 })).toBeCloseTo(150)
  })

  it('round-trips', () => {
    const zoom = { start: 3600, end: 3720 }
    expect(fractionToSeconds(secondsToFraction(3660, zoom), zoom)).toBeCloseTo(3660)
  })

  it('returns 0 rather than dividing by zero on a collapsed window', () => {
    expect(secondsToFraction(10, { start: 10, end: 10 })).toBe(0)
  })
})

describe('clampZoom', () => {
  it('keeps the window inside the stream', () => {
    expect(clampZoom({ start: -50, end: 70 }, 600)).toEqual({ start: 0, end: 120 })
    expect(clampZoom({ start: 580, end: 700 }, 600)).toEqual({ start: 480, end: 600 })
  })

  it('refuses to collapse below the minimum span', () => {
    // A window narrower than this cannot be dragged accurately, and a zero-width
    // one divides by zero in secondsToFraction.
    const zoom = clampZoom({ start: 100, end: 100.5 }, 600, 10)
    expect(zoom.end - zoom.start).toBeCloseTo(10)
  })

  it('gives the whole stream when it is shorter than the requested span', () => {
    expect(clampZoom({ start: 0, end: 120 }, 45)).toEqual({ start: 0, end: 45 })
  })
})

describe('zoomAround', () => {
  it('centres the span on the given second', () => {
    expect(zoomAround(3660, 120, 28800)).toEqual({ start: 3600, end: 3720 })
  })

  it('shifts rather than overhanging at the start of the stream', () => {
    expect(zoomAround(10, 120, 28800)).toEqual({ start: 0, end: 120 })
  })

  it('shifts rather than overhanging at the end of the stream', () => {
    expect(zoomAround(28795, 120, 28800)).toEqual({ start: 28680, end: 28800 })
  })
})

describe('curveBucket', () => {
  it('indexes the activity array by the stream position', () => {
    // The curve is one value per 60 s (moments.activity_curve's step).
    const activity = [0.1, 0.9, 0.4]
    expect(curveBucket(0, activity, 180)).toBe(0)
    expect(curveBucket(90, activity, 180)).toBe(1)
    expect(curveBucket(179, activity, 180)).toBe(2)
  })

  it('clamps past the end instead of reading off the array', () => {
    // The curve can be one bucket longer or shorter than duration/60 implies -
    // activity_curve derives its length from the last WORD, not the stream, and
    // appends a trailing bucket when that lands on an exact multiple.
    expect(curveBucket(10_000, [0.1, 0.2], 180)).toBe(1)
  })

  it('returns -1 for an empty curve so a caller can skip drawing', () => {
    expect(curveBucket(10, [], 180)).toBe(-1)
  })
})
```

Create `src/yt_shorts/studio/web/src/momentList.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { categoryLabel, filterMoments, sortMoments } from './momentList'

const m = (start: number, score: number, category = 'incident') => ({
  start, end: start + 20, category, score, reason: 'x', hook_suggestion: '',
})

describe('sortMoments', () => {
  it('orders by score, strongest first', () => {
    const sorted = sortMoments([m(10, 3), m(20, 9), m(30, 6)], 'score')
    expect(sorted.map((x) => x.score)).toEqual([9, 6, 3])
  })

  it('orders by time, earliest first', () => {
    const sorted = sortMoments([m(30, 6), m(10, 3), m(20, 9)], 'time')
    expect(sorted.map((x) => x.start)).toEqual([10, 20, 30])
  })

  it('does not mutate its input', () => {
    const input = [m(30, 6), m(10, 3)]
    sortMoments(input, 'time')
    expect(input[0].start).toBe(30)
  })

  it('breaks a score tie by time so the order is stable to look at', () => {
    const sorted = sortMoments([m(30, 5), m(10, 5)], 'score')
    expect(sorted.map((x) => x.start)).toEqual([10, 30])
  })
})

describe('filterMoments', () => {
  it('keeps only the selected categories', () => {
    const list = [m(10, 5, 'incident'), m(20, 5, 'reaction')]
    expect(filterMoments(list, new Set(['incident']))).toHaveLength(1)
  })

  it('an empty selection means no filter, not no results', () => {
    // Unticking every box must not look like "the stream has nothing in it".
    const list = [m(10, 5, 'incident'), m(20, 5, 'reaction')]
    expect(filterMoments(list, new Set())).toHaveLength(2)
  })
})

describe('categoryLabel', () => {
  it('renders the five known categories readably', () => {
    expect(categoryLabel('start_finish')).toBe('Start / finish')
    expect(categoryLabel('race_control')).toBe('Race control')
  })

  it('passes an unknown category through rather than hiding it', () => {
    // A category this client does not know about is still a real detection;
    // showing the raw value beats showing nothing.
    expect(categoryLabel('weather')).toBe('weather')
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run, from `src/yt_shorts/studio/web/`: `npm test`
Expected: FAIL — both modules do not exist.

- [ ] **Step 3: Implement `streamTimeline.ts`**

```ts
/**
 * Pure geometry for the stream screen: seconds to pixels and back.
 *
 * No React here, deliberately - the same rule words.ts and format.ts follow, so
 * Vite's fast-refresh boundary stays component-only and this maths is tested
 * without rendering anything.
 *
 * The screen has two lanes because one cannot do the job. An endurance stream
 * is six to eight hours; over a 1200-pixel overview that is ~24 seconds per
 * pixel, so a 20-second clip is under a single pixel and no boundary can be
 * expressed there. The overview LOCATES, the zoom lane EDITS, and zoomAround is
 * the bridge between them.
 */

export interface Zoom {
  start: number
  end: number
}

/** The narrowest zoom window that can still be dragged accurately. */
export const MIN_ZOOM_SECONDS = 10

export function secondsToFraction(seconds: number, zoom: Zoom): number {
  const span = zoom.end - zoom.start
  // A collapsed window would divide by zero and paint NaN into a style
  // attribute, which renders as an element that silently vanishes.
  if (span <= 0) return 0
  return (seconds - zoom.start) / span
}

export function fractionToSeconds(fraction: number, zoom: Zoom): number {
  return zoom.start + fraction * (zoom.end - zoom.start)
}

export function clampZoom(zoom: Zoom, duration: number, minSpan = MIN_ZOOM_SECONDS): Zoom {
  // A stream shorter than the requested span is shown whole rather than
  // padded with time that does not exist.
  if (duration <= minSpan) return { start: 0, end: Math.max(duration, 0) }
  const span = Math.min(Math.max(zoom.end - zoom.start, minSpan), duration)
  const start = Math.min(Math.max(zoom.start, 0), duration - span)
  return { start, end: start + span }
}

export function zoomAround(centre: number, span: number, duration: number): Zoom {
  return clampZoom({ start: centre - span / 2, end: centre + span / 2 }, duration, span)
}

/** One curve value per 60 s - moments.activity_curve's own step. */
export const CURVE_STEP_SECONDS = 60

export function curveBucket(seconds: number, activity: number[], duration: number): number {
  // -1 rather than 0 for an empty curve: 0 is a real bucket, and a caller that
  // cannot tell the two apart draws a bar for a stream with no curve at all.
  if (activity.length === 0) return -1
  void duration
  const index = Math.floor(seconds / CURVE_STEP_SECONDS)
  // The curve's length is derived from the last WORD, not from the stream's
  // duration, and activity_curve appends a trailing bucket when that word ends
  // on an exact multiple of the step - so it is neither safe to assume
  // length === ceil(duration/step) nor to index past the end.
  return Math.min(Math.max(index, 0), activity.length - 1)
}
```

- [ ] **Step 4: Implement `momentList.ts`**

```ts
/**
 * Pure list logic for the hit list: sorting, filtering, category labels.
 *
 * Non-component for the same reason as streamTimeline.ts. Note the name: this
 * is about DETECTED moments, not the excitement lexicon that MomentsEditor.tsx
 * edits - two different things share the filename moments.json on disk, and
 * conflating them in a component name is how the next maintainer loses an hour.
 */

export interface Moment {
  start: number
  end: number
  category: string
  score: number
  reason: string
  hook_suggestion: string
}

export type SortKey = 'score' | 'time'

/** The channel's own order of importance, from the design. */
export const CATEGORY_ORDER = [
  'start_finish', 'incident', 'highlight', 'race_control', 'reaction',
] as const

export const CATEGORY_LABELS: Record<string, string> = {
  start_finish: 'Start / finish',
  incident: 'Incident',
  highlight: 'Highlight',
  race_control: 'Race control',
  reaction: 'Reaction',
}

export function categoryLabel(category: string): string {
  // An unknown category is still a real detection - showing the raw value beats
  // showing nothing, and beats silently dropping the row.
  return CATEGORY_LABELS[category] ?? category
}

export function sortMoments(moments: Moment[], key: SortKey): Moment[] {
  // A copy, never in place: the caller holds this array in state, and sorting
  // it under React would mutate state without a re-render.
  const copy = [...moments]
  if (key === 'time') return copy.sort((a, b) => a.start - b.start)
  // Time breaks a score tie, so equal-scoring rows keep a stable, meaningful
  // order instead of whatever the engine happened to emit.
  return copy.sort((a, b) => b.score - a.score || a.start - b.start)
}

export function filterMoments(moments: Moment[], categories: Set<string>): Moment[] {
  // An empty selection means "no filter", not "nothing matches": unticking every
  // box must not look like a stream with nothing in it.
  if (categories.size === 0) return moments
  return moments.filter((moment) => categories.has(moment.category))
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `npm test`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run build && cd -
git add src/yt_shorts/studio/web/src/streamTimeline.ts \
        src/yt_shorts/studio/web/src/streamTimeline.test.ts \
        src/yt_shorts/studio/web/src/momentList.ts \
        src/yt_shorts/studio/web/src/momentList.test.ts \
        src/yt_shorts/studio/static
git commit -m "feat(studio-web): the pure geometry and list logic for the stream screen"
```

---

## Task 5: The screen itself, useful with no detection at all

**Files:**
- Create: `src/yt_shorts/studio/web/src/components/StreamScreen.tsx`
- Create: `src/yt_shorts/studio/web/src/components/TranscriptPane.tsx`
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Modify: `src/yt_shorts/studio/web/src/Root.tsx`
- Modify: `src/yt_shorts/studio/web/src/components/StreamPanel.tsx` (each row becomes a link)

**Interfaces:**
- Consumes: `parseRoute`/`routePath`/`navigate`, `eventBase(channel, event)`, `NavScreen` (`{ crumbs: Crumb[]; title: string; subtitle?: string; children: ReactNode }`), `ApiError`, `asJson<T>`, `formatStreamDuration`.
- Produces, from `api.ts`:
  - `export interface StreamAnalysis { video_id: string; stream_title: string; engine: string | null; created_at: string | null; duration_seconds: number; activity: number[]; moments: Moment[]; missing_windows: number[]; missing_chunks: number[] }`
  - `export interface StreamTranscript { video_id: string; duration_seconds: number; words: Word[]; missing_chunks: number[] }`
  - `export function getStreamAnalysis(channel: string, event: string, videoId: string): Promise<StreamAnalysis>`
  - `export function getStreamTranscript(channel: string, event: string, videoId: string): Promise<StreamTranscript>`
- Produces, from `TranscriptPane.tsx`: `export function TranscriptPane(props: { words: Word[]; currentTime: number; onSeek: (seconds: number) => void }): JSX.Element`
- Produces, from `StreamScreen.tsx`: `export function StreamScreen(props: { channel: string; event: string; videoId: string }): JSX.Element`

**These two clients take `channel`/`event` as ARGUMENTS rather than using `eventScope()`.** Every existing event-scoped call in `api.ts` reads a module-level `scope` that `App.tsx` sets during render (`api.ts`'s `eventScope()` throws `'event-scoped API called before the editor scope was set'` when it is null). `StreamScreen` is a sibling of `App`, not a child, so that scope is never set for it — a call through `eventScope()` would throw on mount. Passing the two segments explicitly is the smaller change and removes the ordering hazard entirely.

**This task delivers a screen that is already worth opening**: the stream's transcript, searchable, with a seek callback. No analysis, no player, no timeline yet — and nothing here may depend on `moments.json` existing.

- [ ] **Step 1: Add the API clients**

In `src/yt_shorts/studio/web/src/api.ts`, beside the existing `listStreams`/`startDetect`:

```ts
export interface Moment {
  start: number
  end: number
  category: string
  score: number
  reason: string
  hook_suggestion: string
}

export interface StreamAnalysis {
  video_id: string
  stream_title: string
  engine: string | null
  created_at: string | null
  duration_seconds: number
  activity: number[]
  moments: Moment[]
  missing_windows: number[]
  missing_chunks: number[]
}

export interface StreamTranscript {
  video_id: string
  duration_seconds: number
  words: Word[]
  missing_chunks: number[]
}

// channel/event are parameters, not eventScope(): the stream screen mounts as a
// sibling of App, which is the only place that sets the module scope, so a
// scoped call would throw on mount instead of fetching.
export function getStreamAnalysis(
  channel: string, event: string, videoId: string,
): Promise<StreamAnalysis> {
  return fetch(
    `${eventBase(channel, event)}/streams/${encodeURIComponent(videoId)}/moments`,
  ).then(asJson<StreamAnalysis>)
}

export function getStreamTranscript(
  channel: string, event: string, videoId: string,
): Promise<StreamTranscript> {
  return fetch(
    `${eventBase(channel, event)}/streams/${encodeURIComponent(videoId)}/transcript`,
  ).then(asJson<StreamTranscript>)
}
```

Import `eventBase` from `./scopedApi` if it is not already imported.

- [ ] **Step 2: Write `TranscriptPane.tsx`**

```tsx
import { useMemo, useState } from 'react'
import { Badge, Box, Group, ScrollArea, Stack, Text, TextInput } from '@mantine/core'

import { formatStreamDuration } from '../format'
import type { Word } from '../api'

const GROUP_SECONDS = 12

interface Line {
  start: number
  text: string
}

/**
 * The stream transcript, grouped into readable lines and searchable in place.
 *
 * Grouped at the same ~12 s the detector uses for its own line numbering, so a
 * moment's span and a transcript line mean the same unit of text on screen as
 * they do in the model's answer.
 *
 * The whole transcript is in memory - ~2 MB for eight hours, served whole by
 * design - so search is a filter over an array and needs no request, no paging
 * and no debounce beyond what typing already gives.
 */
export function TranscriptPane(
  { words, currentTime, onSeek }: {
    words: Word[]
    currentTime: number
    onSeek: (seconds: number) => void
  },
) {
  const [query, setQuery] = useState('')

  const lines = useMemo(() => {
    const out: Line[] = []
    let current: Line | null = null
    for (const word of words) {
      if (!current || word.end - current.start > GROUP_SECONDS) {
        current = { start: word.start, text: '' }
        out.push(current)
      }
      // Joined with "" because a decoder token carries its own leading space -
      // the same rule captions.py relies on, and why "C.L.R." renders correctly.
      current.text += word.text
    }
    return out
  }, [words])

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return lines
    return lines.filter((line) => line.text.toLowerCase().includes(needle))
  }, [lines, query])

  return (
    <Stack gap="xs" style={{ flex: 1, minHeight: 0 }}>
      <Group justify="space-between" wrap="nowrap">
        <TextInput
          placeholder="Search the transcript"
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
          style={{ flex: 1 }}
        />
        <Badge variant="light" color="steel">
          {shown.length} / {lines.length}
        </Badge>
      </Group>
      {lines.length === 0 && (
        <Text c="dimmed" size="sm">This stream has no transcript words.</Text>
      )}
      {lines.length > 0 && shown.length === 0 && (
        <Text c="dimmed" size="sm">Nothing in the transcript matches “{query}”.</Text>
      )}
      <ScrollArea style={{ flex: 1 }} offsetScrollbars>
        <Stack gap={2}>
          {shown.map((line) => {
            const active = currentTime >= line.start && currentTime < line.start + GROUP_SECONDS
            return (
              <Box
                key={line.start}
                onClick={() => onSeek(line.start)}
                style={{
                  cursor: 'pointer',
                  padding: '2px 6px',
                  borderRadius: 4,
                  background: active ? 'var(--mantine-color-steel-light)' : undefined,
                }}
              >
                <Text component="span" size="xs" c="dimmed" mr={8}>
                  {formatStreamDuration(line.start)}
                </Text>
                <Text component="span" size="sm">{line.text.trim()}</Text>
              </Box>
            )
          })}
        </Stack>
      </ScrollArea>
    </Stack>
  )
}
```

- [ ] **Step 3: Write `StreamScreen.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { Alert, Button, Center, Group, Loader, Stack, Text } from '@mantine/core'

import { ApiError, getStreamAnalysis, getStreamTranscript } from '../api'
import type { StreamAnalysis, StreamTranscript } from '../api'
import { NavScreen } from './NavScreen'
import { TranscriptPane } from './TranscriptPane'
import { routePath } from '../scopedApi'

/**
 * One stream: its transcript, its activity curve and its detected moments.
 *
 * Reachable at /{channel}/{event}/streams/{video_id}, and the ORDER of what it
 * loads is the design. The transcript is what the screen is for; the analysis
 * is an overlay on it. A stream that has never been analysed still opens, still
 * searches and still lets a window be picked by hand - so the analysis failing
 * to exist is not an error state here, it is the ordinary starting state.
 */
export function StreamScreen({ channel, event, videoId }: {
  channel: string
  event: string
  videoId: string
}) {
  const [transcript, setTranscript] = useState<StreamTranscript | null>(null)
  const [analysis, setAnalysis] = useState<StreamAnalysis | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [currentTime, setCurrentTime] = useState(0)

  function load() {
    setError(null)
    getStreamTranscript(channel, event, videoId)
      .then(setTranscript)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
    // The analysis is loaded independently and its failure is NOT surfaced as a
    // screen error: the route answers 200 with an empty analysis when detection
    // has never run, and anything worse still leaves a usable transcript.
    getStreamAnalysis(channel, event, videoId).then(setAnalysis).catch(() => setAnalysis(null))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel, event, videoId])

  const title = analysis?.stream_title || videoId

  return (
    <NavScreen
      crumbs={[
        { label: 'Channels', path: routePath({ screen: 'channels' }) },
        { label: channel, path: routePath({ screen: 'events', channel }) },
        { label: event, path: routePath({ screen: 'editor', channel, event }) },
      ]}
      title={title}
      subtitle={videoId}
    >
      {error && (
        <Alert color="red" title="Could not load this stream">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{error}</Text>
            <Text size="sm" c="dimmed">
              A stream has a transcript only after it has been transcribed. Start
              a detection run from the event screen’s Streams tab, which
              transcribes first.
            </Text>
            <Button size="xs" variant="light" onClick={load}>Retry</Button>
          </Stack>
        </Alert>
      )}
      {!error && !transcript && (
        <Center h="100%">
          <Group gap="xs">
            <Loader color="steel" />
            <Text c="dimmed">Loading the transcript…</Text>
          </Group>
        </Center>
      )}
      {!error && transcript && (
        <TranscriptPane
          words={transcript.words}
          currentTime={currentTime}
          onSeek={setCurrentTime}
        />
      )}
    </NavScreen>
  )
}
```

- [ ] **Step 4: Dispatch it in `Root.tsx`**

Add, before the `events` arm:

```tsx
  if (route.screen === 'stream' && route.channel && route.event && route.videoId) {
    return (
      <StreamScreen
        key={`${route.channel}/${route.event}/${route.videoId}`}
        channel={route.channel}
        event={route.event}
        videoId={route.videoId}
      />
    )
  }
```

The `key` remounts the screen when the operator moves to another stream, for the same reason the editor carries one: state belonging to the previous stream must not survive into the next.

- [ ] **Step 5: Make each stream row a link**

In `StreamPanel.tsx`, wrap the row's title so the whole row navigates. Keep the existing Detect button working and stop the click from bubbling into the navigation:

```tsx
              <Text
                fw={500}
                style={{ cursor: 'pointer' }}
                onClick={() => navigate(routePath({
                  screen: 'stream', channel, event, videoId: stream.video_id,
                }))}
              >
                {stream.title}
              </Text>
```

`StreamPanel` already imports `navigate` and `routePath`. It needs `channel` and `event` — thread them from `App.tsx`, which already holds both.

- [ ] **Step 6: Verify and commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run lint && npm run build && cd -
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "feat(studio-web): a stream screen with a searchable transcript"
```

---

## Task 6: The overview strip and the zoom lane

**Files:**
- Create: `src/yt_shorts/studio/web/src/components/StreamTimeline.tsx`
- Modify: `src/yt_shorts/studio/web/src/components/StreamScreen.tsx`

**Interfaces:**
- Consumes: `Zoom`, `clampZoom`, `zoomAround`, `secondsToFraction`, `fractionToSeconds`, `CURVE_STEP_SECONDS` from `../streamTimeline`; `Moment` from `../momentList`.
- Produces: `export function StreamTimeline(props: { duration: number; activity: number[]; moments: Moment[]; zoom: Zoom; selection: { start: number; end: number } | null; onZoomChange: (zoom: Zoom) => void; onSelectionChange: (selection: { start: number; end: number }) => void }): JSX.Element`

**Why two lanes and not one slider.** Over an eight-hour stream a 1200-pixel overview is ~24 seconds per pixel: a clip is under a single pixel, so boundaries cannot be set there. The overview answers "where in the stream", the zoom lane answers "exactly which seconds". Clicking the overview calls `zoomAround`; dragging in the zoom lane sets the selection. A single full-width timeline was the first design and it does not survive an endurance stream.

- [ ] **Step 1: Write the component**

```tsx
import { useRef } from 'react'
import { Box, Group, Stack, Text } from '@mantine/core'

import { formatStreamDuration } from '../format'
import type { Moment } from '../momentList'
import {
  CURVE_STEP_SECONDS, fractionToSeconds, secondsToFraction, zoomAround,
} from '../streamTimeline'
import type { Zoom } from '../streamTimeline'

const ZOOM_SPAN_SECONDS = 180

const CATEGORY_COLOUR: Record<string, string> = {
  start_finish: 'var(--mantine-color-grape-6)',
  incident: 'var(--mantine-color-red-6)',
  highlight: 'var(--mantine-color-yellow-6)',
  race_control: 'var(--mantine-color-blue-6)',
  reaction: 'var(--mantine-color-teal-6)',
}

function colourFor(category: string): string {
  return CATEGORY_COLOUR[category] ?? 'var(--mantine-color-gray-5)'
}

/** Where in a lane's box a pointer event landed, as 0..1. */
function fractionOf(element: HTMLElement, clientX: number): number {
  const box = element.getBoundingClientRect()
  if (box.width <= 0) return 0
  return Math.min(Math.max((clientX - box.left) / box.width, 0), 1)
}

export function StreamTimeline({
  duration, activity, moments, zoom, selection, onZoomChange, onSelectionChange,
}: {
  duration: number
  activity: number[]
  moments: Moment[]
  zoom: Zoom
  selection: { start: number; end: number } | null
  onZoomChange: (zoom: Zoom) => void
  onSelectionChange: (selection: { start: number; end: number }) => void
}) {
  const laneRef = useRef<HTMLDivElement | null>(null)
  const dragStart = useRef<number | null>(null)

  function handleOverviewClick(event: React.MouseEvent<HTMLDivElement>) {
    const seconds = fractionOf(event.currentTarget, event.clientX) * duration
    onZoomChange(zoomAround(seconds, ZOOM_SPAN_SECONDS, duration))
  }

  function handleLanePointerDown(event: React.PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId)
    dragStart.current = fractionToSeconds(fractionOf(event.currentTarget, event.clientX), zoom)
  }

  function handleLanePointerMove(event: React.PointerEvent<HTMLDivElement>) {
    if (dragStart.current === null) return
    const now = fractionToSeconds(fractionOf(event.currentTarget, event.clientX), zoom)
    onSelectionChange({
      start: Math.min(dragStart.current, now), end: Math.max(dragStart.current, now),
    })
  }

  function handleLanePointerUp(event: React.PointerEvent<HTMLDivElement>) {
    event.currentTarget.releasePointerCapture(event.pointerId)
    dragStart.current = null
  }

  return (
    <Stack gap={4}>
      <Group justify="space-between">
        <Text size="xs" c="dimmed">Whole stream — click to zoom</Text>
        <Text size="xs" c="dimmed">{formatStreamDuration(duration)}</Text>
      </Group>

      {/* Overview: activity as bars, moments as ticks. Locates, never edits. */}
      <Box
        onClick={handleOverviewClick}
        aria-label="Stream overview"
        style={{
          position: 'relative', height: 44, cursor: 'pointer',
          background: 'var(--mantine-color-dark-6)', borderRadius: 4, overflow: 'hidden',
        }}
      >
        {activity.map((value, index) => (
          <Box
            key={index}
            style={{
              position: 'absolute', bottom: 0,
              left: `${(index * CURVE_STEP_SECONDS / Math.max(duration, 1)) * 100}%`,
              width: `${(CURVE_STEP_SECONDS / Math.max(duration, 1)) * 100}%`,
              height: `${Math.max(value, 0) * 100}%`,
              background: 'var(--mantine-color-steel-5)', opacity: 0.55,
            }}
          />
        ))}
        {moments.map((moment) => (
          <Box
            key={`${moment.start}-${moment.end}`}
            title={moment.reason}
            style={{
              position: 'absolute', top: 0, bottom: 0,
              left: `${(moment.start / Math.max(duration, 1)) * 100}%`,
              width: 2, background: colourFor(moment.category),
            }}
          />
        ))}
        {/* Where the zoom lane currently is. */}
        <Box
          style={{
            position: 'absolute', top: 0, bottom: 0,
            left: `${(zoom.start / Math.max(duration, 1)) * 100}%`,
            width: `${((zoom.end - zoom.start) / Math.max(duration, 1)) * 100}%`,
            border: '1px solid var(--mantine-color-yellow-4)',
            background: 'rgba(255,255,255,0.06)',
          }}
        />
      </Box>

      <Group justify="space-between">
        <Text size="xs" c="dimmed">{formatStreamDuration(zoom.start)}</Text>
        <Text size="xs" c="dimmed">Drag to set a clip window</Text>
        <Text size="xs" c="dimmed">{formatStreamDuration(zoom.end)}</Text>
      </Group>

      {/* Zoom lane: edits. Wide enough that a second is several pixels. */}
      <Box
        ref={laneRef}
        onPointerDown={handleLanePointerDown}
        onPointerMove={handleLanePointerMove}
        onPointerUp={handleLanePointerUp}
        aria-label="Zoom lane"
        style={{
          position: 'relative', height: 56, cursor: 'ew-resize',
          background: 'var(--mantine-color-dark-7)', borderRadius: 4,
          overflow: 'hidden', touchAction: 'none',
        }}
      >
        {moments
          .filter((moment) => moment.end > zoom.start && moment.start < zoom.end)
          .map((moment) => (
            <Box
              key={`${moment.start}-${moment.end}`}
              title={moment.reason}
              style={{
                position: 'absolute', top: 4, bottom: 4,
                left: `${secondsToFraction(moment.start, zoom) * 100}%`,
                width: `${Math.max(
                  (secondsToFraction(moment.end, zoom)
                    - secondsToFraction(moment.start, zoom)) * 100, 0.4,
                )}%`,
                background: colourFor(moment.category), opacity: 0.5, borderRadius: 2,
              }}
            />
          ))}
        {selection && (
          <Box
            style={{
              position: 'absolute', top: 0, bottom: 0,
              left: `${secondsToFraction(selection.start, zoom) * 100}%`,
              width: `${Math.max(
                (secondsToFraction(selection.end, zoom)
                  - secondsToFraction(selection.start, zoom)) * 100, 0.3,
              )}%`,
              border: '2px solid var(--mantine-color-yellow-4)',
              background: 'rgba(255, 214, 0, 0.15)',
            }}
          />
        )}
      </Box>
    </Stack>
  )
}
```

- [ ] **Step 2: Wire it into `StreamScreen`**

Add state and render it above the transcript:

```tsx
  const [zoom, setZoom] = useState<Zoom>({ start: 0, end: 180 })
  const [selection, setSelection] = useState<{ start: number; end: number } | null>(null)

  const duration = analysis?.duration_seconds || transcript?.duration_seconds || 0

  useEffect(() => {
    // Re-clamp when the duration arrives: the initial zoom is a guess made
    // before anything is loaded, and on a stream shorter than 180 s it would
    // otherwise show time that does not exist.
    setZoom((current) => clampZoom(current, duration))
  }, [duration])
```

```tsx
          <StreamTimeline
            duration={duration}
            activity={analysis?.activity ?? []}
            moments={analysis?.moments ?? []}
            zoom={zoom}
            selection={selection}
            onZoomChange={setZoom}
            onSelectionChange={setSelection}
          />
```

Both `?? []` fallbacks are load-bearing: with no analysis the lanes render empty rather than the screen failing.

- [ ] **Step 3: Verify and commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run lint && npm run build && cd -
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "feat(studio-web): an overview strip that locates and a lane that edits"
```

---

## Task 7: The hit list and the player

**Files:**
- Create: `src/yt_shorts/studio/web/src/components/HitList.tsx`
- Create: `src/yt_shorts/studio/web/src/components/StreamPlayer.tsx`
- Modify: `src/yt_shorts/studio/web/src/components/StreamScreen.tsx`

**Interfaces:**
- Consumes: `sortMoments`, `filterMoments`, `categoryLabel`, `CATEGORY_ORDER`, `Moment` from `../momentList`; `zoomAround` from `../streamTimeline`.
- Produces: `export function HitList(props: { moments: Moment[]; engine: string | null; missingWindows: number[]; selectedStart: number | null; onPick: (moment: Moment) => void }): JSX.Element`; `export function StreamPlayer(props: { videoId: string; startAt: number }): JSX.Element`

**The hit list leads the layout** — that was the layout decision: it is the first thing read, and the timeline and transcript support it. It also carries two pieces of bad news the operator must not have to go looking for in a log: **which engine produced this list**, and **which windows failed**. A list produced by the lexicon fallback looks exactly like a model list unless it says so, and a stream with three failed windows has three hours nobody looked at.

- [ ] **Step 1: Write `HitList.tsx`**

```tsx
import { useState } from 'react'
import {
  Alert, Badge, Box, Chip, Group, ScrollArea, SegmentedControl, Stack, Text,
} from '@mantine/core'

import { formatStreamDuration } from '../format'
import { CATEGORY_ORDER, categoryLabel, filterMoments, sortMoments } from '../momentList'
import type { Moment, SortKey } from '../momentList'

/**
 * The detected moments, and the two things about them the operator must not
 * have to dig for.
 *
 * `engine` is shown because a lexicon-fallback list looks identical to a model
 * list on screen, and the two are not comparable in quality - this project has
 * already paid once for a degradation that was technically logged and
 * practically invisible. `missingWindows` is shown for the same reason: a
 * failed window is an hour of the stream nobody looked at, and an absent
 * moment is indistinguishable from an uneventful hour unless it is said.
 */
export function HitList({
  moments, engine, missingWindows, selectedStart, onPick,
}: {
  moments: Moment[]
  engine: string | null
  missingWindows: number[]
  selectedStart: number | null
  onPick: (moment: Moment) => void
}) {
  const [sort, setSort] = useState<SortKey>('score')
  const [categories, setCategories] = useState<string[]>([])

  const shown = sortMoments(filterMoments(moments, new Set(categories)), sort)

  return (
    <Stack gap="xs" style={{ flex: 1, minHeight: 0 }}>
      <Group justify="space-between" wrap="nowrap">
        <SegmentedControl
          size="xs"
          value={sort}
          onChange={(value) => setSort(value as SortKey)}
          data={[{ label: 'Strongest', value: 'score' }, { label: 'In order', value: 'time' }]}
        />
        <Badge variant="light" color="steel">{shown.length} / {moments.length}</Badge>
      </Group>

      <Chip.Group multiple value={categories} onChange={setCategories}>
        <Group gap={4}>
          {CATEGORY_ORDER.map((category) => (
            <Chip key={category} value={category} size="xs" variant="outline">
              {categoryLabel(category)}
            </Chip>
          ))}
        </Group>
      </Chip.Group>

      {engine === 'lexicon' && (
        <Alert color="yellow" title="Found without a model">
          These came from the offline lexicon engine, which is markedly weaker.
          Configure an API key to use the model.
        </Alert>
      )}
      {engine === null && moments.length === 0 && (
        <Text size="sm" c="dimmed">
          This stream has not been analysed yet. You can still pick a window by
          hand in the lane below.
        </Text>
      )}
      {missingWindows.length > 0 && (
        <Alert color="orange" title={`${missingWindows.length} window(s) failed`}>
          Those parts of the stream were not analysed at all — an absent moment
          there means nobody looked, not that nothing happened.
        </Alert>
      )}

      <ScrollArea style={{ flex: 1 }} offsetScrollbars>
        <Stack gap={4}>
          {shown.map((moment) => (
            <Box
              key={`${moment.start}-${moment.end}`}
              onClick={() => onPick(moment)}
              style={{
                cursor: 'pointer', padding: 6, borderRadius: 4,
                background: selectedStart === moment.start
                  ? 'var(--mantine-color-steel-light)' : 'var(--mantine-color-dark-6)',
              }}
            >
              <Group justify="space-between" wrap="nowrap" gap="xs">
                <Text size="xs" c="dimmed">{formatStreamDuration(moment.start)}</Text>
                <Badge size="xs" variant="light">{categoryLabel(moment.category)}</Badge>
                <Text size="xs" fw={600}>{moment.score.toFixed(1)}</Text>
              </Group>
              <Text size="sm" mt={2}>{moment.reason}</Text>
              {moment.hook_suggestion && (
                <Text size="xs" c="dimmed" mt={2}>“{moment.hook_suggestion}”</Text>
              )}
            </Box>
          ))}
        </Stack>
      </ScrollArea>
    </Stack>
  )
}
```

- [ ] **Step 2: Write `StreamPlayer.tsx`**

```tsx
import { useState } from 'react'
import { ActionIcon, Box, Group, Modal, Text } from '@mantine/core'

/**
 * The stream, small by default and expandable to an overlay.
 *
 * A YouTube embed, not a local file: the source is a public stream and this
 * project already downloads only its AUDIO for transcription. Re-fetching hours
 * of video to scrub through it locally would cost the operator's disk and buy
 * nothing the embed does not give.
 *
 * `startAt` is applied through the src, so changing it remounts the iframe and
 * seeks. That is deliberate rather than lazy: the alternative is the YouTube
 * iframe API, a third-party script this project's studio does not load and a
 * network dependency on a screen that is otherwise entirely local.
 */
export function StreamPlayer({ videoId, startAt }: { videoId: string; startAt: number }) {
  const [expanded, setExpanded] = useState(false)
  const src = `https://www.youtube.com/embed/${encodeURIComponent(videoId)}`
    + `?start=${Math.max(Math.floor(startAt), 0)}`

  const frame = (height: number) => (
    <Box
      component="iframe"
      title="Stream"
      src={src}
      allow="accelerometer; encrypted-media; picture-in-picture"
      style={{ width: '100%', height, border: 0, borderRadius: 4, background: '#000' }}
    />
  )

  return (
    <>
      <Group justify="space-between" mb={4}>
        <Text size="xs" c="dimmed">Player</Text>
        <ActionIcon
          size="sm" variant="subtle" aria-label="Expand the player"
          onClick={() => setExpanded(true)}
        >
          ⤢
        </ActionIcon>
      </Group>
      {frame(180)}
      <Modal
        opened={expanded}
        onClose={() => setExpanded(false)}
        size="80%"
        title="Stream"
        centered
      >
        {frame(520)}
      </Modal>
    </>
  )
}
```

- [ ] **Step 3: Wire both into `StreamScreen`**

Picking a moment does three things at once — it is the whole point of the hit list:

```tsx
  function handlePick(moment: Moment) {
    setSelection({ start: moment.start, end: moment.end })
    setZoom(zoomAround((moment.start + moment.end) / 2, 180, duration))
    setCurrentTime(moment.start)
  }
```

Lay the screen out with the hit list on the left and the rest on the right, each pane owning its own scroll:

```tsx
        <Grid gutter="md" style={{ flex: 1, minHeight: 0 }}>
          <Grid.Col span={{ base: 12, md: 4 }} style={{ display: 'flex', minHeight: 0 }}>
            <HitList
              moments={analysis?.moments ?? []}
              engine={analysis?.engine ?? null}
              missingWindows={analysis?.missing_windows ?? []}
              selectedStart={selection?.start ?? null}
              onPick={handlePick}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, md: 8 }} style={{ display: 'flex', minHeight: 0 }}>
            <Stack gap="sm" style={{ flex: 1, minHeight: 0 }}>
              <StreamPlayer videoId={videoId} startAt={currentTime} />
              <StreamTimeline … />
              <TranscriptPane … />
            </Stack>
          </Grid.Col>
        </Grid>
```

- [ ] **Step 4: Verify and commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run lint && npm run build && cd -
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "feat(studio-web): the hit list leads, and says which engine found it"
```

---

## Task 8: Creating a clip from the chosen window

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Modify: `src/yt_shorts/studio/web/src/components/StreamScreen.tsx`
- Modify: `CLAUDE.md`
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Consumes: `clip_from_moment.create_clip(event_dir, *, video_id, start, end, hook, source_title) -> Path`, `clip_from_moment.ClipIdentityCollision`, `clip_from_moment.ClipIdentityUnreadable`, `lock.EventLock`, `lock.LockError`.
- Produces: `POST …/streams/{video_id}/clips` with body `{"start": float, "end": float, "hook": str, "source_title": str}` → `{"name": "<clip dir name>"}`; **400** on `ValueError` (end ≤ start) or a bad segment; **409** on `ClipIdentityCollision`, `ClipIdentityUnreadable` and `LockError`, each with its own message; and `export function createClipFromWindow(channel, event, videoId, body): Promise<{ name: string }>` in `api.ts`.

**This is where the studio's write boundary is amended, and the amendment is part of the task, not a follow-up.** Do not add the route without also editing `CLAUDE.md` — a rule that the code quietly contradicts is worse than no rule.

**The three failure modes are three different sentences to the operator**, which is exactly why `clip_from_moment` defines two exception types instead of one: a collision means "you already have a clip for a window this close — open it instead"; unreadable means "a clip directory is already there but its `clip.json` will not parse — look at it"; a lock means "a render or detection is running for this event". Collapsing them into one 409 with one message throws away the distinction the previous task paid for.

- [ ] **Step 1: Write the failing tests**

```python
class TestCreateClipFromWindow:
    def _body(self, **over):
        body = {"start": 90.0, "end": 104.0, "hook": "BIG ONE",
                "source_title": "ERF Race Part 1"}
        body.update(over)
        return body

    def test_creates_a_clip_directory(self, client, event_dir):
        response = client.post(f"{EV}/streams/vid123/clips", json=self._body())
        assert response.status_code == 200
        name = response.json()["name"]
        assert (event_dir / "clips" / name / "clip.json").is_file()

    def test_the_same_window_twice_is_idempotent(self, client):
        first = client.post(f"{EV}/streams/vid123/clips", json=self._body())
        second = client.post(f"{EV}/streams/vid123/clips", json=self._body())
        assert second.status_code == 200
        assert second.json()["name"] == first.json()["name"]

    def test_a_colliding_different_window_is_a_409_naming_both(self, client):
        client.post(f"{EV}/streams/vid123/clips", json=self._body())
        # Rounds to the same identity, but is a different window.
        response = client.post(f"{EV}/streams/vid123/clips",
                               json=self._body(start=90.4, end=104.4, hook="OTHER"))
        assert response.status_code == 409
        assert "90" in response.json()["detail"]

    def test_an_inverted_window_is_a_400(self, client):
        response = client.post(f"{EV}/streams/vid123/clips",
                               json=self._body(start=104.0, end=90.0))
        assert response.status_code == 400

    def test_a_traversing_video_id_is_refused(self, client):
        response = client.post(f"{EV}/streams/..%2F..%2Fauth/clips", json=self._body())
        assert response.status_code == 400

    def test_it_is_refused_while_the_event_lock_is_held(self, client, event_dir):
        from yt_shorts.lock import EventLock
        lock = EventLock(event_dir)
        lock.acquire()
        try:
            response = client.post(f"{EV}/streams/vid123/clips", json=self._body())
            assert response.status_code == 409
        finally:
            lock.release()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k CreateClipFromWindow`
Expected: FAIL — 404/405, the route does not exist.

- [ ] **Step 3: Implement the route**

```python
    class NewClipWindow(BaseModel):
        start: float
        end: float
        hook: str = ""
        source_title: str = ""

    @app.post(EV + "/streams/{video_id}/clips")
    def post_clip_from_window(channel: str, event: str, video_id: str,
                              body: NewClipWindow) -> dict:
        """Create a clip from a window the operator chose. The ONE studio write
        outside edit.json and a render (see CLAUDE.md's amended boundary).

        The three failures are three different sentences on purpose: a
        collision, an unreadable neighbour and a held lock each have a
        different remedy, and clip_from_moment defines two exception types
        precisely so a caller can tell the first two apart.
        """
        profile = _load_profile(channel, event)
        try:
            validate_segment(video_id, what="video id")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        # The same EventLock a render and a detect take: creating a clip
        # restructures the event's clip directory, and a concurrent render
        # walking that directory must not see it half-built.
        lock = EventLock(profile.event_dir)
        try:
            lock.acquire()
        except LockError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            directory = create_clip(profile.event_dir, video_id=video_id,
                                    start=body.start, end=body.end, hook=body.hook,
                                    source_title=body.source_title)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (ClipIdentityCollision, ClipIdentityUnreadable) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        finally:
            lock.release()
        return {"name": directory.name}
```

Import `create_clip`, `ClipIdentityCollision`, `ClipIdentityUnreadable` from `..clip_from_moment`, `EventLock`/`LockError` from `..lock` (check which are already imported), and `validate_segment` from `..pathnames`. Add the route to the module docstring's table.

- [ ] **Step 4: Amend the boundary in `CLAUDE.md`**

In the section beginning "**`yt_shorts/studio/` is the local editor's boundary, and it has two rules.**", the first rule currently says the package writes `edit.json` and nothing else, with the render carve-out. Add the second carve-out in the same voice, stating what is and is not permitted and why the line falls there:

```
There is a SECOND carve-out, added with the stream view: `POST
…/streams/{video_id}/clips` CREATES a clip directory and its `clip.json`
through `clip_from_moment.create_clip`. That is a write outside `edit.json`,
and it is deliberate rather than an erosion - detection deliberately produces
no clips, so an operator picking a window is the only way one can come into
existence from the studio at all, and refusing it would leave the whole
analysis unusable without dropping to the CLI. The line the rule still holds
is the one that matters: the studio may CREATE a clip the operator explicitly
asked for, and still never EDITS an existing event's `clip.json`,
`transcript.json`, `sources.json` or rendered short. It takes the same
`EventLock` a render does, for the same reason.
```

- [ ] **Step 5: Add the client and the button**

In `api.ts`:

```ts
export function createClipFromWindow(
  channel: string, event: string, videoId: string,
  body: { start: number; end: number; hook: string; source_title: string },
): Promise<{ name: string }> {
  return fetch(
    `${eventBase(channel, event)}/streams/${encodeURIComponent(videoId)}/clips`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  ).then(asJson<{ name: string }>)
}
```

In `StreamScreen`, a hook field and a button beside the timeline. Import `notifications` from `@mantine/notifications` and `TextInput`/`Button`/`Group` from `@mantine/core` if they are not already imported. The JSX, rendered between the timeline and the transcript:

```tsx
            <Group gap="xs" align="flex-end" wrap="nowrap">
              <TextInput
                label="Hook"
                placeholder="On-screen title for this clip"
                value={hook}
                onChange={(event) => setHook(event.currentTarget.value)}
                style={{ flex: 1 }}
              />
              <Button
                onClick={handleCreate}
                loading={creating}
                disabled={!selection}
              >
                {selection
                  ? `Make a clip (${formatStreamDuration(selection.start)}–${formatStreamDuration(selection.end)})`
                  : 'Make a clip'}
              </Button>
            </Group>
```

The button is disabled without a selection rather than hidden: a control that vanishes leaves the operator wondering where it went, while a disabled one that names the window it would cut says what is missing.

The handler:

```tsx
  const [hook, setHook] = useState('')
  const [creating, setCreating] = useState(false)

  async function handleCreate() {
    if (!selection) return
    setCreating(true)
    try {
      const { name } = await createClipFromWindow(channel, event, videoId, {
        start: selection.start, end: selection.end, hook,
        source_title: analysis?.stream_title ?? '',
      })
      notifications.show({ message: `Created ${name}.`, color: 'green' })
      setHook('')
    } catch (err) {
      // The server's message is the useful half here: it names both windows on
      // a collision and the directory on an unreadable neighbour.
      notifications.show({
        title: 'Could not create the clip', color: 'red',
        message: err instanceof ApiError ? err.message : String(err),
      })
    } finally {
      setCreating(false)
    }
  }
```

- [ ] **Step 6: Verify and commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run lint && npm run build && cd -
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q
git add src/yt_shorts/studio src/yt_shorts/studio/static CLAUDE.md tests/test_studio_api.py
git commit -m "feat(studio): create a clip from a chosen window, and amend the write boundary"
```

---

## Task 9: What a detection run would cost, before it runs

**Files:**
- Create: `src/yt_shorts/estimate.py`
- Test: `tests/test_estimate.py`
- Modify: `src/yt_shorts/studio/api.py`, `src/yt_shorts/studio/web/src/api.ts`, `StreamScreen.tsx`

**Interfaces:**
- Consumes: `moment_scan.group_lines`, `moment_scan.split_windows`, `moment_scan.render_window`, `moment_scan.build_system_prompt`, `stream_analysis.read_transcript`.
- Produces: `estimate.PRICES: dict[str, tuple[float, float]]`; `estimate.estimate_run(words, lexicon, *, model) -> dict` returning `{"windows": int, "input_tokens": int, "output_tokens": int, "usd": float, "model": str, "estimated": True}`; `POST …/streams/{video_id}/estimate` with body `{"model": str | null}` → that dict; **404** when there is no transcript.

**It is an ESTIMATE and must say so in the payload, not only in the UI.** Tokens are approximated from character count (~4 characters per token) rather than by calling the API's token-counting endpoint: this screen must work with no key at all, and a preview that fails without one would defeat its own purpose. The `estimated: True` field exists so no caller can present the number as billing truth.

Prices are the per-million input/output figures already recorded in `claude_client.DEFAULT_MODEL`'s note. They are a snapshot and will drift — the docstring must say so and say where to re-check.

- [ ] **Step 1: Write the failing tests**

```python
from yt_shorts import estimate
from yt_shorts.lexicon import Lexicon


def words(seconds=7200):
    return [{"start": t, "end": t + 0.8, "text": " word"} for t in range(seconds)]


class TestEstimateRun:
    def test_counts_one_window_per_hour_with_the_overlap(self, tmp_path):
        result = estimate.estimate_run(words(7200), Lexicon(markers={}),
                                       model="claude-opus-5")
        assert result["windows"] >= 2

    def test_the_cost_scales_with_the_model(self):
        cheap = estimate.estimate_run(words(3600), Lexicon(markers={}),
                                      model="claude-haiku-4-5")
        dear = estimate.estimate_run(words(3600), Lexicon(markers={}),
                                     model="claude-opus-5")
        assert dear["usd"] > cheap["usd"]

    def test_an_unknown_model_does_not_crash_and_says_it_priced_nothing(self):
        result = estimate.estimate_run(words(600), Lexicon(markers={}), model="made-up")
        assert result["usd"] == 0.0 and result["model"] == "made-up"

    def test_an_empty_transcript_is_a_zero_estimate(self):
        result = estimate.estimate_run([], Lexicon(markers={}), model="claude-opus-5")
        assert result["windows"] == 0 and result["usd"] == 0.0

    def test_the_payload_says_it_is_an_estimate(self):
        # No caller may present this as what the run will actually be billed.
        assert estimate.estimate_run(words(600), Lexicon(markers={}),
                                     model="claude-opus-5")["estimated"] is True

    def test_it_makes_no_network_call(self, monkeypatch):
        # The screen must work with no key and no connectivity; a token count
        # fetched from the API would defeat the preview's whole purpose.
        import socket
        monkeypatch.setattr(socket, "socket",
                            lambda *a, **k: pytest.fail("estimate must not use the network"))
        estimate.estimate_run(words(600), Lexicon(markers={}), model="claude-opus-5")
```

- [ ] **Step 2: Implement `estimate.py`**

```python
"""What a detection run would cost, worked out locally.

Pure and dependency-free by design - no `anthropic` import at module scope or
anywhere else, and no network call. The stream screen is specified to be useful
with NO API key, so a preview that needed one (the API's own token-counting
endpoint, for instance) would fail exactly where it is most wanted: before the
operator has decided whether to configure a key at all.

The count is therefore APPROXIMATE - characters divided by four, the usual
rough ratio - and the payload carries `estimated: True` so no caller can render
it as billing truth. It is meant to answer "cents or euros?", not "what will
the invoice say".

PRICES is a snapshot of the published per-million rates on 2026-07-29 and will
drift; re-check at anthropic.com/pricing rather than trusting these numbers.
"""

from __future__ import annotations

from .lexicon import Lexicon
from .moment_scan import build_system_prompt, group_lines, render_window, split_windows

CHARS_PER_TOKEN = 4

# model -> (USD per 1M input tokens, USD per 1M output tokens)
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}

# A window's answer is a short JSON list of moments; measured runs over a
# 98-minute qualifying returned well under this, and over-estimating the
# cheaper half of the bill is the safe direction to be wrong in.
OUTPUT_TOKENS_PER_WINDOW = 700


def estimate_run(words, lexicon: Lexicon, *, model: str) -> dict:
    lines = group_lines(words)
    windows = split_windows(lines)
    system = build_system_prompt(lexicon)
    characters = sum(len(system) + len(render_window(window)) for window in windows)
    input_tokens = characters // CHARS_PER_TOKEN
    output_tokens = len(windows) * OUTPUT_TOKENS_PER_WINDOW
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    usd = input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out
    return {
        "model": model,
        "windows": len(windows),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usd": round(usd, 4),
        "estimated": True,
    }
```

- [ ] **Step 3: The route**

```python
    class EstimateRequest(BaseModel):
        model: str | None = None

    @app.post(EV + "/streams/{video_id}/estimate")
    def post_estimate(channel: str, event: str, video_id: str,
                      body: EstimateRequest) -> dict:
        """What a detection run over this stream would cost. Reads only."""
        profile = _load_profile(channel, event)
        root = _resolve_workspace().root
        try:
            transcript = stream_analysis.read_transcript(root, video_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except stream_analysis.AnalysisError as error:
            status = 404 if error.kind == "not_found" else 500
            raise HTTPException(status_code=status, detail=str(error)) from error
        settings = profile.config.get("detect", {}) or {}
        model = body.model or settings.get("model", claude_client.DEFAULT_MODEL)
        return estimate.estimate_run(transcript["words"],
                                     profile.config.get("lexicon", LEXICON_EMPTY),
                                     model=model)
```

- [ ] **Step 4: Show it on the screen**

In `api.ts`:

```ts
export interface RunEstimate {
  model: string
  windows: number
  input_tokens: number
  output_tokens: number
  usd: number
  estimated: boolean
}

export function estimateRun(
  channel: string, event: string, videoId: string, model: string | null,
): Promise<RunEstimate> {
  return fetch(
    `${eventBase(channel, event)}/streams/${encodeURIComponent(videoId)}/estimate`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    },
  ).then(asJson<RunEstimate>)
}
```

In `StreamScreen`, beside the create-clip controls:

```tsx
  const [runEstimate, setRunEstimate] = useState<RunEstimate | null>(null)

  async function handleEstimate() {
    try {
      setRunEstimate(await estimateRun(channel, event, videoId, null))
    } catch (err) {
      notifications.show({
        title: 'Could not estimate the run', color: 'red',
        message: err instanceof ApiError ? err.message : String(err),
      })
    }
  }
```

```tsx
            <Group gap="xs" align="center">
              <Button size="xs" variant="light" onClick={handleEstimate}>
                Estimate a detection run
              </Button>
              {runEstimate && (
                <Text size="xs" c="dimmed">
                  ~${runEstimate.usd.toFixed(3)} with {runEstimate.model} over{' '}
                  {runEstimate.windows} window(s) — an estimate, not a quote.
                </Text>
              )}
            </Group>
```

Do NOT auto-fetch on mount: it is information the operator asks for, not a number that should appear unbidden. The trailing "an estimate, not a quote" is not decoration — it is the UI half of `estimate_run`'s `estimated: True`, and it must survive any rewording.

- [ ] **Step 5: Verify and commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run lint && npm run build && cd -
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q
git add src/yt_shorts/estimate.py tests/test_estimate.py src/yt_shorts/studio CLAUDE.md
git commit -m "feat(studio): estimate a detection run before paying for it"
```

---

## Task 10: The integrated flow, and the scrolling gate

**Files:**
- Modify: `tests/test_studio_e2e.py`
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:** none produced; this task proves the previous nine.

**The scrolling criterion is an acceptance gate, not a nicety.** Every element must be reachable at a short viewport — the operator has stated this as a requirement for every studio screen. Verify it in a real browser at 1280x600, and pin it in a test.

- [ ] **Step 1: Add the e2e test**

Follow the file's existing patterns exactly: the `live_server` fixture, `monkeypatch.setattr(api, …)` at the api-module level, a `threading.Event()` gate for observing an in-flight state, and assertions that read real disk state.

```python
def stream_url(base_url: str, video_id: str) -> str:
    return f"{base_url}/{CHANNEL}/{EVENT}/streams/{video_id}"


class TestStreamScreen:
    """The stream view: it opens without an analysis, and creates a clip."""

    def _write_transcript(self, root, video_id, words):
        directory = root / "streams" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "transcript.json").write_text(json.dumps({
            "video_id": video_id, "duration_seconds": 600.0, "chunk_seconds": 600,
            "words": words, "missing_chunks": [],
        }), encoding="utf-8")

    def test_the_screen_opens_and_searches_without_any_analysis(
            self, page, live_server, workspace_root):
        # The load-bearing case: no moments.json at all. The screen must be
        # useful anyway - that is the whole "works with no API key" promise.
        self._write_transcript(workspace_root, "vid123", [
            {"start": 10.0, "end": 10.5, "text": " Karussell"},
            {"start": 11.0, "end": 11.5, "text": " contact"},
        ])
        page.goto(stream_url(live_server, "vid123"))
        page.get_by_text("Karussell").wait_for(timeout=5000)
        page.get_by_placeholder("Search the transcript").fill("contact")
        page.get_by_text("contact").wait_for(timeout=5000)

    def test_every_pane_is_reachable_at_a_short_viewport(
            self, page, live_server, workspace_root):
        # The operator's standing acceptance criterion for every studio screen.
        self._write_transcript(workspace_root, "vid123",
                               [{"start": float(i), "end": i + 0.5, "text": " word"}
                                for i in range(400)])
        page.set_viewport_size({"width": 1280, "height": 600})
        page.goto(stream_url(live_server, "vid123"))
        page.get_by_label("Stream overview").wait_for(timeout=5000)
        page.get_by_label("Zoom lane").scroll_into_view_if_needed()
        page.get_by_placeholder("Search the transcript").scroll_into_view_if_needed()
```

- [ ] **Step 2: Run it**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q -k StreamScreen`
Expected: all pass. If Chromium is unavailable the module skips — confirm the skip reason rather than assuming a pass.

- [ ] **Step 3: Verify the scrolling criterion in a real browser**

Start the studio (`bin/yt-shorts studio`), open a stream screen at a 1280x600 window, and confirm by eye that the hit list, the zoom lane, the transcript and the player are all reachable. The test above pins the mechanism; this step is the operator's own criterion and the test is not a substitute for it.

- [ ] **Step 4: Document the screen**

In `CLAUDE.md`, add the stream view to the studio section — the fourth route level, the two-lane timeline and why one lane cannot work at eight hours, that the screen is useful with no key, and that the hit list surfaces `engine` and `missing_windows` because a silent degradation is this project's recurring failure mode. **Also correct the stale sentence** describing the router as three screens: it is six now (channels, events, editor, settings, logs, stream).

In `README.md`, describe the operator's flow: open the event, the Streams tab, click a stream, read the transcript, pick a window, create a clip.

- [ ] **Step 5: Final verification and commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run lint && npm run build && cd -
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q
git add -A
git commit -m "test+docs(studio): the stream view end to end, and its scrolling gate"
```
