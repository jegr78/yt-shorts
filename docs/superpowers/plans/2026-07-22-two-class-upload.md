# Two-Class Upload (owned vs. render-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every channel an explicit upload class — `api` (owned, the default, unchanged) or `manual` (render-only) — and make the CLI and studio behave correctly for both, offering render-only channels a download + copy-to-paste metadata instead of an API upload that would fail.

**Architecture:** One config flag `config["upload"]["mode"]` in brand.json, validated in `profile.py`. One shared, dependency-free predicate module `upload_policy.py` (`mode` / `is_render_only` / `require_api_upload`→`RenderOnlyError`) that both the CLI and the studio consult at four enforcement points. The studio frontend reads a new `upload_mode` field from `GET /api/auth` and renders download + copy-metadata controls (reusing the existing `/short` and `/upload-preview` endpoints) for manual channels.

**Tech Stack:** Python 3 (stdlib, no build), pytest; React + Vite + Mantine + TypeScript, Vitest; FastAPI (studio only, lazy).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation; run tests as `PYTHONPATH=src .venv/bin/pytest`.
- `upload_policy.py` imports nothing heavy — no FastAPI, no google — at module scope (mirrors `subtitle_pipeline.py`).
- Privacy stays always `private` on the API path; the re-upload guard and the verify-and-refuse auth check are unchanged.
- Default is `api`: an absent `upload` block, an absent `mode`, or any value other than the exact string `"manual"` resolves to `api` (existing behaviour), so a real owned channel can never be accidentally blocked.
- `upload.mode`'s only accepted values are `"api"` and `"manual"`; profile validation rejects anything else and collects it with all other profile defects (`ProfileError`), never raising on the first defect.
- The one operator-facing sentence for a render-only refusal is the module constant `upload_policy.RENDER_ONLY_MESSAGE`; every caller reuses it, none reinvents the wording.
- The frontend's pure logic lives in its own module (`upload.ts`), NOT exported from a component (keeps Vite's fast-refresh boundary component-only); it is unit-tested with Vitest. Rebuild and commit `static/` after any frontend change (`npm run build`).
- CLI functions live in `bin/yt-shorts` (no `.py`), loaded in tests via `SourceFileLoader` (`tests/test_cli.py`'s `_load_cli`/`cli` fixture).

---

### Task 1: `upload_policy.py` — the shared predicate

**Files:**
- Create: `src/yt_shorts/upload_policy.py`
- Test: `tests/test_upload_policy.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `RENDER_ONLY_MESSAGE: str`
  - `class RenderOnlyError(Exception)`
  - `mode(config: dict) -> str` — returns `"manual"` iff `config["upload"]["mode"] == "manual"`, else `"api"`.
  - `is_render_only(config: dict) -> bool`
  - `require_api_upload(config: dict) -> None` — no-op for api, raises `RenderOnlyError(RENDER_ONLY_MESSAGE)` for manual.

- [ ] **Step 1: Write the failing test**

Create `tests/test_upload_policy.py`:

```python
import pytest

from yt_shorts import upload_policy


class TestMode:
    def test_absent_upload_block_is_api(self):
        assert upload_policy.mode({}) == "api"

    def test_upload_block_without_mode_is_api(self):
        assert upload_policy.mode({"upload": {"tags": ["x"]}}) == "api"

    def test_explicit_api_is_api(self):
        assert upload_policy.mode({"upload": {"mode": "api"}}) == "api"

    def test_manual_is_manual(self):
        assert upload_policy.mode({"upload": {"mode": "manual"}}) == "manual"

    def test_unexpected_value_falls_back_to_api(self):
        # Never accidentally block a real owned channel; validation is what
        # rejects a bad value at load time, not this predicate.
        assert upload_policy.mode({"upload": {"mode": "bogus"}}) == "api"

    def test_non_dict_upload_is_api(self):
        assert upload_policy.mode({"upload": None}) == "api"


class TestGuard:
    def test_is_render_only_matches_mode(self):
        assert upload_policy.is_render_only({"upload": {"mode": "manual"}}) is True
        assert upload_policy.is_render_only({}) is False

    def test_require_api_upload_is_a_noop_for_api(self):
        upload_policy.require_api_upload({})  # must not raise

    def test_require_api_upload_refuses_manual_with_the_shared_message(self):
        with pytest.raises(upload_policy.RenderOnlyError) as error:
            upload_policy.require_api_upload({"upload": {"mode": "manual"}})
        assert str(error.value) == upload_policy.RENDER_ONLY_MESSAGE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_upload_policy.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.upload_policy'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/yt_shorts/upload_policy.py`:

```python
"""Which upload class a channel is, and the one guard that enforces it.

A channel is either API-uploadable ("api", the default) or render-only
("manual" - a YouTube manager/editor delegation the Data API cannot upload
to, so the tool renders the short and the operator uploads it by hand in
YouTube Studio). This module is the single source of that decision, imported
by both the CLI and the studio so the rule lives in exactly one place. It
imports nothing heavy (no FastAPI, no google) and reads only a plain config
dict - the same flat config profile.load produces.
"""

from __future__ import annotations

RENDER_ONLY_MESSAGE = (
    "channel is render-only (upload.mode=manual): the YouTube Data API cannot "
    "upload to a manager/editor channel. Render the short, download it, and "
    "upload it by hand in YouTube Studio."
)


class RenderOnlyError(Exception):
    """Raised when an API-upload path is reached for a render-only channel."""


def mode(config: dict) -> str:
    """"manual" only when explicitly set so; "api" (the default) otherwise.

    Any value other than the exact string "manual" - a missing 'upload'
    block, a missing 'mode', a non-dict, or an unexpected value - resolves to
    "api", i.e. the existing behaviour, so this can never accidentally block a
    real owned channel. profile._validate_upload is what rejects an unexpected
    value at load time.
    """
    upload = config.get("upload") if isinstance(config, dict) else None
    value = upload.get("mode") if isinstance(upload, dict) else None
    return "manual" if value == "manual" else "api"


def is_render_only(config: dict) -> bool:
    return mode(config) == "manual"


def require_api_upload(config: dict) -> None:
    """No-op for an api channel; raises RenderOnlyError for a manual one."""
    if is_render_only(config):
        raise RenderOnlyError(RENDER_ONLY_MESSAGE)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_upload_policy.py -q`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/upload_policy.py tests/test_upload_policy.py
git commit -m "Add upload_policy: the owned-vs-render-only predicate and guard"
```

---

### Task 2: Validate `upload.mode` in `profile.py`

**Files:**
- Modify: `src/yt_shorts/profile.py` (add `_validate_upload`; add it to the `problems` aggregation in `load`)
- Test: `tests/test_profile.py` (add a `TestValidateUpload` class)

**Interfaces:**
- Consumes: nothing from Task 1 (validation is independent; it rejects bad values so `upload_policy.mode` only ever sees good ones in practice).
- Produces: `profile._validate_upload(config: dict, path: Path) -> list[str]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_profile.py` (it already imports `from pathlib import Path` and `from yt_shorts import profile` — reuse those; if not present, add them):

```python
class TestValidateUpload:
    PATH = Path("brand.json")

    def test_absent_upload_is_fine(self):
        assert profile._validate_upload({}, self.PATH) == []

    def test_api_and_manual_are_accepted(self):
        assert profile._validate_upload({"upload": {"mode": "api"}}, self.PATH) == []
        assert profile._validate_upload({"upload": {"mode": "manual"}}, self.PATH) == []

    def test_upload_without_mode_is_fine(self):
        assert profile._validate_upload({"upload": {"tags": ["x"]}}, self.PATH) == []

    def test_unknown_mode_is_rejected_and_names_the_field(self):
        problems = profile._validate_upload({"upload": {"mode": "owner"}}, self.PATH)
        assert len(problems) == 1
        assert "upload.mode" in problems[0]
        assert "owner" in problems[0]

    def test_null_upload_is_rejected(self):
        problems = profile._validate_upload({"upload": None}, self.PATH)
        assert len(problems) == 1
        assert "upload" in problems[0]

    def test_non_object_upload_is_rejected(self):
        problems = profile._validate_upload({"upload": "manual"}, self.PATH)
        assert len(problems) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_profile.py::TestValidateUpload -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.profile' has no attribute '_validate_upload'`.

- [ ] **Step 3: Write minimal implementation**

In `src/yt_shorts/profile.py`, add `_validate_upload` next to the other `_validate_*` functions (e.g. after `_validate_subtitles`):

```python
def _validate_upload(config: dict, path: Path) -> list[str]:
    """The optional 'upload' section's class flag, if present, must be a known
    value. Absent 'upload' or absent 'mode' means "api" (see
    upload_policy.mode) - the default, which needs no declaration - and every
    existing profile that never wrote an 'upload' block keeps loading. An
    explicit null or a non-object is rejected here the same collect-all way
    _validate_subtitles rejects a null 'subtitles', so a typo is named in the
    one ProfileError alongside every other defect."""
    if "upload" not in config:
        return []
    upload = config["upload"]
    if upload is None:
        return [f"{path.name}: 'upload' must be an object, not null"]
    if not isinstance(upload, dict):
        return [f"{path.name}: 'upload' must be an object"]
    if "mode" in upload and upload["mode"] not in ("api", "manual"):
        return [
            f"{path.name}: 'upload.mode' must be \"api\" or \"manual\", "
            f"got {upload['mode']!r}"
        ]
    return []
```

Then add it to the `problems` aggregation in `load` (the block that already sums `_validate_channel + _validate_brand + _validate_logo + _validate_subtitles + glossary_problems + lexicon_problems`):

```python
    problems = (
        _validate_channel(channel, channel_dir / "channel.json")
        + _validate_brand(config, channel_dir / "brand.json")
        + _validate_logo(config, channel_dir / "brand.json")
        + _validate_subtitles(config, channel_dir / "brand.json")
        + _validate_upload(config, channel_dir / "brand.json")
        + glossary_problems
        + lexicon_problems
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_profile.py -q`
Expected: PASS (the new class green, the rest of `test_profile.py` still green).

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/profile.py tests/test_profile.py
git commit -m "Validate upload.mode in profile (api|manual, collected)"
```

---

### Task 3: CLI enforcement — `cmd_auth` and `cmd_upload` refuse a render-only channel

**Files:**
- Modify: `bin/yt-shorts` (add `from yt_shorts import upload_policy`; guard `cmd_auth` and `cmd_upload`)
- Test: `tests/test_cli.py` (add tests in `TestCmdAuth` and `TestCmdUpload`)

**Interfaces:**
- Consumes: `upload_policy.require_api_upload`, `upload_policy.RenderOnlyError` (Task 1).
- Produces: no new public interface; behaviour only.

Note on where each reads the mode: `cmd_upload` already receives the merged `config` (which carries `upload`), so it guards on that. `cmd_auth` runs before `profile.load` and reads only the channel folder, so it reads that channel's own `brand.json` directly (it always exists for a real channel; treat an absent one as `api`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, add to `class TestCmdAuth` (its `_channel` helper writes only `channel.json`; the manual case also writes a `brand.json`):

```python
    def test_render_only_channel_is_refused_before_any_consent(self, cli, tmp_path, capsys):
        channels_dir = tmp_path / "channels"
        self._channel(channels_dir)
        (channels_dir / "erf" / "brand.json").write_text(
            json.dumps({"upload": {"mode": "manual"}}), encoding="utf-8")
        oauth = FakeOAuthCLI()
        code = cli.cmd_auth("erf", channels_dir, tmp_path / "auth",
                            oauth=oauth, require=lambda feature: None)
        assert code == 2
        assert oauth.consented == 0                       # never reached consent
        assert "render-only" in capsys.readouterr().err
```

And to `class TestCmdUpload`:

```python
    def test_render_only_channel_uploads_nothing(self, cli, tmp_path, capsys):
        event_dir = tmp_path / "event"
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"mp4")
        editorial.save(directory, editorial.Edit(title=None, status="kept", transcript=None))
        calls = []

        def fake_upload_one(d, clip, edit):
            calls.append(d.name)
            return {"video_id": "X", "url": "u"}

        code = cli.cmd_upload(event_dir, {"upload": {"mode": "manual"}},
                              {"id": "UCabc"}, tmp_path / "auth", "erf",
                              upload_one=fake_upload_one)
        assert code == 2
        assert calls == []                                # no clip was uploaded
        assert "render-only" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q -k "render_only"`
Expected: FAIL — both return `0` and call the upload/consent boundary (guard not yet added).

- [ ] **Step 3: Write minimal implementation**

In `bin/yt-shorts`, add the import near the existing `from yt_shorts import upload_record` line:

```python
from yt_shorts import upload_policy                            # noqa: E402
```

Rewrite `cmd_auth` so it refuses a render-only channel before requiring google or touching consent. Read that channel's own `brand.json` (absent ⇒ treat as `api`):

```python
def cmd_auth(channel_name, channels_dir, auth_dir, *, oauth=None,
             require=google_require) -> int:
    """Authorizes a channel for upload: browser consent (operator's), token stored.

    The heavy Google boundary is `oauth` (defaults to the production adapter),
    injected so this tests without a real consent flow. A render-only channel
    (upload.mode=manual) has no token to store and is refused up front.
    """
    channel_dir = Path(channels_dir) / channel_name
    channel_json = channel_dir / "channel.json"
    if not channel_json.exists():
        print(f"ERROR: no channel.json for channel {channel_name!r}", file=sys.stderr)
        return 2
    brand_json = channel_dir / "brand.json"
    brand = (json.loads(brand_json.read_text(encoding="utf-8"))
             if brand_json.exists() else {})
    try:
        upload_policy.require_api_upload(brand)
    except upload_policy.RenderOnlyError as error:
        print(f"ERROR: {channel_name}: {error}", file=sys.stderr)
        return 2
    try:
        require("upload")
    except GoogleUnavailable as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    channel = json.loads(channel_json.read_text(encoding="utf-8"))
    channel_id = channel["id"]
    from yt_shorts.auth import AuthError, authorize
    if oauth is None:
        from yt_shorts.google_oauth import GoogleOAuth
        oauth = GoogleOAuth()
    try:
        authorize(channel_id, auth_dir=auth_dir, oauth=oauth)
    except AuthError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(f"Connected channel {channel_id} ({channel.get('handle', '')}).")
    return 0
