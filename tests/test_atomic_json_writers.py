"""Every file another process reads is REPLACED, never rewritten in place.

`atomicwrite` proves the mechanic; this file proves it is the one being used.
Together they are the whole guarantee - a module that quietly goes back to
`Path.write_text` would pass every behavioural test in the suite, because a
truncating write is only wrong for the microseconds another reader is in it.

That is exactly how the defect this all came from stayed invisible: one
E2E test, on one CI leg, once.
"""

import ast
import pathlib

SOURCE_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "yt_shorts"

# The only receivers in this project allowed a raw `.write_text(...)`, with the
# reason each one is not the hazard. Keyed by (file, receiver name) rather than
# by line, so reformatting cannot silently retire an entry.
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


def _raw_write_text_calls():
    """(file, receiver) for every `<x>.write_text(...)` that is not
    atomicwrite's own function."""
    found = set()
    for path in sorted(SOURCE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write_text"):
                continue
            base = node.func.value
            if isinstance(base, ast.Name) and base.id == "atomicwrite":
                continue
            name = base.id if isinstance(base, ast.Name) else ast.dump(base)
            found.add((path.name, name))
    return found


class TestNothingWritesAsharedFileInPlace:
    def test_every_raw_write_text_is_one_of_the_four_known_ones(self):
        unexpected = _raw_write_text_calls() - set(ALLOWED)
        assert not unexpected, (
            f"{unexpected}: write JSON another process reads through "
            f"atomicwrite.write_text, or add the receiver to ALLOWED with the "
            f"reason it is safe")

    def test_the_allowlist_has_not_gone_stale(self):
        """An entry that no longer matches anything is a claim about code that
        is gone - it would quietly excuse the next receiver to reuse the
        name."""
        gone = set(ALLOWED) - _raw_write_text_calls()
        assert not gone, f"{gone}: no longer exists; drop it from ALLOWED"
