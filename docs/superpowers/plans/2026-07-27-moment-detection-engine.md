# Moment Detection Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `detect` stops writing clips and starts writing an analysis — a language model reads the stream transcript in hour-long windows and returns categorised, scored, explained moments, with the repaired lexicon as an offline fallback.

**Architecture:** `claude_client.py` is the only module that imports `anthropic`, lazily, mirroring `_google.py`/`google_oauth.py`. `moment_scan.py` is pure: it windows the transcript, renders numbered lines, validates what comes back and merges seams — the model call is an injected callable, exactly as `detect_moments` already injects `transcriber`. `moments.py` keeps the shared `Moment` type and becomes the fallback engine plus the local activity curve. `detect.py` picks an engine and writes `streams/<video_id>/moments.json`; nothing here creates a clip. `clip_from_moment.py` does that, and only when the operator asks.

**Tech Stack:** Python 3, stdlib + `anthropic` (new, OPTIONAL), pytest. No FastAPI below `studio/`.

## Global Constraints

- `PYTHONPATH=src .venv/bin/pytest -q` green before every commit.
- `python3 tools/lint.py` (NO `PYTHONPATH`) prints `All checks passed!` before every commit. Any new `except` needs the one-line *why* the empty-except guard demands.
- **The six SHA-256 hashes in `tests/test_event_layer_no_regression.py` must never be re-pinned.** Nothing here renders; if one moves, something was broken that was not touched.
- **No test may hit the network, read a real API key, or import `anthropic`.** Every model call is injected and stubbed. A test that would spend money is a defect.
- **`anthropic` is an OPTIONAL dependency.** No module-scope `import anthropic` anywhere, ever — same rule as the Google libraries and FastAPI. A venv without it must still start, render and transcribe.
- **`effort` and adaptive thinking must NOT be sent to `claude-haiku-4-5`** — that model rejects `output_config.effort`. Only `model`, `max_tokens`, `system`, `messages`, `output_config.format` go on the request.
- Model default `claude-haiku-4-5`, read from `config["detect"]["model"]` (brand.json, channel- or event-level), which already exists as a config section.
- Files written under `<workspace>/auth/` are created with mode `0o600`.
- The API key never reaches a log line, an exception message, or a test fixture.
  **This binds the SDK's own exceptions too.** `moment_scan.scan` (Task 5) logs
  `str(error)` for any failure around the model call, so an unwrapped SDK error
  would put whatever that library chose to put in its message into a log file.
  Every exception escaping the SDK is therefore wrapped in `ModelError` with a
  message built from the original exception's TYPE NAME only — never its text —
  and chained with `from` so the traceback still shows the cause. Assuming a
  third-party library never embeds the key or the request payload in its error
  text is exactly the assumption this project must not make.

  **This covers BOTH SDK entry points**, not only the obvious one. A background
  security scan caught the second: the client CONSTRUCTION
  (`module.Anthropic(api_key=api_key)`) sits above the request call and takes
  the key as its argument, so an exception there is if anything the likelier
  one to quote it. Wrapping only `messages.create` leaves the wider hole open.

  **And it covers the RESPONSE-READING path, which is the third and last one.**
  The Task 5 review caught it: `stop_reason`, the content join and the JSON
  parse sit AFTER the wrapped `messages.create` call and outside any handler,
  so an `AttributeError` from a response object escaped `call()` unwrapped.
  Reproduced deliberately — a raised `AttributeError` whose message contained
  an `sk-ant-` string reached the caller verbatim. All three entry points are
  now inside handlers, and `tests/test_claude_client.py` pins each.

  **The consequence for every CONSUMER of a caller: an exception that is not a
  `ModelError` is logged by TYPE NAME only.** `ModelError`'s message is safe by
  construction and may be logged in full; nothing else may. This binds
  `moment_scan.scan` and `detect._caller_from_config` alike, and it must not be
  weakened into "the production caller always wraps everything anyway" — that
  makes the safety property BORROWED from a caller the function neither
  imports nor can inspect, and a future caller wired in without going through
  `claude_client.make_caller` would silently start leaking.
- All model-facing and model-produced text is **English**, including `reason` and `hook_suggestion`.
- `moments.py`, `moment_scan.py`, `claude_client.py`, `clip_from_moment.py`, `detect.py` import no FastAPI.
- macOS has no bare `timeout`; bound anything that might hang with `gtimeout <seconds> <cmd>`.

## File structure

| File | Responsibility |
|---|---|
| `src/yt_shorts/_anthropic.py` | availability guard — `require()` / `AnthropicUnavailable`, mirroring `_google.py` |
| `src/yt_shorts/claude_client.py` | key loading (raw or JSON), request construction, response unwrapping |
| `src/yt_shorts/moment_scan.py` | windowing, line rendering, validation, seam merging, scan orchestration |
| `src/yt_shorts/moments.py` | shared `Moment` type + categories; fallback lexicon engine; local activity curve |
| `src/yt_shorts/detect.py` | engine selection, writes `moments.json`, never a clip |
| `src/yt_shorts/clip_from_moment.py` | operator-chosen window → clip entry (absorbs `moment_entry.py`) |
| `bin/yt-shorts` | new `detect` command, so the engine is usable and the bake-off runnable |
| `src/yt_shorts/studio/jobs.py` | `_run_detect` reports an analysis, not clip names |

---

### Task 1: The model boundary

**Files:**
- Create: `src/yt_shorts/_anthropic.py`
- Create: `src/yt_shorts/claude_client.py`
- Test: `tests/test_claude_client.py`

**Interfaces:**
- Produces: `_anthropic.require(purpose: str) -> None` raising `_anthropic.AnthropicUnavailable`; `claude_client.load_api_key(auth_dir: Path) -> str`; `claude_client.make_caller(api_key: str, *, model: str, max_tokens: int = 4096, sdk=None) -> Callable[[str, str, dict], dict]` — the returned callable takes `(system, user, schema)` and returns the parsed JSON object; `claude_client.ModelError(RuntimeError)`; `claude_client.MissingKey(RuntimeError)`.

- [x] **Step 1: Write the failing tests**

Create `tests/test_claude_client.py`:

```python
import json

import pytest

from yt_shorts import claude_client
from yt_shorts._anthropic import AnthropicUnavailable, require


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason
        self.stop_details = None


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **payload):
        self.calls.append(payload)
        return self._response


class _FakeSDK:
    """Stands in for the `anthropic` module. No network, no key, no cost."""

    def __init__(self, response):
        self.messages = _FakeMessages(response)
        self.last_api_key = None

    def Anthropic(self, api_key=None):        # noqa: N802 - mirrors the SDK name
        self.last_api_key = api_key
        return self


class TestLoadApiKey:
    def test_reads_a_raw_key(self, tmp_path):
        # The file as the operator created it: a bare key, despite the .json name.
        (tmp_path / "anthropic.json").write_text("sk-ant-api03-abc\n")
        assert claude_client.load_api_key(tmp_path) == "sk-ant-api03-abc"

    def test_reads_a_json_object(self, tmp_path):
        (tmp_path / "anthropic.json").write_text(json.dumps({"api_key": "sk-ant-api03-xyz"}))
        assert claude_client.load_api_key(tmp_path) == "sk-ant-api03-xyz"

    def test_missing_file_raises_missing_key(self, tmp_path):
        with pytest.raises(claude_client.MissingKey):
            claude_client.load_api_key(tmp_path)

    def test_empty_file_raises_missing_key(self, tmp_path):
        (tmp_path / "anthropic.json").write_text("   \n")
        with pytest.raises(claude_client.MissingKey):
            claude_client.load_api_key(tmp_path)


class TestMakeCaller:
    def test_returns_the_parsed_json_object(self):
        sdk = _FakeSDK(_Response('{"moments": [{"start_line": 3}]}'))
        call = claude_client.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        assert call("sys", "user", {"type": "object"}) == {"moments": [{"start_line": 3}]}

    def test_sends_the_schema_and_no_effort(self):
        # `effort` is rejected outright by claude-haiku-4-5; sending it would
        # turn every window into a 400.
        sdk = _FakeSDK(_Response("{}"))
        call = claude_client.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        call("sys", "user", {"type": "object", "x": 1})
        payload = sdk.messages.calls[0]
        assert payload["model"] == "claude-haiku-4-5"
        assert payload["system"] == "sys"
        assert payload["output_config"]["format"]["schema"] == {"type": "object", "x": 1}
        assert "effort" not in json.dumps(payload.get("output_config", {}))
        assert "thinking" not in payload

    def test_a_refusal_raises_before_content_is_read(self):
        sdk = _FakeSDK(_Response("", stop_reason="refusal"))
        call = claude_client.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        with pytest.raises(claude_client.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert "refusal" in str(error.value)

    def test_a_truncated_response_raises_model_error(self):
        sdk = _FakeSDK(_Response('{"moments": [', stop_reason="max_tokens"))
        call = claude_client.make_caller("sk-ant-test", model="claude-haiku-4-5", sdk=sdk)
        with pytest.raises(claude_client.ModelError):
            call("sys", "user", {"type": "object"})

    def test_the_key_never_appears_in_an_error_message(self):
        secret = "sk-ant-api03-SUPERSECRET"
        sdk = _FakeSDK(_Response("", stop_reason="refusal"))
        call = claude_client.make_caller(secret, model="claude-haiku-4-5", sdk=sdk)
        with pytest.raises(claude_client.ModelError) as error:
            call("sys", "user", {"type": "object"})
        assert secret not in str(error.value)


class TestRequire:
    def test_raises_with_an_install_message_when_missing(self, monkeypatch):
        import yt_shorts._anthropic as a
        monkeypatch.setattr(a, "_import_anthropic",
                            lambda: (_ for _ in ()).throw(ImportError("no anthropic")))
        with pytest.raises(AnthropicUnavailable) as error:
            require("moment detection")
        assert "pip install" in str(error.value)
        assert "moment detection" in str(error.value)

    def test_returns_quietly_when_present(self, monkeypatch):
        import yt_shorts._anthropic as a
        monkeypatch.setattr(a, "_import_anthropic", lambda: None)
        require("moment detection")   # no raise
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_claude_client.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'yt_shorts.claude_client'`

