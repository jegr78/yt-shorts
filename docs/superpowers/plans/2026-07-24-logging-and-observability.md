# Logging & Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the tool a complete, rotating, gzip-compressed logging subsystem written to `<workspace>/logs/` — a central app log plus one log per heavy background job — viewable in the studio, so a failed run (like the silent transcription failure that produced zero moments) is diagnosable.

**Architecture:** A new stdlib-only `yt_shorts/logsetup.py`, ported from racecast's `src/scripts/logsetup.py`, provides rotation (daily, resilient rollover), gzip archiving, age-pruning, tailing, traversal-guarded log-surface helpers and a classifying/throttling subprocess pump. `workspace.py` resolves `<workspace>/logs/`. The CLI, the studio app and every background job attach to it; `stream_transcribe`/`detect` stop swallowing failures. Studio routes over the same helpers back a new top-level **Logs** screen plus a per-job "View log" link.

**Tech Stack:** Python 3 stdlib (`logging`, `logging.handlers.TimedRotatingFileHandler`, `gzip`, `shutil`, `subprocess`), FastAPI (studio routes), React + Mantine + TypeScript + Vite (studio frontend), pytest + Playwright (backend/E2E), Vitest (frontend units).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Suite: `PYTHONPATH=src .venv/bin/pytest -q`. Linter: `python3 tools/lint.py` (must print `All checks passed!`). Frontend, in `src/yt_shorts/studio/web`: `npx tsc -b` (0 errors), `npm run lint` (oxlint, clean), `npm test` (Vitest, all pass), `npm run build` (regenerates the COMMITTED `src/yt_shorts/studio/static/`).
- **`logsetup.py` is stdlib-only.** No project imports, no FastAPI, no google — the CLI must import it in a venv that never installed them (same rule as `pathnames.py`/`upload_policy.py`).
- **Logging is best-effort and must never abort a run.** Any logging, rotation or compression failure warns and continues. This preserves "one failed clip must never abort a run".
- **Secrets never reach a log.** `client_secret.json`, `token-<id>.json`, `quota.json` contents are never logged. Signed URLs (yt-dlp googlevideo `sig`/`lsig` tokens) are elided by `shorten_urls`.
- **Log retention is 30 days**, and `prune_old_logs` is the ONLY deletion authority (handlers use `backupCount=0` and never delete).
- **Log file layout** (exact):
  - `<workspace>/logs/yt-shorts.log`, rotated to `yt-shorts.<YYYY-MM-DD>` then gzipped to `yt-shorts.<YYYY-MM-DD>.gz`
  - `<workspace>/logs/jobs/<kind>-<jobid>.log`, gzipped to `<kind>-<jobid>.log.gz` when the job reaches a terminal state
  - `<kind>` is exactly one of: `render`, `detect`, `upload`, `connect`, `copy`
- **Log line format:** `%(asctime)s %(levelname)s %(message)s` with `datefmt="%Y-%m-%d %H:%M:%S"`.
- **No pipeline timeout changes.** The documented "Unbounded decode" decision and `stream_transcribe`'s per-chunk subprocess timeout stay exactly as they are; logging observes, it does not re-time.
- **Every studio path segment is validated before any filesystem touch** (`pathnames.validate_segment` for names; `logsetup.resolve_archive` for dated archives). A `/api/*` path never falls through to the SPA.
- **Frontend purity rule:** pure logic lives in non-component `.ts` modules with Vitest tests, so Vite's fast-refresh boundary stays component-only.
- The test suite must pass identically whether `~/YT-Shorts-Data` exists or not; never point a test at the real workspace (see `tests/conftest.py`).
- Source of the port: `<racecast-repo>/src/scripts/logsetup.py` (read-only reference, like `<racecast-runtime>/`).

---

### Task 1: `logsetup.py` — rotation, gzip, prune

**Files:**
- Create: `src/yt_shorts/logsetup.py`
- Create: `tests/test_logsetup.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces:
  - `configure_logging(name: str, log_path, *, level: int = logging.INFO, to_stdout: bool | None = None) -> logging.Logger`
  - `close_logging(name: str) -> None`
  - `compress_file(path) -> str | None` (returns the `.gz` path, or `None` on best-effort failure)
  - `prune_old_logs(log_dir, keep_days: int = 30, now_ts: float | None = None) -> list[str]`
  - `DEFAULT_RETENTION_DAYS = 30`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_logsetup.py`:

```python
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
    handler.rolloverAt = 1  # in the past -> shouldRollover() is True

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
    handler.rolloverAt = 1
    log.info("second day")     # triggers the rollover
    handler.flush()
    archives = sorted(p.name for p in tmp_path.iterdir() if p.name != "yt-shorts.log")
    assert archives and all(name.endswith(".gz") for name in archives), archives
    body = gzip.decompress((tmp_path / archives[0]).read_bytes()).decode("utf-8")
    assert "first day" in body
    assert "second day" in path.read_text(encoding="utf-8")


def test_compress_file_gzips_and_removes_the_original(tmp_path):
    plain = tmp_path / "detect-abc.log"
    plain.write_text("job output\n", encoding="utf-8")
    gz = logsetup.compress_file(plain)
    assert gz == str(plain) + ".gz"
    assert not plain.exists()
    assert gzip.decompress(Path(gz).read_bytes()).decode("utf-8") == "job output\n"


def test_compress_file_leaves_the_original_when_it_fails(tmp_path):
    """Best-effort: compression must never lose a log."""
    missing = tmp_path / "not-there.log"
    assert logsetup.compress_file(missing) is None


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_logsetup.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.logsetup'`.

- [ ] **Step 3: Write `src/yt_shorts/logsetup.py`**

Port from `<racecast-repo>/src/scripts/logsetup.py` — READ that file first; keep its structure and its explanatory docstrings (they record why each guard exists), adapting names to this project. This step is the rotation/gzip/prune half:

