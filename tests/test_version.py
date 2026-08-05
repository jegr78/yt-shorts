"""The version resolver: what the package declares, and what a frozen build overrides it with."""

from pathlib import Path

from yt_shorts import version


class TestTheDeclaredVersion:
    def test_an_unfrozen_process_reports_the_packages_own_version(self, monkeypatch):
        monkeypatch.delattr("sys._MEIPASS", raising=False)
        from yt_shorts.__version__ import __version__ as declared

        assert version.resolve() == declared

    def test_there_is_no_bundle_path_when_not_frozen(self, monkeypatch):
        monkeypatch.delattr("sys._MEIPASS", raising=False)

        assert version.bundled_version_path() is None


class TestTheFrozenOverride:
    """tools/build-binary.py is handed the git TAG (`v1.2.0`) and writes it into
    the bundle, so a binary reports the tag it was built from - not the `1.2.0`
    in __version__.py. Without this the build's own smoke test could not check
    that the artefact knows what it is."""

    def test_a_bundled_version_file_wins(self, monkeypatch):
        monkeypatch.setattr("sys._MEIPASS", "/nowhere", raising=False)

        assert version.resolve(read=lambda path: "v9.9.9\n") == "v9.9.9"

    def test_the_bundle_path_sits_at_the_root_of_the_unpacked_bundle(self, monkeypatch):
        monkeypatch.setattr("sys._MEIPASS", "/nowhere", raising=False)

        assert version.bundled_version_path() == Path("/nowhere/VERSION")

    def test_an_unreadable_version_file_falls_back_rather_than_raising(self, monkeypatch):
        """A version string is not worth failing a command over."""
        monkeypatch.setattr("sys._MEIPASS", "/nowhere", raising=False)
        from yt_shorts.__version__ import __version__ as declared

        def explode(path):
            raise OSError("no such file")

        assert version.resolve(read=explode) == declared

    def test_an_empty_version_file_falls_back_too(self, monkeypatch):
        monkeypatch.setattr("sys._MEIPASS", "/nowhere", raising=False)
        from yt_shorts.__version__ import __version__ as declared

        assert version.resolve(read=lambda path: "  \n") == declared


class TestTheTwoFilesAgree:
    """version.txt is release-please's write target; __version__.py is what code
    reads. release-please updates both in one commit, so they must never drift."""

    def test_version_txt_matches_the_declared_version(self):
        from yt_shorts.__version__ import __version__ as declared

        root = Path(__file__).resolve().parent.parent
        assert (root / "version.txt").read_text(encoding="utf-8").strip() == declared
