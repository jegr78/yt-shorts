"""A background job writes its own log file, gzipped when it finishes."""

import gzip
from pathlib import Path

import pytest

from yt_shorts import logsetup
from yt_shorts.studio import jobs


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A JobStore whose job logs land in tmp_path/logs/jobs."""
    monkeypatch.setattr(jobs, "_resolve_workspace",
                        type("W", (), {"root": tmp_path}))
    return jobs.JobStore()


def test_job_gets_its_own_log_file_named_for_its_kind(store, tmp_path):
    job = store.create("detect")
    assert job.kind == "detect"
    assert Path(job.log_path).name == f"detect-{job.id}.log"
    assert Path(job.log_path).parent == tmp_path / "logs" / "jobs"


def test_snapshot_exposes_kind_and_log_name_but_not_the_path(store):
    job = store.create("render")
    snapshot = job.snapshot()
    assert snapshot["kind"] == "render"
    assert snapshot["log_name"] == f"render-{job.id}.log"
    assert "log_path" not in snapshot     # the client asks by name; the server resolves


def test_recorded_lines_reach_the_job_log(store):
    job = store.create("render")
    job.record("clip-a", "done", None, "done: clip-a")
    job.record("clip-b", "failed", "boom", "ERROR: clip-b: boom")
    for handler in jobs.job_logger(job).handlers:
        handler.flush()
    body = Path(job.log_path).read_text(encoding="utf-8")
    assert "INFO done: clip-a" in body
    assert "ERROR ERROR: clip-b: boom" in body


def test_finishing_the_job_gzips_its_log(store):
    job = store.create("upload")
    job.record("clip", "done", None, "done: clip")
    plain = Path(job.log_path)
    jobs.finish_job_log(job)
    gz = Path(str(plain) + ".gz")
    assert gz.exists() and not plain.exists()
    assert "done: clip" in gzip.decompress(gz.read_bytes()).decode("utf-8")


def test_finish_job_log_is_idempotent(store):
    job = store.create("upload")
    job.record("clip", "done", None, "done: clip")
    plain = Path(job.log_path)
    gz = Path(str(plain) + ".gz")
    jobs.finish_job_log(job)
    original = gzip.decompress(gz.read_bytes()).decode("utf-8")
    assert "done: clip" in original

    jobs.finish_job_log(job)              # must not raise on the already-gzipped log

    # The second call must be a true no-op: the .gz from the first call must
    # survive intact, not be destroyed by a failing re-compress attempt on
    # the now-absent plain file (the bug this test guards against).
    assert gz.exists()
    assert gzip.decompress(gz.read_bytes()).decode("utf-8") == original


def test_job_log_does_not_leak_into_the_central_log(store, tmp_path):
    """A per-job logger sits under the 'ytshorts' tree by name but must not
    propagate: a one-hour transcription would otherwise bury the app log."""
    central = tmp_path / "logs" / "yt-shorts.log"
    logsetup.configure_logging("ytshorts", central, to_stdout=False)
    try:
        job = store.create("detect")
        job.record("x", "done", None, "detected: x")
        for handler in jobs.job_logger(job).handlers:
            handler.flush()
        # The central handler is delay=True (see logsetup.configure_logging):
        # with propagation genuinely stopped, nothing ever emits to it, so the
        # file itself may not exist yet - that absence IS the proof of no
        # leak, not a reason to fail reading it.
        assert not central.exists() or "detected: x" not in central.read_text(encoding="utf-8")
    finally:
        logsetup.close_logging("ytshorts")


def test_run_detect_hands_the_jobs_own_logger_to_detect_fn(store, tmp_path):
    """Regression for the fix: `_run_detect` used to tell the operator an
    empty result was explained 'in the job log', but detect_moments only
    ever logged through the module logger, which reaches the CENTRAL app
    log, not the job's own file - so the pointer led nowhere useful. Now
    `_run_detect` passes `logger=job_logger(job)` into `detect_fn`, so a
    detect_fn that logs through it (as detect_moments now does) lands its
    diagnosis in the job's own log file."""
    class _NoOpLock:
        def release(self):
            pass

    class _Profile:
        config: dict = {}

    def fake_detect_fn(video_id, workspace_root, config, *,
                       stream_title, logger, progress=None):
        logger.warning("%s: no moments detected - the transcript has 0 words "
                       "(2 chunk(s) failed)", video_id)
        written = tmp_path / "moments.json"
        written.write_text('{"engine": "lexicon", "moments": [], '
                           '"missing_windows": []}', encoding="utf-8")
        return written

    job = store.create("detect")
    plain = Path(job.log_path)
    jobs.job_logger(job)  # created lazily on first use elsewhere; harmless here
    jobs._run_detect(_Profile(), job, "vid123", "Race Part 1", _NoOpLock(), fake_detect_fn)

    # _run_detect's own `finally` gzips the log on completion, same as every
    # other job kind (see test_finishing_the_job_gzips_its_log above).
    gz = Path(str(plain) + ".gz")
    assert gz.exists() and not plain.exists()
    body = gzip.decompress(gz.read_bytes()).decode("utf-8")
    assert "no moments detected" in body
    assert "0 words" in body
    assert "vid123" in body


