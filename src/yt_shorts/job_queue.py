"""A pure job queue: a data structure with a file behind it.

This module spawns no threads and performs no work - it decides ORDER and
ELIGIBILITY only. `JobQueue.claim_next` hands back the next entry a worker
may start; a later task's worker thread is what actually runs a transcribe,
render, detect or upload and reports back via `mark_running`/`mark_finished`.
That split is what makes the queue testable with a fake clock and no
network, no ffmpeg and no Whisper decode anywhere in this file.

Like `subtitle_pipeline.py`, `upload_policy.py` and `pathnames.py`, this
module is stdlib-only and imports nothing project-shaped: no FastAPI, no
google, no vendor SDK, and deliberately NOT `yt_shorts.studio.jobs` even
though that is where the real `KindSpec` table (`jobs.KINDS`) lives. The
`kinds` parameter is duck-typed instead - anything with a `.pool` (str) and
a `.queueable` (bool) attribute per kind name works, so `jobs.py` can pass
its own table in without this module ever importing back into `studio/`.

**Two pools, not one.** A job's `kind` maps (via `kinds[kind].pool`) to the
resource it actually saturates - `"cpu"` for transcribe/render/trim,
`"net"` for detect/upload - and `limits` (a `dict[str, int]`) caps how many
entries may be `running`/`stopping` in each pool at once. A pool ABSENT
from `limits` is unlimited, by construction rather than by a special case:
an operator who configures no limit for a pool gets an unlimited one. This
used to be described as how `"io"` (copy) "runs outside both pools", and
that is no longer true of anything shipped - `jobs.KINDS["copy"]` is the
only `"io"` kind and it is not queueable, so no production path enqueues
one. The RULE is still exactly as stated (and pinned by
`test_io_runs_outside_both_pools`, which builds its own kinds table to do
it); what has gone is the shipped kind that exercised it.

**Nothing here decides that a job may not start for a reason outside the
queue - but it will ASK.** `claim_next` knows about pools and dependencies;
the EVENT LOCK is not its business. Two mechanisms carry that division:
`claim_next(blocked_by=...)` is an optional callback the caller supplies,
consulted on a candidate BEFORE it is claimed and free to answer with a
reason ("the event lock is held"), which is recorded on the entry while it
stays `queued` - nothing is claimed and, if the reason is unchanged from
last time, nothing is even written. And a worker that only finds out once
the starter has already refused calls `defer`, which puts the entry back as
`queued` with a reason - never `failed`, because a CLI render holding that
lock is normal and temporary - and passes over it for the rest of that pass
via `claim_next(skip=...)`, so a locked event cannot block an entry for a
different one. Both paths exist because the second is a RACE the first
cannot close: the lock can be taken between the question and the starter's
own acquire.

**Two locks, and each answers a different question.** This class holds a
re-entrant lock of its own, so every public call here is atomic against a
concurrent one - a route mutating the plan from a request thread while the
worker's thread drains cannot interleave with it half-way through and lose
an update. That is a property of THIS module, not of its callers' good
manners: no route author has to remember it. `studio.worker.Worker.lock` is
the coarser one and is still needed, for the opposite job - keeping a
SEQUENCE of calls here atomic as one decision (reap-then-claim-then-start,
or read-the-running-job-then-`mark_stopping`), which no per-call lock can
provide. The order is always Worker.lock first, then this one; nothing here
ever calls back into the worker, and a `blocked_by` callback (which runs
under this lock) must not call back into the queue either.

**`claim_next` skips what it cannot start.** It scans entries in the order
they were added and returns the first one that is both dependency-clear and
has room in its pool, walking straight past anything it cannot claim yet -
a full cpu pool must never stop a net job further down the queue from
running (see `TestHeadOfLineBlocking` in the test module). A `stopping`
entry counts as still occupying its pool slot, same as `running`: the
underlying thread has not released the resource yet, and handing the slot
to a new entry while the old one is still shutting down would oversubscribe
the pool silently.

**A dependency (`after`) that can never finish fails the entry that is
waiting on it, at claim time.** `claim_next` is the one place that inspects
`after`, so this is decided lazily, exactly when it starts to matter,
rather than being swept for on every mutation. An entry whose dependency
ended `failed`, `stopped` or `interrupted` is itself marked `failed`, with
a `reason` naming the dependency and why - it must never be left sitting in
the queue looking pending when it can structurally never run.

**`progress` belongs to a RUNNING entry and to nothing else.** It is set by
`mark_running(progress=...)` while the work is in flight and CLEARED by
every transition that takes an entry out of `_ACTIVE_STATES` -
`mark_finished` (into any terminal state), `defer` (back to `queued`),
`recover` (into `interrupted`) - as well as by `retry`, which re-queues one
of those. A finished row still reading "chunk 20 of 50" is a stale claim
about a job that is over, and this file is read back after a restart, so a
reading left behind would outlive the process that produced it. The shape
is the QUEUE's rather than each kind's: `{"unit": ..., "done": ...,
"total": ...}`, the unit naming what is being counted (chunk, window,
clip), so a screen renders "chunk 20 of 50" without knowing which kind
produced it. Nothing here validates that shape - `progress` is `Any`, the
file is hand-editable, and the client guards its own read (see
`web/src/jobs.ts`'s `reading`).

Note what follows from `mark_running` refusing any state but `running`: a
progress write that arrives after the entry has moved on - the work is
`stopping`, or a callback raced a `mark_finished` - is REFUSED rather than
resurrecting a reading on an entry that is no longer running. That is the
other half of the rule above, and it is why clearing on the way out cannot
be undone by a late write.

**Persistence is write-aside-then-replace**, the same mechanic
`providers.save_api_key` and `render.compose` use: the new state is written
to a scratch file that is a SIBLING of the target and then moved into place
with `os.replace`, which is atomic within one filesystem. A process killed
between those two steps leaves either the old, complete file or the new,
complete file - never a half-written one.

**Persistence is single-WRITER, and nothing here enforces that.** `save()`
replaces the whole file from an in-memory list that is read once, at
construction. Two processes holding one `jobs.json` therefore destroy each
other's plan silently - measured, not feared: merely constructing the
second one's `Worker` marked the first one's genuinely-running
transcription `interrupted`, and the next `save()` from either dropped
everything the other had queued. The lock that prevents it is
`lock.StudioLock`, taken by `bin/yt-shorts studio` over the WORKSPACE
before the app is built, because that is the process that owns the queue;
this module stays a data structure with a file behind it and takes no lock
of its own (its `_lock` is a THREAD lock - see "Two locks" above - and
guards nothing between processes). A caller that opens a second `JobQueue`
on a live workspace outside that guard is asking for the corruption above.

**An unparseable state file is renamed aside, never overwritten or
deleted, and the fact is reported to the caller** via `JobQueue.load_error`
- losing an operator's queued plan silently is worse than starting empty
loudly. The aside name is derived from the injected clock (`now`) so two
successive corruptions cannot clobber each other's evidence.

**No parameter that looks like a secret is ever written.** `enqueue`
inspects only the NAMES of a `params` dict - case-insensitively for `key`,
`token`, `secret`, `password` or `credential` - and refuses (never silently
strips) an entry whose params carry one. A silent strip would drop a
parameter a job actually needs without telling anyone, which is exactly
the kind of quiet degradation this project keeps having to pay for
elsewhere (see subtitle_pipeline's and detect's own history in CLAUDE.md).
This is a property of THIS module, not of its callers' good manners - no
caller-side discipline is required for `<workspace>/jobs.json` to stay free
of API keys.

**And a params value may carry no NAMES of its own** - `looks_like_a_secret_name`
is a test on a name, so it can only see the top level, and a review drove
`{"creds": {"api_key": "sk-ant-…"}}` straight past it into `jobs.json` and
back out through `GET /api/jobs`: `"creds"` looks like nothing, and nothing
ever looked inside it. `_carries_no_names` closes that by refusing any dict
BELOW the top level outright, at any depth, rather than by walking one -
a dict is the only JSON shape that introduces a name, and refusing it needs
no second copy of the predicate that could drift from the first. Lists ARE
allowed, because a shipped kind needs one (`render`'s `clips: [str]`) and a
list of scalars introduces no names; a list is walked only to be sure it
holds no dict. The refusal is deliberate rather than a redaction, for the
same reason the secret-NAME check refuses: no shipped kind takes a nested
param, so an entry carrying one is a mistake to report, not a value to
quietly rewrite. `studio.api._safe_params` still redacts nested names on the
way OUT, which covers a jobs.json this module never wrote.
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Entry", "JobQueue", "QueueError", "STATES",
           "looks_like_a_secret_name"]


# The exact state list the design calls for - see the task brief. Pool
# accounting and recovery treat `stopping` like `running`; `mark_stopping`
# is the transition into it, called by the worker once it has reached the
# running job's cancel token.
STATES = frozenset({
    "queued", "running", "done", "failed", "stopped", "paused", "stopping",
    "interrupted",
})

# States that occupy a pool slot: the work is still in progress, or the
# thread doing it has not released the resource yet.
_ACTIVE_STATES = frozenset({"running", "stopping"})

# States a mark_finished call may land an entry in.
_TERMINAL_STATES = frozenset({"done", "failed", "stopped", "interrupted"})

# The terminal states `retry` puts back in the queue. Every terminal state
# except `done` - see `retry`'s own docstring on why a stop is retryable and
# a success is not.
_RETRYABLE_STATES = frozenset({"failed", "interrupted", "stopped"})

# A finished entry (any state in _TERMINAL_STATES) older than this many most-
# recent ones is dropped from the queue - see the module docstring's file-
# growth concern. Never-finished entries (queued/running/paused/stopping)
# are never subject to this cap, however many there are.
_KEEP_FINISHED = 50

# Case-insensitive substrings that make a params key refused outright. Named,
# not sniffed from the value - see the module docstring on why the value
# itself cannot be the discriminator.
_SECRET_NAME_MARKERS = ("key", "token", "secret", "password", "credential")


class QueueError(Exception):
    """A request the queue refuses. `kind` lets a caller (a future studio
    route) map it to an HTTP status without string-sniffing the message,
    the same pattern `EventAdminError`/`*AdminError` use elsewhere."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class Entry:
    """One planned unit of work. Mutable - the queue is what owns the
    transitions between states, via its own methods, never by an outside
    caller reaching into the dataclass fields directly."""

    id: str
    kind: str
    params: dict = field(default_factory=dict)
    state: str = "queued"
    reason: str | None = None
    # What the work has reported, while it is running and only then:
    # `{"unit": str, "done": int, "total": int}` - see the module docstring.
    # `Any`, not a declared shape: it round-trips through a hand-editable
    # jobs.json, so the reader is what has to be defensive about it.
    progress: Any = None
    created_at: float = 0.0
    after: str | None = None
    # The id of the `studio.jobs.Job` currently doing this entry's work, set
    # by `mark_running` and cleared by `defer`. It is what lets a screen link
    # a queue entry to that job's own log under `logs/jobs/`; without it a
    # running entry would be a state with nothing behind it. Defaulted, so a
    # jobs.json written before this field existed still loads.
    job_id: str | None = None


