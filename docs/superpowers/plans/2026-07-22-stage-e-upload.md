# Stage E — Upload to YouTube Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upload a rendered short to the right YouTube channel as a *private* video, from the studio or the CLI, with OAuth, per-channel accounts, a re-upload guard, and quota awareness — every network and OAuth boundary injected so the whole thing tests offline.

**Architecture:** `auth.py` handles OAuth consent and per-channel-id token storage behind an injected `oauth` adapter (a thin wrapper over the Google library). `youtube_upload.py` builds video metadata (pure) and performs a resumable upload behind an injected `service`. Quota and the re-upload guard are small JSON-file trackers. The studio and CLI expose upload as a confirmed action; the Google libraries are an optional dependency, imported lazily.

**Tech Stack:** `google-api-python-client`, `google-auth-oauthlib` (new, optional, `.venv` only); existing Python stdlib; FastAPI + React/Vite/Mantine for the studio.

## Global Constraints

- `PYTHONPATH=src` is mandatory. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` — 552 tests pass at the start of this plan.
- **The Google libraries are an OPTIONAL dependency.** Nothing at module scope imports them; `auth.py` and `youtube_upload.py` import google **lazily inside functions/adapter methods**, exactly as `cmd_studio` imports FastAPI. `harvest`, `render`, `gallery`, `migrate`, and the studio's non-upload routes must keep working with them uninstalled, and their absence yields a "run `pip install …`" message, not a traceback.
- **Every OAuth and network boundary is injected.** `authorize`/`load_credentials` take an `oauth` adapter; `upload_short` takes a built `service`; quota takes a `now` clock. No test performs a real OAuth flow, a real upload, or any network call.
- **Default privacy is `private`, always.** `build_metadata` sets `privacyStatus="private"`; nothing in this stage uploads public or makes a video public.
- **No auto-publish, ever.** Upload is always an explicit act (a CLI `upload` invocation, or a confirmed studio action).
- **Secrets never enter the repo or any output.** `client_secret.json`, `token-*.json`, `quota.json` live in the workspace `auth/` dir; `.gitignore` gets explicit entries (Task 1); nothing logs or echoes them. When the resolved workspace root IS the repo (`Workspace.origin == "repository"`), writing a secret warns.
- **One failed upload never aborts a run**; the re-upload guard prevents a second upload of the same clip without an explicit force.
- Tests must not depend on `~/YT-Shorts-Data`; `tests/conftest.py` pins `profile.CHANNELS_DIR` to `tests/fixtures/channels`.
- English only. Imperative commit messages. Do not modify `.venv` beyond installing the two named libraries, ffmpeg, or `/Users/jegr/racecast/` (read-only).

---

## Task 1: Dependencies, gitignore, and the optional-import guard

**Files:**
- Modify: `.gitignore`, `README.md` (deps note)
- Create: `src/yt_shorts/_google.py`
- Test: `tests/test_google_guard.py`

**Interfaces:**
- Produces: `_google.require(feature: str)` — raises `_google.GoogleUnavailable` with an install message when the Google libraries are missing; used by the CLI/studio to turn a missing dependency into a readable message rather than an `ImportError` at an awkward spot.

- [ ] **Step 1: Install the libraries**

```bash
.venv/bin/pip install google-api-python-client google-auth-oauthlib
```

- [ ] **Step 2: Add the gitignore entries**

Append to `.gitignore`:

```
# Stage E upload secrets. In normal use the workspace (and therefore auth/)
# is outside the repo (YT_SHORTS_DATA / ~/YT-Shorts-Data), but workspace.py's
# last-resort fallback is the repo's own channels/, so guard against a secret
# ever being staged from there.
auth/
client_secret*.json
token-*.json
quota.json
```

- [ ] **Step 3: Write the failing test**

`tests/test_google_guard.py`:

```python
import pytest

from yt_shorts._google import GoogleUnavailable, require


class TestRequire:
    def test_raises_with_an_install_message_when_missing(self, monkeypatch):
        import yt_shorts._google as g
        monkeypatch.setattr(g, "_import_google", lambda: (_ for _ in ()).throw(ImportError("no google")))
        with pytest.raises(GoogleUnavailable) as error:
            require("upload")
        assert "pip install" in str(error.value)
        assert "upload" in str(error.value)

    def test_returns_quietly_when_present(self, monkeypatch):
        import yt_shorts._google as g
        monkeypatch.setattr(g, "_import_google", lambda: None)
        require("upload")   # no raise
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_google_guard.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts._google'`

- [ ] **Step 5: Write the implementation**

`src/yt_shorts/_google.py`:

