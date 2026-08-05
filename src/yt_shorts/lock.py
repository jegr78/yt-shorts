"""Exclusive, atomically-created lock files: one per event, one per workspace.

Nothing prevents two `bin/yt-shorts render` processes from running against
the same event: they collide on raw/<name>.raw.mp4, raw/<name>.subs/ and
drafts/. This is not hypothetical - two racing render processes once
truncated files in a real event's drafts/ and destroyed reference
artifacts that were not recoverable.

EventLock guards against exactly that: acquire() refuses to start a second
concurrent run with a message naming the event, the process already
holding it, and what to do about it. It never blocks or waits - refusing
immediately is the point, so an operator finds out before paying for a
download and transcription that get thrown away.

`render` is the only command that needs this: `harvest` and `gallery`
never write into raw/ or drafts/.

**StudioLock is the same mechanism one level up**, over a WORKSPACE rather
than an event, and it exists for a second collision of exactly the same
shape. `cmd_studio` deliberately picks a free port when 8765 is busy, so a
second `bin/yt-shorts studio` against the same workspace used to start
perfectly happily - and the two then shared one `jobs.json`, which
`job_queue.JobQueue` replaces wholesale from an in-memory list it reads
once. Measured, not feared: merely STARTING the second marked the first's
running two-hour transcription `interrupted`, and the next write from
either silently deleted everything the other had queued. Nothing logged,
nothing warned. So the second studio is refused, loudly, before it builds
an app - the same stance, for the same reason, as the render lock above.

Both classes share `_PidLock`: the lock file's content is the owning
process's pid, so a refusal can name what holds it and a lock left behind
by a crashed run is recognised as stale and taken over rather than
bricking the tool forever. Only the wording of the refusal differs, and it
differs because the advice does: the answer to a locked event is to wait,
and the answer to a locked workspace is to use the studio that is already
running (or point this one at another workspace).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

LOCK_NAME = ".render.lock"
STUDIO_LOCK_NAME = ".studio.lock"


class LockError(Exception):
    """A live process already holds this lock - an event's, or a workspace's.

    One class for both, deliberately: every handler in this project reacts
    the same way (refuse and say what holds it), and `studio.worker` catches
    it BY MODULE ATTRIBUTE for reasons its own comment records. A second
    exception class would be one more thing that handler could miss.
    """


def _process_is_alive(pid: int) -> bool:
    """True if pid names a process this user can currently see.

    Signal 0 sends nothing; the kernel only checks whether the target
    exists and is reachable, which is exactly what a stale-lock check
    needs (os.kill's name is misleading here - no process is killed).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just not signalable by us (different user/privilege) -
        # still alive as far as staleness is concerned.
        return True
    return True


class _PidLock:
    """One directory, one lock file, whose content is the holder's pid.

    acquire() is atomic against another process racing to create the same
    lock file, via O_CREAT | O_EXCL - no dependency needed, this is enough
    on a local filesystem. The lock file's content is the owning process's
    pid, so a refusal can name what holds it, and so a lock left behind by
    a crashed run (pid no longer alive) is recognised as stale and taken
    over instead of refusing forever.

    Subclasses supply the file NAME and the two sentences an operator
    reads - the refusal and the stale-takeover note. Nothing else differs
    between an event lock and a studio lock, and the shared mechanics are
    here rather than copied so a second copy cannot drift on the part that
    is actually hard (the stale-takeover race at the bottom).
    """

    # The lock file's name inside the guarded directory. Named FILENAME
    # rather than LOCK_NAME so a subclass can assign it from this
    # module's own public constant without the two names shadowing each
    # other in the class body.
    FILENAME = ""

    def __init__(self, directory: str | Path):
        self.path = Path(directory) / self.FILENAME

    def _refusal(self, holder_pid: int) -> str:
        raise NotImplementedError

    def _stale_note(self, why: str) -> str:
        raise NotImplementedError

    def acquire(self) -> None:
        """Raises LockError if a live process already holds the lock.
        Otherwise creates (or takes over a stale) lock file recording this
        process's pid."""
        try:
            self._create_exclusive()
            return
        except FileExistsError:
            pass  # someone else holds it; fall through to the stale-takeover path
        self._take_over_stale_or_raise()

    def _create_exclusive(self) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)

    def is_held(self) -> bool:
        """True iff the lock file exists and names a live process - a read-only
        check that never creates or takes over a lock (unlike acquire). A stale
        (dead-pid), empty, or absent lock is not held. Used by channel_admin to
        refuse renaming/deleting a channel while any of its events is rendering,
        and by the worker's own pre-check before it claims an entry."""
        pid = self._read_holder_pid()
        return pid is not None and _process_is_alive(pid)

    def _read_holder_pid(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _take_over_stale_or_raise(self) -> None:
        holder_pid = self._read_holder_pid()
        if holder_pid is not None and _process_is_alive(holder_pid):
            raise LockError(self._refusal(holder_pid))

        # The recorded pid is no longer alive (or the file was empty/
        # unreadable, e.g. a crash between O_CREAT and the write) - a
        # permanently-locked event from a crashed run is worse than taking
        # over a lock nothing is actually holding. Recreate it via
        # O_CREAT|O_EXCL, one retry: if that still fails, someone else won
        # the race to take it over first, and whichever pid it now holds is
        # reported instead of insisting on the one we just judged stale.
        note = (
            f"previously held by process {holder_pid}, no longer running"
            if holder_pid is not None else "lock file was empty or unreadable"
        )
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass  # already gone (a concurrent takeover beat us to it)
        try:
            self._create_exclusive()
        except FileExistsError:
            self._take_over_stale_or_raise()
            return
        print(self._stale_note(note), file=sys.stderr)

    def release(self) -> None:
        """Removes the lock file. Safe to call even if nothing was ever
        acquired, or if it was already removed by someone else."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass  # already released (or never acquired) — release is idempotent


class EventLock(_PidLock):
    """Exclusive lock for one event directory (channels/<c>/events/<e>/).

    Taken by every render - the CLI's own and the studio's job runner's
    alike - so a studio render and a CLI render against the same event can
    never race each other.
    """

    FILENAME = LOCK_NAME

    def _refusal(self, holder_pid: int) -> str:
        return (
            f"Event '{self.path.parent.name}' is locked by process "
            f"{holder_pid} (lock file: {self.path}). Another render is "
            f"already running against this event - wait for it to "
            f"finish before starting another, or if you are certain "
            f"process {holder_pid} is not actually a render of this "
            f"event, remove the lock file and try again."
        )

    def _stale_note(self, why: str) -> str:
        return (f"NOTE: taking over stale lock for event "
                f"'{self.path.parent.name}' ({why})")


class StudioLock(_PidLock):
    """Exclusive lock for one WORKSPACE, held by the running studio.

    One studio per workspace, because two of them share one `jobs.json`
    and destroy each other's plan silently - see this module's own
    docstring for the measurement. Taken by `bin/yt-shorts studio` before
    it builds the app and released when the server exits; NEVER by
    `create_app()`, which thousands of tests call and none of which may
    acquire a lock as a side effect of construction (the same rule the
    worker thread follows - see studio/worker.py's docstring).

    A stale lock from a studio that was killed is taken over, exactly like
    an event's: a workspace that could never be opened again because a
    process crashed would be a worse failure than the one this prevents.
    """

    FILENAME = STUDIO_LOCK_NAME

    def _refusal(self, holder_pid: int) -> str:
        return (
            f"A studio is already running against this workspace "
            f"({self.path.parent}), as process {holder_pid} (lock file: "
            f"{self.path}). Two studios on one workspace share one "
            f"jobs.json and silently destroy each other's queued plan, so "
            f"this one is refusing to start. Use the studio that is "
            f"already running (it prints its own URL when it starts), or "
            f"point this one at a different workspace with YT_SHORTS_DATA. "
            f"If you are certain process {holder_pid} is not a studio, "
            f"remove the lock file and try again."
        )

    def _stale_note(self, why: str) -> str:
        return (f"NOTE: taking over stale studio lock for workspace "
                f"'{self.path.parent}' ({why})")