```

Guard `cmd_upload` at the very top (before iterating any clip):

```python
def cmd_upload(dir_, config, channel, auth_dir, channel_name, *,
               upload_one=None, now=None) -> int:
    """Uploads every kept, rendered, not-yet-uploaded clip as private.

    One failure never aborts the run (same guarantee as render). A render-only
    channel (upload.mode=manual) refuses the whole run up front - the Data API
    cannot upload to it. `upload_one` is the injected boundary that actually
    talks to YouTube; the default composes auth + service + metadata + insert +
    record + quota.
    """
    try:
        upload_policy.require_api_upload(config)
    except upload_policy.RenderOnlyError as error:
        print(f"ERROR: {channel_name}: {error}", file=sys.stderr)
        return 2
    if upload_one is None:
        upload_one = _default_cli_upload_one(config, channel, auth_dir, channel_name)
    # ... rest unchanged ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q`
Expected: PASS — the two new tests green, and the existing `TestCmdAuth`/`TestCmdUpload` tests (which use `{}`/no `upload` block) still green.

- [ ] **Step 5: Commit**

```bash
git add bin/yt-shorts tests/test_cli.py
git commit -m "Refuse a render-only channel in cmd_auth and cmd_upload"
```

---

### Task 4: Studio API — report `upload_mode`, guard connect and upload