```python
"""Rotating, gzip-archived logging for the CLI, the studio and its jobs.

Stdlib-only by design - DO NOT import anything from this project here: the CLI
must configure logging in a venv that never installed FastAPI or google (same
rule as pathnames.py and upload_policy.py). Ported from the racecast broadcast
project's src/scripts/logsetup.py, which proved these guards in production;
what this adds is gzip compression of rotated archives (racecast keeps them
plain) and a 30-day default retention, because a batch tool's logs are read
days after the run, not live.

Every path here is best-effort: a failed rotation, a failed compression or a
failed prune warns and carries on. Logging must never be the reason a render
or a job dies (see CLAUDE.md, "One failed clip must never abort a run").
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sys
import time
from logging.handlers import TimedRotatingFileHandler

DEFAULT_RETENTION_DAYS = 30

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def compress_file(path) -> str | None:
    """Gzip `path` to `path + '.gz'` and remove the original, returning the new
    path - or None if anything went wrong, leaving the original in place. A log
    that could not be compressed is still a log; losing it to a failed
    compression would defeat the point of writing it."""
    path = str(path)
    target = path + ".gz"
    try:
        with open(path, "rb") as plain, gzip.open(target, "wb") as packed:
            shutil.copyfileobj(plain, packed)
        os.remove(path)
        return target
    except OSError:
        try:
            if os.path.exists(target):
                os.remove(target)          # a half-written .gz is worse than none
        except OSError:
            pass
        return None


class _ResilientTimedRotatingFileHandler(TimedRotatingFileHandler):
    """A TimedRotatingFileHandler whose rollover never drops a log record and
    whose archive is gzipped.

    A failed rollover (the rename raising, e.g. because another process holds
    the file) would let the exception escape emit() in the stock handler, and
    the line would be lost to stderr instead of reaching the log. Here a failed
    rollover re-opens the still-present base file (so the in-flight record is
    written) and pushes the next attempt forward one interval, so we do not
    re-attempt - and re-fail - on every subsequent emit."""

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()
            now = int(time.time())
            while self.rolloverAt <= now:
                self.rolloverAt += self.interval
            return
        self._compress_archives()

    def _compress_archives(self) -> None:
        # getFilesToDelete() lists the rotated siblings; with backupCount=0 the
        # stock handler deletes none of them (prune_old_logs is the sole
        # deletion authority), so we walk the directory ourselves and gzip any
        # plain archive left behind.
        directory = os.path.dirname(self.baseFilename) or "."
        base = os.path.basename(self.baseFilename)
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if name == base or not name.startswith(base + ".") or name.endswith(".gz"):
                continue
            compress_file(os.path.join(directory, name))


def configure_logging(name, log_path, *, level=logging.INFO,
                      to_stdout=None) -> logging.Logger:
    """A Logger writing timestamped, leveled lines to log_path with daily
    midnight rotation (archive suffix `.YYYY-MM-DD`, then gzipped).

    backupCount=0 -> the handler never deletes; prune_old_logs is the sole
    deletion authority. A stdout StreamHandler is added only on a TTY (a
    foreground CLI run), so a server whose stdout is redirected never
    double-writes. Idempotent per `name`."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if any(getattr(h, "_ytshorts", False) for h in logger.handlers):
        return logger
    directory = os.path.dirname(str(log_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    handler = _ResilientTimedRotatingFileHandler(
        str(log_path), when="midnight", backupCount=0, encoding="utf-8", delay=True)
    handler.setFormatter(formatter)
    handler._ytshorts = True
    logger.addHandler(handler)
    on_tty = sys.stdout.isatty() if to_stdout is None else to_stdout
    if on_tty:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        stream._ytshorts = True
        logger.addHandler(stream)
    return logger


def close_logging(name) -> None:
    """Close and detach the handlers this module attached to `name`. A test
    pointing a logger at a temporary directory must call this before the
    directory is removed."""
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        if getattr(handler, "_ytshorts", False):
            handler.close()
            logger.removeHandler(handler)


def prune_old_logs(log_dir, keep_days: int = DEFAULT_RETENTION_DAYS,
                   now_ts: float | None = None) -> list[str]:
    """Delete files under log_dir (including its `jobs/` subdirectory) whose
    mtime is older than keep_days; return the removed paths, sorted.

    The ONLY log-deletion path in the project. `now_ts` is injectable for
    deterministic tests. Best-effort: an unreadable directory or a file that
    vanishes between listing and removal is skipped, never raised."""
    now_ts = time.time() if now_ts is None else now_ts
    cutoff = now_ts - keep_days * 86400
    removed: list[str] = []
    for root, _dirs, files in os.walk(str(log_dir)):
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed.append(path)
            except OSError:
                continue  # vanished or unreadable between walk and remove
    return sorted(removed)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_logsetup.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/logsetup.py tests/test_logsetup.py
git commit -m "feat(logging): rotating, gzip-archived logger with 30-day pruning"
```

---

### Task 2: `logsetup.py` — tail, log surface, subprocess pump

**Files:**
- Modify: `src/yt_shorts/logsetup.py`
- Modify: `tests/test_logsetup.py`

**Interfaces:**
- Consumes: Task 1's module.
- Produces:
  - `read_new_lines(path, pos: int) -> tuple[list[str], int]`
  - `list_logs(log_dir) -> list[str]` (regular files, newest-first; `[]` if absent)
  - `newest_log(log_dir) -> str | None`
  - `archive_dates(log_dir, basenames) -> list[str]` (descending `YYYY-MM-DD`)
  - `resolve_archive(log_dir, basename: str, date: str) -> str | None` (traversal-guarded)
  - `shorten_urls(text: str, max_len: int = 120) -> str`
  - `normalize_for_dedup(text: str) -> str`
  - `classify_subproc_line(line: str) -> int` (a `logging` level)
  - `LineThrottle` with `emit(level, text, now) -> list[tuple[int, str]]` and `flush(now) -> list[tuple[int, str]]`
  - `pump_subprocess(stream, logger, tag: str, on_line=None, now=time.monotonic) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_logsetup.py`:

```python
def test_read_new_lines_returns_only_complete_lines_and_advances(tmp_path):
    path = tmp_path / "t.log"
    path.write_text("one\ntwo\npart", encoding="utf-8")
    lines, pos = logsetup.read_new_lines(path, 0)
    assert lines == ["one", "two"]           # the partial trailing line is held back
    path.write_text("one\ntwo\npartial done\n", encoding="utf-8")
    lines, pos = logsetup.read_new_lines(path, pos)
    assert lines == ["partial done"]


def test_read_new_lines_restarts_after_rotation(tmp_path):
    path = tmp_path / "t.log"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    _lines, pos = logsetup.read_new_lines(path, 0)
    path.write_text("fresh\n", encoding="utf-8")   # rotated: now shorter than pos
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
    for name in ("yt-shorts.2026-07-20.gz", "yt-shorts.2026-07-22.gz",
                 "yt-shorts.log", "other.2026-07-21.gz"):
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_logsetup.py -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.logsetup' has no attribute 'read_new_lines'`.

- [ ] **Step 3: Append the implementation to `src/yt_shorts/logsetup.py`**

Port these from racecast's `logsetup.py` — read it and carry over `read_new_lines`, `list_logs`, `newest_log`, `archive_dates`, `resolve_archive`, `shorten_urls`, `normalize_for_dedup`, `classify_subproc_line`, `LineThrottle`, `pump_subprocess` and `tag_line` with their docstrings intact. Two adaptations are required:

1. `resolve_archive` must accept this project's archive names. Racecast resolves `<basename>.<date>`; here the central log's archives are gzipped, so the resolver tries `<basename>.<date>.gz` FIRST and falls back to the plain `<basename>.<date>` (a rotation whose compression failed). Both candidates go through the same realpath containment check.

