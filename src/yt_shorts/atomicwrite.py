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

Two details are load-bearing and were both got wrong in an earlier draft:

- **No explicit mode.** The scratch is written with `Path.write_text`, so the
  file lands with 0666 masked by the process umask - byte-for-byte the
  permissions the truncating write it replaces produced. Reaching the same
  atomicity with `tempfile.mkstemp` (0600) plus `os.chmod(tmp, 0o644)` looks
  equivalent and is not: it RESETS a mode an operator tightened by hand, and
  writing that literal mask is what CodeQL's `py/overly-permissive-file`
  flags, correctly - the code would be stating "world readable" as an
  intention nobody had.
- **The scratch name carries random bytes.** A fixed `.part` sibling lets two
  concurrent writers of one file interleave into a single scratch, and names
  the file an attacker would symlink to. `secrets.token_hex` costs nothing
  and removes both.

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
import secrets
from pathlib import Path


def write_text(path: Path | str, text: str) -> None:
    """Writes `text` to `path` so that no reader ever sees it half-written.

    The parent directory must exist - this is a replacement for
    `Path.write_text`, not for `mkdir`. A failure anywhere leaves the previous
    file untouched and removes the scratch.
    """
    target = Path(path)
    scratch = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
    try:
        scratch.write_text(text, encoding="utf-8")
        os.replace(scratch, target)
    except BaseException:
        try:
            scratch.unlink()
        except FileNotFoundError:
            pass  # never created, or already moved into place
        raise
