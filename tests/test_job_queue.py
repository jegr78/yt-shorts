"""Tests for the pure job queue: a data structure with a file behind it.

job_queue.JobQueue spawns no thread, and only one test here starts one: the
proof that the queue's own lock actually excludes a second thread mid-
mutation (`TestItsOwnLock`), which cannot be shown from a single thread.
Every clock is injected (`now=`), so nothing sleeps and nothing else is
timing-dependent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from yt_shorts import job_queue
from yt_shorts.job_queue import JobQueue, QueueError
from yt_shorts.studio import jobs as studio_jobs


@dataclass(frozen=True)
class Spec:
    """A tiny stand-in for studio.jobs.KindSpec - duck-typed (`.pool`,
    `.queueable`), never imported from studio.jobs, so this module stays
    provably independent of it except in the one compatibility test below."""
    pool: str
    queueable: bool = True


KINDS = {
    "transcribe": Spec("cpu"),
    "render": Spec("cpu"),
    "trim": Spec("cpu"),
    "detect": Spec("net"),
    "upload": Spec("net"),
    "connect": Spec("net", queueable=False),
    "copy": Spec("io"),
}


class FakeClock:
    """Injected clock: advances only when told to, never sleeps."""

    def __init__(self, start: float = 1000.0):
        self._t = start

    def __call__(self) -> float:
        self._t += 1.0
        return self._t


def make_queue(tmp_path, limits, *, kinds=None, now=None):
    path = tmp_path / "jobs.json"
    return JobQueue(path, kinds or KINDS, limits, now=now or FakeClock())


class TestOrdering:
    def test_entries_run_in_the_order_they_were_added(self, tmp_path):
        queue = make_queue(tmp_path, {})
        e1 = queue.enqueue("detect", {})
        e2 = queue.enqueue("detect", {})
        e3 = queue.enqueue("detect", {})

        assert queue.claim_next().id == e1.id
        assert queue.claim_next().id == e2.id
        assert queue.claim_next().id == e3.id

    def test_move_reorders_a_queued_entry(self, tmp_path):
        queue = make_queue(tmp_path, {})
        e1 = queue.enqueue("detect", {})
        queue.enqueue("detect", {})
        e3 = queue.enqueue("detect", {})

        queue.move(e3.id, 0)

        assert queue.claim_next().id == e3.id
        assert queue.claim_next().id == e1.id

    def test_move_refuses_to_reorder_a_running_entry(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 5})
        e1 = queue.enqueue("transcribe", {})
        queue.claim_next()

        with pytest.raises(QueueError) as exc:
            queue.move(e1.id, 0)
        assert exc.value.kind == "invalid_state"


class TestPools:
    def test_one_cpu_job_at_a_time_by_default(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 1})
        queue.enqueue("transcribe", {})
        queue.enqueue("transcribe", {})

        first = queue.claim_next()
        assert first is not None
        assert queue.claim_next() is None  # pool full, second not claimable

    def test_a_net_job_is_claimable_while_a_cpu_job_runs(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 1, "net": 1})
        queue.enqueue("transcribe", {})
        d = queue.enqueue("detect", {})

        assert queue.claim_next() is not None  # claims the cpu job
        claimed = queue.claim_next()
        assert claimed is not None
        assert claimed.id == d.id

    def test_the_limits_are_configurable(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 2})
        queue.enqueue("transcribe", {})
        queue.enqueue("transcribe", {})
        queue.enqueue("transcribe", {})

        assert queue.claim_next() is not None
        assert queue.claim_next() is not None
        assert queue.claim_next() is None  # third exceeds the configured limit of 2

    def test_the_limits_can_be_changed_on_a_live_queue(self, tmp_path):
        # The studio's Settings screen edits the pool limits (Task 6's
        # PUT /api/settings/limits), and a change that only reached the
        # workspace's settings file would do nothing until the next restart.
        queue = make_queue(tmp_path, {"cpu": 1})
        queue.enqueue("transcribe", {})
        queue.enqueue("transcribe", {})
        assert queue.claim_next() is not None
        assert queue.claim_next() is None

        assert queue.set_limits({"cpu": 2}) == {"cpu": 2}

        assert queue.limits() == {"cpu": 2}
        assert queue.claim_next() is not None, "the new limit did not take effect"

    def test_the_limits_are_a_copy_in_both_directions(self, tmp_path):
        # Neither the caller's dict nor the queue's own may be reachable
        # through the other: a mutation elsewhere must not silently re-limit
        # a live pool.
        given = {"cpu": 2}
        queue = make_queue(tmp_path, {"cpu": 1})
        queue.set_limits(given)
        given["cpu"] = 99
        assert queue.limits() == {"cpu": 2}
        queue.limits()["cpu"] = 99
        assert queue.limits() == {"cpu": 2}

    def test_a_stopping_entry_still_holds_its_pool_slot(self, tmp_path):
        # A "stopping" entry counts against its pool's limit exactly like
        # "running" does, so a pool cannot be oversubscribed while a job is
        # still shutting down: the thread has not released the CPU yet, and
        # a newcomer would fight it for the same resource.
        #
        # This used to fabricate the state by assigning `claimed.state`
        # directly, because nothing on JobQueue could produce it. That is no
        # longer true - `mark_stopping` is the transition (the worker calls
        # it once it has actually reached the running job's cancel token), so
        # the state is reached here through the public API like every other
        # one in this file.
        queue = make_queue(tmp_path, {"cpu": 1})
        e1 = queue.enqueue("transcribe", {})
        claimed = queue.claim_next()
        assert claimed.id == e1.id

        stopping = queue.mark_stopping(e1.id)
        assert stopping.state == "stopping"

        queue.enqueue("transcribe", {})
        assert queue.claim_next() is None  # pool still full: stopping holds its slot

    def test_mark_stopping_refuses_an_entry_that_is_not_running(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 1})
        e1 = queue.enqueue("transcribe", {})
        with pytest.raises(QueueError) as exc:
            queue.mark_stopping(e1.id)   # still queued: nothing is running to stop
        assert exc.value.kind == "invalid_state"
        assert queue.list()[0].state == "queued"

    def test_io_runs_outside_both_pools(self, tmp_path):
        # cpu/net are saturated; limits carries no "io" key at all, so copy
        # jobs are unlimited - that absence IS how io escapes both pools.
        queue = make_queue(tmp_path, {"cpu": 0, "net": 0})
        ids = [queue.enqueue("copy", {}).id for _ in range(5)]

        claimed_ids = {queue.claim_next().id for _ in range(5)}
        assert claimed_ids == set(ids)


class TestDependency:
    def test_an_entry_with_an_unfinished_after_is_not_claimable(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 5, "net": 5})
        t1 = queue.enqueue("transcribe", {})
        queue.claim_next()  # t1 now running, not done
        queue.enqueue("detect", {}, after=t1.id)

        assert queue.claim_next() is None

    def test_it_becomes_claimable_once_its_dependency_is_done(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 5, "net": 5})
        t1 = queue.enqueue("transcribe", {})
        queue.claim_next()
        d1 = queue.enqueue("detect", {}, after=t1.id)

        queue.mark_finished(t1.id, "done")
        claimed = queue.claim_next()
        assert claimed is not None
        assert claimed.id == d1.id

    def test_a_failed_dependency_fails_the_dependent_rather_than_stranding_it(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 5, "net": 5})
        t1 = queue.enqueue("transcribe", {})
        queue.claim_next()
        d1 = queue.enqueue("detect", {}, after=t1.id)

        queue.mark_finished(t1.id, "failed", reason="ffmpeg exploded")
        assert queue.claim_next() is None  # nothing else claimable

        by_id = {e.id: e for e in queue.list()}
        assert by_id[d1.id].state == "failed"
        assert by_id[d1.id].reason is not None
        assert t1.id in by_id[d1.id].reason


class TestHeadOfLineBlocking:
    def test_an_unclaimable_entry_does_not_block_the_one_behind_it(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 1, "net": 5})
        queue.enqueue("transcribe", {})
        queue.claim_next()  # takes the one cpu slot

        queue.enqueue("transcribe", {})  # queued but unclaimable: pool full
        d1 = queue.enqueue("detect", {})  # behind it, but a different pool

        claimed = queue.claim_next()
        assert claimed is not None
        assert claimed.id == d1.id

    def test_a_skipped_entry_does_not_block_the_one_behind_it(self, tmp_path):
        # `skip` is the worker's own pass: an entry whose EVENT LOCK is held
        # is deferred back to queued and would be the very next thing
        # claim_next returns, forever, while an entry for a different event
        # sat behind it. The queue knows nothing about locks - it only has
        # to be able to pass over an id.
        queue = make_queue(tmp_path, {"cpu": 5})
        blocked = queue.enqueue("transcribe", {})
        behind = queue.enqueue("transcribe", {})

        claimed = queue.claim_next(skip={blocked.id})

        assert claimed is not None
        assert claimed.id == behind.id
        assert queue.list()[0].state == "queued"  # the skipped one is untouched

    def test_a_candidate_the_caller_calls_blocked_is_not_claimed_and_says_why(
            self, tmp_path):
        # `blocked_by` answers for a condition this module deliberately
        # knows nothing about (the event lock). A blocked candidate stays
        # QUEUED with the reason recorded - never claimed, never failed,
        # never moved.
        queue = make_queue(tmp_path, {"cpu": 5})
        first = queue.enqueue("transcribe", {})
        second = queue.enqueue("transcribe", {})

        claimed = queue.claim_next(
            blocked_by=lambda e: "the event lock is held" if e.id == first.id else None)

        assert claimed is not None and claimed.id == second.id, (
            "a blocked candidate blocked the entry behind it")
        waiting = queue.list()[0]
        assert waiting.state == "queued"
        assert waiting.reason == "the event lock is held"
        assert [e.id for e in queue.list()] == [first.id, second.id]  # place kept

    def test_a_reason_that_has_not_changed_is_not_written_again(self, tmp_path):
        # The point of asking BEFORE claiming: a lock held for hours must
        # not rewrite jobs.json on every pass. Nothing has changed, so
        # nothing is written - and the entry is never `running` on disk in
        # between, which a reader of the file would otherwise see.
        queue = make_queue(tmp_path, {"cpu": 5})
        queue.enqueue("transcribe", {})

        def blocked_by(_entry):
            return "the event lock is held"

        assert queue.claim_next(blocked_by=blocked_by) is None
        writes = []
        real_save = queue.save

        def counting_save():
            writes.append(1)
            real_save()

        queue.save = counting_save

        for _ in range(3):
            assert queue.claim_next(blocked_by=blocked_by) is None

        assert writes == [], f"{len(writes)} pointless rewrite(s) of the plan"
        assert queue.list()[0].reason == "the event lock is held"


class TestPersistence:
    def test_a_queue_round_trips_through_the_file(self, tmp_path):
        path = tmp_path / "jobs.json"
        clock = FakeClock()
        queue1 = JobQueue(path, KINDS, {"cpu": 5, "net": 5}, now=clock)
        t = queue1.enqueue("transcribe", {"clip": "a"})
        d = queue1.enqueue("detect", {"video_id": "xyz"}, after=None)
        queue1.claim_next()  # claims the transcribe entry
        queue1.claim_next()  # claims the detect entry
        queue1.mark_running(t.id, job_id="job-abc")
        queue1.mark_finished(d.id, "failed", reason="boom")

        queue2 = JobQueue(path, KINDS, {"cpu": 5, "net": 5}, now=clock)
        entries1 = {e.id: e for e in queue1.list()}
        entries2 = {e.id: e for e in queue2.list()}
        assert entries1.keys() == entries2.keys()
        for entry_id, e1 in entries1.items():
            e2 = entries2[entry_id]
            assert e1.kind == e2.kind
            assert e1.params == e2.params
            assert e1.state == e2.state
            assert e1.reason == e2.reason
            assert e1.progress == e2.progress
            assert e1.created_at == e2.created_at
            assert e1.after == e2.after
            assert e1.job_id == e2.job_id

    def test_the_file_is_written_aside_and_replaced(self, tmp_path, monkeypatch):
        path = tmp_path / "jobs.json"
        queue = JobQueue(path, KINDS, {}, now=FakeClock())
        queue.enqueue("detect", {"n": 1})
        good_content = path.read_text(encoding="utf-8")

        def boom(_src, _dst):
            raise OSError("simulated crash between write and replace")

        monkeypatch.setattr(job_queue.os, "replace", boom)
        with pytest.raises(OSError):
            queue.enqueue("detect", {"n": 2})

        # The target must still hold the last GOOD, complete state - a crash
        # between writing the scratch file and replacing the target must
        # never leave a half-written or missing jobs.json.
        assert path.read_text(encoding="utf-8") == good_content
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 1

    def test_an_unparseable_file_is_renamed_aside_and_reported(self, tmp_path):
        path = tmp_path / "jobs.json"
        path.write_text("{ not valid json at all", encoding="utf-8")
        clock = FakeClock(2000.0)

        queue = JobQueue(path, KINDS, {}, now=clock)

        assert queue.list() == []
        assert queue.load_error is not None
        assert not path.exists()  # the bad file must not sit at the real path

        siblings = list(tmp_path.glob("jobs.json.corrupt-*"))
        assert len(siblings) == 1
        assert siblings[0].read_text(encoding="utf-8") == "{ not valid json at all"
        assert siblings[0].name in queue.load_error

    def test_no_parameter_that_looks_like_a_key_is_ever_written(self, tmp_path):
        path = tmp_path / "jobs.json"
        queue = JobQueue(path, KINDS, {}, now=FakeClock())
        queue.enqueue("detect", {"video_id": "abc"})  # gives the file real content

        marker = "sk-ant-DO-NOT-PERSIST-THIS-MARKER-4f8c9"
        for bad_key in ("api_key", "Token", "SECRET", "password", "credential_blob"):
            with pytest.raises(QueueError) as exc:
                queue.enqueue("detect", {bad_key: marker})
            assert exc.value.kind == "secret_in_params"

        assert path.exists()
        raw_bytes = path.read_bytes()
        assert marker.encode("utf-8") not in raw_bytes

    def test_a_params_value_that_carries_names_of_its_own_is_refused(self, tmp_path):
        # The check above tests a NAME and only ever sees the top level, so
        # a nested dict smuggled a key straight past it and into jobs.json -
        # driven against the real HTTP route by a review, not theorised.
        # Refusing the nesting is what closes it, at any depth and inside a
        # list too.
        path = tmp_path / "jobs.json"
        queue = JobQueue(path, KINDS, {}, now=FakeClock())
        queue.enqueue("detect", {"video_id": "abc"})  # gives the file real content

        marker = "sk-ant-NESTED-DO-NOT-PERSIST-2b6e1"
        for params in (
            {"creds": {"api_key": marker}},
            {"outer": {"inner": {"token": marker}}},
            {"blobs": [{"password": marker}]},
            {"blobs": [[{"secret": marker}]]},
        ):
            with pytest.raises(QueueError) as exc:
                queue.enqueue("detect", params)
            assert exc.value.kind == "nested_params"

        assert marker.encode("utf-8") not in path.read_bytes()
        assert len(queue.list()) == 1, "a refused entry was still added"

    def test_a_scalar_or_a_list_of_scalars_is_still_accepted(self, tmp_path):
        # The refusal must not be "no non-scalar values": `render` ships with
        # `clips: [str]` (see studio/worker.py's table), so a rule that
        # refused every list would break the one shipped kind that needs one.
        queue = make_queue(tmp_path, {})
        entry = queue.enqueue("detect", {"video_id": "abc", "clips": ["a", "b"],
                                         "force": True, "n": 3, "ratio": 0.5,
                                         "nothing": None})
        assert entry.params["clips"] == ["a", "b"]

    def test_only_the_last_50_finished_entries_are_kept(self, tmp_path):
        queue = make_queue(tmp_path, {})
        ids = []
        for i in range(55):
            e = queue.enqueue("detect", {"n": i})
            ids.append(e.id)
            claimed = queue.claim_next()
            queue.mark_finished(claimed.id, "done")

        entries = queue.list()
        assert len(entries) == 50
        kept_ids = {e.id for e in entries}
        assert kept_ids == set(ids[-50:])
        assert ids[0] not in kept_ids


class TestRecovery:
    def test_an_entry_left_running_becomes_interrupted(self, tmp_path):
        path = tmp_path / "jobs.json"
        clock = FakeClock()
        queue1 = JobQueue(path, KINDS, {"cpu": 1}, now=clock)
        e = queue1.enqueue("transcribe", {})
        queue1.claim_next()

        queue2 = JobQueue(path, KINDS, {"cpu": 1}, now=clock)
        recovered = queue2.recover()

        assert [r.id for r in recovered] == [e.id]
        assert queue2.list()[0].state == "interrupted"

    def test_an_interrupted_entry_never_starts_by_itself(self, tmp_path):
        path = tmp_path / "jobs.json"
        clock = FakeClock()
        queue1 = JobQueue(path, KINDS, {"cpu": 1}, now=clock)
        e = queue1.enqueue("transcribe", {})
        queue1.claim_next()

        queue2 = JobQueue(path, KINDS, {"cpu": 1}, now=clock)
        queue2.recover()

        assert queue2.claim_next() is None  # not claimable on its own

        queue2.retry(e.id)
        assert queue2.list()[0].state == "queued"
        claimed = queue2.claim_next()
        assert claimed is not None
        assert claimed.id == e.id


class TestTransitions:
    def test_pause_keeps_its_place_and_resume_restores_it(self, tmp_path):
        queue = make_queue(tmp_path, {})
        e1 = queue.enqueue("detect", {})
        e2 = queue.enqueue("detect", {})

        queue.pause(e1.id)
        assert [e.id for e in queue.list()] == [e1.id, e2.id]  # place unchanged

        claimed = queue.claim_next()
        assert claimed.id == e2.id  # e1 is paused, skipped

        queue.resume(e1.id)
        assert [e.id for e in queue.list()] == [e1.id, e2.id]  # still in place
        claimed = queue.claim_next()
        assert claimed.id == e1.id  # claimable again

    def test_remove_refuses_a_running_entry(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 5})
        e1 = queue.enqueue("transcribe", {})
        queue.claim_next()

        with pytest.raises(QueueError) as exc:
            queue.remove(e1.id)
        assert exc.value.kind == "invalid_state"
        assert any(e.id == e1.id for e in queue.list())

    @pytest.mark.parametrize("ended", ["failed", "interrupted", "stopped"])
    def test_retry_re_enqueues_every_terminal_state_but_done(self, tmp_path, ended):
        # `stopped` is here on the operator's own decision, and it makes a
        # promise the studio was already printing come true: the stop dialog
        # says "a retry resumes at the first window nobody reached" (detect)
        # and "from the first missing chunk" (transcribe), and this route
        # used to answer 409 for exactly the state that sentence is about.
        queue = make_queue(tmp_path, {"cpu": 5})
        e1 = queue.enqueue("transcribe", {})
        queue.claim_next()
        queue.mark_finished(e1.id, ended, reason="boom")

        queue.retry(e1.id)
        assert queue.list()[0].state == "queued"
        assert queue.list()[0].reason is None
        claimed = queue.claim_next()
        assert claimed.id == e1.id

    def test_retry_refuses_a_done_entry(self, tmp_path):
        # The one terminal state with no retry: re-running work that
        # succeeded is a new request, not a recovery, and for a paid kind it
        # would spend the money again on a click that means "try again".
        queue = make_queue(tmp_path, {"cpu": 5})
        e1 = queue.enqueue("detect", {})
        queue.claim_next()
        queue.mark_finished(e1.id, "done")

        with pytest.raises(QueueError) as exc:
            queue.retry(e1.id)
        assert exc.value.kind == "invalid_state"
        assert queue.list()[0].state == "done"

    def test_defer_puts_a_claimed_entry_back_queued_with_a_reason(self, tmp_path):
        # The case this exists for is an event lock held by a CLI render:
        # normal, temporary, nobody's mistake. The entry must not be failed,
        # must keep its place, and must say what it is waiting for.
        queue = make_queue(tmp_path, {"cpu": 1})
        first = queue.enqueue("transcribe", {})
        second = queue.enqueue("transcribe", {})
        queue.claim_next()
        # A job_id is set here on purpose. Both tests of `defer` used to
        # reach it on a path where the entry had never had one, so the
        # `job_id is None` assertion below was true whether or not `defer`
        # cleared it - and a queued entry still naming a job would put a
        # link to a finished job's log on a row where nothing is running.
        queue.mark_running(first.id, job_id="job-7")

        deferred = queue.defer(first.id, reason="the event is locked by pid 42")

        assert deferred.state == "queued"
        assert "locked" in deferred.reason
        assert deferred.job_id is None
        assert [e.id for e in queue.list()] == [first.id, second.id]  # place kept
        # The pool slot went back with it: the deferred entry is not running.
        claimed = queue.claim_next()
        assert claimed is not None and claimed.id == first.id

    def test_a_claim_clears_a_stale_deferral_reason(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 1})
        entry = queue.enqueue("transcribe", {})
        queue.claim_next()
        queue.defer(entry.id, reason="the event is locked by pid 42")

        claimed = queue.claim_next()

        assert claimed.reason is None, (
            "a running entry still showed why it was previously NOT running")

    def test_defer_refuses_an_entry_that_was_never_claimed(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 1})
        entry = queue.enqueue("transcribe", {})
        with pytest.raises(QueueError) as exc:
            queue.defer(entry.id, reason="nope")
        assert exc.value.kind == "invalid_state"

    def test_mark_running_records_which_job_is_doing_the_work(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 1})
        entry = queue.enqueue("transcribe", {})
        queue.claim_next()

        updated = queue.mark_running(entry.id, job_id="job-123")

        assert updated.job_id == "job-123"

    def test_an_unknown_kind_is_refused(self, tmp_path):
        queue = make_queue(tmp_path, {})
        with pytest.raises(QueueError) as exc:
            queue.enqueue("levitate", {})
        assert exc.value.kind == "unknown_kind"

    def test_a_non_queueable_kind_is_refused(self, tmp_path):
        queue = make_queue(tmp_path, {})
        with pytest.raises(QueueError) as exc:
            queue.enqueue("connect", {})
        assert exc.value.kind == "not_queueable"


class TestProgressBelongsToARunningEntry:
    """A reading describes work in flight, and nothing else.

    The exits are ENUMERATED rather than spot-checked: `mark_finished` is
    parameterised over `job_queue._TERMINAL_STATES` itself, so a terminal
    state added to that frozenset without clearing the reading fails here
    instead of shipping a `done` row that still says "chunk 20 of 50". The
    other two ways out of `running` (`defer` back to `queued`, `recover`
    into `interrupted`) and `retry` get one test each - four exits, four
    `_clear_progress` call sites.
    """

    READING = {"unit": "chunk", "done": 20, "total": 50}

    def _running(self, tmp_path):
        queue = make_queue(tmp_path, {"cpu": 5})
        entry = queue.enqueue("transcribe", {})
        queue.claim_next()
        queue.mark_running(entry.id, progress=dict(self.READING))
        assert queue.list()[0].progress == self.READING
        return queue, entry

    def test_a_reading_is_recorded_while_the_entry_runs(self, tmp_path):
        queue, entry = self._running(tmp_path)
        stored = queue.mark_running(entry.id, progress={"unit": "chunk",
                                                        "done": 21, "total": 50})
        assert stored.progress == {"unit": "chunk", "done": 21, "total": 50}

    def test_a_reading_survives_the_file(self, tmp_path):
        # It is read back after a restart, which is exactly why leaving one
        # behind on a finished row would outlive the process that wrote it.
        path = tmp_path / "jobs.json"
        clock = FakeClock()
        queue = JobQueue(path, KINDS, {"cpu": 5}, now=clock)
        entry = queue.enqueue("transcribe", {})
        queue.claim_next()
        queue.mark_running(entry.id, progress=dict(self.READING))

        reread = JobQueue(path, KINDS, {"cpu": 5}, now=clock)
        assert reread.list()[0].progress == self.READING

    @pytest.mark.parametrize("state", sorted(job_queue._TERMINAL_STATES))
    def test_every_terminal_state_clears_the_reading(self, tmp_path, state):
        queue, entry = self._running(tmp_path)
        finished = queue.mark_finished(entry.id, state, reason="whatever")
        assert finished.state == state
        assert finished.progress is None, (
            f"a {state} entry still claims to be {self.READING['done']} of "
            f"{self.READING['total']} chunks into work that is over")

    def test_a_deferred_entry_keeps_no_reading(self, tmp_path):
        # `defer` puts the entry back as `queued` because its event was
        # locked - nothing ran, so a reading from a PREVIOUS claim would
        # describe a run that is not happening.
        queue, entry = self._running(tmp_path)
        deferred = queue.defer(entry.id, reason="the event is locked")
        assert deferred.state == "queued"
        assert deferred.progress is None

    def test_recovery_clears_the_reading_of_a_dead_process(self, tmp_path):
        path = tmp_path / "jobs.json"
        clock = FakeClock()
        queue1 = JobQueue(path, KINDS, {"cpu": 1}, now=clock)
        entry = queue1.enqueue("transcribe", {})
        queue1.claim_next()
        queue1.mark_running(entry.id, progress=dict(self.READING))

        queue2 = JobQueue(path, KINDS, {"cpu": 1}, now=clock)
        queue2.recover()

        recovered = queue2.list()[0]
        assert recovered.state == "interrupted"
        assert recovered.progress is None, (
            "an interrupted entry showed a live-looking reading for work no "
            "thread is doing any more")

    def test_a_retried_entry_starts_with_no_reading(self, tmp_path):
        queue, entry = self._running(tmp_path)
        finished = queue.mark_finished(entry.id, "stopped")
        # mark_finished already clears the reading, which would make this
        # test pass even with retry's own _clear_progress call deleted - so
        # set a reading directly on the terminal entry (the same shape a
        # hand-edited jobs.json could carry) to actually exercise retry's
        # own clear rather than relying on mark_finished having already done
        # the work.
        finished.progress = dict(self.READING)
        queue.retry(entry.id)
        assert queue.list()[0].progress is None

    def test_a_late_reading_cannot_resurrect_one_on_a_finished_entry(self, tmp_path):
        # The other half of the rule, and the one that happens for real: the
        # work carries on to its own safe point after a stop, so a callback
        # can arrive after the entry has moved on. mark_running refuses it.
        queue, entry = self._running(tmp_path)
        queue.mark_finished(entry.id, "done")

        with pytest.raises(QueueError) as exc:
            queue.mark_running(entry.id, progress={"unit": "chunk",
                                                   "done": 50, "total": 50})

        assert exc.value.kind == "invalid_state"
        assert queue.list()[0].progress is None

    def test_a_reading_is_refused_while_the_entry_is_stopping(self, tmp_path):
        # Not a defect: the entry has left `running`, and the studio's own
        # reporter swallows this (see worker._progress_reporter). Pinned so
        # that a future widening of mark_running is a deliberate decision
        # rather than an accident.
        queue, entry = self._running(tmp_path)
        queue.mark_stopping(entry.id)

        with pytest.raises(QueueError) as exc:
            queue.mark_running(entry.id, progress={"unit": "chunk",
                                                   "done": 21, "total": 50})
        assert exc.value.kind == "invalid_state"
        # The last reading is kept, though: the work IS still running, and
        # 20 of 50 is the truest thing anyone knows about it.
        assert queue.list()[0].progress == self.READING


class TestItsOwnLock:
    """The queue is mutated from the worker's thread AND (from the routes
    task onwards) from request threads. That used to be guarded by a
    COMMENT on `studio.worker` saying routes should hold `Worker.lock` too:
    a convention a route author had to remember, whose failure mode is a
    lost update to the operator's own plan, and which no test anywhere
    could catch. It is a property of this class now.
    """

    def test_every_public_method_runs_under_the_lock(self):
        # Enumerated, not spot-checked: the point is that a mutator added
        # later cannot quietly miss it.
        unguarded = [
            name for name in dir(JobQueue)
            if not name.startswith("_") and callable(getattr(JobQueue, name))
            and getattr(getattr(JobQueue, name), "_synchronised", False) is not True
        ]
        assert not unguarded, (
            f"public JobQueue method(s) not wrapped in the queue's own lock: "
            f"{unguarded}")

    def test_a_mutation_excludes_another_thread_for_its_whole_duration(self, tmp_path):
        # The one place this file starts a thread (see its docstring). The
        # injected clock is called from INSIDE enqueue, so the second
        # thread tries to take the lock exactly mid-mutation.
        import threading

        outcomes = []

        def clock_that_lets_another_thread_try():
            def another_thread():
                taken = queue._lock.acquire(timeout=0.05)
                outcomes.append(taken)
                if taken:
                    queue._lock.release()

            thread = threading.Thread(target=another_thread)
            thread.start()
            thread.join()
            return 1000.0

        queue = JobQueue(tmp_path / "jobs.json", KINDS, {},
                         now=clock_that_lets_another_thread_try)
        queue.enqueue("detect", {})

        assert outcomes == [False], (
            "another thread got into the plan while a mutation was half done")


class TestRealKindsCompatibility:
    """job_queue.py must not import studio.jobs, but the real KINDS table it
    passes in must still work here unmodified - this is the one test that
    imports studio.jobs, to pin that the two stay compatible."""

    def test_the_real_jobs_kinds_table_works_with_the_queue(self, tmp_path):
        path = tmp_path / "jobs.json"
        queue = JobQueue(path, studio_jobs.KINDS, {"cpu": 1, "net": 1},
                         now=FakeClock())

        with pytest.raises(QueueError) as exc:
            queue.enqueue("connect", {})  # real KINDS marks connect non-queueable
        assert exc.value.kind == "not_queueable"

        render_entry = queue.enqueue("render", {"clip": "a"})
        detect_entry = queue.enqueue("detect", {"video_id": "xyz"})

        claimed_render = queue.claim_next()
        assert claimed_render.id == render_entry.id  # cpu pool
        claimed_detect = queue.claim_next()
        assert claimed_detect.id == detect_entry.id  # net pool, not blocked by cpu


class TestAKindThisBuildDoesNotKnow:
    """`jobs.json` is a plain file, and `load` accepts any `kind` string.

    A hand edit, or a downgrade after a later version added a kind, puts an
    entry in the plan whose kind this build has no table row for - and the
    claim path used to index `_kinds` with it. The `KeyError` was caught by
    the worker's loop, logged, and retried every second, forever: the thread
    survived, no job of ANY kind for ANY event ever started again, `GET
    /api/jobs` answered 200, `worker_running` reported true, and the screen
    told the operator their entry was next in line. One bad entry has to cost
    one entry, which is what `drain_once`'s own docstring already promised.
    """

    @staticmethod
    def _plant(path, entries):
        path.write_text(json.dumps({"entries": entries}), encoding="utf-8")

    def _entry(self, entry_id, kind, state="queued"):
        return {"id": entry_id, "kind": kind, "params": {}, "state": state,
                "reason": None, "progress": None, "created_at": 1.0,
                "after": None, "job_id": None}

    def test_an_unknown_kind_fails_that_entry_and_names_the_kind(self, tmp_path):
        path = tmp_path / "jobs.json"
        self._plant(path, [self._entry("planted", "teleport")])
        queue = JobQueue(path, KINDS, {"cpu": 1}, now=FakeClock())

        assert queue.claim_next() is None       # nothing to start, no KeyError

        entry = queue.list()[0]
        assert entry.state == "failed"
        assert "teleport" in entry.reason
        # And on disk, so a restart does not re-live the same pass.
        reread = JobQueue(path, KINDS, {"cpu": 1}, now=FakeClock())
        assert reread.list()[0].state == "failed"

    def test_the_rest_of_the_plan_still_runs(self, tmp_path):
        # The whole point: the unknown entry is in FRONT, so a claim path
        # that raises never reaches the good one behind it.
        path = tmp_path / "jobs.json"
        self._plant(path, [self._entry("planted", "teleport"),
                           self._entry("good", "render")])
        queue = JobQueue(path, KINDS, {"cpu": 1}, now=FakeClock())

        claimed = queue.claim_next()

        assert claimed is not None and claimed.id == "good"
        assert claimed.state == "running"

    def test_an_active_unknown_kind_does_not_break_pool_accounting(self, tmp_path):
        # The second indexing site, and the quieter one: `_pool_has_room`
        # walks every ACTIVE entry to count a pool's occupants, so an
        # unknown kind left `running` by a previous version raised from a
        # function that is only being asked whether there is room.
        path = tmp_path / "jobs.json"
        self._plant(path, [self._entry("planted", "teleport", state="running"),
                           self._entry("good", "render")])
        queue = JobQueue(path, KINDS, {"cpu": 1}, now=FakeClock())

        claimed = queue.claim_next()

        assert claimed is not None and claimed.id == "good", (
            "an unknown ACTIVE kind was counted against the cpu pool, or raised")

    def test_a_dependency_on_an_unknown_kind_fails_rather_than_waits(self, tmp_path):
        # The failed unknown entry is a dependency that can never finish, so
        # whatever waited on it must be failed too rather than left looking
        # pending forever - the rule claim_next already applies to any
        # dependency that ended without succeeding.
        path = tmp_path / "jobs.json"
        waiting = self._entry("waiting", "render")
        waiting["after"] = "planted"
        self._plant(path, [self._entry("planted", "teleport"), waiting])
        queue = JobQueue(path, KINDS, {"cpu": 1}, now=FakeClock())

        queue.claim_next()      # fails the unknown one
        queue.claim_next()      # …and then the dependent

        states = {e.id: e.state for e in queue.list()}
        assert states == {"planted": "failed", "waiting": "failed"}
