"""Tests for the worker that drives the job queue.

**Almost nothing here starts a thread.** `Worker.drain_once()` performs one
whole pass - reap what finished, then claim and start what may run - so a
test drives the queue synchronously and asserts on the state that pass left
behind. The only tests that start the worker's thread are the ones in
`TestTheThread`, which are ABOUT the thread (it drains, a bad pass does not
kill it, `stop()` really ends it, and a stop that times out says so);
everything else would be a sleep-and-hope if it went through the thread,
which is the flaky shape this design exists to avoid. No thread any test
starts may outlive it - see `_no_thread_outlives_its_test`.

The jobs the worker starts are real `studio.jobs.start_*_job` calls wherever
that is affordable - a render with `render.build_short` stubbed, a trim with
`trim.ensure_applied` stubbed - so the worker's own parameter translation and
the real `EventLock` acquisition are both exercised. `transcribe` and
`detect` are the exception: their real starters would decode a stream or call
a paid model, so those two are driven with the STARTER itself replaced (which
still covers the translation, just not the starter's body, which
tests/test_studio_jobs.py already covers).

That is now a structural guarantee rather than a habit: `tests/conftest.py`'s
autouse `_no_real_job_starter` replaces all five `studio.jobs.start_*_job`
with one that FAILS the test, so a `drain_once()` written without a stub is
refused instead of starting real work. The tests here that legitimately
drive a real starter (or merely read its signature) say so by requesting the
`real_job_starters` fixture; `TestNoRealStarterRunsByAccident` is the guard's
own coverage.

No network, no ffmpeg, no Whisper decode, no API key, and no money anywhere
in this file.
"""

from __future__ import annotations

import inspect
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from yt_shorts import clipstore
from yt_shorts import profile as profile_module
from yt_shorts import workspace
from yt_shorts.cancel import CancelToken, Stopped
from yt_shorts.job_queue import JobQueue, QueueError
from yt_shorts.lock import EventLock, LockError
from yt_shorts.profile import load as profile_load
from yt_shorts.studio import jobs as jobs_module
from yt_shorts.studio import worker as worker_module

FIXTURE_CHANNELS = Path(__file__).parent / "fixtures" / "channels"
CLIP_URL = "https://www.youtube.com/clip/UgkxSpeedy123"

# A deadlock backstop for every wait below, not a performance guess (same
# convention as tests/test_studio_jobs.py): the work behind each one is
# stubbed, so reaching this means a thread never got there at all.
WAIT_TIMEOUT = 30.0


def clip_entry(url, hook, duration=60.0, error=None):
    return {"url": url, "hook": hook, "source_title": "ERF Round 3",
            "start": 10.0, "end": 10.0 + duration, "duration": duration,
            "error": error}


@pytest.fixture
def studio_profile(tmp_path, monkeypatch):
    """Two events under the fixture channel: the second one is what
    `test_a_locked_entry_does_not_block_an_entry_for_another_event` needs -
    the EventLock is per event directory, so a second event is the only way
    to have one entry blocked while another is free to start."""
    channels = tmp_path / "channels"
    shutil.copytree(FIXTURE_CHANNELS / "erf", channels / "erf")
    monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels)
    (channels / "erf" / "events" / "studio-test").mkdir(parents=True)
    (channels / "erf" / "events" / "other-event").mkdir(parents=True)
    return profile_load("erf/studio-test")


@pytest.fixture(autouse=True)
def _the_two_events(studio_profile):
    """Autouse: every test here enqueues entries naming `erf/studio-test` or
    `erf/other-event`, including the ones that build their own queue and
    worker. Without it those would resolve against the suite's shared
    fixture channels dir, where neither event exists, and fail for a reason
    that has nothing to do with what they measure."""
    return studio_profile


@pytest.fixture
def event_dir(studio_profile):
    return studio_profile.event_dir


@pytest.fixture
def other_event_dir(studio_profile):
    return profile_load("erf/other-event").event_dir


@pytest.fixture
def queue(tmp_path):
    return JobQueue(tmp_path / "jobs.json", jobs_module.KINDS,
                    dict(worker_module.DEFAULT_LIMITS))


@pytest.fixture
def store():
    return jobs_module.JobStore()


@pytest.fixture
def worker(queue, store):
    return worker_module.Worker(queue, store)


HERE = {"channel": "erf", "event": "studio-test"}
THERE = {"channel": "erf", "event": "other-event"}


def install_render_stub(monkeypatch, *, gate: threading.Event | None = None,
                        seen: list | None = None,
                        started: threading.Event | None = None):
    """Replaces render.build_short as jobs.py calls it (same technique as
    tests/test_studio_jobs.py), optionally capturing the cancel token it was
    handed and optionally blocking until the test releases it. `started` is
    set once the stub has run - after `seen` is filled and before the gate, so
    a test driving a THREAD waits on it instead of polling the list."""
    calls: list[str] = []

    def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
        calls.append(Path(work_dir).name)
        if seen is not None:
            seen.append(kwargs.get("cancel"))
        if started is not None:
            started.set()
        if gate is not None:
            # WAIT_TIMEOUT, not a small budget: this gate holds a render open
            # so the test can observe it mid-flight, and an early expiry ends
            # the job and fails the test for a timing reason.
            gate.wait(timeout=WAIT_TIMEOUT)
        Path(target).write_bytes(b"stub short")
        return target

    monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
    return calls


def fake_starter(monkeypatch, attribute: str, kind: str,
                 started: threading.Event | None = None):
    """Replaces one `jobs.start_*_job` with a recorder returning a Job the
    test controls. Used only where the real starter would transcribe or spend
    money; the returned Job records its cancel token exactly as the real
    starters do, since the worker reads `job.cancel` when a stop is asked
    for. `started` is set on the first call, so a test driving the worker's
    THREAD waits on it rather than polling the returned list."""
    made: list[dict] = []

    def fake(profile, job_store, *args, **kwargs):
        job = jobs_module.Job(f"fake-{kind}-{len(made)}", kind=kind)
        job.cancel = kwargs.get("cancel")
        made.append({"profile": profile, "args": args, "kwargs": kwargs, "job": job})
        if started is not None:
            started.set()
        return job

    monkeypatch.setattr(jobs_module, attribute, fake)
    return made


def wait_for_job(job, timeout=WAIT_TIMEOUT):
    """Waits on `job.finished`, which covers the runner's whole `finally`
    where a terminal `job.status` does not - see `jobs._finishing` for what
    that event promises and what it does not.

    The timeout is a deadlock backstop, not a performance budget: reaching it
    means a thread is genuinely wedged, never that the machine was slow.
    """
    assert job.finished.wait(timeout), (
        f"job {job.id} ({job.kind}) never signalled finished within {timeout}s "
        f"(status {job.status}) - its thread is wedged, not merely slow")
    return job


@pytest.fixture(autouse=True)
def _no_thread_outlives_its_test():
    """No thread a test starts may still be alive when the next one begins.

    Hygiene, and not cosmetic. A job thread that outlives its test holds a
    REAL `EventLock`, writes into a `tmp_path` pytest is about to remove,
    and shows up in `threading.enumerate()` - which `TestCreateAppWiring`
    asserts on by name. A leak therefore turns a LATER test into the
    reporter of an earlier one's untidiness: exactly how a mutation of
    `Worker.stop()` (which left `ytshorts-worker` threads running) came to
    be caught two classes away, pointing at the wrong test entirely.

    It WAITS rather than requiring every test to join: a job thread ends on
    its own within milliseconds once its work is done, and `JobStore.
    any_running()` goes False slightly before the thread has finished its
    own logging and lock release, so joining on the store alone would not
    be enough. A thread that never ends fails here, where it was started.
    """
    before = {thread.ident for thread in threading.enumerate()}
    yield
    deadline = time.monotonic() + 5.0
    while True:
        leaked = [t for t in threading.enumerate()
                  if t.is_alive() and t.ident not in before
                  and t is not threading.main_thread()]
        if not leaked or time.monotonic() > deadline:
            break
        time.sleep(0.01)
    assert not leaked, (
        f"thread(s) outlived the test: {[t.name for t in leaked]}")