2. `archive_dates` must match a `.gz` suffix as well: the pattern becomes
   `re.fullmatch(re.escape(base) + r"\.(\d{4}-\d{2}-\d{2})(?:\.gz)?", name)`.

Add the module-level constants racecast defines: `URL_SHORTEN_MAX = 120`, `_URL_RE`, `_ITAG_RE`, `_DIGITS_RE`, `_ERROR_HINTS`, `_WARN_HINTS`, `LINE_THROTTLE_RATE_MAX = 30`, `LINE_THROTTLE_WINDOW_S = 10.0`, `LINE_THROTTLE_SUMMARY_S = 30.0`, and `import re`.

The `resolve_archive` adaptation, written out (the rest is a faithful port):

```python
def resolve_archive(log_dir, basename, date) -> str | None:
    """Realpath of the rotated archive `<basename>.<date>[.gz]` inside log_dir,
    or None. The gzipped form is tried first (that is what a successful
    rollover leaves); the plain form is the fallback for a rotation whose
    compression failed.

    Guards traversal, because `basename` and `date` arrive from an HTTP path
    or query: `date` must be exactly YYYY-MM-DD, `basename` must carry no path
    separator, and the resolved path must stay inside log_dir."""
    if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return None
    if not basename or "/" in basename or "\\" in basename or os.sep in basename:
        return None
    root = os.path.realpath(str(log_dir))
    for candidate in (f"{basename}.{date}.gz", f"{basename}.{date}"):
        full = os.path.realpath(os.path.join(root, candidate))
        try:
            inside = os.path.commonpath([root, full]) == root
        except ValueError:
            continue
        if inside and os.path.isfile(full):
            return full
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_logsetup.py -q`
Expected: PASS (all, ~22 tests).

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/logsetup.py tests/test_logsetup.py
git commit -m "feat(logging): tailing, traversal-guarded log surface, subprocess pump"
```

---

### Task 3: `<workspace>/logs/` resolution and app-level wiring

**Files:**
- Modify: `src/yt_shorts/workspace.py`
- Modify: `bin/yt-shorts`
- Modify: `src/yt_shorts/studio/api.py` (the module-level `_logger` at line 305 and `create_app`)
- Create: `tests/test_workspace_logs.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `logsetup.configure_logging`, `logsetup.prune_old_logs`.
- Produces:
  - `workspace.logs_dir(root) -> Path` — `<root>/logs`, created on demand
  - `workspace.job_logs_dir(root) -> Path` — `<root>/logs/jobs`, created on demand
  - `workspace.CENTRAL_LOG_NAME = "yt-shorts.log"`
  - `logging.getLogger("ytshorts")` is the configured central logger; every module logs to `logging.getLogger("ytshorts.<area>")` so records propagate to it.

**Design note:** `configure_logging` sets `propagate = False` on the logger it configures. Configure the ROOT-of-our-tree logger `"ytshorts"`, then have callers use children (`"ytshorts.cli"`, `"ytshorts.studio"`, `"ytshorts.transcribe"`, `"ytshorts.detect"`): a child propagates UP to `"ytshorts"`, whose handler writes the file, and `propagate=False` on `"ytshorts"` stops it leaking into the root logger. Per-job loggers (Task 4) are configured separately by name and do NOT sit under `"ytshorts"` — they must not double-write into the central log.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workspace_logs.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspace_logs.py -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.workspace' has no attribute 'logs_dir'`.

- [ ] **Step 3: Implement the resolvers and wire the apps**

In `src/yt_shorts/workspace.py`, after the existing constants:

```python
CENTRAL_LOG_NAME = "yt-shorts.log"


def logs_dir(root) -> Path:
    """The workspace's log directory, created on demand.

    Logs are workspace data, not repository data - they sit beside auth/ and
    streams/ so they are per-operator, backed up with the rest of the
    workspace, and readable by the studio (see logsetup.py)."""
    path = Path(root) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_logs_dir(root) -> Path:
    """Where a background job's own log file lives (see studio/jobs.py)."""
    path = logs_dir(root) / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path
```

In `bin/yt-shorts`, inside `main()` right after `space = workspace.resolve()` succeeds (around line 555) and BEFORE any command dispatch:

```python
    # Every command writes to the workspace's central log, and echoes to the
    # console on a TTY. Pruning runs once per invocation: it is the only
    # deletion authority (see logsetup.prune_old_logs) and a CLI run is the
    # natural, low-frequency moment to do it. Both are best-effort - a tool
    # that refuses to render because it could not open a log file would be
    # worse than one that renders without logging.
    try:
        log_dir = workspace.logs_dir(space.root)
        logsetup.configure_logging("ytshorts", log_dir / workspace.CENTRAL_LOG_NAME)
        logsetup.prune_old_logs(log_dir)
        logging.getLogger("ytshorts.cli").info("%s (%s)", " ".join(sys.argv[1:]),
                                               space.describe())
    except OSError as error:
        print(f"WARNING: logging unavailable: {error}", file=sys.stderr)
```

Add `import logging` and `from yt_shorts import logsetup` to the CLI's imports (the existing `from yt_shorts import workspace` line at 16 shows the pattern — `logsetup` is stdlib-only, so it is safe to import at module scope alongside it).

In `src/yt_shorts/studio/api.py`, replace the bare `_logger = logging.getLogger(__name__)` at line 305 with `_logger = logging.getLogger("ytshorts.studio")`, and inside `create_app()` (before the routes are registered) configure the central logger the same way:

```python
    # The studio server logs into the same central log the CLI writes, so one
    # file carries the whole session. Per-job logs are separate files (see
    # studio/jobs.py) - a one-hour transcription must not bury the app log.
    try:
        _log_dir = _workspace_logs_dir()          # workspace.logs_dir(resolve().root)
        logsetup.configure_logging("ytshorts", _log_dir / workspace.CENTRAL_LOG_NAME,
                                   to_stdout=False)
        logsetup.prune_old_logs(_log_dir)
    except OSError as error:
        _logger.warning("logging unavailable: %s", error)
```

Add `.gitignore` entries if the repository-fallback workspace is in play:

```
logs/
*.log
*.log.gz
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspace_logs.py -q`
Expected: PASS (4 tests).