```python
"""Optional-dependency guard for the Google upload libraries.

The Google client is only needed for stage E (upload). Like FastAPI for the
studio, it is imported LAZILY so harvest/render/gallery/migrate run in a venv
that never installed it; this turns a missing library into a readable message.
"""

from __future__ import annotations

INSTALL = (".venv/bin/pip install google-api-python-client google-auth-oauthlib")


class GoogleUnavailable(Exception):
    """The Google upload libraries are not installed."""


def _import_google() -> None:
    import google_auth_oauthlib.flow  # noqa: F401
    import googleapiclient.discovery  # noqa: F401


def require(feature: str) -> None:
    """Raises GoogleUnavailable with an install hint if the libraries are absent."""
    try:
        _import_google()
    except ImportError as error:
        raise GoogleUnavailable(
            f"{feature} needs the Google libraries, which are not installed in "
            f"this venv.\nInstall them with: {INSTALL}\n"
            f"Every other command works without them.\n({error})"
        ) from error
```

- [ ] **Step 6: Run the test, then the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_google_guard.py -q` → PASS
Run: `PYTHONPATH=src .venv/bin/pytest -q` → 554 passed (552 + 2)

- [ ] **Step 7: Commit**

```bash
git add .gitignore src/yt_shorts/_google.py tests/test_google_guard.py README.md
git commit -m "Add the optional Google-library guard and gitignore upload secrets"
```

---

## Task 2: The auth adapter seam and token store

**Files:**
- Create: `src/yt_shorts/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces:
  - `auth.AuthError`
  - `auth.TokenStore(auth_dir)` — `path(channel_id) -> Path`, `load(channel_id) -> str | None`, `save(channel_id, text: str) -> None`; pure JSON-text file I/O keyed by channel id, no Google types
  - `auth.load_credentials(channel_id, *, auth_dir, oauth, store=None) -> object | None` — returns fresh credentials for the channel, refreshing if needed, or `None` if no token is stored
  - `auth.authorize(channel_id, *, auth_dir, oauth, store=None) -> object` — returns valid credentials, running consent through `oauth` and storing the token when there is none
  - The `oauth` adapter protocol (duck-typed; production impl in Task 3): `run_consent(client_secret_path, scopes) -> creds`, `to_json(creds) -> str`, `from_json(text) -> creds`, `valid(creds) -> bool`, `ensure_fresh(creds) -> creds`
  - `auth.SCOPES` — `["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]`

The point of the adapter: `auth.py` is pure orchestration over file I/O and the adapter, so every path tests with a fake adapter and no Google, no network.

- [ ] **Step 1: Write the failing test**

```python
import json

import pytest

from yt_shorts.auth import AuthError, TokenStore, authorize, load_credentials


class FakeCreds:
    def __init__(self, token, valid=True, refreshed=False):
        self.token = token
        self._valid = valid
        self.refreshed = refreshed


class FakeOAuth:
    """A stand-in for the Google adapter: no network, no Google types."""
    def __init__(self, consent_token="fresh"):
        self.consent_token = consent_token
        self.consented = 0

    def run_consent(self, client_secret_path, scopes):
        self.consented += 1
        return FakeCreds(self.consent_token)

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

    def test_load_absent_is_none(self, tmp_path):
        assert TokenStore(tmp_path).load("UCabc") is None


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.auth'`

- [ ] **Step 3: Write the implementation**

```python
"""OAuth credentials and per-channel token storage for uploads.

All Google interaction is behind an injected `oauth` adapter (production impl in
yt_shorts.google_oauth, which imports the Google library lazily), so this module
is pure orchestration over file I/O and that adapter and tests need no network,
no Google, no real consent. Tokens are keyed by YouTube channel id: authorizing
'switches account' by granting the right Google account's consent for a channel.
"""

from __future__ import annotations

import json
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

CLIENT_SECRET = "client_secret.json"


class AuthError(Exception):
    """Understandable message about a failed or missing authorization."""


class TokenStore:
    def __init__(self, auth_dir):
        self.auth_dir = Path(auth_dir)

    def path(self, channel_id: str) -> Path:
        return self.auth_dir / f"token-{channel_id}.json"

    def load(self, channel_id: str) -> str | None:
        path = self.path(channel_id)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def save(self, channel_id: str, text: str) -> None:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.path(channel_id).write_text(text, encoding="utf-8")


def _client_secret_path(auth_dir: Path) -> Path:
    path = Path(auth_dir) / CLIENT_SECRET
    if not path.exists():
        raise AuthError(
            f"No {CLIENT_SECRET} in {auth_dir}. Create an OAuth client in your "
            "Google Cloud project and place its client_secret.json there (see "
            "README, 'Upload')."
        )
    return path


def load_credentials(channel_id, *, auth_dir, oauth, store=None):
    """Fresh credentials for the channel, refreshing a stale token, or None."""
    store = store or TokenStore(auth_dir)
    text = store.load(channel_id)
    if text is None:
        return None
    creds = oauth.from_json(text)
    if not oauth.valid(creds):
        creds = oauth.ensure_fresh(creds)
        store.save(channel_id, oauth.to_json(creds))
    return creds


def authorize(channel_id, *, auth_dir, oauth, store=None):
    """Valid credentials for the channel, running consent and storing on first use."""
    store = store or TokenStore(auth_dir)
    existing = load_credentials(channel_id, auth_dir=auth_dir, oauth=oauth, store=store)
    if existing is not None:
        return existing
    client_secret = _client_secret_path(Path(auth_dir))
    creds = oauth.run_consent(client_secret, SCOPES)
    store.save(channel_id, oauth.to_json(creds))
    return creds
```