**Files:**
- Modify: `src/yt_shorts/studio/api.py` (`get_auth` adds `upload_mode`; `post_connect` and `post_upload` guard render-only)
- Test: `tests/test_studio_api.py` (a `manual_client` fixture + a `TestRenderOnly` class)

**Interfaces:**
- Consumes: `upload_policy.mode`, `upload_policy.require_api_upload`, `upload_policy.RenderOnlyError` (Task 1).
- Produces: `GET /api/auth` response gains `"upload_mode": "api" | "manual"`.

Note on the test fixture: the existing `studio_profile` fixture builds a fresh `Profile(...)` from `profile_load("erf/community-clips-back-catalogue")` with its `event_dir` pointed at tmp. The `manual_client` fixture does the same but with a config whose `upload` block is `{"mode": "manual"}` — a NEW config dict (`{**base.config, "upload": {"mode": "manual"}}`) so it never mutates the module-cached profile other tests share. No new on-disk fixture channel is needed.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_studio_api.py`. First a fixture that builds a manual-mode client (place it right after the existing `studio_profile` fixture; it reuses that file's `event_dir`, `profile_load`, `Profile`, `create_app`, `TestClient` imports):

```python
@pytest.fixture
def manual_client(event_dir):
    base = profile_load("erf/community-clips-back-catalogue")
    profile = Profile(
        identifier="erf/studio-test",
        channel_name="erf",
        event_name="studio-test",
        channel_dir=base.channel_dir,
        event_dir=event_dir,
        channel=base.channel,
        config={**base.config, "upload": {"mode": "manual"}},
    )
    return TestClient(create_app(profile))
