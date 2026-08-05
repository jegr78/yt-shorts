"""Loads the clip and composes it with the brand overlay.

Knows neither typography nor detection logic — only source, overlay, target.
"""

from __future__ import annotations

import subprocess  # noqa: F401 - no longer called directly (see cancel.run_cancellable),
# kept importable as `yt_shorts.render.subprocess` because
# tests/test_render.py's existing fakes monkeypatch that exact attribute path
# rather than an injected seam; it is the SAME module object `cancel.py`'s
# own `import subprocess` resolves to, so patching it here still intercepts
# run_cancellable's internal subprocess.run call - BUT ONLY on the path with
# no cancel token. Given one, `run_cancellable` uses `subprocess.Popen`
# instead, which a fake that replaces only `run` does not intercept at all:
# such a test would spawn a real yt-dlp/ffmpeg. No current test does (they
# pass no token), and one that passes a token has to stub `Popen` too.
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import clipid, editorial
from .cancel import CancelToken, run_cancellable
from .overlay import build_overlay

FORMAT_SELECTION = "bv*[height<=1080]+ba/b[height<=1080]/b"
TIMEOUT_SECONDS = 600


@dataclass
class Source:
    clip_url: str | None = None
    video_id: str | None = None
    start: float | None = None
    end: float | None = None


def ytdlp_command(source: Source, target: str, ytdlp: str = "yt-dlp") -> list[str]:
    """Builds the yt-dlp command line. Executes nothing."""
    command = [ytdlp, "-f", FORMAT_SELECTION, "--merge-output-format", "mp4",
               "--no-warnings", "-o", target]
    if source.clip_url:
        # A clip URL already carries its boundaries within itself. Constrain it to
        # http(s) and place it after '--' so a 'file://'/'-'-leading value can
        # neither be read as a local file nor as a yt-dlp option (e.g. --exec).
        clipid.require_http_url(source.clip_url)
        command += ["--", source.clip_url]
        return command
    if source.video_id:
        if source.start is None or source.end is None:
            raise ValueError("video_id needs start and end")
        command += ["--download-sections", f"*{source.start}-{source.end}",
                    "--force-keyframes-at-cuts",
                    f"https://www.youtube.com/watch?v={source.video_id}"]
        return command
    raise ValueError("Source needs clip_url or video_id")


def source_for_clip(clip: dict, edit) -> Source:
    """Builds the Source for a clip entry, editorial window applied.

    A moment carries a video_id and a detected window; it downloads the
    effective window (the operator's editorial override if set, else the
    detected one - see editorial.effective_window) through the same
    --download-sections path render.Source already had. A community clip has no
    video_id and is fetched by its clip URL, exactly as before. This is the one
    place both cmd_render and the studio job build a Source, so they cannot
    drift on how a moment is fetched.
    """
    video_id = clip.get("video_id")
    if video_id:
        start, end = editorial.effective_window(edit, clip["start"], clip["end"])
        return Source(video_id=video_id, start=start, end=end)
    return Source(clip_url=clip["url"])


