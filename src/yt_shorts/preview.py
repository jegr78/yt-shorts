"""Draws one frame of what a short would look like, without re-encoding it.

The studio's editor needs feedback in milliseconds when an operator tweaks
a caption or the hook: re-running render.compose() (a real ffmpeg encode
of the whole clip) costs seconds to minutes. This module instead extracts
ONE frame from the clip's cached raw.mp4 with ffmpeg and composes it with
Pillow - the same library overlay.py already uses for the brand overlay
and captions - so no second encoder is needed and nothing is written to
disk.

The composition deliberately reproduces render.compose()'s geometry (the
blurred full-frame background, the sharp picture fitted into the window
and centered, the brand overlay on top) rather than re-deriving it, so a
caption an operator repositions here lands where the real render will put
it. It is NOT expected to be pixel-identical to compose()'s ffmpeg output
- one path scales and blurs in Pillow, the other in ffmpeg's own scaler -
only geometrically equivalent: same window position and size, same
caption position and size. See tests/test_preview.py's own comparison
against a real rendered frame for how that is verified.

Typography is never reimplemented here: build_overlay and build_caption
(overlay.py) are called exactly as render.build_short/compose call them,
so a caption's font, size and position come from the one place that
decides them.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

from PIL import Image, ImageFilter

from . import clipstore
from .captions import DEFAULT_MAX_SECONDS, DEFAULT_MAX_WORDS, group_words
from .overlay import build_caption, build_overlay

TIMEOUT_SECONDS = 30

# The small width the background is downscaled to before blurring, and the
# box-blur radius applied there - chosen to match render.compose()'s own
# "scale=320:-2,boxblur=12:2" background branch (see its docstring for why
# blurring small first is cheap and looks the same as blurring full-size).
BACKGROUND_SMALL_WIDTH = 320
BACKGROUND_BLUR_RADIUS = 12
BACKGROUND_BLUR_PASSES = 2  # boxblur's own "power" parameter: two passes


class PreviewError(Exception):
    """Understandable error naming the clip a preview could not be built for."""


def _extract_frame(raw: Path, at: float, ffmpeg: str) -> Image.Image:
    """Extracts one frame from raw.mp4 at time `at`, corrected for sample
    aspect ratio (see the project's own "Extracted frames are not proof"
    note in CLAUDE.md - a distorted video yields a correct-looking still
    unless the SAR is applied). raw.mp4 is the untouched download
    compose() itself reads, so this is exactly the frame compose() would
    place in the output at the same timestamp."""
    command = [
        ffmpeg, "-v", "error", "-y",
        "-ss", str(at), "-i", str(raw),
        "-frames:v", "1",
        "-vf", "scale=iw*sar:ih",
        "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    result = subprocess.run(command, capture_output=True, timeout=TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise PreviewError(
            "ffmpeg failed to extract a preview frame.\n"
            "Command: " + " ".join(command) + "\n"
            "Output: " + result.stderr.decode("utf-8", "replace").strip()
        )
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def _fit(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """The size `size` scales to in order to fit inside `box`, preserving
    aspect ratio - what ffmpeg's force_original_aspect_ratio=decrease
    computes for the sharp branch of compose()'s filter chain. Unlike
    Image.thumbnail, this may also enlarge (ffmpeg's "decrease" only
    guarantees the result does not EXCEED the box, not that it never grows
    past the source size)."""
    src_w, src_h = size
    box_w, box_h = box
    scale = min(box_w / src_w, box_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def _background(frame: Image.Image, width: int, height: int) -> Image.Image:
    """Blurred, frame-filling background - see render.compose()'s own
    background branch: downscale small (cheap blurring), blur, stretch
    (distorting) to the full canvas."""
    src_w, src_h = frame.size
    small_w = BACKGROUND_SMALL_WIDTH
    small_h = max(2, round(small_w * src_h / src_w / 2) * 2)  # even, like ffmpeg's -2
    small = frame.resize((small_w, small_h), Image.BILINEAR)
    blurred = small
    for _ in range(BACKGROUND_BLUR_PASSES):
        blurred = blurred.filter(ImageFilter.BoxBlur(BACKGROUND_BLUR_RADIUS))
    return blurred.resize((width, height), Image.BILINEAR)


def _active_caption(words: list[dict], config: dict, at: float) -> str | None:
    """The text of whichever caption group.group_words puts at time `at`,
    or None if no group covers it - grouped with the profile's own
    subtitles.max_words/max_seconds if set, exactly like cmd_render's
    subtitle_provider closure in bin/yt-shorts."""
    if not words:
        return None
    subtitles = config.get("subtitles", {})
    groups = group_words(
        words,
        max_words=subtitles.get("max_words", DEFAULT_MAX_WORDS),
        max_seconds=subtitles.get("max_seconds", DEFAULT_MAX_SECONDS),
    )
    for caption in groups:
        if caption.start <= at <= caption.end:
            return caption.text
    return None


def build(clip_dir: str | Path, config: dict, at: float, words: list[dict],
          hook: str, footer: str, ffmpeg: str = "ffmpeg") -> bytes:
    """Composes one PNG frame of what the finished short looks like at
    time `at`, using the clip's cached raw.mp4 - never short.mp4, which
    already has captions burned in. Returns PNG bytes; nothing is written
    to disk.

    Raises PreviewError, naming the clip, if raw.mp4 is missing (not yet
    rendered, or its cache was deleted by hand) - the caller (Stage C's
    API) turns that into a 409: there is nothing to preview until a real
    render has produced one.
    """
    clip_dir = Path(clip_dir)
    raw = clipstore.raw_path(clip_dir)
    if not raw.exists():
        raise PreviewError(
            f"no raw.mp4 for clip {clip_dir.name!r} ({hook!r}); "
            f"render this clip at least once before previewing it"
        )

    output = config["output"]
    canvas_w, canvas_h = output["width"], output["height"]

    frame = _extract_frame(raw, at, ffmpeg)

    canvas = _background(frame, canvas_w, canvas_h).convert("RGBA")

    fitted_w, fitted_h = _fit(frame.size, (output["video_width"], output["video_height"]))
    sharp = frame.resize((fitted_w, fitted_h), Image.LANCZOS).convert("RGBA")
    x = (canvas_w - fitted_w) // 2
    y = output["video_y"] + (output["video_height"] - fitted_h) // 2
    canvas.alpha_composite(sharp, (x, y))

    canvas.alpha_composite(build_overlay(hook, footer, config))

    caption_text = _active_caption(words, config, at)
    if caption_text:
        canvas.alpha_composite(build_caption(caption_text, config))

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()
