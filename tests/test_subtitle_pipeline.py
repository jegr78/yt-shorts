"""How the subtitle pipeline REPORTS a clip it could not caption.

Degrading to "no subtitles" for one clip rather than failing the whole render
is deliberate and documented (see make_subtitle_provider's own docstring). Doing
it SILENTLY was not: the notes went to sys.stderr, which is fine for the CLI
(the operator is watching a terminal) and useless for the studio, whose job
runs on a background thread nobody is reading stderr from. An operator saw a
render succeed, got a short with no captions, and found nothing anywhere saying
why.

The worst case is an overlapping word list. captions.group_words groups it
happily, but subtitle_track refuses it outright - so the clip loses EVERY
caption, not one - and the studio's transcript editor lets an operator create
exactly that state by dragging a timing.
"""

from __future__ import annotations

import logging

import pytest

from yt_shorts import clipstore, editorial, subtitle_pipeline
from yt_shorts.clipid import canonical_url
from yt_shorts.profile import load as profile_load

CLIP_URL = "https://www.youtube.com/clip/UgkxNotes123"


def _clip_entry():
    return {"url": CLIP_URL, "hook": "Speedy!", "source_title": "ERF Round 3",
            "start": 10.0, "end": 25.0, "duration": 15.0, "error": None}


@pytest.fixture
def captioning_config():
    config = profile_load("erf/community-clips-back-catalogue").config
    return {**config, "subtitles": {**config.get("subtitles", {}), "enabled": True}}


def _provider(directory, config, words, monkeypatch, **kwargs):
    """A provider whose transcription is stubbed to `words` - no Whisper, no
    audio. Everything after that point (the glossary, effective_words,
    group_words, build_track) runs for real, which is the whole point: the
    refusal under test comes from subtitle_track, not from a mock."""
    monkeypatch.setattr(subtitle_pipeline, "transcribe",
                        lambda *a, **kw: list(words))
    edit = editorial.load(directory)
    return subtitle_pipeline.make_subtitle_provider(
        directory, edit, clipstore.read_clip(directory), "Speedy!", config, **kwargs)


def _edit(**over):
    return editorial.Edit(title=None, status=editorial.CANDIDATE,
                          transcript=None, **over)


class TestTranscriptSourceFollowsTheTrimmedWindow:
    """A trimmed window must not keep the untrimmed window's captions.

    render.source_for_clip downloads editorial.effective_window; the transcript
    cache used to be keyed on clip["url"], which an editorial trim never
    changes. Trimming therefore re-downloaded a shorter passage and burned in
    the OLD transcript against it - every caption offset by exactly the amount
    trimmed, with nothing reporting it.
    """

    def test_an_untrimmed_clip_keeps_the_clip_url_byte_for_byte(self):
        # Not cosmetic: any other value would miss the cache once for every
        # clip in every workspace, at minutes of Whisper apiece.
        clip = _clip_entry()
        assert subtitle_pipeline.transcript_source(
            clip, _edit()) == clip["url"]

    def test_a_trimmed_clip_gets_an_identity_the_old_cache_cannot_answer(self):
        clip = _clip_entry()
        trimmed = subtitle_pipeline.transcript_source(
            clip, _edit(window=(16.0, 25.0)))
        assert trimmed != clip["url"]
        # The window rides in the PATH: canonical_url strips a query string
        # and a fragment before comparing, so either would erase it and this
        # whole fix would silently do nothing.
        assert "?" not in trimmed and "#" not in trimmed
        assert canonical_url(trimmed) != canonical_url(clip["url"])

    def test_two_different_trims_do_not_share_one_cache(self):
        clip = _clip_entry()
        first = subtitle_pipeline.transcript_source(
            clip, _edit(window=(16.0, 25.0)))
        second = subtitle_pipeline.transcript_source(
            clip, _edit(window=(17.0, 25.0)))
        assert first != second

    def test_the_provider_hands_transcribe_the_trimmed_identity(
            self, tmp_path, captioning_config, monkeypatch):
        """End to end through make_subtitle_provider, because the helper being
        right is worth nothing if the call site still passes clip["url"]."""
        directory = clipstore.write_clip(tmp_path, _clip_entry())
        edit = editorial.load(directory)
        edit.window = (16.0, 25.0)
        editorial.save(directory, edit)
        seen: list[str] = []

        def fake_transcribe(video, cache, *a, source, **kw):
            seen.append(source)
            return [{"start": 0.0, "end": 1.0, "text": " hi"}]

        monkeypatch.setattr(subtitle_pipeline, "transcribe", fake_transcribe)
        provider = subtitle_pipeline.make_subtitle_provider(
            directory, editorial.load(directory), clipstore.read_clip(directory),
            "Speedy!", captioning_config)
        provider("unused-raw-path.mp4")

        assert seen and seen[0] != CLIP_URL, seen
        assert "16.000-25.000" in seen[0]