def compose(raw: str, overlay_png: str, target: str, config: dict,
            ffmpeg: str = "ffmpeg", *, subtitle_track: str | None = None,
            cancel: CancelToken | None = None) -> None:
    """Scales to 1080 width, fits into portrait format, places the overlay on top."""
    a = config["output"]
    # The video image serves as its own background: scale down small (cheap
    # blurring), blur it, stretch it to full output size. The distortion
    # isn't noticeable at this degree of blur. On top of that the sharp
    # image, on top of that the semi-transparent brand overlay.
    #
    # The sharp image is FITTED into the window (force_original_aspect_ratio
    # =decrease scales it down until it fits entirely, without distorting or
    # cropping anything) and placed centered there — not just stretched to
    # the window width. For a source whose aspect ratio differs from the
    # window's, hitting the width alone isn't enough: either the width or
    # the height would otherwise be left unaccounted for, and the height
    # could shoot past the frame (e.g. 4:3 sources) or leave a gap in the
    # window that isn't centered (e.g. 21:9 sources). If room is left in
    # the window after fitting, the already-present blurred background
    # simply shows through there, because the fitted image is only placed
    # centered on top of it.
    #
    # setsar=1 at the end is mandatory: the background branch stretches the
    # image non-proportionally to portrait format, which makes ffmpeg set a
    # counteracting non-square SAR (256:81 for 16:9 sources). The file
    # would then have 1080x1920 pixels, but every player would stretch it
    # back to 16:9. Doing it once at the end is enough, because only the
    # final result's SAR ends up in the file. The optional subtitle track
    # (a transparent alpha .mov, since this ffmpeg has no libass) is
    # overlaid AFTER the brand overlay and BEFORE that final setsar=1, so
    # it participates in the same one-time SAR fix instead of needing its
    # own. eof_action=pass matters: the subtitle track ends at the last
    # caption, well before the video does. overlay's default eof_action is
    # "repeat", not truncation, so without this flag ffmpeg keeps repeating
    # the subtitle input's last frame - the last caption's own image - for
    # the remainder of the output once the track runs out, freezing that
    # caption on screen all the way to the end instead of letting it
    # disappear. eof_action=pass instead passes the base video through
    # unchanged as soon as the subtitle input is exhausted.
    chain = (
        f"[0:v]split=2[fg][bgsrc];"
        f"[bgsrc]scale=320:-2,boxblur=12:2,scale={a['width']}:{a['height']}[bg];"
        f"[fg]scale={a['video_width']}:{a['video_height']}:force_original_aspect_ratio=decrease[vf];"
        f"[bg][vf]overlay=x=(main_w-overlay_w)/2:"
        f"y={a['video_y']}+({a['video_height']}-overlay_h)/2[base];"
        f"[base][1:v]overlay=0:0:format=auto[ov];"
    )
    inputs = ["-i", raw, "-i", overlay_png]
    if subtitle_track:
        inputs += ["-i", subtitle_track]
        chain += "[ov][2:v]overlay=0:0:format=auto:eof_action=pass[sub];[sub]setsar=1[v]"
    else:
        chain += "[ov]setsar=1[v]"
    filter_chain = chain
    # ffmpeg writes to a scratch sibling and the finished file is MOVED into
    # place, so `target` never exists half-written. It is not merely tidiness:
    # the studio decides `has_short` - and derives the `short_version` cache
    # token - from nothing but the file's presence and its stat(), so a target
    # that exists while ffmpeg is still writing is a file the studio will
    # serve. That meant an `immutable` cache policy handed out for partial
    # bytes, and a "Download short" button giving a manual-upload operator a
    # truncated video to upload to YouTube by hand. A render takes minutes, so
    # the window was wide open. Writing aside also means a FAILED re-render no
    # longer destroys the previous short, which `-y` on the target did the
    # moment ffmpeg opened it.
    #
    # The `.part` goes BEFORE the extension: ffmpeg picks its muxer from the
    # output's extension, and a name ending in `.part` makes it refuse to
    # write at all.
    destination = Path(target)
    partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    command = [
        ffmpeg, "-v", "error", "-y",
        *inputs,
        "-filter_complex", filter_chain,
        "-map", "[v]", "-map", "0:a?",          # ? = audio optional, silent is allowed
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(partial),
    ]
    try:
        # With no token this is subprocess.run and nothing else - see
        # cancel.run_cancellable's own docstring. With one, a HARD stop
        # terminates the ffmpeg child and raises cancel.Stopped, which rides
        # out through this try UNCAUGHT (the `if result.returncode` check
        # below is simply never reached) straight into the `finally` -
        # exactly what keeps a half-written `partial` from surviving a kill,
        # the same safety property TestComposeIsAtomic already pins for a
        # plain failure.
        result = run_cancellable(command, timeout=TIMEOUT_SECONDS, cancel=cancel)
        if result.returncode != 0:
            raise RuntimeError(
                "ffmpeg failed.\nCommand: " + " ".join(command)
                + "\nOutput: " + result.stderr.strip()
            )
        # Path.replace is os.replace: atomic within one filesystem, and the
        # scratch file is a sibling of the target precisely so it always is.
        partial.replace(destination)
    finally:
        # A no-op after a successful move; on any failure - including the
        # subprocess timeout, which raises rather than returning - this is
        # what keeps a half-written file out of the clip directory.
        partial.unlink(missing_ok=True)



RAW_CACHE_NAME = "raw.mp4"


