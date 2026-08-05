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
import re
import shutil
import sys
import time
from logging.handlers import TimedRotatingFileHandler

DEFAULT_RETENTION_DAYS = 30

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def compress_file(path) -> str | None:
    """Gzip `path` to `path + '.gz'` and remove the original, returning the new
    path - or None if anything went wrong (or the target already exists),
    leaving the original in place. A log that could not be compressed is
    still a log; losing it to a failed compression would defeat the point of
    writing it.

    A pre-existing target is refused OUTRIGHT, on both the success and the
    failure path - not only that a second call on an already-compressed log
    (the plain file gone, the `.gz` already complete) must be a safe no-op,
    which is what `finish_job_log`'s documented idempotency relies on, but
    also that two DIFFERENT plain files can collide on the same archive name:
    the CLI and the studio both attach a `TimedRotatingFileHandler` to the
    same central `<workspace>/logs/yt-shorts.log`, so across midnight one
    process can gzip `yt-shorts.log.<date>` first, and a second process's own
    rollover then renames ITS base file to that same dated name and would,
    without this guard, `gzip.open(target, "wb")` straight over the first
    process's finished archive - silently destroying a day of the central
    log. `target`'s existence is therefore recorded and checked BEFORE
    anything is attempted, so the open-for-write that would truncate it is
    never reached. The colliding plain file is left on disk, visible and
    prunable by `prune_old_logs` (the only deletion authority)."""
    path = str(path)
    target = path + ".gz"
    if os.path.exists(target):
        return None
    try:
        with open(path, "rb") as plain, gzip.open(target, "wb") as packed:
            shutil.copyfileobj(plain, packed)
        os.remove(path)
        return target
    except OSError:
        # The target cannot pre-exist here (the check above already returned
        # for that case), so any target found now was created by THIS
        # attempt - a half-written .gz is worse than none.
        try:
            os.remove(target)
        except OSError:
            pass  # best-effort cleanup; nothing more we can do here
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


