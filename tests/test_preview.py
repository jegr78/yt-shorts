"""Tests for yt_shorts.preview.

The correctness question that matters (see preview.py's own docstring) is
whether a caption drawn by preview.build lands where render.compose's real
ffmpeg encode would put it. That can only be answered by building a real
short (raw.mp4 + short.mp4, both produced locally with ffmpeg's testsrc/
color generators, never downloaded) and comparing a frame extracted from
it against preview.build's own output at the same timestamp - an
"invented data" test that stubs out the geometry itself could not fail
for the right reason, per this project's own history (see CLAUDE.md).
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from yt_shorts import clipstore
from yt_shorts.captions import group_words
from yt_shorts.profile import load as profile_load
from yt_shorts.render import Source, build_short
from yt_shorts.subtitle_track import build_track

from yt_shorts import preview
from yt_shorts.overlay import _footer_top
from yt_shorts.preview import PreviewError


@pytest.fixture
def config():
    return profile_load("erf/community-clips-back-catalogue").config


def _solid_video(path: Path, width: int, height: int, color: str,
                  seconds: float = 4.0) -> None:
    """A plain-colored source video, deliberately far from white: the
    verification below detects a caption by its near-white text pixels
    (colors.text is #FFFFFF for the fixture channel - see
    tests/fixtures/channels/erf/brand.json), and testsrc's own bars
    include near-white content that would confuse that detector."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)


def _marker_video(path: Path, width: int, height: int, seconds: float = 4.0) -> None:
    """A blue frame around a red center (same technique as
    tests/test_render.py's own _marker_video): the outer contour of the
    sharp, fitted picture becomes measurable in a composed result, which
    is what lets the window-placement test below catch a geometry bug a
    caption-only comparison would miss - a caption's own position comes
    straight out of config, independent of where the sharp image itself
    got placed, so a wrong window offset would not otherwise show up in
    the caption's bounding box at all. Neither color is anywhere near
    white, so it does not confuse the caption detector either."""
    frame = max(12, round(min(width, height) * 0.03))
    inner_w, inner_h = width - 2 * frame, height - 2 * frame
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=blue:s={width}x{height}:d={seconds}",
        "-vf", f"drawbox=x={frame}:y={frame}:w={inner_w}:h={inner_h}:color=red:t=fill",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)


