import os
from pathlib import Path

import pytest

from yt_shorts import atomicwrite


class TestItWritesTheWholeText:
    def test_a_new_file_holds_exactly_what_was_written(self, tmp_path):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, '{"a": 1}\n')
        assert path.read_text(encoding="utf-8") == '{"a": 1}\n'

    def test_an_existing_file_is_replaced_whole(self, tmp_path):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "old\n")
        atomicwrite.write_text(path, "new\n")
        assert path.read_text(encoding="utf-8") == "new\n"

    def test_it_writes_utf8_without_escaping(self, tmp_path):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "Nürburgring\n")
        assert path.read_bytes() == "Nürburgring\n".encode("utf-8")

    def test_no_scratch_file_survives_a_successful_write(self, tmp_path):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "hello\n")
        assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


class TestAFailedWriteChangesNothing:
    """The whole point: a reader must find the whole old file or the whole new
    one. `Path.write_text` truncates the target first, so a failure - or a
    reader arriving mid-write - sees an EMPTY file.
    """

    @staticmethod
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    def test_the_previous_file_survives_byte_for_byte(self, tmp_path, monkeypatch):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "old\n")
        monkeypatch.setattr(os, "replace", self._boom)

        with pytest.raises(OSError):
            atomicwrite.write_text(path, "new\n")

        assert path.read_bytes() == b"old\n"

    def test_the_scratch_file_is_cleaned_up(self, tmp_path, monkeypatch):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "old\n")
        monkeypatch.setattr(os, "replace", self._boom)

        with pytest.raises(OSError):
            atomicwrite.write_text(path, "new\n")

        assert [p.name for p in tmp_path.iterdir()] == ["x.json"]

    def test_a_target_that_did_not_exist_is_not_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr(os, "replace", self._boom)

        with pytest.raises(OSError):
            atomicwrite.write_text(tmp_path / "x.json", "new\n")

        assert list(tmp_path.iterdir()) == []


class TestThePermissionsAreTheOnesWriteTextWouldGive:
    """Measured against a reference file rather than against a literal mode:
    the answer is the process umask, which the suite must not assume. This is
    a regression guard - an earlier draft reached the same atomicity with
    `tempfile.mkstemp` (0600) plus an explicit `chmod 0o644`, which both
    changed the mode of a hand-tightened file and made CodeQL flag an
    explicitly world-readable mask.
    """

    def _mode(self, path: Path) -> int:
        return path.stat().st_mode & 0o777

    def test_a_new_file_matches_a_plain_write_text(self, tmp_path):
        reference = tmp_path / "reference.json"
        reference.write_text("x\n", encoding="utf-8")

        written = tmp_path / "written.json"
        atomicwrite.write_text(written, "x\n")

        assert self._mode(written) == self._mode(reference)

    def test_it_is_not_owner_only(self, tmp_path):
        """`glossary.json` and friends are plain config an operator reads, not
        a secret under `auth/` - see ownermode.py for the files that ARE."""
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "x\n")
        if os.name == "nt":
            pytest.skip("POSIX permission bits are not meaningful on Windows")
        assert self._mode(path) & 0o044


class TestTheScratchNameIsNotGuessable:
    def test_two_writes_use_different_scratch_names(self, tmp_path, monkeypatch):
        """A fixed `.part` name (what `workspace.write_settings` uses) lets two
        concurrent writers of the same file share one scratch and interleave
        into it. The name carries random bytes, so they cannot - and so a
        symlink cannot be planted at a name that is known in advance."""
        seen = []
        real_replace = os.replace

        def _record(src, dst):
            seen.append(Path(src).name)
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _record)
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "a\n")
        atomicwrite.write_text(path, "b\n")

        assert len(set(seen)) == 2
        assert all(name != "x.json.part" for name in seen)


class TestItStaysStdlibOnly:
    def test_it_imports_nothing_optional_and_nothing_from_this_project(self):
        """Checked over the IMPORT STATEMENTS via the AST, the way
        test_glossary_admin.py's own guard is - a substring search would
        forbid the docstring from naming the constraint it upholds. The
        project rule is wider here than there: `logsetup.py`-style, this must
        import nothing from `yt_shorts` either, so the CLI's venv can carry it
        whatever else it did or did not install."""
        import ast

        tree = ast.parse(Path(atomicwrite.__file__).read_text(encoding="utf-8"))
        imported = set()
        relative = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative.append(node)
                elif node.module:
                    imported.add(node.module.split(".")[0])
        assert relative == [], "must not import from this project"
        assert imported <= {"__future__", "os", "secrets", "pathlib"}, imported
