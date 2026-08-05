"""Tests for yt_shorts.cancel.CancelToken and yt_shorts.studio.jobs.KINDS.

CancelToken is a plain two-level cancellation request (see cancel.py's own
module docstring): `request_stop()` is a cooperative ask that the work
checks at points of its own choosing, `request_kill()` is the same ask PLUS
"and kill the subprocess you're waiting on" - never a separate mode where
the work would keep going. This file proves the token itself behaves, and
that studio.jobs.KINDS describes every kind of job the studio can actually
start. Where the token is CONSUMED is covered beside each consumer
(tests/test_stream_transcribe.py, test_detect.py, test_render.py,
test_trim.py, test_workspaces.py), where a job starter forwards it in
tests/test_studio_jobs.py, and where an operator's click reaches it in
tests/test_studio_worker.py's TestStopping.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
import threading

import pytest

from yt_shorts import cancel
from yt_shorts.studio import jobs


def test_a_fresh_token_is_not_stopped():
    token = cancel.CancelToken()
    assert token.stop_requested is False
    assert token.kill_requested is False
    token.raise_if_stopped()  # must not raise


def test_request_stop_is_visible_and_idempotent():
    token = cancel.CancelToken()
    token.request_stop()
    assert token.stop_requested is True
    assert token.kill_requested is False
    # A second request is a no-op, not an error.
    token.request_stop()
    assert token.stop_requested is True
    assert token.kill_requested is False


def test_request_kill_implies_stop():
    # A hard stop is a stop that also kills the subprocess - never a
    # separate state where the work would keep going.
    token = cancel.CancelToken()
    token.request_kill()
    assert token.kill_requested is True
    assert token.stop_requested is True


def test_raise_if_stopped_raises_only_after_a_request():
    token = cancel.CancelToken()
    token.raise_if_stopped()  # not stopped yet: no raise
    token.request_stop()
    with pytest.raises(cancel.Stopped):
        token.raise_if_stopped()


def test_the_token_is_safe_across_threads():
    # The work runs on a background thread; the stop request arrives on the
    # route's thread. Set from one, read from the other.
    token = cancel.CancelToken()
    seen_stopped = threading.Event()

    def worker():
        while not token.stop_requested:
            pass
        seen_stopped.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    token.request_stop()
    thread.join(timeout=5)
    assert seen_stopped.is_set()
    assert not thread.is_alive()


class TestCancelKwargs:
    """`cancel_kwargs` is the one place the `{} if cancel is None else
    {"cancel": cancel}` idiom lives now - it used to be written out by hand
    at five call sites (stream_transcribe.py x2, detect.py, studio/jobs.py
    x2). Behaviour is unchanged: a callee that does not accept `cancel`
    keeps working when no token is given, and receives the token by keyword
    when one is."""

    def test_no_token_forwards_nothing(self):
        assert cancel.cancel_kwargs(None) == {}

    def test_a_token_forwards_as_the_cancel_keyword(self):
        token = cancel.CancelToken()
        assert cancel.cancel_kwargs(token) == {"cancel": token}

    def test_a_callee_with_no_cancel_parameter_still_works_with_no_token(self):
        def legacy_runner(command):
            return command

        assert legacy_runner("x", **cancel.cancel_kwargs(None)) == "x"


def _default_create_kind() -> str:
    """The kind JobStore.create uses when no kind argument is given at all,
    read from its own signature rather than hardcoded here - so this test
    follows the source if that default ever changes."""
    default = inspect.signature(jobs.JobStore.create).parameters["kind"].default
    assert isinstance(default, str), "JobStore.create's kind default is no longer a string"
    return default


def _starters_by_kind() -> tuple[dict[str, set[str]], list[str]]:
    """Which `start_*` function in studio.jobs mints which job kind,
    derived from the source of jobs.py itself rather than hardcoded here -
    so a new `start_*_job` function that mints a new kind fails
    `test_every_started_kind_has_a_spec` below instead of silently going
    unclassified, and so a kind whose spec PROMISES a stop is checked
    against the function that actually starts it, whatever it is called.

    Walks the WHOLE module via ast.walk (not just its top-level functions -
    a `start_*` function nested inside another def would still be found) and,
    for every function named `start_*`, collects the kind passed to any
    `....create(...)` call inside its body - including a nested closure,
    since some starters define one. A kind may arrive as a positional
    `args[0]` string literal, as a `kind=` keyword string literal, or be
    absent entirely (which resolves to JobStore.create's own default, read
    via _default_create_kind rather than hardcoded). A call whose kind
    argument is present but is NOT a string literal (a variable, an
    f-string, a nested call, ...) cannot be verified statically - it is
    reported separately rather than silently skipped, since skipping it is
    exactly the false negative this helper exists to close.

    Returns (by_kind, unresolved) where `by_kind` maps each kind to the
    names of the `start_*` functions that create it, and `unresolved` is a
    list of human-readable descriptions of each non-literal kind argument
    found, naming the enclosing start_* function.
    """
    source = inspect.getsource(jobs)
    tree = ast.parse(source)
    by_kind: dict[str, set[str]] = {}
    unresolved: list[str] = []
    default_kind = _default_create_kind()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("start_")):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Attribute) and func.attr == "create"):
                continue

            kind_arg = call.args[0] if call.args else None
            if kind_arg is None:
                for keyword in call.keywords:
                    if keyword.arg == "kind":
                        kind_arg = keyword.value
                        break

            if kind_arg is None:
                # No kind argument at all: JobStore.create falls back to its
                # own default.
                by_kind.setdefault(default_kind, set()).add(node.name)
            elif isinstance(kind_arg, ast.Constant) and isinstance(kind_arg.value, str):
                by_kind.setdefault(kind_arg.value, set()).add(node.name)
            else:
                unresolved.append(
                    f"{node.name} (line {call.lineno}): job kind is not a string "
                    "literal, so this static check cannot see it"
                )
    return by_kind, unresolved


def _started_kinds() -> tuple[set[str], list[str]]:
    """Just the kind names of the above, for the test that only needs those."""
    by_kind, unresolved = _starters_by_kind()
    return set(by_kind), unresolved


class TestKindSpecs:
    def test_every_started_kind_has_a_spec(self):
        # Every kind studio.jobs can create must be described, or the queue
        # cannot classify it. The kind list comes from the code itself (see
        # _started_kinds above), not a hand-maintained list here.
        started, unresolved = _started_kinds()
        assert not unresolved, (
            "found job kind(s) this static check cannot verify - a kind "
            "must be a string literal (positional or `kind=`) so this "
            "test can see it, not a variable, f-string or call: "
            + "; ".join(unresolved)
        )
        assert started, "expected to find at least one start_*_job kind"
        missing = started - jobs.KINDS.keys()
        assert not missing, f"kinds started but not described in KINDS: {missing}"

    def test_upload_forbids_a_hard_stop(self):
        assert jobs.KINDS["upload"].hard_stop_allowed is False

    def test_copy_forbids_a_hard_stop(self):
        # A hard stop is defined in this project as terminating the
        # SUBPROCESS the work waits on (see cancel.run_cancellable) - and a
        # workspace copy waits on none: it is shutil.copytree running in the
        # thread itself, and threads are not killable. Its graceful stop is
        # real (checked before every file, see workspaces.copy_workspace), so
        # a hard button here could never do anything the graceful one does
        # not already do.
        assert jobs.KINDS["copy"].hard_stop_allowed is False

    def test_connect_is_not_queueable(self):
        assert jobs.KINDS["connect"].queueable is False

    def test_copy_is_not_queueable(self):
        # start_copy_job takes an `on_done` callback that re-roots the LIVE
        # app onto the copy. A queue entry is a plan on disk and can hold no
        # such callback, so a copy keeps its own route rather than being
        # something the worker could pick up later.
        assert jobs.KINDS["copy"].queueable is False

    def test_every_spec_names_a_stop_point(self):
        # The UI shows this phrase to the operator BEFORE they click - an
        # empty or blank string would render as a blank promise.
        for kind, spec in jobs.KINDS.items():
            assert spec.stop_point.strip(), f"{kind} has no stop_point"

    def test_every_spec_that_promises_a_stop_names_a_starter_that_can_take_one(
            self, real_job_starters):
        """A `stop_point` other than "cannot be stopped" is a promise made
        to the operator in the UI before they click, and the ONLY thing
        that can back it is the starter taking a `cancel` token: a token is
        how a request reaches the work at all. `copy` is why this exists -
        its spec claimed both a stop point and a hard stop that nothing
        implemented - and the fix at the time was two spot checks for that
        one kind, which is not a guard. This enumerates KINDS instead.

        Deliberately NOT derived from `worker.STARTERS`: that dict contains
        only QUEUEABLE kinds by construction, and `connect` - not queueable
        - is precisely where the last unbacked promise sat. The kind ->
        starter mapping comes from jobs.py's own source (see
        `_starters_by_kind`), so a starter renamed or a kind minted
        somewhere new is followed rather than missed; a kind this cannot
        resolve to a real function FAILS here rather than being skipped,
        because a skipped case is exactly the false negative this test
        exists to close.
        """
        no_stop = "cannot be stopped"
        by_kind, unresolved = _starters_by_kind()
        assert not unresolved, (
            "found job kind(s) this static check cannot verify: "
            + "; ".join(unresolved))
        for kind, spec in jobs.KINDS.items():
            if spec.stop_point.strip().lower() == no_stop and not spec.hard_stop_allowed:
                continue
            names = by_kind.get(kind)
            assert names, (
                f"KINDS[{kind!r}] promises a stop ({spec.stop_point!r}, "
                f"hard_stop_allowed={spec.hard_stop_allowed}) but no start_* "
                f"function in studio.jobs creates a {kind!r} job, so nothing "
                f"can carry that promise")
            for name in sorted(names):
                starter = getattr(jobs, name, None)
                assert starter is not None, (
                    f"{name} creates a {kind!r} job but is not reachable as a "
                    f"module-level function, so this check cannot see its "
                    f"signature")
                assert "cancel" in inspect.signature(starter).parameters, (
                    f"KINDS[{kind!r}] promises a stop ({spec.stop_point!r}) but "
                    f"{name} takes no cancel token, so nothing can ever tell "
                    f"that job to stop")


class StubChild:
    """A subprocess that never exits on its own until it is signalled.

    Stands in for a real ffmpeg/Whisper child so a test can OBSERVE the
    termination rather than infer it from how quickly a call returned.
    `communicate(timeout=...)` raises TimeoutExpired while the child is
    still alive, exactly as subprocess.Popen's does, so the polling loop
    under test takes the same path it takes against a real one.
    """

    def __init__(self, *, ignores_terminate: bool = False, on_start=None):
        self.ignores_terminate = ignores_terminate
        self.terminated = 0
        self.killed = 0
        self.returncode = None
        if on_start is not None:
            on_start()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        # Mirrors real subprocess.Popen.__exit__, which waits for the child
        # on the way out - so a mutation that reverts run_cancellable's own
        # cancellable branch back to a literal subprocess.run(...) (always
        # driven as a context manager) exercises this stub's TimeoutExpired/
        # kill/wait path instead of just failing the `with` statement before
        # the mutation under test is even reached.
        self.wait()
        return False

    def communicate(self, input=None, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("stub", timeout or 0)
        return ("out", "err")

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("stub", timeout or 0)
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if not self.ignores_terminate:
            self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def poll(self):
        return self.returncode


class TestRunCancellable:
    """`run_cancellable` is the one place that says what `kill_requested`
    MEANS for a child process: terminate it, reap it, raise `Stopped`. Both
    `stream_transcribe.run_with_timeout` and `trim._default_runner` go
    through it, so neither owns a second copy of this behaviour."""

    def test_without_a_token_the_command_runs_unchanged(self):
        result = cancel.run_cancellable(
            [sys.executable, "-c", "print('hello')"], timeout=30)
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_with_an_unstopped_token_the_command_still_runs(self):
        token = cancel.CancelToken()
        result = cancel.run_cancellable(
            [sys.executable, "-c", "print('hello')"], timeout=30, cancel=token)
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_a_kill_terminates_the_child_and_raises_stopped(self):
        token = cancel.CancelToken()
        token.request_kill()
        children = []

        def popen(cmd, **kwargs):
            child = StubChild()
            children.append(child)
            return child

        with pytest.raises(cancel.Stopped):
            cancel.run_cancellable(["ffmpeg", "-i", "x"], timeout=30,
                                   cancel=token, popen=popen)
        assert children[0].terminated == 1, "the child was never asked to stop"

    def test_a_plain_stop_does_not_touch_the_child(self):
        # request_stop is the COOPERATIVE ask - the work checks it at a safe
        # point of its own choosing. Only request_kill reaches the subprocess.
        token = cancel.CancelToken()
        token.request_stop()
        children = []

        def popen(cmd, **kwargs):
            child = StubChild()
            children.append(child)
            child.returncode = 0          # finishes on the first poll
            return child

        result = cancel.run_cancellable(["ffmpeg"], timeout=30, cancel=token,
                                        popen=popen)
        assert result.returncode == 0
        assert children[0].terminated == 0 and children[0].killed == 0

    def test_a_child_that_ignores_terminate_is_killed(self):
        token = cancel.CancelToken()
        token.request_kill()
        children = []

        def popen(cmd, **kwargs):
            child = StubChild(ignores_terminate=True)
            children.append(child)
            return child

        with pytest.raises(cancel.Stopped):
            cancel.run_cancellable(["ffmpeg"], timeout=30, cancel=token,
                                   popen=popen, grace_seconds=0.05)
        assert children[0].terminated == 1
        assert children[0].killed >= 1, "a child that ignored SIGTERM was left running"

    def test_the_timeout_still_kills_a_hung_child(self):
        # The token does not replace the timeout: a hang with nobody watching
        # must still end. TimeoutExpired is raised, exactly as subprocess.run
        # does, so every existing caller's error mapping is unchanged.
        token = cancel.CancelToken()
        children = []

        def popen(cmd, **kwargs):
            child = StubChild(ignores_terminate=True)
            children.append(child)
            return child

        with pytest.raises(subprocess.TimeoutExpired):
            cancel.run_cancellable(["ffmpeg"], timeout=0.05, cancel=token,
                                   popen=popen, poll_seconds=0.01)
        assert children[0].killed >= 1