- [ ] **Step 4: Run the test, then the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_auth.py -q` → PASS
Run: `PYTHONPATH=src .venv/bin/pytest -q` → previous + 9

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/auth.py tests/test_auth.py
git commit -m "Store and refresh per-channel upload credentials behind an injected adapter"
```

---

## Task 3: The production Google adapter

**Files:**
- Create: `src/yt_shorts/google_oauth.py`
- Test: `tests/test_google_oauth.py` (import-guarded — skips cleanly if google is absent, since this is the one module that really wraps it)

**Interfaces:**
- Produces:
  - `google_oauth.GoogleOAuth` — the production `oauth` adapter implementing `run_consent`/`to_json`/`from_json`/`valid`/`ensure_fresh`, importing the Google library lazily in each method
  - `google_oauth.build_service(credentials)` — builds the authenticated `youtube` v3 API client (lazy import)

This is a thin wrapper; the orchestration it serves is already tested in Task 2 with a fake. Its own test only checks that constructing it imports nothing at module scope and that its methods exist.

- [ ] **Step 1: Write the failing test**

```python
class TestGoogleOAuthIsLazy:
    def test_importing_the_module_does_not_import_google(self):
        import sys
        # the module must import cleanly even reasoning about google being absent;
        # constructing the adapter must not require google at construction time
        from yt_shorts.google_oauth import GoogleOAuth
        adapter = GoogleOAuth()
        assert hasattr(adapter, "run_consent") and hasattr(adapter, "ensure_fresh")
```

- [ ] **Step 2: Run to verify it fails** (`ModuleNotFoundError`).

- [ ] **Step 3: Write the implementation**

```python
"""Production Google adapter for yt_shorts.auth (imports Google lazily).

Every method imports the Google library at call time, so importing this module,
and constructing GoogleOAuth, needs nothing installed - the orchestration in
yt_shorts.auth stays usable and testable without google, and only an actual
consent/refresh/upload touches it.
"""

from __future__ import annotations

from pathlib import Path


class GoogleOAuth:
    def run_consent(self, client_secret_path: Path, scopes):
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), scopes)
        return flow.run_local_server(port=0)

    def to_json(self, creds) -> str:
        return creds.to_json()

    def from_json(self, text: str):
        from google.oauth2.credentials import Credentials
        import json
        return Credentials.from_authorized_user_info(json.loads(text))

    def valid(self, creds) -> bool:
        return bool(getattr(creds, "valid", False))

    def ensure_fresh(self, creds):
        from google.auth.transport.requests import Request
        if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            creds.refresh(Request())
        return creds


def build_service(credentials):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=credentials)
```

- [ ] **Step 4: Run the test, then the full suite.**
- [ ] **Step 5: Commit**: `Add the production Google OAuth adapter and service builder`

---

## Task 4: Video metadata (pure)

**Files:**
- Create: `src/yt_shorts/youtube_upload.py`
- Test: `tests/test_youtube_upload.py`

**Interfaces:**
- Produces: `youtube_upload.build_metadata(clip: dict, edit, config: dict) -> dict` — the `videos.insert` request body (`snippet` + `status`), pure

- [ ] **Step 1: Write the failing test**

