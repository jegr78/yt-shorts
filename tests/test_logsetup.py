"""The logging helper: rotation, gzip archiving, pruning.

Ported from racecast's tests/test_logs.py and extended with the gzip
compression this project adds (racecast keeps plain archives).
"""

import gzip
import logging
import os
import time
from pathlib import Path

import pytest

from yt_shorts import logsetup


@pytest.fixture(autouse=True)
def _close_loggers():
    """Detach this module's handlers after each test: a handler still holding a
    file in a tmp_path would keep the fixture directory alive."""
    created: list[str] = []
    original = logsetup.configure_logging

    def tracking(name, *args, **kwargs):
        created.append(name)
        return original(name, *args, **kwargs)

    logsetup.configure_logging = tracking
    yield
    logsetup.configure_logging = original
    for name in created:
        logsetup.close_logging(name)


def test_writes_a_timestamped_leveled_line(tmp_path):
    path = tmp_path / "logs" / "yt-shorts.log"
    log = logsetup.configure_logging("test.ytshorts.a", path, to_stdout=False)
    log.info("hello world")
    for handler in log.handlers:
        handler.flush()
    line = path.read_text(encoding="utf-8").strip()
    assert line.endswith("INFO hello world"), line
    assert line[:4].isdigit() and line[4] == "-", line  # leading ISO date


def test_no_stdout_handler_when_not_a_tty(tmp_path):
    log = logsetup.configure_logging("test.ytshorts.b", tmp_path / "b.log",
                                     to_stdout=False)
    assert not any(type(h) is logging.StreamHandler for h in log.handlers)


def test_configure_is_idempotent(tmp_path):
    first = logsetup.configure_logging("test.ytshorts.c", tmp_path / "c.log",
                                       to_stdout=False)
    count = len(first.handlers)
    second = logsetup.configure_logging("test.ytshorts.c", tmp_path / "c.log",
                                        to_stdout=False)
    assert first is second and len(second.handlers) == count


def test_rollover_survives_a_failed_rename_without_losing_the_line(tmp_path):
    """A rollover whose rename fails must not drop the record: the handler
    keeps writing to the base file and defers the next attempt."""
    path = tmp_path / "rot.log"
    log = logsetup.configure_logging("test.ytshorts.rot", path, to_stdout=False)
    handler = next(h for h in log.handlers if getattr(h, "_ytshorts", False))
    # In the past, so shouldRollover() is True - but a REALISTIC past, not 1.
    # doRollover names the archive from `rolloverAt - interval`, which for 1
    # is negative, and time.localtime() of a pre-1970 value raises Errno 22 on
    # Windows.
    handler.rolloverAt = int(time.time()) - 1

    def boom(src, dst):
        raise PermissionError(32, "cannot access the file")

    handler.rotate = boom
    log.warning("must survive the failed rollover")
    for h in log.handlers:
        h.flush()
    assert "WARNING must survive the failed rollover" in path.read_text(encoding="utf-8")


def test_rollover_gzips_the_archive_and_removes_the_plain_file(tmp_path):
    path = tmp_path / "yt-shorts.log"
    log = logsetup.configure_logging("test.ytshorts.gz", path, to_stdout=False)
    log.info("first day")
    handler = next(h for h in log.handlers if getattr(h, "_ytshorts", False))
    handler.flush()
    handler.rolloverAt = int(time.time()) - 1
    log.info("second day")     # triggers the rollover
    handler.flush()
    archives = sorted(p.name for p in tmp_path.iterdir() if p.name != "yt-shorts.log")
    assert archives and all(name.endswith(".gz") for name in archives), archives
    body = gzip.decompress((tmp_path / archives[0]).read_bytes()).decode("utf-8")
    assert "first day" in body
    assert "second day" in path.read_text(encoding="utf-8")


def test_compress_file_gzips_and_removes_the_original(tmp_path):
    plain = tmp_path / "detect-abc.log"
    plain.write_text("job output\n", encoding="utf-8", newline="")
    gz = logsetup.compress_file(plain)
    assert gz == str(plain) + ".gz"
    assert not plain.exists()
    assert gzip.decompress(Path(gz).read_bytes()).decode("utf-8") == "job output\n"


def test_compress_file_leaves_the_original_when_it_fails(tmp_path):
    """Best-effort: compression must never lose a log."""
    missing = tmp_path / "not-there.log"
    assert logsetup.compress_file(missing) is None


