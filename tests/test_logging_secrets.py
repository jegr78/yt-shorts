"""Secrets must never reach a log, and the log routes must never serve one.

The workspace's auth/ directory (client_secret.json, token-<id>.json,
quota.json) is the one place secrets live; logs/ is a sibling, and every log
route resolves inside logs/ only.
"""

import logging
from pathlib import Path

from yt_shorts import logsetup


class _RecordingHandler(logging.Handler):
    """Captures what one logger emitted, without depending on propagation
    to the root logger: `logsetup.configure_logging` sets `propagate =
    False` on the `ytshorts` tree, so pytest's own caplog is not a
    dependable listener for a project logger once anything in the session
    has configured it."""

    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def test_a_signed_download_url_is_elided_before_it_is_logged(tmp_path):
    """yt-dlp prints googlevideo URLs whose query carries sig/lsig tokens."""
    import io
    path = tmp_path / "pump.log"
    log = logsetup.configure_logging("test.secrets.pump", path, to_stdout=False)
    try:
        url = ("https://rr3---sn-x.googlevideo.com/videoplayback?expire=1&itag=140"
               "&sig=" + "A" * 220 + "&lsig=" + "B" * 80)
        logsetup.pump_subprocess(io.StringIO(f"[download] {url}\n"), log, "yt-dlp")
        for handler in log.handlers:
            handler.flush()
        body = path.read_text(encoding="utf-8")
        assert "sig=" not in body and "AAAA" not in body and "BBBB" not in body
        assert "googlevideo.com" in body        # still diagnosable
    finally:
        logsetup.close_logging("test.secrets.pump")


def test_stream_transcribe_elides_a_signed_url_in_a_chunk_failure_warning(tmp_path, caplog):
    """A chunk decode failure logs the underlying error verbatim (see
    stream_transcribe.py's own comment on recording WHY, not just that,
    ported and pinned again by tests/test_transcribe_logging.py) - and a
    yt-dlp/ffmpeg failure message can carry a signed googlevideo URL. The
    warning must run the error message through shorten_urls before it is
    logged, same idiom test_transcribe_logging.py already uses (caplog at
    the module logger)."""
    from yt_shorts import stream_transcribe

    class _Audio:
        def __init__(self, path, duration):
            self.path = path
            self.duration_seconds = duration

    audio = _Audio(tmp_path / "audio.webm", 1.0)
    url = ("https://rr3---sn-y.googlevideo.com/videoplayback?expire=1&itag=140"
           "&sig=" + "C" * 220 + "&lsig=" + "D" * 80)

    def failing_decoder(_path, _start, _length, glossary=None):
        raise RuntimeError(f"ffmpeg failed: {url}")

    with caplog.at_level(logging.WARNING, logger="ytshorts.transcribe"):
        stream_transcribe.transcribe_stream(
            "vid", tmp_path, downloader=lambda _v, _d: audio,
            decoder=failing_decoder, chunk_seconds=1)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "sig=" not in messages and "CCCC" not in messages and "DDDD" not in messages
    assert "googlevideo.com" in messages


def test_job_record_elides_a_signed_url_in_a_failure_reason(tmp_path, monkeypatch):
    """A job's exception reason (`Job.record`'s `reason`/`line`) can carry a
    yt-dlp RuntimeError(tail[0]) with a signed googlevideo URL straight
    through - see studio/jobs.py's _run_detect/_render_one/upload/connect/copy
    exception handlers, all of which build `reason = f"{type(error).__name__}:
    {error}"`. Job.record must elide it before it reaches the job's own log,
    its snapshot (surfaced to the studio UI), or the central summary line."""
    from yt_shorts.studio import jobs as studio_jobs

    monkeypatch.setattr(studio_jobs, "_resolve_workspace",
                        type("W", (), {"root": tmp_path}))
    store = studio_jobs.JobStore()
    job = store.create("detect")

    url = ("https://rr3---sn-z.googlevideo.com/videoplayback?expire=1&itag=140"
           "&sig=" + "E" * 220 + "&lsig=" + "F" * 80)
    reason = f"RuntimeError: {url}"
    job.record("detect", "failed", reason, f"ERROR: {reason}")

    assert "sig=" not in job.results["detect"].reason
    assert "EEEE" not in job.results["detect"].reason
    assert "googlevideo.com" in job.results["detect"].reason
    for handler in studio_jobs.job_logger(job).handlers:
        handler.flush()
    body = Path(job.log_path).read_text(encoding="utf-8")
    assert "sig=" not in body and "EEEE" not in body


