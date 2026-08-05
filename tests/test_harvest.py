import pytest

from yt_shorts.harvest import harvest, read_metadata

# Trimmed but real yt-dlp response (recorded on 2026-07-20).
REAL_RESPONSE = {
    "id": "UgkxitJNF6LgUyiW_cOEJjtVZNX2DZ7c_SXo",
    "title": "PART ONE | 24 Heures Du Mans Classic 2025",
    "webpage_url": "https://www.youtube.com/clip/UgkxitJNF6LgUyiW_cOEJjtVZNX2DZ7c_SXo",
    "section_start": 19455.34,
    "section_end": 19470.34,
    "duration": 15.0,
    "channel_id": "UCb3S2oA7lANdg5IS0QtF46w",
}


class TestReadMetadata:
    def test_reads_timecodes(self):
        e = read_metadata(REAL_RESPONSE, REAL_RESPONSE["webpage_url"], "rei got sliced")
        assert e.start == 19455.34
        assert e.end == 19470.34
        assert e.duration == 15.0

    def test_hook_comes_from_outside_not_from_ytdlp(self):
        """yt-dlp returns the source video title, not the clip title."""
        e = read_metadata(REAL_RESPONSE, REAL_RESPONSE["webpage_url"], "rei got sliced")
        assert e.hook == "rei got sliced"
        assert e.source_title == "PART ONE | 24 Heures Du Mans Classic 2025"

    def test_missing_section_is_recorded_as_error(self):
        without = {k: v for k, v in REAL_RESPONSE.items() if k != "section_start"}
        e = read_metadata(without, "https://example.invalid/clip/x", "whatever")
        assert e.error is not None
        assert "section_start" in e.error

    def test_duration_is_computed_if_necessary(self):
        without = {k: v for k, v in REAL_RESPONSE.items() if k != "duration"}
        e = read_metadata(without, REAL_RESPONSE["webpage_url"], "whatever")
        assert e.duration == pytest.approx(15.0)

    def test_zero_duration_stays_zero_instead_of_computed(self):
        """duration: 0 is a present value, not a missing key."""
        with_zero_duration = {
            **REAL_RESPONSE,
            "section_start": 5.0,
            "section_end": 20.0,
            "duration": 0.0,
        }
        e = read_metadata(with_zero_duration, REAL_RESPONSE["webpage_url"], "whatever")
        assert e.duration == 0.0
        assert e.error is None

    def test_section_start_none_is_recorded_as_error_not_raised(self):
        broken = {**REAL_RESPONSE, "section_start": None}
        e = read_metadata(broken, REAL_RESPONSE["webpage_url"], "whatever")
        assert e.error is not None

    def test_section_start_text_is_recorded_as_error_not_raised(self):
        broken = {**REAL_RESPONSE, "section_start": "abc"}
        e = read_metadata(broken, REAL_RESPONSE["webpage_url"], "whatever")
        assert e.error is not None


    def test_end_before_start_is_recorded_as_error(self):
        bad = {**REAL_RESPONSE, "section_start": 200.0, "section_end": 100.0}
        e = read_metadata(bad, bad["webpage_url"], "h")
        assert e.error is not None and "not valid" in e.error

    def test_negative_duration_is_recorded_as_error(self):
        bad = {**REAL_RESPONSE, "duration": -5.0}
        e = read_metadata(bad, bad["webpage_url"], "h")
        assert e.error is not None and "not valid" in e.error


class TestHarvest:
    def test_missing_ytdlp_binary_does_not_abort_the_run(self):
        entries = harvest(
            [{"url": "https://example.invalid/clip/x", "hook": "h"}],
            ytdlp="definitely-not-a-real-binary-xyz",
        )
        assert len(entries) == 1
        assert entries[0].error is not None

    def test_missing_url_does_not_abort_the_run(self):
        entries = harvest([{"hook": "h without url"}])
        assert len(entries) == 1
        assert entries[0].error is not None

    @pytest.mark.parametrize("bad", ["--exec=touch /tmp/pwn", "file:///etc/passwd", "-x"])
    def test_a_non_http_url_is_isolated_as_an_error_not_run(self, bad):
        # A '-'-leading or non-http URL must be rejected before reaching yt-dlp
        # (it would otherwise be parsed as an option), isolated per-entry.
        entries = harvest([{"url": bad, "hook": "h"}])
        assert len(entries) == 1
        assert entries[0].error is not None
        assert "http" in entries[0].error
