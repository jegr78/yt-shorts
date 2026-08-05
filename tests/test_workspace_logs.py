"""The workspace's logs/ directory: resolution and creation on demand."""

from yt_shorts import workspace


def test_logs_dir_sits_beside_channels(tmp_path):
    assert workspace.logs_dir(tmp_path) == tmp_path / "logs"


def test_logs_dir_is_created_on_demand(tmp_path):
    created = workspace.logs_dir(tmp_path)
    assert created.is_dir()


def test_job_logs_dir_is_under_logs(tmp_path):
    jobs = workspace.job_logs_dir(tmp_path)
    assert jobs == tmp_path / "logs" / "jobs"
    assert jobs.is_dir()


def test_central_log_name_is_stable():
    assert workspace.CENTRAL_LOG_NAME == "yt-shorts.log"