Then the full suite, to prove the CLI/studio wiring broke nothing:
Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS (all).

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/workspace.py bin/yt-shorts src/yt_shorts/studio/api.py tests/test_workspace_logs.py .gitignore
git commit -m "feat(logging): resolve <workspace>/logs, wire the CLI and studio to the central log"
```

---

### Task 4: Per-job logs

**Files:**
- Modify: `src/yt_shorts/studio/jobs.py`
- Modify: `src/yt_shorts/studio/api.py` (job snapshot exposure)
- Create: `tests/test_job_logging.py`

**Interfaces:**
- Consumes: `logsetup.configure_logging`, `logsetup.close_logging`, `logsetup.compress_file`, `workspace.job_logs_dir`.
- Produces:
  - `Job.__init__(self, job_id: str, kind: str = "job", log_path: str | None = None)`
  - `Job.kind: str`, `Job.log_path: str | None`
  - `Job.snapshot()` gains `"kind"` and `"log_name"` (the log file's BASENAME, never a full path — the client asks for it by name and the server resolves it inside `logs/jobs/`)
  - `jobs.job_logger(job) -> logging.Logger` — the job's own logger
  - `jobs.finish_job_log(job) -> None` — closes and gzips the job log (idempotent)
  - `JobStore.create(kind: str = "job") -> Job` — creates the job AND its log file

**Design notes for the implementer:**
- Every `start_*_job` passes its kind: `start_render_job` → `"render"`, `start_detect_job` → `"detect"`, `start_upload_job` → `"upload"`, `start_connect_job` → `"connect"`, `start_copy_job` → `"copy"`.
- The log path is `workspace.job_logs_dir(resolve().root) / f"{kind}-{job.id}.log"`. `JobStore` must not import FastAPI (it does not today); `workspace.resolve` is already imported there as `_resolve_workspace`.
- The per-job logger name is `f"ytshorts.job.{job.id}"`. **It must not propagate into the central log**, and `configure_logging` already sets `propagate = False` on the logger it configures — but `"ytshorts.job.x"` is a CHILD of `"ytshorts"`, so setting `propagate=False` on the child is exactly what stops the double-write. Verify this in the test below.
- Each `_run_*` function logs `"start"`, its per-item records, and a final summary line; the `finally` block calls `finish_job_log(job)` AFTER `job.finish(...)`, so the gzip happens once the job is terminal.
- One summary line per job also goes to the central logger (`logging.getLogger("ytshorts.studio")`): `"job %s (%s) %s"` with id, kind, terminal status.
- `Job.record` additionally writes its `line` to the job log at INFO (or ERROR when `status == "failed"`), so the job log and `Job.log` carry the same narrative.
- Creating the log must be best-effort: if it fails, `log_path` stays `None`, `job_logger` returns a null logger, and the job runs exactly as before.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_job_logging.py`:

```python
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
                        lambda: type("W", (), {"root": tmp_path})())
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
    jobs.finish_job_log(job)
    jobs.finish_job_log(job)              # must not raise on the already-gzipped log


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
        assert "detected: x" not in central.read_text(encoding="utf-8")
    finally:
        logsetup.close_logging("ytshorts")


def test_a_failed_log_setup_leaves_the_job_runnable(tmp_path, monkeypatch):
    """Logging is best-effort: if the log cannot be created the job still runs."""
    def boom(_root):
        raise OSError("read-only workspace")

    monkeypatch.setattr(jobs.workspace, "job_logs_dir", boom)
    monkeypatch.setattr(jobs, "_resolve_workspace",
                        lambda: type("W", (), {"root": tmp_path})())
    store = jobs.JobStore()
    job = store.create("render")
    assert job.log_path is None
    job.record("clip", "done", None, "done: clip")     # must not raise
    jobs.finish_job_log(job)                            # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_job_logging.py -q`
Expected: FAIL — `TypeError: create() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Implement**

In `src/yt_shorts/studio/jobs.py`, add `import logging` and `from .. import logsetup, workspace`, then:

```python
_NULL_LOGGER = logging.getLogger("ytshorts.job.disabled")
_NULL_LOGGER.addHandler(logging.NullHandler())
_NULL_LOGGER.propagate = False


def _open_job_log(job_id: str, kind: str) -> str | None:
    """Create this job's own log file and return its path, or None if it could
    not be created. Best-effort by contract: a job that cannot be logged still
    runs (see CLAUDE.md - logging must never abort a run)."""
    try:
        directory = workspace.job_logs_dir(_resolve_workspace().root)
        path = directory / f"{kind}-{job_id}.log"
        logger = logsetup.configure_logging(f"ytshorts.job.{job_id}", path,
                                            to_stdout=False)
        logger.propagate = False   # never double-write into the central log
        return str(path)
    except OSError:
        return None


def job_logger(job: "Job") -> logging.Logger:
    """The job's own logger - a null logger when its log could not be opened."""
    if job.log_path is None:
        return _NULL_LOGGER
    return logging.getLogger(f"ytshorts.job.{job.id}")


def finish_job_log(job: "Job") -> None:
    """Close and gzip a finished job's log. Idempotent: a second call on an
    already-compressed log is a no-op. Best-effort, like every logging path."""
    if job.log_path is None:
        return
    logsetup.close_logging(f"ytshorts.job.{job.id}")
    logsetup.compress_file(job.log_path)   # returns None if already gone
```

`Job.__init__` takes `kind` and `log_path`; `record` also writes to the job log:

```python
    def record(self, name: str, status: str, reason: str | None, line: str) -> None:
        with self._lock:
            self.results[name] = ClipResult(status=status, reason=reason)
            self.log.append(line)
        level = logging.ERROR if status == "failed" else logging.INFO
        job_logger(self).log(level, "%s", line)
```

`snapshot()` adds:

```python
                "kind": self.kind,
                "log_name": (Path(self.log_path).name
                             if self.log_path is not None else None),
```

`JobStore.create` becomes:

```python
    def create(self, kind: str = "job") -> Job:
        job_id = uuid.uuid4().hex
        job = Job(job_id, kind=kind, log_path=_open_job_log(job_id, kind))
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()
        return job
```

Update every `job_store.create()` call site to pass its kind, and every `_run_*`'s `finally` block to call `finish_job_log(job)` after the status is set. In `_run_detect` and `_run`, log a start line and a summary through `job_logger(job)`, and one line to `logging.getLogger("ytshorts.studio")` when the job reaches its terminal status.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_job_logging.py -q`
Expected: PASS (7 tests).

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS (all — the existing job/API tests must still pass with the new snapshot keys).

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/studio/jobs.py src/yt_shorts/studio/api.py tests/test_job_logging.py
git commit -m "feat(logging): one log file per background job, gzipped on completion"
```

---

### Task 5: End the silent failures in transcription and detection

**Files:**
- Modify: `src/yt_shorts/stream_transcribe.py`
- Modify: `src/yt_shorts/detect.py`
- Modify: `src/yt_shorts/studio/jobs.py` (`_run_detect`'s result message)
- Create: `tests/test_transcribe_logging.py`

**Interfaces:**
- Consumes: `logging.getLogger("ytshorts.transcribe")`, `logging.getLogger("ytshorts.detect")`, `logsetup.pump_subprocess`.
- Produces: no new public functions. `detect.detect_moments` keeps its signature and return type (`list[str]`).

**This is the task that fixes the operator's actual failure.** `stream_transcribe`'s per-chunk handler currently reads:

```python
        except Exception:
            missing.append(index)
            continue