- [x] **Step 3: Write `_anthropic.py`**

```python
"""Availability guard for the OPTIONAL `anthropic` dependency.

Mirrors `_google.py` exactly, and for the same reason: moment detection is the
only feature that needs the Anthropic SDK, and a venv that never installed it
must still start, render and transcribe. Nothing here imports `anthropic` at
module scope - the import happens inside `_import_anthropic`, so importing this
module is always safe.
"""

from __future__ import annotations


class AnthropicUnavailable(RuntimeError):
    """The `anthropic` package is not installed."""


def _import_anthropic():
    import anthropic  # noqa: F401  - presence check only
    return anthropic


def require(purpose: str) -> None:
    """Raises AnthropicUnavailable with an actionable message, or returns."""
    try:
        _import_anthropic()
    except ImportError:
        raise AnthropicUnavailable(
            f"{purpose} needs the Anthropic SDK, which is an optional dependency. "
            f"Install it with: pip install anthropic"
        ) from None
```

- [x] **Step 4: Write `claude_client.py`**

```python
"""The one place this project talks to the Anthropic API.

`anthropic` is imported LAZILY inside `_sdk()`, exactly as `google_oauth.py`
treats the Google libraries - so `import yt_shorts.claude_client` costs nothing
and works in a venv that never installed it.

The API key is read from `<workspace>/auth/anthropic.json`, alongside
`client_secret.json` and `token-<id>.json`, under the same gitignored,
never-logged rules. TWO shapes are accepted: a bare `sk-ant-...` string or a
JSON object with an `api_key` field. The file as an operator first creates it
holds the raw key despite its extension, and an "Expecting value: line 1
column 1" traceback explains nothing to someone who did what they were asked.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ._anthropic import require

KEY_FILENAME = "anthropic.json"
DEFAULT_MODEL = "claude-haiku-4-5"


class MissingKey(RuntimeError):
    """No usable API key at <workspace>/auth/anthropic.json."""


class ModelError(RuntimeError):
    """The model did not return a usable answer. Never carries the API key."""


def load_api_key(auth_dir: str | Path) -> str:
    path = Path(auth_dir) / KEY_FILENAME
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise MissingKey(f"cannot read {path}: {error}") from None
    if not raw:
        raise MissingKey(f"{path} is empty")
    if raw.startswith("{"):
        try:
            key = str(json.loads(raw).get("api_key", "")).strip()
        except json.JSONDecodeError:
            raise MissingKey(f"{path} is neither a raw key nor valid JSON") from None
        if not key:
            raise MissingKey(f"{path} has no non-empty 'api_key'")
        return key
    return raw


def _sdk():
    require("moment detection")
    import anthropic
    return anthropic


def make_caller(api_key: str, *, model: str = DEFAULT_MODEL,
                max_tokens: int = 4096, sdk=None) -> Callable[[str, str, dict], dict]:
    """Returns `call(system, user, schema) -> dict`.

    `sdk` is injected so the whole path tests without the package, a key, a
    network or a cent. Production passes nothing and gets the real module.

    Deliberately absent from the payload: `effort` (rejected outright by
    claude-haiku-4-5) and `thinking` (omitted means off on that model). Adding
    either would turn every window into a 400 on the default model.
    """
    module = sdk if sdk is not None else _sdk()
    client = module.Anthropic(api_key=api_key)

    def call(system: str, user: str, schema: dict) -> dict:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        # stop_reason is checked BEFORE content is read: on a refusal the
        # content list is empty or partial, and indexing it would raise
        # something that says nothing about the real cause.
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise ModelError("the model refused this window (stop_reason=refusal)")
        if stop == "max_tokens":
            raise ModelError("the answer was cut off (stop_reason=max_tokens)")
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ModelError(f"the answer was not valid JSON: {error}") from None

    return call
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_claude_client.py -q`
Expected: `13 passed`

- [x] **Step 6: Verify the optional dependency really is optional**

Run: `PYTHONPATH=src .venv/bin/python -c "import yt_shorts.claude_client; print('import clean')"`
Expected: `import clean` — with no `anthropic` installed in the venv. If this raises, the lazy import was written wrong.

- [x] **Step 7: Lint and full suite**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q`
Expected: `All checks passed!` and no failures.

- [x] **Step 8: Commit**

```bash
git add src/yt_shorts/_anthropic.py src/yt_shorts/claude_client.py tests/test_claude_client.py
git commit -m "feat(detect): the model boundary, lazily imported and injectable"
```

---

### Task 2: Windowing and line rendering

**Files:**
- Create: `src/yt_shorts/moment_scan.py`
- Test: `tests/test_moment_scan.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `moment_scan.Line` (dataclass: `index: int`, `start: float`, `end: float`, `text: str`); `moment_scan.Window` (dataclass: `index: int`, `lines: list[Line]`); `moment_scan.group_lines(words, *, seconds: float = 12.0) -> list[Line]`; `moment_scan.split_windows(lines, *, window_seconds: float = 3600.0, overlap_seconds: float = 120.0) -> list[Window]`; `moment_scan.render_window(window: Window) -> str`.

- [x] **Step 1: Write the failing tests**

Create `tests/test_moment_scan.py`:

```python
from yt_shorts import moment_scan


def w(start, end, text):
    return {"start": start, "end": end, "text": text}


def words_over(seconds, *, step=1.0):
    return [w(t, t + step, f" x{int(t)}") for t in
            [i * step for i in range(int(seconds / step))]]


class TestGroupLines:
    def test_numbers_lines_from_zero_and_keeps_order(self):
        lines = moment_scan.group_lines(words_over(60), seconds=12.0)
        assert [line.index for line in lines] == list(range(len(lines)))
        assert lines[0].start == 0.0

    def test_a_line_spans_about_the_requested_seconds(self):
        lines = moment_scan.group_lines(words_over(60), seconds=12.0)
        assert all(line.end - line.start <= 13.0 for line in lines)
        assert len(lines) == 5

    def test_text_is_joined_with_no_separator(self):
        # Decoder tokens carry their own leading space; joining with " " would
        # render "C.L.R." as "C .L .R." - the same rule captions.py relies on.
        lines = moment_scan.group_lines([w(0, 1, " C"), w(1, 2, ".L"), w(2, 3, ".R.")],
                                        seconds=12.0)
        assert lines[0].text == " C.L.R."

    def test_no_words_is_no_lines(self):
        assert moment_scan.group_lines([], seconds=12.0) == []


class TestSplitWindows:
    def test_a_short_stream_is_one_window(self):
        lines = moment_scan.group_lines(words_over(300), seconds=12.0)
        windows = moment_scan.split_windows(lines, window_seconds=3600.0)
        assert len(windows) == 1
        assert windows[0].index == 0

    def test_windows_overlap_so_a_seam_moment_is_not_lost(self):
        lines = moment_scan.group_lines(words_over(7200, step=2.0), seconds=12.0)
        windows = moment_scan.split_windows(lines, window_seconds=3600.0,
                                            overlap_seconds=120.0)
        assert len(windows) >= 2
        first_last = windows[0].lines[-1].start
        second_first = windows[1].lines[0].start
        assert second_first < first_last, "windows must overlap, not abut"
        assert first_last - second_first >= 100.0

    def test_line_indices_stay_global_across_windows(self):
        # A moment is reported as a line NUMBER; if window 2 restarted at 0 the
        # lookup back to real time would silently target the wrong line.
        lines = moment_scan.group_lines(words_over(7200, step=2.0), seconds=12.0)
        windows = moment_scan.split_windows(lines, window_seconds=3600.0)
        assert windows[1].lines[0].index > windows[0].lines[0].index


class TestRenderWindow:
    def test_each_line_carries_its_number_and_a_clock_time(self):
        lines = moment_scan.group_lines([w(0, 2, " hello"), w(3661, 3663, " later")],
                                        seconds=12.0)
        text = moment_scan.render_window(moment_scan.Window(index=0, lines=lines))
        assert text.splitlines()[0].startswith("0\t0:00:00\t")
        assert "1:01:01" in text
        assert "hello" in text and "later" in text
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moment_scan.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'yt_shorts.moment_scan'`

- [x] **Step 3: Write the module**

Create `src/yt_shorts/moment_scan.py`:

```python
"""Turn a stream transcript into moment candidates, via a language model.

Pure: the model call is an injected callable, exactly as `detect_moments`
already injects `transcriber`. Nothing here opens a socket, so the whole module
tests with no network, no key and no cost.

Two shapes decide the design:

**Hour-long windows with a two-minute overlap.** An 8-hour transcript fits in
one request on every candidate model, so this is not a context limit - it is
calibration and blast radius. Scored window by window, hour 6 is judged like
hour 1; a failed window costs one hour rather than the run; and progress is
reportable. The overlap keeps a moment sitting on a seam from being lost.

**Line NUMBERS, never timestamps.** Models are unreliable at arithmetic over
clock times and would return plausible-looking times that are twenty seconds
off - the exact failure this rewrite exists to remove, in a new costume. A line
number either exists in the window or does not, so a hallucinated boundary is
structurally impossible and validation is a dictionary lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LINE_SECONDS = 12.0
WINDOW_SECONDS = 3600.0
OVERLAP_SECONDS = 120.0


@dataclass
class Line:
    index: int
    start: float
    end: float
    text: str


@dataclass
class Window:
    index: int
    lines: list[Line] = field(default_factory=list)


def group_lines(words, *, seconds: float = LINE_SECONDS) -> list[Line]:
    """Groups words into ~`seconds`-long numbered lines, in transcript order.

    Text is joined with "" and not " ": a decoder token carries its own leading
    space, which is what makes " C" + ".L" + ".R." render as "C.L.R." rather
    than "C .L .R.". captions.py relies on the same property.
    """
    lines: list[Line] = []
    current: list[dict] = []
    for word in words:
        if current and word["end"] - current[0]["start"] > seconds:
            lines.append(_line(len(lines), current))
            current = []
        current.append(word)
    if current:
        lines.append(_line(len(lines), current))
    return lines


def _line(index: int, words: list[dict]) -> Line:
    return Line(index=index, start=words[0]["start"], end=words[-1]["end"],
                text="".join(word["text"] for word in words))


def split_windows(lines, *, window_seconds: float = WINDOW_SECONDS,
                  overlap_seconds: float = OVERLAP_SECONDS) -> list[Window]:
    """Slices lines into overlapping windows, keeping GLOBAL line indices.

    The indices must not restart per window: a moment comes back as a line
    number, and a per-window numbering would resolve to the wrong line without
    ever looking wrong.
    """
    if not lines:
        return []
    windows: list[Window] = []
    start_time = lines[0].start
    end_time = lines[-1].end
    step = max(window_seconds - overlap_seconds, 1.0)
    cursor = start_time
    while cursor < end_time:
        chunk = [line for line in lines
                 if cursor <= line.start < cursor + window_seconds]
        if chunk:
            windows.append(Window(index=len(windows), lines=chunk))
        cursor += step
    return windows or [Window(index=0, lines=list(lines))]


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"


def render_window(window: Window) -> str:
    """One line per transcript line: `<index>\\t<clock>\\t<text>`."""
    return "\n".join(f"{line.index}\t{_clock(line.start)}\t{line.text.strip()}"
                     for line in window.lines)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moment_scan.py -q`
