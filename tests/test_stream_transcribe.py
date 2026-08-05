import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from yt_shorts import stream_transcribe as stream_transcribe_module
from yt_shorts.cancel import CancelToken, Stopped
from yt_shorts.stream_transcribe import (
    DownloadedAudio, StreamTranscribeError, StreamTranscript,
    chunk_windows, offset_words, run_with_timeout, subprocess_decoder,
    transcribe_stream, ytdlp_downloader)

VIDEO = "V9nVNEQNdR4"


def fake_downloader(duration, name="audio.webm"):
    def download(video_id, dest_dir):
        path = Path(dest_dir) / name
        path.write_bytes(b"fake audio")
        return DownloadedAudio(path=path, duration_seconds=duration)
    return download


def fake_decoder(fail_on=()):
    # Returns one chunk-relative word per chunk, tagged by the chunk's start,
    # so the assembly's absolute times are checkable. Raises for chunks in
    # fail_on. Records the glossary it was handed per call - transcribe_stream
    # is required to pass one, and a stub that ignored it would hide a
    # regression in exactly the plumbing that was missing for months.
    calls = []
    glossaries = []

    def decode(audio_path, start, length, glossary=None):
        calls.append(start)
        glossaries.append(glossary)
        if start in fail_on:
            raise RuntimeError(f"decode hung at {start}")
        return [{"start": 0.0, "end": 1.0, "text": f" s{int(start)}"}]

    decode.calls = calls
    decode.glossaries = glossaries
    return decode


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


class TestTranscribeStream:
    @pytest.mark.parametrize("bad", ["../../etc", "..", "a/b"])
    def test_traversal_video_id_is_refused(self, tmp_path, bad):
        # video_id becomes a path segment (streams/<id>) and the download URL.
        with pytest.raises(StreamTranscribeError, match="not a valid"):
            transcribe_stream(bad, tmp_path,
                              downloader=fake_downloader(300), decoder=fake_decoder())

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


class TestProgressPerChunk:
    """The seam that lets a two-hour transcription say where it has got to.

    Additive and defaulting to None, exactly like `cancel`: the tests above
    pass none and are unchanged by its existence, which is the property
    this class's first test pins explicitly rather than leaving implied.
    """

    def _readings(self, tmp_path, **kwargs):
        seen = []
        transcribe_stream(VIDEO, tmp_path,
                          progress=lambda done, total: seen.append((done, total)),
                          **kwargs)
        return seen

    def test_one_reading_per_chunk_counting_chunks_finished(self, tmp_path):
        seen = self._readings(tmp_path, downloader=fake_downloader(1500),
                              decoder=fake_decoder(), chunk_seconds=600)
        assert seen == [(1, 3), (2, 3), (3, 3)]

    def test_a_run_with_no_callback_behaves_exactly_as_before(self, tmp_path):
        # The `cancel` rule, restated for this parameter: every existing
        # caller keeps working untouched, and nothing here even asks
        # whether one was given until a chunk is finished.
        result = transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                                   decoder=fake_decoder(), chunk_seconds=600)
        assert [w["text"] for w in result.words] == [" s0", " s600"]

    def test_a_cached_chunk_counts_too(self, tmp_path):
        # Otherwise a resumed run sits silent over every chunk it already
        # has - which is most of them - and reads as a hung job.
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                          decoder=fake_decoder(), chunk_seconds=600)
        seen = self._readings(tmp_path, downloader=fake_downloader(1200),
                              decoder=fake_decoder(), chunk_seconds=600)
        assert seen == [(1, 2), (2, 2)]

    def test_a_failed_chunk_counts_too(self, tmp_path):
        # It is a chunk nobody will look at again on this run; leaving it
        # out would stall the reading one short for the rest of the stream.
        seen = self._readings(tmp_path, downloader=fake_downloader(1800),
                              decoder=fake_decoder(fail_on=(600.0,)), chunk_seconds=600)
        assert seen == [(1, 3), (2, 3), (3, 3)]

    def test_a_chunk_a_stop_never_reached_is_never_reported(self, tmp_path):
        # The stop is raised at the TOP of the iteration, before the work -
        # reporting the chunk would claim something had been looked at that
        # nothing looked at.
        token = CancelToken()
        seen = []
        with pytest.raises(Stopped):
            transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(3000),
                              decoder=stopping_decoder(token, stop_on_call=2),
                              chunk_seconds=600, cancel=token,
                              progress=lambda done, total: seen.append((done, total)))
        assert seen == [(1, 5), (2, 5)]


