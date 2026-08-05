import subprocess
from pathlib import Path

import pytest

from yt_shorts.captions import Caption
from yt_shorts.profile import load
from yt_shorts.subtitle_track import build_track


@pytest.fixture
def config():
    return load("erf/community-clips-back-catalogue").config


def probe(path, entries):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestBuildTrack:
    def test_no_captions_yields_no_track(self, config, tmp_path):
        assert build_track([], config, str(tmp_path / "t.mov"), str(tmp_path)) is None

    def test_track_has_an_alpha_channel(self, config, tmp_path):
        target = tmp_path / "t.mov"
        build_track([Caption(0.0, 1.0, "one")], config, str(target), str(tmp_path))
        assert probe(target, "stream=pix_fmt").startswith("argb")

    def test_track_has_the_output_dimensions(self, config, tmp_path):
        target = tmp_path / "t.mov"
        build_track([Caption(0.0, 1.0, "one")], config, str(target), str(tmp_path))
        assert probe(target, "stream=width,height") == "1080,1920"

    def test_track_runs_until_the_last_caption_ends(self, config, tmp_path):
        target = tmp_path / "t.mov"
        build_track([Caption(0.0, 1.0, "one"), Caption(2.0, 3.5, "two")],
                    config, str(target), str(tmp_path))
        assert float(probe(target, "format=duration")) == pytest.approx(3.5, abs=0.15)

    def test_a_leading_gap_is_filled_with_transparency(self, config, tmp_path):
        """A caption starting at 2.0s must not shift to 0.0s."""
        target = tmp_path / "t.mov"
        build_track([Caption(2.0, 3.0, "late")], config, str(target), str(tmp_path))
        assert float(probe(target, "format=duration")) == pytest.approx(3.0, abs=0.15)


class TestBuildTrackValidation:
    """`build_track` only ever received sorted, non-overlapping input from
    `captions.group_words` so far. These pin the guard added for callers
    that cannot make that guarantee (a future batch loop, a later stage
    feeding candidate ranges) - malformed input must be rejected with a
    ValueError, not silently mistimed."""

    def test_end_before_start_is_rejected(self, config, tmp_path):
        with pytest.raises(ValueError, match="caption 0"):
            build_track([Caption(2.0, 1.0, "backwards")], config,
                        str(tmp_path / "t.mov"), str(tmp_path))

    def test_unsorted_captions_are_rejected(self, config, tmp_path):
        with pytest.raises(ValueError, match="caption 1"):
            build_track([Caption(3.0, 4.0, "second"), Caption(1.0, 2.0, "first")],
                        config, str(tmp_path / "t.mov"), str(tmp_path))

    def test_overlapping_captions_are_rejected(self, config, tmp_path):
        with pytest.raises(ValueError, match="caption 1"):
            build_track([Caption(1.0, 2.0, "a"), Caption(1.0, 2.0, "b")],
                        config, str(tmp_path / "t.mov"), str(tmp_path))

    def test_all_defects_are_reported_together(self, config, tmp_path):
        """Two independent defects in one list must both appear in a single
        ValueError, not just the first one encountered."""
        captions = [
            Caption(2.0, 1.0, "backwards"),   # defect: end before start
            Caption(0.5, 3.0, "overlaps"),    # defect: starts before caption 0's end
        ]
        with pytest.raises(ValueError) as excinfo:
            build_track(captions, config, str(tmp_path / "t.mov"), str(tmp_path))
        message = str(excinfo.value)
        assert "2 problem" in message
        assert "caption 0" in message
        assert "caption 1" in message

    def test_equal_start_and_end_is_accepted(self, config, tmp_path):
        """A single-frame flash (end == start) is legal - Whisper can emit
        a word whose start and end are identical."""
        target = tmp_path / "t.mov"
        build_track(
            [Caption(0.0, 1.0, "a"), Caption(1.0, 1.0, "flash"), Caption(1.0, 2.0, "b")],
            config, str(target), str(tmp_path),
        )
        assert target.exists()
        assert float(probe(target, "format=duration")) == pytest.approx(2.0, abs=0.15)

    def test_invalid_input_leaves_no_work_dir_output(self, config, tmp_path):
        """Validation must happen before anything is written, not just
        before the ffmpeg call."""
        work_dir = tmp_path / "work"
        with pytest.raises(ValueError):
            build_track([Caption(2.0, 1.0, "backwards")], config,
                        str(tmp_path / "t.mov"), str(work_dir))
        assert not work_dir.exists()