```python
from yt_shorts.editorial import Edit
from yt_shorts.youtube_upload import build_metadata


def _edit(title=None):
    return Edit(title=title, status="kept", transcript=None)


class TestBuildMetadata:
    def test_title_is_the_effective_hook(self):
        clip = {"hook": "harvested", "source_title": "ERF 24h"}
        body = build_metadata(clip, _edit(title="Corrected title"), {})
        assert body["snippet"]["title"] == "Corrected title"

    def test_title_falls_back_to_the_hook(self):
        clip = {"hook": "CRASH at Turn 1", "source_title": "ERF 24h"}
        assert build_metadata(clip, _edit(), {})["snippet"]["title"] == "CRASH at Turn 1"

    def test_privacy_is_always_private(self):
        body = build_metadata({"hook": "x", "source_title": "y"}, _edit(),
                              {"upload": {"privacy": "public"}})   # config cannot override
        assert body["status"]["privacyStatus"] == "private"

    def test_made_for_kids_is_always_present_and_defaults_false(self):
        body = build_metadata({"hook": "x", "source_title": "y"}, _edit(), {})
        assert body["status"]["selfDeclaredMadeForKids"] is False

    def test_description_template_interpolates_source_title_and_title(self):
        clip = {"hook": "Speedy!", "source_title": "ERF 24h Part 1"}
        config = {"upload": {"description": "From {source_title}. {title}"}}
        body = build_metadata(clip, _edit(), config)
        assert body["snippet"]["description"] == "From ERF 24h Part 1. Speedy!"

    def test_category_defaults_to_gaming(self):
        body = build_metadata({"hook": "x", "source_title": "y"}, _edit(), {})
        assert body["snippet"]["categoryId"] == "20"

    def test_tags_come_from_config_and_default_empty(self):
        assert build_metadata({"hook": "x", "source_title": "y"}, _edit(), {})["snippet"]["tags"] == []
        body = build_metadata({"hook": "x", "source_title": "y"}, _edit(),
                              {"upload": {"tags": ["simracing", "erf"]}})
        assert body["snippet"]["tags"] == ["simracing", "erf"]
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Write the implementation**

```python
"""Build a YouTube upload and perform it (resumable) behind an injected service.

build_metadata is pure. upload_short takes an already-built API service, injected
so tests never touch google or the network. Privacy is ALWAYS private here - this
stage never uploads public, and config cannot override that.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import editorial

DEFAULT_CATEGORY = "20"  # Gaming - sim racing sits here more naturally than Sports
DEFAULT_DESCRIPTION = "Clip from {source_title}."


def build_metadata(clip: dict, edit, config: dict) -> dict:
    upload = config.get("upload", {}) if isinstance(config, dict) else {}
    title = editorial.effective_title(edit, clip.get("hook", ""))
    template = upload.get("description", DEFAULT_DESCRIPTION)
    description = template.format(
        source_title=clip.get("source_title", ""), title=title)
    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": list(upload.get("tags", [])),
            "categoryId": str(upload.get("category_id", DEFAULT_CATEGORY)),
        },
        "status": {
            "privacyStatus": "private",   # always - never overridable
            "selfDeclaredMadeForKids": bool(upload.get("made_for_kids", False)),
        },
    }
```

- [ ] **Step 4: Run the test, then the full suite.**
- [ ] **Step 5: Commit**: `Build private-by-default upload metadata from a clip`

---

## Task 5: The resumable upload (injected service)

**Files:**
- Modify: `src/yt_shorts/youtube_upload.py`
- Test: `tests/test_youtube_upload.py`

**Interfaces:**
- Produces:
  - `youtube_upload.UploadError`
  - `youtube_upload.UploadResult` — `video_id: str`, `url: str`
  - `youtube_upload.upload_short(short_path, metadata, *, service, media_factory=None) -> UploadResult` — resumable `videos.insert` via the injected `service`; returns the new id and watch URL

- [ ] **Step 1: Write the failing test**

```python
import pytest

from yt_shorts.youtube_upload import UploadError, UploadResult, upload_short


class FakeInsert:
    def __init__(self, result=None, error=None):
        self._result = result or {"id": "VID123"}
        self._error = error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class FakeVideos:
    def __init__(self, insert):
        self._insert = insert
        self.called_with = None

    def insert(self, part, body, media_body):
        self.called_with = {"part": part, "body": body, "media": media_body}
        return self._insert


class FakeService:
    def __init__(self, insert):
        self._videos = FakeVideos(insert)

    def videos(self):
        return self._videos


def _meta():
    return {"snippet": {"title": "t"}, "status": {"privacyStatus": "private"}}


class TestUploadShort:
    def test_returns_the_video_id_and_url(self, tmp_path):
        short = tmp_path / "short.mp4"
        short.write_bytes(b"x")
        service = FakeService(FakeInsert({"id": "VID123"}))
        result = upload_short(short, _meta(), service=service,
                             media_factory=lambda p: f"media:{p}")
        assert isinstance(result, UploadResult)
        assert result.video_id == "VID123"
        assert result.url == "https://www.youtube.com/watch?v=VID123"

    def test_sends_snippet_and_status_parts(self, tmp_path):
        short = tmp_path / "short.mp4"
        short.write_bytes(b"x")
        service = FakeService(FakeInsert())
        upload_short(short, _meta(), service=service, media_factory=lambda p: p)
        assert "snippet" in service._videos.called_with["part"]
        assert "status" in service._videos.called_with["part"]

    def test_a_quota_error_becomes_an_upload_error(self, tmp_path):
        short = tmp_path / "short.mp4"
        short.write_bytes(b"x")
        service = FakeService(FakeInsert(error=RuntimeError("quotaExceeded")))
        with pytest.raises(UploadError) as error:
            upload_short(short, _meta(), service=service, media_factory=lambda p: p)
        assert "quota" in str(error.value).lower()
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Write the implementation** (append to `youtube_upload.py`)

```python
@dataclass
class UploadResult:
    video_id: str
    url: str


class UploadError(Exception):
    """Understandable message about a failed upload."""


def _default_media(short_path):
    # Real resumable media; imported lazily so tests inject their own factory.
    from googleapiclient.http import MediaFileUpload
    return MediaFileUpload(str(short_path), resumable=True)


def upload_short(short_path, metadata, *, service, media_factory=None) -> UploadResult:
    """Resumable videos.insert via the injected service. Never touches google in tests."""
    media = (media_factory or _default_media)(short_path)
    try:
        request = service.videos().insert(
            part=",".join(metadata.keys()), body=metadata, media_body=media)
        response = request.execute()
    except UploadError:
        raise
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        message = str(error)
        if "quota" in message.lower():
            raise UploadError(
                "today's upload quota is used up; it resets at midnight Pacific"
            ) from error
        raise UploadError(f"upload failed: {message}") from error
    video_id = response.get("id")
    if not video_id:
        raise UploadError(f"upload returned no video id: {response!r}")
    return UploadResult(video_id=video_id,
                        url=f"https://www.youtube.com/watch?v={video_id}")
```

Note: production callers loop `request.next_chunk()` for progress; `.execute()` performs the whole resumable upload in one call, which is correct and simplest for a single short. The studio job (Task 8) can switch to a `next_chunk` loop for progress reporting without changing this signature — document that in the docstring.

- [ ] **Step 4: Run the test, then the full suite.**
- [ ] **Step 5: Commit**: `Upload a short with a resumable insert behind an injected service`

---

## Task 6: The re-upload guard

**Files:**
- Create: `src/yt_shorts/upload_record.py`
- Test: `tests/test_upload_record.py`

**Interfaces:**
- Produces:
  - `upload_record.record_path(clip_dir) -> Path` (`upload.json`)
  - `upload_record.load(clip_dir) -> dict | None`
  - `upload_record.save(clip_dir, video_id, url, privacy, *, when: str) -> None`
  - `upload_record.is_uploaded(clip_dir) -> bool`

`upload.json` records an action, not derived or editorial data, so it is its own file (see the design). `when` is injected (an ISO string) rather than read from the clock, so tests are deterministic — the project bans `Date.now()`-style calls in workflow scripts and prefers injected time here for the same determinism.

- [ ] **Step 1: Write the failing test**

```python
from yt_shorts.upload_record import is_uploaded, load, record_path, save


class TestUploadRecord:
    def test_absent_means_not_uploaded(self, tmp_path):
        assert is_uploaded(tmp_path) is False
        assert load(tmp_path) is None

    def test_save_then_is_uploaded(self, tmp_path):
        save(tmp_path, "VID123", "https://youtu.be/VID123", "private", when="2026-07-22T10:00:00Z")
        assert is_uploaded(tmp_path) is True
        record = load(tmp_path)
        assert record["video_id"] == "VID123"
        assert record["privacy"] == "private"
        assert record["uploaded_at"] == "2026-07-22T10:00:00Z"

    def test_record_path_is_upload_json(self, tmp_path):
        assert record_path(tmp_path).name == "upload.json"
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Write the implementation**

```python
"""Record that a clip was uploaded, so it is not uploaded twice by accident.