def fake_runner(*, meta_duration=1500.0, meta_error=None,
                probe_duration=None, probe_output=None, probe_error=None):
    """Fake subprocess boundary for ytdlp_downloader.

    Dispatches on the shape of the args list - metadata (--skip-download),
    probe (ffprobe), or download (--continue) - the same way the real
    subprocess calls are told apart by their flags, and records every call so
    a test can assert what was (or wasn't) invoked.
    """
    calls: list[list[str]] = []

    def runner(args, *, timeout):
        calls.append(args)
        if "--skip-download" in args:
            if meta_error:
                raise RuntimeError(meta_error)
            return json.dumps({"duration": meta_duration}) + "\n"
        if args[0] == "ffprobe":
            if probe_error:
                raise RuntimeError(probe_error)
            if probe_output is not None:
                return probe_output
            return f"{probe_duration}\n"
        # the download command: mimic yt-dlp writing the file it names with -o
        template = args[args.index("-o") + 1]
        Path(template.replace("%(ext)s", "webm")).write_bytes(b"downloaded audio")
        return json.dumps({"duration": meta_duration}) + "\n"

    runner.calls = calls
    return runner


class TestYtdlpDownloader:
    def test_complete_local_file_is_reused_without_downloading(self, tmp_path):
        (tmp_path / "audio.webm").write_bytes(b"a complete download")
        runner = fake_runner(meta_duration=1500.0, probe_duration=1499.5)  # within tolerance
        result = ytdlp_downloader(VIDEO, tmp_path, runner=runner)
        assert result.path == tmp_path / "audio.webm"
        assert result.duration_seconds == 1500.0
        assert not any("--continue" in call for call in runner.calls)  # never downloaded

    def test_partial_local_file_is_redownloaded(self, tmp_path):
        (tmp_path / "audio.webm").write_bytes(b"a partial download")
        runner = fake_runner(meta_duration=1500.0, probe_duration=300.0)  # far short
        result = ytdlp_downloader(VIDEO, tmp_path, runner=runner)
        assert any("--continue" in call for call in runner.calls)
        assert result.duration_seconds == 1500.0

    def test_no_local_file_downloads(self, tmp_path):
        runner = fake_runner(meta_duration=1500.0)
        result = ytdlp_downloader(VIDEO, tmp_path, runner=runner)
        assert any("--continue" in call for call in runner.calls)
        assert result.path == tmp_path / "audio.webm"
        assert result.duration_seconds == 1500.0

    def test_probe_error_falls_through_to_download(self, tmp_path):
        (tmp_path / "audio.webm").write_bytes(b"unreadable")
        runner = fake_runner(meta_duration=1500.0, probe_error="ffprobe: invalid data")
        result = ytdlp_downloader(VIDEO, tmp_path, runner=runner)
        assert any("--continue" in call for call in runner.calls)
        assert result.duration_seconds == 1500.0

    def test_unparseable_probe_output_falls_through_to_download(self, tmp_path):
        (tmp_path / "audio.webm").write_bytes(b"weird file")
        runner = fake_runner(meta_duration=1500.0, probe_output="N/A\n")
        result = ytdlp_downloader(VIDEO, tmp_path, runner=runner)
        assert any("--continue" in call for call in runner.calls)
        assert result.duration_seconds == 1500.0

    def test_metadata_failure_raises_runtime_error(self, tmp_path):
        runner = fake_runner(meta_error="yt-dlp: video unavailable")
        with pytest.raises(RuntimeError, match="video unavailable"):
            ytdlp_downloader(VIDEO, tmp_path, runner=runner)