def test_no_studio_e2e_server_thread_survives_into_this_module():
    """The actual guard `CLAUDE.md`'s E2E section claims: nothing
    `tests/test_studio_e2e.py` starts may outlive that file, because this
    file enumerates the process's threads by name (see
    `TestTheThread.test_stop_ends_the_thread_it_started` and
    `TestCreateAppWiring.test_the_worker_is_not_started_by_create_app_in_tests`
    above, both filtered to `t.name == worker_module.THREAD_NAME`).

    Those two assertions are NOT that guard: they match only threads named
    `"ytshorts-worker"`, and a stray uvicorn server thread from the e2e
    module's `_ServerThread` is never named that - it gets whatever default
    name `threading.Thread` hands out (`Thread-N`). A `studio_server` fixture
    changed from `scope="module"` to `scope="session"` was proven (by
    execution) to leave its server thread alive through this entire module,
    and the full suite still passed - neither of those name-filtered
    assertions, nor `_no_thread_outlives_its_test` above (which only diffs
    threads created and destroyed WITHIN one test in this file), notices a
    thread that was already running before this module's first test started.

    This is the guard that actually can: pytest collects this directory
    alphabetically (`test_studio_e2e.py` before `test_studio_jobs.py` before
    `test_studio_worker.py`), so when the full suite runs in its normal
    order, `test_studio_e2e.py`'s module-scoped `studio_server` fixture has
    already torn itself down - and asserted its own thread dead - before this
    function runs. A `_ServerThread` (or any thread whose name uvicorn
    itself chose) still alive here means that teardown did not happen on
    schedule, which is exactly what a session-scoped server does. Run this
    file alone and the check is trivially true (the e2e module never ran);
    it is the FULL-suite, default-order run this exists to protect."""
    survivors = [t for t in threading.enumerate()
                 if t.is_alive() and (type(t).__name__ == "_ServerThread"
                                       or t.name.startswith("uvicorn"))]
    assert not survivors, (
        "a studio e2e server thread is still alive at the start of "
        f"test_studio_worker.py: {[t.name for t in survivors]} - the "
        "module-scoped studio_server fixture in test_studio_e2e.py did not "
        "tear down on schedule (e.g. it was made session-scoped)")


def entry_by_id(queue, entry_id):
    return next(e for e in queue.list() if e.id == entry_id)


