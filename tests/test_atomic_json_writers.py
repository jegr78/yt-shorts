"""Every file another process reads is REPLACED, never rewritten in place.

`atomicwrite` proves the mechanic; this file proves it is the one being used.
Together they are the whole guarantee - a module that quietly goes back to
`Path.write_text` would pass every behavioural test in the suite, because a
truncating write is only wrong for the microseconds another reader is in it.

That is exactly how the defect this all came from stayed invisible: one
E2E test, on one CI leg, once.
"""

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The same file set the linter walks - every tracked *.py PLUS extensionless
# files with a python shebang. Scanning `src/**/*.py` alone would miss
# `bin/yt-shorts`, the CLI, for the same reason tools/lint.py's own
# `_python_files` exists: it has no .py suffix.
_spec = importlib.util.spec_from_file_location("lintmod", ROOT / "tools" / "lint.py")
_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lint)

# Both halves of the pair. `write_bytes` is here because leaving it out let a
# real one through: `font_admin.save_font` dropped a TTF into the channel's
# fonts/ with `write_bytes` while a render could be reading that path, and the
# first version of this guard - which looked for `write_text` only - reported
# a clean sweep anyway.
WRITE_METHODS = ("write_text", "write_bytes")

# The only receivers in this project allowed a raw call, with the reason each
# one is not the hazard. Keyed by (file, receiver name) rather than by line, so
# reformatting cannot silently retire an entry.
ALLOWED = {
    ("job_queue.py", "scratch"):
        "its own scratch file, moved into place with os.replace right after - "
        "the same mechanic, kept inline (see atomicwrite's docstring)",
    ("workspace.py", "scratch"):
        "same: write_settings' own scratch, then os.replace",
    ("stream_transcribe.py", "glossary_path"):
        "an argv file for the decoder subprocess, inside a TemporaryDirectory "
        "that nothing else can see, written before the process it is for exists",
    ("subtitle_track.py", "script"):
        "the ffmpeg concat script in a work dir, read only by the ffmpeg run "
        "that follows it",
}

# Deliberately NOT covered: `open(path, "wb")`. The one left in the tree is
# logsetup's gzip recompression of a finished log, which writes to a DIFFERENT
# path and then removes the original - a rename in all but name. Widening the
# guard to every binary open would report that as a violation and teach the
# next reader to ignore it.


def _raw_write_calls():
    """(file, receiver) for every `<x>.write_text(...)`/`.write_bytes(...)`
    that is not atomicwrite's own function."""
    found = set()
    for absolute in _lint._python_files(ROOT):
        path = Path(absolute).relative_to(ROOT)
        # src/ and bin/ only: what an operator runs. tools/ holds developer
        # throwaways (wiki images, a sample generator, an ACL probe) whose
        # output nothing reads concurrently, and tests/ writes fixtures on
        # purpose - pulling either in would mean exemptions that teach the
        # next reader to ignore this list.
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
            # unparse, not dump: the failure message names the expression
            # the way it is written, not as an AST tree.
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
        """An entry that no longer matches anything is a claim about code that
        is gone - it would quietly excuse the next receiver to reuse the
        name."""
        gone = set(ALLOWED) - _raw_write_calls()
        assert not gone, f"{gone}: no longer exists; drop it from ALLOWED"

    def test_the_scan_reaches_the_extensionless_cli(self):
        """The guard is only worth its name if it walks the same files the
        linter does - `bin/yt-shorts` is the one that has no .py suffix."""
        scanned = {Path(p).name for p in _lint._python_files(ROOT)}
        assert "yt-shorts" in scanned