class TestRunWithTimeout:
    def test_returns_stdout_on_success(self):
        out = run_with_timeout([sys.executable, "-c", "print('hello')"], timeout=10)
        assert out.strip() == "hello"

    def test_kills_and_raises_on_timeout(self):
        t0 = time.monotonic()
        with pytest.raises(StreamTranscribeError):
            # sleeps far longer than the timeout; must be killed, not waited out
            run_with_timeout([sys.executable, "-c", "import time; time.sleep(30)"],
                             timeout=1)
        assert time.monotonic() - t0 < 10          # returned promptly, i.e. killed

    def test_nonzero_exit_raises(self):
        with pytest.raises(StreamTranscribeError):
            run_with_timeout([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=10)


def fake_worker_runner():
    """Fake subprocess boundary for subprocess_decoder's -m worker call.

    Mirrors fake_runner above for ytdlp_downloader: records the env each call
    received (rather than actually spawning anything), so a test can assert on
    it without a real subprocess or a real model.
    """
    calls = []

    def runner(cmd, *, timeout, env=None):
        calls.append({"cmd": cmd, "timeout": timeout, "env": env})
        return "[]"

    runner.calls = calls
    return runner


class TestSubprocessDecoderEnv:
    # `ffmpeg="true"` stands in for the extraction step: the coreutils `true`
    # binary exits 0 regardless of its arguments, so the wav-extraction
    # subprocess.run(check=True) call succeeds without a real ffmpeg or a real
    # audio file, leaving the worker call (routed through the fake runner) as
    # the only thing under test.

    def test_worker_env_has_src_dir_first_in_pythonpath(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PYTHONPATH", raising=False)
        runner = fake_worker_runner()
        subprocess_decoder(tmp_path / "audio.webm", 0, 1, ffmpeg="true", runner=runner)
        src_dir = str(Path(stream_transcribe_module.__file__).resolve().parent.parent)
        assert runner.calls[0]["env"]["PYTHONPATH"] == src_dir

    def test_worker_env_preserves_existing_pythonpath_after_src_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PYTHONPATH", "/some/other/path")
        runner = fake_worker_runner()
        subprocess_decoder(tmp_path / "audio.webm", 0, 1, ffmpeg="true", runner=runner)
        src_dir = str(Path(stream_transcribe_module.__file__).resolve().parent.parent)
        assert runner.calls[0]["env"]["PYTHONPATH"] == f"{src_dir}{os.pathsep}/some/other/path"

    def test_other_environment_variables_survive_into_the_child(self, monkeypatch, tmp_path):
        monkeypatch.setenv("YT_SHORTS_TEST_MARKER", "still here")
        runner = fake_worker_runner()
        subprocess_decoder(tmp_path / "audio.webm", 0, 1, ffmpeg="true", runner=runner)
        assert runner.calls[0]["env"]["YT_SHORTS_TEST_MARKER"] == "still here"


class TestSubprocessDecoderForwardsCancel:
    """A code review proved `subprocess_decoder`'s own `runner(..., **
    cancel_kwargs(cancel))` call (stream_transcribe.py, the worker-invoking
    line inside subprocess_decoder) could be mutated to drop the token -
    `runner(args, timeout=..., env=...)` with no cancel_kwargs at all - and
    every existing test in this file stayed green, because none of them
    passes a real token and inspects what the runner actually received. This
    is the same `seen == [token]` shape `test_the_cancel_token_reaches_the_
    transcriber` (tests/test_detect.py) already uses one layer up, applied
    directly at this hop instead of only through transcribe_stream."""

    def test_the_token_reaches_the_runner(self, tmp_path, monkeypatch):
        def fake_run(args, **kwargs):
            # ffmpeg wav-extraction step, stubbed out same as the rest of
            # this file's TestGlossaryReachesTheWorker helpers.
            Path(args[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(stream_transcribe_module.subprocess, "run", fake_run)

        token = CancelToken()
        seen = []

        def spy_runner(args, *, timeout, env=None, cancel=None):
            seen.append(cancel)
            return "[]"

        subprocess_decoder(tmp_path / "audio.webm", 0.0, 600.0,
                           runner=spy_runner, cancel=token)

        assert seen == [token], (
            "subprocess_decoder did not forward its cancel token into runner"
        )


class TestGlossaryReachesTheWorker:
    """The gap this closes: _decode_worker.main has always accepted argv[3] as
    a glossary JSON path, and subprocess_decoder never passed one - so every
    stream chunk ever decoded in this project decoded with an EMPTY glossary."""

    def _runner(self):
        seen = {}

        def run(args, *, timeout, env=None):
            seen["args"] = list(args)
            seen["env"] = env
            return json.dumps([{"start": 0.0, "end": 1.0, "text": " word"}])

        run.seen = seen
        return run

    def test_a_non_empty_glossary_is_written_and_passed_as_argv3(self, tmp_path, monkeypatch):
        from yt_shorts.glossary import Glossary

        written = {}

        def fake_run(args, **kwargs):
            # The ffmpeg extraction: create the wav the decoder expects, and
            # capture the glossary file while the TemporaryDirectory lives.
            Path(args[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(stream_transcribe_module.subprocess, "run", fake_run)

        runner = self._runner()

        def capturing(args, *, timeout, env=None):
            written["payload"] = json.loads(Path(args[5]).read_text(encoding="utf-8"))
            return runner(args, timeout=timeout, env=env)

        out = subprocess_decoder(
            tmp_path / "audio.webm", 0.0, 600.0,
            glossary=Glossary(terms=["Karussell"], replacements={"carousel": "Karussell"}),
            runner=capturing)

        assert out == [{"start": 0.0, "end": 1.0, "text": " word"}]
        assert len(runner.seen["args"]) == 6
        # Only terms are ever sent - replacements are applied once, at
        # assembly, never inside the worker (see subprocess_decoder's
        # docstring on why sending them here would defeat both the
        # boundary-spanning match and the no-re-decode property).
        assert written["payload"] == {"terms": ["Karussell"]}
        assert "replacements" not in written["payload"]

    def test_a_glossary_at_risk_of_truncation_is_warned_about_without_naming_terms(
            self, tmp_path, monkeypatch, caplog):
        from yt_shorts.glossary import HOTWORD_BUDGET_CHARS, Glossary

        def fake_run(args, **kwargs):
            Path(args[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(stream_transcribe_module.subprocess, "run", fake_run)
        runner = self._runner()
        unit = "Confidential Sponsor Name "
        long_term = unit * (HOTWORD_BUDGET_CHARS // len(unit) + 2)  # comfortably over budget

        with caplog.at_level(logging.WARNING, logger="ytshorts.transcribe"):
            subprocess_decoder(
                tmp_path / "audio.webm", 0.0, 600.0,
                glossary=Glossary(terms=[long_term], replacements={}),
                runner=runner)

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "truncation" in messages
        assert long_term not in messages   # counts/lengths only, never term text

    def test_an_empty_glossary_passes_no_fourth_argument(self, tmp_path, monkeypatch):
        def fake_run(args, **kwargs):
            Path(args[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(stream_transcribe_module.subprocess, "run", fake_run)
        runner = self._runner()

        subprocess_decoder(tmp_path / "audio.webm", 0.0, 600.0, runner=runner)

        # Pins that nothing shifted for a caller that passes no glossary:
        # python -m yt_shorts._decode_worker <wav> <model>, five elements.
        assert len(runner.seen["args"]) == 5

    def test_a_replacements_only_glossary_passes_no_fourth_argument(self, tmp_path, monkeypatch):
        # A glossary with only replacements (no terms) has nothing to bias
        # the decoder with - replacements are applied once, at assembly, and
        # must never reach the worker (see subprocess_decoder's docstring).
        from yt_shorts.glossary import Glossary

        def fake_run(args, **kwargs):
            Path(args[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(stream_transcribe_module.subprocess, "run", fake_run)
        runner = self._runner()

        subprocess_decoder(
            tmp_path / "audio.webm", 0.0, 600.0,
            glossary=Glossary(terms=[], replacements={"carousel": "Karussell"}),
            runner=runner)

        assert len(runner.seen["args"]) == 5


class TestGlossaryAtAssembly:
    """Corrections are applied to the ASSEMBLED word list, and the per-chunk
    cache stays RAW - so a glossary change takes effect on the next assembly
    with no re-decode, and a correction may span a chunk boundary."""

    def test_the_transcript_is_corrected_but_the_chunk_cache_is_not(self, tmp_path):
        from yt_shorts.glossary import Glossary

        def decoder(audio_path, start, length, glossary=None):
            return [{"start": 0.0, "end": 1.0, "text": " carousel"}]

        result = transcribe_stream(
            VIDEO, tmp_path, downloader=fake_downloader(300), decoder=decoder,
            glossary=Glossary(terms=[], replacements={"carousel": "Karussell"}))

        assert [w["text"].strip() for w in result.words] == ["Karussell"]
        transcript = json.loads(
            (tmp_path / "streams" / VIDEO / "transcript.json").read_text(encoding="utf-8"))
        assert transcript["words"][0]["text"].strip() == "Karussell"
        cached = json.loads(
            (tmp_path / "streams" / VIDEO / "chunks" / "000.json").read_text(encoding="utf-8"))
        assert cached["words"][0]["text"].strip() == "carousel"

    def test_a_changed_glossary_recorrects_a_fully_cached_run(self, tmp_path):
        from yt_shorts.glossary import Glossary

        first = fake_decoder()
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(300),
                          decoder=first, chunk_seconds=600)
        assert first.calls == [0.0]

        def exploding(audio_path, start, length, glossary=None):
            raise AssertionError("must not decode again - the chunk cache is still valid")

        result = transcribe_stream(
            VIDEO, tmp_path, downloader=fake_downloader(300), decoder=exploding,
            chunk_seconds=600,
            glossary=Glossary(terms=[], replacements={"s0": "CORRECTED"}))

        assert [w["text"].strip() for w in result.words] == ["CORRECTED"]

    def test_a_correction_spans_a_chunk_boundary(self, tmp_path):
        # This uses a STUBBED decoder, so it cannot see the class of bug
        # where subprocess_decoder sends replacements to the worker and gets
        # them applied (and cached) too early - see
        # TestSubprocessDecoderRealChain.test_the_worker_output_is_uncorrected_raw_text
        # below, which runs the real worker for that guard.
        from yt_shorts.glossary import Glossary

        def decoder(audio_path, start, length, glossary=None):
            return [{"start": 0.0, "end": 1.0, "text": " kleine" if start == 0.0 else " carousel"}]

        result = transcribe_stream(
            VIDEO, tmp_path, downloader=fake_downloader(1200), decoder=decoder,
            chunk_seconds=600,
            glossary=Glossary(terms=[], replacements={"kleine carousel": "Kleines Karussell"}))

        assert "".join(w["text"] for w in result.words).strip() == "Kleines Karussell"

    def test_the_glossary_reaches_every_decoded_chunk(self, tmp_path):
        from yt_shorts.glossary import Glossary

        glossary = Glossary(terms=["Karussell"], replacements={})
        decoder = fake_decoder()
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                          decoder=decoder, chunk_seconds=600, glossary=glossary)
        assert decoder.glossaries == [glossary, glossary]

    def test_no_glossary_leaves_the_words_untouched(self, tmp_path):
        result = transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(300),
                                    decoder=fake_decoder())
        assert [w["text"].strip() for w in result.words] == ["s0"]


class TestSubprocessDecoderRealChain:
    """Every test above stubs the decoder, so none of them can see the class
    of bug where subprocess_decoder sends the worker a payload it should
    not: the stub never touches _decode_worker.main or decode_wav, so a
    payload shaped {"terms": [...], "replacements": {...}} would sail
    through every one of those tests unnoticed even though decode_wav
    applies replacements unconditionally and would cache the CORRECTED
    text. This runs the real chain - _decode_worker.main, in-process, with
    only faster_whisper.WhisperModel stubbed (mirroring
    tests/test_transcribe.py's FakeModelCapturingKwargs).

    The payload it feeds the worker is CAPTURED FROM A REAL
    subprocess_decoder CALL, not hand-written here. That distinction is the
    whole point: a hand-written terms-only file would keep this test passing
    unchanged if subprocess_decoder regressed to sending replacements again,
    which is exactly the regression it exists to catch. Capturing instead
    means one test spans the whole chain - what the production writer
    writes, and what the production worker then does with it - rather than
    two tests each proving one half and trusting the halves to stay in sync.
    """

    def test_the_worker_output_is_uncorrected_raw_text(self, tmp_path, monkeypatch, capsys):
        import faster_whisper

        from yt_shorts import _decode_worker, tracks
        from yt_shorts.profile import merge_glossaries

        class FakeWord:
            def __init__(self, start, end, word):
                self.start = start
                self.end = end
                self.word = word

        class FakeSegment:
            def __init__(self, no_speech_prob, words):
                self.no_speech_prob = no_speech_prob
                self.words = words

        class FakeModelCapturingKwargs:
            """Mirrors tests/test_transcribe.py's FakeModelCapturingKwargs:
            stands in for faster_whisper.WhisperModel, returning a mis-heard
            phrase ("carousel") that the Nordschleife pack's replacements
            would correct to "Karussell" if replacements ever reached this
            worker."""

            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, *args, **kwargs):
                return iter([FakeSegment(0.10, [FakeWord(0.0, 1.0, " carousel")])]), None

        # Step 1: capture what the REAL subprocess_decoder hands the worker,
        # against the real shipped Nordschleife pack - 32 terms AND 10
        # replacements, so a payload that wrongly carried replacements would
        # carry the "carousel" -> "Karussell" rule the fake model's output
        # below is chosen to trip. The TemporaryDirectory subprocess_decoder
        # writes into is gone by the time it returns, so the payload's TEXT is
        # read inside the runner and rewritten under tmp_path afterwards.
        captured: dict[str, str] = {}

        def capturing_runner(args, *, timeout, env=None):
            if len(args) > 5:
                captured["payload"] = Path(args[5]).read_text(encoding="utf-8")
            return json.dumps([])

        def fake_extract(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"")  # stand in for the ffmpeg wav extraction
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(stream_transcribe_module.subprocess, "run", fake_extract)
        subprocess_decoder(tmp_path / "audio.webm", 0.0, 600.0,
                           glossary=merge_glossaries(
                               [tracks.as_layer(tracks.get("nurburgring-nordschleife"))]),
                           runner=capturing_runner)
        # Undo the subprocess patch before the worker runs, so nothing the
        # real chain does below is answered by this test's own stand-in.
        monkeypatch.undo()
        assert "payload" in captured, "subprocess_decoder passed the worker no glossary file"

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModelCapturingKwargs)

        wav = tmp_path / "chunk.wav"
        wav.write_bytes(b"")  # content ignored by the fake model

        glossary_path = tmp_path / "glossary.json"
        glossary_path.write_text(captured["payload"], encoding="utf-8")

        argv = ["_decode_worker.py", str(wav), "small", str(glossary_path)]
        rc = _decode_worker.main(argv)

        assert rc == 0
        words = json.loads(capsys.readouterr().out)
        # Proof: even though "carousel" -> "Karussell" IS a rule in the very
        # glossary subprocess_decoder was handed, the word comes back
        # UNCORRECTED - because what subprocess_decoder actually wrote carried
        # no replacements for the worker to apply. Re-add them to that payload
        # and this assertion fails, which is what makes it a guard.
        assert [w["text"] for w in words] == [" carousel"]


def stopping_decoder(token, *, stop_on_call):
    """A decoder that asks for the stop itself, on its `stop_on_call`-th call.

    Records the `cancel` it was handed, because a decoder that never receives
    one cannot terminate its own worker on a hard stop - the plumbing this
    parameter exists for.
    """
    calls = []
    tokens = []

    def decode(audio_path, start, length, glossary=None, cancel=None):
        calls.append(start)
        tokens.append(cancel)
        if len(calls) >= stop_on_call:
            token.request_stop()
        return [{"start": 0.0, "end": 1.0, "text": f" s{int(start)}"}]

    decode.calls = calls
    decode.tokens = tokens
    return decode


class TestStoppingTranscription:
    """Between chunks is the one place stopping a transcription costs
    nothing: every chunk decoded so far is already cached, so a re-run picks
    up at the first missing one."""

    def test_transcription_stops_after_the_current_chunk_and_keeps_what_it_cached(
            self, tmp_path):
        token = CancelToken()
        decoder = stopping_decoder(token, stop_on_call=3)
        with pytest.raises(Stopped):
            transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(3000),
                              decoder=decoder, chunk_seconds=600, cancel=token)
        assert decoder.calls == [0.0, 600.0, 1200.0], "a chunk was decoded after the stop"
        cached = sorted((tmp_path / "streams" / VIDEO / "chunks").glob("*.json"))
        assert [c.name for c in cached] == ["000.json", "001.json", "002.json"]
        assert decoder.tokens == [token, token, token]

    def test_a_stopped_transcription_writes_no_transcript_json(self, tmp_path):
        # A partial transcript that LOOKS complete is how a stream ends up
        # detected against half its own audio - so nothing is written at all.
        token = CancelToken()
        with pytest.raises(Stopped):
            transcribe_stream(VIDEO, tmp_path,
                              downloader=fake_downloader(3000),
                              decoder=stopping_decoder(token, stop_on_call=2),
                              chunk_seconds=600, cancel=token)
        assert not (tmp_path / "streams" / VIDEO / "transcript.json").exists()

    def test_a_stopped_transcription_resumes_where_it_left_off(self, tmp_path):
        token = CancelToken()
        with pytest.raises(Stopped):
            transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(3000),
                              decoder=stopping_decoder(token, stop_on_call=3),
                              chunk_seconds=600, cancel=token)
        resumed = fake_decoder()
        result = transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(3000),
                                   decoder=resumed, chunk_seconds=600)
        assert resumed.calls == [1800.0, 2400.0], "a cached chunk was decoded again"
        assert [w["text"] for w in result.words] == [
            " s0", " s600", " s1200", " s1800", " s2400"]

    def test_a_stop_from_inside_the_decoder_is_not_a_failed_chunk(self, tmp_path):
        # A hard stop kills the decode worker mid-chunk, so the decoder itself
        # raises Stopped. The per-chunk handler must not swallow that into
        # missing_chunks: a chunk nobody finished looking at is not a chunk
        # that was attempted and could not be heard.
        #
        # ONE chunk, and the kill requested from INSIDE the decode rather than
        # before the call - both deliberate. With the whole stream in one
        # chunk there is no second pass through the loop to catch the stop at
        # the top, so this is the only checkpoint that can possibly act; drop
        # the `except Stopped: raise` and the run instead finishes "normally"
        # with missing_chunks=[0] and writes a transcript.json holding no
        # words at all.
        token = CancelToken()

        def killed_decoder(audio_path, start, length, glossary=None, cancel=None):
            cancel.request_kill()
            raise Stopped("the subprocess was terminated on a hard stop")

        with pytest.raises(Stopped):
            transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(600),
                              decoder=killed_decoder, chunk_seconds=600, cancel=token)
        assert not (tmp_path / "streams" / VIDEO / "transcript.json").exists()

    def test_without_a_token_nothing_is_passed_to_the_decoder(self, tmp_path):
        # Every decoder written before this parameter existed keeps working:
        # the kwarg is only sent when a caller actually asked for cancellation.
        seen = {}

        def strict_decoder(audio_path, start, length, glossary=None):
            seen["called"] = True
            return []

        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(600),
                          decoder=strict_decoder, chunk_seconds=600)
        assert seen["called"] is True


class TestRunWithTimeoutHonoursAKill:
    def test_a_kill_ends_a_running_child_promptly(self):
        # A real child process, killed from another thread - the production
        # wiring, not a stub. Without the token this sleeps for 30 seconds.
        token = CancelToken()
        killer = threading.Timer(0.2, token.request_kill)
        killer.start()
        started = time.monotonic()
        try:
            with pytest.raises(Stopped):
                run_with_timeout([sys.executable, "-c", "import time; time.sleep(30)"],
                                 timeout=30, cancel=token)
        finally:
            killer.cancel()
        assert time.monotonic() - started < 10

    def test_a_timeout_still_raises_the_stream_error_not_stopped(self):
        token = CancelToken()
        with pytest.raises(StreamTranscribeError):
            run_with_timeout([sys.executable, "-c", "import time; time.sleep(30)"],
                             timeout=1, cancel=token)