```

Then the tests:

```python
class TestRenderOnly:
    def test_auth_reports_manual_mode(self, manual_client):
        body = manual_client.get("/api/auth").json()
        assert body["upload_mode"] == "manual"

    def test_auth_reports_api_mode_by_default(self, client):
        body = client.get("/api/auth").json()
        assert body["upload_mode"] == "api"

    def test_connect_is_refused_for_a_render_only_channel(self, manual_client):
        response = manual_client.post("/api/auth/connect", json={})
        assert response.status_code == 409
        assert "render-only" in response.json()["detail"]

    def test_upload_is_refused_for_a_render_only_channel(self, manual_client, event_dir):
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"mp4")
        editorial.save(directory, editorial.Edit(title=None, status="kept", transcript=None))
        response = manual_client.post(f"/api/clips/{directory.name}/upload")
        assert response.status_code == 409
        assert "render-only" in response.json()["detail"]

    def test_upload_preview_still_works_for_a_render_only_channel(self, manual_client, event_dir):
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        editorial.save(directory, editorial.Edit(title=None, status="kept", transcript=None))
        response = manual_client.get(f"/api/clips/{directory.name}/upload-preview")
        assert response.status_code == 200
        assert response.json()["privacy"] == "private"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py::TestRenderOnly -q`
Expected: FAIL — `upload_mode` absent from `/api/auth`; connect/upload return 200/job instead of 409.

- [ ] **Step 3: Write minimal implementation**

In `src/yt_shorts/studio/api.py`, add the import at the top with the other `from ..` imports:

```python
from .. import upload_policy
```

In `get_auth`, add the field (it already has `profile` in scope):

```python
        return {"connected": connected, "channel_id": channel_id,
                "remaining_uploads": remaining,
                "upload_mode": upload_policy.mode(profile.config)}