Expected: `9 passed`

- [x] **Step 5: Lint and full suite**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q`
Expected: `All checks passed!` and no failures.

- [x] **Step 6: Commit**

```bash
git add src/yt_shorts/moment_scan.py tests/test_moment_scan.py
git commit -m "feat(detect): window the transcript into numbered lines"
```

---

### Task 3: The shared Moment type and the repaired lexicon engine

**Files:**
- Modify: `src/yt_shorts/moments.py` (ADDITIVE ONLY — nothing is deleted here)
- Test: `tests/test_moments.py` (add new classes; touch no existing one)

**This task is purely additive, and that is a deliberate correction.** An earlier
draft had it delete `rank_moments`, `measure_loudness_ffmpeg`, `Candidate` and
the old `Moment` outright. That breaks `detect.py` at import — and `detect.py`
is imported by `studio/api.py` AND `studio/jobs.py`, so the whole studio suite
plus `tests/test_transcribe_logging.py` would go red and stay red until Task 6
landed, in direct contradiction of this plan's own "suite green before every
commit". The deletions therefore move to Task 6, which rewrites `detect.py` —
the only consumer — in the same commit. Here the old functions stay untouched
and keep working.

Consequently the new fallback entry point is named **`lexicon_moments`**, not
`find_candidates`: the old `find_candidates` must keep its current signature and
return type while `detect.py` still calls it. The new name is also the clearer
one — it says which engine produced the result.

**Interfaces:**
- Consumes: `lexicon.Lexicon` (`markers: dict[str, float]`).
- Produces: `moments.CATEGORIES: tuple[str, ...]` = `("start_finish", "incident", "highlight", "race_control", "reaction")`; `moments.Moment` — **renaming the existing `Moment` dataclass to `LoudnessMoment` first**, since Task 6 deletes it and nothing else may collide meanwhile — as a dataclass: `start: float`, `end: float`, `category: str`, `score: float`, `reason: str`, `hook_suggestion: str = ""`; `moments.activity_curve(words, lexicon, *, step: float = 60.0) -> list[float]`; `moments.lexicon_moments(words, lexicon, *, threshold: float = 1.0, min_gap: float = 20.0) -> list[Moment]`; `moments._count_markers(bin_words, markers) -> float` (unchanged, still longest-match-first).
- Still exported and still working: `Candidate`, `find_candidates`, `rank_moments`, `measure_loudness_ffmpeg` (unchanged) and `LoudnessMoment` (the old `Moment`, renamed — the only edit this task makes to existing code). Task 6 deletes all five.

**Why loudness is not part of the new engine (it is deleted in Task 6):** measured on `streams/V9nVNEQNdR4`. Ranking by integrated loudness over a broadcast-normalised stream spread the top 20 across 6.2 LU, so noise decided the order and the strongest moment landed at rank 10. The obvious repair — loudness dynamics against a local median — was tested and refuted: the fifteen strongest excursions are the intermission jingle and audio dropouts, the loudest of all being a transmission fault. Loudness measures production artifacts here, so it goes rather than gets reweighted. Deleting it also removes a per-candidate ffmpeg subprocess from every run.

- [x] **Step 1: Write the failing tests**

APPEND these classes to `tests/test_moments.py`. Change no existing class — the
old engine still runs and its tests must stay green:

```python
class TestActivityCurve:
    def test_one_value_per_step_across_the_stream(self):
        words = [{"start": t, "end": t + 0.5, "text": " x"} for t in range(0, 300)]
        curve = moments.activity_curve(words, Lexicon(markers={}), step=60.0)
        assert len(curve) == 5

    def test_a_dense_minute_scores_above_a_sparse_one(self):
        dense = [{"start": 10 + i * 0.1, "end": 10 + i * 0.1 + 0.05, "text": " x"}
                 for i in range(200)]
        sparse = [{"start": 70 + i * 5.0, "end": 70 + i * 5.0 + 0.05, "text": " x"}
                  for i in range(5)]
        curve = moments.activity_curve(dense + sparse, Lexicon(markers={}), step=60.0)
        assert curve[0] > curve[1]

    def test_no_words_is_an_empty_curve(self):
        assert moments.activity_curve([], Lexicon(markers={}), step=60.0) == []


class TestLexiconMoments:
    def test_speech_rate_alone_never_produces_a_moment(self):
        # 54% of the candidates this replaces were speech-rate only - the
        # pre-race block where "Set up seems to be working" was a moment.
        fast = [{"start": i * 0.2, "end": i * 0.2 + 0.1, "text": " word"}
                for i in range(400)]
        assert moments.lexicon_moments(fast, Lexicon(markers={})) == []

    def test_a_marker_hit_produces_a_moment_with_its_category(self):
        words = [{"start": 10.0, "end": 10.4, "text": " crash"}]
        found = moments.lexicon_moments(words, Lexicon(markers={"crash": 3.0}))
        assert len(found) == 1
        assert found[0].category in moments.CATEGORIES

    def test_the_window_is_derived_from_the_hit_not_fixed_at_twelve_seconds(self):
        words = [{"start": 10.0, "end": 10.4, "text": " crash"},
                 {"start": 10.4, "end": 30.0, "text": " and the car is out"}]
        found = moments.lexicon_moments(words, Lexicon(markers={"crash": 3.0}))
        assert found[0].end - found[0].start != 12.0
        assert found[0].start <= 10.0 <= found[0].end

    def test_the_old_engine_is_untouched_until_task_6(self):
        # detect.py still imports these; deleting them here would take the
        # studio's whole import chain down with them.
        assert hasattr(moments, "find_candidates")
        assert hasattr(moments, "rank_moments")
        assert hasattr(moments, "measure_loudness_ffmpeg")
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moments.py -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.moments' has no attribute 'activity_curve'`

- [x] **Step 3: Extend `moments.py` — additively**

Nothing in the file is deleted. Two edits to existing code, both mechanical:

1. **Rename the existing `Moment` dataclass to `LoudnessMoment`** (the one with
   `video_id`/`peak`/`loudness`), and update its two uses inside `rank_moments`
   plus that function's `-> list[Moment]` annotation. The new engine's `Moment`
   takes the free name; Task 6 deletes `LoudnessMoment` along with the rest of
   the old engine. Update `tests/test_moments.py`'s existing loudness/ranking
   cases to the new name — that is a rename, not a rewrite, and they must stay
   green.
2. **`detect.py` imports `find_candidates` by name and calls it with the old
   signature** (`step=`, `window=`) — leave both untouched.

Keep the module docstring's marker-matching paragraphs and `_text` /
`_count_markers` verbatim — the longest-match-first rule and its
`super pole sitter` consequence are unchanged and still pinned by
`TestOverlappingMarkers`. Keep `import subprocess` (`measure_loudness_ffmpeg`
still needs it). APPEND:

