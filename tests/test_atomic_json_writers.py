"""Pins WHO writes a file in place - `atomicwrite`'s own tests cover the how.

A module that goes back to `Path.write_text` passes every behavioural test in
the suite: a truncating write is only wrong for the microseconds another
reader is inside it.
"""

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The linter's file set: tracked *.py PLUS extensionless files with a python
# shebang. `src/**/*.py` alone would miss `bin/yt-shorts`.
_spec = importlib.util.spec_from_file_location("lintmod", ROOT / "tools" / "lint.py")
_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lint)

# Both halves: a write_text-only guard reported font_admin's write_bytes clean.
WRITE_METHODS = ("write_text", "write_bytes")

# The receivers allowed a raw call, and why each is not the hazard. Keyed by
# (file, receiver) so reformatting cannot retire an entry.
ALLOWED = {
    ("job_queue.py", "scratch"):
        "its own scratch, os.replace'd right after - the mechanic, inline",
    ("workspace.py", "scratch"):
        "same: write_settings' own scratch, then os.replace",
    ("stream_transcribe.py", "glossary_path"):
        "argv file in a TemporaryDirectory, written before the process it "
        "is for exists",
    ("subtitle_track.py", "script"):
        "the ffmpeg concat script, read only by the ffmpeg run that follows",
}

# NOT covered: `open(path, "wb")`. The one left is logsetup's gzip of a
# finished log, which writes a DIFFERENT path - a rename in all but name.


def _raw_write_calls():
    """(file, receiver) for every `<x>.write_text(...)`/`.write_bytes(...)`
    that is not atomicwrite's own function."""
    found = set()
    for absolute in _lint._python_files(ROOT):
        path = Path(absolute).relative_to(ROOT)
        # src/ and bin/ only: what an operator runs. tools/ and tests/ write
        # throwaways, and exempting them would dilute the list.
        if path.parts[0] not in ("src", "bin"):
            continue
        tree = ast.parse(Path(absolute).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in WRITE_METHODS):
                continue
            base = node.func.value
            if isinstance(base, ast.Name) and base.id == "atomicwrite":
                continue
            # unparse, not dump: readable in the failure message.
            name = base.id if isinstance(base, ast.Name) else ast.unparse(base)
            found.add((path.name, name))
    return found


class TestNothingWritesAsharedFileInPlace:
    def test_every_raw_write_is_one_of_the_known_ones(self):
        unexpected = _raw_write_calls() - set(ALLOWED)
        assert not unexpected, (
            f"{unexpected}: write a file another process reads through "
            f"atomicwrite.write_text/write_bytes, or add the receiver to "
            f"ALLOWED with the reason it is safe")

    def test_the_allowlist_has_not_gone_stale(self):
        """A stale entry would excuse the next receiver to reuse the name."""
        gone = set(ALLOWED) - _raw_write_calls()
        assert not gone, f"{gone}: no longer exists; drop it from ALLOWED"

    def test_the_scan_reaches_the_extensionless_cli(self):
        """`bin/yt-shorts` is the file a *.py glob would miss."""
        scanned = {Path(p).name for p in _lint._python_files(ROOT)}
        assert "yt-shorts" in scanned
