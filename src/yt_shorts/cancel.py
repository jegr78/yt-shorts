"""A two-level cancellation request, passed into long-running work.

Python threads are not killable, so stopping is COOPERATIVE: the work
checks this token at points it chooses, where stopping is safe. The two
levels differ only in what happens to a subprocess the work is waiting on -
`kill` additionally terminates it. Neither ever touches the thread.

Injected as a parameter, like `runner`, `logger`, `on_note` and `caller`
elsewhere in this project - never global state, so a test can drive it
directly and count the checkpoints it reaches.

`run_cancellable` lives here too, and deliberately: it is the one place
that says what `kill_requested` MEANS for a child process - terminate it,
reap it, raise `Stopped` - and BOTH subprocess boundaries that need that
behaviour (`stream_transcribe.run_with_timeout` for a decode worker,
`trim._default_runner` for an ffmpeg cut) go through it. Putting it in
either of those modules would make the other import a heavy peer for
fifteen lines; putting a copy in each would be two chances to get the
reap-the-child-or-leave-it-behind detail wrong. This module stays
stdlib-only either way, the same rule `pathnames.py` and `upload_policy.py`
follow.
"""

from __future__ import annotations

import subprocess
import threading
import time


class Stopped(RuntimeError):
    """The work was asked to stop and reached a safe point. Not a failure:
    a job that ends this way is `stopped`, not `failed`."""


class CancelToken:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._kill = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def request_kill(self) -> None:
        # A hard stop is a stop that ALSO kills the subprocess - never a
        # separate mode in which the work would carry on.
        self._stop.set()
        self._kill.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def kill_requested(self) -> bool:
        return self._kill.is_set()

    def raise_if_stopped(self) -> None:
        if self._stop.is_set():
            raise Stopped("stop requested")


# How long to wait for a terminated child to actually go before resorting to
# SIGKILL, and how often the wait loop looks at the token. The poll interval
# is what bounds "how long after the click does the child die"; it is small
# because nothing is being computed in that loop, and a subprocess this
# project waits on runs for minutes.
GRACE_SECONDS = 5.0
POLL_SECONDS = 0.2


def run_cancellable(cmd, *, timeout: float, env=None, cancel: CancelToken | None = None,
                    poll_seconds: float = POLL_SECONDS,
                    grace_seconds: float = GRACE_SECONDS,
                    popen=None) -> subprocess.CompletedProcess:
    """Runs `cmd` to completion, terminating the child if `cancel` asks for a KILL.

    Returns a `subprocess.CompletedProcess` and raises `subprocess.
    TimeoutExpired` on a hang, exactly as `subprocess.run` does - so a caller
    that already maps those two outcomes keeps its error handling unchanged.
    The third outcome is new: `Stopped`, raised only after the child has
    actually been signalled and reaped.

    A plain `request_stop()` NEVER reaches the child. It is the cooperative
    ask, checked by the work at a safe point of its own choosing (between
    chunks, between windows, between clips); only `request_kill()` means "and
    stop the subprocess you are waiting on". The distinction is the whole
    point of the two levels.

    With no token this is `subprocess.run` and nothing else - byte for byte
    the same call, so a caller that never cancels pays nothing for the
    polling loop and behaves identically to before this function existed.

    `popen` is the injected process boundary (defaulting to
    `subprocess.Popen`), so a test can hand back a stub child and OBSERVE it
    being terminated rather than infer it from how quickly the call returned.
    """
    if cancel is None:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)

    spawn = popen if popen is not None else subprocess.Popen
    deadline = time.monotonic() + timeout
    child = spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, env=env)
    try:
        while True:
            if cancel.kill_requested:
                _end(child, grace_seconds)
                raise Stopped("the subprocess was terminated on a hard stop")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _end(child, grace_seconds)
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                out, err = child.communicate(timeout=min(poll_seconds, remaining))
            except subprocess.TimeoutExpired:
                # Still running. communicate() is documented as resumable
                # after this, and it does NOT kill the child - which is what
                # makes it usable as the wait step of a polling loop.
                continue
            return subprocess.CompletedProcess(cmd, child.returncode, out, err)
    finally:
        # A child left running would outlive the request that started it and
        # keep holding the CPU (or the file) the stop was meant to release.
        if child.poll() is None:
            child.kill()
            child.communicate()


def cancel_kwargs(cancel: CancelToken | None) -> dict:
    """The forwarding idiom every cancel-aware caller in this project needs:
    send the token on ONLY when there is one, so a callee written before
    `cancel` existed - an injected fake in a test, most concretely - keeps
    being called with exactly the keywords it already accepts. `**extra`
    at the call site, not a plain default parameter: a default would force
    every one of those fakes across five test files to grow a `cancel=None`
    parameter it never asked for and never uses.

    Used identically at five call sites before this existed
    (`stream_transcribe.subprocess_decoder`,
    `stream_transcribe.transcribe_stream`, `detect.detect_moments`,
    `studio.jobs._run_detect`, `studio.jobs._run_trim`) - each had written
    out `{} if cancel is None else {"cancel": cancel}` by hand. Folded into
    one place so the idiom exists once; behaviour is unchanged."""
    return {} if cancel is None else {"cancel": cancel}


def _end(child, grace_seconds: float) -> None:
    """Ask the child to stop, then insist. A terminated child is still
    WAITED FOR: skipping the reap leaves a zombie and, worse, returns before
    the child has released the file it was writing."""
    child.terminate()
    try:
        child.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