def build_short(source: Source, hook: str, footer: str, target: str,
                config: dict, work_dir: str, *,
                cleanup_temp_files: bool = True,
                keep_raw: bool = False,
                subtitle_provider: Callable[[str], str | None] | None = None,
                cancel: CancelToken | None = None) -> str:
    """Full path: load, build overlay, compose. Returns the target path.

    ``subtitle_provider``, if given, is called with the freshly downloaded
    raw file's path and must return either a subtitle track path (see
    ``subtitle_track.build_track``) or None for "nothing to show". It is
    called here rather than in the caller so that transcription happens
    on the SAME raw download that gets composed - and so render.py itself
    still knows nothing about Whisper or transcription; it only knows it
    may be handed a finished track to overlay.

    ``work_dir`` (``raw/``) is only a scratch space for a single run of this
    function, not a cache across multiple calls: yt-dlp silently skips a
    download (return value 0) if the target file already exists ("has
    already been downloaded"). Without the removal below, a repeated run
    (e.g. after a race weekend) would compose from long-stale material —
    and because the raw filename depends on the hook/collision suffix,
    even under the wrong name as soon as the order in clips.json changes.
    That's why any existing raw file is removed before every load, and
    after a SUCCESSFUL build the raw file and overlay are deleted again —
    the work folder therefore never holds on to anything longer than a
    single running call. If the build fails, both files are deliberately
    left in place: they are the only artifacts that let a failure (broken
    raw material vs. an ffmpeg problem) be investigated afterwards. A
    subtitle track produced by ``subtitle_provider`` lives in this same
    work folder (its own intermediate PNGs and script are already cleaned
    up by ``build_track`` itself) and is removed on the same terms as the
    raw file and overlay: gone after success, left in place after a
    failure.

    ``keep_raw``, if true, keeps the raw download around after a
    SUCCESSFUL build instead of deleting it: it is renamed (not copied) to
    a fixed ``raw.mp4`` in ``work_dir`` — the name ``clipstore.raw_path``
    expects, since a caller that wants the raw clip to survive is, in
    practice, ``cmd_render`` handing this function a clip's own directory
    as ``work_dir``. render.py does not import ``clipstore`` (it knows
    only source, overlay, target - see the module docstring), hence the
    literal name rather than a call across that boundary; the two are
    covered by ``tests/test_clipstore.py``'s own naming test staying in
    agreement with this one. Only the raw file's fate changes; the
    overlay and any subtitle track are still removed exactly as before,
    governed only by ``cleanup_temp_files``. The disk cost is roughly the
    size of the downloaded source clip, per kept clip - deleting a kept
    ``raw.mp4`` by hand is always safe, since a later render simply
    re-downloads it (see ``ytdlp_command``/the removal above, and the
    README's "No stale material" guarantee).

    ``cancel`` (see ``yt_shorts.cancel``), if given, is forwarded to BOTH
    subprocess boundaries this function drives - the yt-dlp download below
    and ``compose``'s own ffmpeg encode - so a hard stop reaches whichever
    child is running when it arrives, not only the gap between clips a
    caller's own loop checks. A ``Stopped`` raised from either rides out of
    this function unwrapped, exactly like a plain download or encode
    failure: neither is caught here, so the temp-file-left-for-
    troubleshooting behaviour on failure is unchanged either way.
    """
    dir_ = Path(work_dir)
    dir_.mkdir(parents=True, exist_ok=True)
    stem = Path(target).stem
    raw = dir_ / f"{stem}.raw.mp4"
    overlay = dir_ / f"{stem}.overlay.png"

    # Make sure a fresh load actually happens: a raw file of the same name
    # from an earlier run (or under the wrong name due to a swapped
    # collision order) must never be reused.
    raw.unlink(missing_ok=True)

    command = ytdlp_command(source, str(raw))
    result = run_cancellable(command, timeout=TIMEOUT_SECONDS, cancel=cancel)
    if result.returncode != 0 or not raw.exists():
        raise RuntimeError(
            "yt-dlp failed.\nCommand: " + " ".join(command)
            + "\nOutput: " + result.stderr.strip()
        )

    build_overlay(hook, footer, config).save(overlay)
    track = subtitle_provider(str(raw)) if subtitle_provider else None
    compose(str(raw), str(overlay), target, config, subtitle_track=track, cancel=cancel)

    if cleanup_temp_files:
        if keep_raw:
            raw.replace(dir_ / RAW_CACHE_NAME)
        else:
            raw.unlink(missing_ok=True)
        overlay.unlink(missing_ok=True)
        if track:
            Path(track).unlink(missing_ok=True)

    return target