```

In `post_connect`, refuse render-only first (before `google_require`, so it 409s regardless of whether google is installed):

```python
    @app.post("/api/auth/connect")
    def post_connect(body: ConnectBody) -> dict:
        try:
            upload_policy.require_api_upload(profile.config)
        except upload_policy.RenderOnlyError as error:
            raise HTTPException(status_code=409, detail=str(error))
        channel_id = body.channel_id or profile.channel["id"]
        try:
            google_require("upload")
        except GoogleUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error))
        job = jobs.start_connect_job(profile, app.state.job_store, channel_id,
                                     force=body.force)
        return {"job_id": job.id}
```

In `post_upload`, refuse render-only first (before the clip/kept/short checks — the channel is render-only regardless of clip state):

```python
    @app.post("/api/clips/{name}/upload")
    def post_upload(name: str, force: bool = False) -> dict:
        try:
            upload_policy.require_api_upload(profile.config)
        except upload_policy.RenderOnlyError as error:
            raise HTTPException(status_code=409, detail=str(error))
        directory, clip = _load_clip_or_404(name)
        # ... rest unchanged ...
```

- [ ] **Step 4: Run tests + confirm the app still imports no google at module scope**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q`
Expected: PASS (TestRenderOnly green, the rest of the file still green).

Run: `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.studio.api as a; a.create_app; assert 'google' not in sys.modules and 'googleapiclient' not in sys.modules; print('clean - no google at import')"`
Expected: prints `clean - no google at import` (upload_policy pulls in nothing heavy).

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "Studio API: report upload_mode, refuse connect/upload for render-only"
```

---

### Task 5: Studio frontend — manual-mode controls (download + copy metadata)

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts` (add `upload_mode` to `AuthStatus`)
- Create: `src/yt_shorts/studio/web/src/upload.ts` (pure copy helpers) + `src/yt_shorts/studio/web/src/upload.test.ts`
- Create: `src/yt_shorts/studio/web/src/components/ManualUploadPanel.tsx`
- Modify: `src/yt_shorts/studio/web/src/components/AuthStatusBar.tsx` (render-only badge branch)
- Modify: `src/yt_shorts/studio/web/src/App.tsx` (choose UploadPanel vs ManualUploadPanel by `upload_mode`)
- Rebuild: `src/yt_shorts/studio/static/` (`npm run build`)

