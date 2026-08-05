"""Tests for yt_shorts.lock: the exclusive per-event lock that guards
`render` against a second concurrent run colliding on raw/ and drafts/
(finding A2 - two racing render processes once destroyed reference
artifacts that were not recoverable), and the per-WORKSPACE studio
lock that guards one `jobs.json` against two studios (see
`TestStudioLock` at the bottom for what that one cost)."""

from __future__ import annotations

import os
import subprocess

import pytest

from yt_shorts.lock import EventLock, LockError, StudioLock


def _dead_pid() -> int:
    """A pid that is guaranteed to have existed and to no longer be alive,
    without relying on "this large number probably isn't in use" - spawns
    a real child process and waits for it to exit."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class TestAcquireAndRelease:
    def test_acquire_creates_a_lock_file_naming_this_process(self, tmp_path):
        lock = EventLock(tmp_path)
        lock.acquire()

        lock_file = tmp_path / ".render.lock"
        assert lock_file.exists()
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())

    def test_release_removes_the_lock_file(self, tmp_path):
        lock = EventLock(tmp_path)
        lock.acquire()

        lock.release()

        assert not (tmp_path / ".render.lock").exists()

    def test_release_without_acquire_does_not_raise(self, tmp_path):
        EventLock(tmp_path).release()  # must not raise

    def test_release_is_idempotent(self, tmp_path):
        lock = EventLock(tmp_path)
        lock.acquire()
        lock.release()
        lock.release()  # must not raise the second time


class TestSecondConcurrentRunRefuses:
    def test_second_acquire_refuses_while_the_first_is_alive(self, tmp_path):
        first = EventLock(tmp_path)
        first.acquire()

        second = EventLock(tmp_path)
        with pytest.raises(LockError) as excinfo:
            second.acquire()

        message = str(excinfo.value)
        assert tmp_path.name in message, "message must name the locked event"
        assert str(os.getpid()) in message, "message must name what holds the lock"

    def test_refusal_leaves_the_original_lock_untouched(self, tmp_path):
        first = EventLock(tmp_path)
        first.acquire()
        original_pid = (tmp_path / ".render.lock").read_text(encoding="utf-8")

        with pytest.raises(LockError):
            EventLock(tmp_path).acquire()

        assert (tmp_path / ".render.lock").read_text(encoding="utf-8") == original_pid


class TestStaleLockIsTakenOver:
    def test_stale_lock_from_a_dead_process_is_taken_over(self, tmp_path, capsys):
        dead_pid = _dead_pid()
        (tmp_path / ".render.lock").write_text(str(dead_pid), encoding="utf-8")

        lock = EventLock(tmp_path)
        lock.acquire()  # must not raise

        assert (tmp_path / ".render.lock").read_text(encoding="utf-8").strip() == str(os.getpid())
        out = capsys.readouterr()
        assert "stale" in out.err.lower()
        assert tmp_path.name in out.err

    def test_lock_file_with_unparseable_content_is_treated_as_stale(self, tmp_path):
        (tmp_path / ".render.lock").write_text("not-a-pid", encoding="utf-8")

        EventLock(tmp_path).acquire()  # must not raise

        assert (tmp_path / ".render.lock").read_text(encoding="utf-8").strip() == str(os.getpid())

    def test_empty_lock_file_is_treated_as_stale(self, tmp_path):
        (tmp_path / ".render.lock").write_text("", encoding="utf-8")

        EventLock(tmp_path).acquire()  # must not raise


class TestLockPath:
    def test_lock_lives_inside_the_event_directory(self, tmp_path):
        lock = EventLock(tmp_path)
        assert lock.path.parent == tmp_path
        assert lock.path.name == ".render.lock"


class TestIsHeld:
    """is_held() is a read-only check (never creates or takes over a lock) so a
    channel operation can ask 'is any of my events rendering?' without acquiring
    N locks - see channel_admin's rename/delete guard."""

    def test_a_live_pid_lock_is_held(self, tmp_path):
        EventLock(tmp_path).path.write_text(str(os.getpid()), encoding="utf-8")
        assert EventLock(tmp_path).is_held() is True

    def test_no_lock_file_is_not_held(self, tmp_path):
        assert EventLock(tmp_path).is_held() is False

    def test_a_stale_dead_pid_lock_is_not_held(self, tmp_path):
        EventLock(tmp_path).path.write_text(str(_dead_pid()), encoding="utf-8")
        assert EventLock(tmp_path).is_held() is False

    def test_an_empty_or_garbage_lock_is_not_held(self, tmp_path):
        EventLock(tmp_path).path.write_text("not-a-pid", encoding="utf-8")
        assert EventLock(tmp_path).is_held() is False