class TestDrainOnce:
    def test_drain_once_starts_the_head_of_the_queue(
            self, worker, queue, store, event_dir, monkeypatch,
            real_job_starters):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "A"))
        calls = install_render_stub(monkeypatch)
        entry = queue.enqueue("render", dict(HERE))

        worker.drain_once()

        started = entry_by_id(queue, entry.id)
        assert started.state == "running"
        # The entry names the job doing the work, so the Jobs screen can link
        # to that job's own log - a bare "running" would not be reachable.
        assert started.job_id is not None
        job = store.get(started.job_id)
        assert job is not None
        wait_for_job(job)
        assert calls == [directory.name], "the render never actually ran"

    def test_a_second_entry_waits_while_its_pool_is_full(
            self, worker, queue, store, event_dir, monkeypatch,
            real_job_starters):
        gate = threading.Event()
        install_render_stub(monkeypatch, gate=gate)
        first = queue.enqueue("render", dict(HERE))
        second = queue.enqueue("render", dict(THERE))
        try:
            worker.drain_once()

            assert entry_by_id(queue, first.id).state == "running"
            # cpu limit is 1: the second entry is for a DIFFERENT event (so
            # the lock is free) and still must not start - the pool, not the
            # lock, is what holds it back here.
            assert entry_by_id(queue, second.id).state == "queued"
        finally:
            # The gate used to be created inline and never released, so the
            # render thread outlived the test - see the autouse fixture.
            gate.set()

    def test_a_held_event_lock_leaves_the_entry_queued_with_a_reason(
            self, worker, queue, store, event_dir, monkeypatch,
            real_job_starters):
        # A CLI render holding this event's lock is a normal, temporary
        # condition. The entry waits and says why; it is NOT failed, and the
        # operator is not asked to retry something that was never wrong.
        # This is the PRE-CHECK path (`Worker._blocked_by`, asked before the
        # claim); the race the pre-check cannot close is the test below.
        install_render_stub(monkeypatch)
        held = EventLock(event_dir)
        held.acquire()
        entry = queue.enqueue("render", dict(HERE))
        try:
            worker.drain_once()

            waiting = entry_by_id(queue, entry.id)
            assert waiting.state == "queued"
            assert waiting.reason is not None
            assert "lock" in waiting.reason.lower()
            assert waiting.job_id is None
            assert not store.any_running(), "a job was started despite the lock"
        finally:
            held.release()

        # Once the lock is free the very same entry starts, with no retry and
        # no operator intervention - and its stale "waiting" reason is gone.
        worker.drain_once()
        started = entry_by_id(queue, entry.id)
        assert started.state == "running"
        assert started.reason is None

    def test_a_lock_taken_after_the_pre_check_defers_the_entry_not_fails_it(
            self, worker, queue, store, event_dir, monkeypatch,
            real_job_starters):
        """The pre-check reads the lock; the starter takes it. Between those
        two the CLI can start a render, and then only the starter's own
        `LockError` can tell the worker - which is why `JobQueue.defer` is
        still there and is not superseded by `_blocked_by`. This drives that
        window directly: the pre-check answers "free" and the lock is taken
        inside the very same call."""
        install_render_stub(monkeypatch)
        held = EventLock(event_dir)
        entry = queue.enqueue("render", dict(HERE))

        def a_cli_render_starts_right_now(_entry):
            held.acquire()
            return None          # ... and the worker is told the coast is clear

        monkeypatch.setattr(worker, "_blocked_by", a_cli_render_starts_right_now)
        try:
            worker.drain_once()
        finally:
            held.release()

        waiting = entry_by_id(queue, entry.id)
        assert waiting.state == "queued", (
            "a lock taken inside the start window failed the entry instead "
            "of putting it back")
        assert "lock" in (waiting.reason or "").lower()
        assert waiting.job_id is None
        assert not store.any_running()

    def test_a_lock_held_for_many_passes_rewrites_the_plan_only_once(
            self, worker, queue, event_dir, monkeypatch, tmp_path):
        """A CLI render can hold the lock for hours, i.e. thousands of
        passes. Claiming the entry and then deferring it is two full
        rewrites of jobs.json per pass, and - worse - the entry really is
        `running` on disk in between, so a Jobs screen reading the queue at
        the wrong instant reports a run that never happened. Nothing about
        the world changes between those passes, so nothing may be written.
        """
        install_render_stub(monkeypatch)
        held = EventLock(event_dir)
        held.acquire()
        entry = queue.enqueue("render", dict(HERE))
        try:
            worker.drain_once()      # the first pass records why it is waiting
            saves = []
            real_save = queue.save

            def counting_save():
                saves.append(1)
                real_save()

            monkeypatch.setattr(queue, "save", counting_save)
            for _ in range(5):
                worker.drain_once()
        finally:
            held.release()

        assert saves == [], (
            f"{len(saves)} rewrite(s) of jobs.json across five passes in "
            f"which nothing changed")
        # And on disk it never once said `running`.
        reread = JobQueue(queue.path, jobs_module.KINDS,
                          dict(worker_module.DEFAULT_LIMITS))
        assert entry_by_id(reread, entry.id).state == "queued"

    def test_a_deferred_entry_is_passed_over_for_the_rest_of_the_pass(
            self, worker, queue, monkeypatch):
        """`claim_next(skip=...)` is what makes a pass TERMINATE, not just
        what makes it fair: a deferred entry goes back to `queued` in its
        original place, so without `skip` it is the very next thing
        `claim_next` returns - forever. That is a HANG, which a test suite
        reports as nothing at all, so this bounds the number of claims one
        pass may make and turns it into a failure."""
        def refuses_the_lock(*args, **kwargs):
            raise LockError("Event 'studio-test' is locked by process 4242")

        monkeypatch.setattr(jobs_module, "start_render_job", refuses_the_lock)
        queue.enqueue("render", dict(HERE))
        queue.enqueue("render", dict(THERE))
        claims = []
        real_claim = queue.claim_next

        def counting_claim(**kwargs):
            claims.append(kwargs.get("skip"))
            assert len(claims) <= 6, (
                "one pass kept claiming entries - a deferred entry is being "
                "offered again instead of passed over, and the loop cannot end")
            return real_claim(**kwargs)

        monkeypatch.setattr(queue, "claim_next", counting_claim)

        worker.drain_once()

        # Two entries, each claimed once, then the call that finds nothing.
        assert len(claims) == 3
        assert [e.state for e in queue.list()] == ["queued", "queued"]

    def test_the_lock_error_it_catches_is_looked_up_where_it_is_raised(
            self, worker, queue, store, monkeypatch):
        """`worker.py` imports the lock MODULE and catches
        `lock.LockError`, never `from ..lock import LockError`, and that is
        not a style choice. An `except` clause holds the class object it
        was handed at import time; `importlib.reload` (which
        tests/test_profile.py already does to `yt_shorts.profile`) builds a
        NEW class object of the same name, so a name bound at collection
        time stops being the class that is actually raised. That cost this
        module a `ProfileError` escaping a whole pass once - and here it
        would be worse and silent: a held lock would miss its handler, fall
        through to `except Exception` and FAIL the entry instead of
        deferring it, breaking this module's first rule with no noise.

        Reloading `yt_shorts.lock` for real would leave every OTHER module
        in the session holding the old class, so this stands the hazard up
        with a module-shaped stand-in whose `LockError` is a different
        class object of the same name - which is exactly what a reload
        leaves behind.
        """
        import types
        reloaded = types.SimpleNamespace(
            LockError=type("LockError", (Exception,), {}), EventLock=EventLock)
        monkeypatch.setattr(worker_module, "lock", reloaded)

        def refuses_the_lock(*args, **kwargs):
            raise reloaded.LockError(
                "Event 'studio-test' is locked by process 4242")

        monkeypatch.setattr(jobs_module, "start_render_job", refuses_the_lock)
        entry = queue.enqueue("render", dict(HERE))

        worker.drain_once()

        waiting = entry_by_id(queue, entry.id)
        assert waiting.state == "queued", (
            "a held event lock failed the entry instead of deferring it - "
            "the except clause is holding a stale class")
        assert "lock" in (waiting.reason or "").lower()
        assert not store.any_running()

    def test_a_job_that_ends_with_an_unexpected_status_fails_its_entry(
            self, worker, queue, monkeypatch):
        """`_STATE_FOR_STATUS` maps done/failed/stopped. A Job that ends in
        any other state is one this module does not understand, and the one
        reading it must not be `done`: that would tell the operator a short
        they never got is sitting on disk. It fails, naming the status, so
        the defect is diagnosable instead of merely reported."""
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        entry = queue.enqueue("detect", dict(HERE, video_id="vid1"))
        worker.drain_once()

        made[0]["job"].finish("evaporated")
        worker.drain_once()

        finished = entry_by_id(queue, entry.id)
        assert finished.state == "failed"
        assert "evaporated" in (finished.reason or "")

    def test_a_locked_entry_does_not_block_an_entry_for_another_event(
            self, worker, queue, store, event_dir, other_event_dir, monkeypatch,
            real_job_starters):
        install_render_stub(monkeypatch)
        held = EventLock(event_dir)
        held.acquire()
        blocked = queue.enqueue("render", dict(HERE))
        free = queue.enqueue("render", dict(THERE))
        try:
            worker.drain_once()
        finally:
            held.release()

        assert entry_by_id(queue, blocked.id).state == "queued"
        assert entry_by_id(queue, free.id).state == "running", (
            "an entry whose event was locked blocked an entry for another event")

    def test_a_finished_job_marks_its_entry_done(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        entry = queue.enqueue("detect", dict(HERE, video_id="vid1"))
        worker.drain_once()
        assert entry_by_id(queue, entry.id).state == "running"

        made[0]["job"].finish("done")
        worker.drain_once()

        assert entry_by_id(queue, entry.id).state == "done"

    def test_a_failed_job_marks_its_entry_failed_and_carries_its_reason(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        entry = queue.enqueue("detect", dict(HERE, video_id="vid1"))
        worker.drain_once()

        job = made[0]["job"]
        job.record("detect", "failed", "RuntimeError: the model was unreachable",
                   "ERROR: the model was unreachable")
        job.finish("failed")
        worker.drain_once()

        failed = entry_by_id(queue, entry.id)
        assert failed.state == "failed"
        assert "unreachable" in (failed.reason or ""), (
            "the entry says it failed but not why - the job's own reason was lost")

    def test_a_stopped_job_marks_its_entry_stopped_not_failed(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        entry = queue.enqueue("detect", dict(HERE, video_id="vid1"))
        worker.drain_once()

        made[0]["job"].finish("stopped")
        worker.drain_once()

        assert entry_by_id(queue, entry.id).state == "stopped"

    def test_the_pool_slot_is_released_only_once_the_job_has_finished(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_render_job", "render")
        first = queue.enqueue("render", dict(HERE))
        second = queue.enqueue("render", dict(THERE))
        worker.drain_once()
        assert entry_by_id(queue, second.id).state == "queued"

        made[0]["job"].finish("done")
        worker.drain_once()

        assert entry_by_id(queue, first.id).state == "done"
        assert entry_by_id(queue, second.id).state == "running"

    def test_an_entry_missing_a_parameter_fails_with_a_reason_and_the_pass_goes_on(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        broken = queue.enqueue("detect", {"video_id": "vid1"})   # no channel/event
        good = queue.enqueue("detect", dict(HERE, video_id="vid2"))

        worker.drain_once()

        failed = entry_by_id(queue, broken.id)
        assert failed.state == "failed"
        assert "channel" in (failed.reason or "")
        assert entry_by_id(queue, good.id).state == "running", (
            "one malformed entry stopped the rest of the pass")
        assert len(made) == 1

    def test_an_unknown_event_fails_the_entry_rather_than_the_worker(
            self, queue, store, monkeypatch):
        """Regression: this used to pass alone and fail inside the full
        suite. The `worker` fixture builds a `Worker` with NO
        `profile_loader`, which defaults to `_load_profile_by_name` - a name
        `worker.py` binds once via `from ..profile import load` at COLLECTION
        time (module import), before any test body has run. tests/
        test_profile.py's `TestChannelsComeFromTheWorkspace` tests
        `importlib.reload(profile_module)` to prove `CHANNELS_DIR` tracks
        workspace injection, and a reload re-executes the module's class
        statements IN THE SAME namespace dict - so `profile.ProfileError`
        after a reload is a NEW class object, while `worker.py`'s own
        `except (ParamError, ProfileError)` (before this fix broadened it to
        `except Exception`) still held the OLD one from collection time.
        `profile.load`'s `raise ProfileError(...)` resolves the name from
        its own (shared, mutated-in-place) module globals at CALL time, so
        after test_profile.py has run once in the session, an unknown event
        raised a `ProfileError` that `isinstance()` no longer recognised as
        one - an exception with nothing here to catch it, which failed the
        WORKER's pass instead of the entry. Alone, or before test_profile.py
        collects, the stale and current classes are identical and the bug
        does not show.

        The production fix (`_start`'s profile-loading now falls under the
        same broad `except Exception` its starter-call already had) closes
        this regardless of class identity. This test additionally builds its
        OWN `Worker` with `profile_loader=profile_module.load` - an
        ATTRIBUTE lookup on the live module, done here at test-run time,
        rather than the fixture's collection-time-frozen default - so the
        precondition is explicit: this test names the exact loader it wants
        exercised, rather than incidentally trusting whichever `load` and
        `ProfileError` `worker.py` happened to import before some other test
        module got a chance to reload `yt_shorts.profile` out from under it.
        """
        fake_starter(monkeypatch, "start_detect_job", "detect")
        worker = worker_module.Worker(queue, store, profile_loader=profile_module.load)
        entry = queue.enqueue("detect", {"channel": "erf", "event": "no-such-event",
                                         "video_id": "vid1"})

        worker.drain_once()

        failed = entry_by_id(queue, entry.id)
        assert failed.state == "failed"
        assert "no-such-event" in (failed.reason or "")

    def test_a_synchronous_stop_is_never_relabelled_a_failure(
            self, worker, queue, monkeypatch):
        """`_start` now runs profile resolution and the starter's own
        synchronous work under one broad `except Exception`, so an entry
        whose profile cannot be resolved fails cleanly (see the test above)
        instead of taking the whole pass down. `cancel.Stopped` is not a
        failure - it means the operator asked for this (see its own
        docstring) - so it must never be caught by that same blanket
        handler. Nothing in `STARTERS` today raises `Stopped` synchronously
        (see `_start`'s own comment on this), so this drives it with a
        starter replaced to raise it directly, standing in for a future one
        that checks its cancel token before handing off to a thread. If the
        `except Stopped: raise` carve-out in `_start` is ever removed (or a
        future edit reorders it below the `except Exception`), `Stopped`
        would be swallowed and reported as an ordinary failed entry instead
        of propagating - this pins that it does not."""
        def raises_stopped(profile, job_store, *args, **kwargs):
            raise Stopped("stop requested before the job ever started")

        monkeypatch.setattr(jobs_module, "start_detect_job", raises_stopped)
        entry = queue.enqueue("detect", dict(HERE, video_id="vid1"))

        with pytest.raises(Stopped):
            worker.drain_once()

        # Above all, not "failed": claim_next() already marked it "running"
        # before _start ever ran (see job_queue.claim_next), and Stopped
        # propagating out of drain_once left it exactly there rather than
        # a handler downgrading it to a false "failed" - the one outcome
        # this test exists to rule out.
        left = entry_by_id(queue, entry.id)
        assert left.state == "running"
        assert left.job_id is None

    def test_the_stopped_exception_it_catches_is_looked_up_where_it_is_raised(
            self, worker, queue, store, monkeypatch):
        """Same hazard as `test_the_lock_error_it_catches_is_looked_up_
        where_it_is_raised` above, for the OTHER exception `_start` imports
        as a module and catches by attribute lookup: `cancel.Stopped`. A
        stale, collection-time-bound `Stopped` would miss one raised after
        `yt_shorts.cancel` is reloaded and fall through to the blanket
        `except Exception` two clauses below - which does NOT re-raise, it
        calls `_fail` and marks the entry `failed`. That is exactly the
        mislabelling `cancel.Stopped`'s own docstring and this module's
        second binding constraint forbid: a stop must never be recorded as a
        failure.

        Stood up the same way as the LockError test: a module-shaped
        stand-in whose `Stopped` is a different class object of the same
        name - what a real `importlib.reload` would leave behind - rather
        than reloading `yt_shorts.cancel` itself, which would leave every
        OTHER module in the session holding the old class.
        """
        import types
        reloaded = types.SimpleNamespace(Stopped=type("Stopped", (Exception,), {}))
        monkeypatch.setattr(worker_module, "cancel", reloaded)

        def raises_stopped(profile, job_store, *args, **kwargs):
            raise reloaded.Stopped("stop requested before the job ever started")

        monkeypatch.setattr(jobs_module, "start_detect_job", raises_stopped)
        entry = queue.enqueue("detect", dict(HERE, video_id="vid1"))

        with pytest.raises(reloaded.Stopped):
            worker.drain_once()

        left = entry_by_id(queue, entry.id)
        assert left.state == "running", (
            "a Stopped raised after a cancel reload was not recognised - the "
            "except clause is holding a stale class, and the stop was "
            "relabelled a failure instead of propagating")
        assert left.job_id is None

    def test_a_kind_the_worker_cannot_start_fails_loudly(self, store, tmp_path):
        # Every kind in jobs.KINDS that the queue accepts HAS a starter (see
        # TestStarterTable below), so this drives a queue built with a kinds
        # table of its own - the shape a future kind added to KINDS without a
        # starter would have. It must name the gap, not crash the pass.
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Spec:
            pool: str
            queueable: bool = True

        queue = JobQueue(tmp_path / "jobs.json", {"levitate": Spec("cpu")}, {})
        worker = worker_module.Worker(queue, store)
        entry = queue.enqueue("levitate", dict(HERE))

        worker.drain_once()

        failed = entry_by_id(queue, entry.id)
        assert failed.state == "failed"
        assert "levitate" in (failed.reason or "")


class TestParameterTranslation:
    """What the worker hands each starter. A queue entry is a plan on disk;
    these are the four translations that turn one back into a call."""

    def test_a_render_entry_passes_its_clip_names(self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_render_job", "render")
        queue.enqueue("render", dict(HERE, clips=["clip-a", "clip-b"]))
        worker.drain_once()
        assert made[0]["args"][0] == ["clip-a", "clip-b"]
        assert made[0]["profile"].event_dir.name == "studio-test"

    def test_a_render_entry_with_no_clip_names_renders_them_all(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_render_job", "render")
        queue.enqueue("render", dict(HERE))
        worker.drain_once()
        assert made[0]["args"][0] is None   # jobs.py's "every clip" spelling

    def test_a_transcribe_entry_passes_its_video_id(self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_transcribe_job", "transcribe")
        queue.enqueue("transcribe", dict(HERE, video_id="vid-abc"))
        worker.drain_once()
        assert made[0]["args"][0] == "vid-abc"

    def test_a_detect_entry_passes_its_video_id_and_title(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        queue.enqueue("detect", dict(HERE, video_id="vid-abc", stream_title="Race 3"))
        worker.drain_once()
        assert made[0]["args"][:2] == ("vid-abc", "Race 3")

    def test_a_trim_entry_passes_its_clip_name(self, worker, queue, event_dir,
                                               monkeypatch, real_job_starters):
        # The REAL start_trim_job, with only the cut itself stubbed - so the
        # clip name the worker read out of the entry has to survive the whole
        # way into trim.ensure_applied's own directory argument.
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "A"))
        seen: list = []
        cut = threading.Event()   # the stub was reached; `seen` is filled

        def fake_ensure_applied(clip_dir, edit, **kwargs):
            seen.append(clip_dir)
            cut.set()
            return True

        monkeypatch.setattr(jobs_module.trim, "ensure_applied", fake_ensure_applied)
        queue.enqueue("trim", dict(HERE, clip=directory.name))
        worker.drain_once()
        assert cut.wait(WAIT_TIMEOUT), "the trim never reached ensure_applied"
        assert Path(seen[0]).name == directory.name

    def test_a_queued_upload_is_private_and_never_scheduled(
            self, worker, queue, monkeypatch):
        # The privacy invariant (CLAUDE.md, stage E): anything more exposed
        # than private is an explicit, CONFIRMED, per-upload choice made at
        # the route. A queue entry carries no confirmation, so an entry
        # asking for one is refused rather than quietly uploaded as public.
        made = fake_starter(monkeypatch, "start_upload_job", "upload")
        public = queue.enqueue("upload", dict(HERE, clip="clip-a", visibility="public"))
        scheduled = queue.enqueue("upload", dict(HERE, clip="clip-b",
                                                 publish_at="2026-08-02T10:00:00Z"))
        plain = queue.enqueue("upload", dict(HERE, clip="clip-c"))

        worker.drain_once()

        assert entry_by_id(queue, public.id).state == "failed"
        assert "private" in (entry_by_id(queue, public.id).reason or "")
        assert entry_by_id(queue, scheduled.id).state == "failed"
        assert entry_by_id(queue, plain.id).state == "running"
        assert len(made) == 1
        assert made[0]["args"][0] == "clip-c"
        assert "visibility" not in made[0]["kwargs"]


class TestRecovery:
    def test_recovery_marks_a_previously_running_entry_interrupted(
            self, tmp_path, store):
        path = tmp_path / "jobs.json"
        before = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        entry = before.enqueue("render", dict(HERE))
        before.claim_next()                      # the process dies here

        after = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        assert entry_by_id(after, entry.id).state == "running"   # still, on disk
        worker_module.Worker(after, store).recover()

        assert entry_by_id(after, entry.id).state == "interrupted"

    def test_construction_alone_recovers_nothing(self, tmp_path, store):
        """Recovery is what a studio ABOUT TO DRAIN the queue does, not what
        building an object does - it used to run in `Worker.__init__`.

        Two costs, and the second is why a whole-branch review called it
        blocking. Every `create_app()` in this ~2200-test suite rewrote
        whatever plan it found on disk. And because `cmd_studio` picks a free
        port when 8765 is busy, a SECOND studio against one workspace starts
        happily - so merely CONSTRUCTING its worker marked the first
        studio's genuinely-running two-hour transcription `interrupted`, a
        state whose own text says it was running when the studio died.
        `lock.StudioLock` refuses that second studio now; this pins the other
        half, which stands on its own.
        """
        path = tmp_path / "jobs.json"
        before = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        entry = before.enqueue("transcribe", dict(HERE, video_id="vid1"))
        before.claim_next()          # a genuinely running job in another process

        after = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        worker_module.Worker(after, store)

        assert entry_by_id(after, entry.id).state == "running"
        # …and on disk, which is what the other process would read back.
        reread = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        assert entry_by_id(reread, entry.id).state == "running"

    def test_start_recovers_before_the_thread_drains(self, tmp_path, store):
        # `start()` is the studio's own "I am about to run this queue", so
        # that is where recovery belongs. Ordered before the thread on
        # purpose: an entry left `running` still holds its pool slot, so a
        # pass that ran first could find a full cpu pool and start nothing.
        path = tmp_path / "jobs.json"
        before = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        entry = before.enqueue("render", dict(HERE))
        before.claim_next()

        after = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        worker = worker_module.Worker(after, store)
        try:
            worker.start()
        finally:
            worker.stop(timeout=5.0)

        assert entry_by_id(after, entry.id).state == "interrupted"

    def test_an_interrupted_entry_never_restarts_by_itself(
            self, tmp_path, store, monkeypatch):
        # A detection run spends real money: nothing may re-queue it except
        # an explicit retry.
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        path = tmp_path / "jobs.json"
        before = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        entry = before.enqueue("detect", dict(HERE, video_id="vid1"))
        before.claim_next()

        after = JobQueue(path, jobs_module.KINDS, dict(worker_module.DEFAULT_LIMITS))
        worker = worker_module.Worker(after, store)
        worker.recover()
        worker.drain_once()
        worker.drain_once()

        assert entry_by_id(after, entry.id).state == "interrupted"
        assert made == [], "an interrupted entry started again on its own"

        after.retry(entry.id)
        worker.drain_once()
        assert entry_by_id(after, entry.id).state == "running"
        assert len(made) == 1


class TestAKindThisBuildCannotRun:
    """The end-to-end shape of the wedge, with the REAL kinds table.

    `tests/test_job_queue.py` pins the claim path in isolation; this is the
    scenario a whole-branch review reproduced: an entry whose kind this
    build does not know sat at the head of a real plan, `claim_next` raised
    `KeyError` from inside `drain_once`, the worker's loop caught it and
    retried a second later, forever - and no job of any kind for any event
    ever started again while the screen said "next in line".
    """

    def test_a_pass_over_an_unknown_kind_neither_raises_nor_stops_the_queue(
            self, tmp_path, store, monkeypatch):
        made = fake_starter(monkeypatch, "start_render_job", "render")
        path = tmp_path / "jobs.json"
        path.write_text(json.dumps({"entries": [
            {"id": "planted", "kind": "teleport", "params": dict(HERE),
             "state": "queued", "reason": None, "progress": None,
             "created_at": 1.0, "after": None, "job_id": None},
            {"id": "good", "kind": "render", "params": dict(HERE),
             "state": "queued", "reason": None, "progress": None,
             "created_at": 2.0, "after": None, "job_id": None},
        ]}), encoding="utf-8")
        queue = JobQueue(path, jobs_module.KINDS,
                         dict(worker_module.DEFAULT_LIMITS))
        worker = worker_module.Worker(queue, store)

        worker.drain_once()          # must not raise

        planted = entry_by_id(queue, "planted")
        assert planted.state == "failed"
        assert "teleport" in planted.reason
        assert entry_by_id(queue, "good").state == "running"
        assert len(made) == 1, "the queue stopped at the entry it could not run"


class TestStopping:
    """The transition the queue had no way to produce until this task: a
    running entry becomes `stopping`, and the token the operator's click
    reaches is the one the JOB carries (`job.cancel`) - the worker reads it
    rather than keeping a second copy, so a starter that stopped recording
    its token would break stopping loudly instead of silently."""

    def test_a_stop_reaches_the_running_job_s_token_and_moves_the_entry_to_stopping(
            self, worker, queue, event_dir, monkeypatch, real_job_starters):
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "A"))
        gate = threading.Event()
        seen: list = []
        started = threading.Event()
        install_render_stub(monkeypatch, gate=gate, seen=seen, started=started)
        entry = queue.enqueue("render", dict(HERE))
        worker.drain_once()
        assert started.wait(WAIT_TIMEOUT), "the render never started"
        token = seen[0]
        assert token is not None, "the worker started a stoppable job with no token"
        assert token.stop_requested is False

        worker.request_stop(entry.id)

        assert token.stop_requested is True, (
            "the stop never reached the token the work is actually checking")
        assert token.kill_requested is False, "a plain stop must not kill a child"
        assert entry_by_id(queue, entry.id).state == "stopping"
        gate.set()

    def test_a_hard_stop_asks_for_a_kill_where_the_kind_allows_it(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_render_job", "render")
        entry = queue.enqueue("render", dict(HERE))
        worker.drain_once()

        worker.request_stop(entry.id, force=True)

        token = made[0]["job"].cancel
        assert token.kill_requested is True
        assert entry_by_id(queue, entry.id).state == "stopping"

    def test_a_hard_stop_for_an_upload_is_refused_not_downgraded(
            self, worker, queue, monkeypatch):
        # KINDS["upload"].hard_stop_allowed is False and there must be no
        # quiet fallback to a graceful stop either: a half-finished upload to
        # YouTube is worse than waiting for it.
        fake_starter(monkeypatch, "start_upload_job", "upload")
        entry = queue.enqueue("upload", dict(HERE, clip="clip-a"))
        worker.drain_once()

        with pytest.raises(QueueError) as exc:
            worker.request_stop(entry.id, force=True)
        assert exc.value.kind == "no_hard_stop"
        assert entry_by_id(queue, entry.id).state == "running"

    def test_a_graceful_stop_for_an_upload_is_refused_too(
            self, worker, queue, monkeypatch):
        fake_starter(monkeypatch, "start_upload_job", "upload")
        entry = queue.enqueue("upload", dict(HERE, clip="clip-a"))
        worker.drain_once()

        with pytest.raises(QueueError) as exc:
            worker.request_stop(entry.id)
        assert exc.value.kind == "not_stoppable"
        assert entry_by_id(queue, entry.id).state == "running"

    def test_a_hard_stop_on_an_already_stopping_entry_is_performed_not_refused(
            self, worker, queue, monkeypatch):
        """The escalation an operator reaches for when a graceful stop is
        minutes away, and the bug that used to make it a lie.

        `request_stop(force=True)` called `token.request_kill()` and THEN
        `queue.mark_stopping()`, which refuses any state but `running` - so
        on an entry already sitting in `stopping` the call raised
        `QueueError(kind="invalid_state")` **while the kill had already been
        requested**. Nothing was corrupt (the entry was where it belonged
        and `_reap` finished the job) but the operator was told the
        escalation was refused for a kill that was actually being performed:
        this project's recurring lie, in the opposite direction. The
        ordering is what this test pins - swap the two lines back and the
        `pytest.raises`-free call below fails.
        """
        made = fake_starter(monkeypatch, "start_render_job", "render")
        entry = queue.enqueue("render", dict(HERE))
        worker.drain_once()
        worker.request_stop(entry.id)
        token = made[0]["job"].cancel
        assert entry_by_id(queue, entry.id).state == "stopping"
        assert token.kill_requested is False

        escalated = worker.request_stop(entry.id, force=True)

        assert token.kill_requested is True
        assert escalated.state == "stopping"
        assert entry_by_id(queue, entry.id).state == "stopping"

    def test_a_second_graceful_stop_on_a_stopping_entry_is_still_refused(
            self, worker, queue, monkeypatch):
        # Only the ESCALATION is tolerated. A repeat of the same graceful
        # request asks for nothing that was not already asked for, so it
        # keeps saying so rather than answering 202 for a no-op.
        fake_starter(monkeypatch, "start_render_job", "render")
        entry = queue.enqueue("render", dict(HERE))
        worker.drain_once()
        worker.request_stop(entry.id)

        with pytest.raises(QueueError) as exc:
            worker.request_stop(entry.id)
        assert exc.value.kind == "invalid_state"
        assert entry_by_id(queue, entry.id).state == "stopping"

    def test_a_stopping_upload_cannot_be_escalated_either(
            self, worker, queue, monkeypatch):
        # Upload has no stop at ANY level, and the new tolerance must not
        # become a back door into one: it is refused before the entry's own
        # state is ever consulted.
        fake_starter(monkeypatch, "start_upload_job", "upload")
        entry = queue.enqueue("upload", dict(HERE, clip="clip-a"))
        worker.drain_once()

        with pytest.raises(QueueError) as exc:
            worker.request_stop(entry.id, force=True)
        assert exc.value.kind == "no_hard_stop"
        assert entry_by_id(queue, entry.id).state == "running"

    def test_a_stopping_entry_that_finishes_is_reaped_as_stopped(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_render_job", "render")
        entry = queue.enqueue("render", dict(HERE))
        worker.drain_once()
        worker.request_stop(entry.id)
        assert entry_by_id(queue, entry.id).state == "stopping"

        made[0]["job"].finish("stopped")
        worker.drain_once()

        assert entry_by_id(queue, entry.id).state == "stopped"

    def test_a_stop_for_an_entry_that_is_not_running_is_refused(
            self, worker, queue):
        entry = queue.enqueue("render", dict(HERE))
        with pytest.raises(QueueError) as exc:
            worker.request_stop(entry.id)
        assert exc.value.kind == "invalid_state"
        assert entry_by_id(queue, entry.id).state == "queued"

    def test_a_stopping_entry_keeps_holding_its_pool_slot(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_render_job", "render")
        first = queue.enqueue("render", dict(HERE))
        second = queue.enqueue("render", dict(THERE))
        worker.drain_once()
        worker.request_stop(first.id)

        worker.drain_once()

        assert entry_by_id(queue, first.id).state == "stopping"
        assert entry_by_id(queue, second.id).state == "queued", (
            "the pool handed the slot on while the first job was still stopping")
        assert len(made) == 1


class TestStarterTable:
    """The table is the wiring, so it is enumerated rather than spot-checked:
    a kind added to KINDS without a starter, or a starter marked stoppable
    that cannot actually take a token, fails here."""

    def test_every_queueable_kind_has_a_starter(self):
        missing = [kind for kind, spec in jobs_module.KINDS.items()
                   if spec.queueable and kind not in worker_module.STARTERS]
        assert not missing, (
            f"the queue accepts {missing} but the worker cannot start them - "
            f"either add a starter or mark the kind not queueable")

    def test_no_starter_exists_for_a_kind_the_queue_refuses(self):
        # The other direction: a starter for a non-queueable kind would be
        # dead code that looks like a promise.
        extra = [kind for kind in worker_module.STARTERS
                 if not jobs_module.KINDS[kind].queueable]
        assert not extra

    def test_a_starter_is_marked_stoppable_exactly_when_its_job_takes_a_token(
            self, real_job_starters):
        for kind, starter in worker_module.STARTERS.items():
            real = getattr(jobs_module, f"start_{kind}_job")
            accepts = "cancel" in inspect.signature(real).parameters
            assert starter.stoppable == accepts, (
                f"{kind}: the worker says stoppable={starter.stoppable} but "
                f"start_{kind}_job {'takes' if accepts else 'does not take'} "
                f"a cancel token")

    def test_a_kind_names_a_progress_unit_exactly_when_its_job_can_report(
            self, real_job_starters):
        # The same shape as the cancel pin above, for the same reason: the
        # unit in KINDS is what the worker turns two numbers into, and a
        # kind that named one whose starter takes no `progress` would
        # promise a reading nothing could ever write - a permanently empty
        # column with a unit attached to it.
        for kind, spec in jobs_module.KINDS.items():
            real = getattr(jobs_module, f"start_{kind}_job", None)
            assert real is not None, f"KINDS names {kind} with no starter"
            accepts = "progress" in inspect.signature(real).parameters
            assert (spec.progress_unit is not None) == accepts, (
                f"{kind}: KINDS names progress_unit="
                f"{spec.progress_unit!r} but start_{kind}_job "
                f"{'takes' if accepts else 'does not take'} a progress "
                f"callback")

    def test_every_named_unit_is_a_singular_noun_a_screen_can_render(self):
        # "chunk 20 of 50" - a blank or padded unit renders as a broken
        # label, and the client only falls back to its own table when the
        # server names none at all.
        for kind, spec in jobs_module.KINDS.items():
            if spec.progress_unit is None:
                continue
            assert spec.progress_unit == spec.progress_unit.strip()
            assert spec.progress_unit, f"{kind} names a blank unit"

    def test_a_stoppable_kind_is_started_with_a_token_and_the_others_are_not(
            self, worker, queue, monkeypatch):
        detect = fake_starter(monkeypatch, "start_detect_job", "detect")
        upload = fake_starter(monkeypatch, "start_upload_job", "upload")
        queue.enqueue("detect", dict(HERE, video_id="vid1"))
        queue.enqueue("upload", dict(HERE, clip="clip-a"))

        worker.drain_once()

        assert isinstance(detect[0]["kwargs"].get("cancel"), CancelToken)
        assert "cancel" not in upload[0]["kwargs"]


# Every kind that says it counts something, straight off the table - so a
# kind added with a unit is covered here without anyone remembering to add
# it, and one whose unit is removed stops being asserted about.
_REPORTING_KINDS = sorted(k for k, s in jobs_module.KINDS.items()
                          if s.progress_unit is not None and k in worker_module.STARTERS)
_SILENT_KINDS = sorted(k for k, s in jobs_module.KINDS.items()
                       if s.progress_unit is None and k in worker_module.STARTERS)


class TestProgressReachesTheEntry:
    """The route from the work's own `(done, total)` to `Entry.progress`.

    Both lists are derived from `jobs_module.KINDS` rather than written out
    here: this branch shipped eight tasks' worth of a screen with no
    producer behind it, and the way that happened was a wiring point whose
    siblings were enumerated and which was not added to the enumeration.
    """

    def _params(self, kind):
        return dict(HERE, video_id="vid-p", clip="clip-a")

    @pytest.mark.parametrize("kind", _REPORTING_KINDS)
    def test_what_the_work_reports_lands_on_the_entry_with_its_unit(
            self, worker, queue, monkeypatch, kind):
        made = fake_starter(monkeypatch, f"start_{kind}_job", kind)
        entry = queue.enqueue(kind, self._params(kind))
        worker.drain_once()

        report = made[0]["kwargs"]["progress"]
        report(3, 9)

        stored = entry_by_id(queue, entry.id)
        assert stored.progress == {
            "unit": jobs_module.KINDS[kind].progress_unit, "done": 3, "total": 9}
        # And on disk, which is the only place the Jobs screen can read it
        # from after a restart.
        reread = JobQueue(queue.path, jobs_module.KINDS,
                          dict(worker_module.DEFAULT_LIMITS))
        assert next(e for e in reread.list() if e.id == entry.id).progress == {
            "unit": jobs_module.KINDS[kind].progress_unit, "done": 3, "total": 9}

    @pytest.mark.parametrize("kind", _SILENT_KINDS)
    def test_a_kind_that_counts_nothing_is_handed_no_callback(
            self, worker, queue, monkeypatch, kind):
        # `trim` is one cut and `upload` reports bytes to a layer this code
        # does not watch. Neither may be given a callback that would invent
        # "1 of 1" for it - the screen shows nothing instead.
        made = fake_starter(monkeypatch, f"start_{kind}_job", kind)
        queue.enqueue(kind, self._params(kind))
        worker.drain_once()

        assert "progress" not in made[0]["kwargs"]

    def test_a_reading_is_replaced_not_accumulated(self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        entry = queue.enqueue("detect", dict(HERE, video_id="vid-p"))
        worker.drain_once()

        report = made[0]["kwargs"]["progress"]
        report(1, 9)
        report(2, 9)

        assert entry_by_id(queue, entry.id).progress == {
            "unit": "window", "done": 2, "total": 9}

    def test_the_job_id_the_worker_recorded_survives_a_reading(
            self, worker, queue, monkeypatch):
        # `mark_running` is the one method that writes both, and a progress
        # call passes no job_id - it must not blank the one already there,
        # or the row's "View log" link would vanish on the first reading.
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        entry = queue.enqueue("detect", dict(HERE, video_id="vid-p"))
        worker.drain_once()

        made[0]["kwargs"]["progress"](1, 9)

        assert entry_by_id(queue, entry.id).job_id == made[0]["job"].id

    def test_a_finished_job_leaves_no_stale_reading_behind(
            self, worker, queue, monkeypatch):
        made = fake_starter(monkeypatch, "start_detect_job", "detect")
        entry = queue.enqueue("detect", dict(HERE, video_id="vid-p"))
        worker.drain_once()
        made[0]["kwargs"]["progress"](8, 9)

        made[0]["job"].finish("done")
        worker.drain_once()   # reaps it

        stored = entry_by_id(queue, entry.id)
        assert stored.state == "done"
        assert stored.progress is None, (
            "a finished row still claimed to be 8 of 9 windows into work "
            "that is over")

    def test_a_failing_progress_write_never_reaches_the_work(
            self, worker, queue, monkeypatch):
        """The guarantee the whole wrapper exists for.

        The work runs for hours; the queue write can fail (a full disk, a
        vanished workspace) and REFUSES anything but a `running` entry,
        which happens on the ordinary path once a stop has been asked for.
        Neither may travel back into a two-hour transcription.
        """
        made = fake_starter(monkeypatch, "start_transcribe_job", "transcribe")
        queue.enqueue("transcribe", dict(HERE, video_id="vid-p"))
        worker.drain_once()
        report = made[0]["kwargs"]["progress"]

        real_save = queue.save
        failing = {"now": True}

        def boom():
            if failing["now"]:
                raise OSError("no space left on device")
            real_save()

        monkeypatch.setattr(queue, "save", boom)
        report(1, 50)          # must not raise

        # And the other, ordinary way it fails: the entry has moved on.
        failing["now"] = False
        entry = queue.list()[0]
        queue.mark_stopping(entry.id)
        report(2, 50)          # must not raise either

    def test_a_real_transcription_finishes_with_every_write_failing(
            self, worker, queue, monkeypatch, tmp_path):
        """The claim rule 5 actually makes, made against the real producer.

        `transcribe_stream` calls its callback plainly, exactly as
        `moment_scan.scan` does - so if the reporter could raise, a stream
        would die on its first chunk. Driven here with the real function
        over a fake downloader and a fake decoder: no network, no ffmpeg,
        no Whisper decode, and no starter (this is the producer and the
        reporter meeting, nothing else).
        """
        from yt_shorts.stream_transcribe import DownloadedAudio, transcribe_stream

        made = fake_starter(monkeypatch, "start_transcribe_job", "transcribe")
        queue.enqueue("transcribe", dict(HERE, video_id="vid-p"))
        worker.drain_once()
        report = made[0]["kwargs"]["progress"]

        def boom():
            raise OSError("no space left on device")

        monkeypatch.setattr(queue, "save", boom)

        def download(video_id, dest_dir):
            path = Path(dest_dir) / "audio.webm"
            path.write_bytes(b"fake audio")
            return DownloadedAudio(path=path, duration_seconds=1800.0)

        def decode(audio_path, start, length, glossary=None):
            return [{"start": 0.0, "end": 1.0, "text": f" s{int(start)}"}]

        transcript = transcribe_stream("V9nVNEQNdR4", tmp_path, downloader=download,
                                       decoder=decode, chunk_seconds=600,
                                       progress=report)

        assert [w["text"] for w in transcript.words] == [" s0", " s600", " s1200"]

    def test_a_progress_value_that_is_not_a_number_costs_one_reading(
            self, worker, queue, monkeypatch):
        # int() raises inside the wrapper, which is the same swallow: one
        # unreported unit, never the run.
        made = fake_starter(monkeypatch, "start_render_job", "render")
        entry = queue.enqueue("render", dict(HERE))
        worker.drain_once()

        made[0]["kwargs"]["progress"]("two", 6)

        assert entry_by_id(queue, entry.id).progress is None


class TestNoRealStarterRunsByAccident:
    """The structural guard itself: `tests/conftest.py`'s autouse
    `_no_real_job_starter`.

    This whole branch is about a component built to START WORK, and
    `drain_once()` is called at around forty sites across four test files.
    A reviewer demonstrated the hazard by execution: a bare
    `queue.enqueue("transcribe", …)` followed by `drain_once()`, with no
    stub anywhere, reached the REAL `studio.jobs.start_transcribe_job` - a
    real EventLock, a real Job, a real thread, and only that test's own
    sentinel stopped yt-dlp being invoked against YouTube. Until the fixture
    existed the only thing in the way was a comment and a habit.

    So the hazard is written down here as a test, in exactly the shape that
    used to start real work, and what it asserts is that the refusal fires
    instead."""

    def test_an_unstubbed_transcribe_entry_is_refused_not_started(
            self, worker, queue, store, _no_real_job_starter):
        """The reviewer's own reproduction. Note what it does NOT do: stub
        anything.

        `pytest.fail.Exception` is a **BaseException**, which is the point -
        `Worker._start` catches `Exception` broadly, so a refusal raised as
        an ordinary exception would have been turned into this entry's own
        failure reason and the pass would have carried on, leaving a green
        test that had in fact called the starter. Asserting the exception
        ESCAPES `drain_once()` is what pins that.
        """
        queue.enqueue("transcribe", dict(HERE, video_id="vid-unstubbed"))

        with pytest.raises(pytest.fail.Exception) as raised:
            worker.drain_once()

        message = str(raised.value)
        assert "a real job starter ran - stub it" in message
        assert "start_transcribe_job" in message
        assert _no_real_job_starter == ["start_transcribe_job"]
        # It really was refused rather than merely reported: no Job exists.
        # The ENTRY is left reading `running` - `claim_next` claims it
        # before the starter is ever called, and the refusal escaped the
        # pass before `mark_running` could record a job id - so `job_id` is
        # what says whether anything started, not the state.
        entry = entry_by_id(queue, queue.list()[0].id)
        assert entry.job_id is None
        assert store.any_running() is False
        # This test's refusal is the expected outcome, not a leak - clear it
        # so the fixture's own teardown check does not report it as one.
        _no_real_job_starter.clear()

    def test_the_patch_lands_on_the_name_the_worker_actually_calls(self):
        """The from-import hazard this repository has been bitten by twice.

        `worker._start_transcribe` resolves `jobs.start_transcribe_job`
        through the MODULE at call time (see worker.py's own note beside its
        imports, and `_isolated_resolved_workspace`, which patches three
        names for the same reason). A patch set anywhere else - on a name
        `worker.py` had already bound, say - would leave the real function
        running while every test looked protected. This asserts the module
        attribute the fixture patches IS what a worker start resolves.
        """
        for kind in worker_module.STARTERS:
            attribute = getattr(jobs_module, f"start_{kind}_job")
            assert attribute.__name__ == "starter", (
                f"start_{kind}_job is not the conftest stub, so the autouse "
                f"guard is not reaching the name the worker calls")

    def test_a_test_that_opts_in_gets_the_real_functions_back(
            self, real_job_starters):
        # The escape hatch is real, and it is the whole reason the fixture
        # can stay strict: a test that means to drive a starter says so.
        for name in real_job_starters:
            assert getattr(jobs_module, name).__name__ == name


class TestTheThread:
    def test_start_drains_until_stop(self, queue, store, monkeypatch):
        started = threading.Event()
        fake_starter(monkeypatch, "start_detect_job", "detect", started=started)
        worker = worker_module.Worker(queue, store, interval=0.01)
        entry = queue.enqueue("detect", dict(HERE, video_id="vid1"))
        worker.start()
        try:
            assert started.wait(WAIT_TIMEOUT), (
                "the worker thread never drained the queue")
            assert entry_by_id(queue, entry.id).state == "running"
        finally:
            worker.stop()
        assert worker.is_running() is False

    def test_a_failing_pass_does_not_kill_the_loop(self, queue, store, monkeypatch):
        worker = worker_module.Worker(queue, store, interval=0.01)
        passes = []
        third_pass = threading.Event()   # two passes SURVIVED the failing one
        real_drain = worker.drain_once

        def exploding_drain():
            passes.append(1)
            if len(passes) >= 3:
                third_pass.set()
            if len(passes) == 1:
                raise RuntimeError("simulated defect inside a pass")
            return real_drain()

        monkeypatch.setattr(worker, "drain_once", exploding_drain)
        worker.start()
        try:
            assert third_pass.wait(WAIT_TIMEOUT), (
                "the loop died on the first failing pass")
        finally:
            worker.stop()

    def test_stop_is_safe_without_a_start(self, worker):
        worker.stop()             # must not raise
        assert worker.is_running() is False

    def test_stop_ends_the_thread_it_started(self, queue, store):
        """`stop()` must STOP, not merely forget. A version that only
        dropped its reference to the thread left it draining the queue for
        the rest of the process - starting jobs after the studio had shut
        down, against a workspace the operator may already have switched
        away from - while `is_running()` reported False, so nothing said
        so. The thread object is captured here and asked directly."""
        before = {thread.ident for thread in threading.enumerate()}
        worker = worker_module.Worker(queue, store, interval=0.01)
        worker.start()
        thread = next(t for t in threading.enumerate()
                      if t.name == worker_module.THREAD_NAME
                      and t.ident not in before)
        assert worker.is_running() is True

        worker.stop()

        assert thread.is_alive() is False, (
            "stop() returned while the draining thread was still running")
        assert worker.is_running() is False

    def test_a_stop_that_times_out_does_not_claim_the_worker_stopped(
            self, queue, store, monkeypatch):
        """The one real way to reach a timed-out join is a pass wedged
        inside a starter for longer than the timeout. `is_running()` reads
        the thread, so dropping the reference there would answer False for
        a thread that is still draining - a claim nothing had checked. The
        stop flag is set either way, so the thread still ends the moment
        its pass returns."""
        worker = worker_module.Worker(queue, store, interval=0.01)
        entered, release = threading.Event(), threading.Event()

        def a_wedged_pass():
            entered.set()
            # WAIT_TIMEOUT, not a small budget: this gate is what holds the
            # pass wedged, and an early expiry ends the thread and fails the
            # `is_running()` assertion below for a timing reason.
            release.wait(timeout=WAIT_TIMEOUT)

        monkeypatch.setattr(worker, "drain_once", a_wedged_pass)
        worker.start()
        assert entered.wait(timeout=5.0), "the worker thread never ran a pass"

        worker.stop(timeout=0.05)

        assert worker.is_running() is True, (
            "the join timed out and the worker reported itself stopped anyway")
        release.set()
        worker.stop(timeout=5.0)
        assert worker.is_running() is False


class TestCreateAppWiring:
    """The riskiest line in the whole plan: over two thousand existing tests
    construct an app, and none of them may acquire an EventLock, spawn a
    thread or start work as a side effect."""

    @staticmethod
    def _workspace_root(tmp_path, monkeypatch):
        import yt_shorts.studio.api as api
        root = tmp_path / "ws"
        (root / "channels").mkdir(parents=True)
        fixed = workspace.Workspace(root=root, channels_dir=root / "channels",
                                    origin="test")
        monkeypatch.setattr(api, "_resolve_workspace", lambda: fixed)
        return root

    def test_the_worker_is_not_started_by_create_app_in_tests(
            self, studio_profile, tmp_path, monkeypatch):
        import yt_shorts.studio.api as api
        root = self._workspace_root(tmp_path, monkeypatch)
        # A plan already on disk BEFORE the app is built - so a worker
        # started by create_app would claim it on its very first pass, with
        # no waiting for a second one. Asserting only on `is_running()` would
        # be a weaker test: this fails on the WORK starting, whatever
        # mechanism started it.
        planted = JobQueue(root / "jobs.json", jobs_module.KINDS,
                           dict(worker_module.DEFAULT_LIMITS))
        entry = planted.enqueue("render", dict(HERE))

        app = api.create_app()

        worker = app.state.worker
        assert worker is not None, "create_app did not wire a worker at all"
        assert worker.is_running() is False
        assert not [t for t in threading.enumerate()
                    if t.name == worker_module.THREAD_NAME]
        time.sleep(0.3)
        assert entry_by_id(app.state.job_queue, entry.id).state == "queued"
        assert app.state.job_store.any_running() is False
        # And on disk too: nothing wrote a claim into the plan.
        reread = JobQueue(root / "jobs.json", jobs_module.KINDS,
                          dict(worker_module.DEFAULT_LIMITS))
        assert entry_by_id(reread, entry.id).state == "queued"

    def test_create_app_wires_the_queue_at_the_workspace_root(
            self, studio_profile, tmp_path, monkeypatch):
        import yt_shorts.studio.api as api
        root = self._workspace_root(tmp_path, monkeypatch)
        app = api.create_app()
        assert app.state.job_queue.path == root / "jobs.json"
        assert app.state.worker.queue is app.state.job_queue

    def test_a_workspace_switch_repoints_the_queue(
            self, studio_profile, tmp_path, monkeypatch):
        # Same hazard the central log had (see test_studio_api.py's
        # test_switch_repoints_the_central_log_to_the_new_workspace): the
        # queue is workspace data, so after a switch it must be the NEW
        # workspace's plan, not the old one's.
        from fastapi.testclient import TestClient

        import yt_shorts.studio.api as api
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / ".config")
        app = api.create_app()
        client = TestClient(app)
        parent = tmp_path / "parent"
        parent.mkdir()

        made = client.post("/api/workspaces/create",
                           json={"parent": str(parent), "name": "ws-new"})
        assert made.status_code == 200

        assert app.state.job_queue.path == parent / "ws-new" / "jobs.json"
        assert app.state.worker.queue is app.state.job_queue

    def test_a_workspace_switch_picks_up_the_new_workspaces_pool_limits(
            self, studio_profile, tmp_path, monkeypatch):
        """The limits are workspace SETTINGS, so a switch must read the NEW
        workspace's settings.json - not carry the old one's across, and not
        fall back to the shipped defaults either.

        `_build_queue_and_worker`'s own comment claims exactly this and
        nothing pinned it; the class beside this one already enumerates the
        repointing property for the queue's PATH
        (test_a_workspace_switch_repoints_the_queue) and was not extended
        when the limits were added to the same function - which is the
        recurring defect on this branch, one wiring point further on.
        """
        from fastapi.testclient import TestClient

        import yt_shorts.studio.api as api
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / ".config")

        # A workspace that already carries a limit an operator saved there.
        other = tmp_path / "other-ws"
        (other / "channels").mkdir(parents=True)
        workspace.write_settings(other, {"limits": {"cpu": 5, "net": 9}})

        app = api.create_app()
        client = TestClient(app)
        assert app.state.job_queue.limits() != {"cpu": 5, "net": 9}, (
            "the app started with the limits under test - nothing would be proved")

        switched = client.post("/api/workspaces/switch", json={"path": str(other)})
        assert switched.status_code == 200, switched.text

        assert app.state.job_queue.path == other / "jobs.json"
        assert app.state.job_queue.limits() == {"cpu": 5, "net": 9}
        # …and the live queue is the one the worker drains, not a second one
        # built beside it.
        assert app.state.worker.queue is app.state.job_queue
