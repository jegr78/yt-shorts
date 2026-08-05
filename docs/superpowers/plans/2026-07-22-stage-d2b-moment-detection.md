# Stage D2b — Moment detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Find the moments worth clipping in a stream (per-channel lexicon + speech-rate pick candidates, loudness ranks them), write them as candidate clip entries, and let an operator curate them in the studio — adjust each window, keep or discard — then render through the existing pipeline.

**Architecture:** A pure `moments.py` scores emphasis over D2a's transcript and ranks candidates by an injected loudness measurement. `detect_moments` ties D2a's `transcribe_stream` to that scoring and writes clip entries. Moment clips carry a `video_id` and a path-encoded identity so they flow through `clipstore`/`render` unchanged; window edits live in `edit.json` as an editorial override, like a title edit. The studio gains a detect job, a candidate list, and a window control.

**Tech Stack:** Python 3 standard library, existing ffmpeg/yt-dlp, faster-whisper (via D2a). FastAPI + React/Vite/Mantine for the studio, as already used. No new dependencies.

## Global Constraints

- `PYTHONPATH=src` is mandatory. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` — 516 tests pass at the start of this plan.
- No new Python dependencies. Loudness is an ffmpeg subprocess; detection is pure Python.
- **Detection is testable without audio.** `moments.py` scoring is pure over a word list; loudness is an injected boundary (`measure_loudness`), stubbed in tests. No test runs real ffmpeg loudness or a real detection end-to-end in the suite.
- **`canonical_url` strips the query string** (verified: it splits on `?` and `#`). A moment's identity must therefore encode `video_id`, `start`, and `end` in the URL **path**, never the query, or every moment in a stream collapses to one identity.
- **The studio writes `edit.json` and nothing else.** The window override is an editorial field; detection writes clip entries (derived), never `edit.json`; the studio writes the window only into `edit.json`.
- **Derived vs editorial stays strict.** Detection output is derived (re-derivable by re-running detection); the window adjustment and keep/discard are editorial.
- **`setsar=1` stays the final filter step; never crop.** Moment clips render through the same `render.compose`; this stage does not touch the filter chain.
- **One failure never aborts a run**; a detect job that fails one step reports it and does not corrupt the event.
- Tests must not depend on `~/YT-Shorts-Data`, must not hit the network, must not run a real model. `tests/conftest.py` pins `profile.CHANNELS_DIR` to `tests/fixtures/channels`.
- **FastAPI stays optional**: nothing outside `src/yt_shorts/studio/` imports it. `moments.py`, `detect_moments`, and the editorial changes must not import FastAPI.
- Rebuild and commit the studio's `static/`; never commit `node_modules`. English only. Imperative commit messages.

---

## Task 1: Load the channel lexicon

**Files:**
- Create: `src/yt_shorts/lexicon.py`
- Modify: `src/yt_shorts/profile.py`
- Create: `tests/fixtures/channels/erf/moments.json`
- Test: `tests/test_lexicon.py`, `tests/test_profile.py`

**Interfaces:**
- Produces:
  - `lexicon.Lexicon` — dataclass `markers: list[str]`
  - `lexicon.EMPTY`
  - `lexicon.load(path) -> Lexicon` — reads a `moments.json`; `{"markers": [...]}`
  - `profile.load(...).config["lexicon"]` — a `Lexicon`, `EMPTY` when no file

- [ ] **Step 1: Write the failing test**

`tests/test_lexicon.py`:

```python
import json

import pytest

from yt_shorts.lexicon import EMPTY, Lexicon, load


class TestLoad:
    def test_missing_file_is_empty(self, tmp_path):
        assert load(tmp_path / "nope.json") == EMPTY

    def test_reads_markers(self, tmp_path):
        p = tmp_path / "moments.json"
        p.write_text(json.dumps({"markers": ["crash", "safety car"]}), encoding="utf-8")
        assert load(p) == Lexicon(markers=["crash", "safety car"])

    def test_missing_markers_key_is_empty(self, tmp_path):
        p = tmp_path / "moments.json"
        p.write_text(json.dumps({}), encoding="utf-8")
        assert load(p) == EMPTY

    def test_markers_must_be_a_list_of_strings(self, tmp_path):
        p = tmp_path / "moments.json"
        p.write_text(json.dumps({"markers": [1, 2]}), encoding="utf-8")
        with pytest.raises(ValueError):
            load(p)
```

Add to `tests/test_profile.py` (follow its existing glossary-loading tests as the pattern — the profile exposes `config["lexicon"]` the way it exposes `config["glossary"]`):