upload.json records an ACTION - it is neither derived (cannot be recreated) nor
editorial (not a human's correction), so it is its own file. An upload has an
irreversible external effect and costs scarce quota; the studio and CLI read this
to show 'uploaded' and refuse a second upload without an explicit force.
"""

from __future__ import annotations

import json
from pathlib import Path

RECORD_FILENAME = "upload.json"


def record_path(clip_dir) -> Path:
    return Path(clip_dir) / RECORD_FILENAME


def load(clip_dir) -> dict | None:
    path = record_path(clip_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_uploaded(clip_dir) -> bool:
    return load(clip_dir) is not None


def save(clip_dir, video_id, url, privacy, *, when: str) -> None:
    record_path(clip_dir).write_text(json.dumps({
        "video_id": video_id, "url": url, "privacy": privacy, "uploaded_at": when,
    }, indent=2), encoding="utf-8")
```

- [ ] **Step 4: Run the test, then the full suite.**
- [ ] **Step 5: Commit**: `Record uploads so a clip is not uploaded twice`

---

## Task 7: Quota tracking

**Files:**
- Create: `src/yt_shorts/quota.py`
- Test: `tests/test_quota.py`

**Interfaces:**
- Produces:
  - `quota.INSERT_COST = 1600`, `quota.DAILY_DEFAULT = 10000`
  - `quota.QuotaTracker(auth_dir, channel_id, *, daily=DAILY_DEFAULT)`
  - `.remaining_uploads(now) -> int`, `.book_insert(now) -> None`, `.spent_today(now) -> int`
  - the day boundary is **Pacific**; `now` is an injected timezone-aware datetime

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone

from yt_shorts.quota import INSERT_COST, QuotaTracker


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class TestQuota:
    def test_starts_empty(self, tmp_path):
        q = QuotaTracker(tmp_path, "UCabc")
        assert q.spent_today(utc(2026, 7, 22, 18)) == 0

    def test_booking_an_insert_spends_the_cost(self, tmp_path):
        q = QuotaTracker(tmp_path, "UCabc")
        q.book_insert(utc(2026, 7, 22, 18))
        assert q.spent_today(utc(2026, 7, 22, 18)) == INSERT_COST

    def test_remaining_uploads_reflects_the_default_quota(self, tmp_path):
        q = QuotaTracker(tmp_path, "UCabc")
        assert q.remaining_uploads(utc(2026, 7, 22, 18)) == 10000 // INSERT_COST  # 6
        q.book_insert(utc(2026, 7, 22, 18))
        assert q.remaining_uploads(utc(2026, 7, 22, 18)) == 5

    def test_resets_at_the_pacific_day_boundary(self, tmp_path):
        q = QuotaTracker(tmp_path, "UCabc")
        # 2026-07-22 06:00 UTC is 2026-07-21 23:00 Pacific (PDT, UTC-7)
        q.book_insert(utc(2026, 7, 22, 6))
        assert q.spent_today(utc(2026, 7, 22, 6)) == INSERT_COST
        # 2026-07-22 08:00 UTC is 2026-07-22 01:00 Pacific - a new Pacific day
        assert q.spent_today(utc(2026, 7, 22, 8)) == 0

    def test_two_channels_track_separately(self, tmp_path):
        a = QuotaTracker(tmp_path, "UCaaa")
        b = QuotaTracker(tmp_path, "UCbbb")
        a.book_insert(utc(2026, 7, 22, 18))
        assert b.spent_today(utc(2026, 7, 22, 18)) == 0
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Write the implementation**

```python
"""A local per-day estimate of upload quota, per channel account.

videos.insert costs ~1600 units against a default 10,000/day (~6 uploads). The
count is a LOCAL ESTIMATE - the API is the authority - so it only WARNS, never
blocks an upload the API would accept. The day boundary is Pacific, where
YouTube's quota resets. `now` is injected so the reset is testable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

INSERT_COST = 1600
DAILY_DEFAULT = 10000
PACIFIC = ZoneInfo("America/Los_Angeles")


def _pacific_day(now: datetime) -> str:
    return now.astimezone(PACIFIC).strftime("%Y-%m-%d")


class QuotaTracker:
    def __init__(self, auth_dir, channel_id, *, daily: int = DAILY_DEFAULT):
        self.path = Path(auth_dir) / "quota.json"
        self.channel_id = channel_id
        self.daily = daily

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def spent_today(self, now: datetime) -> int:
        data = self._read().get(self.channel_id, {})
        return int(data.get(_pacific_day(now), 0))

    def book_insert(self, now: datetime) -> None:
        data = self._read()
        day = _pacific_day(now)
        channel = data.setdefault(self.channel_id, {})
        channel[day] = int(channel.get(day, 0)) + INSERT_COST
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def remaining_uploads(self, now: datetime) -> int:
        return max(0, (self.daily - self.spent_today(now)) // INSERT_COST)
```

- [ ] **Step 4: Run the test, then the full suite.**
- [ ] **Step 5: Commit**: `Track a local per-day upload-quota estimate, Pacific reset`

---

## Task 8: CLI and studio wiring

**Files:**
- Modify: `bin/yt-shorts`, `src/yt_shorts/studio/jobs.py`, `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_api.py`, `tests/test_studio_jobs.py`, and a CLI test if one exists for command dispatch

**Interfaces:**
- Produces:
  - CLI `auth <channel>` and `upload <channel>/<event>` commands (guarded by `_google.require`)
  - `GET /api/auth` → `{connected: bool, channel_id, remaining_uploads}`
  - `POST /api/clips/{name}/upload` → starts an upload job (kept + rendered clips only), refusing a clip already uploaded unless `?force=true`, honouring the re-upload guard and booking quota; reuses the job surface
  - `start_upload_job(profile, job_store, name, *, force, uploader=..., now=...)` in `studio/jobs.py`, with the uploader (auth + build_service + upload_short + record + quota) injected so the route tests without google

- [ ] **Step 1: Write the failing test** (studio API, stubbed uploader)

```python
class TestUploadRoute:
    def test_upload_starts_a_job_for_a_kept_rendered_clip(self, event_dir, client, monkeypatch):
        import yt_shorts.studio.api as api
        from yt_shorts import clipstore, editorial
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"pretend mp4")
        editorial.save(directory, editorial.Edit(title=None, status="kept", transcript=None))
        monkeypatch.setattr(api, "start_upload_job",
                            lambda *a, **k: type("J", (), {"id": "job1"})())
        r = client.post(f"/api/clips/{directory.name}/upload")
        assert r.status_code in (200, 202)
        assert r.json()["job_id"] == "job1"

    def test_upload_refuses_a_clip_that_is_not_kept(self, event_dir, client):
        from yt_shorts import clipstore, editorial
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "x", "source_title": "y", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"mp4")
        editorial.save(directory, editorial.Edit(title=None, status="candidate", transcript=None))
        r = client.post(f"/api/clips/{directory.name}/upload")
        assert r.status_code == 409

    def test_auth_status_reports_disconnected_without_a_token(self, client, monkeypatch):
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: None)
        body = client.get("/api/auth").json()
        assert body["connected"] is False
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Write the implementation**

