"""The studio's log routes: listing, tailing, archives, and their guards."""

import gzip

import pytest
from fastapi.testclient import TestClient

from yt_shorts import workspace
from yt_shorts.studio import api as studio_api
from yt_shorts.studio import jobs as studio_jobs
from yt_shorts.studio.api import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # The brief's fixture patched only workspace.resolve; api.py and jobs.py
    # each bind their own `_resolve_workspace` via `from ..workspace import
    # resolve as _resolve_workspace` at import time (see api.py's and
    # jobs.py's own module comments, and tests/conftest.py's module
    # docstring on the same gotcha), so reassigning the attribute on the
    # `workspace` module afterwards does not reach either of those already-
    # bound names. All three must be patched to the SAME resolver so the
    # app, its job store (which opens each job's own log file under
    # <root>/logs/jobs/ at job-creation time) and any direct `workspace.*`
    # call in this test agree on one root.
    ws = workspace.Workspace(root=tmp_path, channels_dir=tmp_path / "channels",
                             origin="test")

    def _resolve(*_a, **_k):
        return ws

    monkeypatch.setattr(workspace, "resolve", _resolve)
    monkeypatch.setattr(studio_api, "_resolve_workspace", _resolve)
    monkeypatch.setattr(studio_jobs, "_resolve_workspace", _resolve)
    (tmp_path / "channels").mkdir(exist_ok=True)
    logs = workspace.logs_dir(tmp_path)
    (logs / workspace.CENTRAL_LOG_NAME).write_text(
        "2026-07-24 10:00:00 INFO first\n2026-07-24 10:00:01 INFO second\n",
        encoding="utf-8")
    (logs / f"{workspace.CENTRAL_LOG_NAME}.2026-07-23.gz").write_bytes(
        gzip.compress(b"2026-07-23 09:00:00 INFO yesterday\n"))
    jobs_dir = workspace.job_logs_dir(tmp_path)
    (jobs_dir / "detect-abc123.log").write_text(
        "2026-07-24 10:05:00 INFO detected: clip-1\n", encoding="utf-8")
    return TestClient(create_app())


def test_lists_the_central_log_its_archives_and_job_logs(client):
    body = client.get("/api/logs").json()
    assert body["central"]["name"] == "yt-shorts.log"
    assert body["archives"] == ["2026-07-23"]
    assert [entry["name"] for entry in body["jobs"]] == ["detect-abc123.log"]


def test_reads_the_central_log(client):
    body = client.get("/api/logs/yt-shorts.log").json()
    assert body["lines"] == ["2026-07-24 10:00:00 INFO first",
                             "2026-07-24 10:00:01 INFO second"]
    assert body["position"] > 0


def test_tails_from_a_position(client, tmp_path):
    first = client.get("/api/logs/yt-shorts.log").json()
    path = workspace.logs_dir(tmp_path) / workspace.CENTRAL_LOG_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write("2026-07-24 10:00:02 INFO third\n")
    body = client.get(f"/api/logs/yt-shorts.log?after={first['position']}").json()
    assert body["lines"] == ["2026-07-24 10:00:02 INFO third"]


def test_serves_a_gzipped_archive_by_date(client):
    body = client.get("/api/logs/yt-shorts.log?date=2026-07-23").json()
    assert body["lines"] == ["2026-07-23 09:00:00 INFO yesterday"]
    assert body["date"] == "2026-07-23"


def test_reads_a_job_log(client):
    body = client.get("/api/logs/detect-abc123.log").json()
    assert body["lines"] == ["2026-07-24 10:05:00 INFO detected: clip-1"]


@pytest.mark.parametrize("path", [
    # %2F decodes to a real "/" before Starlette's {name} path converter
    # ever sees it, so this never reaches validate_segment at all - the
    # request falls through to the SPA fallback's `startswith("api/")`
    # check and 404s there. Kept to document that the URL shape is still
    # refused, but see /api/logs/%2e%2e below for the case that actually
    # exercises the guard this feature adds.
    "/api/logs/..%2F..%2Fetc%2Fpasswd",
    "/api/logs/.hidden",
    "/api/logs/yt-shorts.log?date=../2026-07-23",
    "/api/logs/yt-shorts.log?date=not-a-date",
    # %2e%2e decodes to the single segment ".." (no slash), so it DOES
    # reach validate_segment and is rejected there with 400 - this is the
    # case that actually pins the intended guard.
    "/api/logs/%2e%2e",
])
def test_refuses_traversal_and_bad_dates(client, path):
    assert client.get(path).status_code in (400, 404)


