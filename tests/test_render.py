import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from yt_shorts.cancel import CancelToken, Stopped
from yt_shorts.profile import load as profile_load
from yt_shorts.overlay import build_overlay
from yt_shorts.render import Source, compose, build_short, ytdlp_command


class TestYtdlpCommand:
    def test_clip_url_is_loaded_directly(self):
        b = ytdlp_command(Source(clip_url="https://www.youtube.com/clip/ABC"), "/tmp/x.mp4")
        assert "https://www.youtube.com/clip/ABC" in b
        assert "--download-sections" not in b

    def test_video_with_time_range_uses_download_sections(self):
        b = ytdlp_command(Source(video_id="Esm9vv5-PdU", start=100.0, end=120.0), "/tmp/x.mp4")
        assert "--download-sections" in b
        assert "*100.0-120.0" in b
        assert "https://www.youtube.com/watch?v=Esm9vv5-PdU" in b

    def test_height_is_capped_at_1080(self):
        b = ytdlp_command(Source(clip_url="https://www.youtube.com/clip/ABC"), "/tmp/x.mp4")
        assert any("height<=1080" in part for part in b)

    def test_clip_url_follows_a_double_dash_terminator(self):
        b = ytdlp_command(Source(clip_url="https://www.youtube.com/clip/ABC"), "/tmp/x.mp4")
        assert b[b.index("https://www.youtube.com/clip/ABC") - 1] == "--"

    @pytest.mark.parametrize("bad", [
        "file:///etc/passwd", "--exec=touch /tmp/pwn", "-rm", "ftp://h/x", "not a url"])
    def test_non_http_clip_url_is_rejected(self, bad):
        with pytest.raises(ValueError, match="http"):
            ytdlp_command(Source(clip_url=bad), "/tmp/x.mp4")

    def test_without_source_raises(self):
        with pytest.raises(ValueError):
            ytdlp_command(Source(), "/tmp/x.mp4")

    def test_video_without_time_range_raises(self):
        with pytest.raises(ValueError):
            ytdlp_command(Source(video_id="Esm9vv5-PdU"), "/tmp/x.mp4")


