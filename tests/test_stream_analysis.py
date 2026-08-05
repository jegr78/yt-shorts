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