```python
class TestLexiconInProfile:
    def test_channel_lexicon_is_loaded(self):
        from yt_shorts.profile import load
        from yt_shorts.lexicon import Lexicon
        p = load("erf/community-clips-back-catalogue")
        assert isinstance(p.config["lexicon"], Lexicon)
        assert "crash" in p.config["lexicon"].markers
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_lexicon.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.lexicon'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/lexicon.py`:

```python
"""A channel's excitement markers - words that mark a moment worth clipping.

Separate from glossary.json on purpose: the glossary corrects proper nouns the
decoder mishears; the lexicon names the words that signal something happened
(crash, overtake, safety car). A missing or empty file means no lexicon signal,
never an error - speech-rate still detects moments on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Lexicon:
    markers: list[str] = field(default_factory=list)


EMPTY = Lexicon(markers=[])


def load(path) -> Lexicon:
    path = Path(path)
    if not path.exists():
        return EMPTY
    payload = json.loads(path.read_text(encoding="utf-8"))
    markers = payload.get("markers", []) if isinstance(payload, dict) else []
    if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
        raise ValueError(f"'markers' must be a list of strings: {path}")
    return Lexicon(markers=markers)
```

In `profile.py`, load `moments.json` next to where `glossary.json` is loaded (channel-level, event override wholesale), storing `config["lexicon"]`. Follow the exact glossary-loading code already there; a malformed file is reported through `ProfileError` like the glossary's, or — if the glossary loader does not collect its own parse errors — at least raised clearly.

`tests/fixtures/channels/erf/moments.json`:

```json
{
  "markers": ["crash", "contact", "safety car", "overtake", "spin", "incident",
              "oh my", "into the wall", "puncture", "off the track"]
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_lexicon.py tests/test_profile.py -q`
Expected: PASS

- [ ] **Step 5: Confirm no fixture-hash regression**

The ERF fixture gained a file. Confirm the pinned overlay hashes are unaffected:
Run: `PYTHONPATH=src .venv/bin/pytest tests/test_event_layer_no_regression.py -q`
Expected: PASS (a new lexicon file does not touch overlay geometry).

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/lexicon.py src/yt_shorts/profile.py tests/test_lexicon.py tests/test_profile.py tests/fixtures/channels/erf/moments.json
git commit -m "Load a channel excitement lexicon into the profile"
```

---

## Task 2: Emphasis scoring and candidates

**Files:**
- Create: `src/yt_shorts/moments.py`
- Test: `tests/test_moments.py`

**Interfaces:**
- Consumes: `lexicon.Lexicon`
- Produces:
  - `moments.Candidate` — dataclass `peak: float`, `emphasis: float`
  - `moments.find_candidates(words, lexicon, *, step=1.0, window=6.0, threshold=1.0, min_gap=20.0) -> list[Candidate]` — peaks in the combined emphasis signal, merged within `min_gap`, above `threshold`, sorted by time

**Emphasis model (v1, explainable):** over the transcript, at each `step`-second tick, evaluate emphasis in a `[t, t+window)` bin as `lexicon_score + rate_score`:
- `lexicon_score` = number of marker phrases occurring in the bin (case-insensitive, phrase-aware — reuse `glossary`'s matching helper if one is exposed, otherwise a local normalise-and-scan).
- `rate_score` = words-in-bin / baseline, where baseline = the stream's median words-per-`window` across all bins (so a slow stream is judged against itself). Clamped so a silent bin scores 0.
Then take local maxima above `threshold`, and merge maxima within `min_gap` to the strongest.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from yt_shorts.lexicon import EMPTY, Lexicon
from yt_shorts.moments import Candidate, find_candidates


def words_at(times, texts=None):
    texts = texts or ["word"] * len(times)
    return [{"start": t, "end": t + 0.3, "text": " " + w} for t, w in zip(times, texts)]


class TestFindCandidates:
    def test_a_lexicon_hit_makes_a_candidate(self):
        # sparse chatter, one "crash" at t=100
        words = words_at([10, 40, 70], ["ok", "fine", "steady"]) + words_at([100], ["crash"])
        found = find_candidates(words, Lexicon(markers=["crash"]), threshold=0.5)
        assert any(abs(c.peak - 100) <= 6 for c in found)

    def test_a_speech_rate_spike_makes_a_candidate_without_the_lexicon(self):
        # a dense burst at ~200 against otherwise sparse speech
        sparse = words_at(range(0, 400, 40))
        burst = words_at([200 + i * 0.2 for i in range(30)])
        found = find_candidates(sparse + burst, EMPTY, threshold=1.5)
        assert any(abs(c.peak - 200) <= 6 for c in found)

    def test_a_flat_quiet_stretch_has_no_candidates(self):
        words = words_at(range(0, 300, 30))    # even, unremarkable
        assert find_candidates(words, EMPTY, threshold=2.0) == []

    def test_nearby_peaks_merge_to_one(self):
        words = words_at([100, 100.5, 101, 101.5, 102], ["crash"] * 5)
        found = find_candidates(words, Lexicon(markers=["crash"]), threshold=0.5, min_gap=20)
        assert len([c for c in found if 90 <= c.peak <= 115]) == 1

    def test_candidates_are_sorted_by_time(self):
        words = (words_at([50], ["crash"]) + words_at([250], ["spin"]))
        found = find_candidates(words, Lexicon(markers=["crash", "spin"]), threshold=0.5)
        assert [round(c.peak) for c in found] == sorted(round(c.peak) for c in found)

    def test_empty_words_is_no_candidates(self):
        assert find_candidates([], Lexicon(markers=["crash"])) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moments.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.moments'`