class TestReportingALostCaptionTrack:
    def test_an_overlapping_word_list_reports_that_the_clip_lost_its_subtitles(
            self, tmp_path, captioning_config, monkeypatch, caplog):
        """The state the transcript editor can produce with one dragged
        timing, and the reason the editor's warning must not be reassuring:
        subtitle_track refuses the whole list, so the short renders with no
        captions at all."""
        directory = clipstore.write_clip(tmp_path, _clip_entry())
        notes: list[str] = []
        overlapping = [{"start": 0.0, "end": 1.0, "text": " here"},
                       {"start": 0.5, "end": 9.0, "text": " stretched"}]

        provider = _provider(directory, captioning_config, overlapping, monkeypatch,
                             on_note=notes.append)
        with caplog.at_level(logging.WARNING, logger="ytshorts.subtitles"):
            assert provider("unused-raw-path.mp4") is None

        assert notes, "the clip lost every caption and said nothing"
        assert "no subtitles" in notes[0]
        assert "overlap" in notes[0].lower(), notes[0]
        assert any("no subtitles" in record.message for record in caplog.records), (
            "nothing reached the log either"
        )

    def test_no_speech_is_reported_too(
            self, tmp_path, captioning_config, monkeypatch):
        """The benign case still reports: an operator who expected captions
        deserves to know none were found, rather than inferring it from a
        silent video."""
        directory = clipstore.write_clip(tmp_path, _clip_entry())
        notes: list[str] = []

        provider = _provider(directory, captioning_config, [], monkeypatch,
                             on_note=notes.append)
        assert provider("unused-raw-path.mp4") is None

        assert any("no speech" in note for note in notes), notes

    def test_a_stale_correction_is_reported(
            self, tmp_path, captioning_config, monkeypatch):
        """The conflict note - the correction wins, and the operator is told
        it was made against a transcript that has since changed."""
        directory = clipstore.write_clip(tmp_path, _clip_entry())
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE,
            transcript={"based_on": editorial.checksum([{"start": 0.0, "end": 1.0,
                                                         "text": " old"}]),
                        "words": [{"start": 0.0, "end": 1.0, "text": " corrected"}]}))
        notes: list[str] = []

        provider = _provider(
            directory, captioning_config,
            [{"start": 0.0, "end": 1.0, "text": " different"}], monkeypatch,
            on_note=notes.append)
        provider("unused-raw-path.mp4")

        assert any("conflict" in note for note in notes), notes

    def test_a_dropped_segment_reaches_the_callers_channels_not_stderr(
            self, tmp_path, captioning_config, monkeypatch):
        """transcribe()'s own NOTEs were the last ones left on stderr.

        A render that discards segments above NO_SPEECH_PROB_THRESHOLD keeps
        the clip and renders it - it just has no captions for those seconds.
        That is precisely the "succeeded, but not the way you think" outcome
        this module exists to report, and it used to print to a stderr the
        studio's background thread has no reader for. Measured on a real
        clip before this was wired: three quarters of the captions gone, the
        job log saying "done" and nothing else.

        Stubbing transcribe here would prove nothing - the note originates
        INSIDE it - so this drives the real function with a fake decoder,
        and asserts the note comes out of make_subtitle_provider's own
        channels."""
        directory = clipstore.write_clip(tmp_path, _clip_entry())
        notes: list[str] = []

        def fake_transcribe(video, cache, *a, on_note=None, **kw):
            assert on_note is not None, "the pipeline must hand transcribe a reporter"
            on_note("Speedy!: dropped 4 segment(s) above no_speech_prob "
                    "threshold 0.95 - those seconds have no captions")
            return [{"start": 0.0, "end": 1.0, "text": " hi"}]

        monkeypatch.setattr(subtitle_pipeline, "transcribe", fake_transcribe)
        edit = editorial.load(directory)
        provider = subtitle_pipeline.make_subtitle_provider(
            directory, edit, clipstore.read_clip(directory), "Speedy!",
            captioning_config, on_note=notes.append)
        provider("unused-raw-path.mp4")

        assert any("dropped 4 segment" in note for note in notes), notes

    def test_a_caller_that_wants_no_callback_still_gets_a_logged_note(
            self, tmp_path, captioning_config, monkeypatch, caplog):
        """on_note is optional. The CLI passes none and relies on the logger,
        which logsetup puts on stdout when attached to a TTY - so the terminal
        output an operator has always seen is unchanged."""
        directory = clipstore.write_clip(tmp_path, _clip_entry())

        provider = _provider(directory, captioning_config, [], monkeypatch)
        with caplog.at_level(logging.WARNING, logger="ytshorts.subtitles"):
            assert provider("unused-raw-path.mp4") is None

        assert any("no speech" in record.message for record in caplog.records)

    def test_an_injected_logger_receives_the_note(
            self, tmp_path, captioning_config, monkeypatch):
        """The studio hands in the JOB's own logger, so a lost caption track
        lands in logs/jobs/render-<id>.log rather than in a stderr nobody is
        reading - the gap this module's reporting exists to close."""
        directory = clipstore.write_clip(tmp_path, _clip_entry())
        received: list[str] = []

        class Recorder(logging.Logger):
            def warning(self, message, *args, **kwargs):
                received.append(message % args if args else message)

        provider = _provider(directory, captioning_config, [], monkeypatch,
                             logger=Recorder("test-job"))
        provider("unused-raw-path.mp4")

        assert any("no speech" in message for message in received), received