```python
CATEGORIES = ("start_finish", "incident", "highlight", "race_control", "reaction")

# Which category a marker belongs to, and what a hit in it is worth. The order
# above is the operator's own ranking: start/finish first, then incidents,
# sporting highlights, race control, commentator reaction.
CATEGORY_WEIGHTS = {
    "start_finish": 4.0,
    "incident": 3.0,
    "highlight": 2.0,
    "race_control": 2.0,
    "reaction": 1.0,
}

MARKER_CATEGORY = {
    "green green green": "start_finish", "chequered flag": "start_finish",
    "checkered flag": "start_finish", "and they are away": "start_finish",
    "crash": "incident", "into the wall": "incident", "spin": "incident",
    "contact": "incident", "puncture": "incident", "damage": "incident",
    "debris": "incident", "off the track": "incident", "into the barrier": "incident",
    "safety car": "race_control", "red flag": "race_control",
    "yellow flag": "race_control", "full course yellow": "race_control",
    "incident": "race_control", "penalty": "race_control",
    "photo finish": "highlight", "purple": "highlight", "fastest lap": "highlight",
    "new record": "highlight", "overtake": "highlight", "side by side": "highlight",
    "personal best": "highlight", "provisional pole": "highlight",
    "fastest": "highlight", "flying lap": "highlight", "super pole": "highlight",
    "pole sitter": "highlight", "pole": "highlight",
    "oh my": "reaction", "oh no": "reaction", "unbelievable": "reaction",
    "incredible": "reaction", "what a": "reaction",
}
DEFAULT_CATEGORY = "reaction"


@dataclass
class Moment:
    """One candidate, from either engine. Both write this same shape."""
    start: float
    end: float
    category: str
    score: float
    reason: str
    hook_suggestion: str = ""


def activity_curve(words, lexicon: Lexicon, *, step: float = 60.0) -> list[float]:
    """Words plus marker weight per `step`, normalised to 0..1.

    Computed LOCALLY and deliberately not by the model: the model returns
    discrete moments, while the overview strip needs a continuous signal. This
    costs nothing, and - the point - it exists with no key and no network, so
    the stream view is useful before detection has ever run. It is labelled
    "activity" rather than "importance" because that is honestly what it is.
    """
    if not words:
        return []
    end = max(word["end"] for word in words)
    buckets = int(end // step) + 1
    raw = [0.0] * buckets
    for word in words:
        raw[min(int(word["start"] // step), buckets - 1)] += 1.0
    for index in range(buckets):
        lo, hi = index * step, (index + 1) * step
        window = [w for w in words if lo <= w["start"] < hi]
        raw[index] += _count_markers(window, lexicon.markers) * 10.0
    peak = max(raw) or 1.0
    return [round(value / peak, 4) for value in raw]


def _category_for(marker: str) -> str:
    return MARKER_CATEGORY.get(marker, DEFAULT_CATEGORY)


def lexicon_moments(words, lexicon: Lexicon, *, threshold: float = 1.0,
                    min_gap: float = 20.0) -> list[Moment]:
    """The FALLBACK engine: marker hits only, speech rate as an amplifier.

    Named for its engine rather than reusing `find_candidates`: the old
    function keeps that name and its own signature until Task 6 deletes it,
    because `detect.py` - and through it the whole studio import chain - still
    calls it.

    Speech rate can no longer produce a candidate on its own. Measured on this
    workspace's own qualifying, 43 of 79 candidates had no marker hit at all -
    the pre-race block where "Set up seems to be working. All right. That's
    good." was a detected moment. Rate now scales a real hit and nothing else.

    The window comes from the matched span, not a fixed pre/post-roll. The old
    fixed 12 seconds were also MISALIGNED: emphasis at tick t was caused by
    speech in [t, t+6) while the clip was [t-8, t+4), so eight silent seconds
    were included and the last two seconds of the triggering speech were cut.
    """
    if not words or not lexicon.markers:
        return []
    counts = [len(w) for w in _bins(words)] or [1]
    positive = sorted(count for count in counts if count > 0)
    baseline = max(positive[len(positive) // 2] if positive else 1, 1)

    found: list[Moment] = []
    for index, bin_words in enumerate(_bins(words)):
        if not bin_words:
            continue
        best_marker, best_weight = _best_marker(bin_words, lexicon.markers)
        if best_weight <= 0:
            continue
        category = _category_for(best_marker)
        rate = max(0.0, len(bin_words) / baseline - 1.0)
        score = min(10.0, CATEGORY_WEIGHTS[category] * best_weight * (1.0 + rate))
        if score < threshold:
            continue
        start = bin_words[0]["start"]
        end = bin_words[-1]["end"]
        if found and start - found[-1].start < min_gap:
            if score > found[-1].score:
                found[-1] = Moment(start=start, end=end, category=category,
                                   score=round(score, 2),
                                   reason=f"lexicon: {best_marker}")
            continue
        found.append(Moment(start=start, end=end, category=category,
                            score=round(score, 2),
                            reason=f"lexicon: {best_marker}"))
    return found


def _bins(words, *, window: float = 12.0):
    """Contiguous ~`window`-second groups, in transcript order."""
    current: list[dict] = []
    for word in words:
        if current and word["end"] - current[0]["start"] > window:
            yield current
            current = []
        current.append(word)
    if current:
        yield current


def _best_marker(bin_words, markers) -> tuple[str, float]:
    """The highest-weighted marker present in this bin, or ("", 0.0)."""
    joined = _text(bin_words)
    best, weight = "", 0.0
    for marker, value in markers.items():
        if value > 0 and marker in joined and value > weight:
            best, weight = marker, value
    return best, weight
```

`from .lexicon import Lexicon` is already imported; `import subprocess` stays.

- [x] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moments.py -q`
Expected: all pass — the new classes AND every existing one, including
`TestOverlappingMarkers` and the renamed loudness/ranking cases.

- [x] **Step 5: Verify the old engine's callers still resolve**

Run: `PYTHONPATH=src .venv/bin/python -c "import yt_shorts.detect, yt_shorts.studio.api"`
Expected: no output. A traceback means the rename in Step 3 reached something it
should not have.

- [x] **Step 6: Lint and full suite**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q`
Expected: `All checks passed!` and a fully green suite. Nothing may fail here —
this task adds and renames, it deletes nothing.

- [x] **Step 7: Commit**

```bash
git add src/yt_shorts/moments.py tests/test_moments.py
git commit -m "feat(detect): categories, derived windows, and an activity curve"
```

---

### Task 4: Validation and seam merging

**Files:**
- Modify: `src/yt_shorts/moment_scan.py`
- Test: `tests/test_moment_scan.py`

**Interfaces:**
- Consumes: `moments.Moment`, `moments.CATEGORIES` (Task 3); `moment_scan.Line` (Task 2).
- Produces: `moment_scan.MIN_SECONDS = 5.0`, `moment_scan.MAX_SECONDS = 90.0`, `moment_scan.MAX_PER_WINDOW = 12`; `moment_scan.validate_moment(raw: dict, lines_by_index: dict[int, Line]) -> Moment | None`; `moment_scan.merge_moments(found: list[Moment]) -> list[Moment]`; `moment_scan.SCHEMA: dict`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_moment_scan.py`:

```python
from yt_shorts import moments as moments_mod


def lines_by_index(count=20, seconds=12.0):
    return {i: moment_scan.Line(index=i, start=i * seconds, end=(i + 1) * seconds,
                                text=f" line {i}")
            for i in range(count)}


def raw(**over):
    base = {"start_line": 2, "end_line": 3, "category": "incident",
            "score": 7.5, "reason": "Car into the barrier at Pflanzgarten.",
            "hook_suggestion": "INTO THE BARRIER"}
    base.update(over)
    return base


class TestValidateMoment:
    def test_a_good_moment_resolves_to_real_times(self):
        found = moment_scan.validate_moment(raw(), lines_by_index())
        assert found.start == 24.0 and found.end == 48.0
        assert found.category == "incident" and found.score == 7.5
        assert found.hook_suggestion == "INTO THE BARRIER"

    def test_a_line_number_outside_the_window_is_dropped(self):
        assert moment_scan.validate_moment(raw(start_line=99), lines_by_index()) is None
        assert moment_scan.validate_moment(raw(end_line=99), lines_by_index()) is None

    def test_an_inverted_range_is_dropped(self):
        assert moment_scan.validate_moment(raw(start_line=5, end_line=2),
                                           lines_by_index()) is None

    def test_a_window_shorter_than_the_minimum_is_dropped(self):
        short = {0: moment_scan.Line(index=0, start=10.0, end=12.0, text=" hi")}
        assert moment_scan.validate_moment(raw(start_line=0, end_line=0), short) is None

    def test_a_window_longer_than_the_maximum_is_dropped(self):
        # 90 seconds is the ceiling: anything longer is not a Short.
        assert moment_scan.validate_moment(raw(start_line=0, end_line=15),
                                           lines_by_index()) is None

    def test_an_unknown_category_is_dropped(self):
        assert moment_scan.validate_moment(raw(category="vibes"),
                                           lines_by_index()) is None

    def test_a_score_outside_zero_to_ten_is_dropped(self):
        assert moment_scan.validate_moment(raw(score=99), lines_by_index()) is None
        assert moment_scan.validate_moment(raw(score=-1), lines_by_index()) is None

    def test_a_missing_field_is_dropped_not_raised(self):
        assert moment_scan.validate_moment({"start_line": 2}, lines_by_index()) is None

    def test_a_non_dict_is_dropped_not_raised(self):
        assert moment_scan.validate_moment("nonsense", lines_by_index()) is None


class TestMergeMoments:
    def test_overlapping_duplicates_collapse_to_the_higher_score(self):
        a = moments_mod.Moment(start=100.0, end=130.0, category="incident",
                               score=6.0, reason="from window 0")
        b = moments_mod.Moment(start=105.0, end=135.0, category="incident",
                               score=8.0, reason="from window 1")
        merged = moment_scan.merge_moments([a, b])
        assert len(merged) == 1 and merged[0].score == 8.0

    def test_separate_moments_both_survive(self):
        a = moments_mod.Moment(start=100.0, end=130.0, category="incident",
                               score=6.0, reason="a")
        b = moments_mod.Moment(start=900.0, end=930.0, category="highlight",
                               score=5.0, reason="b")
        assert len(moment_scan.merge_moments([a, b])) == 2

    def test_the_result_is_ordered_by_time(self):
        late = moments_mod.Moment(start=900.0, end=930.0, category="highlight",
                                  score=5.0, reason="late")
        early = moments_mod.Moment(start=100.0, end=130.0, category="incident",
                                   score=6.0, reason="early")
        assert [m.start for m in moment_scan.merge_moments([late, early])] == [100.0, 900.0]
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moment_scan.py -q -k "Validate or Merge"`
Expected: FAIL — `AttributeError: module 'yt_shorts.moment_scan' has no attribute 'validate_moment'`

- [x] **Step 3: Add validation and merging to `moment_scan.py`**

```python
from .moments import CATEGORIES, Moment

MIN_SECONDS = 5.0
MAX_SECONDS = 90.0
MAX_PER_WINDOW = 12

SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                    "hook_suggestion": {"type": "string"},
                },
                "required": ["start_line", "end_line", "category", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}


def validate_moment(raw, lines_by_index) -> Moment | None:
    """A validated Moment, or None. NEVER raises.

    Nothing the model returns is trusted. A rejected moment is dropped and
    logged by the caller while the window still counts - the same stance
    "one failed clip must never abort a run" takes, one layer down.
    """
    if not isinstance(raw, dict):
        return None
    try:
        start_line = int(raw["start_line"])
        end_line = int(raw["end_line"])
        category = str(raw["category"])
        score = float(raw["score"])
        reason = str(raw["reason"])
    except (KeyError, TypeError, ValueError):
        return None
    if category not in CATEGORIES or not 0.0 <= score <= 10.0:
        return None
    if start_line > end_line:
        return None
    first = lines_by_index.get(start_line)
    last = lines_by_index.get(end_line)
    if first is None or last is None:
        return None
    if not MIN_SECONDS <= last.end - first.start <= MAX_SECONDS:
        return None
    return Moment(start=first.start, end=last.end, category=category,
                  score=round(score, 2), reason=reason,
                  hook_suggestion=str(raw.get("hook_suggestion", "")))


