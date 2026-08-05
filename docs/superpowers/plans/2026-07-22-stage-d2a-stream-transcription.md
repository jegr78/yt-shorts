# Stage D2a — Whole-stream transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn one full stream into a cached, timed transcript and a kept local audio file, chunked so the ~1-hour decode of an 8-hour stream is resumable and each chunk's decode can actually be timed out.

**Architecture:** A new `stream_transcribe.py` downloads a stream's audio once (injected `downloader`), splits it into fixed time windows, decodes each window through an injected `decoder`, caches each chunk's words, and assembles the present chunks into one transcript. The one Whisper decode core is extracted from `transcribe.py` so a clip and a stream chunk decode identically; the production decoder runs it in a killable subprocess so a hung chunk is abandoned without losing the rest.

**Tech Stack:** Python 3 standard library, the existing `faster-whisper` and yt-dlp/ffmpeg the tool already uses. No new dependencies.

## Global Constraints

- `PYTHONPATH=src` is mandatory. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` — 498 tests pass at the start of this plan.
- No new Python dependencies. yt-dlp and ffmpeg are subprocesses; `faster-whisper` is already a dependency.
- **`transcribe()`'s behaviour must not change.** Task 1 is a pure extraction: the same words, the same cache shape, the same `no_speech_prob > 0.75` drop, the same `vad_filter=False`, the same glossary handling. Its existing tests must stay green unchanged.
- **`vad_filter=False` and `NO_SPEECH_PROB_THRESHOLD = 0.75` are load-bearing and measured** (see `transcribe.py`'s own comments). Do not touch either value or the decode settings; the extraction moves them, it does not alter them.
- **One failure never aborts a run.** A chunk that hangs (timeout) or errors leaves a gap and a recorded failure; the remaining chunks still decode and assemble. Same rule the rest of the tool follows.
- **The killable subprocess is the point of chunking.** A per-chunk timeout is only real because the decode runs in a separate, killable process — a signal cannot stop a hung C-extension decode and a Python thread cannot be killed (see `transcribe.py`'s "Unbounded decode" note). Do not "simplify" the production decoder back to an in-process call.
- **All runtime data lives in the workspace, never the repo.** `streams/<video_id>/` under `workspace.resolve()`. The downloaded audio is **kept** — D2b reads it for loudness.
- Tests must not depend on `~/YT-Shorts-Data`, must not hit the network, and must not run a real Whisper model or a multi-hour job. Inject `downloader` and `decoder`. `tests/conftest.py` pins `profile.CHANNELS_DIR` to `tests/fixtures/channels`.
- English only. Imperative commit messages.

---

## Task 1: Extract the Whisper decode core from `transcribe.py`

**Files:**
- Modify: `src/yt_shorts/transcribe.py`
- Test: `tests/test_transcribe.py` (existing tests must pass unchanged; add one that pins the extracted function's contract)

**Interfaces:**
- Consumes: nothing new
- Produces: `transcribe.decode_wav(wav_path, *, model_name, glossary=EMPTY) -> tuple[list[dict], list[float]]` — decodes one wav with the tool's fixed settings, returns `(words, dropped_probs)` with glossary corrections already applied to `words`. This is exactly the code that today lives inline inside `transcribe()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_transcribe.py`. **Reuse that file's existing fakes** —
`FakeWord`, `FakeSegment`, and `FakeModelCapturingKwargs` (whose returned
segments are set via its `segments` class attribute). The existing tests patch
`faster_whisper.WhisperModel` (the module attribute) and rely on `transcribe()`
importing `WhisperModel` **function-locally** at call time; the new test does the
same, and Task 1 must keep that import function-local (see Step 3) so nothing
about the patch target changes.

```python
class TestDecodeWav:
    def test_it_drops_high_no_speech_segments_and_applies_glossary(self, monkeypatch, tmp_path):
        import faster_whisper
        from yt_shorts.transcribe import decode_wav
        from yt_shorts.glossary import Glossary

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModelCapturingKwargs)
        # A real segment (kept) and a hallucination (dropped at > 0.75).
        FakeModelCapturingKwargs.segments = [
            FakeSegment(0.10, [FakeWord(0.0, 0.5, " very"), FakeWord(0.5, 1.0, " very")]),
            FakeSegment(0.88, [FakeWord(1.0, 1.5, " You")]),
        ]

        wav = tmp_path / "a.wav"          # content ignored by the fake model
        wav.write_bytes(b"")
        words, dropped = decode_wav(
            wav, model_name="small",
            glossary=Glossary(terms=[], replacements={"very very": "Rei"}))

        assert [w["text"] for w in words] == [" Rei"]      # glossary applied
        assert dropped == [0.88]                            # hallucination dropped, recorded
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_transcribe.py::TestDecodeWav -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.transcribe' has no attribute 'decode_wav'`

