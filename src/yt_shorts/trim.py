"""Cutting seconds off the head and tail of an already-rendered short.

The ONE place a rendered short is cut. It imports `clipstore` and `editorial`
but never FastAPI - `bin/yt-shorts` must import it in a venv that never
installed FastAPI, the same rule `subtitle_pipeline.py` follows.

THE INVARIANT: `short.mp4` embodies the trim recorded in `short.trim.json`
(no file means no trim). Everything here exists to keep that true.

Three files, and each has one job:

    short.mp4         the deliverable - what the player, the download link
                      and the upload all read, always already cut
    short.full.mp4    the untrimmed master, present only while a trim is
                      applied. Derived, not an original: a re-render
                      recreates it.
    short.trim.json   which trim short.mp4 currently embodies

Cutting ALWAYS reads the master, never the current `short.mp4`. That is what
stops a second correction from compounding on the first - trim 3 s, then
change your mind and trim 5 s, and the result is 5 s off the original rather
than 8.

The cut RE-ENCODES. A stream copy was measured and rejected: cuts land on
keyframes, which sit 4.18 s apart in this project's own output, so a cut
requested at 5.0 s landed at 4.18 s. `-crf 18 -preset veryfast` takes 15 s
for an 84-second 1080x1920 short and is visually indistinguishable at one
generation - and because every cut comes from the master, there is never a
second generation.
"""

from __future__ import annotations

import functools
import json
import logging
from pathlib import Path
from typing import Callable

from . import atomicwrite, clipstore, editorial
from .cancel import CancelToken, Stopped, run_cancellable

# "ytshorts.*", NOT __name__: logsetup.configure_logging sets up the
# `ytshorts` tree with propagate=False, so a logger named "yt_shorts.trim"
# would sit in a different tree and reach neither the central log nor the
# CLI's TTY handler - the same rule subtitle_pipeline.py documents for its
# own logger.
_LOGGER = logging.getLogger("ytshorts.trim")

# Measured on this project's own 84-second short: 15 s, geometry preserved.
ENCODE_ARGS = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "copy"]

# Tolerance for comparing a recorded trim with a requested one. JSON
# round-trips floats exactly, but an operator's UI may send 3 where 3.0 was
# stored; this keeps that from looking like a change.
TOLERANCE_SECONDS = 0.001


class TrimError(RuntimeError):
    """Cutting failed. The previous short and its state are untouched."""


def _default_runner(command: list[str], *, cancel: CancelToken | None = None) -> str:
    """The ffmpeg/ffprobe boundary. With a `cancel` token a HARD stop
    terminates the child (see `cancel.run_cancellable`), which is what makes
    `ensure_applied`'s own `finally` run while a cut is still in flight -
    the design's central safety property, since a Python thread cannot be
    killed and the scratch file must not survive. Without a token this is
    the plain `subprocess.run` it always was."""
    result = run_cancellable(command, timeout=900, cancel=cancel)
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["failed"])[-1]
        raise RuntimeError(message)
    return result.stdout


def applied(directory: str | Path) -> tuple[float, float] | None:
    """The trim `short.mp4` currently embodies, or None."""
    path = clipstore.short_trim_state_path(directory)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return (float(payload["head"]), float(payload["tail"]))
    except (OSError, ValueError, KeyError, TypeError):
        # An unreadable state file means "we do not know what this file is",
        # which is the same starting position as "nothing applied": the next
        # ensure_applied re-derives from whatever short.mp4 is now. Never
        # raises - a corrupt sidecar must not make a clip unopenable.
        return None


def is_unknown(directory: str | Path) -> bool:
    """Whether short.mp4's cut state cannot be trusted at all.

    True exactly when an untrimmed master survives beside short.mp4 but
    nothing readable says what short.mp4 currently embodies - the state
    file lost in the window between `scratch.replace(short)` and the
    state-file write below, or simply deleted or corrupted afterward.
    `ensure_applied` already detects this same condition itself (its own
    `unknown_but_mastered`) and repairs it by rebuilding from the master;
    this is the READ-ONLY mirror of that check, for callers that only need
    to know whether to trust the file, not fix it - `is_pending` (to
    refuse delivery) and the studio's `_summary` (to tell the client to
    offer the repair rather than silently agreeing nothing is pending).

    A clip that never had a trim has no master at all, so this is False. A
    normally-applied trim has a readable state file, so this is also False
    - only the crash-or-corruption window in between is True.
    """
    return applied(directory) is None and clipstore.short_master_path(directory).exists()