def _carries_no_names(value: Any) -> bool:
    """True for a params VALUE that introduces no names of its own.

    A scalar, or a list/tuple of such values, however deep. A dict is
    refused at any depth: it is the one JSON shape that carries names, and
    `looks_like_a_secret_name` only ever sees the top level - see the
    module docstring on the nested `{"creds": {"api_key": …}}` that reached
    jobs.json before this existed.
    """
    if isinstance(value, dict):
        return False
    if isinstance(value, (list, tuple)):
        return all(_carries_no_names(item) for item in value)
    return isinstance(value, (str, int, float, bool, type(None)))


def _clear_progress(entry: Entry) -> None:
    """Drops a reading that has stopped describing anything.

    Called from EVERY transition that takes an entry out of
    `_ACTIVE_STATES` - `mark_finished`, `defer`, `recover` - and from
    `retry`, which re-queues an entry that already left. A function rather
    than four bare assignments so that the rule is greppable and a
    transition added without it is conspicuous next to the ones that have
    it; `tests/test_job_queue.py`'s
    `TestProgressBelongsToARunningEntry` enumerates the exits rather than
    spot-checking one.
    """
    entry.progress = None


def looks_like_a_secret_name(key: str) -> bool:
    """True for a params key this module refuses to write.

    Public because the READ side needs the same predicate: `jobs.json` is a
    plain file, so one written by hand (or by a version before `enqueue`
    refused such params) can still carry a key-shaped parameter, and the
    studio route that serves the plan to a browser redacts it on the way out
    with exactly this test rather than a second, drifting copy of it.
    """
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_NAME_MARKERS)