def _test_video(path: Path, seconds: int = 3) -> None:
    """Creates a 1280x720 test video with audio — replaces the network download."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def _dimensions(path: Path) -> tuple[int, int]:
    out = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
    ], capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


class TestCompose:
    def test_result_is_portrait_with_audio(self, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "raw.mp4"
        _test_video(raw)
        overlay = tmp_path / "overlay.png"
        build_overlay("BOX THIS LAP", "ERF | @ERFofficial", config).save(overlay)
        target = tmp_path / "short.mp4"

        compose(str(raw), str(overlay), str(target), config)

        assert target.exists() and target.stat().st_size > 10_000
        assert _dimensions(target) == (1080, 1920)
        tracks = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
            "-of", "csv=p=0", str(target),
        ], capture_output=True, text=True, check=True).stdout
        assert "audio" in tracks, "Audio got lost"

    def test_silent_source_material_does_not_abort(self, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "silent.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", "testsrc=size=1280x720:rate=30:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw),
        ], check=True)
        overlay = tmp_path / "overlay.png"
        build_overlay("STILL", "ERF", config).save(overlay)
        target = tmp_path / "short.mp4"

        compose(str(raw), str(overlay), str(target), config)

        assert _dimensions(target) == (1080, 1920)


class TestComposeIsAtomic:
    """short.mp4 must never exist in a half-written state.

    The studio reports `has_short` - and a `short_version` cache token - from
    nothing but the file's presence and its stat(), so a target that exists
    while ffmpeg is still writing it is a file the studio will serve: an
    `immutable` cache policy handed out for partial bytes, and a "Download
    short" button that hands a manual-upload operator a truncated video to
    upload to YouTube by hand. A render takes minutes, so this window is wide
    open in practice, not theoretical.
    """

    def test_the_target_appears_only_once_the_render_is_complete(
            self, monkeypatch, tmp_path):
        """Observes the ffmpeg invocation itself: ffmpeg must be told to write
        somewhere OTHER than the target, and the target must still not exist
        when ffmpeg returns - it may only appear afterwards, by the move. This
        is the mechanism, not a proxy for it."""
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "raw.mp4"
        _test_video(raw)
        overlay = tmp_path / "overlay.png"
        build_overlay("ATOMIC", "ERF", config).save(overlay)
        target = tmp_path / "short.mp4"

        observed = {}
        real_run = subprocess.run

        def watching_run(command, *args, **kwargs):
            if Path(command[0]).name == "ffmpeg":
                observed["output_argument"] = command[-1]
                result = real_run(command, *args, **kwargs)
                observed["target_after_ffmpeg"] = target.exists()
                return result
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", watching_run)
        compose(str(raw), str(overlay), str(target), config)

        assert observed["output_argument"] != str(target), (
            "ffmpeg was told to write straight to the target, so the target "
            "exists in a partial state for the whole render"
        )
        assert observed["target_after_ffmpeg"] is False, (
            "the target existed before the move - it was not written aside"
        )
        assert target.exists(), "the finished file was never moved into place"
        assert _dimensions(target) == (1080, 1920)

    def test_the_extension_stays_mp4_so_ffmpeg_still_picks_its_muxer(
            self, monkeypatch, tmp_path):
        """ffmpeg infers the container from the output's extension. A scratch
        name ending in something it does not know (`short.mp4.part`) makes it
        refuse to write at all, which is why the suffix goes BEFORE `.mp4`.
        Pinned because the failure would be a broken render, not a broken
        test."""
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "raw.mp4"
        _test_video(raw)
        overlay = tmp_path / "overlay.png"
        build_overlay("ATOMIC", "ERF", config).save(overlay)
        target = tmp_path / "short.mp4"

        seen = {}
        real_run = subprocess.run

        def watching_run(command, *args, **kwargs):
            if Path(command[0]).name == "ffmpeg":
                seen["output_argument"] = command[-1]
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", watching_run)
        compose(str(raw), str(overlay), str(target), config)

        assert seen["output_argument"].endswith(".mp4")

    def test_a_render_that_fails_after_writing_cleans_up_its_scratch_file(
            self, monkeypatch, tmp_path):
        """The cleanup pinned deterministically, with ffmpeg stubbed.

        The real-ffmpeg failure below cannot pin this: given unreadable input,
        ffmpeg fails while probing and never opens its output, so no scratch
        file is created either way and the assertion passes with or without the
        cleanup. A failure PART WAY THROUGH an encode does leave one - a full
        disk, a killed process, the subprocess timeout - and that is the case
        that would otherwise litter every clip directory with a half-written
        `short.part.mp4` nobody ever removes.
        """
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "raw.mp4"
        _test_video(raw)
        overlay = tmp_path / "overlay.png"
        build_overlay("ATOMIC", "ERF", config).save(overlay)
        target = tmp_path / "short.mp4"

        def failing_run(command, *args, **kwargs):
            if Path(command[0]).name == "ffmpeg":
                Path(command[-1]).write_bytes(b"half an encode")
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="disk full")
            raise AssertionError(f"unexpected subprocess: {command[0]}")

        monkeypatch.setattr(subprocess, "run", failing_run)
        with pytest.raises(RuntimeError):
            compose(str(raw), str(overlay), str(target), config)

        assert not target.exists(), "a failed render must not produce a target"
        leftovers = [p.name for p in tmp_path.iterdir() if "part" in p.name]
        assert leftovers == [], f"a failed render left scratch files behind: {leftovers}"

    def test_a_failed_render_leaves_nothing_behind(self, tmp_path):
        """Real ffmpeg, real failure. Weaker than the stubbed test above by
        itself - see its docstring - but it pins the real path end to end."""
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "raw.mp4"
        raw.write_bytes(b"not real video material")
        overlay = tmp_path / "overlay.png"
        build_overlay("ATOMIC", "ERF", config).save(overlay)
        target = tmp_path / "short.mp4"

        with pytest.raises(RuntimeError):
            compose(str(raw), str(overlay), str(target), config)

        assert not target.exists()
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("short")]
        assert leftovers == [], f"a failed render left files behind: {leftovers}"

    def test_an_existing_short_survives_a_failed_re_render(self, tmp_path):
        """The consequence that matters to an operator: re-rendering a clip
        whose render then fails must leave the PREVIOUS short intact and
        playable, rather than destroying it the moment ffmpeg opened the
        output with -y."""
        config = profile_load("erf/community-clips-back-catalogue").config
        good_raw = tmp_path / "raw.mp4"
        _test_video(good_raw)
        overlay = tmp_path / "overlay.png"
        build_overlay("ATOMIC", "ERF", config).save(overlay)
        target = tmp_path / "short.mp4"
        compose(str(good_raw), str(overlay), str(target), config)
        first = target.read_bytes()

        broken_raw = tmp_path / "broken.mp4"
        broken_raw.write_bytes(b"not real video material")
        with pytest.raises(RuntimeError):
            compose(str(broken_raw), str(overlay), str(target), config)

        assert target.read_bytes() == first, "the previous short was destroyed"
        assert _dimensions(target) == (1080, 1920)


class _StubEncodingChild:
    """A child process that writes its (scratch) output and then hangs until
    signalled - the shape a real ffmpeg mid-encode takes. It has to WRITE
    before hanging: a stub that never produced a scratch file would make
    "the scratch is gone afterwards" vacuously true - the same trap
    tests/test_trim.py's own StubChild exists to avoid."""

    def __init__(self) -> None:
        self.returncode = None
        self.terminated = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        # Real subprocess.Popen.__exit__ waits for the child on the way out
        # (after closing its pipes); mirrored here so that reverting the
        # fix to a literal `subprocess.run(...)` - which always drives its
        # Popen as a context manager - exercises the SAME code path this
        # stub is built for, rather than failing on the `with` statement
        # itself before the mutation under test is even reached.
        self.wait()
        return False

    def communicate(self, input=None, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("stub", timeout or 0)
        return ("", "")

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("stub", timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def poll(self):
        return self.returncode


class TestComposeHardStop:
    """Task 3 flagged this as the one gap left in KINDS["render"]'s
    hard_stop_allowed=True promise: render.py called subprocess.run
    directly, so its ffmpeg child was unreachable from a CancelToken and a
    render's "cancel now" button behaved like a plain (cooperative-only)
    stop. compose() now forwards cancel into cancel.run_cancellable, the
    same mechanism trim._default_runner already uses - this pins that a
    HARD stop mid-encode still leaves TestComposeIsAtomic's safety property
    intact: no scratch file survives, and an existing short.mp4 is
    untouched (compose never even reaches the replace() that would
    overwrite it)."""

    def test_a_hard_stop_mid_encode_leaves_no_scratch_and_keeps_the_existing_short(
            self, monkeypatch, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "raw.mp4"
        _test_video(raw)
        overlay = tmp_path / "overlay.png"
        build_overlay("ATOMIC", "ERF", config).save(overlay)
        target = tmp_path / "short.mp4"
        target.write_bytes(b"PREVIOUS SHORT")  # a short from an earlier render
        token = CancelToken()
        children: list[_StubEncodingChild] = []

        def fake_popen(cmd, **kwargs):
            # ffmpeg has begun writing its scratch output - and THIS is when
            # the operator clicks "cancel now". A kill requested before the
            # call would never reach a subprocess at all.
            Path(cmd[-1]).write_bytes(b"HALF-ENCODED")
            token.request_kill()
            child = _StubEncodingChild()
            children.append(child)
            return child

        import yt_shorts.cancel as cancel_module
        monkeypatch.setattr(cancel_module.subprocess, "Popen", fake_popen)

        with pytest.raises(Stopped):
            compose(str(raw), str(overlay), str(target), config, cancel=token)

        assert children and children[0].terminated == 1, (
            "the ffmpeg child was never terminated"
        )
        scratch = target.with_name("short.part.mp4")
        assert not scratch.exists(), "the half-written encode survived the kill"
        assert target.read_bytes() == b"PREVIOUS SHORT", (
            "the previous short was destroyed by an encode that never finished"
        )

    def test_without_a_token_compose_is_unchanged(self, tmp_path):
        # cancel=None must behave byte-for-byte as before this parameter
        # existed - the same guarantee run_cancellable itself makes.
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "raw.mp4"
        _test_video(raw)
        overlay = tmp_path / "overlay.png"
        build_overlay("ATOMIC", "ERF", config).save(overlay)
        target = tmp_path / "short.mp4"

        compose(str(raw), str(overlay), str(target), config)

        assert target.exists()
        assert _dimensions(target) == (1080, 1920)


def _marker_video(path: Path, width: int, height: int, frame: int | None = None, seconds: float = 1.0) -> None:
    """Creates a test video: a blue frame around a red area, both highly
    saturated. The frame traces the full outer contour of the source image,
    so its actual position and size in the composed result can be found and
    measured. The frame is deliberately kept thin (~3% of the shorter
    edge): a thick frame would otherwise noticeably tint even the blurred
    background itself blue (it is, after all, made from the small-scaled,
    blurred source image) and would destroy the contrast the measurement
    relies on."""
    if frame is None:
        frame = max(12, round(min(width, height) * 0.03))
    inner_width = width - 2 * frame
    inner_height = height - 2 * frame
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=blue:s={width}x{height}:d={seconds}",
        "-vf", f"drawbox=x={frame}:y={frame}:w={inner_width}:h={inner_height}:color=red:t=fill",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)


def _single_frame(path: Path, target_png: Path, timestamp: float = 0.5) -> Image.Image:
    """Extracts a single frame as PNG and loads it with Pillow."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-ss", str(timestamp),
        "-i", str(path), "-frames:v", "1", str(target_png),
    ], check=True)
    return Image.open(target_png).convert("RGB")


def _sharp_image_area(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Finds the bounding box of the sharp blue frame in the composed
    image: distinct blue (b>90 with simultaneously r<80 and g<80). Outside
    the video window the brand overlay is only semi-transparent (see
    overlay.py), so a sharp image breaking through there gets blended with
    the band's tint instead of being covered — the threshold sits at 90
    instead of nearer the full 255, to still catch exactly this diluted
    breakthrough (measured: pure blue from the source still arrives there
    at over 120). The blurred background, on the other hand (scaled down
    small, boxblur'd, stretched to fill the frame, possibly additionally
    band-tinted afterwards), stays clearly below that with a thin frame
    (see _marker_video) — measured: under 55. The bounding box of all
    pixels marked this way corresponds to the outer contour of the sharp,
    fitted source image in the target — whether it lies within the window
    or (in the failure case) extends beyond it."""
    r, g, b = image.split()
    blue = b.point(lambda v: 255 if v > 90 else 0)
    low_red = r.point(lambda v: 255 if v < 80 else 0)
    low_green = g.point(lambda v: 255 if v < 80 else 0)
    mask = ImageChops.multiply(ImageChops.multiply(blue, low_red), low_green)
    return mask.getbbox()


TOLERANCE = 6  # pixels of slack for scaling rounding and H.264 edge blur


class TestWindowAlignment:
    """The sharp source image must land fully within the video window
    (video_y to video_y+video_height) regardless of the source's aspect
    ratio, and sit centered there. A marker source image (blue frame around
    a red area, see _marker_video) makes the position and size of the
    sharp area in the result measurable, without having to reproduce
    ffmpeg's internal rounding: instead of precomputing an expected value,
    the actual sharp area is measured and checked against the properties a
    correct fit must have (contained, aspect-ratio-preserving, maximal,
    centered)."""

    def _check(self, tmp_path, source_width, source_height):
        config = profile_load("erf/community-clips-back-catalogue").config
        a = config["output"]
        raw = tmp_path / "raw.mp4"
        _marker_video(raw, source_width, source_height)
        overlay = tmp_path / "overlay.png"
        build_overlay("HOOK TEXT", "ERF | @ERFofficial", config).save(overlay)
        target = tmp_path / "short.mp4"

        compose(str(raw), str(overlay), str(target), config)

        image = _single_frame(target, tmp_path / "frame.png")
        box = _sharp_image_area(image)
        assert box is not None, "no sharp image area found in the result"
        x0, y0, x1, y1 = box

        window_top = a["video_y"]
        window_bottom = a["video_y"] + a["video_height"]

        # Fully within the window: nothing visible above or below, and not
        # wider than the canvas.
        assert y0 >= window_top - TOLERANCE, f"sharp area extends past the top window edge: y0={y0}"
        assert y1 <= window_bottom + TOLERANCE, f"sharp area extends past the bottom window edge: y1={y1}"
        assert x0 >= -TOLERANCE
        assert x1 <= a["width"] + TOLERANCE

        # Source aspect ratio is preserved: no distortion, no cropping.
        expected_ratio = source_width / source_height
        measured_ratio = (x1 - x0) / (y1 - y0)
        assert measured_ratio == pytest.approx(expected_ratio, rel=0.03)

        # Fitted as large as possible: at least one axis touches the window.
        width_fits = abs((x1 - x0) - a["video_width"]) <= TOLERANCE
        height_fits = abs((y1 - y0) - a["video_height"]) <= TOLERANCE
        assert width_fits or height_fits, "fitted image does not use the window to the maximum"

        # Centered: margins left/right resp. top/bottom are equal.
        left, right = x0, a["width"] - x1
        top, bottom = y0 - window_top, window_bottom - y1
        assert left == pytest.approx(right, abs=TOLERANCE), f"not centered horizontally: left={left} right={right}"
        assert top == pytest.approx(bottom, abs=TOLERANCE), f"not centered vertically: top={top} bottom={bottom}"

        return x0, y0, x1, y1

    def test_4_3_fits_fully_into_the_window_and_sits_centered(self, tmp_path):
        # Finding before the fix: scaled image becomes 810 px tall instead
        # of 608 and reaches down to y=1410 instead of y=1208 (broken
        # bottom frame).
        x0, y0, x1, y1 = self._check(tmp_path, 640, 480)
        assert y1 <= 600 + 608 + TOLERANCE

    def test_21_9_fits_fully_into_the_window_and_sits_centered(self, tmp_path):
        # Finding before the fix: only 456 px tall, 152 px gap only at the
        # bottom (no centering, edge without image contact).
        x0, y0, x1, y1 = self._check(tmp_path, 2560, 1080)
        assert (y0 - 600) == pytest.approx(1208 - y1, abs=TOLERANCE)

    def test_16_9_stays_unchanged_at_1080x608_and_y600(self, tmp_path):
        # 16:9 already hit the 608 px almost exactly before: must not change.
        x0, y0, x1, y1 = self._check(tmp_path, 1280, 720)
        assert x0 == pytest.approx(0, abs=TOLERANCE)
        assert x1 == pytest.approx(1080, abs=TOLERANCE)
        assert y0 == pytest.approx(600, abs=TOLERANCE)
        assert y1 == pytest.approx(1208, abs=TOLERANCE)


def _test_video_aspect_ratio(path: Path, width: int, height: int, seconds: float = 1.0) -> None:
    """Like _test_video(), but with freely chosen source dimensions — for
    the output-contract test, which has to run through several aspect
    ratios."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def _ffprobe_streams(path: Path) -> list[dict]:
    """Returns all stream info (incl. sample_aspect_ratio/display_aspect_ratio,
    codec_name, pix_fmt) as parsed JSON — the width/height check alone
    wouldn't have caught this bug (see finding), so deliberately the full
    set of fields here instead of -show_entries with a selection."""
    out = subprocess.run([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path),
    ], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


def _atom_positions(path: Path) -> tuple[int, int]:
    """Returns the byte position of the first 'moov' and 'mdat' atom tag in
    the file. For +faststart, moov must come before mdat (the player needs
    the metadata before the image data has finished streaming). The atom
    tag is a 4-byte ASCII tag in the MP4 container; a simple byte search
    for it is enough, because both tags occur exactly once each at the top
    level in a file produced by compose()."""
    data = Path(path).read_bytes()
    return data.find(b"moov"), data.find(b"mdat")


class TestOutputContract:
    """Checks the full contract of the output file, not just width/height.
    Finding: the previous tests only measured width/height, which were
    already correct before the fix (1080x1920) — the non-square pixel
    aspect ratio (SAR) went undetected that way, because ffmpeg sets it
    counteracting the distortion in the background branch, to end up with
    DAR 16:9 (instead of 9:16!). Even a visual check via an extracted
    single frame would have been blind to it: a single frame carries no
    SAR, so it looks correct either way. Hence ffprobe on the full stream
    dataset here, and a real byte search for the moov/mdat atoms for
    +faststart."""

    @pytest.mark.parametrize("source_width,source_height", [
        (1280, 720),   # 16:9
        (640, 480),    # 4:3
        (2560, 1080),  # 21:9
    ])
    def test_full_output_contract_across_aspect_ratios(self, tmp_path, source_width, source_height):
        config = profile_load("erf/community-clips-back-catalogue").config
        raw = tmp_path / "raw.mp4"
        _test_video_aspect_ratio(raw, source_width, source_height)
        overlay = tmp_path / "overlay.png"
        build_overlay("HOOK TEXT", "ERF | @ERFofficial", config).save(overlay)
        target = tmp_path / "short.mp4"

        compose(str(raw), str(overlay), str(target), config)

        streams = _ffprobe_streams(target)
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next((s for s in streams if s["codec_type"] == "audio"), None)

        assert (video["width"], video["height"]) == (1080, 1920)
        assert video.get("sample_aspect_ratio") == "1:1", (
            f"SAR must be square, is {video.get('sample_aspect_ratio')} "
            f"(source {source_width}x{source_height})"
        )
        assert video.get("display_aspect_ratio") == "9:16", (
            f"DAR must be 9:16, is {video.get('display_aspect_ratio')} "
            f"(source {source_width}x{source_height})"
        )
        assert video["pix_fmt"] == "yuv420p"
        assert video["codec_name"] == "h264"
        assert audio is not None, "Audio got lost"
        assert audio["codec_name"] == "aac"

        moov, mdat = _atom_positions(target)
        assert moov != -1 and mdat != -1, "moov or mdat atom not found"
        assert moov < mdat, "moov atom is not located before the mdat atom (+faststart missing)"


def _ytdlp_stub(calls: list, source_video: Path):
    """Replaces subprocess.run within yt_shorts.render: the yt-dlp call is
    intercepted by the stub (no real downloads needed), an ffmpeg call
    (from compose()) is passed through unchanged to the real subprocess.run,
    so compose() runs for real."""
    real_run = subprocess.run

    def _stub(command, *args, **kwargs):
        if Path(command[0]).name == "yt-dlp":
            calls.append(command)
            target = Path(command[command.index("-o") + 1])
            assert not target.exists(), (
                "raw file already existed before the (re)load - it should "
                "have been removed beforehand, otherwise the run composes "
                "from stale material"
            )
            shutil.copyfile(source_video, target)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, *args, **kwargs)

    return _stub


def _ytdlp_stub_returns_broken_material(calls: list):
    """Like _ytdlp_stub, but the 'loaded' file is not a valid video -
    ffmpeg (real, unstubbed) must fail on it in compose()."""
    real_run = subprocess.run

    def _stub(command, *args, **kwargs):
        if Path(command[0]).name == "yt-dlp":
            calls.append(command)
            target = Path(command[command.index("-o") + 1])
            target.write_bytes(b"not real video material")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, *args, **kwargs)

    return _stub


class TestBuildShortRepeatedRun:
    """W3: yt-dlp does not reload if the target file already exists ('has
    already been downloaded', return value 0). build_short() previously
    only checked returncode == 0 and raw.exists() and then composed from
    the (possibly stale) file. The raw/ work folder was never cleaned up
    and remained a silent scratch space forever."""

    def test_existing_raw_file_is_not_reused(self, monkeypatch, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        source_video = tmp_path / "source.mp4"
        _test_video(source_video)
        work_dir = tmp_path / "raw"
        work_dir.mkdir()
        target = tmp_path / "shortname.mp4"
        # Stale raw file already sitting there, apparently from an earlier
        # run - or (finding) under the wrong name due to a swapped
        # collision order.
        (work_dir / "shortname.raw.mp4").write_bytes(b"old broken raw file, must never be used")

        calls: list = []
        monkeypatch.setattr("yt_shorts.render.subprocess.run", _ytdlp_stub(calls, source_video))

        build_short(Source(clip_url="https://example.invalid/clip/x"), "HOOK TEXT",
                    "ERF | @ERFofficial", str(target), config, str(work_dir))

        assert len(calls) == 1, "yt-dlp should have been called (again)"
        assert target.exists() and target.stat().st_size > 10_000

    def test_no_temp_files_remain_after_a_successful_build(self, monkeypatch, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        source_video = tmp_path / "source.mp4"
        _test_video(source_video)
        work_dir = tmp_path / "raw"
        work_dir.mkdir()
        target = tmp_path / "shortname.mp4"

        calls: list = []
        monkeypatch.setattr("yt_shorts.render.subprocess.run", _ytdlp_stub(calls, source_video))

        build_short(Source(clip_url="https://example.invalid/clip/x"), "HOOK TEXT",
                    "ERF | @ERFofficial", str(target), config, str(work_dir))

        temp_files = list(work_dir.glob("*"))
        assert temp_files == [], f"Temp files not cleaned up: {temp_files}"

    def test_temp_files_remain_on_failure_for_troubleshooting(self, monkeypatch, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        work_dir = tmp_path / "raw"
        work_dir.mkdir()
        target = tmp_path / "shortname.mp4"

        calls: list = []
        monkeypatch.setattr("yt_shorts.render.subprocess.run", _ytdlp_stub_returns_broken_material(calls))

        with pytest.raises(RuntimeError):
            build_short(Source(clip_url="https://example.invalid/clip/x"), "HOOK TEXT",
                        "ERF | @ERFofficial", str(target), config, str(work_dir))

        temp_files = list(work_dir.glob("*"))
        assert temp_files, "temp files should remain for troubleshooting on failure"


class TestKeepRaw:
    """Stage C's preview needs a clean, caption-free frame - short.mp4
    already has the brand overlay (and, when subtitles are on, captions)
    burned in, so it must be built from the raw download instead. That
    only works if the raw file survives a successful build somewhere
    findable: build_short's keep_raw flag renames it (not copies it) to
    a fixed raw.mp4 in work_dir, the exact name clipstore.raw_path
    expects - see tests/test_clipstore.py's own naming test."""

    def test_raw_survives_with_keep_raw_true(self, monkeypatch, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        source_video = tmp_path / "source.mp4"
        _test_video(source_video)
        work_dir = tmp_path / "raw"
        work_dir.mkdir()
        target = tmp_path / "shortname.mp4"

        calls: list = []
        monkeypatch.setattr("yt_shorts.render.subprocess.run", _ytdlp_stub(calls, source_video))

        build_short(Source(clip_url="https://example.invalid/clip/x"), "HOOK TEXT",
                    "ERF | @ERFofficial", str(target), config, str(work_dir),
                    keep_raw=True)

        kept = work_dir / "raw.mp4"
        assert kept.exists() and kept.stat().st_size > 10_000
        # Nothing else is left lying around under the old scratch name or
        # from the overlay/subtitle track - keep_raw changes only the raw
        # file's fate.
        assert sorted(p.name for p in work_dir.glob("*")) == ["raw.mp4"]

    def test_raw_is_removed_without_keep_raw(self, monkeypatch, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        source_video = tmp_path / "source.mp4"
        _test_video(source_video)
        work_dir = tmp_path / "raw"
        work_dir.mkdir()
        target = tmp_path / "shortname.mp4"

        calls: list = []
        monkeypatch.setattr("yt_shorts.render.subprocess.run", _ytdlp_stub(calls, source_video))

        build_short(Source(clip_url="https://example.invalid/clip/x"), "HOOK TEXT",
                    "ERF | @ERFofficial", str(target), config, str(work_dir))

        assert not (work_dir / "raw.mp4").exists()
        assert list(work_dir.glob("*")) == [], "raw.mp4 must not survive when keep_raw is not set"

    def test_failed_build_leaves_raw_in_place_under_its_scratch_name_even_with_keep_raw(
            self, monkeypatch, tmp_path):
        """A failure must still leave everything exactly as
        test_temp_files_remain_on_failure_for_troubleshooting expects,
        keep_raw or not: keep_raw only changes what happens to a
        SUCCESSFUL build's raw file, never a failed one's."""
        config = profile_load("erf/community-clips-back-catalogue").config
        work_dir = tmp_path / "raw"
        work_dir.mkdir()
        target = tmp_path / "shortname.mp4"

        calls: list = []
        monkeypatch.setattr("yt_shorts.render.subprocess.run", _ytdlp_stub_returns_broken_material(calls))

        with pytest.raises(RuntimeError):
            build_short(Source(clip_url="https://example.invalid/clip/x"), "HOOK TEXT",
                        "ERF | @ERFofficial", str(target), config, str(work_dir),
                        keep_raw=True)

        # Left under its scratch name, not renamed to raw.mp4 - the
        # rename only happens after compose() succeeds.
        assert (work_dir / "shortname.raw.mp4").exists()
        assert not (work_dir / "raw.mp4").exists()


class TestBuildShortForwardsCancelToBothBoundaries:
    """build_short drives two subprocess boundaries - the yt-dlp download
    and compose's own ffmpeg encode - and forwards its `cancel` token to
    BOTH (see build_short's own docstring). A code review found neither
    forwarding site (render.py's build_short -> run_cancellable call, nor
    build_short -> compose's own `cancel=cancel`) had a test proving the
    token actually ARRIVES rather than merely being accepted and dropped;
    mutating either to `cancel=None` left the whole suite green, and the
    yt-dlp half had no coverage of any kind.

    This patches run_cancellable itself - the single choke point both
    boundaries call through - rather than driving a real subprocess, so it
    stays fast and network-free while still observing the exact value each
    call receives. It does not run a real ffmpeg encode (no new real-encode
    test is added here, per this task's constraints)."""

    def test_the_token_reaches_both_the_download_and_the_encode(
            self, monkeypatch, tmp_path):
        config = profile_load("erf/community-clips-back-catalogue").config
        work_dir = tmp_path / "raw"
        work_dir.mkdir()
        target = tmp_path / "shortname.mp4"
        token = CancelToken()
        seen: list = []

        def fake_run_cancellable(cmd, *, timeout, cancel=None, **kwargs):
            seen.append(cancel)
            if cmd[0] == "yt-dlp":
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"stub raw")
            else:
                # compose's ffmpeg command always ends with the scratch
                # ("...part.mp4") output path.
                Path(cmd[-1]).write_bytes(b"stub encoded")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        import yt_shorts.render as render_module
        monkeypatch.setattr(render_module, "run_cancellable", fake_run_cancellable)

        build_short(Source(clip_url="https://example.invalid/clip/x"), "HOOK TEXT",
                    "ERF | @ERFofficial", str(target), config, str(work_dir),
                    cancel=token)

        assert seen == [token, token], (
            "the download and/or the encode did not receive the cancel token "
            f"(saw {seen!r})"
        )


class TestSourceForClip:
    def test_a_video_id_clip_uses_the_effective_window(self):
        from yt_shorts.editorial import Edit
        from yt_shorts.render import source_for_clip, ytdlp_command
        clip = {"url": "https://www.youtube.com/watch/vid/92-104",
                "video_id": "vid", "start": 92.0, "end": 104.0}
        edit = Edit(title=None, status="candidate", transcript=None, window=(95.0, 108.0))
        source = source_for_clip(clip, edit)
        assert source.video_id == "vid"
        assert (source.start, source.end) == (95.0, 108.0)
        cmd = ytdlp_command(source, "/tmp/x.mp4")
        assert "--download-sections" in cmd and "*95.0-108.0" in cmd

    def test_a_clip_without_a_video_id_uses_the_clip_url(self):
        from yt_shorts.editorial import Edit
        from yt_shorts.render import source_for_clip
        clip = {"url": "https://www.youtube.com/clip/ABC"}
        edit = Edit(title=None, status="candidate", transcript=None)
        source = source_for_clip(clip, edit)
        assert source.clip_url == "https://www.youtube.com/clip/ABC"
        assert source.video_id is None
