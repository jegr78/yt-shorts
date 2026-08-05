import json
import subprocess

import pytest

from yt_shorts.glossary import Glossary
from yt_shorts.transcribe import NO_SPEECH_PROB_THRESHOLD, TranscriptionError, transcribe

SOURCE = "https://example.invalid/this-clip"


def silent_video(path, seconds=1):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
        "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


class TestCache:
    def test_a_present_cache_is_used_without_touching_the_video(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"source": SOURCE, "words": [{"start": 0.0, "end": 0.5, "text": "cached"}]}),
            encoding="utf-8")
        words = transcribe(str(tmp_path / "does-not-exist.mp4"), str(cache), source=SOURCE)
        assert words == [{"start": 0.0, "end": 0.5, "text": "cached"}]

    def test_a_corrupt_cache_is_reported_not_ignored(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text("{not json", encoding="utf-8")
        with pytest.raises(TranscriptionError):
            transcribe(str(tmp_path / "v.mp4"), str(cache), source=SOURCE)

    def test_a_bare_list_cache_is_reported_not_ignored(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps([{"start": 0.0, "end": 0.5, "text": "hi"}]),
                         encoding="utf-8")
        with pytest.raises(TranscriptionError):
            transcribe(str(tmp_path / "v.mp4"), str(cache), source=SOURCE)

    def test_a_cache_missing_the_words_key_is_reported_not_ignored(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps({"model": "small"}), encoding="utf-8")
        with pytest.raises(TranscriptionError):
            transcribe(str(tmp_path / "v.mp4"), str(cache), source=SOURCE)

    def test_a_cache_with_a_malformed_entry_is_reported_not_ignored(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"words": [{"start": 0.0, "end": 0.5, "text": "ok"}, {"start": 0.5}]}),
            encoding="utf-8")
        with pytest.raises(TranscriptionError):
            transcribe(str(tmp_path / "v.mp4"), str(cache), source=SOURCE)

    def test_a_cached_empty_result_is_a_legitimate_outcome(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps({"source": SOURCE, "words": []}), encoding="utf-8")
        assert transcribe(str(tmp_path / "does-not-exist.mp4"), str(cache), source=SOURCE) == []

    def test_an_old_format_cache_without_the_dropped_key_still_loads(self, tmp_path):
        """"dropped" postdates this cache format and is optional - its
        absence must not block a cache hit on its own. "source" is given
        and matching here so this test still exercises exactly the
        "dropped" backward-compat it is named for, unaffected by finding
        F1's separate (and separately tested, see TestSourceIdentity)
        source-identity check."""
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"model": "small", "source": SOURCE,
             "words": [{"start": 0.0, "end": 0.5, "text": "hi"}]}),
            encoding="utf-8")
        words = transcribe(str(tmp_path / "does-not-exist.mp4"), str(cache), source=SOURCE)
        assert words == [{"start": 0.0, "end": 0.5, "text": "hi"}]


