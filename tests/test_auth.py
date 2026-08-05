import json

import pytest

from yt_shorts.auth import (AuthError, TokenStore, authorize, forget_credentials,
                            load_credentials)


class FakeCreds:
    def __init__(self, token, valid=True, refreshed=False):
        self.token = token
        self._valid = valid
        self.refreshed = refreshed


class FakeOAuth:
    """A stand-in for the Google adapter: no network, no Google types.

    ``authorized_channel`` returns what channel the consent actually granted;
    ``granted_channel`` defaults to the id the tests authorize ("UCabc") so the
    happy path matches, and a test can set it to a different id to exercise the
    mismatch guard."""
    def __init__(self, consent_token="fresh", granted_channel="UCabc",
                 granted_title="Test Channel"):
        self.consent_token = consent_token
        self.granted_channel = granted_channel
        self.granted_title = granted_title
        self.consented = 0

    def run_consent(self, client_secret_path, scopes):
        self.consented += 1
        return FakeCreds(self.consent_token)

    def authorized_channel(self, creds):
        return self.granted_channel, self.granted_title

    def to_json(self, creds):
        return json.dumps({"token": creds.token, "valid": creds._valid})

    def from_json(self, text):
        d = json.loads(text)
        return FakeCreds(d["token"], valid=d["valid"])

    def valid(self, creds):
        return creds._valid

    def ensure_fresh(self, creds):
        if not creds._valid:
            return FakeCreds(creds.token, valid=True, refreshed=True)
        return creds


def _client_secret(auth_dir):
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "client_secret.json").write_text("{}", encoding="utf-8")


class TestTokenStore:
    def test_path_is_keyed_by_channel_id(self, tmp_path):
        store = TokenStore(tmp_path)
        assert store.path("UCabc").name == "token-UCabc.json"

    def test_save_then_load_round_trips(self, tmp_path):
        store = TokenStore(tmp_path)
        store.save("UCabc", '{"token": "x"}')
        assert store.load("UCabc") == '{"token": "x"}'

    def test_save_writes_token_0600_and_authdir_0700(self, tmp_path):
        # The refresh token must not be world/group readable.
        auth_dir = tmp_path / "auth"
        store = TokenStore(auth_dir)
        store.save("UCabc", '{"token": "x"}')
        assert (store.path("UCabc").stat().st_mode & 0o777) == 0o600
        assert (auth_dir.stat().st_mode & 0o777) == 0o700

    def test_load_absent_is_none(self, tmp_path):
        assert TokenStore(tmp_path).load("UCabc") is None

    @pytest.mark.parametrize("bad", ["../../etc/hosts", "..", "a/b", ".hidden"])
    def test_a_traversal_channel_id_is_refused(self, tmp_path, bad):
        # channel_id becomes a filename; a traversal id must never read/unlink
        # outside auth/ via load/save/forget.
        store = TokenStore(tmp_path)
        with pytest.raises(AuthError, match="not a valid"):
            store.path(bad)
        with pytest.raises(AuthError, match="not a valid"):
            store.load(bad)