def is_pending(directory: str | Path, edit: editorial.Edit) -> bool:
    """Whether short.mp4 must not be handed out as the operator's desired
    trim yet.

    Ordinarily a comparison of two recorded values - never a probe of the
    video, because this runs per clip on every studio clip-list request.
    But a recorded comparison alone cannot see the one state it must still
    refuse: `is_unknown` above. Without that check, a desired trim of
    (0, 0) - the ordinary, never-touched state of most clips - read as
    "already satisfied" without ever looking at the master sitting right
    beside short.mp4, so a genuinely CUT short.mp4 (state file lost between
    the cut landing and being recorded, or later deleted/corrupted) was
    handed out as the full deliverable on every path that calls this: the
    download link, the studio upload route and `cmd_upload`.
    """
    if not clipstore.short_path(directory).exists():
        return False
    if is_unknown(directory):
        return True
    desired = editorial.effective_trim(edit)
    current = applied(directory) or (0.0, 0.0)
    return not _same(desired, current)


def forget_applied(directory: str | Path) -> None:
    """Drops the master AND the state file.

    Called by a render BEFORE ensure_applied. After a render, `short.mp4` is
    a freshly composed untrimmed file and IS the new master; a
    `short.full.mp4` left over from before that render is the OLD
    composition, and cutting from it would build the deliverable out of stale
    material while the fresh short sat beside it unused. Removing only the
    state file leaves exactly that stale master in place for the promote step
    to skip - which is why both go.
    """
    for path in (clipstore.short_master_path(directory),
                 clipstore.short_trim_state_path(directory)):
        path.unlink(missing_ok=True)