- `bin/yt-shorts`: add `auth` and `upload` to the command dispatch. `auth <channel>` resolves the profile, calls `_google.require("upload")`, then `auth.authorize(channel["id"], auth_dir=<workspace>/auth, oauth=GoogleOAuth())` and prints which channel connected. `upload <channel>/<event>` iterates kept+rendered+not-uploaded clips, builds metadata, uploads, records, books quota, one failure never aborting — same structure as `cmd_render`. Both import google-touching modules lazily and report `_google.require` failures cleanly.
- `studio/jobs.py`: `start_upload_job(profile, job_store, name, *, force=False, uploader=_default_uploader, now=...)`. `_default_uploader` composes `load_credentials`→`build_service`→`build_metadata`→`upload_short`→`upload_record.save`→`quota.book_insert`. Guards: the clip must be `kept` and have `short.mp4`; if `is_uploaded` and not `force`, raise a `LockError`-like refusal the route turns into 409. Runs in a background thread like the render job. Takes the event lock the same way (an upload mutating `upload.json` while a render writes `short.mp4` should not race).
- `studio/api.py`: `GET /api/auth` (connected + remaining_uploads via `load_credentials` + `QuotaTracker`, both stubbable at module scope), `POST /api/clips/{name}/upload` (kept+rendered check → 409 otherwise; `force` query; `start_upload_job`).

