"""Tests for yt_shorts.studio.jobs and the /api/render, /api/jobs/{id}
routes in yt_shorts.studio.api.

No real render runs in most of these tests: render.build_short is stubbed
(monkeypatched at yt_shorts.studio.jobs.render.build_short, the name jobs.py
itself calls it through) so they exercise the job/lock machinery only - no
network, no ffmpeg, no yt-dlp. The one exception is
TestStudioRenderProducesCaptions at the bottom of this file, which
deliberately does NOT stub build_short: a stubbed build_short can never
prove that a studio-initiated render actually burns in captions - only that
jobs.py called something - so that class runs the real render pipeline
(yt-dlp alone is stubbed, to hand back a local synthetic clip instead of
downloading) and measures pixels in the caption band of the result, the
same way tests/test_render_subtitles.py does.

Since a render job runs in a background thread, tests that need to observe
"running" (the 409-while-a-job-is-running case) hold the stub open with a
threading.Event until the test is done inspecting that state, and tests
that need the finished result wait on `job.finished` (see `_wait_for`)
rather than assuming any particular scheduling order.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yt_shorts import clipstore, editorial
from yt_shorts.cancel import CancelToken, Stopped
from yt_shorts import profile as profile_module
from yt_shorts.lock import EventLock, LockError
from yt_shorts.profile import load as profile_load
from yt_shorts.stream_transcribe import StreamTranscript
from yt_shorts.studio import jobs as jobs_module
from yt_shorts.studio.api import create_app

# The `start_*_job` functions ARE this module's subject, so every test here
# opts out of tests/conftest.py's autouse `_no_real_job_starter` guard - the
# guard exists to stop a test that never meant to start a job from starting
# one, and this file means to. What it does NOT opt out of is the rule the
# guard defends: no network, no ffmpeg, no Whisper decode, no key and no
# money - the expensive thing INSIDE each starter is stubbed here (see the
# module docstring above), one level below where the guard sits.
pytestmark = pytest.mark.usefixtures("real_job_starters")

CLIP_URL = "https://www.youtube.com/clip/UgkxSpeedy123"

FIXTURE_CHANNELS = Path(__file__).parent / "fixtures" / "channels"

# A deadlock backstop for `_wait_for`/`_wait_for_job`, not a performance
# guess: the work behind every wait here is stubbed, so reaching this means a
# job thread never ran its `finally` at all.
WAIT_TIMEOUT = 30.0

CHANNEL = "erf"
EVENT = "studio-test"
RENDER_URL = f"/api/channels/{CHANNEL}/events/{EVENT}/render"
EV = f"/api/channels/{CHANNEL}/events/{EVENT}"


def clip_entry(url, hook, duration=60.0, error=None):
    return {"url": url, "hook": hook, "source_title": "ERF Round 3",
            "start": 10.0, "end": 10.0 + duration, "duration": duration,
            "error": error}


@pytest.fixture
def studio_profile(tmp_path, monkeypatch):
    # Same technique as tests/test_studio_api.py: create_app() is
    # workspace-level now (no bound profile), so /api/render resolves a
    # real profile.load("erf/studio-test") from a copy of the checked-in
    # erf fixture channel repointed under a tmp CHANNELS_DIR.
    channels = tmp_path / "channels"
    shutil.copytree(FIXTURE_CHANNELS / "erf", channels / "erf")
    monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels)
    (channels / "erf" / "events" / "studio-test").mkdir(parents=True)
    return profile_load("erf/studio-test")


@pytest.fixture
def event_dir(studio_profile):
    return studio_profile.event_dir


@pytest.fixture
def client(studio_profile):
    return TestClient(create_app())


def install_stub(monkeypatch, *, fail_for=frozenset(), gate: threading.Event | None = None):
    """Replaces render.build_short as jobs.py calls it. Writes a fake
    short.mp4 on success, raises for any clip directory named in
    `fail_for`, and (if `gate` is given) blocks until the test sets it -
    letting a test observe a job mid-flight."""
    calls: list[str] = []

    def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
        name = Path(work_dir).name
        calls.append(name)
        if gate is not None:
            gate.wait(timeout=5.0)
        if name in fail_for:
            raise RuntimeError(f"stubbed failure for {name}")
        Path(target).write_bytes(b"stub short")
        return target

    monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
    return calls


def _wait_for(job, timeout=WAIT_TIMEOUT):
    """Waits on `job.finished` - the event `jobs._finishing` sets AROUND the
    runner, so it is set strictly after that runner's whole `finally`. It
    therefore already means "the event lock is released" and "the job log is
    closed", which is why this no longer takes an `unlocks` argument and no
    longer polls: `job.status` alone goes terminal inside the runner's `try`
    (CLAUDE.md, "A TERMINAL JOB STATUS IS NOT A RELEASED LOCK"), `finished`
    does not. Anything asking about the EVENT rather than about one job still
    asks `EventLock.is_held()`.

    The timeout is a deadlock backstop, not a performance budget: reaching it
    means a thread is genuinely wedged, never that the machine was slow.
    """
    assert job.finished.wait(timeout), (
        f"job {job.id} ({job.kind}) never signalled finished within {timeout}s "
        f"(status {job.status}) - its thread is wedged, not merely slow")
    return job


def _wait_for_job(client, job_id, timeout=WAIT_TIMEOUT) -> dict:
    """Waits on the job's own `finished` event (reached through the app's
    JobStore, the same store the route reads), then returns the HTTP body -
    so the assertions below still read what a client would see.

    The job object is what carries the signal; the id alone cannot.
    """
    job = client.app.state.job_store.get(job_id)
    assert job is not None, f"no job {job_id} in this app's store"
    _wait_for(job, timeout)
    return client.get(f"/api/jobs/{job_id}").json()


class TestAJobSignalsWhenItsThreadIsOver:
    """`job.finished` is what every wait in this file now waits on, so the
    three properties that make it worth waiting on are pinned here rather
    than inferred from the tests that use it."""

    def test_the_thread_is_named_after_the_job(self, studio_profile, event_dir,
                                               monkeypatch):
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        names: list[str] = []

        def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
            names.append(threading.current_thread().name)
            Path(target).write_bytes(b"stub short")
            return target

        monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
        store = jobs_module.JobStore()
        job = jobs_module.start_render_job(studio_profile, store, None)
        _wait_for(job)

        assert names == [f"job-render-{job.id[:8]}"], (
            f"an unnamed job thread is anonymous in a stack dump: {names}")

    def test_finished_is_not_set_until_the_lock_is_released(
            self, studio_profile, event_dir, monkeypatch):
        """The whole point of setting the event from OUTSIDE the runner: a
        terminal status is not a released lock, `finished` is."""
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        started, gate = threading.Event(), threading.Event()

        def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
            started.set()
            gate.wait(timeout=WAIT_TIMEOUT)
            Path(target).write_bytes(b"stub short")
            return target

        monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
        store = jobs_module.JobStore()
        job = jobs_module.start_render_job(studio_profile, store, None)
        try:
            assert started.wait(timeout=WAIT_TIMEOUT)
            assert not job.finished.is_set(), (
                "the signal was set before the runner ran - it says nothing "
                "about the lock or the log then")
            assert EventLock(event_dir).is_held()
        finally:
            gate.set()

        _wait_for(job)
        assert not EventLock(event_dir).is_held(), (
            "finished must mean the runner's whole finally is over")

    def test_a_raising_runner_still_sets_finished(self, monkeypatch):
        """The case a naive implementation loses. Every runner in jobs.py
        handles its own exceptions today, so this drives the helper directly -
        a future one that does not must still signal, or every wait in this
        file waits out its backstop."""
        # Swallowed rather than reported: the raise is this test's own doing.
        # Waited for, so it cannot escape into a later test's excepthook.
        raised = threading.Event()
        monkeypatch.setattr(threading, "excepthook", lambda args: raised.set())
        job = jobs_module.JobStore().create("render")

        def boom():
            raise RuntimeError("the runner exploded")

        jobs_module._start_thread(job, boom)
        _wait_for(job)
        assert raised.wait(WAIT_TIMEOUT), "the runner never raised at all"


class TestJobStoreBounds:
    def test_evicts_oldest_finished_jobs_past_the_cap(self, monkeypatch):
        monkeypatch.setattr(jobs_module, "MAX_JOBS", 5)
        store = jobs_module.JobStore()
        finished = [store.create() for _ in range(5)]
        for job in finished:
            job.finish("done")
        # Creating more (running) jobs past the cap evicts the oldest finished.
        newer = [store.create() for _ in range(5)]
        assert store.get(finished[0].id) is None          # oldest terminal evicted
        assert all(store.get(j.id) is not None for j in newer)  # running kept

    def test_never_evicts_a_running_job(self, monkeypatch):
        monkeypatch.setattr(jobs_module, "MAX_JOBS", 3)
        store = jobs_module.JobStore()
        running = [store.create() for _ in range(10)]  # none finished
        assert all(store.get(j.id) is not None for j in running)

    @pytest.mark.parametrize("ended", ["done", "failed", "stopped"])
    def test_every_status_that_is_not_running_can_be_evicted(
            self, monkeypatch, ended):
        """The cap must hold whatever a job ENDED as.

        `stopped` is the status this branch introduced, and the evictor was
        never extended to it - it read `in ("done", "failed")` - so stopping
        jobs was enough to push the store past MAX_JOBS indefinitely, a cap
        defeated by an ordinary supported action. Parameterised over all
        three rather than adding one case, because the next status added
        would repeat the same omission otherwise.
        """
        monkeypatch.setattr(jobs_module, "MAX_JOBS", 5)
        store = jobs_module.JobStore()
        finished = [store.create() for _ in range(5)]
        for job in finished:
            job.finish(ended)

        newer = [store.create() for _ in range(5)]

        assert store.get(finished[0].id) is None, (
            f"a {ended!r} job was never evicted - MAX_JOBS is defeatable")
        assert all(store.get(j.id) is not None for j in newer)


class TestConnectDedupe:
    def test_a_second_in_flight_connect_is_refused(self, monkeypatch):
        store = jobs_module.JobStore()
        gate = threading.Event()

        def slow_connector(channel_id, force):
            gate.wait(timeout=2)

        first = jobs_module.start_connect_job(None, store, "UCabc",
                                              connector=slow_connector)
        assert first is not None
        with pytest.raises(LockError):
            jobs_module.start_connect_job(None, store, "UCabc",
                                          connector=slow_connector)
        gate.set()  # let the first finish and release the guard

    def test_connect_guard_is_released_after_completion(self, monkeypatch):
        store = jobs_module.JobStore()

        def quick_connector(channel_id, force):
            return None

        job = jobs_module.start_connect_job(None, store, "UCabc",
                                            connector=quick_connector)
        # Wait for the GUARD, not for the status. They are not the same
        # instant: `job.finish(...)` runs inside `run()`'s try and the release
        # runs in its `finally`. This used to poll the status and then connect
        # again immediately, which landed inside that gap often enough to fail
        # a full suite run roughly once - and it failed with a LockError from
        # the SECOND start_connect_job, so it read as an intermittent defect in
        # the guard rather than as this test asking the wrong question. That
        # frequency was taken while a GZIP still stood in front of the release;
        # the commit that fixed this test also moved the release to the front
        # of the `finally`, so the window is now a few instructions. Narrower,
        # not closed, and a race that fires rarely is worse to own than one
        # that fires often - so the wait stays.
        #
        # `any_running()` is the nearest PUBLIC predicate for "the job is over
        # AND no connect is in flight". It is workspace-global (any running
        # job, any channel's connect), and it answers this narrower question
        # only because this store holds nothing else - there is no per-channel
        # predicate to ask, `_active_connects` being private.
        for _ in range(200):        # 2s, far more than a set discard needs
            if not store.any_running():
                break
            time.sleep(0.01)
        assert store.get(job.id).status == "done"
        assert not store.any_running(), "the connect guard was never released"

        # A fresh connect for the same channel now succeeds.
        again = jobs_module.start_connect_job(None, store, "UCabc",
                                              connector=quick_connector)
        assert again is not None



class TestTheReleaseAndTheLogDoNotStrandEachOther:
    """Both the lock release and the job log are best-effort, and ORDERING
    two best-effort steps only moves the problem - whichever goes first can
    strand the other. That is not hypothetical in either direction: with the
    release LAST, a raise out of a log call skipped it and left a lock file
    for the stale-pid takeover to clear; with it FIRST but unguarded, an
    OSError out of `unlink` leaves the job's log handler open and its file
    never compressed. The nested try/finally in every starter is what makes
    neither depend on the other, and NOTHING else in this suite would notice
    it being flattened back into one block - which is the only reason this
    class exists.
    """

    # The PermissionError this test injects has nowhere to go: it is raised
    # inside the runner's own `finally`, so it leaves the thread. That escape
    # IS the scenario - it is what makes the question "did the log still get
    # finished?" worth asking - so the warning is silenced HERE, narrowly,
    # rather than added to pytest.ini, where it would hide a genuinely
    # unhandled thread exception in any other test forever.
    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_a_failing_release_still_finishes_the_job_log(
            self, studio_profile, event_dir, monkeypatch):
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        install_stub(monkeypatch)
        finished: list[str] = []
        monkeypatch.setattr(jobs_module, "finish_job_log",
                            lambda job: finished.append(job.id))

        def unlink_refused(self):
            raise PermissionError("read-only volume")

        monkeypatch.setattr(EventLock, "release", unlink_refused)

        # `job.finished` is set from OUTSIDE the runner, so it is set before
        # the PermissionError leaves the thread. Wait for that escape as well,
        # or it lands in a LATER test where the mark above cannot silence it.
        reported = threading.Event()
        previous_hook = threading.excepthook

        def report_then_note(args):
            previous_hook(args)
            reported.set()

        monkeypatch.setattr(threading, "excepthook", report_then_note)

        store = jobs_module.JobStore()
        job = jobs_module.start_render_job(studio_profile, store, None)
        _wait_for(job)
        assert reported.wait(WAIT_TIMEOUT), "the release never raised at all"

        assert finished == [job.id], (
            "the failing release stranded the job log - the two are ordered "
            "again rather than nested")


class TestPostRender:
    def test_unknown_job_id_is_404(self, client):
        response = client.get("/api/jobs/does-not-exist")
        assert response.status_code == 404

    def test_renders_every_non_discarded_clip_and_skips_the_rest(
            self, event_dir, client, monkeypatch):
        kept = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        discarded = clipstore.write_clip(
            event_dir, clip_entry("https://www.youtube.com/clip/UgkxBarbie456", "Barbie"))
        editorial.save(discarded, editorial.Edit(
            title=None, status=editorial.DISCARDED, transcript=None))

        install_stub(monkeypatch)

        response = client.post(RENDER_URL, json={})
        assert response.status_code == 200
        job_id = response.json()["job_id"]

        body = _wait_for_job(client, job_id)
        assert body["status"] == "done"
        assert body["results"][kept.name]["status"] == "done"
        assert body["results"][discarded.name]["status"] == "skipped"
        assert clipstore.short_path(kept).exists()
        assert not clipstore.short_path(discarded).exists()

    def test_a_failing_clip_does_not_stop_the_job_and_records_its_exception_type(
            self, event_dir, client, monkeypatch):
        good = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        bad = clipstore.write_clip(
            event_dir, clip_entry("https://www.youtube.com/clip/UgkxBarbie456", "Barbie"))

        install_stub(monkeypatch, fail_for={bad.name})

        response = client.post(RENDER_URL, json={"clips": [good.name, bad.name]})
        job_id = response.json()["job_id"]

        body = _wait_for_job(client, job_id)
        assert body["status"] == "failed"  # one bad clip marks the job failed...
        assert body["results"][good.name]["status"] == "done"  # ...but not the good clip
        assert clipstore.short_path(good).exists()
        assert body["results"][bad.name]["status"] == "failed"
        assert "RuntimeError" in body["results"][bad.name]["reason"]
        assert not clipstore.short_path(bad).exists()

    def test_named_clips_are_honored_even_when_discarded(self, event_dir, client, monkeypatch):
        discarded = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        editorial.save(discarded, editorial.Edit(
            title=None, status=editorial.DISCARDED, transcript=None))

        install_stub(monkeypatch)

        response = client.post(RENDER_URL, json={"clips": [discarded.name]})
        job_id = response.json()["job_id"]

        body = _wait_for_job(client, job_id)
        assert body["results"][discarded.name]["status"] == "done"
        assert clipstore.short_path(discarded).exists()

    def test_refuses_with_409_while_a_job_is_already_running_for_this_event(
            self, event_dir, client, monkeypatch):
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        gate = threading.Event()
        install_stub(monkeypatch, gate=gate)

        first = client.post(RENDER_URL, json={})
        assert first.status_code == 200

        try:
            second = client.post(RENDER_URL, json={})
            assert second.status_code == 409
            assert event_dir.name in second.json()["detail"] or "locked" in second.json()["detail"].lower()
        finally:
            gate.set()  # let the first job finish so nothing leaks past the test

        # `_wait_for_job` covers the release, not only the status - the third
        # POST below would otherwise be a coin flip reporting a 409 (the very
        # thing under test) as the failure. See `_wait_for`.
        body = _wait_for_job(client, first.json()["job_id"])
        assert body["status"] == "done"
        # The lock must be released once the job finished, so a THIRD
        # render against the same event is accepted again.
        third = client.post(RENDER_URL, json={})
        assert third.status_code == 200
        _wait_for_job(client, third.json()["job_id"])

    def test_a_lock_already_held_by_someone_else_refuses_before_a_job_is_ever_created(
            self, event_dir, client, monkeypatch):
        """Not the studio's own job this time - a lock held independently
        (as bin/yt-shorts render would hold it) must refuse the API call
        too: a studio render must not be able to race a CLI render."""
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        outside_lock = EventLock(event_dir)
        outside_lock.acquire()
        try:
            response = client.post(RENDER_URL, json={})
            assert response.status_code == 409
        finally:
            outside_lock.release()


class TestStudioRenderProducesCaptions:
    """The gap this module exists to close: jobs.py used to call
    render.build_short with no subtitle_provider at all, so a render
    started from the studio produced a short with NO burned-in captions,
    even with subtitles.enabled and even when the operator had corrected
    the transcript - throwing away exactly the work the studio exists to
    let them do. Every other test in this file stubs render.build_short,
    which can never catch this: the stub happily accepts and ignores
    whatever subtitle_provider it is (or isn't) handed. This class runs the
    REAL render pipeline - yt_shorts.subtitle_pipeline.make_subtitle_provider,
    captions.group_words, subtitle_track.build_track, render.compose, all for
    real - with only yt-dlp stubbed (hand back a local synthetic clip
    instead of downloading) and transcribe() stubbed to a fixed word list
    (so the run is fast and deterministic rather than depending on Whisper
    decoding real speech), and then measures actual pixels in the caption
    band of the finished short.mp4, the same way
    tests/test_render_subtitles.py proves a caption track is visible.

    The editorial correction is deliberately made to differ from the
    stubbed "derived" transcript, so a passing test also proves the studio
    path honours the editorial layer (the correction winning, with the
    conflict reported) - not just that some caption or other appears.
    """

    def test_a_studio_initiated_render_burns_in_the_corrected_captions(
        self, event_dir, studio_profile, monkeypatch, tmp_path, capsys
    ):
        import yt_shorts.render as render_module
        import yt_shorts.subtitle_pipeline as subtitle_pipeline

        source_video = tmp_path / "source.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(source_video),
        ], check=True)

        real_run = subprocess.run

        def fake_run(command, *args, **kwargs):
            if Path(command[0]).name == "yt-dlp":
                target = Path(command[command.index("-o") + 1])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_video, target)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(render_module.subprocess, "run", fake_run)

        # The correction was made against THIS transcript ("based_on")...
        original_words = [{"start": 0.1, "end": 0.5, "text": "wrong"}]
        # ...but what transcribe() now (re-)derives has since changed
        # underneath it - the conflict case editorial.effective_words
        # documents: the correction still wins, but it must be REPORTED.
        changed_words = original_words + [{"start": 0.6, "end": 0.7, "text": "extra"}]
        monkeypatch.setattr(subtitle_pipeline, "transcribe", lambda *a, **k: changed_words)

        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        corrected_words = [
            {"start": 0.2, "end": 0.6, "text": "SHADOW"},
            {"start": 0.8, "end": 1.6, "text": "REALM"},
        ]
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE,
            transcript={"based_on": editorial.checksum(original_words),
                        "words": corrected_words}))

        client = TestClient(create_app())
        response = client.post(RENDER_URL, json={})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        body = _wait_for_job(client, job_id)

        assert body["status"] == "done"
        assert body["results"][directory.name]["status"] == "done"
        # Asserted on the clip's RESULT, not on stderr, and that change is the
        # point rather than a convenience: this job runs on a background
        # thread, so stderr is a channel with no reader, and a note that only
        # went there was how a studio render could lose every caption and say
        # nothing. The reason field is what the render panel shows an operator.
        assert "conflict" in (
            body["results"][directory.name]["reason"] or "").lower(), (
            "the editorial correction differs from the stubbed derived "
            "transcript - the studio path must report that where an operator "
            "will see it"
        )

        short = clipstore.short_path(directory)
        assert short.exists()

        frame = tmp_path / "frame.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(short),
                        "-frames:v", "1", str(frame)], check=True)
        from PIL import Image
        pixels = Image.open(frame).convert("RGBA").load()
        band = [pixels[x, y] for y in range(1290, 1420, 2) for x in range(150, 950, 2)]
        bright = [p for p in band if p[:3] == (255, 255, 255)]
        assert bright, (
            "no caption pixels found in the caption band - a studio-initiated "
            "render produced a short with no visible subtitles"
        )


class TestAnyRunning:
    """The guard the workspace switch/create/copy routes use to refuse
    re-rooting mid-operation: any_running() must be true while a job is
    still "running" OR a connect is in flight, and false once both clear."""

    def test_any_running_reflects_job_status(self):
        store = jobs_module.JobStore()
        assert store.any_running() is False
        job = store.create()
        assert store.any_running() is True     # a fresh job starts "running"
        job.finish("done")
        assert store.any_running() is False

    def test_any_running_reflects_connect_set(self):
        store = jobs_module.JobStore()
        assert store.begin_connect("UC1") is True
        assert store.any_running() is True
        store.end_connect("UC1")
        assert store.any_running() is False


class TestALostCaptionTrackIsReported:
    """A studio render that silently produced a caption-less short was the
    gap here: the subtitle pipeline's notes went to sys.stderr, which a
    background job thread has no reader for, so the render simply succeeded
    and the operator had no way to learn the clip lost its captions - nor
    why. An overlapping word list, which the transcript editor lets an
    operator create with one dragged timing, costs the clip EVERY caption.

    Both halves are asserted: the note reaches the clip's own result (so the
    render panel shows it - RenderPanel already renders `result.reason`) and
    it reaches the job's log line.
    """

    def test_the_note_reaches_the_clips_result_and_the_job_log(
            self, event_dir, studio_profile, monkeypatch):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))

        def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
            # The provider is what reports; build_short is the thing that
            # calls it, so a stub that ignores it could never see this.
            provider = kwargs.get("subtitle_provider")
            if provider is not None:
                provider(str(Path(work_dir) / "raw.mp4"))
            Path(target).write_bytes(b"stub short")
            return target

        monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
        monkeypatch.setattr(
            "yt_shorts.subtitle_pipeline.transcribe",
            lambda *a, **kw: [{"start": 0.0, "end": 1.0, "text": " here"},
                              {"start": 0.5, "end": 9.0, "text": " stretched"}])
        config = studio_profile.config
        config["subtitles"] = {**config.get("subtitles", {}), "enabled": True}

        job = jobs_module.JobStore().create(kind="render")
        jobs_module._render_one(studio_profile, directory, directory.name,
                         skip_discarded=False, job=job)

        snapshot = job.snapshot()
        result = snapshot["results"][directory.name]
        assert result["status"] == "done", "the short itself rendered fine"
        assert result["reason"], "the clip lost every caption and said nothing"
        assert "no subtitles" in result["reason"], result["reason"]
        assert any("no subtitles" in line for line in snapshot["log"]), snapshot["log"]

    def test_a_clean_render_records_no_reason(
            self, event_dir, studio_profile, monkeypatch):
        """The other half: a clip whose captions build fine must not grow a
        spurious note, or the panel would cry wolf on every render."""
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        install_stub(monkeypatch)

        job = jobs_module.JobStore().create(kind="render")
        jobs_module._render_one(studio_profile, directory, directory.name,
                         skip_discarded=False, job=job)

        result = job.snapshot()["results"][directory.name]
        assert result["status"] == "done"
        assert result["reason"] is None, result["reason"]

    def test_the_job_s_own_logger_is_handed_to_the_pipeline(
            self, event_dir, studio_profile, monkeypatch):
        """The other half of the wiring, and it needs its own test: the note
        reaching the clip's RESULT comes from `on_note`, so a `_render_one`
        that forgot `logger=` would still satisfy the assertions above while
        the job's own log file - logs/jobs/render-<id>.log, the thing an
        operator opens to find out what happened - silently lost it."""
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        install_stub(monkeypatch)
        captured: dict = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return None

        monkeypatch.setattr(jobs_module, "make_subtitle_provider", spy)
        job = jobs_module.JobStore().create(kind="render")
        jobs_module._render_one(studio_profile, directory, directory.name,
                                skip_discarded=False, job=job)

        assert captured.get("logger") is jobs_module.job_logger(job), (
            "the pipeline was not given the job's own logger, so its notes "
            "would not reach this job's log file"
        )
        assert callable(captured.get("on_note"))


class TestRenderAppliesTheTrim:
    """A render writes an UNTRIMMED short.mp4. Without re-applying, the
    operator's trim silently vanishes on every re-render - and the stale
    master from before the render must go first, or the next cut would read
    the old composition instead of this one."""

    def test_the_render_forgets_the_old_master_then_re_applies(
            self, event_dir, studio_profile, monkeypatch):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE, transcript=None, trim=(3.0, 2.0)))

        def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
            Path(target).write_bytes(b"stub short")
            return target

        calls = []
        monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
        monkeypatch.setattr("yt_shorts.studio.jobs.trim.forget_applied",
                            lambda d: calls.append(("forget", Path(d).name)))
        monkeypatch.setattr(
            "yt_shorts.studio.jobs.trim.ensure_applied",
            lambda d, e, **kw: calls.append(("apply", Path(d).name, e.trim)) or True)

        job = jobs_module.JobStore().create(kind="render")
        jobs_module._render_one(studio_profile, directory, directory.name,
                                skip_discarded=False, job=job)

        assert [c[0] for c in calls] == ["forget", "apply"], calls
        assert calls[1][2] == (3.0, 2.0)
        assert job.snapshot()["results"][directory.name]["status"] == "done"


class TestTrimJobRunsForReal:
    """Task 4's Critical 2: `grep -rn "_run_trim(" only found its own
    definition - TestTrimRoutes.test_applying_starts_a_job (in
    test_studio_api.py) monkeypatches jobs.start_trim_job wholesale, so
    POST …/clips/{name}/trim never actually reached `_run_trim`. These two
    tests drive the real route (client.post -> start_trim_job -> the
    background thread -> _run_trim), stubbing only `trim.ensure_applied` -
    the one call that would otherwise shell out to a real ffmpeg/ffprobe
    (trim.py's own module docstring: it re-encodes) - the same way
    TestPostRender above drives a render for real and stubs only
    render.build_short. The real ffmpeg cut itself is Task 2's test
    surface, not this one.
    """

    def _clip_with_pending_trim(self, event_dir, client, url):
        directory = clipstore.write_clip(event_dir, clip_entry(url, "Speedy!"))
        clipstore.short_path(directory).write_bytes(b"pretend mp4")
        patched = client.patch(f"{EV}/clips/{directory.name}", json={"trim": [3.0, 2.0]})
        assert patched.status_code == 200
        return directory

    def test_a_successful_trim_is_reflected_by_a_later_get(
            self, event_dir, client, monkeypatch):
        directory = self._clip_with_pending_trim(
            event_dir, client, "https://www.youtube.com/clip/UgkxTrimOK000")

        def fake_ensure_applied(clip_dir, edit, **kwargs):
            # A real ensure_applied re-encodes short.mp4 with ffmpeg and
            # writes short.trim.json recording what it cut (see trim.py).
            # Stubbed to just the state write, so this test proves the
            # REPORTING path - job status, then a later GET's trim_applied -
            # rather than the ffmpeg cut, which is covered elsewhere.
            clipstore.short_trim_state_path(clip_dir).write_text(
                json.dumps({"head": 3.0, "tail": 2.0}), encoding="utf-8")
            return True

        monkeypatch.setattr(jobs_module.trim, "ensure_applied", fake_ensure_applied)

        response = client.post(f"{EV}/clips/{directory.name}/trim")
        assert response.status_code == 200
        body = _wait_for_job(client, response.json()["job_id"])
        assert body["status"] == "done"
        assert body["results"][directory.name]["status"] == "done"

        clip_body = client.get(f"{EV}/clips/{directory.name}").json()
        assert clip_body["trim_applied"] == [3.0, 2.0], (
            "the job ran and the state file was written, but GET's own "
            "trim_applied field never picked it up"
        )

    def test_a_failed_cut_fails_the_clip_and_still_releases_the_lock(
            self, event_dir, client, monkeypatch):
        directory = self._clip_with_pending_trim(
            event_dir, client, "https://www.youtube.com/clip/UgkxTrimFail000")

        def failing_ensure_applied(clip_dir, edit, **kwargs):
            raise jobs_module.trim.TrimError("ffmpeg exited 1")

        monkeypatch.setattr(jobs_module.trim, "ensure_applied", failing_ensure_applied)

        response = client.post(f"{EV}/clips/{directory.name}/trim")
        assert response.status_code == 200
        # Like the other two "the lock must be released" tests in this file,
        # this waits for `finished` rather than the status - a status-only
        # wait leaves the second POST below able to 409 on a lock that is
        # about to be released, reporting the thing under test as the failure.
        body = _wait_for_job(client, response.json()["job_id"])
        assert body["status"] == "failed"
        result = body["results"][directory.name]
        assert result["status"] == "failed"
        assert "TrimError" in result["reason"] and "ffmpeg exited 1" in result["reason"], (
            result["reason"]
        )

        # The event lock must be released even though the job failed - proven
        # by starting a second trim job for the SAME event and watching it
        # actually run, rather than 409ing on a lock the failed job left
        # held. (Not asserted via a private attribute - see this task's brief.)
        monkeypatch.setattr(jobs_module.trim, "ensure_applied",
                            lambda clip_dir, edit, **kwargs: True)
        second = client.post(f"{EV}/clips/{directory.name}/trim")
        assert second.status_code == 200, (
            "a lock left held by the failed job would 409 this instead"
        )
        second_body = _wait_for_job(client, second.json()["job_id"])
        assert second_body["status"] == "done"


@pytest.fixture
def erf_profile(studio_profile):
    # Same profile TestStudioRenderProducesCaptions etc. already build via
    # studio_profile - just a name matching this task's brief, since a
    # detect job is event-scoped exactly like a render job.
    return studio_profile


class TestDetectJobReportsAnAnalysis:
    def test_records_the_engine_and_counts_not_clip_names(self, tmp_path, erf_profile):
        # detect_moments now returns a PATH; a job that still expects a list of
        # clip names would iterate the characters of that path.
        analysis = tmp_path / "moments.json"
        analysis.write_text('{"engine": "model:claude-haiku-4-5", '
                            '"moments": [{"start": 1, "end": 20}], "missing_windows": []}')
        store = jobs_module.JobStore()
        job = jobs_module.start_detect_job(erf_profile, store, "vid123", "Race",
                                           detect_fn=lambda *a, **k: analysis)
        _wait_for(job)
        assert job.results
        assert any("claude-haiku-4-5" in (r.reason or "") for r in job.results.values())
        assert any("1 moment" in (r.reason or "") for r in job.results.values())


class TestStoppingAJobIsNotFailingIt:
    """A job the operator stopped reads `stopped`. `Stopped` must never be
    caught by the blanket handlers that mark a job `failed` - the operator
    asked for this, and calling it a failure would send them looking for a
    cause that does not exist."""

    def test_a_stop_is_not_a_failure(self, tmp_path, erf_profile):
        store = jobs_module.JobStore()
        token = CancelToken()

        def stopping_detect(*args, **kwargs):
            raise Stopped("stopped after 3 window(s)")

        job = jobs_module.start_detect_job(erf_profile, store, "vid123", "Race",
                                           detect_fn=stopping_detect, cancel=token)
        _wait_for(job)
        assert job.status == "stopped"
        assert all(r.status != "failed" for r in job.results.values())

    def test_a_render_stops_between_clips_and_leaves_the_rest_unrendered(
            self, studio_profile, event_dir, monkeypatch):
        for suffix, hook in (("Aaa", "A"), ("Bbb", "B"), ("Ccc", "C")):
            clipstore.write_clip(event_dir, clip_entry(CLIP_URL + suffix, hook))
        token = CancelToken()
        rendered = []

        def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
            rendered.append(Path(work_dir).name)
            token.request_stop()          # the operator clicks after the first clip
            Path(target).write_bytes(b"stub short")
            return target

        monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
        store = jobs_module.JobStore()
        job = jobs_module.start_render_job(studio_profile, store, None, cancel=token)
        _wait_for(job)
        assert len(rendered) == 1, "a clip was rendered after the stop"
        assert job.status == "stopped"
        # The clip that DID render is still reported as done - it really rendered.
        assert [r.status for r in job.results.values()] == ["done"]

    def test_a_render_hard_stopped_mid_clip_reads_stopped_not_failed(
            self, studio_profile, event_dir, monkeypatch):
        """The gap Task 3 flagged: a hard stop lands INSIDE build_short's own
        ffmpeg/yt-dlp child (see render.compose's cancel plumbing, pinned for
        real in tests/test_render.py's TestComposeHardStop), not only between
        clips. That reaches _render_one as a bare `Stopped`, exactly as it
        would from a real run_cancellable kill - this pins that the render
        loop reads it as `stopped`, never as a failed clip."""
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "A"))
        token = CancelToken()

        def hard_stopped_build_short(source, hook, footer, target, config, work_dir, **kwargs):
            raise Stopped("the subprocess was terminated on a hard stop")

        monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short",
                            hard_stopped_build_short)
        store = jobs_module.JobStore()
        job = jobs_module.start_render_job(studio_profile, store, None, cancel=token)
        _wait_for(job)
        assert job.status == "stopped"
        assert all(r.status != "failed" for r in job.results.values())
        assert job.results == {}, "the clip was never recorded as done or failed"

    def test_a_stopped_trim_job_reads_stopped(self, studio_profile, event_dir,
                                              monkeypatch):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "A"))

        def stopping_ensure(*args, **kwargs):
            raise Stopped("the subprocess was terminated on a hard stop")

        monkeypatch.setattr("yt_shorts.studio.jobs.trim.ensure_applied", stopping_ensure)
        store = jobs_module.JobStore()
        job = jobs_module.start_trim_job(studio_profile, store, directory.name,
                                         cancel=CancelToken())
        _wait_for(job)
        assert job.status == "stopped"

    def test_a_stopped_copy_job_reads_stopped(self, tmp_path):
        # The fifth stoppable starter (Task 5). Same stance as the four
        # above: a copy the operator stopped is `stopped`, never `failed`.
        def stopping_copier(src, parent, name, created, **kwargs):
            raise Stopped("stopped after the current file")

        store = jobs_module.JobStore()
        job = jobs_module.start_copy_job(store, tmp_path, tmp_path, "ws-copy",
                                         "2026-08-01T00:00:00", lambda _p: None,
                                         copier=stopping_copier,
                                         cancel=CancelToken())
        _wait_for(job)
        assert job.status == "stopped"
        assert all(r.status != "failed" for r in job.results.values())

    def test_upload_takes_no_cancel_token_at_all(self):
        # Not "accepts and ignores one": a half-finished upload to YouTube is
        # worse than waiting for it, so there must be no parameter a future
        # UI could wire a button to and have it silently do nothing.
        import inspect
        for function in (jobs_module.start_upload_job, jobs_module._default_uploader):
            assert "cancel" not in inspect.signature(function).parameters, (
                f"{function.__name__} accepts a cancel token, but upload cannot "
                f"be stopped - see KINDS['upload'].hard_stop_allowed")
        assert jobs_module.KINDS["upload"].hard_stop_allowed is False


class TestCancelTokenForwarding:
    """A code review found that jobs.py's own forwarding of a CancelToken
    onto its three cancel-aware callees (render.build_short, trim.
    ensure_applied, a detect_fn) was unverified everywhere except at the
    compose() level directly: mutating any ONE of these forwarding sites to
    "accept the token, then drop it before the callee" left the whole suite
    green, because every existing test only checks the JOB's eventual
    status, never what the callee itself actually received. These capture
    the kwargs the callee is called with and assert the token rode along -
    each dies if its forwarding site is severed (verified by hand, see
    task-3-report.md's addendum)."""

    def test_the_token_reaches_build_short(self, studio_profile, event_dir, monkeypatch):
        # Driven through the PUBLIC starter (start_render_job), not the
        # private _render_one directly - calling _render_one by hand only
        # proves that function forwards its own cancel argument, and stays
        # green even if _run (the actual caller, at its `_render_one(...,
        # cancel=cancel)` call) silently drops the token before it gets
        # there. Going through start_render_job covers that hop too, the
        # same shape test_the_token_reaches_ensure_applied and
        # test_the_token_reaches_detect_fn already use.
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "A"))
        token = CancelToken()
        seen: list = []

        def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
            seen.append(kwargs.get("cancel"))
            Path(target).write_bytes(b"stub short")
            return target

        monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
        store = jobs_module.JobStore()
        job = jobs_module.start_render_job(studio_profile, store, None, cancel=token)
        _wait_for(job)
        assert seen == [token], (
            "_render_one did not forward its cancel token into render.build_short"
        )

    def test_the_token_reaches_ensure_applied(self, studio_profile, event_dir, monkeypatch):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "A"))
        token = CancelToken()
        seen: list = []

        def fake_ensure_applied(clip_dir, edit, **kwargs):
            seen.append(kwargs.get("cancel"))
            return True

        monkeypatch.setattr(jobs_module.trim, "ensure_applied", fake_ensure_applied)
        store = jobs_module.JobStore()
        job = jobs_module.start_trim_job(studio_profile, store, directory.name,
                                         cancel=token)
        _wait_for(job)
        assert seen == [token], (
            "_run_trim did not forward its cancel token into trim.ensure_applied"
        )

    def test_the_token_reaches_detect_fn(self, tmp_path, erf_profile):
        token = CancelToken()
        seen: list = []
        analysis = tmp_path / "moments.json"
        analysis.write_text(
            '{"engine": "model:x", "moments": [], "missing_windows": []}',
            encoding="utf-8")

        def fake_detect_fn(*args, **kwargs):
            seen.append(kwargs.get("cancel"))
            return analysis

        store = jobs_module.JobStore()
        job = jobs_module.start_detect_job(erf_profile, store, "vid123", "Race",
                                           detect_fn=fake_detect_fn, cancel=token)
        _wait_for(job)
        assert seen == [token], (
            "_run_detect did not forward its cancel token into detect_fn"
        )

    def test_the_token_reaches_transcribe_fn(self, erf_profile):
        # Task 4: start_transcribe_job/_run_transcribe is a fourth forwarding
        # site added alongside the three above - same hazard, same proof
        # shape: capture what transcribe_fn is actually called with rather
        # than only checking the job's eventual status.
        token = CancelToken()
        seen: list = []

        def fake_transcribe_fn(*args, **kwargs):
            seen.append(kwargs.get("cancel"))
            return StreamTranscript(video_id="vid-task4-forward",
                                    audio_path=Path("unused"),
                                    duration_seconds=1.0, words=[])

        store = jobs_module.JobStore()
        job = jobs_module.start_transcribe_job(
            erf_profile, store, "vid-task4-forward",
            transcribe_fn=fake_transcribe_fn, cancel=token)
        _wait_for(job)
        assert seen == [token], (
            "_run_transcribe did not forward its cancel token into transcribe_fn"
        )

    def test_the_token_reaches_the_copier(self, tmp_path):
        # Task 5: a fifth forwarding site. KINDS["copy"] promised a stop
        # "after the current file" while start_copy_job took no token at all
        # and its copy had no checkpoint - the same unbacked promise that was
        # found and closed for `render` earlier on this branch.
        token = CancelToken()
        seen: list = []

        def fake_copier(src, parent, name, created, **kwargs):
            seen.append(kwargs.get("cancel"))
            return Path(parent) / name

        store = jobs_module.JobStore()
        job = jobs_module.start_copy_job(store, tmp_path, tmp_path, "ws-copy",
                                         "2026-08-01T00:00:00", lambda _p: None,
                                         copier=fake_copier, cancel=token)
        _wait_for(job)
        assert seen == [token], (
            "start_copy_job did not forward its cancel token into the copier"
        )