def test_run_detects_empty_transcript_diagnosis_does_not_leak_into_the_central_log(
        store, tmp_path):
    """Same isolation guarantee test_job_log_does_not_leak_into_the_central_log
    pins for ordinary records, checked specifically for the detect diagnosis
    this fix threads through: it must land ONLY in the job's own log."""
    central = tmp_path / "logs" / "yt-shorts.log"
    logsetup.configure_logging("ytshorts", central, to_stdout=False)
    try:
        class _NoOpLock:
            def release(self):
                pass

        class _Profile:
            config: dict = {}

        def fake_detect_fn(video_id, workspace_root, config, *,
                           stream_title, logger, progress=None):
            logger.warning("%s: no moments detected - the transcript has 0 words",
                           video_id)
            written = tmp_path / "moments.json"
            written.write_text('{"engine": "lexicon", "moments": [], '
                               '"missing_windows": []}', encoding="utf-8")
            return written

        job = store.create("detect")
        jobs._run_detect(_Profile(), job, "vid999", "Race Part 2", _NoOpLock(), fake_detect_fn)

        assert (not central.exists()
                or "no moments detected" not in central.read_text(encoding="utf-8"))
    finally:
        logsetup.close_logging("ytshorts")


def test_a_failed_log_setup_leaves_the_job_runnable(tmp_path, monkeypatch):
    """Logging is best-effort: if the log cannot be created the job still runs."""
    def boom(_root):
        raise OSError("read-only workspace")

    monkeypatch.setattr(jobs.workspace, "job_logs_dir", boom)
    monkeypatch.setattr(jobs, "_resolve_workspace",
                        type("W", (), {"root": tmp_path}))
    store = jobs.JobStore()
    job = store.create("render")
    assert job.log_path is None
    job.record("clip", "done", None, "done: clip")     # must not raise
    jobs.finish_job_log(job)                            # must not raise


def test_run_detect_zero_moments_summary_counts_zero_not_one(store, tmp_path):
    """`_run_detect` now reads the moment count from the written analysis
    (`len(data["moments"])`), not from the return value of `detect_fn` - it
    used to be `len(names)` on a return value that is a Path, which raised
    TypeError on every real call (see CLAUDE.md's note on this exact
    breakage). This pins that an analysis with zero moments reports the
    summary as 0, not 1 - a stray synthetic placeholder entry would inflate it."""
    class _NoOpLock:
        def release(self):
            pass

    class _Profile:
        config: dict = {}

    def fake_detect_fn(video_id, workspace_root, config, *,
                       stream_title, logger, progress=None):
        written = tmp_path / "moments.json"
        written.write_text('{"engine": "lexicon", "moments": [], '
                           '"missing_windows": []}', encoding="utf-8")
        return written

    job = store.create("detect")
    plain = Path(job.log_path)
    jobs._run_detect(_Profile(), job, "vid-zero", "Race", _NoOpLock(), fake_detect_fn)

    gz = Path(str(plain) + ".gz")
    body = gzip.decompress(gz.read_bytes()).decode("utf-8")
    assert "summary: done (0 moments)" in body


def test_run_detect_exception_summary_also_counts_zero(store, tmp_path):
    """Same miscount on the exception path: the synthetic 'detect' failure
    record must not be counted as a moment either."""
    class _NoOpLock:
        def release(self):
            pass

    class _Profile:
        config: dict = {}

    def boom_detect_fn(*_args, **_kwargs):
        raise RuntimeError("boom")

    job = store.create("detect")
    plain = Path(job.log_path)
    jobs._run_detect(_Profile(), job, "vid-boom", "Race", _NoOpLock(), boom_detect_fn)

    gz = Path(str(plain) + ".gz")
    body = gzip.decompress(gz.read_bytes()).decode("utf-8")
    assert "summary: failed (0 moments)" in body


def test_a_workspace_error_from_the_resolver_leaves_the_job_runnable(tmp_path, monkeypatch):
    """Regression: `_open_job_log`'s first statement calls `_resolve_workspace()`,
    which can raise `workspace.WorkspaceError` (e.g. a renamed/unmounted
    YT_SHORTS_DATA - see workspace.py, "a set-but-missing YT_SHORTS_DATA is
    an error"), not just OSError. `JobStore.create()` runs AFTER the caller
    has already acquired the event lock (see start_render_job/start_detect_job/
    start_upload_job), so an uncaught WorkspaceError here used to leave that
    lock held by the still-live studio process forever - the event refusing
    every render/detect/upload with 409 until the studio was restarted. This
    pins the fix: the job must still be created and runnable, with
    log_path=None, exactly like the pre-existing OSError case above."""
    def boom():
        raise jobs.workspace.WorkspaceError("YT_SHORTS_DATA points at a directory "
                                            "that no longer exists")

    monkeypatch.setattr(jobs, "_resolve_workspace", boom)
    store = jobs.JobStore()
    job = store.create("render")
    assert job.log_path is None
    job.record("clip", "done", None, "done: clip")     # must not raise
    jobs.finish_job_log(job)                            # must not raise
