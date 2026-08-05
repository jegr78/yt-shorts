import pytest

from yt_shorts._google import GoogleUnavailable, require


class TestRequire:
    def test_raises_with_an_install_message_when_missing(self, monkeypatch):
        import yt_shorts._google as g
        monkeypatch.setattr(g, "_import_google",
                            lambda: (_ for _ in ()).throw(ImportError("no google")))
        with pytest.raises(GoogleUnavailable) as error:
            require("upload")
        assert "pip install" in str(error.value)
        assert "upload" in str(error.value)

    def test_returns_quietly_when_present(self, monkeypatch):
        import yt_shorts._google as g
        monkeypatch.setattr(g, "_import_google", lambda: None)
        require("upload")   # no raise
