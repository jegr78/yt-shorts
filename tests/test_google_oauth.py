import pytest


class TestGoogleOAuthIsLazy:
    def test_constructing_the_adapter_needs_no_google_and_has_the_methods(self):
        # Constructing GoogleOAuth must not require google at construction time -
        # only its methods import it, so yt_shorts.auth stays usable without the
        # libraries installed.
        from yt_shorts.google_oauth import GoogleOAuth
        adapter = GoogleOAuth()
        for method in ("run_consent", "to_json", "from_json", "valid", "ensure_fresh"):
            assert hasattr(adapter, method)


class TestConsentTimeout:
    def test_a_consent_timeout_becomes_a_clear_error_and_passes_the_timeout_through(
            self, monkeypatch, tmp_path):
        import google_auth_oauthlib.flow as flowmod
        from yt_shorts.google_oauth import GoogleOAuth

        captured = {}

        class FakeFlow:
            def run_local_server(self, **kwargs):
                captured.update(kwargs)
                raise flowmod.WSGITimeoutError("no request received")

        monkeypatch.setattr(flowmod.InstalledAppFlow, "from_client_secrets_file",
                            lambda path, scopes: FakeFlow())
        cs = tmp_path / "client_secret.json"
        cs.write_text("{}", encoding="utf-8")

        with pytest.raises(RuntimeError) as error:
            GoogleOAuth().run_consent(cs, ["scope"], timeout_seconds=7)
        # a hung/abandoned consent no longer hangs forever - it is a bounded,
        # understandable failure the connect job can record and show
        assert "7" in str(error.value)
        assert captured["timeout_seconds"] == 7
        assert captured["port"] == 0