class TestSourceIdentity:
    """Finding F1: the cache file is keyed on the caller's own file name
    choice (see bin/yt-shorts's cmd_render, which names it after the
    clip's collision-suffixed slug) - not on anything intrinsic to the
    clip. Reordering clip_urls.json can make a different clip inherit an
    old cache file under a reused name, the same hazard
    render.build_short already guards against for the raw download (see
    its docstring). transcribe() now stores the caller's ``source`` (the
    clip URL) in the cache and only trusts a cache hit whose recorded
    "source" matches; anything else - a mismatch, or no "source" key at
    all (a cache from before this change) - is treated exactly like a
    cache miss: not an error, just a silent re-transcribe, reported once
    on stderr.
    """

    def test_a_cache_recorded_for_a_different_source_is_not_used(self, tmp_path, capsys):
        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"source": "https://example.invalid/other-clip",
             "words": [{"start": 0.0, "end": 0.5, "text": "stale caption"}]}),
            encoding="utf-8")

        words = transcribe(str(video), str(cache), source=SOURCE)

        # The video is genuinely silent, so whatever re-transcribing it
        # yields is the decoder's own output (since the threshold move, a
        # hallucinated word or two rather than nothing - see TestSilence).
        # Either way it is not the stale caption, which is the point.
        assert "stale caption" not in [w["text"] for w in words]
        payload = json.loads(cache.read_text(encoding="utf-8"))
        assert payload["source"] == SOURCE
        err = capsys.readouterr().err
        assert str(video) in err
        assert "different source" in err

    def test_a_cache_with_no_source_key_is_not_used(self, tmp_path, capsys):
        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"words": [{"start": 0.0, "end": 0.5, "text": "stale caption"}]}),
            encoding="utf-8")

        words = transcribe(str(video), str(cache), source=SOURCE)

        assert "stale caption" not in [w["text"] for w in words]
        payload = json.loads(cache.read_text(encoding="utf-8"))
        assert payload["source"] == SOURCE
        err = capsys.readouterr().err
        assert "different source" in err

    def test_a_matching_source_is_used_without_touching_the_video(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"source": SOURCE, "words": [{"start": 0.0, "end": 0.5, "text": "cached"}]}),
            encoding="utf-8")

        words = transcribe(str(tmp_path / "does-not-exist.mp4"), str(cache), source=SOURCE)

        assert words == [{"start": 0.0, "end": 0.5, "text": "cached"}]

    def test_a_share_parameter_variant_still_hits_the_cache(self, tmp_path):
        """I2: the same raw-string-comparison bug clipstore._existing_dir
        had, on the same real-world trigger - a cached "source" and the
        current call's ``source`` that canonicalize to the same clip (a
        fresh share parameter, say) must still be treated as a match, not
        as "a different source" that throws away a good cache and forces
        an unnecessary re-transcription."""
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"source": SOURCE, "words": [{"start": 0.0, "end": 0.5, "text": "cached"}]}),
            encoding="utf-8")

        words = transcribe(str(tmp_path / "does-not-exist.mp4"), str(cache),
                           source=f"{SOURCE}?si=share123")

        assert words == [{"start": 0.0, "end": 0.5, "text": "cached"}]

    def test_a_different_source_note_names_the_display_name_when_given(self, tmp_path, capsys):
        """Finding B2: every NOTE this function prints should name the
        clip, not the raw file path it was told to decode - transcribe()
        itself has no notion of a clip's short name, so a caller (like
        bin/yt-shorts's cmd_render) can supply one via display_name."""
        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"source": "https://example.invalid/other-clip",
             "words": [{"start": 0.0, "end": 0.5, "text": "stale caption"}]}),
            encoding="utf-8")

        transcribe(str(video), str(cache), source=SOURCE, display_name="speedy")

        err = capsys.readouterr().err
        assert "NOTE: speedy: cached transcript" in err
        assert str(video) not in err


class TestFailures:
    def test_a_missing_video_is_reported_with_the_command(self, tmp_path):
        with pytest.raises(TranscriptionError) as error:
            transcribe(str(tmp_path / "missing.mp4"), str(tmp_path / "t.json"), source=SOURCE)
        assert "ffmpeg" in str(error.value)


class TestSilence:
    def test_silence_now_keeps_the_hallucination_and_still_writes_a_cache(self, tmp_path):
        """The accepted cost of NO_SPEECH_PROB_THRESHOLD's move to 0.95,
        with a real model rather than a stub.

        This test used to assert ``words == []`` - silence produced nothing
        because the 0.75 threshold dropped Whisper's classic "You"
        hallucination. It cannot assert that any more, and the reason is
        not a regression: real speech was measured ABOVE that hallucination
        once the glossary began biasing the decoder, so no threshold
        separates them and the operator chose to keep everything ("wrong
        captions beat missing ones - correcting a few words costs less than
        re-typing every word silently discarded").

        So a silent clip now yields a spurious word or two, visible in the
        studio's transcript editor and one edit away from gone. What this
        pins is that the pipeline still WORKS on silence - a cache is
        written, the source is recorded - and that the words it produces
        are the decoder's own, not an empty list smuggled in by a filter
        nobody re-measured."""
        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        words = transcribe(str(video), str(cache), source=SOURCE)
        payload = json.loads(cache.read_text())
        assert payload["words"] == words
        assert payload["source"] == SOURCE
        # Nothing was dropped: the hallucination scores well under 0.95.
        assert payload["dropped"]["count"] == 0