def _sharp_image_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the SHARP, fitted marker image's blue contour in a
    composed frame - deliberately a tighter threshold than
    test_render.py's own _sharp_image_area (b>90): that threshold was
    calibrated for ffmpeg's real blur/compression characteristics, and
    the two blur implementations here dilute a saturated corner pixel by
    different amounts (measured: a blurred background corner reaches
    b~105-150 in Pillow's own box blur vs. b~54-77 after ffmpeg's, for
    the same marker source and radius) - loose enough to fail this
    test's real purpose (finding the SHARP contour) by also matching
    Pillow's more blue-tinted blurred corners. b>200 with r<50 and g<50
    only matches near-saturated, unblurred blue - measured well over 250
    on the true sharp contour in both images, comfortably above any
    blurred-background pixel in either."""
    from PIL import ImageChops
    r, g, b = image.split()
    blue = b.point(lambda v: 255 if v > 200 else 0)
    low_red = r.point(lambda v: 255 if v < 50 else 0)
    low_green = g.point(lambda v: 255 if v < 50 else 0)
    mask = ImageChops.multiply(ImageChops.multiply(blue, low_red), low_green)
    return mask.getbbox()


def _ytdlp_stub(calls: list, source_video: Path):
    """Same technique as tests/test_render.py's own stub: replaces
    subprocess.run inside yt_shorts.render for the yt-dlp call only, so
    build_short runs its real ffmpeg compose() against a locally-built
    source instead of downloading anything."""
    real_run = subprocess.run

    def _stub(command, *args, **kwargs):
        if Path(command[0]).name == "yt-dlp":
            calls.append(command)
            target = Path(command[command.index("-o") + 1])
            import shutil
            shutil.copyfile(source_video, target)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, *args, **kwargs)

    return _stub


def _build_real_clip(tmp_path: Path, config: dict, words: list[dict],
                      hook: str, footer: str, monkeypatch) -> Path:
    """Builds a real clip directory the way cmd_render would: a locally
    generated marker source video (see _marker_video) stands in for
    yt-dlp's download, build_short runs its real ffmpeg compose()
    (subtitle_provider wires up a real caption track from `words` via the
    same captions.group_words + subtitle_track.build_track cmd_render
    itself uses), and keep_raw=True leaves raw.mp4 behind - exactly what
    preview.build needs. Returns the clip directory (clipstore.raw_path/
    short_path apply to it).
    """
    clip_dir = tmp_path / "clips" / "preview-test"
    clip_dir.mkdir(parents=True)
    source_video = tmp_path / "source.mp4"
    _marker_video(source_video, 1280, 720)

    def provider(raw_path):
        groups = group_words(words)
        return build_track(groups, config, str(clipstore.subs_track_path(clip_dir)),
                           str(clipstore.subs_work_dir(clip_dir)))

    calls: list = []
    monkeypatch.setattr("yt_shorts.render.subprocess.run", _ytdlp_stub(calls, source_video))

    target = clipstore.short_path(clip_dir)
    build_short(Source(clip_url="https://example.invalid/clip/preview"), hook, footer,
                str(target), config, str(clip_dir), keep_raw=True,
                subtitle_provider=provider)

    assert clipstore.raw_path(clip_dir).exists(), "keep_raw=True must leave raw.mp4 behind"
    assert target.exists()
    return clip_dir


def _extract_frame(path: Path, at: float) -> Image.Image:
    """Extracts a frame from a real mp4 at time `at`, applying the sample
    aspect ratio (see CLAUDE.md's "Extracted frames are not proof" note) -
    render.compose's own output carries setsar=1 so this is normally a
    no-op, but applying it is what makes the comparison trustworthy rather
    than merely convenient."""
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", str(path),
        "-frames:v", "1", "-vf", "scale=iw*sar:ih",
        "-f", "image2pipe", "-vcodec", "png", "-",
    ], capture_output=True, check=True)
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def _near_white_bbox(image: Image.Image, top: int, bottom: int) -> tuple[int, int, int, int] | None:
    """Bounding box of near-white pixels (the caption's own text color,
    colors.text = #FFFFFF for the fixture channel) within [top, bottom) -
    the caption band below the video window and above the footer. All
    three channels above 200 catches solidly-painted glyph interior even
    after H.264 compression softens antialiased edges slightly."""
    cropped = image.crop((0, top, image.width, bottom))
    r, g, b = cropped.split()
    mask = r.point(lambda v: 255 if v > 200 else 0)
    for channel in (g, b):
        mask2 = channel.point(lambda v: 255 if v > 200 else 0)
        from PIL import ImageChops
        mask = ImageChops.multiply(mask, mask2)
    box = mask.getbbox()
    if box is None:
        return None
    x0, y0, x1, y1 = box
    return x0, y0 + top, x1, y1 + top


TOLERANCE = 8  # px of slack: H.264 compression on the reference frame vs. lossless Pillow


class TestPreviewCaptionGeometryMatchesRealRender:
    def test_caption_position_agrees_with_the_rendered_frame(self, tmp_path, config, monkeypatch):
        words = [
            {"start": 1.0, "end": 1.3, "text": " SPEEDY"},
            {"start": 1.3, "end": 1.7, "text": " OVERTAKES"},
        ]
        hook = "PREVIEW GEOMETRY TEST"
        footer = "ERF | @ERFofficial"
        at = 1.4  # inside the second (and, by group_words' 4-word/3s default, only) caption group

        clip_dir = _build_real_clip(tmp_path, config, words, hook, footer, monkeypatch)

        png_bytes = preview.build(clip_dir, config, at, words, hook, footer)
        preview_image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        assert preview_image.size == (config["output"]["width"], config["output"]["height"])

        rendered_image = _extract_frame(clipstore.short_path(clip_dir), at)

        output = config["output"]
        band_top = output["video_y"] + output["video_height"]
        band_bottom = _footer_top(output["height"])  # exclusive: overlay.py's own boundary

        preview_box = _near_white_bbox(preview_image, band_top, band_bottom)
        rendered_box = _near_white_bbox(rendered_image, band_top, band_bottom)

        assert preview_box is not None, "no caption text found in the preview"
        assert rendered_box is not None, "no caption text found in the rendered frame"

        for name, p, r in zip(("x0", "y0", "x1", "y1"), preview_box, rendered_box, strict=True):
            assert p == pytest.approx(r, abs=TOLERANCE), (
                f"caption {name} differs: preview={preview_box} rendered={rendered_box}"
            )

        # Window placement (position AND size) must also agree - the
        # caption's own position comes straight out of config and would
        # not, by itself, catch a wrong sharp-image offset or scale (see
        # _marker_video's docstring). Uses the marker source's blue/red
        # contour rather than a hardcoded expected box, the same way
        # test_render.py's TestWindowAlignment measures compose()'s own
        # output instead of precomputing it.
        preview_sharp = _sharp_image_bbox(preview_image)
        rendered_sharp = _sharp_image_bbox(rendered_image)
        assert preview_sharp is not None, "no sharp marker image found in the preview"
        assert rendered_sharp is not None, "no sharp marker image found in the rendered frame"
        for name, p, r in zip(("x0", "y0", "x1", "y1"), preview_sharp, rendered_sharp, strict=True):
            assert p == pytest.approx(r, abs=TOLERANCE), (
                f"window {name} differs: preview={preview_sharp} rendered={rendered_sharp}"
            )

    def test_no_caption_drawn_outside_any_word_group(self, tmp_path, config, monkeypatch):
        words = [{"start": 1.0, "end": 1.3, "text": " HELLO"}]
        hook = "NO CAPTION HERE"
        footer = "ERF | @ERFofficial"

        clip_dir = _build_real_clip(tmp_path, config, words, hook, footer, monkeypatch)

        # Well after the only caption's end (1.3s) and before the clip ends.
        png_bytes = preview.build(clip_dir, config, 3.0, words, hook, footer)
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")

        output = config["output"]
        band_top = output["video_y"] + output["video_height"]
        band_bottom = _footer_top(output["height"])
        assert _near_white_bbox(image, band_top, band_bottom) is None, (
            "no caption should be drawn at a time no word group covers"
        )


class TestPreviewContract:
    def test_missing_raw_mp4_raises_a_clear_error_naming_the_clip(self, tmp_path, config):
        clip_dir = tmp_path / "clips" / "no-raw-here"
        clip_dir.mkdir(parents=True)

        with pytest.raises(PreviewError) as excinfo:
            preview.build(clip_dir, config, 1.0, [], "SOME HOOK", "FOOTER")

        assert "no-raw-here" in str(excinfo.value)

    def test_returns_png_bytes_at_the_configured_canvas_size(self, tmp_path, config):
        clip_dir = tmp_path / "clips" / "plain"
        clip_dir.mkdir(parents=True)
        _solid_video(clipstore.raw_path(clip_dir), 1280, 720, "0x336699", seconds=2)

        png_bytes = preview.build(clip_dir, config, 0.5, [], "A HOOK", "A FOOTER")

        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        image = Image.open(io.BytesIO(png_bytes))
        assert image.size == (config["output"]["width"], config["output"]["height"])

    def test_broken_raw_mp4_raises_a_clear_error(self, tmp_path, config):
        clip_dir = tmp_path / "clips" / "broken"
        clip_dir.mkdir(parents=True)
        clipstore.raw_path(clip_dir).write_bytes(b"not a real video")

        with pytest.raises(PreviewError):
            preview.build(clip_dir, config, 0.5, [], "A HOOK", "A FOOTER")