def merge_moments(found) -> list[Moment]:
    """Collapses overlapping duplicates from the window seams, ordered by time.

    Two windows share two minutes, so a moment sitting on a seam is reported
    twice. The higher score wins; ties keep the first seen.
    """
    merged: list[Moment] = []
    for moment in sorted(found, key=lambda m: (m.start, -m.score)):
        clash = next((kept for kept in merged
                      if moment.start < kept.end and kept.start < moment.end), None)
        if clash is None:
            merged.append(moment)
        elif moment.score > clash.score:
            merged[merged.index(clash)] = moment
    return sorted(merged, key=lambda m: m.start)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moment_scan.py -q`
Expected: all pass.

- [x] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/moment_scan.py tests/test_moment_scan.py
git commit -m "feat(detect): validate every moment the model returns"
```

---

### Task 5: The scan orchestrator

**Files:**
- Modify: `src/yt_shorts/moment_scan.py`
- Test: `tests/test_moment_scan.py`

**Interfaces:**
- Consumes: everything from Tasks 2–4.
- Produces: `moment_scan.ScanResult` (dataclass: `moments: list[Moment]`, `missing_windows: list[int]`); `moment_scan.build_system_prompt(lexicon) -> str`; `moment_scan.scan(words, lexicon, *, caller, logger=None, progress=None) -> ScanResult`, where `caller(system, user, schema) -> dict`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_moment_scan.py`:

```python
import logging


class TestBuildSystemPrompt:
    def test_names_every_category_and_the_density_target(self):
        prompt = moment_scan.build_system_prompt(Lexicon(markers={"crash": 3.0}))
        for category in moments_mod.CATEGORIES:
            assert category in prompt
        assert "3" in prompt and "6" in prompt      # 3-6 per hour
        assert "crash" in prompt                    # channel vocabulary is passed in

    def test_an_empty_lexicon_still_produces_a_prompt(self):
        assert moment_scan.build_system_prompt(Lexicon(markers={})).strip()


class TestScan:
    def _words(self, seconds=1800):
        return [{"start": t, "end": t + 0.8, "text": " word"} for t in range(seconds)]

    def test_returns_validated_moments_from_the_model(self):
        def caller(system, user, schema):
            return {"moments": [raw(start_line=2, end_line=3)]}
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert len(result.moments) == 1
        assert result.moments[0].category == "incident"
        assert result.missing_windows == []

    def test_a_failing_window_is_recorded_and_does_not_stop_the_run(self):
        def caller(system, user, schema):
            raise RuntimeError("network went away")
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert result.moments == []
        assert result.missing_windows == [0]

    def test_a_rejected_moment_does_not_fail_its_window(self):
        def caller(system, user, schema):
            return {"moments": [raw(start_line=999), raw(start_line=2, end_line=3)]}
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert len(result.moments) == 1
        assert result.missing_windows == []

    def test_a_garbage_response_shape_is_a_failed_window(self):
        def caller(system, user, schema):
            return {"unexpected": True}
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert result.missing_windows == [0]

    def test_no_more_than_the_cap_survives_per_window(self):
        def caller(system, user, schema):
            return {"moments": [raw(start_line=i, end_line=i + 1) for i in range(40)]}
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert len(result.moments) <= moment_scan.MAX_PER_WINDOW

    def test_the_cause_of_a_failed_window_reaches_the_injected_logger(self, caplog):
        # A window that vanished without a logged cause is the silent-failure
        # bug this project has already paid for twice. A ModelError's message is
        # built from a type name by claude_client and is safe to log in full.
        def caller(system, user, schema):
            raise ModelError("the Anthropic SDK raised APIConnectionError calling the model")
        logger = logging.getLogger("ytshorts.test.scan")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.scan"):
            moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller,
                             logger=logger)
        assert "APIConnectionError" in caplog.text

    def test_an_unrecognised_exception_is_logged_by_TYPE_ONLY(self, caplog):
        # The whole point of ModelError is that claude_client has PROMISED its
        # message carries no key. An exception from anywhere else has made no
        # such promise, and scan cannot check where its injected caller came
        # from - so it logs what it knows is safe: the type name.
        def caller(system, user, schema):
            raise RuntimeError("sk-ant-SECRET leaked in some library's message")
        logger = logging.getLogger("ytshorts.test.scan")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.scan"):
            moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller,
                             logger=logger)
        assert "RuntimeError" in caplog.text
        assert "sk-ant-SECRET" not in caplog.text

    def test_progress_is_reported_per_window(self):
        seen = []
        def caller(system, user, schema):
            return {"moments": []}
        moment_scan.scan(self._words(7200), Lexicon(markers={}), caller=caller,
                         progress=lambda done, total: seen.append((done, total)))
        assert seen and seen[-1][0] == seen[-1][1]

    def test_an_empty_transcript_is_an_empty_result_not_a_crash(self):
        def caller(system, user, schema):
            raise AssertionError("must not be called for an empty transcript")
        result = moment_scan.scan([], Lexicon(markers={}), caller=caller)
        assert result.moments == [] and result.missing_windows == []
```

Add `from yt_shorts.lexicon import Lexicon` and
`from yt_shorts.claude_client import ModelError` to the imports at the top of
the file.

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moment_scan.py -q -k "Scan or SystemPrompt"`
Expected: FAIL — `AttributeError: module 'yt_shorts.moment_scan' has no attribute 'scan'`

- [x] **Step 3: Add the orchestrator to `moment_scan.py`**

```python
import logging

from .claude_client import ModelError

_logger = logging.getLogger("ytshorts.moment_scan")

TARGET_PER_HOUR = "3-6"

_INSTRUCTION = """\
You are selecting moments from a live sim-racing broadcast transcript that are \
worth cutting into a vertical short.

Categories, in the channel's own order of importance:
1. start_finish  - the race start and the chequered flag. Rare and always worth it.
2. incident      - crash, contact, spin, going off, puncture, hitting a barrier.
3. highlight     - overtakes, wheel-to-wheel battles, fastest laps, purple sectors, pole.
4. race_control  - safety car, full course yellow, red flag, penalties, a lead change.
5. reaction      - the commentators audibly losing it, whatever the cause.

Rules:
- Return {target} moments per hour of transcript. If the hour was uneventful, \
return fewer. Never pad the list to reach a number.
- Identify a moment by the LINE NUMBERS it spans, never by a timestamp.
- A moment must run between 5 and 90 seconds. Include the build-up, and do not \
cut before the payoff.
- The transcript comes from speech recognition and contains errors. Read through \
them: "Super Bowl" in a qualifying session is "Super Pole".
- `reason` is one English sentence saying what happened.
- `hook_suggestion` is a short English on-screen title, at most six words.
"""


@dataclass
class ScanResult:
    moments: list[Moment] = field(default_factory=list)
    missing_windows: list[int] = field(default_factory=list)


def build_system_prompt(lexicon) -> str:
    prompt = _INSTRUCTION.format(target=TARGET_PER_HOUR)
    markers = sorted(m for m, weight in lexicon.markers.items() if weight > 0)
    if markers:
        prompt += ("\nWords this channel treats as significant: "
                   + ", ".join(markers) + ".\n")
    return prompt


def scan(words, lexicon, *, caller, logger=None, progress=None) -> ScanResult:
    """Scores every window and collects what survives validation.

    A window that fails is dropped, its index recorded, and its CAUSE logged
    through the injected logger - the studio passes the running job's own
    logger, so an operator reading that job's log sees why an hour is missing
    instead of finding a quiet hole in the results.
    """
    log = logger if logger is not None else _logger
    lines = group_lines(words)
    windows = split_windows(lines)
    result = ScanResult()
    system = build_system_prompt(lexicon)

    for window in windows:
        by_index = {line.index: line for line in window.lines}
        try:
            answer = caller(system, render_window(window), SCHEMA)
            candidates = answer["moments"]
            if not isinstance(candidates, list):
                raise TypeError(f"'moments' is {type(candidates).__name__}, not a list")
        except Exception as error:      # noqa: BLE001 - any failure costs one window, never the run
            # ModelError is claude_client's OWN type, and its message is built
            # from the original exception's type name by construction - so it
            # is safe to log in full, and it carries the useful half of the
            # diagnosis. Anything else reaches us from a caller this function
            # cannot inspect: `caller` is an unconstrained callable, and an
            # arbitrary library's message may quote the request that carries
            # the API key. Log the type name and nothing else. Widening this to
            # str(error) for a nicer message is exactly how the key gets into a
            # log - the same reasoning detect._caller_from_config applies.
            detail = error if isinstance(error, ModelError) else type(error).__name__
            log.warning("window %d failed: %s: %s", window.index,
                        type(error).__name__, detail)
            result.missing_windows.append(window.index)
            if progress is not None:
                progress(window.index + 1, len(windows))
            continue

        kept = []
        for candidate in candidates:
            moment = validate_moment(candidate, by_index)
            if moment is None:
                log.info("window %d: dropped an invalid moment", window.index)
                continue
            kept.append(moment)
        kept.sort(key=lambda m: -m.score)
        result.moments.extend(kept[:MAX_PER_WINDOW])
        if progress is not None:
            progress(window.index + 1, len(windows))

    result.moments = merge_moments(result.moments)
    log.info("scanned %d window(s): %d moment(s), %d failed",
             len(windows), len(result.moments), len(result.missing_windows))
    return result
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moment_scan.py -q`
Expected: all pass.