class TestStudioLock:
    """The same mechanism one level up: one studio per WORKSPACE.

    `cmd_studio` picks a free port when 8765 is busy, so a second studio
    against the same workspace starts happily - and the two then share one
    `jobs.json`, which `JobQueue` replaces wholesale from an in-memory list
    it read once. Measured before this lock existed: merely starting the
    second marked the first's running transcription `interrupted`, and the
    next write from either deleted everything the other had queued, with
    nothing logged anywhere.
    """

    def test_the_lock_lives_at_the_workspace_root_under_its_own_name(self, tmp_path):
        # Its own name, not the render lock's: a workspace root and an event
        # directory are different places, but nothing stops an operator from
        # nesting them, and two locks sharing one filename would then make a
        # running render look like a running studio.
        lock = StudioLock(tmp_path)
        assert lock.path == tmp_path / ".studio.lock"
        assert lock.path.name != EventLock(tmp_path).path.name

    def test_a_second_studio_is_refused_and_told_what_to_do(self, tmp_path):
        first = StudioLock(tmp_path)
        first.acquire()

        with pytest.raises(LockError) as refused:
            StudioLock(tmp_path).acquire()

        message = str(refused.value)
        assert str(os.getpid()) in message          # names what holds it
        assert str(tmp_path) in message             # and which workspace
        assert "already running" in message
        # And what to do about it - a refusal with no way forward is how an
        # operator ends up deleting a lock file on a guess.
        assert "YT_SHORTS_DATA" in message

    def test_the_refusal_leaves_the_first_studios_lock_untouched(self, tmp_path):
        StudioLock(tmp_path).acquire()
        before = (tmp_path / ".studio.lock").read_text(encoding="utf-8")

        with pytest.raises(LockError):
            StudioLock(tmp_path).acquire()

        assert (tmp_path / ".studio.lock").read_text(encoding="utf-8") == before

    def test_a_stale_lock_from_a_crashed_studio_does_not_brick_the_tool(
            self, tmp_path, capsys):
        # A studio that was killed leaves its lock file behind. Refusing
        # forever would make one crash cost the operator their whole tool,
        # which is worse than the collision this guards against.
        (tmp_path / ".studio.lock").write_text(str(_dead_pid()), encoding="utf-8")

        StudioLock(tmp_path).acquire()

        assert (tmp_path / ".studio.lock").read_text(encoding="utf-8") == str(os.getpid())
        assert "stale studio lock" in capsys.readouterr().err

    def test_release_lets_the_next_studio_start(self, tmp_path):
        first = StudioLock(tmp_path)
        first.acquire()
        first.release()

        StudioLock(tmp_path).acquire()      # no LockError
        assert (tmp_path / ".studio.lock").exists()

    def test_an_event_lock_and_a_studio_lock_do_not_see_each_other(self, tmp_path):
        # Both may legitimately be held at once - a studio serving while one
        # of its own events renders - so neither may refuse the other.
        StudioLock(tmp_path).acquire()
        EventLock(tmp_path).acquire()

        assert StudioLock(tmp_path).is_held() is True
        assert EventLock(tmp_path).is_held() is True