**Interfaces:**
- Consumes (from the backend, already built in Task 4): `GET /api/auth` returns `upload_mode: 'api' | 'manual'`; `GET /api/clips/{name}/upload-preview` returns `UploadPreview` (`title`, `description`, `tags: string[]`, `category_id`, `privacy`, `made_for_kids`); `GET /api/clips/{name}/short` streams the short (already exposed as `shortUrl(name)` in `api.ts`).
- Produces (frontend-internal): `upload.ts` exports `formatTagsForCopy(tags: string[]): string` and `composeCopyAll(preview: UploadPreview): string`; `ManualUploadPanel` component.

Work in `src/yt_shorts/studio/web`. `npm test` runs Vitest; `npm run build` typechecks and writes `../static/`.

- [ ] **Step 1: Write the failing Vitest test for the pure helpers**

Create `src/upload.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { composeCopyAll, formatTagsForCopy } from './upload'
import type { UploadPreview } from './api'

describe('formatTagsForCopy', () => {
  it('joins tags with ", " as YouTube Studio expects', () => {
    expect(formatTagsForCopy(['sim racing', 'endurance', 'crash'])).toBe(
      'sim racing, endurance, crash',
    )
  })
  it('is empty for no tags', () => {
    expect(formatTagsForCopy([])).toBe('')
  })
})

describe('composeCopyAll', () => {
  it('labels title, description and tags for one paste', () => {
    const preview: UploadPreview = {
      title: 'CRASH!',
      description: 'A big one.',
      tags: ['a', 'b'],
      category_id: '17',
      privacy: 'private',
      made_for_kids: false,
    }
    expect(composeCopyAll(preview)).toBe(
      'Title:\nCRASH!\n\nDescription:\nA big one.\n\nTags:\na, b',
    )
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npm test -- --run upload.test.ts`
Expected: FAIL — `Cannot find module './upload'`.

- [ ] **Step 3: Add `upload_mode` to `AuthStatus`, then implement `upload.ts`**

In `src/api.ts`, extend the `AuthStatus` interface:

```ts
export interface AuthStatus {
  connected: boolean
  channel_id: string
  remaining_uploads: number
  /** "api" (owned - connect + API upload) or "manual" (render-only: a
   * manager/editor channel the Data API cannot upload to, so the studio
   * offers a download + copy-to-paste metadata instead). See
   * yt_shorts.upload_policy and GET /api/auth. */
  upload_mode: 'api' | 'manual'
}
```

Create `src/upload.ts`:

```ts
import type { UploadPreview } from './api'

/** Tags as YouTube Studio's tag field expects them: a single
 * comma-separated line. Pure so it is unit-tested without a DOM. */
export function formatTagsForCopy(tags: string[]): string {
  return tags.join(', ')
}

/** Title, description and tags as one labelled block for a single "copy
 * all". Privacy and made-for-kids are deliberately NOT included: privacy is
 * always "private" on the API path (build_metadata) and is a YouTube Studio
 * toggle the operator sets themselves for a manual upload. */
export function composeCopyAll(preview: UploadPreview): string {
  return [
    `Title:\n${preview.title}`,
    `Description:\n${preview.description}`,
    `Tags:\n${formatTagsForCopy(preview.tags)}`,
  ].join('\n\n')
}
```

- [ ] **Step 4: Run the Vitest helper test to verify it passes**

Run: `npm test -- --run upload.test.ts`
Expected: PASS (4 passed).

