"""Replace a text file whole, or not at all.

`Path.write_text` TRUNCATES its target and only then writes it, so every
reader - `profile.load` on each render, the studio's own poll after a save,
another operator's `gallery` - can observe an EMPTY file for the length of a
write. That is not theory: it took a CI run down (see
`tests/test_glossary_admin.py`'s TestTheWriteIsAtomic), reading a zero-byte
`glossary.json` and dying in `json.loads`.

The mechanic is the one `quota._atomic_write`, `workspace.write_settings` and
`job_queue.save` each already implement inline - write a scratch sibling,
`os.replace` it into place, which is atomic within one filesystem. Those three
are deliberately NOT rewritten to call this: `quota` writes 0600 via
`tempfile.mkstemp` and the other two use a fixed `.part` name, so folding them
in would change file modes and scratch names for no defect anyone has
measured.

Two details are load-bearing, and both were got wrong in earlier drafts of
this module - each time in a way the test suite could not see and CodeQL
could:

- **No mode literal anywhere.** The first draft reached the same atomicity
  with `tempfile.mkstemp` (0600) plus `os.chmod(tmp, 0o644)`. That RESETS a
  mode an operator tightened by hand, and writing the literal mask is
  `py/overly-permissive-file` ("sets file to world readable") - stating an
  intention nobody had about a file that already had those permissions.
  `shutil.copymode` carries the existing file's own mode across instead, so a
  replacement changes the contents and nothing else. A file that does not
  exist yet is created OWNER-ONLY, which is mkstemp's mode and a deliberate
  narrowing: 0600 for a new `clip.json` in an operator's own workspace costs
  nothing, and picking any wider literal is the mistake above.
- **The scratch path is `mkstemp`'s, not one built from the target's name.**
  The second draft used `target.with_name(f".{target.name}.{token}.part")`,
  which is a path expression over a caller-supplied string: four
  `py/path-injection` alerts, measured on this branch. `mkstemp` with a
  constant prefix raises none, and gives O_EXCL and an unguessable name for
  free - so two concurrent writers of one file can neither share a scratch
  nor have one symlinked in advance.

Callers, as of this writing: `glossary_admin`, `lexicon_admin`, `brand_admin`
(2), `event_brand_admin`, `channel_admin` (3), `editorial` and `clipstore` -
every module that rewrites a JSON file some other process reads. NOT yet
swept, and each still truncating: `transcribe`'s word cache, `detect`,
`stream_transcribe`, `upload_record`, `trim`'s state file, `workspaces` and
`cli`'s gallery page. None of them has been measured failing this way; the
list is here so the next person knows what is left rather than assuming the
sweep was total.

Stdlib only, imports nothing from this project: `bin/yt-shorts` runs in a venv
that may have installed neither FastAPI nor anything else optional.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# Constant, and NOT built from the target's own name: a scratch path assembled
# out of a caller-supplied string is a path expression over tainted data, which
# is CodeQL's py/path-injection - four alerts, measured on this branch, where
# the mkstemp form raises none. The file lives for milliseconds; it does not
# need to be self-describing.
SCRATCH_PREFIX = ".tmp-"
SCRATCH_SUFFIX = ".part"


def write_text(path: Path | str, text: str) -> None:
    """Writes `text` to `path` so that no reader ever sees it half-written.

    The parent directory must exist - this is a replacement for
    `Path.write_text`, not for `mkdir`. A failure anywhere leaves the previous
    file untouched and removes the scratch.

    The handle is a TEXT handle, like the `write_text` this replaces, so
    Windows still writes CRLF here and the bytes on any one platform are
    unchanged by this module. Do not "fix" that with newline="": it would
    silently change every file this writes on one platform only.
    """
    target = Path(path)
    fd, scratch = tempfile.mkstemp(dir=target.parent, prefix=SCRATCH_PREFIX,
                                   suffix=SCRATCH_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _carry_permissions(target, scratch)
        os.replace(scratch, target)
    except BaseException:
        try:
            os.unlink(scratch)
        except FileNotFoundError:
            pass  # already moved into place; nothing to clean up
        raise


def _carry_permissions(target: Path, scratch: str) -> None:
    """Gives the replacement the mode the file it replaces already had.

    An operator who tightened a file by hand keeps that; everything else keeps
    whatever it was created with. A file that does NOT exist yet is created
    owner-only, mkstemp's mode - deliberately not widened to a literal 0o644
    here, which would both undo a hand-tightened mode and state a
    world-readable intention nobody had (CodeQL's py/overly-permissive-file
    flags exactly that, and did).
    """
    try:
        shutil.copymode(target, scratch)
    except FileNotFoundError:
        pass  # a new file keeps mkstemp's owner-only mode