class TestAuthorize:
    def test_first_time_runs_consent_and_stores_the_token(self, tmp_path):
        auth_dir = tmp_path / "auth"
        _client_secret(auth_dir)
        oauth = FakeOAuth()
        creds = authorize("UCabc", auth_dir=auth_dir, oauth=oauth)
        assert oauth.consented == 1
        assert creds.token == "fresh"
        assert TokenStore(auth_dir).load("UCabc") is not None

    def test_second_time_reuses_the_stored_token_without_consent(self, tmp_path):
        auth_dir = tmp_path / "auth"
        _client_secret(auth_dir)
        oauth = FakeOAuth()
        authorize("UCabc", auth_dir=auth_dir, oauth=oauth)
        oauth2 = FakeOAuth()
        creds = authorize("UCabc", auth_dir=auth_dir, oauth=oauth2)
        assert oauth2.consented == 0          # no second browser consent
        assert creds.token == "fresh"

    def test_a_stale_token_is_refreshed_and_saved(self, tmp_path):
        auth_dir = tmp_path / "auth"
        _client_secret(auth_dir)
        store = TokenStore(auth_dir)
        store.save("UCabc", json.dumps({"token": "old", "valid": False}))
        oauth = FakeOAuth()
        creds = load_credentials("UCabc", auth_dir=auth_dir, oauth=oauth)
        assert creds.refreshed is True

    def test_no_token_returns_none_from_load(self, tmp_path):
        auth_dir = tmp_path / "auth"
        _client_secret(auth_dir)
        assert load_credentials("UCabc", auth_dir=auth_dir, oauth=FakeOAuth()) is None

    def test_a_missing_client_secret_is_an_auth_error(self, tmp_path):
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        with pytest.raises(AuthError) as error:
            authorize("UCabc", auth_dir=auth_dir, oauth=FakeOAuth())
        assert "client_secret" in str(error.value)

    def test_consenting_as_the_wrong_channel_is_refused_and_not_stored(self, tmp_path):
        # The operator picked a different channel (e.g. their personal one) in
        # Google's consent screen than the one they asked to connect. The token
        # must NOT be stored under the requested id, and the error must name both.
        auth_dir = tmp_path / "auth"
        _client_secret(auth_dir)
        oauth = FakeOAuth(granted_channel="UCpersonal", granted_title="JeGr")
        with pytest.raises(AuthError) as error:
            authorize("UCabc", auth_dir=auth_dir, oauth=oauth)
        message = str(error.value)
        assert "UCpersonal" in message and "UCabc" in message and "JeGr" in message
        assert TokenStore(auth_dir).load("UCabc") is None   # nothing stored

    def test_consenting_as_the_right_channel_stores_the_token(self, tmp_path):
        auth_dir = tmp_path / "auth"
        _client_secret(auth_dir)
        oauth = FakeOAuth(granted_channel="UCabc", granted_title="ERF")
        authorize("UCabc", auth_dir=auth_dir, oauth=oauth)
        assert TokenStore(auth_dir).load("UCabc") is not None

    def test_force_re_consents_even_with_a_valid_token(self, tmp_path):
        auth_dir = tmp_path / "auth"
        _client_secret(auth_dir)
        # a valid token is already stored
        authorize("UCabc", auth_dir=auth_dir, oauth=FakeOAuth())
        # without force, a second call reuses it (no consent); with force it re-consents
        again = FakeOAuth()
        authorize("UCabc", auth_dir=auth_dir, oauth=again)
        assert again.consented == 0
        forced = FakeOAuth()
        authorize("UCabc", auth_dir=auth_dir, oauth=forced, force=True)
        assert forced.consented == 1

    def test_force_still_refuses_a_wrong_channel_consent(self, tmp_path):
        auth_dir = tmp_path / "auth"
        _client_secret(auth_dir)
        authorize("UCabc", auth_dir=auth_dir, oauth=FakeOAuth())   # good token stored
        wrong = FakeOAuth(granted_channel="UCpersonal", granted_title="JeGr")
        with pytest.raises(AuthError):
            authorize("UCabc", auth_dir=auth_dir, oauth=wrong, force=True)
        # the previously-good token is left intact (a refused force does not wipe it)
        assert TokenStore(auth_dir).load("UCabc") is not None


class TestForgetCredentials:
    def test_deletes_an_existing_token(self, tmp_path):
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        token = auth_dir / "token-UCabc.json"
        token.write_text("{}", encoding="utf-8")
        removed = forget_credentials("UCabc", auth_dir=auth_dir)
        assert removed is True
        assert not token.exists()

    def test_is_a_noop_when_absent(self, tmp_path):
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        assert forget_credentials("UCabc", auth_dir=auth_dir) is False

    def test_touches_only_the_token(self, tmp_path):
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        (auth_dir / "token-UCabc.json").write_text("{}", encoding="utf-8")
        secret = auth_dir / "client_secret.json"
        quota = auth_dir / "quota.json"
        secret.write_text("SECRET", encoding="utf-8")
        quota.write_text("{}", encoding="utf-8")
        forget_credentials("UCabc", auth_dir=auth_dir)
        assert secret.exists() and quota.exists()