- [ ] **Step 4: Run the tests, then the full suite.**
- [ ] **Step 5: Confirm optional-dependency**: `moments.py`… no — confirm `auth.py`, `youtube_upload.py`, `upload_record.py`, `quota.py` import no google at module scope: `PYTHONPATH=src .venv/bin/python -c "import sys; import yt_shorts.auth, yt_shorts.youtube_upload, yt_shorts.upload_record, yt_shorts.quota; assert 'googleapiclient' not in sys.modules and 'google_auth_oauthlib' not in sys.modules; print('clean')"`.
- [ ] **Step 6: Commit**: `Add auth and upload to the CLI and studio`

---

## Task 9: The studio upload UI

**Files:** `src/yt_shorts/studio/web/` (rebuild `static/`), `tests/test_studio_e2e.py`

Frontend task, built to the API contract from Task 8, in the studio's existing neutral timing-tower look (see the studio redesign brief; NOT ERF colours). Dispatched to a focused frontend agent with the API contract as its brief.

**What to build:**
- **Auth status** — `GET /api/auth`: connected/disconnected for the channel, remaining uploads today, and a "Connect account" affordance for the disconnected state (it explains consent happens in the operator's own browser via the CLI `auth` command — the studio does not run the browser flow itself; it surfaces the state and the instruction).
- **Upload action** — on a clip that is **kept** and has a rendered short: show the exact metadata (title, description, tags, privacy: **private**) for confirmation, upload on confirm via `POST /api/clips/{name}/upload`, poll the job, then show the resulting private video URL. A clip with an upload record shows "uploaded" with its URL and requires explicit confirmation (a re-upload toggle → `?force=true`) to upload again.
- **Quota indicator** — remaining uploads today, from `/api/auth`.

**Acceptance (E2E, real Chromium):** with the upload job stubbed to record an upload, a kept+rendered clip shows an Upload action, confirming it starts a job, and on completion the clip shows "uploaded" with the URL (assert `upload.json` via the Python layer). A candidate (not kept) clip shows no upload action.

**Verify:** `npm run build` typechecks; `static/` rebuilt and committed; full suite green incl. E2E; drive the real page and screenshot the upload confirmation and the uploaded state; report contrast ratios for new surfaces.

- [ ] Build to the above; update E2E selectors rather than weakening assertions; rebuild+commit `static/`.
- [ ] Commit: `Add the upload action and auth status to the studio`

---

## Task 10: Documentation

**Files:** `README.md`, `CLAUDE.md`

- [ ] **README** — an "Upload" section: the one-time Google Cloud + OAuth-client setup (operator creates it, places `client_secret.json` in `<workspace>/auth/`), `bin/yt-shorts auth <channel>` (browser consent, the tool never sees the password), that uploads are **private** by default and made public manually in YouTube Studio, the ~6/day quota, the re-upload guard, and the `upload` config block in `brand.json` (description template, tags, category_id, made_for_kids). State plainly: never move `client_secret.json` or the token files into the repo.
- [ ] **CLAUDE.md** — stage E's boundaries: the Google libraries are optional (lazy import, like FastAPI); all OAuth/network is injected; secrets live in `<workspace>/auth/` and are gitignored and never printed; privacy is always `private` and nothing auto-publishes; `upload.json` records an action (not derived, not editorial); quota is a local Pacific-day estimate that only warns. The pipeline arc (Stage 1 → 2a → A–C → D1/D2a/D2b → E) is complete.
- [ ] Commit: `Document the upload stage`

---

## Verification for the branch

- Full suite green, E2E included.
- **No real OAuth, upload, or network call anywhere in the suite** — every such boundary is injected; grep the new tests to confirm none construct a real `GoogleOAuth`/`build_service` against the network.
- **Optional dependency holds:** the four core Python modules import no google at module scope (Task 8 Step 5); the CLI's non-upload commands and the studio's non-upload routes run with the Google libraries uninstalled.
- **Secrets safety:** `.gitignore` covers `auth/`, `client_secret*.json`, `token-*.json`, `quota.json`; `git status` after a simulated auth writes nothing tracked; no test or code path prints a token or client secret.
- **Privacy invariant:** `build_metadata` always emits `privacyStatus="private"`, even when config says otherwise (tested).
- **No real end-to-end upload is performed by the tooling.** A genuine upload needs the operator's Google Cloud project, their browser consent, and would publish to a real channel — it stays the operator's explicit, authorized step. Verification is by injected boundaries plus (optionally, operator-run) a single real `auth` + private upload the operator performs and confirms out of band. Do NOT attempt a real upload from the implementation.

## Self-review notes

Checked against the spec:
- official library, optional/lazy — Tasks 1, 3, 8
- OAuth consent in the operator's browser, tool never handles the password — Tasks 2/3 (injected), README (Task 10)
- tokens keyed by YouTube channel id, "switch account" = authorize the right account — Task 2
- private by default, never overridable, no auto-publish — Task 4 (tested)
- resumable insert, injected service — Task 5
- re-upload guard (`upload.json`, its own file) — Task 6
- quota, Pacific reset, warns-not-blocks, per channel — Task 7
- studio + CLI, confirmed upload, quota indicator, auth status — Tasks 8, 9
- `upload` config block in brand.json (defaults) — Task 4
- secrets gitignored + repo-fallback warning — Task 1 (+ Task 8 for the warning at write time)
- every boundary injected; no network in tests — all tasks

Deferred with reason (not gaps): making a video public, scheduling, thumbnails, playlists, deleting — out of scope, manual in YouTube Studio; raising quota / content-owner uploads — out of scope.
