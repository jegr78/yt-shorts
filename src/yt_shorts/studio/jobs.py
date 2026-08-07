"""Background render jobs the studio can start and poll.

A render can take minutes (see CLAUDE.md: "a six-clip run takes minutes"),
far too long to hold an HTTP request open for. `start_render_job` starts
one in a background thread and returns immediately with a `Job` the caller
can poll by id via `JobStore.get`.

Deliberate limit: jobs live ONLY in the `JobStore` instance held in memory
(`app.state.job_store` - see `studio.api.create_app`) and do not survive a
process restart. There is no persistence, no queue, no resumption of a job
that was running when the process died. This is acceptable for a
single-user local tool an operator runs and watches interactively - it
would not be for anything meant to survive a restart or serve more than
one operator at a time. If that ever changes, this is the module to
replace, not patch around.

Exactly one render runs against an event at a time, same guarantee
`bin/yt-shorts render` gives via `yt_shorts.lock.EventLock`: this module
uses that SAME lock, acquired synchronously before the background thread
is even started (so a second call refuses immediately, before an HTTP 200
is ever returned) and released when the thread finishes, success or not.
This is what stops a studio render from racing a CLI render against the
same event, not just two studio renders against each other.

A failing clip never stops the job, exactly as `cmd_render` in
`bin/yt-shorts` behaves: every clip is rendered inside its own try/except,
and its failure is recorded with its exception type alongside every other
clip's result, in `Job.results`.

`_render_one` builds the same `subtitle_provider` `cmd_render` does, via
`yt_shorts.subtitle_pipeline.make_subtitle_provider` - the one place both
callers get it from, so the two can never drift apart. A studio render
that skipped this would throw away exactly the editorial corrections the
studio exists to let an operator make, which is the entire point of
running it locally in the first place.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from .. import clipstore, editorial, logsetup, render, trim, upload_record, workspace, workspaces
from ..cancel import CancelToken, Stopped, cancel_kwargs
from ..detect import detect_moments, require_cached_transcript
from ..glossary import EMPTY as GLOSSARY_EMPTY
from ..lock import EventLock, LockError
from ..profile import Profile
from ..stream_transcribe import transcribe_stream
from ..subtitle_pipeline import make_subtitle_provider
from ..workspace import resolve as _resolve_workspace

__all__ = ["Job", "ClipResult", "JobStore", "KindSpec", "KINDS", "start_render_job",
           "start_detect_job", "start_transcribe_job", "start_upload_job",
           "start_connect_job", "start_copy_job", "start_trim_job"]


_NULL_LOGGER = logging.getLogger("ytshorts.job.disabled")
_NULL_LOGGER.addHandler(logging.NullHandler())
_NULL_LOGGER.propagate = False

# The central studio log - one summary line per job lands here (see
# _log_terminal below), same logger the rest of studio.api logs through.
_STUDIO_LOGGER = logging.getLogger("ytshorts.studio")


def _open_job_log(job_id: str, kind: str) -> str | None:
    """Create this job's own log file and return its path, or None if it could
    not be created. Best-effort by contract: a job that cannot be logged still
    runs (see CLAUDE.md - logging must never abort a run).

    `_resolve_workspace()` (called first, above) can raise `workspace.
    WorkspaceError` - not only `OSError` - when the workspace is misconfigured
    (e.g. a renamed/unmounted YT_SHORTS_DATA; see workspace.py, "a set-but-
    missing YT_SHORTS_DATA is an error"). `create_app()` already guards the
    same call this way one file over; missing it here meant a bad workspace
    raised out of JobStore.create() AFTER the caller had already acquired the
    event lock, leaving it held by the still-live studio process forever -
    exactly the failure this best-effort contract exists to prevent."""
    try:
        directory = workspace.job_logs_dir(_resolve_workspace().root)
        path = directory / f"{kind}-{job_id}.log"
        logger = logsetup.configure_logging(f"ytshorts.job.{job_id}", path,
                                            to_stdout=False)
        logger.propagate = False   # never double-write into the central log
        return str(path)
    except (OSError, workspace.WorkspaceError):
        return None


def job_logger(job: "Job") -> logging.Logger:
    """The job's own logger - a null logger when its log could not be opened."""
    if job.log_path is None:
        return _NULL_LOGGER
    return logging.getLogger(f"ytshorts.job.{job.id}")


def finish_job_log(job: "Job") -> None:
    """Close and gzip a finished job's log. Idempotent: a second call on an
    already-compressed log is a no-op. Best-effort, like every logging path."""
    if job.log_path is None:
        return
    logsetup.close_logging(f"ytshorts.job.{job.id}")
    # Guard against the plain file already being gone (a second call, after a
    # prior successful compress): skip the attempt entirely rather than rely
    # solely on compress_file's own internal protection of a pre-existing .gz.
    if Path(job.log_path).exists():
        logsetup.compress_file(job.log_path)


@dataclass(frozen=True)
class KindSpec:
    """What the queue needs to know about a job kind, in one place.

    `pool` is the resource it saturates, not what it "is": transcribe and
    render both pin the CPU, detect and upload both wait on a network.
    `stop_point` is shown to the operator BEFORE they click, so the phrase
    must name a real boundary in the work ("after the current chunk"), not
    a reassurance.

    `progress_unit` names what this kind's work COUNTS - "chunk", "window",
    "clip" - or is None for a kind that reports nothing. It is the queue's
    half of a reading (`Entry.progress` is `{"unit", "done", "total"}`, see
    job_queue.py): the work reports two numbers and `studio.worker` adds
    the unit from here, so a screen renders "chunk 20 of 50" without
    knowing which kind produced it and no kind has to agree with any other
    about the payload's shape.

    It is KEYWORD-ONLY and has NO DEFAULT, deliberately: a kind added to
    the table below must SAY whether it reports progress. A default of None
    would let the next one be added silently reporting nothing, which is
    exactly the "the machinery is complete and the feature is not" gap this
    field exists to close - and None is a real answer here, not an absence
    (a trim is one cut; "1 of 1" for it is worse than silence).
    """
    pool: str            # "cpu" | "net" | "io"
    stop_point: str
    hard_stop_allowed: bool
    queueable: bool = True
    progress_unit: str | None = field(kw_only=True)


KINDS: dict[str, KindSpec] = {
    "transcribe": KindSpec("cpu", "after the current chunk", True,
                           progress_unit="chunk"),
    "render": KindSpec("cpu", "after the current clip", True,
                       progress_unit="clip"),
    # One ffmpeg invocation with no interior boundary - the same thing that
    # gives it no interior stop point gives it nothing to count. "1 of 1"
    # would be a progress bar that is a decoration.
    "trim": KindSpec("cpu", "after the current cut", True, progress_unit=None),
    "detect": KindSpec("net", "after the current window", True,
                       progress_unit="window"),
    # No stop at any level: a half-finished upload to YouTube is worse than
    # waiting for it, so the UI must not offer a button that would lie.
    # Nothing to count either: the API's own resumable upload reports bytes
    # to a layer this code does not watch, so there is no honest reading to
    # pass on.
    "upload": KindSpec("net", "cannot be stopped", False, progress_unit=None),
    # Opens the operator's browser and waits for a consent only they can
    # give; queueing it behind a two-hour transcription is meaningless.
    "connect": KindSpec("net", "cannot be stopped", False, queueable=False,
                        progress_unit=None),
    # Two things about copy that were once declared and not backed, and are
    # now both true. Its stop point IS "after the current file": the token
    # rides into `workspaces.copy_workspace` as the `copy_function` that
    # copies each entry, so it is checked before every file. But there is no
    # HARD stop, because a hard stop is defined here as terminating the
    # subprocess the work waits on (see cancel.run_cancellable) and a
    # copytree waits on no subprocess at all - it is the thread itself, and
    # threads are not killable. `hard_stop_allowed=True` promised a button
    # that could never do anything more than the graceful one.
    # Not queueable, for the same reason `connect` is not: `start_copy_job`
    # takes an `on_done` callback that re-roots the LIVE app onto the copy,
    # which no state file can hold - so it keeps its own route (POST
    # /api/workspaces/copy) rather than being a plan the worker could pick up.
    # Not queueable, so no queue entry can exist for it to report against -
    # `Entry.progress` is the only thing a unit would feed.
    "copy": KindSpec("io", "after the current file", False, queueable=False,
                     progress_unit=None),
}


@dataclass
class ClipResult:
    status: str  # "done" | "failed" | "skipped"
    reason: str | None = None


class Job:
    """One render run. Mutated from the background thread that performs
    the render, read from whatever thread handles a GET /api/jobs/{id} -
    `_lock` only guards against torn reads/writes between those two, it is
    not a queueing or scheduling mechanism.

    `cancel` is the token the work in flight is checking, or None for a kind
    that cannot be stopped at all (`upload`, `connect` - see KINDS). None is
    the honest value there, not a token nobody consults: a route or a screen
    that finds no token cannot offer a button that silently does nothing.
    The starter that supports stopping sets it; nothing here creates one.

    `finished` is set by `_finishing` once this job's runner is over,
    strictly after that runner's whole `finally` has run - which a terminal
    `status`, set inside the runner's `try`, does not imply (CLAUDE.md, "A
    TERMINAL JOB STATUS IS NOT A RELEASED LOCK"). It says the release was
    attempted, never that it succeeded; `EventLock.is_held()` is still the
    only way to ask about the event itself.
    """

    def __init__(self, job_id: str, kind: str = "job", log_path: str | None = None):
        self.id = job_id
        self.kind = kind
        self.log_path = log_path
        self.cancel: CancelToken | None = None
        self.status = "running"  # "running" | "done" | "failed" | "stopped"
        self.results: dict[str, ClipResult] = {}
        self.log: list[str] = []
        self.finished = threading.Event()
        self._lock = threading.Lock()

    def record(self, name: str, status: str, reason: str | None, line: str) -> None:
        # `reason`/`line` here can carry a caught exception's own message
        # verbatim (every `except Exception` site above builds `reason =
        # f"{type(error).__name__}: {error}"`) - and a yt-dlp failure
        # (RuntimeError(tail[0]), see stream_transcribe.py) can put a signed
        # googlevideo URL with a sig/lsig token straight into that message.
        # Elide it here, once, for every caller - the job's own log, its
        # snapshot (surfaced to the studio UI) and the central "job ...
        # done/failed" summary line all read from `reason`/`line`.
        if reason is not None:
            reason = logsetup.shorten_urls(reason)
        line = logsetup.shorten_urls(line)
        with self._lock:
            self.results[name] = ClipResult(status=status, reason=reason)
            self.log.append(line)
        level = logging.ERROR if status == "failed" else logging.INFO
        job_logger(self).log(level, "%s", line)

    def finish(self, status: str) -> None:
        with self._lock:
            self.status = status

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "results": {name: {"status": r.status, "reason": r.reason}
                            for name, r in self.results.items()},
                "log": list(self.log),
                "log_name": (Path(self.log_path).name
                             if self.log_path is not None else None),
            }