class TestJobRecordsItsCancelToken:
    """`job.cancel` is what a stop actually reaches, and since Task 5 it has
    a READER: `studio.worker.Worker.request_stop` looks the token up on the
    Job rather than keeping a second copy of it, so a starter that stopped
    recording its token would break stopping loudly (the worker refuses,
    naming the kind) instead of leaving a button that silently does nothing.
    This test proves the five stoppable starters RECORD the token they were
    given; tests/test_studio_worker.py's TestStopping covers the reading
    end."""

    def test_every_stoppable_starter_records_the_token_it_was_given(
            self, studio_profile, event_dir, erf_profile, tmp_path, monkeypatch):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "A"))
        install_stub(monkeypatch)
        monkeypatch.setattr(jobs_module.trim, "ensure_applied",
                            lambda *a, **k: True)
        store = jobs_module.JobStore()

        render_token = CancelToken()
        render_job = jobs_module.start_render_job(studio_profile, store, None,
                                                   cancel=render_token)
        assert render_job.cancel is render_token
        # Every starter below takes the SAME event lock, so each wait has to
        # cover the release as well as the status - which `finished` does.
        _wait_for(render_job)

        def stopping_detect(*args, **kwargs):
            raise Stopped("stopped")

        detect_token = CancelToken()
        detect_job = jobs_module.start_detect_job(
            erf_profile, store, "vid123", "Race",
            detect_fn=stopping_detect, cancel=detect_token)
        assert detect_job.cancel is detect_token
        _wait_for(detect_job)

        trim_token = CancelToken()
        trim_job = jobs_module.start_trim_job(studio_profile, store, directory.name,
                                              cancel=trim_token)
        assert trim_job.cancel is trim_token
        _wait_for(trim_job)

        def stopping_transcribe(*args, **kwargs):
            raise Stopped("stopped")

        transcribe_token = CancelToken()
        transcribe_job = jobs_module.start_transcribe_job(
            erf_profile, store, "vid-task4-cancel-record",
            transcribe_fn=stopping_transcribe, cancel=transcribe_token)
        assert transcribe_job.cancel is transcribe_token
        _wait_for(transcribe_job)

        copy_token = CancelToken()
        copy_job = jobs_module.start_copy_job(
            store, tmp_path, tmp_path, "ws-record", "2026-08-01T00:00:00",
            lambda _p: None, copier=lambda *a, **k: tmp_path / "ws-record",
            cancel=copy_token)
        assert copy_job.cancel is copy_token
        _wait_for(copy_job)