- [ ] **Step 3: Extract the function**

**Keep the `from faster_whisper import WhisperModel` import function-local**, exactly where it is today — inside the new function's body. The existing tests patch `faster_whisper.WhisperModel` and depend on this late import; moving it to module top level would bind the name at import time and silently break every one of them. This is a pure extraction: the import stays local, the settings stay identical.

Add the extracted function (its body is the exact code that lived inside `transcribe()`'s `try` — the lines that import `WhisperModel`, build `segments`, filter by `NO_SPEECH_PROB_THRESHOLD`, and apply the glossary):

```python
def decode_wav(wav_path, *, model_name: str,
               glossary: Glossary = _glossary.EMPTY) -> tuple[list[dict], list[float]]:
    """Decodes one wav with the tool's fixed, measured settings.

    Returns (words, dropped_probs). vad_filter stays False and segments above
    NO_SPEECH_PROB_THRESHOLD are dropped after decoding, not skipped during it -
    see this module's long comments for why both choices are load-bearing and
    measured. Glossary hotwords bias the decoder and replacements are applied to
    the returned words. This is the single place decode settings live: transcribe()
    calls it in-process for a clip, and stream_transcribe's subprocess worker calls
    it for a chunk, so the two decode identically.

    The `from faster_whisper import WhisperModel` stays a function-local import:
    the test suite patches faster_whisper.WhisperModel and relies on this late
    binding.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(wav_path), word_timestamps=True,
                                   vad_filter=False,
                                   hotwords=_glossary.hotwords(glossary))
    words: list[dict] = []
    dropped_probs: list[float] = []
    for segment in segments:
        if segment.no_speech_prob <= NO_SPEECH_PROB_THRESHOLD:
            words.extend(
                {"start": word.start, "end": word.end, "text": word.word}
                for word in (segment.words or [])
            )
        else:
            dropped_probs.append(float(segment.no_speech_prob))
    words = _glossary.apply(words, glossary)
    return words, dropped_probs
```

Then replace that block inside `transcribe()` with a call. The `try/except`
that wraps it in `TranscriptionError` and the `_extract_audio` before it stay
exactly as they are; only the decode body moves:

```python
    _extract_audio(video, wav, ffmpeg)
    try:
        words, dropped_probs = decode_wav(wav, model_name=model_name, glossary=glossary)
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        raise TranscriptionError(f"{type(error).__name__}: {error}") from error

    _report_dropped(name, dropped_probs)
    cache_path.write_text(...)   # unchanged
```

Note `decode_wav` applies the glossary itself, so the standalone
`words = _glossary.apply(words, glossary)` line that followed the old block is
removed (it now happens inside `decode_wav`) — do not apply it twice.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_transcribe.py -q`
Expected: PASS — the new test and **every existing transcribe test, unchanged**. The existing tests are untouched: the import stayed function-local, so their `faster_whisper.WhisperModel` patch still lands. If any existing transcribe test fails, the extraction changed behaviour — fix the extraction, not the test.

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 499 passed (498 + 1).

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/transcribe.py tests/test_transcribe.py
git commit -m "Extract the Whisper decode core so a clip and a stream chunk share it"
```

---

## Task 2: Chunk windows and assembly (pure)

**Files:**
- Create: `src/yt_shorts/stream_transcribe.py`
- Test: `tests/test_stream_transcribe.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `stream_transcribe.chunk_windows(total_seconds: float, chunk_seconds: int) -> list[tuple[int, float, float]]` — `(index, start, length)` per window, covering the whole duration, last window short.
  - `stream_transcribe.offset_words(words: list[dict], start: float) -> list[dict]` — each word's `start`/`end` shifted by `start`, text unchanged.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from yt_shorts.stream_transcribe import chunk_windows, offset_words


class TestChunkWindows:
    def test_exact_multiple(self):
        assert chunk_windows(1200, 600) == [(0, 0.0, 600.0), (1, 600.0, 600.0)]

    def test_short_last_window(self):
        assert chunk_windows(1500, 600) == [
            (0, 0.0, 600.0), (1, 600.0, 600.0), (2, 1200.0, 300.0)]

    def test_shorter_than_one_chunk(self):
        assert chunk_windows(310, 600) == [(0, 0.0, 310.0)]

    def test_zero_duration_is_no_windows(self):
        assert chunk_windows(0, 600) == []


class TestOffsetWords:
    def test_times_shift_text_unchanged(self):
        words = [{"start": 0.0, "end": 0.5, "text": " go"},
                 {"start": 0.5, "end": 1.0, "text": " green"}]
        assert offset_words(words, 1800.0) == [
            {"start": 1800.0, "end": 1800.5, "text": " go"},
            {"start": 1800.5, "end": 1801.0, "text": " green"}]

    def test_empty(self):
        assert offset_words([], 100.0) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_stream_transcribe.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.stream_transcribe'`

- [ ] **Step 3: Write the implementation**

Create `src/yt_shorts/stream_transcribe.py` with the module docstring and these two functions (the rest of the module lands in Tasks 3–4):

```python
"""Transcribe a whole stream in resumable chunks (see the stage D2a design).

An 8-hour stream is ~1 hour of Whisper decode; done as one call it cannot be
resumed or safely timed out. This splits the stream's audio into fixed windows,
decodes each in a killable subprocess, caches each chunk's words, and assembles
the present chunks into one timed transcript. The downloaded audio is kept for
D2b's loudness signal. downloader and decoder are injected so all of this tests
without the network or a real model.
"""

from __future__ import annotations


def chunk_windows(total_seconds: float, chunk_seconds: int) -> list[tuple[int, float, float]]:
    """(index, start, length) windows covering total_seconds; last one short."""
    windows: list[tuple[int, float, float]] = []
    index = 0
    start = 0.0
    while start < total_seconds:
        length = min(float(chunk_seconds), total_seconds - start)
        windows.append((index, start, length))
        index += 1
        start += chunk_seconds
    return windows


def offset_words(words: list[dict], start: float) -> list[dict]:
    """Shift each word's start/end by `start`; text unchanged."""
    return [{"start": w["start"] + start, "end": w["end"] + start, "text": w["text"]}
            for w in words]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_stream_transcribe.py -q`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/stream_transcribe.py tests/test_stream_transcribe.py
git commit -m "Compute stream chunk windows and offset chunk words"
```

---

## Task 3: Orchestrate the stream transcription

**Files:**
- Modify: `src/yt_shorts/stream_transcribe.py`
- Test: `tests/test_stream_transcribe.py`

**Interfaces:**
- Consumes: `chunk_windows`, `offset_words` (Task 2)
- Produces:
  - `stream_transcribe.DownloadedAudio` — dataclass `path: Path`, `duration_seconds: float`
  - `stream_transcribe.StreamTranscript` — dataclass `video_id: str`, `audio_path: Path`, `duration_seconds: float`, `words: list[dict]`, `missing_chunks: list[int]`
  - `stream_transcribe.StreamTranscribeError`
  - `stream_transcribe.transcribe_stream(video_id, workspace_dir, *, downloader, decoder, chunk_seconds=600) -> StreamTranscript`
    - `downloader(video_id, dest_dir: Path) -> DownloadedAudio`
    - `decoder(audio_path: Path, start: float, length: float) -> list[dict]` — chunk-relative words (times in `0..length`); raises on failure/timeout

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path

import pytest

from yt_shorts.stream_transcribe import (
    DownloadedAudio, StreamTranscribeError, StreamTranscript, transcribe_stream)

VIDEO = "V9nVNEQNdR4"


def fake_downloader(duration, name="audio.webm"):
    def download(video_id, dest_dir):
        path = Path(dest_dir) / name
        path.write_bytes(b"fake audio")
        return DownloadedAudio(path=path, duration_seconds=duration)
    return download


def word_at(t):
    return {"start": 0.0, "end": 1.0, "text": f" w{int(t)}"}


def fake_decoder(fail_on=()):
    # Returns one chunk-relative word per chunk, tagged by the chunk's start,
    # so the assembly's absolute times are checkable. Raises for chunks in fail_on.
    calls = []

    def decode(audio_path, start, length):
        calls.append(start)
        if start in fail_on:
            raise RuntimeError(f"decode hung at {start}")
        return [{"start": 0.0, "end": 1.0, "text": f" s{int(start)}"}]

    decode.calls = calls
    return decode


class TestTranscribeStream:
    def test_assembles_absolute_time_words(self, tmp_path):
        result = transcribe_stream(
            VIDEO, tmp_path,
            downloader=fake_downloader(1500), decoder=fake_decoder(), chunk_seconds=600)
        assert isinstance(result, StreamTranscript)
        # three chunks at 0, 600, 1200; each word offset to its chunk start
        assert [w["text"] for w in result.words] == [" s0", " s600", " s1200"]
        assert [w["start"] for w in result.words] == [0.0, 600.0, 1200.0]
        assert result.missing_chunks == []
        assert result.audio_path.exists()

    def test_keeps_the_downloaded_audio(self, tmp_path):
        result = transcribe_stream(
            VIDEO, tmp_path, downloader=fake_downloader(300), decoder=fake_decoder(),
            chunk_seconds=600)
        assert result.audio_path.read_bytes() == b"fake audio"
        assert result.audio_path.parent == tmp_path / "streams" / VIDEO

    def test_caches_each_chunk(self, tmp_path):
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                          decoder=fake_decoder(), chunk_seconds=600)
        chunks = sorted((tmp_path / "streams" / VIDEO / "chunks").glob("*.json"))
        assert [c.name for c in chunks] == ["000.json", "001.json"]

    def test_resumes_without_redecoding_cached_chunks(self, tmp_path):
        d1 = fake_decoder()
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                          decoder=d1, chunk_seconds=600)
        assert d1.calls == [0.0, 600.0]
        d2 = fake_decoder()
        result = transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                                   decoder=d2, chunk_seconds=600)
        assert d2.calls == []                          # nothing re-decoded
        assert [w["text"] for w in result.words] == [" s0", " s600"]

    def test_a_failing_chunk_leaves_a_gap_and_is_recorded(self, tmp_path):
        result = transcribe_stream(
            VIDEO, tmp_path, downloader=fake_downloader(1800),
            decoder=fake_decoder(fail_on=(600.0,)), chunk_seconds=600)
        assert [w["text"] for w in result.words] == [" s0", " s1200"]   # 600 missing
        assert result.missing_chunks == [1]

    def test_a_later_run_fills_a_previously_failed_chunk(self, tmp_path):
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                          decoder=fake_decoder(fail_on=(600.0,)), chunk_seconds=600)
        d2 = fake_decoder()
        result = transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                                   decoder=d2, chunk_seconds=600)
        assert d2.calls == [600.0]                     # only the previously-failed chunk
        assert result.missing_chunks == []

    def test_a_chunk_cached_for_a_different_window_is_redecoded(self, tmp_path):
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                          decoder=fake_decoder(), chunk_seconds=600)
        d2 = fake_decoder()
        # Same stream, different chunk_seconds -> chunk 0 now covers a different
        # window, so its stale cache must not be trusted.
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                          decoder=d2, chunk_seconds=300)
        assert 0.0 in d2.calls

    def test_a_download_failure_becomes_a_stream_error(self, tmp_path):
        def boom(video_id, dest_dir):
            raise RuntimeError("yt-dlp: video unavailable")
        with pytest.raises(StreamTranscribeError) as error:
            transcribe_stream(VIDEO, tmp_path, downloader=boom, decoder=fake_decoder())
        assert "video unavailable" in str(error.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_stream_transcribe.py -q -k TranscribeStream`
Expected: FAIL — `ImportError: cannot import name 'transcribe_stream'`

- [ ] **Step 3: Write the implementation**

Append to `src/yt_shorts/stream_transcribe.py`:

```python
import json
from dataclasses import dataclass, field
from pathlib import Path


class StreamTranscribeError(Exception):
    """Understandable message about a failed stream transcription."""


@dataclass
class DownloadedAudio:
    path: Path
    duration_seconds: float


@dataclass
class StreamTranscript:
    video_id: str
    audio_path: Path
    duration_seconds: float
    words: list[dict]
    missing_chunks: list[int] = field(default_factory=list)


def _stream_dir(workspace_dir: Path, video_id: str) -> Path:
    return Path(workspace_dir) / "streams" / video_id


def _read_cached_chunk(path: Path, video_id: str, start: float, length: float) -> list[dict] | None:
    """Returns the cached words if the file is for exactly this window, else None."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if (payload.get("stream") == video_id
            and payload.get("start") == start
            and payload.get("length") == length
            and isinstance(payload.get("words"), list)):
        return payload["words"]
    return None


def transcribe_stream(video_id, workspace_dir, *, downloader, decoder,
                      chunk_seconds: int = 600) -> StreamTranscript:
    """Downloads a stream's audio once and transcribes it in resumable chunks.

    A present, matching chunk cache is reused; only missing chunks are decoded.
    A chunk whose decoder raises (an error, or the production decoder's timeout on
    a hang) leaves a gap and is recorded in missing_chunks - the run does not
    abort, and a later run fills it. The audio is kept for D2b.
    """
    stream_dir = _stream_dir(Path(workspace_dir), video_id)
    chunks_dir = stream_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    try:
        audio = downloader(video_id, stream_dir)
    except Exception as error:
        raise StreamTranscribeError(
            f"Could not download audio for {video_id}: {error}") from error

    words: list[dict] = []
    missing: list[int] = []
    for index, start, length in chunk_windows(audio.duration_seconds, chunk_seconds):
        chunk_path = chunks_dir / f"{index:03d}.json"
        cached = _read_cached_chunk(chunk_path, video_id, start, length)
        if cached is not None:
            words.extend(cached)
            continue
        try:
            chunk_words = offset_words(decoder(audio.path, start, length), start)
        except Exception:
            missing.append(index)
            continue
        chunk_path.write_text(json.dumps({
            "stream": video_id, "index": index, "start": start,
            "length": length, "words": chunk_words,
        }, indent=2), encoding="utf-8")
        words.extend(chunk_words)

    transcript = StreamTranscript(
        video_id=video_id, audio_path=audio.path,
        duration_seconds=audio.duration_seconds, words=words, missing_chunks=missing)
    (stream_dir / "transcript.json").write_text(json.dumps({
        "video_id": video_id, "duration_seconds": audio.duration_seconds,
        "chunk_seconds": chunk_seconds, "words": words, "missing_chunks": missing,
    }, indent=2), encoding="utf-8")
    return transcript
```

Move the `import json` / `from pathlib import Path` / dataclass imports to the top of the module with the existing `from __future__` line rather than mid-file; they are shown here beside their use for reading.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_stream_transcribe.py -q`
Expected: PASS — the 6 Task-2 tests and the 8 Task-3 tests.

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 513 passed — 499 after Task 1, +6 from Task 2, +8 from Task 3. Confirm the delta against the last full run rather than trusting the arithmetic.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/stream_transcribe.py tests/test_stream_transcribe.py
git commit -m "Orchestrate resumable chunked stream transcription"
```

---

## Task 4: The production downloader and killable-subprocess decoder

**Files:**
- Modify: `src/yt_shorts/stream_transcribe.py`
- Create: `src/yt_shorts/_decode_worker.py`
- Test: `tests/test_stream_transcribe.py`

**Interfaces:**
- Consumes: `transcribe.decode_wav` (Task 1), `DownloadedAudio`
- Produces:
  - `stream_transcribe.ytdlp_downloader(video_id, dest_dir) -> DownloadedAudio` — the default `downloader`: `yt-dlp -f bestaudio --continue`, duration from yt-dlp's JSON.
  - `stream_transcribe.subprocess_decoder(audio_path, start, length) -> list[dict]` — the default `decoder`: ffmpeg-extract the window to a wav, decode it in a killable worker subprocess with a per-chunk timeout.
  - `stream_transcribe.run_with_timeout(cmd: list[str], timeout: float) -> str` — run a subprocess, return stdout, **kill the whole process on timeout** and raise `StreamTranscribeError`.
  - `transcribe_stream` gains defaults: `downloader=ytdlp_downloader`, `decoder=subprocess_decoder`.
  - `_decode_worker.py` — `python -m yt_shorts._decode_worker <wav> <model> [<glossary_json>]` prints the chunk's words as JSON on stdout.

- [ ] **Step 1: Write the failing test**

The heavy paths (real ffmpeg, real Whisper) are not unit-tested; the killable timeout — the whole reason this task exists — is, with cheap shell commands:

```python
import sys

from yt_shorts.stream_transcribe import run_with_timeout, StreamTranscribeError


class TestRunWithTimeout:
    def test_returns_stdout_on_success(self):
        out = run_with_timeout([sys.executable, "-c", "print('hello')"], timeout=10)
        assert out.strip() == "hello"

    def test_kills_and_raises_on_timeout(self):
        import time
        t0 = time.monotonic()
        with pytest.raises(StreamTranscribeError):
            # sleeps far longer than the timeout; must be killed, not waited out
            run_with_timeout([sys.executable, "-c", "import time; time.sleep(30)"],
                             timeout=1)
        assert time.monotonic() - t0 < 10          # returned promptly, i.e. killed

    def test_nonzero_exit_raises(self):
        with pytest.raises(StreamTranscribeError):
            run_with_timeout([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=10)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_stream_transcribe.py -q -k RunWithTimeout`
Expected: FAIL — `ImportError: cannot import name 'run_with_timeout'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/_decode_worker.py`:

```python
"""Decode one wav in a separate, killable process (see stage D2a design).

A hung Whisper decode cannot be stopped in-process - see transcribe.py's
"Unbounded decode" note - so a stream chunk decodes here, where the parent can
kill the whole process on timeout. Prints the chunk's words as JSON on stdout.
"""

import json
import sys

from .glossary import Glossary
from .transcribe import decode_wav


def main(argv: list[str]) -> int:
    wav, model = argv[1], argv[2]
    glossary = Glossary(terms=[], replacements={})
    if len(argv) > 3 and argv[3]:
        data = json.loads(open(argv[3], encoding="utf-8").read())
        glossary = Glossary(terms=data.get("terms", []),
                            replacements=data.get("replacements", {}))
    words, _dropped = decode_wav(wav, model_name=model, glossary=glossary)
    json.dump(words, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

Append to `stream_transcribe.py`:

```python
import subprocess
import sys


def run_with_timeout(cmd: list[str], timeout: float) -> str:
    """Runs cmd, returns stdout; kills the whole process group on timeout.

    subprocess.run(timeout=...) raises TimeoutExpired and kills the immediate
    child, which is what a chunk decode needs: the point of running the decode in
    a separate process is that this kill actually stops it, where a signal or a
    thread could not (see transcribe.py's "Unbounded decode" note).
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise StreamTranscribeError(
            f"decode exceeded {timeout}s and was killed") from error
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["worker failed"]
        raise StreamTranscribeError(tail[0])
    return result.stdout


# A generous multiple of the measured ~74s decode for a 600s chunk: it must only
# ever fire on a genuine hang, never on a slow-but-healthy chunk (same reasoning
# transcribe.py gives for having no clip timeout at all).
CHUNK_TIMEOUT_SECONDS = 600


def subprocess_decoder(audio_path, start, length, *, ffmpeg="ffmpeg",
                       model_name="small") -> list[dict]:
    """Extracts [start, start+length] to a wav and decodes it in a killable worker."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "chunk.wav"
        extract = [ffmpeg, "-v", "error", "-y", "-ss", str(start), "-t", str(length),
                   "-i", str(audio_path), "-ac", "1", "-ar", "16000", str(wav)]
        subprocess.run(extract, capture_output=True, text=True, timeout=900, check=True)
        out = run_with_timeout(
            [sys.executable, "-m", "yt_shorts._decode_worker", str(wav), model_name],
            timeout=CHUNK_TIMEOUT_SECONDS)
    return json.loads(out)


def ytdlp_downloader(video_id, dest_dir, *, ytdlp="yt-dlp") -> DownloadedAudio:
    """Downloads a stream's audio once (bestaudio, resumable) and its duration."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = str(dest_dir / "audio.%(ext)s")
    cmd = [ytdlp, "-f", "bestaudio", "--continue", "--no-part",
           "--print-json", "--no-simulate", "-o", template,
           f"https://www.youtube.com/watch?v={video_id}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["yt-dlp failed"]
        raise RuntimeError(tail[0])
    info = json.loads(result.stdout.splitlines()[-1])
    audio_files = sorted(p for p in dest_dir.glob("audio.*") if p.suffix != ".json")
    if not audio_files:
        raise RuntimeError("yt-dlp produced no audio file")
    return DownloadedAudio(path=audio_files[0],
                           duration_seconds=float(info.get("duration", 0.0)))
```

Then set the defaults on `transcribe_stream`: change its signature to
`downloader=ytdlp_downloader, decoder=subprocess_decoder`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_stream_transcribe.py -q`
Expected: PASS — all previous tests plus the three `run_with_timeout` tests. The injected-boundary tests from Task 3 still pass because they pass their own `downloader`/`decoder` and never touch the defaults.

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: previous count + 3.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/stream_transcribe.py src/yt_shorts/_decode_worker.py tests/test_stream_transcribe.py
git commit -m "Add the yt-dlp downloader and killable-subprocess chunk decoder"
```

---

## Task 5: Documentation

**Files:** `CLAUDE.md`, `README.md`

- [ ] **Step 1: Update CLAUDE.md**

Under the Architecture / subtitles area, add a short paragraph: `stream_transcribe.py` transcribes a whole stream in resumable chunks into `streams/<video_id>/` in the workspace (audio kept for D2b's loudness, per-chunk caches, assembled `transcript.json`); it shares the one decode core (`transcribe.decode_wav`) with clip transcription, and its production decoder runs each chunk in a **killable subprocess** so the per-chunk timeout is real — the resolution of the "Unbounded decode" note for the stream case. No user-facing trigger yet; that lands with D2b.

- [ ] **Step 2: Update README.md**

Note that stream transcription is a library capability at this stage (no CLI command yet), keyed by video id, that it keeps the downloaded audio, and that deleting `streams/<video_id>/` re-derives everything.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "Document whole-stream transcription"
```

---

## Verification for the branch

- Full suite green.
- **`transcribe()` unchanged in behaviour:** its existing tests pass untouched; the extraction moved code, not settings.
- **One real end-to-end smoke, measured not assumed** (the project's discipline; do it once, not in the suite): run `transcribe_stream` against a real ~5-minute public slice by pointing `chunk_seconds` small and a real `subprocess_decoder`/`ytdlp_downloader` at a short real video, OR reuse the 10-minute slice technique from the design measurement. Confirm: chunk caches appear, the assembled transcript has plausibly-timed words at absolute stream time, a second run re-decodes nothing, and deleting one chunk file makes the next run re-decode only that chunk. Report the observed timing.
- Confirm nothing outside `src/yt_shorts/` imports `stream_transcribe`; there is no CLI or studio trigger yet (deferred to D2b), matching how D1 shipped `list_streams` without a UI.

## Self-review notes

Checked against the spec:
- chunked, fixed windows, last short — Task 2
- resumable, per-chunk cache, source-matched to the window — Task 3 (`_read_cached_chunk`, the redecode-on-different-window test)
- killable subprocess = real per-chunk timeout — Task 4 (`run_with_timeout`, `_decode_worker`)
- failure isolation, gaps recorded, later run fills them — Task 3
- audio kept, `streams/<video_id>/` layout, runtime-only — Task 3, Task 4
- one shared decode core, `transcribe()` unchanged — Task 1
- injected boundaries, no network / no real model in tests — Tasks 2–4
- no user-facing trigger (deferred to D2b) — Task 5, Verification

Deferred with reason (not gaps): the CLI/studio trigger and moment detection — D2b.