# Keep the in-memory job map bounded: an operator session starts a handful of
# jobs, but a misbehaving (or hostile, via CSRF) client could start many. Evict
# the oldest FINISHED jobs once past this cap so the map cannot grow without end.
MAX_JOBS = 200


class JobStore:
    """Every job this process has started, keyed by id. One instance per
    app (see `studio.api.create_app`) - never a bare module-level dict, so
    two apps in the same test run (or, in principle, two events) never see
    each other's jobs. Bounded to MAX_JOBS by evicting the oldest terminal jobs.
    Also tracks which channels have an in-flight connect, so a duplicate connect
    is refused rather than spawning a second OAuth thread."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._active_connects: set[str] = set()
        self._lock = threading.Lock()

    def create(self, kind: str = "job") -> Job:
        job_id = uuid.uuid4().hex
        job = Job(job_id, kind=kind, log_path=_open_job_log(job_id, kind))
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()
        return job

    def _evict_locked(self) -> None:
        # dict preserves insertion order; drop the oldest jobs that are OVER
        # first, never a still-running one, until back within the cap.
        #
        # "Over" is spelled `!= "running"`, not a list of terminal statuses,
        # and that is the fix for a real defect rather than a stylistic
        # preference: this used to read `in ("done", "failed")`, written
        # before `stopped` existed, so stopping jobs was enough to push the
        # store past MAX_JOBS indefinitely - a cap defeated by an ordinary
        # supported action. A status that is not "running" is finished
        # whatever it is called, including one a later version adds, which
        # is the only spelling that cannot go stale the same way again.
        if len(self._jobs) <= MAX_JOBS:
            return
        for job_id in list(self._jobs):
            if len(self._jobs) <= MAX_JOBS:
                break
            if self._jobs[job_id].status != "running":
                del self._jobs[job_id]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def begin_connect(self, channel_id: str) -> bool:
        """Reserve an in-flight connect for a channel. False if one is already
        running (the caller should refuse the duplicate)."""
        with self._lock:
            if channel_id in self._active_connects:
                return False
            self._active_connects.add(channel_id)
            return True

    def end_connect(self, channel_id: str) -> None:
        with self._lock:
            self._active_connects.discard(channel_id)

    def any_running(self) -> bool:
        """True if any job is still running or a connect is in flight - the
        guard the workspace switch/create/copy routes use to refuse
        re-rooting mid-operation."""
        with self._lock:
            if any(job.status == "running" for job in self._jobs.values()):
                return True
            return bool(self._active_connects)


def _finishing(job: Job, fn):
    """Wrap `fn` so `job.finished` is set in a `finally` AROUND it - outside
    the runner, so the event is set strictly after the runner's whole
    `finally` has RUN. That is the exact claim: the release and the log
    close were ATTEMPTED, not that either succeeded - a release that raises
    (see TestTheReleaseAndTheLogDoNotStrandEachOther) leaves the lock file on
    disk and still sets this. It is strictly more than a terminal
    `job.status`, which is set inside the runner's `try` with the whole
    `finally` still ahead of it.

    `try`/`finally`, never `try`/`except`: a raising runner still signals, and
    its exception still reaches threading's excepthook unchanged.
    """
    def run():
        try:
            fn()
        finally:
            job.finished.set()
    return run


def _start_thread(job: Job, target, args=()) -> threading.Thread:
    """Start `job`'s background thread, NAMED (an anonymous daemon thread is
    unidentifiable in a stack dump), signalling completion via `_finishing`."""
    thread = threading.Thread(target=_finishing(job, partial(target, *args)),
                              name=f"job-{job.kind}-{job.id[:8]}", daemon=True)
    thread.start()
    return thread


def _target_clips(profile: Profile, clip_names: list[str] | None) -> list[tuple[str, Path]]:
    """Named clips render exactly what was asked, even a discarded one -
    the operator picked it on purpose. The default set (no names given)
    mirrors `cmd_render`: every clip directory that exists, discarded ones
    filtered out inside `_render_one` below, same as `cmd_render`'s own
    `if edit.status == editorial.DISCARDED: continue`.

    A NAMED clip goes through `clipstore.clip_dir_by_name`, which validates
    it as one safe path segment - these names arrive in a request body (a
    render's `clips` list, a queue entry's `clips`) and used to be joined
    onto `clips_dir` raw; see that function's own docstring for what was
    measured. The default set needs no such check: those names are read back
    off disk, not supplied by anyone.
    """
    if clip_names:
        return [(name, clipstore.clip_dir_by_name(profile.event_dir, name))
                for name in clip_names]
    return [(d.name, d) for d in clipstore.iter_clip_dirs(profile.event_dir)]


def _refuse_unusable_clip_names(profile: Profile, *names: str) -> None:
    """Raises ValueError for a clip name that is not one safe path segment,
    BEFORE a starter takes the event lock or spawns a thread.

    The thread would refuse it anyway - every path it builds now goes through
    `clipstore.clip_dir_by_name` - but only after the lock had been acquired,
    so a malformed name would cost the event a lock/release cycle and the
    operator a job that exists purely in order to fail. Refusing here means a
    route answers 400 at the click, and a queue entry is failed with its
    reason without ever being claimed.
    """
    for name in names:
        clipstore.clip_dir_by_name(profile.event_dir, name)


def _render_one(profile: Profile, directory: Path, name: str, skip_discarded: bool,
                job: Job, cancel: CancelToken | None = None) -> None:
    try:
        clip = clipstore.read_clip(directory)
        if clip.get("error"):
            job.record(name, "failed", clip["error"], f"ERROR: {name}: {clip['error']}")
            return
        edit = editorial.load(directory)
        if skip_discarded and edit.status == editorial.DISCARDED:
            job.record(name, "skipped", "discarded", f"skipped (discarded): {name}")
            return

        hook = editorial.effective_title(edit, clip.get("hook", name))
        target = clipstore.short_path(directory)
        # Same subtitle pipeline the CLI's cmd_render uses (see
        # yt_shorts.subtitle_pipeline) - without this, a studio-initiated
        # render would throw away the operator's own editorial
        # corrections, which defeats the entire point of the studio.
        # The subtitle pipeline degrades to "no subtitles" for one clip rather
        # than failing the render - deliberate - but from here that used to be
        # SILENT: its notes went to sys.stderr, which this background thread
        # has no reader for, so a render that lost every caption reported
        # nothing anywhere and simply succeeded. An overlapping word list does
        # exactly that (subtitle_track refuses the whole list), and the
        # transcript editor lets an operator create one with a dragged timing.
        #
        # So the notes go two places, because two different people look in two
        # different places: the job's own logger writes them to
        # logs/jobs/render-<id>.log, and `notes` carries them into the clip's
        # RESULT, which the render panel already renders as `result.reason`.
        notes: list[str] = []
        provider = make_subtitle_provider(
            directory, edit, clip, hook, profile.config,
            logger=job_logger(job), on_note=notes.append)
        render.build_short(
            render.source_for_clip(clip, edit), hook, profile.channel["footer"],
            str(target), profile.config, str(directory), keep_raw=True,
            subtitle_provider=provider, cancel=cancel)
        # The freshly composed short IS the new master, so any master left
        # from before this render is stale - forget_applied drops it AND the
        # state file, then ensure_applied promotes this composition and cuts.
        # Without this pair a render silently discards the operator's trim.
        trim.forget_applied(directory)
        trim.ensure_applied(directory, edit, logger=job_logger(job),
                            on_note=notes.append)
        # Still "done" - the short itself rendered, and calling it failed would
        # be a lie that also breaks the kept/rendered flow. The reason is what
        # makes the loss visible.
        summary = "; ".join(notes)
        job.record(name, "done", summary or None,
                   f"done: {name}" + (f" ({summary})" if summary else ""))
    except Stopped:
        # A HARD stop can now land mid-clip - inside build_short's own
        # ffmpeg/yt-dlp child, via render.compose's `run_cancellable` - not
        # only between clips as `_run`'s own loop check covers. Re-raised
        # rather than swallowed here: the blanket `except Exception` below
        # would record this clip as `failed`, which is exactly the "lying
        # button" this design forbids - a stop is not a failure, same stance
        # as `_run_detect`/`_run_trim`. `_run` catches it and marks the job
        # `stopped` instead.
        raise
    except Exception as error:  # noqa: BLE001 - one failed clip must never abort the job
        reason = f"{type(error).__name__}: {error}"
        job.record(name, "failed", reason, f"ERROR: {name}: {reason}")


def _log_terminal(job: Job) -> None:
    """One summary line to the central studio log once a job reaches a
    terminal status - the job's own log carries the full narrative, this is
    just enough for the central log to say the job happened."""
    _STUDIO_LOGGER.info("job %s (%s) %s", job.id, job.kind, job.status)


def _run(profile: Profile, job: Job, clip_names: list[str] | None,
        event_lock: EventLock, cancel: CancelToken | None = None,
        progress=None) -> None:
    job_logger(job).info("start: render %s", profile.event_dir.name)
    try:
        skip_discarded = not clip_names
        stopped = False
        targets = _target_clips(profile, clip_names)
        for position, (name, directory) in enumerate(targets):
            # BETWEEN clips, and outside _render_one's own try/except: that
            # handler catches Exception per clip (one failed clip must never
            # abort a run), so a stop signalled from inside it would be
            # recorded as that clip failing and the loop would carry on -
            # the same reason moment_scan.scan checks a predicate rather
            # than catching an exception. A clip already rendered stays
            # `done`: it really did render.
            if cancel is not None and cancel.stop_requested:
                stopped = True
                job_logger(job).info(
                    "stopped after %d clip(s); %s and the rest were not started",
                    len(job.results), name)
                break
            try:
                _render_one(profile, directory, name, skip_discarded, job, cancel=cancel)
            except Stopped:
                # A hard stop landed WHILE this clip's own ffmpeg/yt-dlp
                # child was running, not between clips - the check above
                # only ever catches a soft stop before the next clip starts.
                # Same "stopped outranks failed" stance as the rest of this
                # loop; this clip is simply not in `results` at all, the
                # same as any clip the loop never reached.
                stopped = True
                job_logger(job).info(
                    "stopped mid-render of %s (%d clip(s) completed)",
                    name, len(job.results))
                break
            # AFTER the clip, counting clips ACCOUNTED FOR - a skipped or
            # failed one included, since neither will be looked at again on
            # this run. Same "done" semantics stream_transcribe and
            # moment_scan report with, so the queue's reading means one
            # thing whichever kind wrote it. Not reached for a clip a stop
            # cut short: `break` above skips it, which is right, because
            # that clip was not finished.
            if progress is not None:
                progress(position + 1, len(targets))
        any_failed = any(r.status == "failed" for r in job.results.values())
        if stopped:
            # `stopped` outranks `failed`: the run did not finish, so
            # reporting it as a completed-with-failures render would claim
            # every remaining clip had been considered. Any failures are
            # still visible per clip in `results`.
            job.finish("stopped")
        else:
            job.finish("failed" if any_failed else "done")
        job_logger(job).info("summary: %s (%d clips)", job.status, len(job.results))
        _log_terminal(job)
    finally:
        # The release goes FIRST in the `finally` of every one of the five
        # starters that take this lock, ahead of whatever logging is left,
        # and the ordering is deliberate: the event is done being written
        # the moment the records above were made, and `finish_job_log`
        # GZIPS a file. Holding an event's lock while a log is compressed is
        # dead time that costs the next job for that event a LockError, a
        # `defer` and a re-queue. THIS function is the one exception to the
        # "ahead of the log calls" reading - two of its three log calls sit
        # above, inside the `try`, so only the gzip follows the release here;
        # the other four starters do all of their logging after it.
        #
        # The nested try/finally is not decoration. Ordering two best-effort
        # steps means whichever goes first can strand the other: with the
        # release LAST, a raise out of a log call skipped it and left a lock
        # file behind for the stale-pid takeover to clear - and with it FIRST
        # but unguarded, an OSError out of `unlink` (a PermissionError, a
        # read-only volume) would leave this job's log handler open and its
        # file never compressed. Neither may depend on the other having
        # succeeded, which is what nesting buys and ordering alone cannot.
        #
        # None of this closes the status/lock gap: `job.finish(...)` runs
        # inside the `try` above, so a reader that sees a terminal status can
        # still find the lock held for an instant. Anything that needs "the
        # event is free" must ask `EventLock.is_held()` rather than infer it
        # from a job status. `start_connect_job` has the same shape for its
        # own connect guard.
        try:
            event_lock.release()
        finally:
            finish_job_log(job)


def _run_detect(profile: Profile, job: Job, video_id: str, stream_title: str,
                event_lock: EventLock, detect_fn,
                cancel: CancelToken | None = None, progress=None) -> None:
    """Runs detect_moments in the background and records ONE "detect" result,
    never one per clip: detect_moments creates no clip directories at all any
    more (see its own module docstring - a clip exists only once the operator
    picks a window in the studio), it returns the PATH of the analysis it
    wrote. This used to do `moment_count = len(names)` on that return value,
    left over from when detect_moments returned a list of clip names - which
    raised `TypeError: object of type 'PosixPath' has no len()` on every real
    detect job, reporting the job as failed even though the analysis had
    already been written correctly to disk.
    """
    job_logger(job).info("start: detect %s", video_id)
    moment_count = 0
    try:
        workspace_root = _resolve_workspace().root
        # detect_fn is `detect_moments` by default, which accepts a `logger`
        # kwarg (see detect.py) and routes ALL of its own log calls through
        # it. Passing this job's own logger means detect_moments's
        # word/chunk-count diagnosis lands in the file the studio actually
        # shows for this job, not only in the central app log. `progress`
        # likewise lands per-window progress in that same file rather than
        # nowhere at all.
        # The cancel kwarg only when there is a token, so a detect_fn
        # injected by a test keeps being called with the keywords it
        # already accepts - the same rule detect_moments applies to its
        # own transcriber one layer down.
        extra = cancel_kwargs(cancel)

        def report(done, total):
            # Two readers, one callback: the job's own log file (where an
            # operator reading the narrative looks) and, when the worker
            # gave us one, the queue entry (where the Jobs screen looks).
            # The log line came first and stays unconditional - a detect
            # started from a route rather than the queue still narrates.
            job_logger(job).info("window %d/%d", done, total)
            if progress is not None:
                progress(done, total)

        path = detect_fn(video_id, workspace_root,
                         profile.config, stream_title=stream_title,
                         logger=job_logger(job), progress=report, **extra)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        engine = data.get("engine", "?")
        moment_count = len(data.get("moments", []))
        failed_windows = len(data.get("missing_windows", []))
        note = f"engine={engine}, {moment_count} moment(s)"
        if failed_windows:
            note += f", {failed_windows} window(s) failed"
        # reason carries the same note as the log line (unlike _render_one,
        # where a None reason means "no note to show") - a detect job has no
        # per-clip UI panel of its own, so `reason` here IS how the studio
        # would surface the engine/count summary if it ever renders one.
        job.record("detect", "done", note, note)
        job.finish("done")
    except Stopped as stop:
        # BEFORE the blanket handler, and that order is the point: a stop is
        # what the operator asked for, and reporting it as `failed` would
        # send them looking for a cause that does not exist. Every window
        # that was scored is cached (see detect.WindowCache), so re-running
        # pays only for what was never reached.
        job.record("detect", "stopped", str(stop), f"stopped: {stop}")
        job.finish("stopped")
    except Exception as error:  # noqa: BLE001 - one failed detect must not kill the runner
        reason = logsetup.shorten_urls(f"{type(error).__name__}: {error}")
        job.record("detect", "failed", reason, f"ERROR: {reason}")
        job.finish("failed")
    finally:
        try:
            event_lock.release()   # first, and guarded - see _run's own note
        finally:
            job_logger(job).info("summary: %s (%d moments)", job.status, moment_count)
            _log_terminal(job)
            finish_job_log(job)


_STUDIO_DETECT_FN = partial(detect_moments, transcriber=require_cached_transcript)


def start_detect_job(profile: Profile, job_store: JobStore, video_id: str,
                     stream_title: str, *, detect_fn=None,
                     cancel: CancelToken | None = None, progress=None) -> Job:
    """Acquires the event lock, registers a Job, and detects moments in a thread.

    Same guarantee as start_render_job: the event lock is taken HERE,
    synchronously, so a second detect (or a render) for this event is refused
    with LockError before this returns, not after a thread has started.

    Task 4: the studio's detect no longer transcribes on its own. The
    DEFAULT `detect_fn` is `detect_moments` with its `transcriber` bound to
    `detect.require_cached_transcript` - see detect.py's own module
    docstring on why this is the studio's policy and not detect_moments's.
    A stream with no cached transcript now fails this job with
    `detect.TranscriptNotCached` (recorded as the job's `reason`, same as
    any other exception - see `_run_detect`) rather than silently spending
    two hours of decode a background job nobody is watching. Get a
    transcript first via `start_transcribe_job` - which an operator reaches
    by queueing a `transcribe` job (`POST /api/jobs`).

    Task 6 finished that change: `studio.api.post_detect` no longer passes
    `detect_fn=detect_moments`, so this default is what an operator's detect
    button actually runs now, and both callers (the route and the queue
    worker) behave identically.

    `detect_fn=None` means that default, resolved HERE rather than bound as
    the parameter's own default value - a default expression is evaluated
    once at def time, so `_STUDIO_DETECT_FN` could then never be substituted
    (by a test, or by a future policy change) without also changing every
    caller. The module attribute is read at CALL time instead, the same
    reason `studio.worker` calls each starter through the module rather than
    through a bound name.

    `progress`, when given, is called `progress(done, total)` once per
    scanned window - `KINDS["detect"].progress_unit` is what names those
    numbers "window" for a screen. The worker passes one so the queue
    entry's own reading advances; a caller that passes none still gets the
    per-window line in the job's log, where that narrative always went.
    """
    detect_fn = _STUDIO_DETECT_FN if detect_fn is None else detect_fn
    event_lock = EventLock(profile.event_dir)
    event_lock.acquire()

    job = job_store.create("detect")
    job.cancel = cancel
    _start_thread(job, _run_detect,
                  (profile, job, video_id, stream_title, event_lock, detect_fn,
                   cancel, progress))
    return job


def _run_transcribe(profile: Profile, job: Job, video_id: str,
                    event_lock: EventLock, transcribe_fn,
                    cancel: CancelToken | None = None, progress=None) -> None:
    """Runs transcribe_fn in the background and records ONE "transcribe"
    result. Writes only streams/<video_id>/transcript.json and its chunk
    cache (see stream_transcribe.transcribe_stream) - never moments.json and
    never a clip; this job is the whole-stream-transcription half Task 4
    split out of detection, so scoring what it produced is a SEPARATE job
    (start_detect_job) the operator asks for afterwards, on purpose."""
    job_logger(job).info("start: transcribe %s", video_id)
    word_count = 0
    try:
        workspace_root = _resolve_workspace().root
        # Same cancel-forwarding idiom every other starter here uses (see
        # cancel_kwargs's own docstring) - a transcribe_fn injected by a test
        # keeps being called with exactly the keywords it already accepts.
        extra = cancel_kwargs(cancel)
        if progress is not None:
            # Conditionally, for exactly the reason `cancel_kwargs` is
            # conditional: `transcribe_fn` is injected, and every fake in
            # the suite was written before this keyword existed. Only a
            # caller that actually wants a reading changes the call shape.
            extra["progress"] = progress
        transcript = transcribe_fn(video_id, workspace_root,
                                   glossary=profile.config.get("glossary", GLOSSARY_EMPTY),
                                   **extra)
        word_count = len(transcript.words)
        missing = len(getattr(transcript, "missing_chunks", []) or [])
        note = f"{word_count} word(s)"
        if missing:
            note += f", {missing} chunk(s) missing"
        job.record("transcribe", "done", note, note)
        job.finish("done")
    except Stopped as stop:
        # BEFORE the blanket handler, same reason as _run_detect: the
        # operator asked for this, and every chunk decoded so far is already
        # cached (see stream_transcribe.transcribe_stream), so the re-run
        # pays only for what was never reached.
        job.record("transcribe", "stopped", str(stop), f"stopped: {stop}")
        job.finish("stopped")
    except Exception as error:  # noqa: BLE001 - one failed transcribe must not kill the runner
        reason = logsetup.shorten_urls(f"{type(error).__name__}: {error}")
        job.record("transcribe", "failed", reason, f"ERROR: {reason}")
        job.finish("failed")
    finally:
        try:
            event_lock.release()   # first, and guarded - see _run's own note
        finally:
            job_logger(job).info("summary: %s (%d words)", job.status, word_count)
            _log_terminal(job)
            finish_job_log(job)


def start_transcribe_job(profile: Profile, job_store: JobStore, video_id: str, *,
                         transcribe_fn=transcribe_stream,
                         cancel: CancelToken | None = None,
                         progress=None) -> Job:
    """Acquires the event lock, registers a Job, and transcribes a whole
    stream in a background thread. Returns the Job immediately, same
    fire-and-poll shape as start_detect_job/start_render_job.

    Task 4: transcription is now a job kind of its own, split out of
    detection (see detect.py's module docstring on why) - this writes only
    streams/<video_id>/transcript.json and its chunk cache, never
    moments.json and never a clip. Same event-lock guarantee as
    start_detect_job: a second transcribe, detect, or render for this event
    is refused with LockError before this returns, not after the thread has
    started - the lock is scoped to the EVENT, exactly like every other job
    starter here, even though what this job writes lives under
    streams/<video_id>/ rather than the event directory (see detect.py's own
    module docstring on why the STREAM's derived data lives outside any one
    event: two events that look at the same stream share it).

    `transcribe_fn` is injected (default `stream_transcribe.transcribe_stream`)
    so this - and a route built on it - tests without the network or a real
    Whisper decode, the same as `detect_fn` on start_detect_job.

    `progress`, when given, is forwarded to `transcribe_fn` and called
    `progress(done, total)` once per chunk (see
    `stream_transcribe.transcribe_stream`); `KINDS["transcribe"].
    progress_unit` is what names those numbers "chunk" for a screen. It is
    forwarded ONLY when there is one, so an injected `transcribe_fn`
    written before this keyword existed keeps being called with exactly the
    keywords it already accepts - the same rule `cancel_kwargs` follows.
    """
    event_lock = EventLock(profile.event_dir)
    event_lock.acquire()

    job = job_store.create("transcribe")
    job.cancel = cancel
    _start_thread(job, _run_transcribe,
                  (profile, job, video_id, event_lock, transcribe_fn, cancel, progress))
    return job


def _default_uploader(profile: Profile, directory: Path, clip: dict,
                      edit: editorial.Edit, when: str, *,
                      visibility: str = "private", publish_at: str | None = None) -> dict:
    """Composes the real upload: auth -> service -> metadata -> insert -> record.

    Imports google-touching modules lazily (via the adapter), so this module
    stays import-free of google at module scope. Returns the upload record dict.

    ``visibility``/``publish_at`` are the operator's explicit, per-upload
    choice (see api.py's post_upload confirm gate) threaded straight into
    build_metadata. The record saved afterwards is the ACTUAL privacy YouTube
    reports back (`result.privacy_status`), not a hard-coded "private" -
    YouTube is the authority on what actually happened (see
    youtube_upload.upload_short's own docstring on this).
    """
    from datetime import datetime, timezone

    from ..auth import load_credentials
    from ..google_oauth import GoogleOAuth, build_service
    from ..quota import QuotaTracker
    from ..youtube_upload import build_metadata, upload_short

    channel_id = profile.channel["id"]
    auth_dir = _resolve_workspace().root / "auth"
    creds = load_credentials(channel_id, auth_dir=auth_dir, oauth=GoogleOAuth())
    if creds is None:
        raise RuntimeError(
            f"channel {channel_id} is not connected; run: bin/yt-shorts auth "
            f"{profile.channel_name}")
    service = build_service(creds)
    metadata = build_metadata(clip, edit, profile.config,
                              visibility=visibility, publish_at=publish_at)
    result = upload_short(clipstore.short_path(directory), metadata, service=service)
    upload_record.save(directory, result.video_id, result.url,
                       result.privacy_status, when=when)
    QuotaTracker(auth_dir, channel_id).book_insert(datetime.now(timezone.utc))
    return {"video_id": result.video_id, "url": result.url}


def start_upload_job(profile: Profile, job_store: JobStore, name: str, *,
                     force: bool = False, uploader=_default_uploader,
                     when: str | None = None, visibility: str = "private",
                     publish_at: str | None = None) -> Job:
    """Uploads one kept, rendered clip in a background thread, under the event lock.

    The kept/rendered/already-uploaded guards are the caller's (studio.api) to
    check synchronously so a refusal is a 409 before this returns. `uploader` is
    injected so the route and this function test without google or the network.
    ``visibility``/``publish_at`` are threaded straight through to the uploader
    (see `_default_uploader`) - the confirm gate for a non-private or
    scheduled upload is the route's job (api.py's post_upload), not this
    function's.

    `name` is refused here if it is not one safe path segment (see
    `_refuse_unusable_clip_names`), before the lock and before the thread.
    """
    from datetime import datetime, timezone

    _refuse_unusable_clip_names(profile, name)
    event_lock = EventLock(profile.event_dir)
    event_lock.acquire()
    job = job_store.create("upload")
    stamp = when or datetime.now(timezone.utc).isoformat()

    def run():
        job_logger(job).info("start: upload %s", name)
        try:
            directory = clipstore.clip_dir_by_name(profile.event_dir, name)
            clip = clipstore.read_clip(directory)
            edit = editorial.load(directory)
            record = uploader(profile, directory, clip, edit, stamp,
                              visibility=visibility, publish_at=publish_at)
            job.record(name, "done", None, f"uploaded: {name} -> {record.get('url')}")
            job.finish("done")
        except Exception as error:  # noqa: BLE001 - reported into the job
            reason = f"{type(error).__name__}: {error}"
            job.record(name, "failed", reason, f"ERROR: {reason}")
            job.finish("failed")
        finally:
            try:
                event_lock.release()   # first, and guarded - see _run's note
            finally:
                job_logger(job).info("summary: %s", job.status)
                _log_terminal(job)
                finish_job_log(job)

    _start_thread(job, run)
    return job


def _default_connector(channel_id: str, force: bool) -> None:
    """Runs OAuth consent for a channel and stores its token (the real flow).

    Imports google-touching modules lazily; opens the operator's browser via the
    adapter's run_local_server and blocks until they consent. The tool never sees
    the password - Google's consent page handles it - it only stores the token.
    ``force`` re-consents even when a valid token already exists (switch account).
    """
    from ..auth import authorize
    from ..google_oauth import GoogleOAuth
    auth_dir = _resolve_workspace().root / "auth"
    authorize(channel_id, auth_dir=auth_dir, oauth=GoogleOAuth(), force=force)


def start_connect_job(profile: Profile, job_store: JobStore, channel_id: str, *,
                      force: bool = False, connector=_default_connector) -> Job:
    """Runs the browser OAuth consent for a channel in a background thread.

    Consent blocks on the operator interacting with Google in their browser, far
    too long for an HTTP request, so it is a background job the studio polls -
    like render/detect/upload. ``force`` re-consents even with a valid token (to
    switch account). `connector` is injected so the route tests without google or
    a real consent flow. No event lock: connecting writes to the workspace auth
    dir, not the event - but a per-channel in-flight guard refuses a duplicate
    connect (which would otherwise spawn a second OAuth thread and browser popup),
    raising LockError the route maps to 409.
    """
    if not job_store.begin_connect(channel_id):
        raise LockError(f"a connect for {channel_id} is already in progress")

    job = job_store.create("connect")

    def run():
        job_logger(job).info("start: connect %s", channel_id)
        try:
            connector(channel_id, force)
            job.record(channel_id, "done", None, f"connected: {channel_id}")
            job.finish("done")
        except Exception as error:  # noqa: BLE001 - reported into the job
            reason = f"{type(error).__name__}: {error}"
            job.record(channel_id, "failed", reason, f"ERROR: {reason}")
            job.finish("failed")
        finally:
            # The release goes FIRST, ahead of the three log calls, and that
            # ordering is deliberate: the connect is over the moment the
            # connector returned and the job was recorded, and the last of
            # those calls GZIPS a file. Holding every other connect for this
            # channel out while a log is compressed serves nothing, and it
            # made the window between "the job reports done" and "a second
            # connect is accepted" wide enough to be hit in practice - a 409
            # telling an operator a connect is in progress when it is not.
            # Guarded like the event lock's release (see `_run`), for the same
            # reason - though `end_connect` is a `set.discard` under a lock and
            # cannot in fact raise, so here the nesting is uniformity rather
            # than protection.
            #
            # It cannot be closed entirely from here (`job.finish` runs inside
            # the try, above). There is no PUBLIC per-channel predicate to ask
            # instead - `_active_connects` is private - so a test that needs
            # "this connect is fully over" uses `job_store.any_running()`,
            # which is workspace-GLOBAL (any running job, any channel's
            # in-flight connect) and answers the narrower question only
            # because the store it is asked of holds nothing else. See
            # TestConnectDedupe.
            try:
                job_store.end_connect(channel_id)
            finally:
                job_logger(job).info("summary: %s", job.status)
                _log_terminal(job)
                finish_job_log(job)

    _start_thread(job, run)
    return job


def start_render_job(profile: Profile, job_store: JobStore,
                     clip_names: list[str] | None, *,
                     cancel: CancelToken | None = None, progress=None) -> Job:
    """Acquires the event lock, registers a new Job, and starts rendering
    in a background thread. Returns the Job immediately - it is very
    likely still "running" by the time this returns.

    Raises `yt_shorts.lock.LockError` if a render (studio or CLI) already
    holds the lock for this event; the caller (`studio.api`) turns that
    into a 409. The lock is acquired HERE, synchronously, before any
    thread starts - not inside the thread - specifically so that refusal
    happens before this function ever returns, matching the CLI's own
    "refuses immediately" guarantee (see yt_shorts.lock's own docstring).

    `cancel`, when given, is recorded on the Job (so whoever holds the id
    can ask it to stop) and checked between clips - see `_run`. Its
    COOPERATIVE stop point is `KINDS["render"].stop_point`, "after the
    current clip" - but `hard_stop_allowed` is also True, and a hard stop
    reaches further than that: `_render_one` forwards this same token into
    `render.build_short`, so a kill mid-clip terminates whichever ffmpeg or
    yt-dlp child is running (see `render.compose`'s own `cancel` plumbing)
    rather than only taking effect once that clip finishes.

    `progress`, when given, is called `progress(done, total)` after each
    clip is accounted for - rendered, skipped or failed;
    `KINDS["render"].progress_unit` names those numbers "clip" for a
    screen. The count is the clips this run will consider, which for a
    default run is every clip directory in the event.

    A named clip is refused here if it is not one safe path segment (see
    `_refuse_unusable_clip_names`), before the lock and before the thread.
    """
    _refuse_unusable_clip_names(profile, *(clip_names or ()))
    event_lock = EventLock(profile.event_dir)
    event_lock.acquire()

    job = job_store.create("render")
    job.cancel = cancel
    _start_thread(job, _run,
                  (profile, job, clip_names, event_lock, cancel, progress))
    return job


def _run_trim(profile: Profile, job: Job, name: str, event_lock: EventLock,
              cancel: CancelToken | None = None) -> None:
    log = job_logger(job)
    log.info("start: trim %s", name)
    try:
        directory = clipstore.clip_dir_by_name(profile.event_dir, name)
        edit = editorial.load(directory)
        notes: list[str] = []
        extra = cancel_kwargs(cancel)
        changed = trim.ensure_applied(directory, edit, logger=log,
                                      on_note=notes.append, **extra)
        summary = "; ".join(notes)
        job.record(name, "done", summary or None,
                   f"done: {name}" + ("" if changed else " (already applied)"))
        job.finish("done")
    except Stopped as stop:
        # Before the blanket handler, same reason as _run_detect: a stopped
        # cut left short.mp4 exactly as it was (trim.ensure_applied restores
        # the deliverable and removes its scratch file), so there is nothing
        # to repair and nothing to call a failure.
        job.record(name, "stopped", str(stop), f"stopped: {name}: {stop}")
        job.finish("stopped")
    except Exception as error:  # noqa: BLE001 - a failed trim must not kill the studio
        reason = f"{type(error).__name__}: {logsetup.shorten_urls(str(error))}"
        job.record(name, "failed", reason, f"ERROR: {name}: {reason}")
        job.finish("failed")
    finally:
        try:
            event_lock.release()   # first, and guarded - see _run's own note
        finally:
            _log_terminal(job)
            finish_job_log(job)


def start_trim_job(profile: Profile, job_store: JobStore, name: str, *,
                   cancel: CancelToken | None = None) -> Job:
    """Applies a clip's trim in the background, under the SAME event lock a
    render takes: applying writes short.mp4, and so does a render.

    A cut has no interior safe point - it is one ffmpeg invocation - so a
    plain stop is honoured only before it starts, and a HARD stop terminates
    the ffmpeg child (see trim.ensure_applied). Either way the master is
    untouched and short.mp4 stays what it was.

    `name` is refused here if it is not one safe path segment (see
    `_refuse_unusable_clip_names`), before the lock and before the thread."""
    _refuse_unusable_clip_names(profile, name)
    event_lock = EventLock(profile.event_dir)
    event_lock.acquire()
    job = job_store.create("trim")
    job.cancel = cancel
    _start_thread(job, _run_trim, (profile, job, name, event_lock, cancel))
    return job


def _spawn(fn) -> None:
    """Thin seam over starting a background thread. Tests monkeypatch this
    to run `fn` synchronously (`lambda fn: fn()`), so a copy job's outcome
    is deterministic without polling a real thread - the same trick the
    other job starters don't need because they only ever run for real.

    It takes no `Job`, so it cannot use `_start_thread`: it is a seam a test
    replaces, and the substitute may not start a thread at all. The copy
    job's own signalling is wrapped around `run` by its caller instead, which
    is what makes it hold under either implementation of this function.
    """
    threading.Thread(target=fn, daemon=True).start()


def start_copy_job(job_store: JobStore, src: Path, parent: Path, name: str,
                   created: str, on_done, *, copier=None,
                   cancel: CancelToken | None = None) -> Job:
    """Clones an entire workspace (see `workspaces.copy_workspace` - the
    whole tree, including `auth/`) in a background thread and returns the
    Job immediately, same fire-and-poll shape as every other job starter
    here - a large workspace's copy can take a while.

    `on_done(new_path)` runs INSIDE the job thread, after the copy succeeds
    and before the job is marked "done"; the studio route passes `_select`
    so a successful copy re-roots the studio onto it. `copier` is injected
    for tests, same as `connector`/`uploader`/`detect_fn` above. No event
    lock: a workspace copy is not scoped to one event, and `_guard_reroot`
    (the caller's job) already refuses this while the CURRENT workspace has
    any job running.

    `cancel`, when given, is recorded on the Job and forwarded into the
    copier, which checks it before each file - `KINDS["copy"].stop_point`,
    "after the current file", is that check and nothing else. There is no
    hard stop (see KINDS): a copytree waits on no subprocess, so there is
    nothing a kill could reach that a graceful stop does not already.
    """
    copier = workspaces.copy_workspace if copier is None else copier
    job = job_store.create("copy")
    job.cancel = cancel

    def run():
        job_logger(job).info("start: copy %s", name)
        try:
            # Same conditional-forwarding idiom every other starter here uses
            # (see cancel_kwargs's own docstring): a copier injected by a test
            # keeps being called with exactly the keywords it already accepts.
            new_path = copier(src, parent, name, created, **cancel_kwargs(cancel))
            on_done(new_path)
            job.record(name, "done", None, f"copied workspace: {name} -> {new_path}")
            job.finish("done")
        except Stopped as stop:
            # Before the blanket handler, same reason as _run_detect/_run_trim:
            # the operator asked for this. copy_workspace removes the
            # half-copied target on its way out, so there is nothing to repair
            # and nothing to call a failure.
            job.record(name, "stopped", str(stop), f"stopped: {name}: {stop}")
            job.finish("stopped")
        except Exception as error:  # noqa: BLE001 - reported into the job, never swallowed
            reason = f"{type(error).__name__}: {error}"
            job.record(name, "failed", reason, f"ERROR: {reason}")
            job.finish("failed")
        finally:
            job_logger(job).info("summary: %s", job.status)
            _log_terminal(job)
            finish_job_log(job)

    _spawn(_finishing(job, run))
    return job