def _synchronised(method):
    """Runs one public queue call under the instance's own lock.

    A decorator rather than a `with self._lock:` in fifteen bodies, so that
    what is protected is visible in one glance down the class and a method
    added without it is conspicuous. It is applied to every public method
    that reads or writes `_entries` - including the readers (`list`,
    `load`), since a reader that sees a half-applied mutation is the same
    defect as a lost write. `_get`, `_pool_has_room`, `_dependency_status`
    and `_trim_finished` are private and only ever called from a method
    that already holds it (the lock is re-entrant, so a nested public call
    is fine too).
    """
    @functools.wraps(method)
    def call_under_the_lock(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    # Stamped so the enumeration test in tests/test_job_queue.py can tell
    # "wrapped by _synchronised" apart from "wrapped by SOMETHING that uses
    # functools.wraps" - `__wrapped__` alone is set by any such decorator
    # (an audit-trail or timing wrapper added later, say), so checking only
    # for that would pass a mutator that runs outside this lock entirely.
    call_under_the_lock._synchronised = True
    return call_under_the_lock


class JobQueue:
    """The plan, in order, plus enough bookkeeping to say what may run next.

    ``kinds`` maps a kind name to an object with `.pool` and `.queueable`
    attributes (duck-typed - see the module docstring). ``limits`` maps a
    pool name to the number of `running`/`stopping` entries allowed in it at
    once; a pool absent from ``limits`` is unlimited. ``now`` is the
    injected clock (`time.time` by default) used for `created_at` and for
    naming an aside file after a corrupt load - tests pass a fake one and
    never sleep.
    """

    def __init__(self, path: str | Path, kinds: dict, limits: dict[str, int],
                *, now=None):
        self.path = Path(path)
        self._kinds = kinds
        self._limits = limits
        self._now = now or time.time
        self._entries: list[Entry] = []
        self.load_error: str | None = None
        # Re-entrant, and re-entrant for a concrete reason: several public
        # methods here call others (claim_next -> _trim_finished -> save),
        # and the worker holds its own coarser lock across a whole run of
        # them - see the module docstring's "Two locks". Built before
        # load(), which takes it.
        self._lock = threading.RLock()
        self.load()

    # -- persistence ---------------------------------------------------

    @_synchronised
    def load(self) -> str | None:
        """(Re)reads the queue from `self.path`. Returns None on a clean
        load (including "no file yet" - a fresh queue starts empty) or the
        report string when the file existed but could not be parsed, in
        which case it is renamed aside rather than touched further and the
        in-memory queue starts empty. Also sets `self.load_error` to the
        same value, so a caller that only inspects the instance (rather
        than this return value) can still learn what happened.
        """
        if not self.path.exists():
            self._entries = []
            self.load_error = None
            return None
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            entries = [Entry(**item) for item in data["entries"]]
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            aside = self._aside_path()
            try:
                self.path.replace(aside)
            except OSError:
                # The rename itself failed (e.g. a permissions problem) -
                # there is nothing further this code can do to preserve the
                # file. The important part happens regardless: the in-memory
                # queue starts empty and `load_error` below tells the caller
                # the file could not be read, so the operator's plan is
                # never lost SILENTLY even when it cannot be moved aside.
                pass
            message = (
                f"{self.path} could not be read as a job queue "
                f"({type(error).__name__}: {error}); the unreadable file was "
                f"renamed aside to {aside.name} and the queue starts empty"
            )
            self._entries = []
            self.load_error = message
            return message
        self._entries = entries
        self.load_error = None
        return None

    def _aside_path(self) -> Path:
        # Named from the injected clock so a SECOND corrupt load (a second
        # crash before the first aside file was ever cleaned up by a human)
        # cannot silently overwrite the first one's evidence.
        stamp = int(self._now() * 1000)
        return self.path.with_name(f"{self.path.name}.corrupt-{stamp}")

    @_synchronised
    def save(self) -> None:
        payload = json.dumps(
            {"entries": [asdict(entry) for entry in self._entries]}, indent=2)
        scratch = self.path.with_name(self.path.name + ".part")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(payload, encoding="utf-8")
        # Sibling scratch file, atomic move into place - see the module
        # docstring. If this raises (simulated by a test, or a real crash),
        # `self.path` still holds whatever it held before this call.
        os.replace(scratch, self.path)

    # -- reading ---------------------------------------------------------

    @_synchronised
    def list(self) -> list[Entry]:
        return list(self._entries)

    @_synchronised
    def limits(self) -> dict[str, int]:
        """What each pool currently allows. A COPY: the caller may not reach
        into the queue's own accounting by mutating what it reads."""
        return dict(self._limits)

    @_synchronised
    def set_limits(self, limits: dict[str, int]) -> dict[str, int]:
        """Re-limits the pools on a LIVE queue, taking effect at the next
        `claim_next`.

        Deliberately does NOT `save()`: the limits are workspace SETTINGS
        (see `workspace.read_settings`), not part of the plan this file
        holds - writing them into `jobs.json` would give one setting two
        homes that could disagree. The route that changes them writes the
        settings file and calls this, so the live queue and the next
        restart agree.

        Copied in, for the same reason `limits()` copies out: a caller that
        kept its own reference could otherwise re-limit a pool later without
        going through here.
        """
        self._limits = dict(limits)
        return dict(self._limits)

    def _get(self, entry_id: str) -> Entry:
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        raise QueueError(f"no such queue entry: {entry_id!r}", kind="not_found")

    # -- pools / dependencies ---------------------------------------------

    def _pool_has_room(self, pool: str) -> bool:
        # `_ACTIVE_STATES` (running + stopping) is what makes a `stopping`
        # entry keep holding its pool slot - see the module docstring.
        # `mark_stopping` is the transition that produces that state.
        limit = self._limits.get(pool)
        if limit is None:
            return True
        # `.get`, never `[...]`: an ACTIVE entry whose kind this build does
        # not know (see `claim_next`'s own note on where such a kind comes
        # from) used to raise `KeyError` from here, out of a function that is
        # only being asked whether a pool has room - which wedged the whole
        # queue rather than costing that one entry. An unknown kind belongs
        # to no pool this build can account for, so it is counted against
        # none of them; `claim_next` is where it is failed and named.
        count = sum(
            1 for entry in self._entries
            if entry.state in _ACTIVE_STATES
            and getattr(self._kinds.get(entry.kind), "pool", None) == pool
        )
        return count < limit

    def _dependency_status(self, entry: Entry) -> str:
        """"ok" (claimable), "waiting" (not yet, try later) or "failed"
        (can never run).

        An `after` naming no entry counts as SATISFIED here, and that is
        deliberate rather than lax: `_trim_finished` ages a long-since-done
        dependency out of the plan, so "not on record" is the ordinary end
        state of a constraint that was met, and refusing at this point would
        strand an entry whose dependency actually succeeded. A typo is
        caught where it can still be told apart from that - the studio's
        `POST /api/jobs` refuses an `after` the plan does not know AT
        ENQUEUE, when the dependency would necessarily still be there.
        """
        if entry.after is None:
            return "ok"
        dependency = next((e for e in self._entries if e.id == entry.after), None)
        if dependency is None:
            return "ok"  # nothing on record to wait for; see the docstring
        if dependency.state == "done":
            return "ok"
        if dependency.state in ("failed", "stopped", "interrupted"):
            return "failed"
        return "waiting"

    # -- mutation ----------------------------------------------------------

    @_synchronised
    def enqueue(self, kind: str, params: dict | None = None, *,
               after: str | None = None) -> Entry:
        params = dict(params or {})
        bad_key = next((k for k in params if looks_like_a_secret_name(k)), None)
        if bad_key is not None:
            raise QueueError(
                f"params key {bad_key!r} looks like a secret and is refused - "
                f"never written to {self.path.name}", kind="secret_in_params")
        nested = next((k for k, v in params.items() if not _carries_no_names(v)), None)
        if nested is not None:
            raise QueueError(
                f"params key {nested!r} holds a nested object; a params value "
                f"may only be a scalar or a list of them, so that the "
                f"secret-name check above can see every name there is",
                kind="nested_params")
        spec = self._kinds.get(kind)
        if spec is None:
            raise QueueError(f"unknown job kind: {kind!r}", kind="unknown_kind")
        if not spec.queueable:
            raise QueueError(f"{kind!r} cannot be queued", kind="not_queueable")
        entry = Entry(id=uuid.uuid4().hex, kind=kind, params=params, state="queued",
                     reason=None, progress=None, created_at=self._now(), after=after)
        self._entries.append(entry)
        self.save()
        return entry

    @_synchronised
    def claim_next(self, *, skip=(), blocked_by=None) -> Entry | None:
        """Returns the next claimable entry, now `running`, or None. Skips
        (never removes) anything it cannot start - see the module
        docstring on head-of-line blocking - and, in the same pass, fails
        any entry whose dependency can never finish.

        `skip` is a collection of entry ids to pass over. It exists for the
        worker's own pass: an entry whose event lock is held is `defer`red
        back to `queued` and would otherwise be the very next thing this
        method returns, forever, while an entry for a DIFFERENT event sat
        behind it. Passing over it here (rather than leaving it claimed, or
        moving it to the back of the queue) keeps the operator's order
        intact and its pool slot free, since nothing is actually running
        for it.

        `blocked_by`, when given, is called with a candidate entry that has
        cleared every check this module knows about, and answers either
        None ("go ahead") or a REASON why it cannot start yet - which is
        recorded on the entry, still `queued`, and the scan moves on. It is
        how a caller keeps a condition outside this module (the event lock)
        from costing a claim-and-defer round trip - and, crucially, a WRITE
        - on every pass for as long as it lasts: an unchanged reason is not
        written at all. It must be read-only and must not call back into
        this queue (it runs under the queue's own lock); it is a
        best-effort pre-check, never a substitute for the caller's own
        failure path, since the condition can arise between the question
        and the answer being acted on.
        """
        claimed: Entry | None = None
        changed = False
        for entry in self._entries:
            if entry.state != "queued" or entry.id in skip:
                continue
            status = self._dependency_status(entry)
            if status == "waiting":
                continue
            if status == "failed":
                entry.state = "failed"
                entry.reason = (
                    f"dependency {entry.after} ended without succeeding; "
                    f"this entry can never run")
                changed = True
                continue
            spec = self._kinds.get(entry.kind)
            if spec is None:
                # A kind this build has no table entry for. `load` accepts
                # any `kind` string (`Entry(**item)` over a plain file), so
                # this arrives from a `jobs.json` this build did not write -
                # a hand edit, or a downgrade after a later version added a
                # kind - which is the same file-provenance model
                # `looks_like_a_secret_name`'s read side exists for.
                #
                # Indexing `self._kinds` here used to raise `KeyError`, and
                # the cost was out of all proportion to the defect: the
                # worker's loop caught it, logged it and retried a second
                # later, forever, so NO entry of ANY kind for ANY event ever
                # started again while `GET /api/jobs` answered 200 and the
                # screen told the operator it was next in line. One bad
                # entry fails one entry - the same stance `_start` already
                # takes for a kind it has no starter for, and the one
                # `drain_once`'s own docstring promises.
                entry.state = "failed"
                entry.reason = (
                    f"unknown job kind {entry.kind!r}: this build has no such "
                    f"kind, so nothing can run it. It was most likely written "
                    f"by a different version of this tool; remove the entry.")
                changed = True
                continue
            if not self._pool_has_room(spec.pool):
                continue
            if blocked_by is not None:
                blocked = blocked_by(entry)
                if blocked is not None:
                    # Not claimed, not failed, not moved: it waits and says
                    # what for. The `!=` is the whole point of doing this
                    # here rather than after a claim - a condition that
                    # lasts hours (a CLI render holding the event lock)
                    # writes the file ONCE, not twice per pass.
                    if entry.reason != blocked:
                        entry.reason = blocked
                        changed = True
                    continue
            entry.state = "running"
            # Whatever this entry last said about why it was NOT running
            # (most concretely a `defer`'s "the event lock is held") is now
            # stale, and a running entry still showing it would be read as a
            # description of the run itself.
            entry.reason = None
            claimed = entry
            changed = True
            break
        if changed:
            self._trim_finished()
            self.save()
        return claimed

    @_synchronised
    def mark_running(self, entry_id: str, *, progress: Any = None,
                     job_id: str | None = None) -> Entry:
        """Records what a RUNNING entry is doing: which job is doing it, and
        how far it has got.

        This is the one place `Entry.progress` is ever written, and it is
        called from the JOB's own thread (see `studio.worker`'s
        `_progress_reporter`) while the worker's thread and the request
        threads call other methods here - which is safe because every public
        method of this class runs under the instance's own re-entrant lock,
        and this one takes no other (see the module docstring's "Two
        locks": Worker.lock first, this one second, never the reverse).

        It refuses any state but `running`, `progress` included, and that
        refusal is load-bearing rather than incidental: the work carries on
        after a stop is requested (the entry sits in `stopping` until it
        reaches its own safe point) and can finish a unit after
        `mark_finished` has already cleared the reading. Both of those
        arrive here as a `QueueError` the caller swallows, which is what
        stops a reading from reappearing on an entry that is no longer
        running.
        """
        entry = self._get(entry_id)
        if entry.state != "running":
            raise QueueError(
                f"entry {entry_id} is not running (state={entry.state!r})",
                kind="invalid_state")
        if progress is not None:
            entry.progress = progress
        if job_id is not None:
            entry.job_id = job_id
        self.save()
        return entry

    @_synchronised
    def defer(self, entry_id: str, *, reason: str) -> Entry:
        """Puts a claimed entry BACK, `running` -> `queued`, keeping its
        place and saying why it could not start.

        This is not a failure and must never be recorded as one: the case
        it exists for is a `render` whose event lock is held by a CLI run,
        which is normal, temporary, and nobody's mistake. The entry waits
        and the reason says what it is waiting for.
        """
        entry = self._get(entry_id)
        if entry.state != "running":
            raise QueueError(
                f"entry {entry_id} is not running (state={entry.state!r}); "
                f"only a claimed entry can be deferred", kind="invalid_state")
        entry.state = "queued"
        entry.reason = reason
        entry.job_id = None
        _clear_progress(entry)   # nothing is running: see the module docstring
        self.save()
        return entry

    @_synchronised
    def mark_stopping(self, entry_id: str) -> Entry:
        """`running` -> `stopping`: a stop was ASKED FOR and the work has not
        reached its safe point yet. The entry keeps holding its pool slot
        (see `_pool_has_room`) - the thread is still using the resource, and
        handing the slot on would oversubscribe silently.

        Only the worker calls this, right after it has actually reached the
        running job's cancel token: a `stopping` state with no request behind
        it would be a state the work never learns about.
        """
        entry = self._get(entry_id)
        if entry.state != "running":
            raise QueueError(
                f"entry {entry_id} is not running (state={entry.state!r})",
                kind="invalid_state")
        entry.state = "stopping"
        self.save()
        return entry

    @_synchronised
    def mark_finished(self, entry_id: str, state: str, *,
                      reason: str | None = None) -> Entry:
        if state not in _TERMINAL_STATES:
            raise QueueError(f"{state!r} is not a terminal state", kind="invalid_state")
        entry = self._get(entry_id)
        if entry.state not in _ACTIVE_STATES:
            raise QueueError(
                f"entry {entry_id} is not running or stopping "
                f"(state={entry.state!r})", kind="invalid_state")
        entry.state = state
        entry.reason = reason
        # The work is over, so its last reading is a stale claim about it -
        # and this file is read back after a restart, where "chunk 20 of 50"
        # on a `done` row would outlive the process that reported it.
        _clear_progress(entry)
        self._trim_finished()
        self.save()
        return entry

    @_synchronised
    def pause(self, entry_id: str) -> Entry:
        entry = self._get(entry_id)
        if entry.state != "queued":
            raise QueueError(
                f"entry {entry_id} is not queued (state={entry.state!r})",
                kind="invalid_state")
        entry.state = "paused"
        self.save()
        return entry

    @_synchronised
    def resume(self, entry_id: str) -> Entry:
        entry = self._get(entry_id)
        if entry.state != "paused":
            raise QueueError(
                f"entry {entry_id} is not paused (state={entry.state!r})",
                kind="invalid_state")
        entry.state = "queued"
        self.save()
        return entry

    @_synchronised
    def move(self, entry_id: str, index: int) -> Entry:
        """Reorders a QUEUED entry to `index` in the overall list. Only the
        relative order among queued entries matters to `claim_next` (it
        skips everything else), so moving within the whole list is enough -
        no separate "queued-only" index space is needed."""
        entry = self._get(entry_id)
        if entry.state != "queued":
            raise QueueError(
                f"entry {entry_id} is not queued (state={entry.state!r}); "
                f"only a queued entry can be reordered", kind="invalid_state")
        self._entries.remove(entry)
        index = max(0, min(index, len(self._entries)))
        self._entries.insert(index, entry)
        self.save()
        return entry

    @_synchronised
    def remove(self, entry_id: str) -> None:
        """Drops an entry from the plan outright. Refuses a `running` or
        `stopping` entry - dropping a plan and halting in-progress work are
        two different operations, and this is only the first one."""
        entry = self._get(entry_id)
        if entry.state in _ACTIVE_STATES:
            raise QueueError(
                f"entry {entry_id} is {entry.state} and cannot be removed; "
                f"stop it first", kind="invalid_state")
        self._entries.remove(entry)
        self.save()

    @_synchronised
    def retry(self, entry_id: str) -> Entry:
        """Puts a `failed`, `interrupted` or `stopped` entry back in the
        queue, from the params the plan already holds.

        `stopped` is here on the operator's own decision, and it is the
        whole reason "stopping costs nothing" is a true sentence: a stopped
        transcribe resumes at the first missing chunk and a stopped detect
        at the first window nobody reached, because each caches its own unit
        of work. Refusing a retry here left the studio promising exactly
        that resumption in its stop dialog while offering no control that
        could perform it.

        `done` is still refused: re-running work that succeeded is a new
        request, not a retry of this one, and for a paid kind it would spend
        the money a second time on a click meant to recover from a failure.
        """
        entry = self._get(entry_id)
        if entry.state not in _RETRYABLE_STATES:
            raise QueueError(
                f"entry {entry_id} is not failed, interrupted or stopped "
                f"(state={entry.state!r})", kind="invalid_state")
        entry.state = "queued"
        entry.reason = None
        _clear_progress(entry)
        self.save()
        return entry

    @_synchronised
    def recover(self) -> list[Entry]:
        """Called explicitly after a restart (never automatically from
        `load`/`__init__`, which stay side-effect-free about entry states):
        any entry left `running` or `stopping` when the previous process
        stopped is not actually in progress any more - nothing here spawned
        a thread - so it becomes `interrupted`. It will not start again on
        its own; only an explicit `retry` re-queues it. Returns the
        recovered entries so a caller can log/report them."""
        recovered = []
        for entry in self._entries:
            if entry.state in _ACTIVE_STATES:
                entry.state = "interrupted"
                entry.reason = (
                    "left running when the process stopped; retry to re-queue")
                # Whatever the dead process last reported describes work no
                # thread is doing any more - the most misleading place of
                # all for a reading to survive, since it looks live.
                _clear_progress(entry)
                recovered.append(entry)
        if recovered:
            self._trim_finished()
            self.save()
        return recovered

    # -- housekeeping ------------------------------------------------------

    def _trim_finished(self) -> None:
        """Keeps only the most recently added `_KEEP_FINISHED` finished
        entries; queued/running/paused/stopping entries are never dropped,
        however many of them exist - only a terminal outcome ages out."""
        finished_positions = [
            i for i, e in enumerate(self._entries) if e.state in _TERMINAL_STATES]
        if len(finished_positions) <= _KEEP_FINISHED:
            return
        drop = set(finished_positions[: len(finished_positions) - _KEEP_FINISHED])
        self._entries = [e for i, e in enumerate(self._entries) if i not in drop]