class TestTranscribeJobWritesOnlyATranscript:
    """Task 4: whole-stream transcription becomes a job kind of its own -
    start_transcribe_job writes streams/<video_id>/transcript.json (and its
    chunk cache) and nothing else: no moments.json, no clip directory. The
    studio's detect job is the ONLY thing that spends money, and only once
    an operator has separately asked for a transcript."""

    def test_a_transcribe_job_writes_the_transcript_and_nothing_else(
            self, tmp_path, erf_profile):
        # A video id used by no other test in this file: the fixed workspace
        # root is SESSION-scoped (see conftest.py's _fixed_workspace_root),
        # so a transcript.json this test writes under streams/<video_id>/
        # would otherwise leak into any later test reusing "vid123".
        video_id = "vid-task4-transcribe-only"
        from yt_shorts.stream_transcribe import StreamTranscript

        def fake_transcribe(video_id, workspace_dir, *, glossary=None, cancel=None):
            stream_dir = Path(workspace_dir) / "streams" / video_id
            stream_dir.mkdir(parents=True, exist_ok=True)
            words = [{"start": 0.0, "end": 0.5, "text": " hi"}]
            (stream_dir / "transcript.json").write_text(json.dumps({
                "video_id": video_id, "duration_seconds": 600.0,
                "words": words, "missing_chunks": [],
            }), encoding="utf-8")
            return StreamTranscript(video_id=video_id,
                                    audio_path=stream_dir / "audio.webm",
                                    duration_seconds=600.0, words=words)

        store = jobs_module.JobStore()
        job = jobs_module.start_transcribe_job(erf_profile, store, video_id,
                                               transcribe_fn=fake_transcribe)
        _wait_for(job)
        assert job.status == "done"

        root = jobs_module._resolve_workspace().root
        assert (root / "streams" / video_id / "transcript.json").exists()
        assert not (root / "streams" / video_id / "moments.json").exists()
        assert list(clipstore.iter_clip_dirs(erf_profile.event_dir)) == []