- [ ] **Step 5: Implement `ManualUploadPanel.tsx`**

Create `src/components/ManualUploadPanel.tsx`. It mirrors `UploadPanel`'s render gate (`kept` + `has_short`), fetches the preview on mount, and offers a download plus per-field copy buttons. Use Mantine's `CopyButton` and `Anchor`:

```tsx
import { useEffect, useState } from 'react'
import {
  Alert,
  Anchor,
  Box,
  Button,
  CopyButton,
  Group,
  Stack,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core'
import type { ClipDetail, UploadPreview } from '../api'
import { ApiError, getUploadPreview, shortUrl } from '../api'
import { composeCopyAll, formatTagsForCopy } from '../upload'

interface ManualUploadPanelProps {
  clip: ClipDetail
}

function CopyField({ label, value, multiline = false }: {
  label: string; value: string; multiline?: boolean
}) {
  return (
    <Box>
      <Group justify="space-between" mb={2}>
        <Text size="xs" fw={600} tt="uppercase" c="dimmed">{label}</Text>
        <CopyButton value={value}>
          {({ copied, copy }) => (
            <Button size="compact-xs" variant="subtle" color="steel" onClick={copy}>
              {copied ? 'Copied' : 'Copy'}
            </Button>
          )}
        </CopyButton>
      </Group>
      {multiline
        ? <Textarea value={value} readOnly autosize minRows={2} maxRows={6} />
        : <TextInput value={value} readOnly />}
    </Box>
  )
}

/**
 * The manual-upload panel for a render-only channel (upload.mode=manual - a
 * YouTube manager/editor channel the Data API cannot upload to). Rendered
 * instead of UploadPanel (App.tsx chooses on auth.upload_mode). It never
 * offers a connect or an API upload - the backend 409s both (see
 * upload_policy) - and instead lets the operator download the rendered short
 * and copy the prepared metadata into YouTube Studio by hand.
 *
 * Gated exactly like UploadPanel: only a `kept` clip with a rendered short
 * (`has_short`) shows anything. Privacy and "made for kids" are shown as a
 * note, not as copy values: privacy is always "private" on the API path and
 * both are toggles the operator sets in YouTube Studio for a manual upload.
 */
export function ManualUploadPanel({ clip }: ManualUploadPanelProps) {
  const [preview, setPreview] = useState<UploadPreview | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPreview(null)
    setError(null)
    if (clip.status !== 'kept' || !clip.has_short) return
    getUploadPreview(clip.name)
      .then((p) => { if (!cancelled) setPreview(p) })
      .catch((e) => { if (!cancelled) setError(e instanceof ApiError ? e.message : String(e)) })
    return () => { cancelled = true }
  }, [clip.name, clip.status, clip.has_short])

  if (clip.status !== 'kept' || !clip.has_short) return null

  return (
    <Box pt="sm" style={{ borderTop: '1px solid var(--mantine-color-dark-6)' }}>
      <Stack gap="xs">
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Text fw={600} size="xs" tt="uppercase" c="dimmed">Manual upload</Text>
          <Button
            component="a"
            href={shortUrl(clip.name)}
            download
            size="xs"
            color="steel"
            variant="light"
          >
            Download short
          </Button>
        </Group>
        <Text size="xs" c="dimmed">
          This is a manager/editor channel - upload the downloaded short in YouTube Studio
          and paste the fields below. Set visibility and "made for kids" there yourself.
        </Text>
        {error && <Alert color="red" variant="light" title="Metadata unavailable">{error}</Alert>}
        {preview && (
          <Stack gap="xs">
            <CopyField label="Title" value={preview.title} />
            <CopyField label="Description" value={preview.description} multiline />
            <CopyField label="Tags" value={formatTagsForCopy(preview.tags)} />
            <Text size="xs" c="dimmed">Category id: {preview.category_id}</Text>
            <CopyButton value={composeCopyAll(preview)}>
              {({ copied, copy }) => (
                <Button size="xs" variant="default" onClick={copy}>
                  {copied ? 'Copied all' : 'Copy all'}
                </Button>
              )}
            </CopyButton>
          </Stack>
        )}
      </Stack>
    </Box>
  )
}
```

- [ ] **Step 6: Branch AuthStatusBar for manual mode**

