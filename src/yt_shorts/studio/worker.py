"""The one thread that drives the job queue.

`job_queue.JobQueue` decides ORDER and ELIGIBILITY and spawns nothing; the
existing `studio.jobs.start_*_job` functions run the work and own their own
threads, logging, `EventLock` and URL redaction. This module is the seam
between them, and it reimplements neither: it translates a queue entry back
into the call the operator would otherwise have made from a route.

**`drain_once()` is the whole worker.** One pass: reap the jobs that
finished, then claim and start what may run. The thread (`start()`/`stop()`)
is a loop around it and nothing else - which is why almost every test of this
module runs with no thread at all, and why none of them sleeps waiting for a
scheduler.

**Nothing here starts - or WRITES - when `create_app()` is called.**
`create_app` builds a Worker and leaves it stopped; `bin/yt-shorts studio`
starts it. Over two thousand tests construct an app, and an app that acquired
an `EventLock`, spawned a thread or rewrote the plan on disk as a side effect
of construction would be a defect in every one of them. The last of those
three was true until a whole-branch review found it: `recover()` ran in
`__init__`.

Four rules this module exists to keep:

**A held `EventLock` leaves the entry QUEUED, with a reason - never failed.**
A CLI render holding the lock is a normal, temporary condition, so the entry
waits and says what it is waiting for. The lock is not acquired here: each
`start_*_job` takes it itself, synchronously, before its thread starts, so a
`LockError` out of the starter means nothing began. Two processes taking the
same file lock is exactly what `lock.py` refuses, so a worker that acquired
it first would make every starter refuse its own job.

There are two paths into that "waiting" state and both are needed.
`_blocked_by` is asked BEFORE the entry is claimed (`claim_next
(blocked_by=...)`), which is what stops a lock held for hours from costing
a claim and a `defer` - two rewrites of `jobs.json` - on every single pass;
`JobQueue.defer` still handles the race the pre-check cannot close, where
the lock is taken between the question and the starter's own acquire.

**A locked entry never blocks an entry for a different event.** A deferred
entry is passed over for the rest of the pass (`claim_next(skip=...)`), not
left claimed and not moved to the back of the queue.

**`start()` recovers the queue; construction does not.** Anything left
`running` by a process that died becomes `interrupted`, and an interrupted
entry never restarts by itself - only an explicit `retry` re-queues it,
because a detection run spends real money. That work moved OUT of
`__init__` (see `recover`): it writes entry states into a file another
process may own, which is not something building an app may do.

**The token a stop reaches is the JOB's own (`job.cancel`).** The worker
creates one per stoppable job, hands it to the starter, and then reads it
back off the Job when a stop is asked for rather than keeping a second copy
- so a starter that stopped recording its token would break stopping loudly
instead of leaving a button that silently does nothing. A kind whose Job
carries no token cannot be stopped at all (`upload`), and a hard stop is
refused where `KINDS[kind].hard_stop_allowed` is False rather than quietly
downgraded to a graceful one.

**Progress is the queue's shape, filled from two places.** The work
reports two numbers (`progress(done, total)` - `stream_transcribe` per
chunk, `moment_scan` per window, the render loop per clip) and this module
adds the unit from `jobs.KINDS[kind].progress_unit`, writing
`{"unit", "done", "total"}` onto the entry via `JobQueue.mark_running`.
A kind whose `progress_unit` is None is handed no callback at all and its
rows show nothing - a trim is one cut, and "1 of 1" for it is worse than
silence. `Worker._progress_reporter` is the one place a production
callback is built, which is why the "a reading must never cost the run"
guarantee lives there and not in each of the three producers.

A hard stop terminates the SUBPROCESS the work waits on (see
`cancel.run_cancellable`), never the thread - Python threads are not
killable, so the thread runs its own `finally` cleanup and reports
`stopped`. Nothing in this module tries to kill a thread.

**What a queue entry must carry, per kind.** `STARTERS` is the honest set
of kinds a route may enqueue (`connect` and `copy` are `queueable=False`,
so the queue itself refuses them). Every one of them needs `channel` and
`event`; a missing or wrong-typed parameter fails that ONE entry with a
reason and the pass carries on.

| kind | required | optional |
|---|---|---|
| `transcribe` | `channel`, `event`, `video_id` | - |
| `detect` | `channel`, `event`, `video_id` | `stream_title` |
| `render` | `channel`, `event` | `clips: [str]` (absent/empty = every clip) |
| `trim` | `channel`, `event`, `clip` | - |
| `upload` | `channel`, `event`, `clip` | `force: bool` |

`upload` additionally REFUSES `visibility` other than `"private"` and any
`publish_at` - see `_start_upload` for why a queue entry can carry no
confirmation. No params key may look like a secret; `JobQueue.enqueue`
refuses those itself.

**A stop route must go through `Worker.request_stop`, never
`JobQueue.mark_stopping`.** The queue can only move the entry to
`stopping`; it holds no cancel token and reaches no thread, so calling it
directly produces an entry that says a stop was asked for while nothing
whatsoever has told the work to stop - a button that lies. `request_stop`
reaches the token first and calls `mark_stopping` only then - and an entry
already sitting in `stopping` may still be ESCALATED to a hard stop, which
performs the kill and skips the mark rather than raising for something it
did (see `request_stop` itself). It raises
`QueueError` with a `kind` a route maps to a status: `invalid_state` (the
entry is not running here), `no_hard_stop` (`force=True` on a kind whose
`KINDS[...].hard_stop_allowed` is False - never quietly downgraded to a
graceful stop) and `not_stoppable` (the Job carries no token at all).
Those two are new here; `not_found`, `invalid_state`, `unknown_kind`,
`not_queueable` and `secret_in_params` come from the queue itself.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import cancel, job_queue, lock, logsetup, profile
from ..cancel import CancelToken
from ..job_queue import Entry, JobQueue
from . import jobs

# `lock`, `profile`, `cancel` and `job_queue` are imported as MODULES,
# never as `from ..lock import LockError`, and that is not a style
# preference. An `except SomeError` clause holds the class object it was
# handed at import time, while the raising module resolves the same name
# from its own globals at call time - and `importlib.reload` (which
# tests/test_profile.py performs) rebuilds those class objects in place, so
# a name bound here at collection time stops being the class actually
# raised. That already cost this module one silent defect: a `ProfileError`
# no `isinstance` check recognised escaped a whole pass. `lock.LockError`
# would have been worse and quieter - a held lock would have missed its
# handler, fallen through to `except Exception` and FAILED the entry
# instead of deferring it, violating this module's first rule with no
# noise at all. An attribute lookup on the module happens when the
# `except` is evaluated, so it always names the live class. The same
# reasoning applies to `cancel.Stopped` (a stop mislabelled a failure would
# violate this module's OTHER binding constraint - a stop is never
# relabelled a failure) and to `job_queue.QueueError` (defensive: a miss
# there just lets the exception escape instead of being logged and
# swallowed) - both are looked up the same way below, at `except`-evaluation
# time, rather than imported by name.

__all__ = ["DEFAULT_LIMITS", "STARTERS", "THREAD_NAME", "Starter", "Worker"]


# The spec's defaults: one CPU-bound job at a time (a transcribe, a render or
# a trim), three network-bound ones (detect, upload). A pool absent from this
# dict is unlimited - see job_queue.py's docstring, which also records that no
# shipped kind relies on that any more.
DEFAULT_LIMITS: dict[str, int] = {"cpu": 1, "net": 3}

# How long a pass waits before the next one. Nothing is computed in between,
# and a job that finishes is only noticed on the next pass, so this is the
# worst-case lag between "a render finished" and "the next one starts".
POLL_SECONDS = 1.0

THREAD_NAME = "ytshorts-worker"

_LOGGER = logging.getLogger("ytshorts.studio.worker")


class ParamError(ValueError):
    """A queue entry whose params cannot be turned into a call. Reported as
    the entry's own failure reason - never raised out of a pass, because one
    malformed entry must not stop the rest of the queue."""


def _required(params: dict, key: str) -> Any:
    value = params.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ParamError(f"missing required parameter {key!r}")
    return value


def _text(params: dict, key: str) -> str:
    value = _required(params, key)
    if not isinstance(value, str):
        raise ParamError(f"parameter {key!r} must be a string, not "
                         f"{type(value).__name__}")
    return value


# -- the per-kind translations ------------------------------------------
#
# Each takes the resolved profile, the job store, the entry's params, a
# cancel token (None for a kind that cannot be stopped) and a progress
# callback (None for a kind that reports nothing - see `KINDS[kind].
# progress_unit`), and returns the Job.
# They call `jobs.start_*_job` through the MODULE, not a bound name, so a
# test can replace one starter without the others - the same reason
# `studio.api` calls them that way.
#
# All five take `progress` even though two ignore it, for the same reason
# all five take `cancel`: `Starter.start` is called one way, and the two
# tables that decide what actually reaches the work (`KINDS[kind].
# progress_unit` here, `Starter.stoppable` below) are pinned against the
# starters' own signatures by `TestStarterTable` rather than by each
# translation quietly doing its own thing.

def _start_transcribe(event_profile, job_store, params: dict, cancel,
                      progress) -> jobs.Job:
    return jobs.start_transcribe_job(event_profile, job_store, _text(params, "video_id"),
                                     cancel=cancel, progress=progress)


def _start_detect(event_profile, job_store, params: dict, cancel,
                  progress) -> jobs.Job:
    # No `detect_fn`: the default requires a CACHED transcript and never
    # transcribes on its own. A queue entry runs hours later with nobody
    # watching, so silently starting an hour of Whisper decode off the back
    # of it would be the wrong thing to do - and since Task 6 dropped
    # `post_detect`'s `detect_fn=detect_moments` override, the HTTP route
    # behaves the same way, so there is no longer a difference between the
    # two paths to remember. An operator without a transcript queues a
    # `transcribe` job (`POST /api/jobs`) first.
    title = params.get("stream_title") or ""
    return jobs.start_detect_job(event_profile, job_store,
                                 _text(params, "video_id"), title, cancel=cancel,
                                 progress=progress)


def _start_render(event_profile, job_store, params: dict, cancel,
                  progress) -> jobs.Job:
    clips = params.get("clips")
    if clips is not None and not (isinstance(clips, list)
                                  and all(isinstance(c, str) for c in clips)):
        raise ParamError("parameter 'clips' must be a list of clip names or absent")
    # None (not []) is jobs.py's own spelling of "every clip in the event" -
    # an empty list would render nothing at all, silently.
    return jobs.start_render_job(event_profile, job_store, clips or None,
                                 cancel=cancel, progress=progress)


def _start_trim(event_profile, job_store, params: dict, cancel,
                progress) -> jobs.Job:
    # `progress` is accepted and dropped: a cut is one ffmpeg invocation
    # with nothing to count (KINDS["trim"].progress_unit is None), so the
    # worker never builds one to hand over in the first place.
    del progress
    return jobs.start_trim_job(event_profile, job_store, _text(params, "clip"),
                               cancel=cancel)


def _start_upload(event_profile, job_store, params: dict, cancel,
                  progress) -> jobs.Job:
    # A queued upload is PRIVATE, always. Anything more exposed is an
    # explicit, confirmed, per-upload operator choice (see CLAUDE.md, stage
    # E, and api.py's post_upload confirm gate) - and a queue entry carries
    # no confirmation: it was written at enqueue time and runs, possibly
    # hours later, with nobody watching. Refusing loudly is the only honest
    # option; quietly downgrading it to private would upload something the
    # operator asked to be public without telling them, and honouring it
    # would publish without the confirmation the gate exists to require.
    #
    # `progress` is accepted and dropped, same as `_start_trim`: the
    # resumable upload reports bytes to a layer this code does not watch,
    # so KINDS["upload"].progress_unit is None and no callback is built.
    del progress
    if params.get("visibility") not in (None, "private"):
        raise ParamError(
            f"a queued upload is always private, and this entry asks for "
            f"{params.get('visibility')!r}; a non-private upload has to be "
            f"confirmed per upload, so start it from the clip's own upload "
            f"panel instead")
    if params.get("publish_at") is not None:
        raise ParamError(
            "a queued upload cannot be scheduled; a publish time has to be "
            "confirmed per upload, so start it from the clip's own upload panel")
    return jobs.start_upload_job(event_profile, job_store, _text(params, "clip"),
                                 force=bool(params.get("force")))


@dataclass(frozen=True)
class Starter:
    """One kind's translation, plus whether its job can be stopped at all.

    `stoppable` is not a second opinion about `KINDS[kind].stop_point`: it
    says whether `start_*_job` TAKES a cancel token, which is the only thing
    that decides whether a token would reach the work. The two are pinned
    against each other in tests/test_studio_worker.py.
    """
    start: Callable[..., jobs.Job]
    stoppable: bool


STARTERS: dict[str, Starter] = {
    "transcribe": Starter(_start_transcribe, True),
    "render": Starter(_start_render, True),
    "trim": Starter(_start_trim, True),
    "detect": Starter(_start_detect, True),
    # No token: upload has no stop at any level (KINDS["upload"]), so there
    # is nothing here for a future stop route to reach - deliberately, not
    # by omission.
    "upload": Starter(_start_upload, False),
}

# What a Job's terminal status means for its queue entry. `stopped` is NOT
# `failed`: the operator asked for it.
_STATE_FOR_STATUS = {"done": "done", "failed": "failed", "stopped": "stopped"}


@dataclass
class _Running:
    kind: str
    job: jobs.Job


class Worker:
    """Drives one `JobQueue`. One thread, or none at all - see `drain_once`.

    ``profile_loader`` resolves ``"<channel>/<event>"`` to a `Profile`
    (default `profile.load`, looked up when the Worker is built rather than
    when this module was imported - see the note beside the imports).
    ``interval`` is the pause between passes of the thread; it has no effect
    on `drain_once`, which a test calls directly.
    """

    def __init__(self, queue: JobQueue, job_store: jobs.JobStore, *,
                 profile_loader=None,
                 interval: float = POLL_SECONDS, logger=None):
        self.queue = queue
        self._job_store = job_store
        self._load_profile = profile_loader or profile.load
        self._interval = interval
        self._log = logger or _LOGGER
        self._running: dict[str, _Running] = {}
        self._event_dirs: dict[str, Path] = {}
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        # Each call into the queue is atomic on its own (JobQueue holds its
        # own lock - see its docstring's "Two locks"). This one is coarser
        # and does a different job: it keeps a SEQUENCE of those calls
        # atomic as one decision - reap, then claim, then start, or read
        # the running job's token and only then `mark_stopping`. It is
        # public so a route that needs the same all-or-nothing view across
        # several calls can hold it too; a route making a single mutation
        # needs nothing.
        self.lock = threading.RLock()

    # -- recovery ---------------------------------------------------------

    def recover(self) -> list[Entry]:
        """Marks anything left `running`/`stopping` by a dead process
        `interrupted`, and says so. Returns what it recovered.

        Called by `start()`, and NOT by `__init__` - which is not a detail.
        Recovery WRITES entry states into another process's file, and doing
        it from a constructor made it a side effect of building an app:
        every `create_app()` in a ~2200-test suite rewrote whatever plan it
        found, and - the reason a review called it blocking - merely
        STARTING a second studio against a live workspace marked the first
        studio's genuinely-running two-hour transcription `interrupted`,
        a state whose own text says "it was running when the studio died".
        `lock.StudioLock` now refuses that second studio outright, but the
        rule stands on its own: recovery is something a studio ABOUT TO
        DRAIN the queue does, once, not something constructing an object
        does.
        """
        recovered = self.queue.recover()
        if recovered:
            self._log.warning(
                "%d queue entr%s left running by a previous process, marked "
                "interrupted (retry to re-queue): %s", len(recovered),
                "y was" if len(recovered) == 1 else "ies were",
                ", ".join(f"{e.kind}/{e.id}" for e in recovered))
        return recovered

    # -- one pass ---------------------------------------------------------

    def drain_once(self) -> None:
        """Reap what finished, then start what may run. Never raises for a
        defect in ONE entry - that entry is failed with its reason and the
        pass carries on, the same stance `cmd_render` takes per clip."""
        with self.lock:
            self._reap()
            self._start_claimable()

    def _reap(self) -> None:
        for entry_id, running in list(self._running.items()):
            status = running.job.status
            if status == "running":
                continue
            state = _STATE_FOR_STATUS.get(status)
            reason = _reason_from(running.job)
            if state is None:
                state, reason = "failed", (
                    f"the job ended with an unexpected status {status!r}")
            del self._running[entry_id]
            try:
                self.queue.mark_finished(entry_id, state, reason=reason)
            except job_queue.QueueError as error:
                # The entry is gone or no longer active (a hand-edited
                # jobs.json, most concretely). The job itself is finished
                # either way; say so rather than letting the pass die.
                self._log.warning("could not record job %s as %s: %s",
                                  running.job.id, state, error)

    def _start_claimable(self) -> None:
        deferred: set[str] = set()
        # Bounded defensively, not because correct code needs it: the
        # claim/defer loop's own termination rests entirely on `skip` ever
        # growing (a deferred entry is passed over for the rest of THIS
        # pass) or on claim_next eventually returning None. Both held before
        # this bound existed - this is insurance against a FUTURE regression
        # in either one (dropping `skip` here, or `claim_next` ignoring it),
        # which was reproduced and turns this into an infinite loop with no
        # trace: a hung worker thread, not a stack anyone can read. One
        # entry can be claimed-and-started, or deferred, at most once per
        # pass, so the number of entries at the start of the pass is an
        # exact bound on useful iterations; +1 covers the final call that
        # finds nothing left to claim.
        limit = len(self.queue.list()) + 1
        for _ in range(limit):
            entry = self.queue.claim_next(skip=deferred,
                                          blocked_by=self._blocked_by)
            if entry is None:
                return
            if not self._start(entry):
                deferred.add(entry.id)
        # Reached only if claim_next kept returning claimable entries beyond
        # the bound above - i.e. an entry was neither started nor added to
        # `deferred`, which should be structurally impossible. Logged loudly
        # rather than silently returning, because a worker that just stops
        # draining without a trace is worse than one that errors visibly.
        self._log.error(
            "_start_claimable hit its safety bound (%d) without exhausting "
            "the queue - claim_next may be ignoring `skip`, or a starter is "
            "returning True without actually starting the entry; stopping "
            "this pass rather than spinning forever", limit)

    def _blocked_by(self, entry: Entry) -> str | None:
        """Why this entry cannot be claimed YET, or None. Reads only.

        This exists to keep a LONG-held event lock quiet. Without it, every
        pass claimed the entry (one full rewrite of jobs.json), watched the
        starter raise `LockError` and deferred it (a second rewrite) - two
        writes a second for as long as a multi-hour CLI render holds that
        lock, with the entry oscillating queued -> running -> queued ON
        DISK, so a Jobs screen reading the queue at the wrong instant
        showed `running` for an entry that never ran once. Answering the
        question BEFORE the claim costs one write the first time the answer
        changes and nothing at all thereafter.

        It does not replace the `LockError` path in `_start`, and cannot:
        the lock may be taken between this read and the starter's own
        acquire. Anything this cannot answer - a malformed entry, an
        unresolvable profile - is deliberately answered with None, so the
        real path runs and fails the entry loudly rather than having it
        quietly held back here by a defect it never gets told about.
        """
        params = entry.params or {}
        channel, event = params.get("channel"), params.get("event")
        if not isinstance(channel, str) or not isinstance(event, str):
            return None
        try:
            event_dir = self._event_dir(f"{channel}/{event}")
        except Exception:  # noqa: BLE001 - unresolvable: let _start fail it loudly
            return None
        if not lock.EventLock(event_dir).is_held():
            return None
        return (f"waiting for the event lock on {channel}/{event}: another "
                f"render or job is using this event")

    def _event_dir(self, identifier: str) -> Path:
        """Where `<channel>/<event>` lives, cached by name.

        The PROFILE is deliberately not cached - a brand edit has to reach
        the next job that runs - but which directory a given channel/event
        name resolves to does not change while the studio runs, and this is
        asked once per second per waiting entry, where `profile.load` is
        several file reads and a `layout.py` import.
        """
        directory = self._event_dirs.get(identifier)
        if directory is None:
            directory = self._load_profile(identifier).event_dir
            self._event_dirs[identifier] = directory
        return directory

    def _start(self, entry: Entry) -> bool:
        """True if the entry is now running; False if it was deferred (and
        so must be passed over for the rest of this pass). An entry that can
        never run is failed here, which counts as "not deferred"."""
        starter = STARTERS.get(entry.kind)
        if starter is None:
            self._fail(entry, f"the worker has no way to start a {entry.kind!r} "
                              f"job; it must be started from its own route")
            return True
        params = entry.params or {}
        # Profile resolution and the starter's own synchronous work (the
        # EventLock acquire, mainly) are ONE try - both touch the outside
        # world (disk, another process's lock) on this entry's behalf, and
        # both must fail the ENTRY rather than escape drain_once() and take
        # the whole worker down with them. A channel/event that does not
        # exist, or whose profile is malformed (profile.load collects every
        # such defect and raises one ProfileError for all of them), is just
        # as much "one bad entry" as a bad param - the queue is one thread
        # driving every event's jobs, so a typo in ONE event's brand.json
        # must not stop every other event's queued work (see this module's
        # own docstring and CLAUDE.md's "one failed clip must never abort a
        # run").
        try:
            event_profile = self._load_profile(
                f"{_text(params, 'channel')}/{_text(params, 'event')}")
            # Named `cancel_token`, not `cancel` - the latter would shadow
            # the `cancel` MODULE for this whole function (Python scopes a
            # name assigned anywhere in a function as local to it, including
            # in an earlier `except` clause), which would turn
            # `except cancel.Stopped:` below into an AttributeError on this
            # token instead of the module. Caught by running this file's own
            # tests, not by inspection.
            cancel_token = CancelToken() if starter.stoppable else None
            job = starter.start(event_profile, self._job_store, params,
                                cancel_token, self._progress_reporter(entry))
        except lock.LockError as error:
            # NOT a failure: the event is busy (a CLI render, or another job
            # of this studio's own). Put it back where it was, saying so.
            self.queue.defer(entry.id, reason=str(error))
            self._log.info("queue entry %s (%s) is waiting for the event lock",
                           entry.id, entry.kind)
            return False
        except ParamError as error:
            self._fail(entry, str(error))
            return True
        except profile.ProfileError as error:
            # Named rather than left to the blanket handler below: an
            # unknown channel/event or a malformed brand.json is the most
            # ordinary way one entry is unstartable, and it must read as
            # that entry's own defect. The class is looked up on the module
            # HERE, at call time - see the note beside the imports.
            self._fail(entry, f"{type(error).__name__}: {error}")
            return True
        except cancel.Stopped:
            # Not a failure - see cancel.Stopped's own docstring: a stop is
            # the operator's own ask, and a blanket `except Exception` below
            # must never be the thing that decides that. Nothing on this
            # synchronous path checks a cancel token today (profile.load
            # takes none, and a start_*_job's pre-thread work is only an
            # EventLock acquire), so this cannot fire yet - it is guarded
            # anyway so a future starter that DOES check one before handing
            # off to its thread can never have its stop mislabelled failed.
            raise
        except Exception as error:  # noqa: BLE001 - one bad entry - including one
            # naming a channel/event whose profile cannot be resolved at all
            # (an unknown channel, an unknown event, or a malformed
            # brand.json/layout.py) - must not stop the pass, the same
            # stance cmd_render takes per clip.
            self._fail(entry, f"{type(error).__name__}: {error}")
            return True

        self.queue.mark_running(entry.id, job_id=job.id)
        self._running[entry.id] = _Running(kind=entry.kind, job=job)
        self._log.info("queue entry %s started as job %s (%s)",
                       entry.id, job.id, entry.kind)
        return True

    def _progress_reporter(self, entry: Entry):
        """The callback this entry's work reports through, or None for a
        kind that counts nothing (`jobs.KINDS[kind].progress_unit`).

        **The unit comes from the KIND table, the numbers from the work.**
        The work reports `(done, total)` and nothing else - which is what
        lets `stream_transcribe`, `moment_scan` and the render loop all use
        one signature - and the payload written here is the QUEUE's shape,
        `{"unit", "done", "total"}`, so a screen reads "chunk 20 of 50"
        without knowing which kind produced it.

        **It cannot break the work, and that is the whole reason this
        wrapper exists rather than a bare lambda.** The callback is called
        from the JOB's thread, inside a two-hour transcription or a paid
        detection run, and everything it does can fail: `mark_running`
        writes the whole plan to disk (a full disk, a vanished workspace),
        and it REFUSES any state but `running` - which happens on the
        ordinary path, not only in disasters, because the work carries on
        while the entry sits in `stopping` and can finish one more unit
        after `mark_finished` has already cleared the reading. So every
        exception is swallowed and logged at debug: one unreported unit
        costs one reading, never the run, the same stance "one failed clip
        must never abort a run" takes one layer down. This is the ONE place
        the guarantee lives - the three producers call their callback
        plainly, exactly as `moment_scan.scan` always has.

        **It takes only the queue's own lock**, never `Worker.lock`: this
        runs on the job's thread while the worker's thread may be holding
        the coarse lock and waiting for the queue's, and taking them in the
        other order here is precisely the inversion that would deadlock
        (see job_queue.py's "Two locks, and each answers a different
        question"). One call into the queue needs nothing coarser - it is
        atomic on its own.

        **A whole-file save per unit is deliberate.** A chunk is minutes, a
        window is an hour of stream and a clip is minutes; anything finer
        would need `logsetup.LineThrottle`, which exists for exactly that
        and is not needed at these rates.

        **Entry ids are reused across a retry, and this closure has no way
        to tell "late" from "the new run's own reading" apart.** A retried
        entry re-enters `running` under the SAME id (see `JobQueue.retry`),
        so a callback that fired after ITS job had already called
        `job.finish(...)` would be accepted by `mark_running` exactly as
        readily as a genuine reading from whatever now occupies that id -
        the entry really is `running` again, just for different work.
        Unreachable today: none of the three producers calls `progress`
        after its own work has ended. It would stop being unreachable the
        moment a future producer reported from a `finally` block that ran
        after its own stop/failure handling had already returned control to
        the caller - so a producer added later must call `progress` only
        from inside its own work loop, never from cleanup that can outlive it.
        """
        unit = getattr(jobs.KINDS.get(entry.kind), "progress_unit", None)
        if unit is None:
            return None
        entry_id = entry.id

        def report(done, total) -> None:
            try:
                self.queue.mark_running(entry_id, progress={
                    "unit": unit, "done": int(done), "total": int(total)})
            except Exception as error:  # noqa: BLE001 - a reading must never cost the run
                self._log.debug(
                    "queue entry %s: progress %s/%s not recorded (%s: %s)",
                    entry_id, done, total, type(error).__name__, error)

        return report

    def _fail(self, entry: Entry, reason: str) -> None:
        # The reason may be an exception's own str() (see the generic
        # `except Exception` above), which - like the decode-failure and
        # job-record cases logsetup's own docstring names - can in principle
        # carry a URL with a signed token in it (a stalled/expired-manifest
        # googlevideo URL, most concretely). Centralised here rather than at
        # each call site, so every path into this method is covered by
        # construction rather than by remembering to wrap it each time.
        reason = logsetup.shorten_urls(reason)
        self._log.error("queue entry %s (%s) failed to start: %s",
                        entry.id, entry.kind, reason)
        try:
            self.queue.mark_finished(entry.id, "failed", reason=reason)
        except job_queue.QueueError as error:
            self._log.warning("could not record entry %s as failed: %s",
                              entry.id, error)

    # -- stopping one entry's job -----------------------------------------

    def request_stop(self, entry_id: str, *, force: bool = False) -> Entry:
        """Asks the job running `entry_id` to stop, and moves the entry to
        `stopping`. Raises `QueueError` (with a `kind` a route can map) when
        the request cannot be honoured - never a quiet no-op, and never a
        quiet downgrade of a hard stop to a graceful one."""
        with self.lock:
            running = self._running.get(entry_id)
            if running is None:
                raise job_queue.QueueError(
                    f"entry {entry_id} is not running here, so there is "
                    f"nothing to stop", kind="invalid_state")
            spec = jobs.KINDS[running.kind]
            if force and not spec.hard_stop_allowed:
                raise job_queue.QueueError(
                    f"a {running.kind} job has no hard stop ({spec.stop_point})",
                    kind="no_hard_stop")
            token = running.job.cancel
            if token is None:
                raise job_queue.QueueError(
                    f"a {running.kind} job cannot be stopped ({spec.stop_point})",
                    kind="not_stoppable")
            # An entry that is ALREADY `stopping` can still be ESCALATED to a
            # hard stop, and that escalation is really performed: the token is
            # the job's own, and `request_kill` reaches the subprocess whatever
            # state the ENTRY happens to be recorded in. What must not follow
            # is `mark_stopping`, which refuses any state but `running` - so
            # this call used to request the kill and THEN raise
            # `invalid_state`, telling the operator the escalation was refused
            # while the subprocess was being killed. That is this project's
            # recurring lie in its other direction, and it also cost the Jobs
            # screen the escalation entirely: with the click half-applying,
            # `allowedActions` could offer nothing at all on a stopping row,
            # and a graceful stop on a long chunk can be minutes away.
            #
            # Marking FIRST would have been the other repair and is wrong:
            # `request_stop`'s whole rule is that the token is reached before
            # the entry is moved, so a `mark_stopping` that succeeded while
            # nothing had told the work to stop can never happen. Nothing has
            # to move here anyway - the graceful stop already put the entry
            # where it belongs.
            current = next((e for e in self.queue.list() if e.id == entry_id), None)
            escalating = force and current is not None and current.state == "stopping"
            if force:
                token.request_kill()
            else:
                token.request_stop()
            self._log.info("stop requested for queue entry %s (%s, force=%s)",
                           entry_id, running.kind, force)
            if escalating:
                return current
            # A GRACEFUL stop on an already-stopping entry still raises from
            # here (`invalid_state`), and stays that way: it asks for nothing
            # that has not already been asked for.
            return self.queue.mark_stopping(entry_id)

    # -- the thread -------------------------------------------------------

    def start(self) -> None:
        """Recovers the queue, then starts the draining thread. Idempotent;
        never called by `create_app` - see this module's own docstring."""
        with self.lock:
            # Cleared BEFORE the idempotence check, not after: if a previous
            # `stop()` timed out, the old thread is still alive and still
            # ours, and clearing the flag puts that same thread back to work
            # rather than leaving a worker holding a thread that is about to
            # exit and nothing to replace it with.
            self._stopping.clear()
            if self._thread is not None and self._thread.is_alive():
                return
            # Before the thread, not after: an entry left `running` by a dead
            # process still occupies its pool slot (`_ACTIVE_STATES`), so a
            # pass that ran first could find a full pool and start nothing.
            # Inside the idempotence check, so a `start()` on an already-
            # draining worker cannot interrupt a live entry - see `recover`.
            self.recover()
            self._thread = threading.Thread(target=self._loop, name=THREAD_NAME,
                                            daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ends the draining thread. Safe with no thread started, and it
        stops only the WORKER - a job already running keeps running, and is
        picked up again after a restart via `recover`/`retry`.

        The thread reference is dropped only once the join has actually
        SEEN the thread end. It used to be cleared unconditionally, which
        made `is_running()` answer False for a thread that was still
        draining - and there is one real way to reach that: a pass wedged
        inside a starter for longer than `timeout`. Reporting "stopped"
        there would be a claim nothing had checked; the flag is set either
        way, so the thread still ends as soon as its current pass does.
        """
        self._stopping.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            self._log.warning(
                "the worker thread did not end within %.1fs; it is still "
                "draining its current pass and will stop when that pass "
                "returns", timeout)
            return
        self._thread = None

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.drain_once()
            except Exception as error:  # noqa: BLE001 - the loop must outlive one bad pass
                # A pass that raises would otherwise end the only thread that
                # runs anything, silently, for the rest of the session.
                self._log.exception("job queue pass failed: %s", error)
            self._stopping.wait(self._interval)


def _reason_from(job: jobs.Job) -> str | None:
    """The job's own per-result reasons, joined - what the render panel
    already shows, carried onto the queue entry so a finished entry says why
    without the operator having to open the job's log."""
    snapshot = job.snapshot()
    parts = [f"{name}: {result['reason']}"
             for name, result in snapshot["results"].items() if result["reason"]]
    return "; ".join(parts) or None