class TestTranscribeJobTakesTheEventLock:
    """Same guarantee as start_render_job/start_detect_job/start_trim_job
    (see TestPostRender.test_refuses_with_409_while_a_job_is_already_running_for_this_event
    and jobs.py's own module docstring): the event lock is what stops a
    studio job and a CLI run - or two studio jobs - from racing each other
    over the same event, the exact race that destroyed reference files
    earlier in this project. Without `event_lock.acquire()`,
    EventLock.release() is a silent no-op when never acquired (lock.py), so
    a second transcribe job for the same event would start immediately
    instead of being refused."""

    def test_a_second_transcribe_job_is_refused_while_the_first_still_holds_the_lock(
            self, erf_profile):
        store = jobs_module.JobStore()
        gate = threading.Event()

        def slow_transcribe(video_id, workspace_dir, *, glossary=None, cancel=None):
            gate.wait(timeout=5.0)
            return StreamTranscript(video_id=video_id,
                                    audio_path=Path(workspace_dir) / "audio.webm",
                                    duration_seconds=1.0, words=[])

        first = jobs_module.start_transcribe_job(
            erf_profile, store, "vid-task4-lock-a", transcribe_fn=slow_transcribe)
        try:
            with pytest.raises(LockError):
                jobs_module.start_transcribe_job(
                    erf_profile, store, "vid-task4-lock-b",
                    transcribe_fn=slow_transcribe)
        finally:
            gate.set()  # let the first job finish so nothing leaks past the test

        # This is the test that ASSERTS the release, so waiting on the status
        # alone would assert a property that is true but not instantaneous -
        # the release runs in the runner's `finally`, after `job.finish`, and
        # the third start below would be a coin flip. `finished` covers it.
        body = _wait_for(first)
        assert body.status == "done"
        # The lock must be released once the job finished, so a THIRD
        # transcribe against the same event is accepted again.
        third = jobs_module.start_transcribe_job(
            erf_profile, store, "vid-task4-lock-c", transcribe_fn=slow_transcribe)
        gate.set()
        _wait_for(third)