def ensure_applied(directory: str | Path, edit: editorial.Edit, *,
                   runner=_default_runner, ffmpeg: str = "ffmpeg",
                   ffprobe: str = "ffprobe",
                   logger: logging.Logger | None = None,
                   on_note: Callable[[str], None] | None = None,
                   cancel: CancelToken | None = None) -> bool:
    """Makes `short.mp4` embody `edit.trim`. Idempotent. Returns whether it
    changed anything.

    `runner` is the injected subprocess boundary, so every branch tests with
    no ffmpeg, no ffprobe and no video - the same treatment
    `stream_transcribe.ytdlp_downloader` gives its own tool calls.

    `logger`/`on_note` mirror `subtitle_pipeline.make_subtitle_provider`'s own
    pair, for the same reason: two different people look in two different
    places. The logger is the durable record - a CLI trim still reaches the
    operator's terminal via the `ytshorts.*` tree's TTY handler and persists
    in `logs/yt-shorts.log`; a studio job injects `job_logger(job)` so the
    note lands in that job's own `logs/jobs/...` file. `on_note` is for a
    caller that surfaces notes in a channel of its own - a studio trim job
    collects them as the clip's `reason`, the same way a render's dropped-
    caption note already does. Passing only `on_note` used to mean the
    logger never saw it at all; passing only `logger` used to mean a studio
    caller had no way to learn what happened. Both, always - see `note()`
    below.

    ``cancel`` (see yt_shorts.cancel) is checked ONCE, before anything on
    disk is touched: a cut has no interior safe point - it is one ffmpeg
    invocation - so "stop after the current cut" is the only honest promise,
    and a stop that arrives before the promotion costs nothing at all. A
    HARD stop is different and does reach inside: it terminates the ffmpeg
    child, and the `Stopped` that comes back travels out UNWRAPPED, past
    both `except` clauses below, having first restored the deliverable and
    removed the scratch file exactly as a `TrimError` would. Wrapping it in
    `TrimError` would mark a job the operator stopped as `failed`, sending
    them to look for a cause that does not exist.
    """
    directory = Path(directory)
    notes_logger = logger if logger is not None else _LOGGER
    if cancel is not None:
        # Before the promotion, the probe and the cut - so a stop that has
        # already arrived leaves short.mp4 exactly where it is.
        cancel.raise_if_stopped()
        # Bound once rather than at each call site, so the two subprocess
        # calls below (probe and cut) cannot drift apart, and so a runner
        # written before this parameter existed keeps being called with the
        # single positional argument it accepts.
        runner = functools.partial(runner, cancel=cancel)

    def note(message: str) -> None:
        """Every "this trim did something you should know about" message
        goes through here, to BOTH a logger and the caller's own channel -
        `subtitle_pipeline.make_subtitle_provider`'s own `note()`, mirrored
        rather than reinvented. Two consumers, two mechanisms, on purpose:
        the logger is the durable record (a CLI operator's terminal and
        `logs/yt-shorts.log`, or a studio job's own log file); `on_note` is
        for a caller - the studio job runner - that surfaces notes in a
        channel of its own, recorded against the clip as its `reason`. A
        single `_log_note` default used to make these either/or: a caller
        that supplied its own `on_note` got no log record at all.
        """
        notes_logger.warning("%s", message)
        if on_note is not None:
            on_note(message)

    short = clipstore.short_path(directory)
    if not short.exists():
        return False                      # nothing rendered yet; a render will apply it

    desired = editorial.effective_trim(edit)
    known = applied(directory)
    current = known or (0.0, 0.0)
    master = clipstore.short_master_path(directory)
    # A master surviving with no readable state means "we do not know what
    # short.mp4 currently embodies" - not "nothing was ever trimmed" (see
    # `is_unknown` above, the same condition in read-only form). The
    # ordinary comparison below treats a missing/unreadable state file the
    # same as "no trim recorded", and for a desired trim of (0, 0) that used
    # to return here without ever looking at short.mp4 or the master beside
    # it - so a genuinely CUT short.mp4 (the state file lost in the window
    # between scratch.replace(short) and the state-file write, or simply
    # deleted/corrupted afterward) was handed out as the full deliverable on
    # every delivery path. `is_pending` now refuses delivery in exactly this
    # state - but refusing is not repairing: an operator still needs a way
    # to fix short.mp4 itself, and that is what falling through to here
    # does. The (0, 0) branch below restores short.mp4 from the master, and
    # any other desired trim re-cuts from the master exactly as a known
    # trim already does - both already read the master, never the current
    # short.mp4, so trusting it here costs nothing.
    unknown_but_mastered = known is None and master.exists()
    if not unknown_but_mastered and _same(desired, current):
        return False
    if unknown_but_mastered:
        note(f"{directory.name}: the recorded trim is unreadable (or missing) "
             f"but an untrimmed master exists; short.mp4 may not be the file "
             f"it appears to be, so it is being rebuilt from the master "
             f"rather than trusted as-is")

    # Recorded so a failure below can put the deliverable back. Without this,
    # a failed cut left short.mp4 gone for good: the promotion below is a
    # RENAME, done before the cut is even attempted, so a subsequent ffmpeg
    # failure used to strand the master under short.full.mp4 forever - the
    # next ensure_applied early-returns at the `not short.exists()` check
    # above, is_pending reports False for the same reason, and the studio
    # shows the clip as "not rendered" while a good short sits right beside
    # it under the other name. Only undo the rename if THIS call performed
    # it - if the master already existed, it is not this call's to move.
    promoted = False
    if not master.exists():
        if clipstore.short_trim_state_path(directory).exists():
            if known is None:
                # The state file exists but applied() could not read it - do
                # not report numbers we do not have. Falling back to the
                # `_same()` comparison's own (0.0, 0.0) default here used to
                # claim "already 0.0s + 0.0s shorter than the render" in the
                # same sentence that says a trim IS recorded - self-
                # contradictory and simply false whenever the real numbers
                # were anything else.
                note(f"{directory.name}: the untrimmed master is gone and "
                     f"the recorded trim is unreadable; re-cutting from "
                     f"the current short, whose own loss is unknown")
            else:
                note(f"{directory.name}: the untrimmed master is gone; re-cutting "
                     f"from the current short, which is already {known[0]}s + "
                     f"{known[1]}s shorter than the render")
        short.replace(master)             # a rename, not a copy
        promoted = True

    if _same(desired, (0.0, 0.0)):
        # Reached with `promoted` True whenever the master was missing but a
        # state file existed (the half-deleted-directory case above just
        # re-promoted the current short to master) - so this rename can fail
        # for the same OS reasons the cut's own restore below guards
        # against. Left unguarded, that failure used to escape as a raw
        # OSError instead of a TrimError, with short.mp4 stranded under the
        # master's name - the same shape as the bug the scratch/`finally`
        # dance below exists to prevent, in the one branch that dance does
        # not cover.
        try:
            master.replace(short)
            clipstore.short_trim_state_path(directory).unlink(missing_ok=True)
        except OSError as error:
            raise TrimError(
                f"could not restore {directory.name} to its untrimmed "
                f"master: {type(error).__name__}: {error}. "
                f"{clipstore.short_master_path(directory).name} still holds "
                f"a complete copy - rename it to "
                f"{clipstore.short_path(directory).name} by hand, or "
                f"re-render the clip") from error
        return True

    # The scratch name keeps the .mp4 EXTENSION: ffmpeg picks its muxer from
    # it and refuses to write to an unknown one. Same rule (and same hard
    # lesson) as render.compose's own scratch file.
    scratch = directory / "short.trim-part.mp4"
    try:
        duration = _probe_duration(master, runner=runner, ffprobe=ffprobe)
        head, tail = desired
        try:
            # The only place that knows the master's REAL length. Without
            # this, MIN_REMAINING_SECONDS is never enforced against the
            # rendered duration and an over-trim reaches the operator as
            # ffmpeg's own "Error opening input files: Invalid argument"
            # instead of a clear message naming the floor.
            editorial.validate_trim(list(desired), duration, path=directory.name)
        except editorial.EditError as error:
            # Preserve validate_trim's own message verbatim - it already
            # names the floor and the duration - rather than re-wording it
            # through the generic "could not cut ..." wrapper below.
            raise TrimError(str(error)) from error
        # -ss AND -to both BEFORE -i. Measured on this project's own short:
        # `-ss 5 -to 79 -i in.mp4` yields 74.08 s; moving -to after -i yields
        # 79.10 s, because the seek has reset timestamps and -to is then read
        # as a LENGTH. The tail cut silently does not happen and ffmpeg exits
        # 0 either way.
        runner([ffmpeg, "-v", "error", "-y",
                "-ss", _seconds(head), "-to", _seconds(duration - tail),
                "-i", str(master), *ENCODE_ARGS, str(scratch)])
        # Written aside and MOVED into place, never straight to the target:
        # from the moment a naive in-place cut starts, short.mp4 exists but
        # is incomplete, its (mtime, size) version token matches, and the
        # studio would hand out `immutable` for bytes still being written.
        scratch.replace(short)
    except (TrimError, Stopped):
        # Stopped rides alongside TrimError here and NOT in the generic
        # handler below: the recovery is identical (put the deliverable
        # back), but the exception must reach the job runner as itself. A
        # stop wrapped in TrimError reads as a failure, which is the one
        # thing a stop is not.
        if promoted:
            master.replace(short)     # put the deliverable back
        raise
    except Exception as error:
        if promoted:
            master.replace(short)     # put the deliverable back
        raise TrimError(f"could not cut {directory.name}: "
                        f"{type(error).__name__}: {error}") from error
    finally:
        scratch.unlink(missing_ok=True)

    # AFTER the replace, never before: a failed cut must leave the previous
    # file and its state agreeing with each other.
    atomicwrite.write_text(
        clipstore.short_trim_state_path(directory),
        json.dumps({"head": head, "tail": tail}, indent=2) + "\n")
    return True


def _same(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return (abs(left[0] - right[0]) <= TOLERANCE_SECONDS
            and abs(left[1] - right[1]) <= TOLERANCE_SECONDS)


def _seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _probe_duration(path: Path, *, runner, ffprobe: str) -> float:
    """The master's real length. Never guessed: `-to` is a position, and a
    wrong one silently produces a wrongly-sized short."""
    try:
        output = runner([ffprobe, "-v", "error", "-show_entries",
                         "format=duration", "-of", "csv=p=0", str(path)])
        return float(output.strip())
    except Stopped:
        # A hard stop killed the probe. Not a probe that failed - see
        # ensure_applied's own account of why this must not become a
        # TrimError.
        raise
    except Exception as error:
        raise TrimError(
            f"could not read the duration of {path.name}: "
            f"{type(error).__name__}: {error}") from error