- [ ] **Step 3: Write the implementation**

```python
"""Find candidate moments in a stream's transcript (see the stage D2b design).

Pure logic over a word list: a per-channel excitement lexicon and speech-rate
spikes each contribute to an emphasis signal; peaks above a threshold, merged
when close, are the candidates. Loudness (in moments_rank/detect) then orders
them - this module only decides *which* moments, never their rank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .lexicon import Lexicon


@dataclass
class Candidate:
    peak: float
    emphasis: float


def _text(words) -> str:
    return " ".join(w["text"].strip().lower() for w in words)


def _count_markers(bin_words, markers) -> int:
    if not markers or not bin_words:
        return 0
    joined = _text(bin_words)
    return sum(len(re.findall(re.escape(m.lower()), joined)) for m in markers)


def find_candidates(words, lexicon: Lexicon, *, step: float = 1.0, window: float = 6.0,
                    threshold: float = 1.0, min_gap: float = 20.0) -> list[Candidate]:
    if not words:
        return []
    end = max(w["end"] for w in words)
    ticks = []
    t = 0.0
    while t < end:
        ticks.append(t)
        t += step

    def bin_words(start):
        return [w for w in words if start <= w["start"] < start + window]

    counts = [len(bin_words(t)) for t in ticks]
    positive = sorted(c for c in counts if c > 0)
    baseline = positive[len(positive) // 2] if positive else 1
    baseline = max(baseline, 1)

    emphasis = []
    for t, count in zip(ticks, counts):
        rate_score = count / baseline
        lex_score = _count_markers(bin_words(t), lexicon.markers)
        emphasis.append(lex_score + rate_score)

    # local maxima above threshold
    peaks = []
    for i, value in enumerate(emphasis):
        if value < threshold:
            continue
        left = emphasis[i - 1] if i > 0 else -1
        right = emphasis[i + 1] if i + 1 < len(emphasis) else -1
        if value >= left and value >= right:
            peaks.append(Candidate(peak=ticks[i], emphasis=value))

    # merge peaks within min_gap, keeping the strongest
    peaks.sort(key=lambda c: c.peak)
    merged: list[Candidate] = []
    for cand in peaks:
        if merged and cand.peak - merged[-1].peak < min_gap:
            if cand.emphasis > merged[-1].emphasis:
                merged[-1] = cand
        else:
            merged.append(cand)
    return merged
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moments.py -q`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/moments.py tests/test_moments.py
git commit -m "Score transcript emphasis into candidate moments"
```

---

## Task 3: Loudness ranking and windows

**Files:**
- Modify: `src/yt_shorts/moments.py`
- Test: `tests/test_moments.py`

**Interfaces:**
- Consumes: `Candidate` (Task 2)
- Produces:
  - `moments.Moment` — dataclass `video_id: str`, `start: float`, `end: float`, `peak: float`, `loudness: float`
  - `moments.rank_moments(candidates, video_id, *, measure_loudness, preroll=8.0, postroll=4.0, top_n=20) -> list[Moment]` — window each candidate, measure loudness, sort by loudness desc, take top_n
  - `moments.measure_loudness_ffmpeg(audio_path, start, end, *, ffmpeg="ffmpeg") -> float` — production loudness (EBU R128 integrated) for a window

- [ ] **Step 1: Write the failing test**

```python
from yt_shorts.moments import Candidate, Moment, rank_moments


def stub_loudness(values):
    # returns a preset loudness per (start) so ordering is checkable
    def measure(audio_path, start, end):
        return values[round(start)]
    return measure