def test_the_worker_elides_a_signed_url_in_an_entry_s_failure_reason(tmp_path,
                                                                     monkeypatch):
    """The third place external-tool text reaches a log (the other two are
    above). `studio.worker.Worker._fail` builds an entry's `reason` from a
    blanket `except Exception` around `start_*_job` - so a yt-dlp or ffmpeg
    RuntimeError raised while the starter does its synchronous work arrives
    here verbatim, signed googlevideo URL and all. That reason is BOTH
    logged and written into `<workspace>/jobs.json`, where it stays until
    the entry ages out, and Task 6's `GET /api/jobs` serves it to the
    browser: three destinations for one string, which is why the elision
    sits in `_fail` (every path into it covered by construction) rather
    than at each call site.
    """
    from yt_shorts.job_queue import JobQueue
    from yt_shorts.studio import jobs as studio_jobs
    from yt_shorts.studio import worker as worker_module

    url = ("https://rr3---sn-w.googlevideo.com/videoplayback?expire=1&itag=140"
           "&sig=" + "G" * 220 + "&lsig=" + "H" * 80)

    def a_starter_that_explodes(*args, **kwargs):
        raise RuntimeError(f"yt-dlp failed: {url}")

    monkeypatch.setattr(studio_jobs, "start_render_job", a_starter_that_explodes)
    queue = JobQueue(tmp_path / "jobs.json", studio_jobs.KINDS, {})
    # Injected, so the elision is asserted on the record the worker itself
    # emits without depending on how the "ytshorts" tree happens to be
    # configured by whatever ran before this test.
    logger = logging.getLogger("test.secrets.worker")
    handler = _RecordingHandler()
    logger.addHandler(handler)
    worker = worker_module.Worker(queue, studio_jobs.JobStore(), logger=logger,
                                  profile_loader=lambda _identifier: None)
    entry = queue.enqueue("render", {"channel": "erf", "event": "an-event"})
    try:
        worker.drain_once()
    finally:
        logger.removeHandler(handler)

    failed = next(e for e in queue.list() if e.id == entry.id)
    assert failed.state == "failed"
    assert "sig=" not in failed.reason and "GGGG" not in failed.reason
    assert "googlevideo.com" in failed.reason        # still diagnosable
    written = (tmp_path / "jobs.json").read_text(encoding="utf-8")
    assert "sig=" not in written and "GGGG" not in written
    logged = " ".join(handler.records)
    assert "sig=" not in logged and "GGGG" not in logged
    assert "googlevideo.com" in logged


def test_the_log_directory_never_contains_auth_material(tmp_path):
    """A structural check: logs_dir and the auth dir are siblings, so a log
    route rooted at logs/ cannot reach a token even by name."""
    from yt_shorts import workspace
    logs = workspace.logs_dir(tmp_path)
    auth = tmp_path / "auth"
    auth.mkdir(exist_ok=True)
    (auth / "token-UC123.json").write_text('{"refresh_token": "secret"}',
                                           encoding="utf-8")
    assert logsetup.resolve_archive(logs, "token-UC123.json", "2026-07-24") is None
    assert not any("token-" in name for name in
                   (p.rsplit("/", 1)[-1] for p in logsetup.list_logs(logs)))


def test_the_jobs_route_never_serves_a_signed_url_in_an_entry_reason(tmp_path,
                                                                     monkeypatch):
    """The last of the three destinations the test above names: the entry's
    `reason` is logged, written into `<workspace>/jobs.json`, AND served to
    the browser by `GET /api/jobs`. The first two are pinned above; this is
    the third, driven through the real route rather than argued from the
    elision that produced the string.

    A hand-written jobs.json is the shape that matters here - the elision in
    `Worker._fail` cannot cover a file some earlier version (or an operator)
    wrote, and the route is the last thing between it and a page.
    """
    import json

    from fastapi.testclient import TestClient

    from yt_shorts.job_queue import JobQueue
    from yt_shorts.studio import jobs as studio_jobs
    from yt_shorts.studio import worker as worker_module
    from yt_shorts.studio.api import create_app

    url = ("https://rr3---sn-v.googlevideo.com/videoplayback?expire=1&itag=140"
           "&sig=" + "J" * 220 + "&lsig=" + "K" * 80)
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"entries": [{
        "id": "planted", "kind": "render",
        "params": {"channel": "erf", "event": "an-event"},
        "state": "failed", "reason": f"RuntimeError: yt-dlp failed: {url}",
        "progress": None, "created_at": 1.0, "after": None, "job_id": None,
    }]}), encoding="utf-8")

    app = create_app()
    queue = JobQueue(path, studio_jobs.KINDS, dict(worker_module.DEFAULT_LIMITS))
    app.state.job_queue = queue
    app.state.worker = worker_module.Worker(queue, app.state.job_store)

    body = TestClient(app).get("/api/jobs")

    assert body.status_code == 200, body.text
    assert "sig=" not in body.text and "JJJJ" not in body.text
    assert "googlevideo.com" in body.json()["finished"][0]["reason"]


def test_a_failed_playlist_reason_is_elided_before_it_reaches_a_browser():
    """The fifth site that writes external-tool text, and the only one whose
    text is RENDERED rather than logged.

    `channel_catalogue` quotes yt-dlp's own message into
    `FailedPlaylist.reason`, which `GET .../streams` serves and StreamPanel
    displays verbatim. Both construction paths are covered - the channel's
    playlist LIST failing (no playlists at all) and one playlist's members
    failing - because they are two separate `FailedPlaylist(...)` calls and
    an elision added to one of them proves nothing about the other.
    """
    import json

    from yt_shorts.youtube import channel_catalogue

    channel = "https://www.youtube.com/channel/UCb3S2oA7lANdg5IS0QtF46w"

    url = ("https://rr5---sn-q.googlevideo.com/videoplayback?expire=1&itag=140"
           "&sig=" + "S" * 200 + "&lsig=" + "T" * 60)

    def runner_failing_the_playlist_list(args):
        if args[-1].endswith("/playlists"):
            raise RuntimeError(f"yt-dlp failed: {url}")
        return ""

    catalogue = channel_catalogue(channel, runner=runner_failing_the_playlist_list)
    reason = catalogue.failed_playlists[0].reason
    assert "sig=" not in reason and "SSSS" not in reason, reason
    assert "googlevideo.com" in reason        # still diagnosable

    playlist_line = json.dumps({"_type": "url", "id": "PLaaa", "title": "A list"})

    def runner_failing_one_playlist(args):
        if args[-1].endswith("/playlists"):
            return playlist_line
        if "list=PLaaa" in args[-1]:
            raise RuntimeError(f"yt-dlp failed: {url}")
        return ""

    catalogue = channel_catalogue(channel, runner=runner_failing_one_playlist)
    reason = catalogue.failed_playlists[0].reason
    assert "sig=" not in reason and "SSSS" not in reason, reason
    assert "googlevideo.com" in reason