class TestNoSpeechThreshold:
    def test_keeps_every_segment_measured_as_real_speech(self):
        """0.8984: the highest no_speech_prob measured on a segment that is
        demonstrably real speech - one of four dropped from this workspace's
        `last-gasp-lap-steals-super-pole` clip, which decoded separately read
        "this should be enough to send the number 5 car into the Super Pole".
        Real speech scores that high because the glossary's terms are handed
        to the decoder as hotwords, and biasing it raises its own
        no_speech_prob (same file, same model, glossary off: 165 words and
        nothing dropped; glossary on: 40 words and those four gone).

        This test deliberately REPLACES an earlier one that pinned the
        constant strictly between 0.6847 (highest real speech, measured
        before the glossary existed) and 0.8361 (a silent clip's
        hallucinated "You"). That premise is dead: real speech now measures
        ABOVE the hallucination, so no value separates the two populations
        and the old test would have to fail for any correct constant. What
        remains is a policy - the operator's, stated explicitly: wrong
        captions beat missing ones, because correcting a few words costs
        less than re-typing every word silently discarded. Below 0.8984 this
        constant starts deleting measured commentary again; at 1.0 or above
        it stops being a net at all."""
        assert 0.8984 < NO_SPEECH_PROB_THRESHOLD < 1.0


class FakeWord:
    def __init__(self, start, end, word):
        self.start = start
        self.end = end
        self.word = word


class FakeSegment:
    def __init__(self, no_speech_prob, words):
        self.no_speech_prob = no_speech_prob
        self.words = words


class FakeModel:
    """Stands in for faster_whisper.WhisperModel: returns fixed segments
    instead of actually decoding, so drop-reporting can be tested without
    needing a real audio sample that scores above the threshold."""

    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, *args, **kwargs):
        segments = [
            FakeSegment(0.10, [FakeWord(0.0, 0.5, " kept")]),
            FakeSegment(0.98, [FakeWord(0.5, 1.0, " dropped")]),
        ]
        return iter(segments), None


class TestDroppedSegments:
    def test_a_drop_is_reported_at_transcribe_time(self, tmp_path, monkeypatch, capsys):
        import faster_whisper

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        words = transcribe(str(video), str(cache), source=SOURCE)

        assert words == [{"start": 0.0, "end": 0.5, "text": " kept"}]
        err = capsys.readouterr().err
        assert str(video) in err
        assert "dropped 1 segment" in err
        assert "0.9800" in err

    def test_a_drop_is_reported_using_the_display_name_when_given(
        self, tmp_path, monkeypatch, capsys
    ):
        """Finding B2: bin/yt-shorts's cmd_render calls this with the raw
        download's path as ``video`` (e.g.
        raw/speedy.raw.mp4) and the clip's short name as ``display_name``
        - the NOTE must name the latter, matching every other NOTE this
        tool prints, not the raw file path."""
        import faster_whisper

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        words = transcribe(str(video), str(cache), source=SOURCE, display_name="speedy")

        assert words == [{"start": 0.0, "end": 0.5, "text": " kept"}]
        err = capsys.readouterr().err
        assert "NOTE: speedy: dropped 1 segment" in err
        assert str(video) not in err

    def test_the_dropped_count_survives_a_cache_round_trip(self, tmp_path, monkeypatch, capsys):
        import faster_whisper

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        transcribe(str(video), str(cache), source=SOURCE)
        capsys.readouterr()  # discard the first run's NOTE

        payload = json.loads(cache.read_text(encoding="utf-8"))
        assert payload["dropped"]["count"] == 1
        assert payload["dropped"]["no_speech_prob"] == [0.98]

        # A second call, same source, hits the cache (no WhisperModel
        # involved) and must still report the drop that was recorded in it.
        words = transcribe(str(video), str(cache), source=SOURCE)
        assert words == [{"start": 0.0, "end": 0.5, "text": " kept"}]
        err = capsys.readouterr().err
        assert "dropped 1 segment" in err
        assert "0.9800" in err