def test_compress_file_twice_leaves_the_first_gz_intact(tmp_path):
    """A second call, made after the plain file is already gone (exactly what
    a repeat finish_job_log call does), must not destroy the .gz the first,
    successful call produced - the critical regression this guards against."""
    plain = tmp_path / "detect-abc.log"
    plain.write_text("job output\n", encoding="utf-8", newline="")

    first = logsetup.compress_file(plain)
    assert first is not None and not plain.exists()
    gz = Path(first)
    original = gz.read_bytes()

    second = logsetup.compress_file(plain)   # plain is already gone

    assert second is None                     # this call did nothing useful
    assert gz.exists()                        # and, crucially, destroyed nothing
    assert gz.read_bytes() == original
    assert gzip.decompress(gz.read_bytes()).decode("utf-8") == "job output\n"


def test_compress_file_never_destroys_a_pre_existing_target_on_failure(tmp_path):
    """A pre-existing .gz (from an earlier, unrelated successful compress)
    must survive even when THIS call fails outright - `open(path)` raising
    because the plain file never existed at all."""
    plain = tmp_path / "detect-xyz.log"
    target = tmp_path / "detect-xyz.log.gz"
    target.write_bytes(gzip.compress(b"earlier good archive"))

    result = logsetup.compress_file(plain)    # plain never existed -> fails

    assert result is None
    assert target.exists()
    assert gzip.decompress(target.read_bytes()) == b"earlier good archive"


def test_compress_file_never_overwrites_a_pre_existing_archive_on_success(tmp_path):
    """The success-path bug this fix closes: two processes rotating the SAME
    central log across midnight (the CLI and the studio both attach a
    TimedRotatingFileHandler to <workspace>/logs/yt-shorts.log) can each
    produce a plain file with the same dated archive name. Reproduced
    directly: a .gz pre-seeded with one day's archive, then compress_file
    called on a second, unrelated plain file that collides on the same
    target name - the day-one archive must survive completely untouched."""
    plain = tmp_path / "yt-shorts.log.2026-07-23"
    plain.write_text("SECOND ROLLOVER SAME DAY\n", encoding="utf-8", newline="")
    target = tmp_path / "yt-shorts.log.2026-07-23.gz"
    target.write_bytes(gzip.compress(b"DAY-ONE ARCHIVE\n"))

    result = logsetup.compress_file(plain)

    assert result is None
    assert gzip.decompress(target.read_bytes()) == b"DAY-ONE ARCHIVE\n"
    assert plain.exists()   # the collision is left on disk, visible and prunable


def test_prune_removes_only_aged_files_and_recurses_into_jobs(tmp_path):
    logs = tmp_path / "logs"
    (logs / "jobs").mkdir(parents=True)
    fresh = logs / "yt-shorts.log"
    old = logs / "yt-shorts.2026-06-01.gz"
    old_job = logs / "jobs" / "detect-old.log.gz"
    for path in (fresh, old, old_job):
        path.write_text("x", encoding="utf-8")
    now = time.time()
    ancient = now - 40 * 86400
    os.utime(old, (ancient, ancient))
    os.utime(old_job, (ancient, ancient))

    removed = logsetup.prune_old_logs(logs, keep_days=30, now_ts=now)

    assert str(old) in removed and str(old_job) in removed
    assert fresh.exists() and not old.exists() and not old_job.exists()


def test_prune_tolerates_a_missing_directory(tmp_path):
    assert logsetup.prune_old_logs(tmp_path / "nope") == []


def test_read_new_lines_returns_only_complete_lines_and_advances(tmp_path):
    path = tmp_path / "t.log"
    path.write_text("one\ntwo\npart", encoding="utf-8", newline="")
    lines, pos = logsetup.read_new_lines(path, 0)
    assert lines == ["one", "two"]           # the partial trailing line is held back
    path.write_text("one\ntwo\npartial done\n", encoding="utf-8", newline="")
    lines, pos = logsetup.read_new_lines(path, pos)
    assert lines == ["partial done"]


def test_read_new_lines_restarts_after_rotation(tmp_path):
    path = tmp_path / "t.log"
    path.write_text("alpha\nbeta\n", encoding="utf-8", newline="")   # 11 bytes: > len("fresh\n")
    _lines, pos = logsetup.read_new_lines(path, 0)
    path.write_text("fresh\n", encoding="utf-8", newline="")   # rotated: now shorter than pos
    lines, pos = logsetup.read_new_lines(path, pos)
    assert lines == ["fresh"] and pos == len("fresh\n")