class TestRankMoments:
    def test_windows_are_peak_minus_preroll_to_peak_plus_postroll(self):
        cands = [Candidate(peak=100.0, emphasis=3.0)]
        out = rank_moments(cands, "vid", measure_loudness=lambda *a: 1.0,
                           preroll=8.0, postroll=4.0)
        assert out[0].start == 92.0 and out[0].end == 104.0

    def test_ordered_by_loudness_not_emphasis(self):
        cands = [Candidate(peak=100.0, emphasis=9.0),   # loud? no
                 Candidate(peak=200.0, emphasis=1.0)]   # quiet emphasis, loudest
        measure = stub_loudness({92: -20.0, 192: -5.0})
        out = rank_moments(cands, "vid", measure_loudness=measure)
        assert [round(m.peak) for m in out] == [200, 100]   # louder first

    def test_top_n_caps_the_list(self):
        cands = [Candidate(peak=float(t), emphasis=1.0) for t in (100, 200, 300)]
        measure = stub_loudness({92: -1.0, 192: -2.0, 292: -3.0})
        out = rank_moments(cands, "vid", measure_loudness=measure, top_n=2)
        assert len(out) == 2

    def test_a_window_never_starts_before_zero(self):
        cands = [Candidate(peak=3.0, emphasis=1.0)]
        out = rank_moments(cands, "vid", measure_loudness=lambda *a: 1.0, preroll=8.0)
        assert out[0].start == 0.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moments.py -q -k Rank`
Expected: FAIL — `ImportError: cannot import name 'Moment'`

- [ ] **Step 3: Write the implementation**

Append to `moments.py`:

```python
import subprocess


@dataclass
class Moment:
    video_id: str
    start: float
    end: float
    peak: float
    loudness: float


def rank_moments(candidates, video_id, *, measure_loudness,
                 preroll: float = 8.0, postroll: float = 4.0,
                 top_n: int = 20) -> list["Moment"]:
    moments = []
    for cand in candidates:
        start = max(0.0, cand.peak - preroll)
        end = cand.peak + postroll
        loudness = measure_loudness(None, start, end)   # audio path bound by caller
        moments.append(Moment(video_id=video_id, start=start, end=end,
                              peak=cand.peak, loudness=loudness))
    moments.sort(key=lambda m: m.loudness, reverse=True)
    return moments[:top_n]