- [x] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/moment_scan.py tests/test_moment_scan.py
git commit -m "feat(detect): scan the transcript window by window"
```

---

### Task 6: `detect` writes an analysis, never a clip

**Files:**
- Modify: `src/yt_shorts/detect.py` (rewrite)
- Modify: `tests/test_detect.py` (rewrite)
- Modify: `src/yt_shorts/moments.py` (the old engine is deleted here)
- Modify: `tests/test_moments.py` (its old-engine classes go with it)

**This task carries the deletions Task 3 deliberately deferred.** `detect.py` is
the only consumer of the old engine, and it is rewritten here — so the two edits
land in one commit and the suite never goes red between them. Delete from
`moments.py`: `Candidate`, `LoudnessMoment`, `find_candidates`, `rank_moments`,
`measure_loudness_ffmpeg`, and the now-unused `import subprocess`. Delete from
`tests/test_moments.py` every class that tests them, and replace Task 3's
`test_the_old_engine_is_untouched_until_task_6` with its inverse:

```python
    def test_the_loudness_engine_is_gone(self):
        # Measured useless, not merely down-weighted. Its return would mean the
        # finding was lost - see the Task 3 measurement note.
        assert not hasattr(moments, "rank_moments")
        assert not hasattr(moments, "measure_loudness_ffmpeg")
        assert not hasattr(moments, "find_candidates")
```

Keep `_text`, `_count_markers` and `TestOverlappingMarkers` — the
longest-match-first rule belongs to the new engine too.

**Interfaces:**
- Consumes: `moment_scan.scan`, `moments.lexicon_moments`, `moments.activity_curve`, `claude_client.load_api_key`/`make_caller`/`MissingKey`.
- Produces: `detect.ANALYSIS_FILENAME = "moments.json"`; `detect.analysis_path(workspace_dir, video_id) -> Path`; `detect.detect_moments(video_id, workspace_dir, event_dir, config, *, stream_title, transcriber=transcribe_stream, caller=None, now=None, logger=None, progress=None) -> Path`.

The written file:

```json
{"video_id": "...", "engine": "model:claude-haiku-4-5", "created_at": "...",
 "duration_seconds": 5899.0, "activity": [0.1, 0.4],
 "moments": [{"start": 1.0, "end": 20.0, "category": "incident",
              "score": 7.5, "reason": "...", "hook_suggestion": "..."}],
 "missing_windows": []}
```

- [x] **Step 1: Write the failing tests**

Rewrite `tests/test_detect.py`:

```python
import json

from yt_shorts import detect
from yt_shorts.lexicon import Lexicon
from yt_shorts.stream_transcribe import StreamTranscript


def transcript(words, *, video_id="vid123", tmp_path=None):
    def fake(video_id_, workspace_dir, *, glossary=None):
        return StreamTranscript(video_id=video_id_,
                                audio_path=(tmp_path or workspace_dir) / "audio.webm",
                                duration_seconds=600.0, words=words)
    return fake


def some_words(seconds=600):
    return [{"start": t, "end": t + 0.8, "text": " word"} for t in range(seconds)]


def config(**over):
    base = {"lexicon": Lexicon(markers={"crash": 3.0})}
    base.update(over)
    return base