def test_read_new_lines_tolerates_a_missing_file(tmp_path):
    assert logsetup.read_new_lines(tmp_path / "gone.log", 7) == ([], 7)


def test_list_logs_is_newest_first_and_absent_dir_is_empty(tmp_path):
    older, newer = tmp_path / "old.log", tmp_path / "new.log"
    older.write_text("x", encoding="utf-8")
    newer.write_text("x", encoding="utf-8")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    assert [Path(p).name for p in logsetup.list_logs(tmp_path)] == ["new.log", "old.log"]
    assert logsetup.list_logs(tmp_path / "nope") == []


def test_archive_dates_are_descending(tmp_path):
    for name in ("yt-shorts.log.2026-07-20.gz", "yt-shorts.log.2026-07-22.gz",
                 "yt-shorts.log", "other.log.2026-07-21.gz"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert logsetup.archive_dates(tmp_path, ["yt-shorts.log"]) == ["2026-07-22", "2026-07-20"]


def test_resolve_archive_finds_a_real_archive(tmp_path):
    target = tmp_path / "yt-shorts.log.2026-07-22.gz"
    target.write_text("x", encoding="utf-8")
    assert logsetup.resolve_archive(tmp_path, "yt-shorts.log", "2026-07-22") == str(target.resolve())


@pytest.mark.parametrize("basename,date", [
    ("yt-shorts.log", "not-a-date"),
    ("yt-shorts.log", "../2026-07-22"),
    ("../../etc/passwd", "2026-07-22"),
    ("sub/dir", "2026-07-22"),
    ("yt-shorts.log", ""),
])
def test_resolve_archive_refuses_traversal_and_bad_dates(tmp_path, basename, date):
    assert logsetup.resolve_archive(tmp_path, basename, date) is None


def test_shorten_urls_elides_a_signed_url_query(tmp_path):
    line = ("[youtube] downloading https://rr3---sn-abc.googlevideo.com/videoplayback"
            "?expire=1&itag=140&sig=" + "S" * 200 + "&lsig=" + "L" * 60)
    out = logsetup.shorten_urls(line)
    assert "sig=" not in out and "SSSS" not in out
    assert "googlevideo.com" in out and "itag 140" in out


def test_classify_subproc_line_levels():
    assert logsetup.classify_subproc_line("ERROR: could not open file") == logging.ERROR
    assert logsetup.classify_subproc_line("retrying in 5s") == logging.WARNING
    assert logsetup.classify_subproc_line("downloading chunk 3") == logging.INFO


def test_line_throttle_collapses_duplicates_and_rate_limits():
    throttle = logsetup.LineThrottle(rate_max=2, window_s=10.0, summary_s=30.0)
    assert throttle.emit(logging.INFO, "same line", 0.0) == [(logging.INFO, "same line")]
    assert throttle.emit(logging.INFO, "same line", 1.0) == []          # duplicate, held
    out = throttle.emit(logging.INFO, "different", 2.0)
    assert any("repeated" in text for _level, text in out)
    assert any(text == "different" for _level, text in out)


def test_pump_subprocess_logs_each_line_tagged(tmp_path):
    import io
    path = tmp_path / "pump.log"
    log = logsetup.configure_logging("test.ytshorts.pump", path, to_stdout=False)
    logsetup.pump_subprocess(io.StringIO("downloading\nERROR: boom\n"), log, "yt-dlp")
    for handler in log.handlers:
        handler.flush()
    body = path.read_text(encoding="utf-8")
    assert "INFO [yt-dlp] downloading" in body
    assert "ERROR [yt-dlp] ERROR: boom" in body


def test_pump_subprocess_survives_a_failing_observer(tmp_path):
    """A broken on_line callback must never break the pump thread."""
    import io
    path = tmp_path / "pump2.log"
    log = logsetup.configure_logging("test.ytshorts.pump2", path, to_stdout=False)

    def boom(_line):
        raise RuntimeError("observer exploded")

    logsetup.pump_subprocess(io.StringIO("still logged\n"), log, "ffmpeg", on_line=boom)
    for handler in log.handlers:
        handler.flush()
    assert "[ffmpeg] still logged" in path.read_text(encoding="utf-8")