class TestBuildTrackCleanup:
    """Mirrors render.build_short's convention: work_dir is scratch space
    for a single call, not a cache. On success it must end up holding only
    what the caller didn't ask this function to manage; on failure the
    intermediates must survive for troubleshooting."""

    def test_no_temp_files_remain_after_a_successful_build(self, config, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        target = tmp_path / "t.mov"

        build_track([Caption(0.0, 1.0, "one"), Caption(2.0, 3.5, "two")],
                    config, str(target), str(work_dir))

        assert target.exists()
        assert list(work_dir.glob("*")) == [], "PNGs and captions.txt must be removed on success"

    def test_temp_files_remain_on_failure_for_troubleshooting(self, config, tmp_path, monkeypatch):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        target = tmp_path / "t.mov"

        real_run = subprocess.run

        def _fail_ffmpeg(command, *args, **kwargs):
            if Path(command[0]).name == "ffmpeg" or command[0] == "ffmpeg":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="forced failure")
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr("yt_shorts.subtitle_track.subprocess.run", _fail_ffmpeg)

        with pytest.raises(RuntimeError):
            build_track([Caption(0.0, 1.0, "one")], config, str(target), str(work_dir))

        temp_files = list(work_dir.glob("*"))
        assert temp_files, "temp files should remain for troubleshooting on failure"
        assert not target.exists()

    def test_a_failed_call_does_not_leak_into_a_later_successful_call(
        self, config, tmp_path, monkeypatch
    ):
        """Reproduction: a failed call with more captions (more timeline
        entries, more PNGs) leaves its intermediates behind, as documented.
        A later successful call on the SAME work_dir with fewer captions
        must still end up with none of its own PNGs, none of captions.txt,
        AND none of the stale PNGs the failed call left behind - "delete
        only what I created" cannot achieve that when the caption count
        drops between calls."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        target = tmp_path / "t.mov"

        real_run = subprocess.run

        def _fail_ffmpeg(command, *args, **kwargs):
            if Path(command[0]).name == "ffmpeg" or command[0] == "ffmpeg":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="forced failure")
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr("yt_shorts.subtitle_track.subprocess.run", _fail_ffmpeg)

        # 3 captions, no leading gap, a gap between each -> 5 timeline entries
        # (cap, gap, cap, gap, cap) -> caption-0000..0004.png.
        with pytest.raises(RuntimeError):
            build_track(
                [Caption(0.0, 1.0, "a"), Caption(2.0, 3.0, "b"), Caption(4.0, 5.0, "c")],
                config, str(target), str(work_dir),
            )
        leftovers = sorted(p.name for p in work_dir.glob("caption-*.png"))
        assert leftovers == [f"caption-{i:04d}.png" for i in range(5)]

        # A file this function does not write must survive both calls.
        sentinel = work_dir / "not-mine.txt"
        sentinel.write_text("unrelated scratch user", encoding="utf-8")

        monkeypatch.undo()

        # 1 caption, real ffmpeg, succeeding.
        build_track([Caption(0.0, 1.0, "only")], config, str(target), str(work_dir))

        assert target.exists()
        assert list(work_dir.glob("caption-*.png")) == []
        assert not (work_dir / "captions.txt").exists()
        assert sentinel.exists()

    def test_cleanup_can_be_disabled(self, config, tmp_path):
        """The flag exists so a caller investigating a downstream problem
        can opt out of cleanup even on success; unused it defaults to True
        (exercised by test_no_temp_files_remain_after_a_successful_build)."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        target = tmp_path / "t.mov"

        build_track([Caption(0.0, 1.0, "one")], config, str(target), str(work_dir),
                    cleanup_temp_files=False)

        temp_files = list(work_dir.glob("*"))
        assert temp_files, "temp files should remain when cleanup is disabled"