class FakeModelCapturingKwargs:
    """Stands in for faster_whisper.WhisperModel like FakeModel above, but
    also records the kwargs its transcribe() was called with, so hotwords
    wiring can be tested without a real decode. The words it returns can be
    overridden per test via the class attribute `segments`."""

    segments: list = []

    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, *args, **kwargs):
        FakeModelCapturingKwargs.last_kwargs = kwargs
        return iter(FakeModelCapturingKwargs.segments), None


class TestGlossaryWiring:
    """Task 3: transcribe() gains a keyword-only `glossary` that biases the
    decoder (hotwords, on every decode) and corrects its output
    (replacements, applied before the result is cached)."""

    def test_hotwords_are_passed_to_the_decoder(self, tmp_path, monkeypatch):
        import faster_whisper

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModelCapturingKwargs)
        FakeModelCapturingKwargs.segments = [FakeSegment(0.10, [FakeWord(0.0, 0.5, " hi")])]

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        g = Glossary(terms=["Rei Racing", "Nordschleife"], replacements={})

        transcribe(str(video), str(cache), source=SOURCE, glossary=g)

        assert FakeModelCapturingKwargs.last_kwargs["hotwords"] == "Rei Racing, Nordschleife"

    def test_no_glossary_passes_none_as_hotwords(self, tmp_path, monkeypatch):
        """A missing glossary is the default EMPTY one - hotwords(EMPTY) is
        None, not an empty string (see glossary.hotwords), and that must
        reach the decoder unchanged: today's exact behaviour."""
        import faster_whisper

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModelCapturingKwargs)
        FakeModelCapturingKwargs.segments = [FakeSegment(0.10, [FakeWord(0.0, 0.5, " hi")])]

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"

        transcribe(str(video), str(cache), source=SOURCE)

        assert FakeModelCapturingKwargs.last_kwargs["hotwords"] is None

    def test_a_glossary_at_risk_of_truncation_is_reported_without_naming_terms(
            self, tmp_path, monkeypatch, capsys):
        import faster_whisper
        from yt_shorts.glossary import HOTWORD_BUDGET_CHARS

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModelCapturingKwargs)
        FakeModelCapturingKwargs.segments = [FakeSegment(0.10, [FakeWord(0.0, 0.5, " hi")])]

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        unit = "Confidential Sponsor Name "
        long_term = unit * (HOTWORD_BUDGET_CHARS // len(unit) + 2)  # comfortably over budget
        g = Glossary(terms=[long_term], replacements={})

        transcribe(str(video), str(cache), source=SOURCE, glossary=g)

        err = capsys.readouterr().err
        assert "truncation" in err
        assert long_term not in err   # counts/lengths only, never term text

    def test_replacements_are_applied_before_caching(self, tmp_path, monkeypatch):
        import faster_whisper

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModelCapturingKwargs)
        FakeModelCapturingKwargs.segments = [
            FakeSegment(0.10, [FakeWord(0.0, 0.34, " very"), FakeWord(0.34, 0.68, " very")]),
        ]

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        g = Glossary(terms=[], replacements={"very very": "Rei Racing"})

        words = transcribe(str(video), str(cache), source=SOURCE, glossary=g)

        assert [w["text"] for w in words] == [" Rei", " Racing"]
        payload = json.loads(cache.read_text(encoding="utf-8"))
        assert [w["text"] for w in payload["words"]] == [" Rei", " Racing"]

    def test_real_transcript_text_very_very_becomes_rei_racing(self, tmp_path, monkeypatch):
        """Built from the real `rei-got-sliced` transcript (workspace
        clip rei-got-sliced--dbd87e50), not an invented word list: Whisper
        actually decoded "...as that is very, very easy..." with a comma
        attached to the first "very" - exactly the shape Task 1's tests
        (invented, punctuation-free word lists) did not cover. This proves
        the wiring survives that real punctuation, end to end through
        transcribe(), not just glossary.apply() in isolation."""
        import faster_whisper

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModelCapturingKwargs)
        FakeModelCapturingKwargs.segments = [
            FakeSegment(0.10, [
                FakeWord(5.0, 5.7, " as"), FakeWord(5.7, 5.88, " that"),
                FakeWord(5.88, 6.1, " is"), FakeWord(6.1, 6.34, " very,"),
                FakeWord(6.34, 6.52, " very"), FakeWord(6.52, 6.74, " easy"),
            ]),
        ]

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        g = Glossary(terms=["Rei Racing"], replacements={"very very": "Rei Racing"})

        words = transcribe(str(video), str(cache), source=SOURCE, glossary=g)

        before = "".join(w.word for w in FakeModelCapturingKwargs.segments[0].words)
        after = "".join(w["text"] for w in words)
        assert before == " as that is very, very easy"
        assert after == " as that is Rei Racing easy"

    def test_a_cache_hit_is_not_re_corrected(self, tmp_path):
        """Nothing invalidates a cached transcript when the glossary
        changes (see transcribe()'s own docstring): a cache hit returns
        exactly what was cached, even against a glossary that would now
        match it."""
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps({
            "source": SOURCE,
            "words": [
                {"start": 6.1, "end": 6.34, "text": " very,"},
                {"start": 6.34, "end": 6.52, "text": " very"},
            ],
        }), encoding="utf-8")
        g = Glossary(terms=[], replacements={"very very": "Rei Racing"})

        words = transcribe(str(tmp_path / "does-not-exist.mp4"), str(cache),
                           source=SOURCE, glossary=g)

        assert [w["text"] for w in words] == [" very,", " very"]