```

A chunk failure is recorded as an index and the cause is destroyed. That is why a detection run over stream `V9nVNEQNdR4` produced `words: []`, `missing_chunks: [0..9]` and no explanation anywhere.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcribe_logging.py`:

```python
"""A failing chunk decode must say WHY, and an empty transcript must be loud.

The regression this pins: stream_transcribe used to record a failed chunk as a
bare index (`except Exception: missing.append(index)`), so a run that decoded
nothing left no trace of the cause anywhere.
"""

import logging
from pathlib import Path

import pytest

from yt_shorts import detect, stream_transcribe


class _Audio:
    def __init__(self, path, duration):
        self.path = path
        self.duration_seconds = duration


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.webm"
    path.write_bytes(b"not really audio")
    return _Audio(path, 1200.0)      # two 600 s chunks


def test_a_failing_chunk_logs_the_real_cause(tmp_path, audio, caplog):
    def failing_decoder(_path, start, _length):
        raise RuntimeError(f"model exploded at {start}")

    with caplog.at_level(logging.WARNING, logger="ytshorts.transcribe"):
        transcript = stream_transcribe.transcribe_stream(
            "vid", tmp_path,
            downloader=lambda _v, _d: audio,
            decoder=failing_decoder)

    assert transcript.missing_chunks == [0, 1]
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "model exploded" in messages          # the cause survived
    assert "RuntimeError" in messages
    assert "0" in messages and "1" in messages   # both chunk indices named


def test_an_all_empty_transcript_is_logged_as_a_warning(tmp_path, audio, caplog):
    with caplog.at_level(logging.WARNING, logger="ytshorts.transcribe"):
        stream_transcribe.transcribe_stream(
            "vid", tmp_path,
            downloader=lambda _v, _d: audio,
            decoder=lambda _p, _s, _l: [])

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "0 words" in messages or "no words" in messages


def test_a_successful_run_logs_a_summary(tmp_path, audio, caplog):
    def decoder(_path, start, _length):
        return [{"start": 1.0, "end": 1.5, "text": "hello"}]

    with caplog.at_level(logging.INFO, logger="ytshorts.transcribe"):
        stream_transcribe.transcribe_stream(
            "vid", tmp_path, downloader=lambda _v, _d: audio, decoder=decoder)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "2" in messages and "words" in messages       # chunks decoded, word count


def test_detect_logs_its_counts_and_warns_on_an_empty_transcript(tmp_path, caplog):
    """Zero moments from an empty transcript is a failure to report, not a
    quiet success - that is exactly what looked like 'detection found nothing'."""
    class _Transcript:
        words: list = []
        audio_path = tmp_path / "audio.webm"
        missing_chunks = [0, 1, 2]

    config = {"lexicon": __import__("yt_shorts.lexicon", fromlist=["EMPTY"]).EMPTY,
              "detect": {}}
    event_dir = tmp_path / "event"
    event_dir.mkdir()

    with caplog.at_level(logging.WARNING, logger="ytshorts.detect"):
        names = detect.detect_moments(
            "vid", tmp_path, event_dir, config, stream_title="Race",
            transcriber=lambda _v, _w: _Transcript(),
            measure_loudness=lambda _p, _s, _e: 0.0)

    assert names == []
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "empty" in messages.lower() or "0 words" in messages
    assert "3" in messages          # the missing-chunk count is named
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_transcribe_logging.py -q`
Expected: FAIL — the assertions on `caplog` find no records (nothing logs today).

- [ ] **Step 3: Implement**

In `src/yt_shorts/stream_transcribe.py`, add `import logging` and a module logger `_logger = logging.getLogger("ytshorts.transcribe")`, then replace the swallowing handler:

```python
        try:
            chunk_words = offset_words(decoder(audio.path, start, length), start)
        except Exception as error:  # noqa: BLE001 - a bad chunk leaves a gap, never aborts the run
            # Record WHY, not just that. A bare `missing.append(index)` is what
            # made a whole stream decode to zero words with no explanation
            # anywhere - the failure this logging exists to end.
            _logger.warning("chunk %d (%.0fs-%.0fs) failed: %s: %s",
                            index, start, start + length,
                            type(error).__name__, error)
            missing.append(index)
            continue
```

After the loop, before writing `transcript.json`:

```python
    decoded = len(chunk_windows(audio.duration_seconds, chunk_seconds)) - len(missing)
    if not words:
        _logger.warning(
            "%s: transcript is EMPTY - 0 words from %d chunk(s), %d failed: %s",
            video_id, decoded, len(missing), missing)
    else:
        _logger.info("%s: decoded %d chunk(s), %d words, %d missing %s",
                     video_id, decoded, len(words), len(missing), missing or "")
```

In `subprocess_decoder`, stream the decode worker's stderr through the pump instead of discarding it, so a decode failure's own message reaches the log. Keep `run_with_timeout`'s timeout semantics exactly as they are (the plan does not change any timeout); the pump only consumes the stderr that `run_with_timeout` already captures — log it at the point the failure is raised:

```python
        out = run_with_timeout(
            [sys.executable, "-m", "yt_shorts._decode_worker", str(wav), model_name],
            timeout=CHUNK_TIMEOUT_SECONDS)
```

`run_with_timeout` already surfaces the worker's stderr tail in its exception message; the new `_logger.warning` above prints that message, so no change to the timeout path is needed. Add one INFO line before the decode so a long chunk is visible while it runs:

```python
    _logger.info("decoding chunk at %.0fs (%.0fs) with model %s", start, length, model_name)
```

In `src/yt_shorts/detect.py`, add `import logging`, `_logger = logging.getLogger("ytshorts.detect")`, and log around the existing flow:

```python
    transcript = transcriber(video_id, workspace_dir)
    words = transcript.words
    missing = list(getattr(transcript, "missing_chunks", []) or [])
    if not words:
        _logger.warning(
            "%s: no moments possible - the transcript has 0 words (%d chunk(s) failed: %s). "
            "See the transcription log for the cause.",
            video_id, len(missing), missing)
        return []
    if missing:
        _logger.warning("%s: %d chunk(s) missing from the transcript: %s - "
                        "moments in those windows cannot be found",
                        video_id, len(missing), missing)
```

and after ranking:

```python
    _logger.info("%s: %d words, %d candidate(s), %d moment(s) written",
                 video_id, len(words), len(candidates), len(names))
```

The early `return []` on an empty transcript is a behavior change worth stating: it skips scoring that could only produce nothing, and it is what lets the studio report the real reason. In `src/yt_shorts/studio/jobs.py`'s `_run_detect`, when `names` is empty, record a message that names the cause rather than a silent success:

```python
        names = detect_fn(...)
        if not names:
            job.record("detect", "done", None,
                       "no moments detected - see the job log for why "
                       "(an empty transcript or failed chunks are reported there)")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_transcribe_logging.py -q`