def test_an_unknown_log_is_404(client):
    assert client.get("/api/logs/nope.log").status_code == 404


def test_a_log_route_never_serves_a_file_outside_logs(client, tmp_path):
    (tmp_path / "auth").mkdir(exist_ok=True)
    (tmp_path / "auth" / "token-secret.json").write_text('{"refresh_token": "x"}',
                                                         encoding="utf-8")
    response = client.get("/api/logs/token-secret.json")
    assert response.status_code == 404
    assert "refresh_token" not in response.text


def test_a_symlinked_log_escaping_the_logs_dir_is_not_served(client, tmp_path):
    # A plain-name candidate (root/name) is accepted on `.is_file()` with no
    # containment check of its own - `.is_file()` follows symlinks. Plant one
    # inside logs/ pointing at a secret outside it, and confirm the new
    # realpath+commonpath guard in _resolve_log refuses to serve it.
    (tmp_path / "auth").mkdir(exist_ok=True)
    secret = tmp_path / "auth" / "secret.json"
    secret.write_text('{"refresh_token": "sneaky"}', encoding="utf-8")
    link = workspace.logs_dir(tmp_path) / "escape.log"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks unavailable in this environment: {error}")
    response = client.get("/api/logs/escape.log")
    assert response.status_code == 404
    assert "sneaky" not in response.text


def test_job_log_route_returns_the_jobs_own_log(client, tmp_path):
    app = client.app
    job = app.state.job_store.create("detect")
    job.record("clip", "done", None, "done: clip")
    for handler in __import__("yt_shorts.studio.jobs", fromlist=["job_logger"]) \
            .job_logger(job).handlers:
        handler.flush()
    body = client.get(f"/api/jobs/{job.id}/log").json()
    assert any("done: clip" in line for line in body["lines"])


def test_job_log_route_404s_on_an_unknown_job(client):
    assert client.get("/api/jobs/deadbeef/log").status_code == 404


def test_a_corrupt_archive_is_404_not_500(client, tmp_path):
    """A truncated/corrupt .gz (e.g. a rollover caught mid-write) raises
    BadGzipFile/EOFError from inside the gzip module - the read-only log
    route must report that as 'not readable' (404), never a 500."""
    logs = workspace.logs_dir(tmp_path)
    (logs / f"{workspace.CENTRAL_LOG_NAME}.2026-07-22.gz").write_bytes(b"not actually gzip data")
    response = client.get(f"/api/logs/{workspace.CENTRAL_LOG_NAME}?date=2026-07-22")
    assert response.status_code == 404


def test_logs_root_workspace_error_degrades_to_empty_listing_not_500(client, monkeypatch):
    """`GET /api/logs` must degrade to an empty listing, not 500, when the
    workspace itself is misconfigured (workspace.py: a set-but-missing
    YT_SHORTS_DATA raises WorkspaceError rather than falling back silently)."""
    def boom():
        raise workspace.WorkspaceError("YT_SHORTS_DATA points at a directory "
                                       "that no longer exists")

    monkeypatch.setattr(studio_api, "_resolve_workspace", boom)
    response = client.get("/api/logs")
    assert response.status_code == 200
    body = response.json()
    assert body["archives"] == [] and body["jobs"] == []


def test_read_log_with_a_broken_workspace_is_404_not_500(client, monkeypatch):
    """Same WorkspaceError guard for the single-log route, via _resolve_log."""
    def boom():
        raise workspace.WorkspaceError("YT_SHORTS_DATA points at a directory "
                                       "that no longer exists")

    monkeypatch.setattr(studio_api, "_resolve_workspace", boom)
    response = client.get(f"/api/logs/{workspace.CENTRAL_LOG_NAME}")
    assert response.status_code == 404