class TestWavCleanup:
    """render.build_short's convention: clean up after success, leave
    everything in place after a failure, because the artifacts are the
    only thing that makes the failure investigable."""

    def test_the_wav_is_left_behind_when_transcription_fails(self, tmp_path, monkeypatch):
        import faster_whisper

        class BoomModel:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(faster_whisper, "WhisperModel", BoomModel)

        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        with pytest.raises(TranscriptionError):
            transcribe(str(video), str(cache), source=SOURCE)

        assert cache.with_suffix(".wav").exists()

    def test_the_wav_is_removed_after_a_successful_transcription(self, tmp_path):
        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        transcribe(str(video), str(cache), source=SOURCE)

        assert not cache.with_suffix(".wav").exists()


class TestDecodeWav:
    def test_it_drops_high_no_speech_segments_and_applies_glossary(self, monkeypatch, tmp_path):
        import faster_whisper
        from yt_shorts.transcribe import decode_wav

        monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModelCapturingKwargs)
        # A real segment (kept) and a degenerate one (dropped at > 0.95 -
        # see NO_SPEECH_PROB_THRESHOLD on why only near-certainty drops now).
        FakeModelCapturingKwargs.segments = [
            FakeSegment(0.10, [FakeWord(0.0, 0.5, " very"), FakeWord(0.5, 1.0, " very")]),
            FakeSegment(0.98, [FakeWord(1.0, 1.5, " You")]),
        ]

        wav = tmp_path / "a.wav"          # content ignored by the fake model
        wav.write_bytes(b"")
        words, dropped = decode_wav(
            wav, model_name="small",
            glossary=Glossary(terms=[], replacements={"very very": "Rei"}))

        assert [w["text"] for w in words] == [" Rei"]      # glossary applied
        assert dropped == [0.98]                            # degenerate segment dropped, recorded