Expected: PASS (4 tests).

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS (all — `tests/test_stream_transcribe.py` and `tests/test_detect.py` must still pass; the empty-transcript early return may need an existing test's expectation updated, which is correct and intended).

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/stream_transcribe.py src/yt_shorts/detect.py src/yt_shorts/studio/jobs.py tests/test_transcribe_logging.py
git commit -m "fix(logging): report why a chunk decode failed instead of swallowing it"
```

---

### Task 6: Studio log routes

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Create: `tests/test_studio_logs_api.py`

**Interfaces:**
- Consumes: `logsetup.list_logs/newest_log/read_new_lines/archive_dates/resolve_archive`, `workspace.logs_dir/job_logs_dir/CENTRAL_LOG_NAME`, `pathnames.validate_segment`.
- Produces three routes:
  - `GET /api/logs` → `{"central": {"name": str, "size": int, "modified": float}, "archives": ["YYYY-MM-DD", ...], "jobs": [{"name": str, "size": int, "modified": float}, ...]}`
  - `GET /api/logs/{name}?after=<int>&date=<YYYY-MM-DD>` → `{"lines": [str], "position": int, "name": str, "date": str | null}`
  - `GET /api/jobs/{job_id}/log?after=<int>` → the same body shape

**Design notes:**
- `{name}` is validated with `pathnames.validate_segment` BEFORE any filesystem touch (400 on a bad segment), then resolved: the central log by exact name, a job log inside `logs/jobs/`. Anything else is a 404. A `date` goes through `logsetup.resolve_archive` — a traversal, a malformed date or a cross-directory escape yields 404, never a file outside `logs/`.
- A gzipped target (`.gz`, either a dated archive or a finished job log) is decompressed server-side for viewing. Tailing with `after` applies only to a live plain file; for a gzipped file the whole content is returned with `position` echoed back unchanged.
- `GET /api/jobs/{job_id}/log` looks up the job in the store, uses its `log_name`, and falls back to `<name>.gz` when the job has finished and its log was compressed. An unknown job is 404.
- These are READ routes over `<workspace>/logs/` only; they never serve anything else, and no secret is ever in that directory.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_studio_logs_api.py`:

```python
"""The studio's log routes: listing, tailing, archives, and their guards."""

import gzip

import pytest
from fastapi.testclient import TestClient

from yt_shorts import workspace
from yt_shorts.studio.api import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "resolve",
                        lambda *a, **k: workspace.Workspace(
                            root=tmp_path, channels_dir=tmp_path / "channels",
                            origin="test"))
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
    "/api/logs/..%2F..%2Fetc%2Fpasswd",
    "/api/logs/.hidden",
    "/api/logs/yt-shorts.log?date=../2026-07-23",
    "/api/logs/yt-shorts.log?date=not-a-date",
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_logs_api.py -q`
Expected: FAIL — 404 on `/api/logs` (the SPA fallback catches it, since the route does not exist yet).

- [ ] **Step 3: Implement the routes**

In `src/yt_shorts/studio/api.py`, register these BEFORE the SPA fallback (which must stay last):

```python
    def _logs_root():
        return workspace.logs_dir(_resolve_workspace().root)

    def _resolve_log(name: str, date: str | None):
        """The file a log request names, or None. Every path is inside
        <workspace>/logs: the central log by exact name, a job log inside
        jobs/, a dated archive via logsetup.resolve_archive (which guards
        traversal). Nothing else is reachable - and the workspace's auth/
        directory, the only place secrets live, is not under logs/ at all."""
        root = _logs_root()
        if date is not None:
            return logsetup.resolve_archive(root, name, date)
        for candidate in (root / name, root / "jobs" / name,
                          root / "jobs" / f"{name}.gz"):
            if candidate.is_file():
                return str(candidate)
        return None

    def _log_body(path: str, after: int) -> dict:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                return {"lines": handle.read().splitlines(), "position": after}
        lines, position = logsetup.read_new_lines(path, after)
        return {"lines": lines, "position": position}

    @app.get("/api/logs")
    def list_log_files() -> dict:
        root = _logs_root()
        central = root / workspace.CENTRAL_LOG_NAME
        return {
            "central": _describe_log(central),
            "archives": logsetup.archive_dates(root, [workspace.CENTRAL_LOG_NAME]),
            "jobs": [_describe_log(Path(p))
                     for p in logsetup.list_logs(root / "jobs")],
        }

    @app.get("/api/logs/{name}")
    def read_log(name: str, after: int = 0, date: str | None = None) -> dict:
        try:
            pathnames.validate_segment(name, what="log name")
        except pathnames.SegmentError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        path = _resolve_log(name, date)
        if path is None:
            raise HTTPException(status_code=404, detail=f"No such log: {name}")
        return {**_log_body(path, after), "name": name, "date": date}

    @app.get("/api/jobs/{job_id}/log")
    def read_job_log(job_id: str, after: int = 0) -> dict:
        job = app.state.job_store.get(job_id)
        if job is None or job.log_path is None:
            raise HTTPException(status_code=404, detail="No log for that job")
        path = job.log_path if Path(job.log_path).is_file() else f"{job.log_path}.gz"
        if not Path(path).is_file():
            raise HTTPException(status_code=404, detail="No log for that job")
        return {**_log_body(path, after), "name": Path(path).name, "date": None}
```

Add a small `_describe_log(path) -> dict` returning `{"name", "size", "modified"}` (0/0.0 when the file is absent), plus `import gzip` and the `logsetup` import.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_logs_api.py -q`
Expected: PASS (11 tests).

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS (all).

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/studio/api.py tests/test_studio_logs_api.py
git commit -m "feat(studio): read-only log routes over the workspace log directory"
```

---

### Task 7: Frontend — API client and pure log helpers

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Modify: `src/yt_shorts/studio/web/src/scopedApi.ts` (the `Screen` union and `parseRoute`/`routePath`)
- Modify: `src/yt_shorts/studio/web/src/scopedApi.test.ts`
- Create: `src/yt_shorts/studio/web/src/logs.ts`
- Create: `src/yt_shorts/studio/web/src/logs.test.ts`

**Interfaces:**
- Consumes: Task 6's routes.
- Produces, in `api.ts`:
  - `export interface LogFile { name: string; size: number; modified: number }`
  - `export interface LogListing { central: LogFile; archives: string[]; jobs: LogFile[] }`
  - `export interface LogContent { lines: string[]; position: number; name: string; date: string | null }`
  - `listLogs(): Promise<LogListing>`
  - `readLog(name: string, opts?: { after?: number; date?: string }): Promise<LogContent>`
  - `readJobLog(jobId: string, opts?: { after?: number }): Promise<LogContent>`
- Produces, in `logs.ts` (pure, no React):
  - `export type LogLevel = 'ERROR' | 'WARNING' | 'INFO' | 'OTHER'`
  - `parseLine(line: string): { timestamp: string; level: LogLevel; message: string }`
  - `appendLines(existing: string[], incoming: string[], max?: number): string[]` (caps at `MAX_LINES = 5000`, dropping from the front)
  - `formatSize(bytes: number): string`
  - `jobKindFromLogName(name: string): string | null` (`"detect-abc.log"` → `"detect"`)
- In `scopedApi.ts`: `Screen` gains `'logs'`; `parseRoute` maps the single segment `logs` to `{ screen: 'logs' }` (matched with the same precedence as `settings`, BEFORE the generic one-segment channel rule); `routePath` returns `/logs`.

- [ ] **Step 1: Write the failing tests**

Create `src/yt_shorts/studio/web/src/logs.test.ts`:

```typescript
import { describe, expect, it } from 'vitest'
import { appendLines, formatSize, jobKindFromLogName, MAX_LINES, parseLine } from './logs'

describe('parseLine', () => {
  it('splits a standard log line into its parts', () => {
    const parsed = parseLine('2026-07-24 10:00:00 WARNING chunk 3 failed: RuntimeError: boom')
    expect(parsed.timestamp).toBe('2026-07-24 10:00:00')
    expect(parsed.level).toBe('WARNING')
    expect(parsed.message).toBe('chunk 3 failed: RuntimeError: boom')
  })

  it('classifies ERROR and INFO', () => {
    expect(parseLine('2026-07-24 10:00:00 ERROR nope').level).toBe('ERROR')
    expect(parseLine('2026-07-24 10:00:00 INFO fine').level).toBe('INFO')
  })

  it('keeps an unparseable line whole as OTHER', () => {
    const parsed = parseLine('  Traceback (most recent call last):')
    expect(parsed.level).toBe('OTHER')
    expect(parsed.message).toBe('  Traceback (most recent call last):')
    expect(parsed.timestamp).toBe('')
  })
})

describe('appendLines', () => {
  it('appends incoming lines', () => {
    expect(appendLines(['a'], ['b', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('drops from the front past the cap so a long tail cannot grow forever', () => {
    const existing = Array.from({ length: MAX_LINES }, (_unused, index) => `line ${index}`)
    const result = appendLines(existing, ['newest'])
    expect(result).toHaveLength(MAX_LINES)
    expect(result[result.length - 1]).toBe('newest')
    expect(result[0]).toBe('line 1')
  })

  it('returns the existing array unchanged when nothing arrived', () => {
    const existing = ['a']
    expect(appendLines(existing, [])).toBe(existing)
  })
})

describe('formatSize', () => {
  it('formats bytes, kB and MB', () => {
    expect(formatSize(512)).toBe('512 B')
    expect(formatSize(2048)).toBe('2.0 kB')
    expect(formatSize(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})

describe('jobKindFromLogName', () => {
  it('reads the kind from a job log name', () => {
    expect(jobKindFromLogName('detect-abc123.log')).toBe('detect')
    expect(jobKindFromLogName('render-xyz.log.gz')).toBe('render')
  })

  it('returns null for the central log', () => {
    expect(jobKindFromLogName('yt-shorts.log')).toBeNull()
  })
})
```

Append to `src/yt_shorts/studio/web/src/scopedApi.test.ts`:

```typescript
  it('routes /logs to the logs screen', () => {
    expect(parseRoute('/logs')).toEqual({ screen: 'logs' })
    expect(routePath({ screen: 'logs' })).toBe('/logs')
  })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (in `src/yt_shorts/studio/web`): `npm test`
Expected: FAIL — `Cannot find module './logs'`.

- [ ] **Step 3: Implement**

Create `src/yt_shorts/studio/web/src/logs.ts`:

```typescript
/** Pure helpers for the Logs screen (see components/LogsScreen.tsx and the
 * /api/logs routes in api.py), in their own module - exporting no React - so
 * Vite's fast-refresh boundary stays component-only, the same convention as
 * settings.ts/uploadMeta.ts, and the parsing is unit-tested directly. */

export type LogLevel = 'ERROR' | 'WARNING' | 'INFO' | 'OTHER'

/** The tail keeps at most this many lines in memory: a one-hour transcription
 * writes a lot, and an unbounded array would grow until the tab dies. */
export const MAX_LINES = 5000

const LINE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (ERROR|WARNING|INFO|DEBUG|CRITICAL) (.*)$/

/** Split one log line into timestamp / level / message. A line the backend did
 * not write (a traceback's continuation, say) is returned whole at OTHER rather
 * than being dropped - a stack trace is exactly what an operator needs to see. */
export function parseLine(line: string): { timestamp: string; level: LogLevel; message: string } {
  const match = LINE.exec(line)
  if (!match) return { timestamp: '', level: 'OTHER', message: line }
  const level = match[2]
  const known: LogLevel = level === 'ERROR' || level === 'CRITICAL'
    ? 'ERROR'
    : level === 'WARNING' ? 'WARNING' : level === 'INFO' ? 'INFO' : 'OTHER'
  return { timestamp: match[1], level: known, message: match[3] }
}

/** Append newly-tailed lines, capped at `max` by dropping the oldest. Returns
 * the original array untouched when nothing arrived, so React can skip a
 * re-render. */
export function appendLines(existing: string[], incoming: string[],
                            max: number = MAX_LINES): string[] {
  if (incoming.length === 0) return existing
  const combined = [...existing, ...incoming]
  return combined.length <= max ? combined : combined.slice(combined.length - max)
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} kB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** The job kind a job-log filename encodes (`detect-<id>.log[.gz]`), or null
 * when the name is not a job log. */
export function jobKindFromLogName(name: string): string | null {
  const match = /^(render|detect|upload|connect|copy)-[^.]+\.log(\.gz)?$/.exec(name)
  return match ? match[1] : null
}
```

In `api.ts`, add the three fetchers and their types beside the existing ones (follow the file's own fetch/error conventions). In `scopedApi.ts`, extend `Screen` with `'logs'`, add the `logs` branch to `parseRoute` next to the `settings` branch (same reserved-name comment applies), and the `/logs` branch to `routePath`.

- [ ] **Step 4: Run the tests to verify they pass**

Run (in `src/yt_shorts/studio/web`): `npm test`
Expected: PASS (all, including the new `logs.test.ts` and the routing case).

Run: `npx tsc -b` → 0 errors; `npm run lint` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/logs.ts src/yt_shorts/studio/web/src/logs.test.ts src/yt_shorts/studio/web/src/api.ts src/yt_shorts/studio/web/src/scopedApi.ts src/yt_shorts/studio/web/src/scopedApi.test.ts
git commit -m "feat(studio-web): log API client, pure log helpers, /logs route"
```

---

### Task 8: Frontend — the Logs screen and the per-job link

**Files:**
- Create: `src/yt_shorts/studio/web/src/components/LogsScreen.tsx`
- Modify: `src/yt_shorts/studio/web/src/Root.tsx` (dispatch the new screen)
- Modify: `src/yt_shorts/studio/web/src/components/SettingsScreen.tsx` or the nav host (a link to `/logs`)
- Modify: `src/yt_shorts/studio/web/src/components/RenderPanel.tsx`, `StreamPanel.tsx`, `UploadPanel.tsx` (a "View log" link per job)

**Interfaces:**
- Consumes: Task 7's `listLogs`/`readLog`/`readJobLog` and `logs.ts`; `NavScreen.tsx` for the screen chrome; `useRoute`/`navigate`.
- Produces: no new pure modules (all logic already lives in `logs.ts`).

**Design notes for the implementer:**
- Read `SettingsScreen.tsx` in full FIRST and mirror it: it is the existing workspace-level screen, and the Logs screen is its sibling in structure (NavScreen chrome, loading/error states, Mantine cards).
- **Scrolling is a mandatory acceptance criterion** (`body { overflow: hidden }` in `index.css` means every full-height screen owns its own scroll container). The log viewer pane must scroll independently — `flex: 1 1 auto; minHeight: 0; overflowY: auto` — and the screen must be usable at a short viewport, with every control reachable. Verify at a short window before calling the task done.
- Layout: a left list (central log with its archive dates in a `Select`, then job logs newest-first with kind + size via `formatSize`/`jobKindFromLogName`), a right viewer pane rendering parsed lines in a monospace block, with `ERROR` red and `WARNING` yellow via `parseLine`'s level.
- Live tail: while the central (or a live job) log is selected and no archive date is chosen, poll `readLog(name, { after: position })` on an interval (2 s), feeding `appendLines`. Stop polling when an archive date is selected (it is immutable) and on unmount. Follow the existing `hooks/useJobPolling.ts` idiom rather than inventing a new one.
- "View log" per job: each job panel that holds a job id renders a link/button navigating to `/logs` with that job's `log_name` preselected — the simplest correct approach is `navigate('/logs')` plus a module-level selected-name argument via the URL query (`/logs?file=<name>`), read in `LogsScreen`. Keep any parsing of that query in `logs.ts` if it needs more than one line.
- A download link per file: an anchor to `/api/logs/<name>` (and `?date=` for an archive) with `download`, so the operator can keep a copy.

- [ ] **Step 1: Implement the Logs screen and wire the route**

Build `LogsScreen.tsx` per the notes, add the `'logs'` case to `Root.tsx`'s screen dispatch beside `'settings'`, and add a nav entry pointing at `/logs`.

- [ ] **Step 2: Add the per-job "View log" links**

In each panel that shows a running/finished job (`RenderPanel`, `StreamPanel` for detect, `UploadPanel`), render a "View log" action when the job snapshot carries a non-null `log_name`.

- [ ] **Step 3: Verify (including scrolling)**

Run (in `src/yt_shorts/studio/web`): `npx tsc -b` → 0; `npm run lint` → clean; `npm test` → all pass.
Then check the screen at a short viewport (e.g. 900x600): the log list and the viewer pane must each scroll, and every control must be reachable.

- [ ] **Step 4: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/LogsScreen.tsx src/yt_shorts/studio/web/src/Root.tsx src/yt_shorts/studio/web/src/components/SettingsScreen.tsx src/yt_shorts/studio/web/src/components/RenderPanel.tsx src/yt_shorts/studio/web/src/components/StreamPanel.tsx src/yt_shorts/studio/web/src/components/UploadPanel.tsx
git commit -m "feat(studio-web): Logs screen with live tail, archives, and per-job links"
```

---

### Task 9: Secret-safety tests, CLAUDE.md, E2E, build, full verification

**Files:**
- Create: `tests/test_logging_secrets.py`
- Modify: `tests/test_studio_e2e.py`
- Modify: `CLAUDE.md`
- Modify: `src/yt_shorts/studio/static/**` (rebuilt)

- [ ] **Step 1: Write the secret-safety tests**

Create `tests/test_logging_secrets.py`:

```python
"""Secrets must never reach a log, and the log routes must never serve one.

The workspace's auth/ directory (client_secret.json, token-<id>.json,
quota.json) is the one place secrets live; logs/ is a sibling, and every log
route resolves inside logs/ only.
"""

import logging

from yt_shorts import logsetup


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
```

- [ ] **Step 2: Run them**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_logging_secrets.py -q`
Expected: PASS (2 tests).

- [ ] **Step 3: Add the Playwright E2E**

In `tests/test_studio_e2e.py`, following the existing E2E fixtures and selector style, add a `TestLogsScreen` covering:
1. seed `<workspace>/logs/yt-shorts.log` with two lines, open `/logs`, assert both lines render and the file is listed;
2. seed a job log (`detect-<id>.log`), assert it is listed with its kind and that selecting it shows its content;
3. run a stubbed detect job (reuse the existing detect-job stub pattern), follow its "View log" link, and assert the job's log content is shown.

- [ ] **Step 4: Update `CLAUDE.md`**

Add a section documenting the new subsystem, in the file's voice (constraints and their reasons, not a tutorial). It must state:
- `logsetup.py` is stdlib-only and must never import from the project or FastAPI/google — the CLI imports it in a minimal venv.
- Logs live in `<workspace>/logs/`: central `yt-shorts.log` (daily rotation → `.YYYY-MM-DD.gz`) plus one gzipped log per background job under `jobs/`; `prune_old_logs` (30 days) is the ONLY deletion authority, handlers use `backupCount=0`.
- Logging is best-effort everywhere: a failed log/rotation/compression warns and continues, never aborts a job or a render.
- Secrets never reach a log; `shorten_urls` elides signed-URL queries.
- The per-job logger must not propagate into the central log.
- `stream_transcribe` no longer swallows a chunk failure — it logs the cause and records the index; `detect` reports `words/candidates/written` and returns `[]` loudly on an empty transcript.
- The studio's log routes are read-only and resolve strictly inside `<workspace>/logs/` (`resolve_archive`'s traversal guard); they are registered before the SPA fallback.

- [ ] **Step 5: Build and run every suite**

```bash
cd src/yt_shorts/studio/web && npm run lint && npm run build && npm test && cd -
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
```
Expected: oxlint clean, build exit 0, Vitest all pass, pytest all pass, `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md tests/test_logging_secrets.py tests/test_studio_e2e.py src/yt_shorts/studio/static
git commit -m "docs+build(logging): document the logging contract; e2e; rebuild static"
```

- [ ] **Step 7: Manual smoke (operator)**

Restart `bin/yt-shorts studio` (uvicorn runs without `--reload`, so backend routes need a restart), open `/logs`, confirm the central log renders and tails, then start a detect run on stream `V9nVNEQNdR4` and watch its job log report per-chunk progress and any failure cause.
