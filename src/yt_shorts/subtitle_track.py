"""Turns caption groups into a transparent video track.

ffmpeg here has no libass, so subtitles cannot be burned in as text. Each
caption is drawn as a PNG instead and the PNGs are concatenated into an
alpha video (qtrle/argb) that the renderer overlays in a single step. The
alternative - one timeline-gated overlay filter per caption - would grow
the filter chain to 60-80 links on a one minute short.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .captions import Caption
from .overlay import build_caption

TIMEOUT_SECONDS = 300


def _validate(captions: list[Caption]) -> None:
    """Validates that a caption list can be walked in order without
    mistiming the result: every `end` no earlier than its own `start`, and
    every `start` no earlier than the previous caption's `end` (captions
    must be sorted and must not overlap).

    `end == start` is deliberately legal: Whisper can emit a word whose
    start and end are identical, so `captions.group_words` can legitimately
    produce one. It yields a single-frame flash - acceptable, not an error.

    Mirrors profile.py's validation style: every defect is collected and
    reported together in one ValueError, rather than raising on the first,
    so a caller feeding a whole batch sees every problem in one run.
    """
    problems: list[str] = []
    previous_end: float | None = None
    for index, caption in enumerate(captions):
        if caption.end < caption.start:
            problems.append(
                f"caption {index}: end ({caption.end}) is before start "
                f"({caption.start})"
            )
        if previous_end is not None and caption.start < previous_end:
            problems.append(
                f"caption {index}: start ({caption.start}) is before the "
                f"previous caption's end ({previous_end}) - captions must be "
                "sorted and must not overlap"
            )
        previous_end = caption.end

    if problems:
        listing = "\n".join(f"  - {p}" for p in problems)
        raise ValueError(
            f"Invalid caption list ({len(problems)} problem(s)):\n{listing}"
        )


def _timeline(captions: list[Caption]) -> list[tuple[str, float]]:
    """Caption list -> (text, duration) pairs covering 0 to the last end,
    with gaps represented as empty text."""
    entries: list[tuple[str, float]] = []
    position = 0.0
    for caption in captions:
        if caption.start > position:
            entries.append(("", caption.start - position))
        entries.append((caption.text, max(caption.end - caption.start, 0.0)))
        position = caption.end
    return entries


def build_track(captions: list[Caption], config: dict, target: str,
                work_dir: str, ffmpeg: str = "ffmpeg", *,
                cleanup_temp_files: bool = True) -> str | None:
    """Writes an alpha .mov of the captions. Returns None if there are none.

    ``captions`` must be sorted and non-overlapping - each caption's `start`
    at or after the previous caption's `end` - and every caption's `end` at
    or after its own `start` (`end == start` is a legal single-frame flash,
    not an error). See `_validate`. Raises `ValueError` otherwise, before
    anything is written to `work_dir`.

    ``work_dir`` is only scratch space for a single running call, never a
    cache across calls - mirroring `render.build_short`, but needing the
    other half of its convention. `build_short` writes fixed-name scratch
    files, so deleting its own files on success is complete by
    construction. `build_track` writes a variable number of PNGs, one per
    timeline entry, so "delete what this call created" can never be
    complete: a call that fails after writing N PNGs, followed by a
    successful call whose caption list produces fewer than N entries,
    would leave the excess behind. So any `caption-*.png` and
    `captions.txt` already sitting in `work_dir` are removed before this
    call writes its own, in addition to removing this call's own
    intermediates again after a SUCCESSFUL build. On failure the files
    this call wrote are left in place - they are the only artifacts that
    make an ffmpeg failure investigable. A consequence of clearing the
    directory up front: `work_dir` must not be shared by two concurrently
    running `build_track` calls (each clip gets its own - see Task 5).
    """
    if not captions:
        return None

    _validate(captions)

    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)

    for stale in directory.glob("caption-*.png"):
        stale.unlink(missing_ok=True)
    (directory / "captions.txt").unlink(missing_ok=True)

    pngs: list[Path] = []
    lines: list[str] = []
    for index, (text, duration) in enumerate(_timeline(captions)):
        png = directory / f"caption-{index:04d}.png"
        build_caption(text, config).save(png)
        pngs.append(png)
        lines.append(f"file '{png.name}'")
        lines.append(f"duration {duration:.3f}")
    # The concat demuxer ignores the final entry's duration, so the last
    # image is repeated to give it one.
    #
    # How long that repeated frame actually lasts depends on the ffmpeg build,
    # and this is NOT settled: ffmpeg 8 honours the preceding `duration`, while
    # ffmpeg 6 (Ubuntu 24.04 LTS) gives it the 1s default, so the same caption
    # list yields a track ~1s too long there - measured on CI as 4.48s where
    # 3.5s was asked for. Capping the output with `-t <sum of durations>` was
    # tried and is WRONG: the concat demuxer reports r_frame_rate 1/1, so `-t`
    # truncates at a frame boundary and produced 2.04s locally instead of 3.5s.
    # Left as-is deliberately until a fix is verified against both versions.
    lines.append(lines[-2])

    script = directory / "captions.txt"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")

    command = [
        ffmpeg, "-v", "error", "-y",
        "-f", "concat", "-i", str(script),
        "-c:v", "qtrle", "-pix_fmt", "argb",
        target,
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed building the subtitle track.\nCommand: " + " ".join(command)
            + "\nOutput: " + result.stderr.strip()
        )

    if cleanup_temp_files:
        for png in pngs:
            png.unlink(missing_ok=True)
        script.unlink(missing_ok=True)

    return target