def read_new_lines(path, pos):
    """Read whole lines appended to `path` since byte offset `pos`, returning
    (lines, new_pos). RE-OPENS AND CLOSES the file each call so a concurrent
    writer can rotate/rename it on Windows — a continuously-held read handle is
    exactly what blocks the relay's midnight rollover. A half-written trailing
    line (no terminating newline yet) is held back for the next poll. On
    rotation/truncation (file now shorter than `pos`) it restarts from offset 0.
    A missing/unreadable file yields ([], pos) so the caller just retries."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() < pos:
                pos = 0                  # rotated or truncated — re-read from top
            fh.seek(pos)
            data = fh.read()
    except OSError:
        return [], pos                   # vanished mid-rotation — retry next poll
    nl = data.rfind(b"\n")
    if nl == -1:
        return [], pos                   # no complete line yet
    consumed = data[:nl + 1]
    return consumed.decode("utf-8", "replace").splitlines(), pos + len(consumed)


def list_logs(log_dir):
    """Regular files in log_dir, newest-first by mtime; [] if dir is absent."""
    try:
        files = [os.path.join(str(log_dir), f) for f in os.listdir(str(log_dir))]
    except OSError:
        return []
    files = [f for f in files if os.path.isfile(f)]
    files.sort(key=os.path.getmtime, reverse=True)
    return files


def newest_log(log_dir):
    """Newest file in log_dir, or None."""
    files = list_logs(log_dir)
    return files[0] if files else None


def archive_dates(log_dir, basenames):
    """Sorted-descending list of YYYY-MM-DD dates for which any of `basenames` has a
    rotated archive (`<basename>.<date>` or its gzipped `<basename>.<date>.gz`) in
    log_dir."""
    dates = set()
    for path in list_logs(log_dir):
        name = os.path.basename(path)
        for base in basenames:
            m = re.fullmatch(re.escape(base) + r"\.(\d{4}-\d{2}-\d{2})(?:\.gz)?", name)
            if m:
                dates.add(m.group(1))
    return sorted(dates, reverse=True)


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
            continue  # different drives on Windows — never inside root
        if inside and os.path.isfile(full):
            return full
    return None


_ERROR_HINTS = ("error", "fatal", "forbidden", "403", "401", "traceback",
                "exception", "failed", "denied", "could not", "no such")
_WARN_HINTS = ("warn", "retry", "retrying", "unable", "timeout", "timed out",
               "waiting for")


def classify_subproc_line(line):
    """Heuristic logging level for one pumped subprocess line."""
    low = line.lower()
    if any(h in low for h in _ERROR_HINTS):
        return logging.ERROR
    if any(h in low for h in _WARN_HINTS):
        return logging.WARNING
    return logging.INFO


URL_SHORTEN_MAX = 120
_URL_RE = re.compile(r"https?://[^\s]+")
# racecast's original ([/=]itag[/=]) only catches HLS manifest path segments
# (.../itag/301/...); yt-dlp's real googlevideo videoplayback URLs put itag in
# the query string (...&itag=140&...), so "&" and "?" are accepted here too.
_ITAG_RE = re.compile(r"[/=&?]itag[/=](\d+)")
_DIGITS_RE = re.compile(r"\d+")


def shorten_urls(text, max_len=URL_SHORTEN_MAX):
    """Replace each URL longer than max_len with a compact host-only form, dropping
    the path+query (where googlevideo sig/lsig tokens live) and keeping the itag for
    diagnostics. URLs <= max_len and non-URL text are returned unchanged. Pure."""
    def _shrink(match):
        url = match.group(0)
        if len(url) <= max_len:
            return url
        scheme, _, after = url.partition("://")
        host = after.split("/", 1)[0].split("?", 1)[0]
        elided = len(url) - len(scheme) - len("://") - len(host)
        itag = _ITAG_RE.search(url)
        tag = f"itag {itag.group(1)}, " if itag else ""
        return f"{scheme}://{host}/…({tag}+{elided} chars elided)"
    return _URL_RE.sub(_shrink, text)


def normalize_for_dedup(text):
    """A dedup key that ignores the volatile parts of a repeated line: every URL
    becomes <url> and every digit run becomes <n>, so the same error with a
    different expired URL / timestamp maps to one key. Pure."""
    return _DIGITS_RE.sub("<n>", _URL_RE.sub("<url>", text))


LINE_THROTTLE_RATE_MAX = 30
LINE_THROTTLE_WINDOW_S = 10.0
LINE_THROTTLE_SUMMARY_S = 30.0


class LineThrottle:
    """Per-stream throttle for pumped subprocess lines. Collapses consecutive
    duplicate-after-normalization lines (emitting a periodic '(last line repeated
    ×N)' at the line's own level, plus a '(previous line repeated ×N)' when the
    pattern changes) AND rate-limits distinct lines to rate_max per window_s (excess
    dropped, surfaced as a WARNING '(suppressed N lines)'). Pure given an injected
    monotonic clock. One instance per pump_subprocess call -> per feed, thread-isolated."""

    def __init__(self, rate_max=LINE_THROTTLE_RATE_MAX,
                 window_s=LINE_THROTTLE_WINDOW_S, summary_s=LINE_THROTTLE_SUMMARY_S):
        self.rate_max = rate_max
        self.window_s = window_s
        self.summary_s = summary_s
        self.last_key = None
        self.last_level = logging.INFO
        self.dup_count = 0
        self.last_summary_at = 0.0
        self.window_start = 0.0
        self.window_count = 0
        self.dropped_in_window = 0

    def emit(self, level, text, now):
        """Return the (level, text) records to log for one incoming line."""
        key = normalize_for_dedup(text)
        out = []
        if key == self.last_key:                       # consecutive duplicate
            self.dup_count += 1
            if now - self.last_summary_at >= self.summary_s:
                out.append((self.last_level, f"(last line repeated ×{self.dup_count})"))
                self.last_summary_at = now
            return out
        if self.dup_count > 0:                          # a new, distinct line ends a dup run
            out.append((self.last_level, f"(previous line repeated ×{self.dup_count})"))
            self.dup_count = 0
        self.last_key = key
        self.last_level = level
        self.last_summary_at = now
        if now - self.window_start >= self.window_s:    # roll the rate-limit window
            if self.dropped_in_window > 0:
                out.append((logging.WARNING, f"(suppressed {self.dropped_in_window} lines)"))
                self.dropped_in_window = 0
            self.window_start = now
            self.window_count = 0
        if self.window_count < self.rate_max:
            self.window_count += 1
            out.append((level, text))
        else:
            self.dropped_in_window += 1
        return out

    def flush(self, now):
        """Emit any pending summary at EOF so a trailing flood still reports its count."""
        out = []
        if self.dup_count > 0:
            out.append((self.last_level, f"(previous line repeated ×{self.dup_count})"))
            self.dup_count = 0
        if self.dropped_in_window > 0:
            out.append((logging.WARNING, f"(suppressed {self.dropped_in_window} lines)"))
            self.dropped_in_window = 0
        return out


def tag_line(source, line):
    """Prefix a single log line with its source tag for the merged view, stripping
    the trailing newline/carriage-return. chr(10)/chr(13) avoid a backslash escape
    inside the f-string expression (only allowed on Python 3.12+; the repo is 3.11)."""
    return f"[{source}] {line.rstrip(chr(10)).rstrip(chr(13))}"


def pump_subprocess(stream, logger, tag, on_line=None, now=time.monotonic):
    """Read text lines from a subprocess pipe (stream) and log each at a classified
    level, prefixed `[tag]`. Repeated lines are throttled and long URLs shortened
    (LineThrottle + shorten_urls) so a stuck retry loop can't flood the log; the
    first occurrence and periodic counts survive. When on_line is given, call it per
    (stripped) ORIGINAL line for side-channel parsing (e.g. feed quality) — a failing
    callback never breaks the pump. Runs to EOF; swallows read errors. Designed for a
    daemon thread."""
    throttle = LineThrottle()
    try:
        for raw in iter(stream.readline, ""):   # sentinel "" stops at EOF
            line = raw.rstrip("\n").rstrip("\r")
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:                # noqa: BLE001 — observer is best-effort
                    pass  # a broken observer must never break the pump
            try:
                level = classify_subproc_line(line)   # classify the ORIGINAL line
                for lvl, text in throttle.emit(level, shorten_urls(line), now()):
                    logger.log(lvl, "[%s] %s", tag, text)
            except Exception:                    # noqa: BLE001 — throttling must never break the pump
                # Fallback logs the raw line at a fixed level: re-classifying here
                # could raise again (if classify was the failing call) and break the
                # pump thread, defeating the best-effort contract.
                logger.log(logging.ERROR, "[%s] %s", tag, line)
    except (ValueError, OSError):
        pass  # pipe closed mid-read — end the thread, never the daemon
    finally:
        try:
            for lvl, text in throttle.flush(now()):   # surface a trailing flood's count
                logger.log(lvl, "[%s] %s", tag, text)
        except Exception:                        # noqa: BLE001 — flush is best-effort too
            pass  # flush is best-effort; never let it break shutdown
