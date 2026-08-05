"""A failing chunk decode must say WHY, and an empty transcript must be loud.

The regression this pins: stream_transcribe used to record a failed chunk as a
bare index (`except Exception: missing.append(index)`), so a run that decoded
nothing left no trace of the cause anywhere.
"""

import json
import logging

import pytest

from yt_shorts import detect, stream_transcribe


class _Audio:
    def __init__(self, path, duration):
        self.path = path
        self.duration_seconds = duration


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.webm"
    path.write_bytes(b"not really audio")
    return _Audio(path, 1200.0)      # two 600 s chunks


def test_a_failing_chunk_logs_the_real_cause(tmp_path, audio, caplog):
    def failing_decoder(_path, start, _length, glossary=None):
        raise RuntimeError(f"model exploded at {start}")

    with caplog.at_level(logging.WARNING, logger="ytshorts.transcribe"):
        transcript = stream_transcribe.transcribe_stream(
            "vid", tmp_path,
            downloader=lambda _v, _d: audio,
            decoder=failing_decoder)

    assert transcript.missing_chunks == [0, 1]
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "model exploded" in messages          # the cause survived
    assert "RuntimeError" in messages
    assert "0" in messages and "1" in messages   # both chunk indices named


def test_an_all_empty_transcript_is_logged_as_a_warning(tmp_path, audio, caplog):
    with caplog.at_level(logging.WARNING, logger="ytshorts.transcribe"):
        stream_transcribe.transcribe_stream(
            "vid", tmp_path,
            downloader=lambda _v, _d: audio,
            decoder=lambda _p, _s, _l, glossary=None: [])

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "0 words" in messages or "no words" in messages


def test_a_successful_run_logs_a_summary(tmp_path, audio, caplog):
    def decoder(_path, start, _length, glossary=None):
        return [{"start": 1.0, "end": 1.5, "text": "hello"}]

    with caplog.at_level(logging.INFO, logger="ytshorts.transcribe"):
        stream_transcribe.transcribe_stream(
            "vid", tmp_path, downloader=lambda _v, _d: audio, decoder=decoder)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "2" in messages and "words" in messages       # chunks decoded, word count


def test_detect_logs_its_counts_and_warns_on_an_empty_transcript(tmp_path, caplog):
    """Zero moments from an empty transcript is a failure to report, not a
    quiet success - that is exactly what looked like 'detection found nothing'."""
    class _Transcript:
        words: list = []
        audio_path = tmp_path / "audio.webm"
        duration_seconds = 0.0
        missing_chunks = [0, 1, 2]

    config = {"lexicon": __import__("yt_shorts.lexicon", fromlist=["EMPTY"]).EMPTY,
              "detect": {}}

    with caplog.at_level(logging.WARNING, logger="ytshorts.detect"):
        path = detect.detect_moments(
            "vid", tmp_path, config, stream_title="Race",
            transcriber=lambda _v, _w, **_k: _Transcript())

    assert json.loads(path.read_text())["moments"] == []
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "empty" in messages.lower() or "0 words" in messages
    assert "3" in messages          # the missing-chunk count is named


def test_detect_moments_logs_through_an_injected_logger_not_the_module_one(tmp_path, caplog):
    """The studio hands detect_moments the running JOB's own logger (see
    studio/jobs.py's _run_detect), not the module-level ytshorts.detect
    logger - so its diagnosis lands in the one log file the studio actually
    shows for that job. Every one of detect_moments's own log calls must
    route through the injected logger when one is given, and the module
    logger (what a plain CLI run still uses) must stay silent in that case."""
    class _Transcript:
        words: list = []
        audio_path = tmp_path / "audio.webm"
        duration_seconds = 0.0
        missing_chunks = [0, 1]

    config = {"lexicon": __import__("yt_shorts.lexicon", fromlist=["EMPTY"]).EMPTY,
              "detect": {}}

    injected = logging.getLogger("test.injected.job.logger")
    injected.setLevel(logging.WARNING)
    injected.propagate = False
    seen = []

    class _Collector(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = _Collector()
    injected.addHandler(handler)
    try:
        with caplog.at_level(logging.WARNING, logger="ytshorts.detect"):
            path = detect.detect_moments(
                "vid", tmp_path, config, stream_title="Race",
                transcriber=lambda _v, _w, **_k: _Transcript(),
                logger=injected)
    finally:
        injected.removeHandler(handler)

    assert json.loads(path.read_text())["moments"] == []
    assert any("0 words" in message for message in seen)   # reached the injected logger
    # ...and did NOT also go through the module logger - the whole point of
    # threading a logger through is that a job's diagnosis lands in exactly
    # one place, not a duplicate leaking into the central app log too.
    assert not any("0 words" in record.getMessage() for record in caplog.records)