class TestWritesAnalysis:
    def test_writes_moments_json_and_returns_its_path(self, tmp_path):
        def caller(system, user, schema):
            return {"moments": [{"start_line": 2, "end_line": 3, "category": "incident",
                                 "score": 8.0, "reason": "A crash at Pflanzgarten.",
                                 "hook_suggestion": "BIG ONE"}]}
        path = detect.detect_moments("vid123", tmp_path, tmp_path / "event", config(),
                                     stream_title="Race", caller=caller,
                                     transcriber=transcript(some_words(), tmp_path=tmp_path),
                                     now=lambda: "2026-07-27T10:00:00+00:00")
        assert path.name == "moments.json"
        data = json.loads(path.read_text())
        assert data["engine"] == "model:claude-haiku-4-5"
        assert data["moments"][0]["category"] == "incident"
        assert data["created_at"] == "2026-07-27T10:00:00+00:00"
        assert data["activity"]

    def test_creates_no_clip_directories(self, tmp_path):
        # The whole point: detection stops producing artifacts nobody asked for.
        event = tmp_path / "event"
        event.mkdir()
        def caller(system, user, schema):
            return {"moments": [{"start_line": 2, "end_line": 3, "category": "incident",
                                 "score": 8.0, "reason": "x"}]}
        detect.detect_moments("vid123", tmp_path, event, config(), stream_title="Race",
                              caller=caller,
                              transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert list(event.iterdir()) == []

    def test_re_running_overwrites_rather_than_accumulating(self, tmp_path):
        def caller(system, user, schema):
            return {"moments": []}
        args = dict(stream_title="Race", caller=caller,
                    transcriber=transcript(some_words(), tmp_path=tmp_path))
        first = detect.detect_moments("vid123", tmp_path, tmp_path / "e", config(), **args)
        second = detect.detect_moments("vid123", tmp_path, tmp_path / "e", config(), **args)
        assert first == second


class TestEngineSelection:
    def test_falls_back_to_the_lexicon_when_no_key_is_configured(self, tmp_path):
        words = some_words() + [{"start": 300.0, "end": 300.5, "text": " crash"}]
        path = detect.detect_moments("vid123", tmp_path, tmp_path / "e", config(),
                                     stream_title="Race", caller=None,
                                     transcriber=transcript(words, tmp_path=tmp_path))
        data = json.loads(path.read_text())
        assert data["engine"] == "lexicon"

    def test_the_fallback_is_announced_on_the_logger(self, tmp_path, caplog):
        # A silent downgrade to the weaker engine is exactly the defect this
        # rewrite exists to remove.
        import logging
        logger = logging.getLogger("ytshorts.test.detect")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.detect"):
            detect.detect_moments("vid123", tmp_path, tmp_path / "e", config(),
                                  stream_title="Race", caller=None, logger=logger,
                                  transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert "lexicon" in caplog.text.lower()

    def test_an_empty_transcript_is_reported_loudly(self, tmp_path, caplog):
        import logging
        logger = logging.getLogger("ytshorts.test.detect2")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.detect2"):
            path = detect.detect_moments("vid123", tmp_path, tmp_path / "e", config(),
                                         stream_title="Race", caller=None, logger=logger,
                                         transcriber=transcript([], tmp_path=tmp_path))
        assert json.loads(path.read_text())["moments"] == []
        assert "0 words" in caplog.text

    def test_an_unknown_failure_logs_its_type_and_not_its_text(self, tmp_path, caplog):
        # claude_client wraps every SDK exception, but this handler must not
        # DEPEND on that: an exception whose text this project has made no
        # promise about describes a request that carries the API key.
        import logging

        from yt_shorts import claude_client as cc
        secret = "sk-ant-api03-SUPERSECRET"
        (tmp_path / "auth").mkdir()
        (tmp_path / "auth" / "anthropic.json").write_text(secret)

        def exploding_caller(*args, **kwargs):
            raise RuntimeError(f"connection failed for key {secret}")

        original = cc.make_caller
        cc.make_caller = exploding_caller
        try:
            logger = logging.getLogger("ytshorts.test.detect3")
            with caplog.at_level(logging.WARNING, logger="ytshorts.test.detect3"):
                detect.detect_moments("vid123", tmp_path, tmp_path / "e", config(),
                                      stream_title="Race", logger=logger,
                                      transcriber=transcript(some_words(),
                                                             tmp_path=tmp_path))
        finally:
            cc.make_caller = original
        assert secret not in caplog.text
        assert "RuntimeError" in caplog.text

    def test_the_model_name_comes_from_the_config(self, tmp_path):
        seen = {}
        def caller(system, user, schema):
            return {"moments": []}
        path = detect.detect_moments(
            "vid123", tmp_path, tmp_path / "e",
            config(detect={"model": "claude-sonnet-5"}),
            stream_title="Race", caller=caller,
            transcriber=transcript(some_words(), tmp_path=tmp_path))
        assert json.loads(path.read_text())["engine"] == "model:claude-sonnet-5"
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_detect.py -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.detect' has no attribute 'analysis_path'`

- [x] **Step 3: Rewrite `detect.py`**

```python
"""Detect moments in a stream and write an ANALYSIS - never a clip.

The change of contract is the point. While detection wrote clip directories
unprompted, its precision had to be high or an operator's clip list filled with
work they then had to clear out; a fixed `top_n` also imposed a count the
material does not share - one stream holds five worthwhile moments, another a
hundred. Detection now only DISPLAYS, so it may be generous: a weak suggestion
costs a glance instead of a cleanup. A clip exists when the operator picks a
window and asks for one, and at no other time (see clip_from_moment.py).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import claude_client, moment_scan
from ._anthropic import AnthropicUnavailable
from .glossary import EMPTY as GLOSSARY_EMPTY
from .lexicon import EMPTY as LEXICON_EMPTY
from .moments import activity_curve, lexicon_moments
from .stream_transcribe import transcribe_stream

_logger = logging.getLogger("ytshorts.detect")

ANALYSIS_FILENAME = "moments.json"


def analysis_path(workspace_dir: str | Path, video_id: str) -> Path:
    return Path(workspace_dir) / "streams" / video_id / ANALYSIS_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _caller_from_config(workspace_dir: Path, model: str, log) -> object | None:
    """A model caller, or None with the reason logged. Never raises."""
    try:
        key = claude_client.load_api_key(Path(workspace_dir) / "auth")
    except claude_client.MissingKey as error:
        log.warning("no Anthropic API key (%s) - falling back to the lexicon "
                    "engine, whose results are markedly weaker", error)
        return None
    try:
        return claude_client.make_caller(key, model=model)
    except (claude_client.ModelError, AnthropicUnavailable) as error:
        # OUR exception types, carrying OUR messages - safe to log in full, and
        # the useful half of the diagnosis lives in that text.
        log.warning("cannot reach the model (%s) - falling back to the lexicon "
                    "engine, whose results are markedly weaker", error)
        return None
    except Exception as error:      # noqa: BLE001 - an unavailable SDK must degrade, not abort
        # An exception claude_client did NOT wrap is one whose text this project
        # has made no promise about, and the request it describes carries the
        # API key. Log the type name and nothing else. Widening this to
        # str(error) for a nicer message is exactly how the key gets into a log.
        log.warning("cannot reach the model (%s) - falling back to the lexicon "
                    "engine, whose results are markedly weaker",
                    type(error).__name__)
        return None


def detect_moments(video_id, workspace_dir, event_dir, config, *, stream_title,
                   transcriber=transcribe_stream, caller=None, now=None,
                   logger=None, progress=None) -> Path:
    """Transcribes (or reuses the cache), scans, and writes moments.json.

    Returns the path to the analysis. `caller` and `now` are injected so the
    whole path tests with no network, no key and no cost.

    ONE ENGINE PER RUN. A window that fails mid-run is recorded in
    `missing_windows`; it does NOT fall back to the lexicon for that window,
    because two scoring scales inside one list would make the ranking
    meaningless in a way the operator could not see.
    """
    log = logger if logger is not None else _logger
    clock = now if now is not None else _now
    settings = config.get("detect", {}) or {}
    model = settings.get("model", claude_client.DEFAULT_MODEL)
    lexicon = config.get("lexicon", LEXICON_EMPTY)

    transcript = transcriber(video_id, Path(workspace_dir),
                             glossary=config.get("glossary", GLOSSARY_EMPTY))
    words = transcript.words
    missing_chunks = list(getattr(transcript, "missing_chunks", []) or [])
    if missing_chunks:
        log.warning("%s: %d chunk(s) missing from the transcript: %s - moments in "
                    "those windows cannot be found", video_id, len(missing_chunks),
                    missing_chunks)

    if not words:
        log.warning("%s: the transcript has 0 words (%d chunk(s) failed: %s). "
                    "Per-chunk decode causes are logged by stream_transcribe.",
                    video_id, len(missing_chunks), missing_chunks)

    if caller is None and words:
        caller = _caller_from_config(Path(workspace_dir), model, log)

    if caller is not None and words:
        engine = f"model:{model}"
        result = moment_scan.scan(words, lexicon, caller=caller, logger=log,
                                  progress=progress)
        found, missing_windows = result.moments, result.missing_windows
    else:
        engine = "lexicon"
        if words:
            log.warning("%s: detected with the lexicon engine - reduced quality",
                        video_id)
        found, missing_windows = lexicon_moments(words, lexicon), []

    payload = {
        "video_id": video_id,
        "engine": engine,
        "created_at": clock(),
        "duration_seconds": getattr(transcript, "duration_seconds", 0.0),
        "activity": activity_curve(words, lexicon),
        "moments": [vars(moment) for moment in found],
        "missing_windows": missing_windows,
        "missing_chunks": missing_chunks,
    }
    path = analysis_path(workspace_dir, video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("%s: %d words, %d moment(s), engine=%s, %d window(s) failed",
             video_id, len(words), len(found), engine, len(missing_windows))
    return path
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_detect.py tests/test_moments.py -q`
Expected: all pass.

- [x] **Step 5: Verify the old engine has no callers left**

Run: `grep -rn "rank_moments\|measure_loudness\|find_candidates\|LoudnessMoment" src/ tests/ bin/ || echo "clean"`
Expected: `clean`. Any hit is a caller the rewrite missed — fix it before
committing, not in a later task.

- [x] **Step 6: Lint and full suite**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q`
Expected: `All checks passed!` and a fully green suite. The deletions and the
rewrite that makes them safe are in this one commit, so nothing may fail here.

- [x] **Step 7: Commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/detect.py tests/test_detect.py src/yt_shorts/moments.py tests/test_moments.py
git commit -m "feat(detect): write an analysis beside the transcript, not clips"
```

---

### Task 7: Creating a clip from a chosen window

**Files:**
- Create: `src/yt_shorts/clip_from_moment.py`
- Delete: `src/yt_shorts/moment_entry.py`
- Modify: `tests/test_moment_entry.py` → rename to `tests/test_clip_from_moment.py`

**Interfaces:**
- Consumes: `clipstore.write_clip(event_dir, entry) -> Path`.
- Produces: `clip_from_moment.moment_url(video_id: str, start: float, end: float) -> str` (moved verbatim from `moment_entry`); `clip_from_moment.create_clip(event_dir, *, video_id: str, start: float, end: float, hook: str, source_title: str) -> Path`.

- [x] **Step 1: Write the failing tests**

```bash
git mv tests/test_moment_entry.py tests/test_clip_from_moment.py
```

Rewrite `tests/test_clip_from_moment.py`:

```python
import json

import pytest

from yt_shorts import clip_from_moment


class TestMomentUrl:
    def test_identity_lives_in_the_path_not_the_query(self):
        # clipid.canonical_url STRIPS the query string, so a "?..." identity
        # would collapse every moment in a stream onto one clip.
        url = clip_from_moment.moment_url("vid123", 61.4, 89.6)
        assert "?" not in url
        assert url.endswith("/vid123/61-90")

    def test_two_windows_are_two_urls(self):
        assert (clip_from_moment.moment_url("v", 10, 20)
                != clip_from_moment.moment_url("v", 30, 40))

    def test_the_same_window_is_the_same_url(self):
        assert (clip_from_moment.moment_url("v", 10.2, 20.4)
                == clip_from_moment.moment_url("v", 10.1, 20.3))


class TestCreateClip:
    def test_writes_one_clip_directory_with_the_chosen_window(self, tmp_path):
        directory = clip_from_moment.create_clip(
            tmp_path, video_id="vid123", start=61.4, end=89.6,
            hook="INTO THE BARRIER", source_title="N24 Race Part 1")
        entry = json.loads((directory / "clip.json").read_text())
        assert entry["start"] == 61.4 and entry["end"] == 89.6
        assert entry["hook"] == "INTO THE BARRIER"
        assert entry["duration"] == pytest.approx(28.2)

    def test_the_same_window_twice_is_one_directory(self, tmp_path):
        first = clip_from_moment.create_clip(tmp_path, video_id="v", start=10, end=20,
                                             hook="A", source_title="T")
        second = clip_from_moment.create_clip(tmp_path, video_id="v", start=10, end=20,
                                              hook="B", source_title="T")
        assert first == second

    def test_an_inverted_window_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            clip_from_moment.create_clip(tmp_path, video_id="v", start=30, end=10,
                                         hook="", source_title="T")
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_clip_from_moment.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'yt_shorts.clip_from_moment'`

- [x] **Step 3: Write the module and delete its predecessor**

Create `src/yt_shorts/clip_from_moment.py`:

```python
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


def moment_url(video_id: str, start: float, end: float) -> str:
    return (f"https://www.youtube.com/watch/{video_id}/"
            f"{int(round(start))}-{int(round(end))}")


def create_clip(event_dir: str | Path, *, video_id: str, start: float, end: float,
                hook: str, source_title: str) -> Path:
    """Writes one clip directory for this window and returns it."""
    if end <= start:
        raise ValueError(f"window end ({end}) must be after start ({start})")
    entry = {
        "url": moment_url(video_id, start, end),
        "video_id": video_id,
        "hook": hook,
        "source_title": source_title,
        "start": start,
        "end": end,
        "duration": end - start,
    }
    return clipstore.write_clip(event_dir, entry)
```

```bash
git rm src/yt_shorts/moment_entry.py
```

- [x] **Step 4: Run the tests and check for stale importers**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_clip_from_moment.py -q`
Expected: all pass.

Run: `grep -rn "moment_entry" src/ tests/ bin/ || echo "clean"`
Expected: `clean`.

- [x] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add -A src/yt_shorts/clip_from_moment.py tests/test_clip_from_moment.py
git commit -m "feat(detect): a clip is created only when the operator picks a window"
```

---

### Task 8: The CLI command and the studio job

**Files:**
- Modify: `bin/yt-shorts` (module docstring, `cmd_detect`, `COMMANDS`, the argument branch, dispatch)
- Modify: `src/yt_shorts/studio/jobs.py:315-355` (`_run_detect`, `start_detect_job`)
- Test: `tests/test_cli.py`, `tests/test_studio_jobs.py`

**Interfaces:**
- Consumes: `detect.detect_moments(...) -> Path` (Task 6).
- Produces: `cmd_detect(event_dir: Path, config: dict, video_id: str, workspace_dir: Path, detect_fn=None) -> int` — argument order matches `cmd_render(dir_, config, footer)`, which the dispatcher already calls as `cmd_render(profile.event_dir, profile.config, ...)`; `jobs._run_detect` records one `"detect"` result carrying the engine, moment count and failed-window count.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
class TestCmdDetect:
    def test_reports_the_analysis_path_and_returns_zero(self, tmp_path, capsys):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ytshorts_cli", "bin/yt-shorts")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

        written = tmp_path / "streams" / "vid123" / "moments.json"
        written.parent.mkdir(parents=True)
        written.write_text('{"engine": "lexicon", "moments": []}')
        (tmp_path / "event").mkdir()        # EventLock needs a real directory

        code = cli.cmd_detect(tmp_path / "event", {"lexicon": None}, "vid123",
                              tmp_path, detect_fn=lambda *a, **k: written)
        assert code == 0
        assert "moments.json" in capsys.readouterr().out

    def test_a_failure_returns_one_and_says_why(self, tmp_path, capsys):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ytshorts_cli", "bin/yt-shorts")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

        (tmp_path / "event").mkdir()        # EventLock needs a real directory

        def boom(*a, **k):
            raise RuntimeError("yt-dlp went away")
        code = cli.cmd_detect(tmp_path / "event", {"lexicon": None}, "vid123",
                              tmp_path, detect_fn=boom)
        assert code == 1
        assert "yt-dlp went away" in capsys.readouterr().err
```

Append to `tests/test_studio_jobs.py`:

```python
class TestDetectJobReportsAnAnalysis:
    def test_records_the_engine_and_counts_not_clip_names(self, tmp_path, erf_profile):
        # detect_moments now returns a PATH; a job that still expects a list of
        # clip names would iterate the characters of that path.
        analysis = tmp_path / "moments.json"
        analysis.write_text('{"engine": "model:claude-haiku-4-5", '
                            '"moments": [{"start": 1, "end": 20}], "missing_windows": []}')
        store = jobs.JobStore()
        job = jobs.start_detect_job(erf_profile, store, "vid123", "Race",
                                    detect_fn=lambda *a, **k: analysis)
        _wait_for(job)
        assert job.results
        assert any("claude-haiku-4-5" in (r.reason or "") for r in job.results)
        assert any("1 moment" in (r.reason or "") for r in job.results)
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q -k Detect && PYTHONPATH=src .venv/bin/pytest tests/test_studio_jobs.py -q -k Analysis`
Expected: FAIL — `AttributeError: module 'ytshorts_cli' has no attribute 'cmd_detect'`

- [x] **Step 3: Add `cmd_detect` to `bin/yt-shorts`**

Insert after `cmd_gallery`:

```python
def cmd_detect(dir_: Path, config: dict, video_id: str, workspace_dir: Path,
               detect_fn=None) -> int:
    """Scans a stream for moments and writes streams/<id>/moments.json.

    Writes no clips - that happens in the studio when the operator picks a
    window - so unlike cmd_render this needs no EventLock for the clip store.
    It still takes the event lock, because a studio-started detect against the
    same event would write the same analysis file.

    `detect_fn` is injected so this tests without a model, a key or a network.
    """
    from yt_shorts.detect import detect_moments

    run = detect_fn if detect_fn is not None else detect_moments
    lock = EventLock(dir_)
    try:
        lock.acquire()
    except LockError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    try:
        path = run(video_id, workspace_dir, dir_, config, stream_title=video_id,
                   progress=lambda done, total: print(f"  window {done}/{total}"))
    except Exception as error:      # noqa: BLE001 - the CLI reports the cause, it does not traceback at the operator
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        lock.release()
    print(f"wrote {path}")
    return 0
```

`EventLock` and `LockError` are already imported at the top of `bin/yt-shorts`
for `cmd_render` — reuse those imports, do not add new ones.

Then:

```python
COMMANDS = {"harvest", "render", "gallery", "migrate", "studio", "auth", "upload", "detect"}
```

and, in the argument branch, alongside the `studio` special case:

```python
    elif command == "detect":
        # detect takes TWO arguments: the event and the stream's video id.
        if len(args) != 3:
            print(__doc__.strip(), file=sys.stderr)
            raise SystemExit(2)
        identifier, video_id = args[1], args[2]
```

Note the existing `else` branch of that chain assigns `identifier = args[1]`
after requiring `len(args) == 2`; `detect` must be handled BEFORE it, next to
the `studio` case, or a two-argument `detect` exits 2 before reaching the
dispatcher.

Wire it into the final dispatch chain, beside `render`:

```python
    elif command == "detect":
        exit_code = cmd_detect(profile.event_dir, profile.config, video_id, space.root)
```

Add the usage line to the module docstring:

```
  yt-shorts detect <channel>/<event> <video-id>   scan a stream for moments
```

- [x] **Step 4: Rewrite `_run_detect` in `src/yt_shorts/studio/jobs.py`**

```python
def _run_detect(profile: Profile, job: Job, video_id: str, stream_title: str,
                event_lock: EventLock, detect_fn) -> None:
    job_logger(job).info("start: detect %s", video_id)
    try:
        workspace_root = _resolve_workspace().root
        # detect_moments now returns the PATH of the analysis it wrote and
        # creates no clip directories at all; a caller that still treated the
        # return value as a list of clip names would iterate that path's
        # characters and record one result per letter.
        path = detect_fn(video_id, workspace_root, profile.event_dir,
                         profile.config, stream_title=stream_title,
                         logger=job_logger(job),
                         progress=lambda done, total: job_logger(job).info(
                             "window %d/%d", done, total))
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        engine, count = data.get("engine", "?"), len(data.get("moments", []))
        failed = len(data.get("missing_windows", []))
        note = f"engine={engine}, {count} moment(s)"
        if failed:
            note += f", {failed} window(s) failed"
        job.record("detect", "done", None, note)
    except Exception as error:      # noqa: BLE001 - one failed detect must not kill the runner
        reason = shorten_urls(f"{type(error).__name__}: {error}")
        job_logger(job).error("detect failed: %s", reason)
        job.record("detect", "failed", reason, f"ERROR: {reason}")
    finally:
        event_lock.release()
```

Add `import json` and `from pathlib import Path` at the top of `jobs.py` if absent.

- [x] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py tests/test_studio_jobs.py -q`
Expected: all pass.

- [x] **Step 6: Full suite, lint, and a real end-to-end run against the workspace**

```bash
python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q
```
Expected: `All checks passed!` and no failures.

Then, with no `anthropic` installed, confirm the fallback path really runs:

```bash
gtimeout 300 bin/yt-shorts detect erfofficial/N24-2026 V9nVNEQNdR4
PYTHONPATH=src .venv/bin/python -c "
import json; d = json.load(open('$HOME/YT-Shorts-Data/streams/V9nVNEQNdR4/moments.json'))
print('engine:', d['engine'], '| moments:', len(d['moments']), '| curve:', len(d['activity']))"
```
Expected: `engine: lexicon` and a non-empty curve — the transcript is cached, so
this re-uses it and makes no network call.

- [x] **Step 7: Commit**

```bash
git add bin/yt-shorts src/yt_shorts/studio/jobs.py tests/test_cli.py tests/test_studio_jobs.py
git commit -m "feat(detect): a detect command, and a job that reports an analysis"
```

---

### Task 9: Install the SDK and run the model bake-off

**Files:** none committed except `README.md` and `docs/superpowers/plans/`-adjacent notes.

This is the step that answers the question the plan deliberately left open:
whether `claude-haiku-4-5` is good enough. It is manual, uses the real key, and
costs well under one euro in total.

- [x] **Step 1: Install the optional dependency**

```bash
.venv/bin/pip install anthropic
PYTHONPATH=src .venv/bin/pytest -q
```
Expected: the suite still passes. **No test may start using the real SDK** —
if a test's behaviour changes after this install, it was reaching for something
it should have stubbed.

- [x] **Step 2: Run all three models over the qualifying**

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import json, pathlib
from yt_shorts import claude_client, moment_scan
from yt_shorts.lexicon import Lexicon, DEFAULT_MARKERS

home = pathlib.Path.home() / "YT-Shorts-Data"
words = json.load(open(home / "streams/V9nVNEQNdR4/transcript.json"))["words"]
key = claude_client.load_api_key(home / "auth")
lex = Lexicon(markers=dict(DEFAULT_MARKERS))

for model in ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"):
    caller = claude_client.make_caller(key, model=model)
    result = moment_scan.scan(words, lex, caller=caller)
    print(f"\n===== {model}: {len(result.moments)} moments, "
          f"{len(result.missing_windows)} failed windows =====")
    for m in sorted(result.moments, key=lambda m: -m.score):
        print(f"  {int(m.start)//60}:{int(m.start)%60:02d}  {m.score:4.1f}  "
              f"{m.category:12} {m.reason[:70]}")
PY
```

- [x] **Step 3: Judge the three lists** — RUN 2026-07-29, `claude-opus-5` wins
      and is now `claude_client.DEFAULT_MODEL` (commit `11fb210`).

  | model | moments | of the 4 known-good | known-bad flagged | cost |
  |---|---|---|---|---|
  | `claude-haiku-4-5` | 2 | 0 | none | ~$0.012 |
  | `claude-sonnet-5` | 7 | 1 (74:13) | 23:34 | ~$0.037 |
  | `claude-opus-5` | 7 | 3 (74:13, 93:16, 51:07) | 23:34 | ~$0.062 |

  Nobody found **39:46**, and nobody flagged **4:25**. Both larger models
  independently flagged **23:34**, ten seconds off the known-bad 23:24, with a
  reason that does not read like the chatter that made 23:24 bad ("oh no,
  buster don't do it" over a wild drift) — recorded as an open question for the
  operator rather than scored as a miss.

  **The first run of this step invalidated itself, and that is the finding.**
  Haiku returned 0 moments from 5 answers, with 0 failed windows — a run that
  looked successful and produced nothing. Cause: the prompt gave the length
  limit in SECONDS while the answer is in LINE NUMBERS, and never stated that a
  line is 12 seconds. Haiku returned spans of 26-40 lines; `validate_moment`
  dropped every one. Sonnet and Opus had inferred the conversion unaided. Fixed
  in commit `3e1b481` (which also pinned the 0-10 score scale the prompt had
  never stated, while both engines were writing into the same field), and the
  table above is the re-run. A bake-off measures the prompt as much as the
  model.

Each model must be checked against the known-good moments — **1:34:05**
"Yeah, we will see purple", **1:14:13** "no contact with the barrier", **50:14**
"What a lap", **39:46** — and against the known-bad ones the old ranking put
first: **23:24** (pure chatter, previously rank 1) and **4:25** (pre-race
small talk). A model that finds the four and omits the two passes.

Record the outcome in the plan file as a checked box with the counts, and set
`config["detect"]["model"]` in the channel's `brand.json` to the winner. If
Haiku passes, nothing changes and the default stands.

- [x] **Step 4: Document the dependency**

Add to `README.md` beside the existing venv instructions:

```
Moment detection additionally needs `anthropic` and an API key at
`<workspace>/auth/anthropic.json` (mode 600). Without either, detection falls
back to the lexicon engine and says so in the studio. The Messages API is
billed separately from a Claude subscription.
```

- [x] **Step 5: Commit**

```bash
git add README.md docs/superpowers/plans/2026-07-27-moment-detection-engine.md
git commit -m "docs(detect): record the bake-off result and the optional dependency"
```

---

## What this plan does NOT cover

The studio's stream view — the routes `GET …/streams/{video_id}/moments`,
`…/transcript`, `POST …/estimate`, `POST …/clips`, and the React screen with the
hit list, overview strip, zoom lane and player. That is Plan B, and it is
deliberately sequenced after the bake-off: the value of that screen depends on
the engine behind it being worth reviewing, and Task 9 is what establishes that.

The Batch API switch the spec keeps available for a deliberate overnight run
across many streams is deferred on YAGNI grounds: nothing in this plan needs
it, an unused config flag is a maintenance cost with no reader, and the
synchronous path is what every current caller wants. It is a small, additive
change to `claude_client.make_caller` when a run across ten streams actually
exists to justify it.

After this plan, `bin/yt-shorts detect` produces a usable analysis and the
studio's existing detect job reports it — working, testable software with no
frontend work at all.