class TestStudioDetectRequiresATranscript:
    """The central rule this task changes: the studio's detect job no longer
    transcribes on its own. This drives start_detect_job with NO detect_fn
    override - the real, default wiring a route would use - which must now
    be require_cached_transcript, not detect_moments's own default
    (transcribe_stream)."""

    def test_the_studio_detect_job_refuses_without_a_cached_transcript(
            self, tmp_path, erf_profile):
        # A video id unique to this test - see the sibling class above on why
        # the session-scoped fixed workspace root makes that necessary.
        video_id = "vid-task4-detect-refuses"
        store = jobs_module.JobStore()
        job = jobs_module.start_detect_job(erf_profile, store, video_id, "Race")
        _wait_for(job)
        assert job.status == "failed"
        reason = job.results["detect"].reason or ""
        assert "TranscriptNotCached" in reason
        assert video_id in reason

        root = jobs_module._resolve_workspace().root
        assert not (root / "streams" / video_id / "moments.json").exists()


class TestProgressIsForwardedByTheStarters:
    """What each long starter reports, and to whom.

    The starters are the middle of the route from the work's own
    `(done, total)` to `Entry.progress`: `studio.worker` builds the
    callback and adds the unit, the work counts, and these three carry it
    between them. Driven directly here (no worker, no queue) so what is
    measured is the forwarding itself.
    """

    def test_a_transcribe_job_forwards_the_callback_to_the_transcriber(
            self, erf_profile):
        seen = []
        handed = {}

        def fake_transcribe(video_id, workspace_dir, *, glossary=None,
                            cancel=None, progress=None):
            handed["progress"] = progress
            progress(1, 4)
            return StreamTranscript(video_id=video_id,
                                    audio_path=Path(workspace_dir) / "audio.webm",
                                    duration_seconds=1.0, words=[])

        job = jobs_module.start_transcribe_job(
            erf_profile, jobs_module.JobStore(), "vid-progress-a",
            transcribe_fn=fake_transcribe,
            progress=lambda done, total: seen.append((done, total)))
        _wait_for(job)

        assert job.status == "done"
        assert seen == [(1, 4)]

    def test_a_transcriber_that_never_heard_of_progress_is_called_without_it(
            self, erf_profile):
        """The `cancel_kwargs` rule, applied to this parameter.

        Every injected `transcribe_fn` in this suite was written before the
        keyword existed, and a starter that passed `progress=None`
        unconditionally would TypeError every one of them. This fake
        accepts NO extra keyword at all, so it fails loudly if that ever
        changes.
        """
        def strict_transcribe(video_id, workspace_dir, *, glossary=None):
            return StreamTranscript(video_id=video_id,
                                    audio_path=Path(workspace_dir) / "audio.webm",
                                    duration_seconds=1.0, words=[])

        job = jobs_module.start_transcribe_job(
            erf_profile, jobs_module.JobStore(), "vid-progress-b",
            transcribe_fn=strict_transcribe)
        _wait_for(job)

        assert job.status == "done", job.results

    def test_a_detect_job_logs_every_window_and_forwards_it_too(
            self, tmp_path, erf_profile):
        # Two readers, one callback: the job's log had the per-window line
        # before this task and must keep it, because a detect started from
        # a route rather than the queue still narrates there.
        analysis = tmp_path / "moments.json"
        analysis.write_text('{"engine": "lexicon", "moments": [], '
                            '"missing_windows": []}')
        seen = []
        lines = []

        def fake_detect(video_id, workspace_root, config, *, stream_title,
                        logger=None, progress=None, **_kwargs):
            logger.info = lambda fmt, *args: lines.append(fmt % args)
            progress(2, 7)
            return analysis

        job = jobs_module.start_detect_job(
            erf_profile, jobs_module.JobStore(), "vid-progress-c", "Race",
            detect_fn=fake_detect,
            progress=lambda done, total: seen.append((done, total)))
        _wait_for(job)

        assert seen == [(2, 7)]
        assert "window 2/7" in lines

    def test_a_detect_job_without_a_callback_still_narrates(
            self, tmp_path, erf_profile):
        analysis = tmp_path / "moments.json"
        analysis.write_text('{"engine": "lexicon", "moments": [], '
                            '"missing_windows": []}')
        lines = []

        def fake_detect(video_id, workspace_root, config, *, stream_title,
                        logger=None, progress=None, **_kwargs):
            logger.info = lambda fmt, *args: lines.append(fmt % args)
            progress(1, 1)
            return analysis

        job = jobs_module.start_detect_job(
            erf_profile, jobs_module.JobStore(), "vid-progress-d", "Race",
            detect_fn=fake_detect)
        _wait_for(job)

        assert job.status == "done"
        assert "window 1/1" in lines

    def test_a_render_job_reports_after_each_clip_including_a_skipped_one(
            self, studio_profile, event_dir, monkeypatch):
        # A discarded clip is skipped, not rendered - and it still counts:
        # it is a clip nobody will look at again on this run, and leaving it
        # out would stall the reading one short for the rest of the job.
        kept = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        discarded = clipstore.write_clip(
            event_dir, clip_entry("https://www.youtube.com/clip/UgkxBarbie456",
                                  "Barbie"))
        editorial.save(discarded, editorial.Edit(
            title=None, status=editorial.DISCARDED, transcript=None))
        install_stub(monkeypatch)
        seen = []

        job = jobs_module.start_render_job(
            studio_profile, jobs_module.JobStore(), None,
            progress=lambda done, total: seen.append((done, total)))
        _wait_for(job)

        assert job.status == "done"
        assert sorted(job.results) == sorted([kept.name, discarded.name])
        assert seen == [(1, 2), (2, 2)]

    def test_a_render_job_does_not_report_a_clip_a_stop_cut_short(
            self, studio_profile, event_dir, monkeypatch):
        # The clip the stop landed on was not finished, so counting it would
        # claim a short exists that does not.
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        clipstore.write_clip(
            event_dir, clip_entry("https://www.youtube.com/clip/UgkxBarbie456",
                                  "Barbie"))
        install_stub(monkeypatch)
        token = CancelToken()
        seen = []

        job = jobs_module.start_render_job(
            studio_profile, jobs_module.JobStore(), None, cancel=token,
            progress=lambda done, total: (seen.append((done, total)),
                                          token.request_stop()))
        _wait_for(job)

        assert job.status == "stopped"
        assert seen == [(1, 2)]
