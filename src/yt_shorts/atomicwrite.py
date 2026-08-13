"""Replace a file whole, or not at all.

`Path.write_text` truncates its target and only then writes it, so any reader
arriving mid-write sees an EMPTY file. Everything another process reads goes
through here; `tests/test_atomic_json_writers.py` lists the few exceptions.

Stdlib only and no project imports: the CLI's venv may have installed neither.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# Constant, and NOT built from the target's name: a scratch path assembled from
# a caller-supplied string is CodeQL's py/path-injection.
SCRATCH_PREFIX = ".tmp-"
SCRATCH_SUFFIX = ".part"


def write_text(path: Path | str, text: str) -> None:
    """Replaces `path` with `text`. The parent directory must exist.

    A TEXT handle, like the `write_text` it replaces, so Windows still writes
    CRLF. Do not "fix" that with newline="" - it would change every file this
    writes, on one platform only.
    """
    _replace(path, text, binary=False)


def write_bytes(path: Path | str, data: bytes) -> None:
    """Replaces `path` with `data`. Separate from write_text because that one's
    text handle would turn every 0x0A in a font into CRLF on Windows."""
    _replace(path, data, binary=True)


def _replace(path: Path | str, payload, *, binary: bool) -> None:
    target = Path(path)
    fd, scratch = tempfile.mkstemp(dir=target.parent, prefix=SCRATCH_PREFIX,
                                   suffix=SCRATCH_SUFFIX)
    try:
        with os.fdopen(fd, "wb" if binary else "w",
                       encoding=None if binary else "utf-8") as handle:
            handle.write(payload)
        _carry_permissions(target, scratch)
        os.replace(scratch, target)
    except BaseException:
        try:
            os.unlink(scratch)
        except FileNotFoundError:
            pass  # already moved into place; nothing to clean up
        raise


def _carry_permissions(target: Path, scratch: str) -> None:
    """Gives the replacement the mode the file it replaces already had, so a
    save changes contents and nothing else. No mode literal: widening to 0o644
    would undo a hand-tightened file, and CodeQL flags it (py/overly-permissive
    -file). A file that does not exist yet stays mkstemp's owner-only 0600.

    The mode carries across, a write PROTECTION does not: os.replace needs
    permission on the directory, not the file.
    """
    try:
        shutil.copymode(target, scratch)
    except FileNotFoundError:
        pass  # a new file keeps mkstemp's owner-only mode