def measure_loudness_ffmpeg(audio_path, start, end, *, ffmpeg: str = "ffmpeg") -> float:
    """Integrated loudness (LUFS) of [start, end], via ffmpeg's ebur128 filter."""
    cmd = [ffmpeg, "-v", "info", "-nostats", "-ss", str(start), "-to", str(end),
           "-i", str(audio_path), "-af", "ebur128", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    # ebur128 prints "I: -23.0 LUFS" lines to stderr; the last summary is what we want
    matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", result.stderr)
    return float(matches[-1]) if matches else float("-inf")
```

Note: `rank_moments` passes `None` as the audio path; `detect_moments` (Task 6) binds the real path via a partial, so `moments.py` stays free of file specifics and the ranking tests need no audio. Task 6 wires `measure_loudness=lambda _p, s, e: measure_loudness_ffmpeg(audio_path, s, e)`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moments.py -q`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/moments.py tests/test_moments.py
git commit -m "Rank candidate moments by window loudness"
```

---

## Task 4: A moment's clip identity and entry

**Files:**
- Create: `src/yt_shorts/moment_entry.py`
- Test: `tests/test_moment_entry.py`

**Interfaces:**
- Consumes: `moments.Moment`, `clipid.clip_id`
- Produces:
  - `moment_entry.moment_url(video_id, start, end) -> str` — a path-encoded identity URL, distinct per `(video_id, start, end)`, surviving `canonical_url` (which strips the query)
  - `moment_entry.build_entry(moment, *, source_title, hook) -> dict` — a clip entry dict with `url`, `video_id`, `hook`, `source_title`, `start`, `end`, `duration`

**Why a path-encoded URL:** `canonical_url` splits off `?...`, so a query-based moment URL would collapse to one identity for the whole stream. Encoding the parts in the path keeps them.

- [ ] **Step 1: Write the failing test**

```python
from yt_shorts.clipid import clip_id
from yt_shorts.moments import Moment
from yt_shorts.moment_entry import build_entry, moment_url


class TestMomentUrl:
    def test_distinct_per_window(self):
        a = moment_url("vid", 92.0, 104.0)
        b = moment_url("vid", 200.0, 212.0)
        assert clip_id(a) != clip_id(b)

    def test_distinct_per_video(self):
        assert clip_id(moment_url("aaa", 0.0, 10.0)) != clip_id(moment_url("bbb", 0.0, 10.0))

    def test_same_window_same_identity(self):
        assert clip_id(moment_url("vid", 92.0, 104.0)) == clip_id(moment_url("vid", 92.0, 104.0))

    def test_survives_query_stripping(self):
        # canonical_url strips '?', so the identity must live in the path
        from yt_shorts.clipid import canonical_url
        assert "92" in canonical_url(moment_url("vid", 92.0, 104.0))


class TestBuildEntry:
    def test_fields(self):
        m = Moment(video_id="vid", start=92.0, end=104.0, peak=100.0, loudness=-5.0)
        entry = build_entry(m, source_title="ERF 24h Part 1", hook="CRASH!")
        assert entry["video_id"] == "vid"
        assert entry["start"] == 92.0 and entry["end"] == 104.0
        assert entry["duration"] == 12.0
        assert entry["source_title"] == "ERF 24h Part 1"
        assert entry["hook"] == "CRASH!"
        assert entry["url"] == moment_url("vid", 92.0, 104.0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moment_entry.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
"""Turn a detected moment into a clip entry the rest of the pipeline accepts.

A moment's identity must encode video_id, start and end. clipid.canonical_url
strips the query string, so those parts live in the URL PATH: two moments in one
stream are two clips, and the same moment re-detected is the same clip.
"""

from __future__ import annotations

from .moments import Moment


def moment_url(video_id: str, start: float, end: float) -> str:
    # Path-encoded so canonical_url (which strips '?...') keeps the identity.
    return f"https://www.youtube.com/watch/{video_id}/{int(round(start))}-{int(round(end))}"


def build_entry(moment: Moment, *, source_title: str, hook: str) -> dict:
    return {
        "url": moment_url(moment.video_id, moment.start, moment.end),
        "video_id": moment.video_id,
        "hook": hook,
        "source_title": source_title,
        "start": moment.start,
        "end": moment.end,
        "duration": moment.end - moment.start,
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moment_entry.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/moment_entry.py tests/test_moment_entry.py
git commit -m "Build a clip entry with a path-encoded identity for a moment"
```

---

## Task 5: Render and preview honour a video_id and an editorial window

**Files:**
- Modify: `src/yt_shorts/editorial.py`, `bin/yt-shorts`, `src/yt_shorts/studio/jobs.py` (or wherever `Source` is built for the studio)
- Test: `tests/test_editorial.py`, `tests/test_render.py` (or the CLI render test), `tests/test_studio_jobs.py`

**Interfaces:**
- Produces:
  - `editorial.Edit.window` — optional `tuple[float, float] | None`, loaded/saved like `title`
  - `editorial.effective_window(edit, clip_start, clip_end) -> tuple[float, float]` — the editorial window if set, else the clip's
  - `Source` construction that uses `video_id` + effective window when the clip entry carries a `video_id`, else `clip_url`

- [ ] **Step 1: Write the failing test**

`tests/test_editorial.py` — add:

```python
class TestWindowOverride:
    def test_absent_uses_the_clip_window(self):
        from yt_shorts.editorial import Edit, effective_window
        edit = Edit(title=None, status="candidate", transcript=None, window=None)
        assert effective_window(edit, 92.0, 104.0) == (92.0, 104.0)

    def test_present_window_wins(self):
        from yt_shorts.editorial import Edit, effective_window
        edit = Edit(title=None, status="candidate", transcript=None, window=(95.0, 108.0))
        assert effective_window(edit, 92.0, 104.0) == (95.0, 108.0)

    def test_window_round_trips_through_save_and_load(self, tmp_path):
        from yt_shorts.editorial import Edit, load, save
        save(tmp_path, Edit(title=None, status="kept", transcript=None, window=(95.0, 108.0)))
        assert load(tmp_path).window == (95.0, 108.0)
```

Add a render test asserting a `video_id` clip builds a `--download-sections` command over the effective window (follow `tests/test_render.py`'s existing `ytdlp_command` tests).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py -q -k Window`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'window'`

- [ ] **Step 3: Write the implementation**

- `editorial.Edit` gains `window: tuple[float, float] | None = None`. `load` reads a `"window": [start, end]` list into a tuple (validating two numbers), `save` writes it only when set. Follow the exact `title` handling for absent/present.
- `effective_window(edit, clip_start, clip_end)` returns `edit.window or (clip_start, clip_end)`.
- Where a `Source` is built from a clip entry (CLI `cmd_render` and the studio job runner), build it with `video_id` + the effective window when `clip.get("video_id")` is set, otherwise the existing `clip_url` path. Pass the effective window into `Source(video_id=..., start=s, end=e)`.
- The preview (`subtitle_pipeline`/`preview.build` callers) already renders from `raw.mp4`; ensure the studio preview for a moment uses the effective window when it re-fetches or composes. (If the preview only reads an existing `raw.mp4`, no change is needed here beyond the window feeding the render that produced it — note which applies.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py tests/test_render.py -q`
Expected: PASS, existing editorial/render tests unchanged.

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: previous count + new tests, nothing broken.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/editorial.py bin/yt-shorts src/yt_shorts/studio/jobs.py tests/test_editorial.py tests/test_render.py
git commit -m "Render moments by video_id and an editorial window override"
```

---

## Task 6: Orchestrate detection

**Files:**
- Create: `src/yt_shorts/detect.py`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `stream_transcribe.transcribe_stream`, `moments.find_candidates`, `moments.rank_moments`, `moments.measure_loudness_ffmpeg`, `moment_entry.build_entry`, `clipstore.write_clip`
- Produces:
  - `detect.detect_moments(video_id, workspace_dir, event_dir, config, *, stream_title, transcriber=transcribe_stream, measure_loudness=measure_loudness_ffmpeg, progress=None) -> list[str]` — transcribes the stream (D2a), scores and ranks moments, writes candidate clip entries, returns the directory names written

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path

from yt_shorts.lexicon import Lexicon
from yt_shorts.detect import detect_moments
from yt_shorts.stream_transcribe import StreamTranscript


def fake_transcriber(words, audio="a.webm"):
    def transcribe(video_id, workspace_dir, **k):
        p = Path(workspace_dir) / audio
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return StreamTranscript(video_id=video_id, audio_path=p,
                                duration_seconds=max((w["end"] for w in words), default=0),
                                words=words, missing_chunks=[])
    return transcribe


def config(markers):
    return {"lexicon": Lexicon(markers=markers), "detect": {
        "preroll": 8.0, "postroll": 4.0, "top_n": 20, "threshold": 0.5, "min_gap": 20.0}}


def words_at(times, texts):
    return [{"start": t, "end": t + 0.3, "text": " " + w} for t, w in zip(times, texts)]


class TestDetectMoments:
    def test_writes_candidate_entries(self, tmp_path):
        words = words_at([10, 40, 70, 100], ["ok", "fine", "steady", "crash"])
        names = detect_moments(
            "vid", tmp_path / "ws", tmp_path / "ev", config(["crash"]),
            stream_title="ERF Part 1",
            transcriber=fake_transcriber(words),
            measure_loudness=lambda p, s, e: -5.0)
        assert len(names) >= 1
        from yt_shorts import clipstore
        dirs = list(clipstore.iter_clip_dirs(tmp_path / "ev"))
        assert len(dirs) == len(names)
        clip = clipstore.read_clip(dirs[0])
        assert clip["video_id"] == "vid"
        assert clip["source_title"] == "ERF Part 1"

    def test_no_moments_writes_nothing(self, tmp_path):
        words = words_at(list(range(0, 300, 30)), ["ok"] * 10)
        names = detect_moments(
            "vid", tmp_path / "ws", tmp_path / "ev", config([]),
            stream_title="quiet", transcriber=fake_transcriber(words),
            measure_loudness=lambda p, s, e: -5.0)
        assert names == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_detect.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
"""Detect moments in a stream and write them as candidate clip entries.

Ties D2a's transcription to D2b's scoring: transcribe (or reuse the cache),
find emphasis candidates, rank them by loudness in their windows, and write the
top ones as clip entries the studio can curate. Everything heavy is injected
(transcriber, measure_loudness) so this tests without a model or ffmpeg.
"""

from __future__ import annotations

from pathlib import Path

from . import clipstore
from .moment_entry import build_entry
from .moments import find_candidates, measure_loudness_ffmpeg, rank_moments
from .stream_transcribe import transcribe_stream


def _hook_for(words, peak: float, markers) -> str:
    # provisional: the nearest few words around the peak, operator rewrites it
    near = [w["text"].strip() for w in words if peak - 3 <= w["start"] <= peak + 3]
    return " ".join(near[:6]).strip() or "MOMENT"


def detect_moments(video_id, workspace_dir, event_dir, config, *, stream_title,
                   transcriber=transcribe_stream,
                   measure_loudness=measure_loudness_ffmpeg,
                   progress=None) -> list[str]:
    settings = config.get("detect", {})
    lexicon = config["lexicon"]

    transcript = transcriber(video_id, workspace_dir)
    words = transcript.words

    candidates = find_candidates(
        words, lexicon,
        threshold=settings.get("threshold", 1.0),
        min_gap=settings.get("min_gap", 20.0))

    audio_path = transcript.audio_path
    moments = rank_moments(
        candidates, video_id,
        measure_loudness=lambda _p, s, e: measure_loudness(audio_path, s, e),
        preroll=settings.get("preroll", 8.0),
        postroll=settings.get("postroll", 4.0),
        top_n=settings.get("top_n", 20))

    names = []
    for moment in moments:
        entry = build_entry(moment, source_title=stream_title,
                            hook=_hook_for(words, moment.peak, lexicon.markers))
        directory = clipstore.write_clip(event_dir, entry)
        names.append(directory.name)
    return names
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_detect.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: previous count + new tests.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/detect.py tests/test_detect.py
git commit -m "Orchestrate transcription, scoring and loudness into moment entries"
```

---

## Task 7: The studio detect job

**Files:**
- Modify: `src/yt_shorts/studio/jobs.py`, `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_jobs.py`, `tests/test_studio_api.py`

**Interfaces:**
- Produces:
  - `POST /api/streams/{video_id}/detect` — starts a background job running `detect_moments` for the chosen stream; returns a job id; refuses (409) while one already runs for this event
  - job progress readable through the existing `GET /api/jobs/{id}`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_studio_api.py` (follow `TestStreamsRoute` and the render-job tests; stub `detect_moments` at the api module so no real detection runs):

```python
class TestDetectRoute:
    def test_starts_a_detect_job(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "detect_moments", lambda *a, **k: ["m--00000000"])
        r = client.post("/api/streams/vid123/detect")
        assert r.status_code in (200, 202)
        assert "job_id" in r.json()

    def test_refuses_a_second_job_for_the_event(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        import threading
        gate = threading.Event()
        monkeypatch.setattr(api, "detect_moments", lambda *a, **k: gate.wait(5) or [])
        first = client.post("/api/streams/vid123/detect")
        assert first.status_code in (200, 202)
        second = client.post("/api/streams/vid999/detect")
        assert second.status_code == 409
        gate.set()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k Detect`
Expected: FAIL — 404 / 405, the route does not exist

- [ ] **Step 3: Write the implementation**

- Reuse the render `JobStore` / background-job machinery in `studio/jobs.py`: a detect job runs `detect_moments(video_id, workspace_dir, profile.event_dir, profile.config, stream_title=...)` in a thread, recording progress and result, and takes the same per-event guard the render job uses so two jobs cannot run at once for the event.
- `stream_title` comes from the cached `/api/streams` list (match `video_id`); if the stream is not in the cache, fetch or accept a title of `""`.
- `POST /api/streams/{video_id}/detect` starts it and returns `{"job_id": ...}`; a second start while one runs returns 409, mirroring `/api/render`.
- The workspace dir is `workspace.resolve(profile...)`; reuse however the studio already resolves the workspace for renders.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py tests/test_studio_jobs.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/jobs.py src/yt_shorts/studio/api.py tests/test_studio_jobs.py tests/test_studio_api.py
git commit -m "Run moment detection as a studio background job"
```

---

## Task 8: The window-edit API

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Produces:
  - `GET /api/clips/{name}` gains the clip's detected window and the effective window
  - `PATCH /api/clips/{name}` accepts an optional `window: [start, end]` (or `null` to clear), written through `editorial.save` into `edit.json` only

- [ ] **Step 1: Write the failing test**

```python
class TestWindowEdit:
    def test_patching_a_window_persists_to_edit_json(self, event_dir, client):
        from yt_shorts import clipstore, editorial
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/92-104", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 92.0, "end": 104.0,
            "duration": 12.0, "error": None})
        r = client.patch(f"/api/clips/{directory.name}", json={"window": [95.0, 108.0]})
        assert r.status_code == 200
        assert editorial.load(directory).window == (95.0, 108.0)

    def test_clearing_a_window(self, event_dir, client):
        from yt_shorts import clipstore, editorial
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/92-104", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 92.0, "end": 104.0,
            "duration": 12.0, "error": None})
        client.patch(f"/api/clips/{directory.name}", json={"window": [95.0, 108.0]})
        client.patch(f"/api/clips/{directory.name}", json={"window": None})
        assert editorial.load(directory).window is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k Window`
Expected: FAIL — the PATCH model rejects or ignores `window`

- [ ] **Step 3: Write the implementation**

Extend the existing `PATCH /api/clips/{name}` pydantic model with an optional `window: list[float] | None`, validated as two numbers when present. When the field is provided, load the edit, set `window` (a `None` clears it, a two-list sets the tuple), and `editorial.save`. `GET /api/clips/{name}` adds `detected_window` (the clip's `start`/`end`) and `effective_window` (via `editorial.effective_window`). Do not write anything but `edit.json`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: previous count + new tests.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "Edit a moment's window through the studio API"
```

---

## Task 9: The studio UI — stream picker, candidate list, window control

**Files:**
- Modify: `src/yt_shorts/studio/web/` (React + Vite + Mantine, TypeScript), rebuild into `src/yt_shorts/studio/static/`
- Test: `tests/test_studio_e2e.py` (Playwright)

This is a frontend task built to the studio's existing neutral timing-tower look (NOT ERF's colours — see the studio redesign brief). Build it to the API contract from Tasks 1–8; do not change the API.

**What to build:**
- **Stream panel** — calls `GET /api/streams`, dense list (title, duration, view count in tabular figures), a refresh control, and a "Detect moments" action per stream that `POST`s `/api/streams/{video_id}/detect` and shows the job's progress via `GET /api/jobs/{id}` (transcription can take a long time — show it, do not appear frozen).
- **Candidate list** — the detected moments appear in the existing clip list; make their status vocabulary read as candidates (amber), kept (green), discarded (dimmed), each with its text label, consistent with the redesign brief's sector-colour rule. Show the window and duration in tabular figures.
- **Window control** — for a selected moment, a control to nudge lead-in and lead-out; it updates the preview and the shown effective window and, on explicit save, `PATCH`es `window`. Saving stays explicit; the unsaved indicator stays honest.
- Keep/discard reuse the existing status control; rendering reuses the existing render flow.

**Acceptance (E2E, real Chromium):**
- With `/api/streams` stubbed (or seeded) and a detect job stubbed to write two candidate entries, the stream list renders, "Detect moments" starts a job, and when it finishes the two candidates appear in the list.
- Selecting a candidate, changing its window, and saving results in `edit.json` carrying the new window (assert via the Python layer, as the other E2E tests do).
- Contrast and focus-visibility hold to the redesign brief's floor; report the measured ratios for any new surfaces.

**Verification:**
- `npm run build` succeeds and typechecks; `static/` rebuilt and committed; `node_modules` not committed.
- Full pytest suite green, E2E included.
- Drive the real page in a browser, screenshot the stream panel and the candidate/window editor, and look at them before reporting.

- [ ] **Step 1: Build the stream panel and detect action; wire the job progress.**
- [ ] **Step 2: Surface detected candidates in the clip list with the status vocabulary.**
- [ ] **Step 3: Build the window control; persist on explicit save via PATCH.**
- [ ] **Step 4: Update/extend the Playwright E2E for the acceptance above; update selectors rather than weakening assertions.**
- [ ] **Step 5: `npm run build`, rebuild and commit `static/`.**
- [ ] **Step 6: Commit.**

```bash
git add src/yt_shorts/studio/web src/yt_shorts/studio/static tests/test_studio_e2e.py
git commit -m "Add the moment picker and window editor to the studio"
```

---

## Task 10: Documentation

**Files:** `README.md`, `CLAUDE.md`

- [ ] **Step 1:** README — a "Moment detection" section: what it does (pick a stream, detect, curate, render), the `moments.json` lexicon and the detection config keys, that detection is a background job, and that a moment's window is adjusted editorially.
- [ ] **Step 2:** CLAUDE.md — `moments.py` (pure scoring, loudness injected), `detect.py` (orchestration), the moment identity rule (path-encoded because `canonical_url` strips the query), and the editorial `window` override. Note the D-stage arc is complete; upload is stage E.
- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "Document moment detection"
```

---

## Verification for the branch

- Full suite green, E2E included.
- **Detection is deterministic and explainable:** the same transcript yields the same candidates; a reviewer can point at a lexicon hit or a rate spike for each.
- **Identity holds:** two moments in one stream are two clips; re-running detection on an unchanged stream is idempotent (same identities, `write_clip` does not duplicate).
- **Editorial stays separate:** a window edit lands only in `edit.json`; re-running detection does not overwrite it, and the effective window drives the render.
- **One real end-to-end smoke, measured not assumed** (once, outside the suite): on a real short stream slice, run `detect_moments` with the real `measure_loudness_ffmpeg` (transcription may be stubbed with a real short transcript to keep it quick), confirm candidate entries are written with sane windows, and render one to confirm the `video_id` + window path produces a correct 1080x1920 file (`ffprobe` SAR check per CLAUDE.md). Report what you observed.
- **FastAPI stays optional:** `moments.py`, `detect.py`, `moment_entry.py`, `lexicon.py`, `editorial.py` import no FastAPI; the CLI runs without it.

## Self-review notes

Checked against the spec:
- lexicon + speech-rate → candidates, threshold, merge — Task 2
- loudness orders, transcript filters, top-N — Task 3
- window = peak ± preroll/postroll, profile defaults — Task 3, config
- moment → clip entry, distinct identity per (video_id, start, end) — Task 4 (path-encoded because canonical_url strips the query — verified)
- editorial window override, effective window drives render — Task 5
- orchestration transcribe→detect as a background job — Task 6, Task 7
- studio: stream picker, candidate list, window control, keep/discard — Task 9
- lexicon `moments.json` per channel, separate from glossary — Task 1
- config keys (preroll/postroll/min_gap/top_n/threshold) — Tasks 2, 3, 6
- provisional hook, operator rewrites it — Task 6 (`_hook_for`)

Deferred with reason (not gaps): upload/OAuth (stage E); live-chat signal (future); auto-finished hook (operator's, via existing title override).
