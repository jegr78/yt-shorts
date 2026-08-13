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
        """Bytes, so an ascii/escape encoding cannot hide behind a decode.
        Line endings normalised out: the text handle writes CRLF on Windows,
        here and before."""
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "Nürburgring\n")
        assert path.read_bytes().replace(b"\r\n", b"\n") == "Nürburgring\n".encode("utf-8")

    def test_no_scratch_file_survives_a_successful_write(self, tmp_path):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "hello\n")
        assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


class TestAFailedWriteChangesNothing:

    @staticmethod
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    def test_the_previous_file_survives_byte_for_byte(self, tmp_path, monkeypatch):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "old\n")
        before = path.read_bytes()
        monkeypatch.setattr(os, "replace", self._boom)

        with pytest.raises(OSError):
            atomicwrite.write_text(path, "new\n")

        assert path.read_bytes() == before

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


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX permission bits are not meaningful on Windows")
class TestAReplacementCarriesTheModeAcross:

    def _mode(self, path: Path) -> int:
        return path.stat().st_mode & 0o777

    def test_an_existing_file_keeps_the_mode_it_had(self, tmp_path):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "old\n")
        path.chmod(0o640)

        atomicwrite.write_text(path, "new\n")

        assert self._mode(path) == 0o640

    def test_a_hand_tightened_file_is_not_widened(self, tmp_path):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "old\n")
        path.chmod(0o600)

        atomicwrite.write_text(path, "new\n")

        assert self._mode(path) & 0o077 == 0

    def test_a_new_file_is_owner_only(self, tmp_path):
        path = tmp_path / "x.json"
        atomicwrite.write_text(path, "x\n")
        assert self._mode(path) & 0o077 == 0


class TestTheScratchNameIsNotGuessable:
    def test_two_writes_use_different_scratch_names(self, tmp_path, monkeypatch):
        """A fixed `.part` name lets two writers share one scratch. The name
        also carries none of the target's, which is what keeps it out of
        py/path-injection."""
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
        assert all("x.json" not in name for name in seen)


class TestItStaysStdlibOnly:
    def test_it_imports_nothing_optional_and_nothing_from_this_project(self):
        """Over the AST, not the source text: a substring search would forbid
        the docstring from naming the constraint. Wider than the other admin
        guards - like logsetup, this imports nothing from the project."""
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
        assert imported <= {"__future__", "os", "shutil", "tempfile", "pathlib"}, imported


class TestWriteBytes:

    def test_it_writes_the_bytes_verbatim(self, tmp_path):
        path = tmp_path / "x.ttf"
        blob = bytes(range(256))
        atomicwrite.write_bytes(path, blob)
        assert path.read_bytes() == blob

    def test_it_does_not_translate_line_endings(self, tmp_path):
        """CRLF translation is right for a config file, fatal for a font."""
        path = tmp_path / "x.ttf"
        atomicwrite.write_bytes(path, b"a\nb\r\nc")
        assert path.read_bytes() == b"a\nb\r\nc"

    def test_a_failed_write_leaves_the_previous_bytes(self, tmp_path, monkeypatch):
        path = tmp_path / "x.ttf"
        atomicwrite.write_bytes(path, b"old")
        monkeypatch.setattr(os, "replace", TestAFailedWriteChangesNothing._boom)

        with pytest.raises(OSError):
            atomicwrite.write_bytes(path, b"new")

        assert path.read_bytes() == b"old"
        assert [p.name for p in tmp_path.iterdir()] == ["x.ttf"]

    def test_it_carries_the_mode_across_like_write_text(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permission bits are not meaningful on Windows")
        path = tmp_path / "x.ttf"
        atomicwrite.write_bytes(path, b"old")
        path.chmod(0o640)
        atomicwrite.write_bytes(path, b"new")
        assert path.stat().st_mode & 0o777 == 0o640