In `src/components/AuthStatusBar.tsx`, add a render-only branch at the start of the returned `<Group>` — before the `auth?.connected` branch — so a manual channel shows a badge instead of a connect button / quota. Insert after the `error`/loading branches:

```tsx
      ) : auth?.upload_mode === 'manual' ? (
        <Badge color="grape" variant="light" size="xs">
          Render-only (manual upload)
        </Badge>
      ) : auth?.connected ? (
```

(Keep the rest of the chain unchanged.)

- [ ] **Step 7: Choose the panel in App.tsx**

In `src/App.tsx`, where `<UploadPanel .../>` is rendered for `selectedClip`, choose by `auth?.upload_mode`:

```tsx
              {auth?.upload_mode === 'manual' ? (
                <ManualUploadPanel clip={selectedClip} />
              ) : (
                <UploadPanel
                  clip={selectedClip}
                  job={uploadingClipName === selectedClip.name ? uploadJob : null}
                  jobStarting={uploadingClipName === selectedClip.name && uploadStarting}
                  uploadedRecord={uploadRecords[selectedClip.name] ?? null}
                  remainingUploads={auth?.remaining_uploads ?? null}
                  authConnected={auth?.connected ?? null}
                  blockedBy={blockedByForUpload}
                  onStartUpload={handleStartUpload}
                />
              )}
```

Add the import at the top of `App.tsx`:

```tsx
import { ManualUploadPanel } from './components/ManualUploadPanel'
```

- [ ] **Step 8: Typecheck, test, build, and drive the real page**

Run: `npm test -- --run` — Expected: all Vitest green, including `upload.test.ts`.
Run: `npm run build` — Expected: typecheck clean; `../static/` rewritten.
Then start the studio and look: a manual channel shows the "Render-only" badge, a "Download short" button and the copy-metadata fields (no connect, no upload button); an api channel is unchanged (connect + upload). Screenshot both before reporting.

- [ ] **Step 9: Commit (including the rebuilt static)**

```bash
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "Studio UI: render-only channels get download + copy-metadata"
```

---

### Task 6: Docs — CLAUDE.md and README

**Files:**
- Modify: `CLAUDE.md` (the upload / stage E section)
- Modify: `README.md` (channel setup / upload section)

**Interfaces:** none.

- [ ] **Step 1: Add the invariant to CLAUDE.md**

In `CLAUDE.md`, under the upload ("stage E") non-negotiable invariants, add a bullet:

```markdown
- **A channel is either `api` (owned) or `manual` (render-only).**
  `config["upload"]["mode"]` (brand.json, default `api`) declares it; `upload_policy`
  is the single predicate, enforced at four points (`cmd_auth`, `cmd_upload`,
  `POST /api/auth/connect`, `POST /api/clips/{name}/upload`) - a `manual` channel
  (a YouTube manager/editor delegation the Data API cannot upload to) is refused
  at every API-upload path and the studio offers a download of the short plus
  copy-to-paste metadata (the existing `/short` and `/upload-preview` routes)
  instead. `manual` never gets a token: the connect verify-and-refuse guard would
  reject it anyway, since a manager/editor channel is not returned by
  `channels.list(mine=true)`.
```

- [ ] **Step 2: Add a line to README.md**

In `README.md`, near where channel/upload setup is described, add:

```markdown
Channels you only **manage/edit** (not own) cannot be uploaded to via the API.
Set `"upload": { "mode": "manual" }` in the channel's `brand.json`: the studio
then offers the rendered short as a download and the prepared title/description/
tags to copy, and you upload it by hand in YouTube Studio. Owned channels need
no flag (`mode` defaults to `api`).
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "Document the owned-vs-render-only upload class"
```

---

## Verification (whole feature)

- `PYTHONPATH=src .venv/bin/pytest -q` — full suite green.
- `cd src/yt_shorts/studio/web && npm test -- --run && npm run build` — Vitest green, build clean, `static/` committed.
- Manual smoke: an `api` channel behaves exactly as before (connect + upload); a `manual` channel shows the render-only badge, download and copy-metadata, and both `POST /api/auth/connect` and `POST /api/clips/{name}/upload` return 409 for it.
