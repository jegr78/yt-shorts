# Stage G4 — Studio Settings Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a workspace-level Settings page to the studio: an OAuth connection overview across all channels (status, upload class, quota estimate) with connect / switch-account / disconnect, plus a read-only workspace-info panel.

**Architecture:** One aggregate read route (`GET /api/settings`) reuses the exact per-channel auth logic `GET /api/channels/{channel}/auth` already runs, looped over the channel listing. One channel-scoped write route (`DELETE /api/channels/{channel}/auth`) deletes a channel's stored token via a new pure `auth.forget_credentials`. A fourth frontend screen (`settings`) renders both, reachable from the start screen.

**Tech Stack:** Python 3 + FastAPI (studio backend), Pillow-only render path (untouched here), React + Vite + Mantine + TypeScript (frontend), pytest + Vitest + Playwright-in-pytest.

## Global Constraints

- `PYTHONPATH=src` is mandatory for every pytest invocation. Full suite green at plan start.
- **No new dependency.** The google libraries stay OPTIONAL and lazily imported — no google import at module scope anywhere; `create_app()` pulls no google at import.
- **Secrets never leave the box.** No route returns a token, client secret, or password — only booleans, the public YouTube channel id, and the quota integer. Never log/echo/return `client_secret.json`, `token-*.json`, or `quota.json`.
- **The only new write is deleting `auth/token-<id>.json`.** Disconnect touches that file and nothing else — never `client_secret.json`, never `quota.json`, never a channel/event directory. Upload privacy is unchanged (still always private; G4 adds no upload path).
- **Every URL path segment is validated** as one safe segment via `pathnames.validate_segment` (`^[A-Za-z0-9][A-Za-z0-9._-]*\Z`) BEFORE any filesystem touch — the disconnect route must not inherit the existing unvalidated-`{channel}` gap in `_load_channel`.
- macOS: no bare `timeout`; use `gtimeout <secs>` (`/opt/homebrew/bin/gtimeout`) or the Bash tool's own timeout to bound a hang.
- Built `static/` stays committed; English only; imperative commit messages.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File structure

- `src/yt_shorts/auth.py` — add pure `forget_credentials` (token deletion). No google.
- `src/yt_shorts/studio/api.py` — harden `_load_channel` with segment validation; add `GET /api/settings` and `DELETE /api/channels/{channel}/auth`; add `from .. import pathnames`.
- `tests/test_auth.py` — unit tests for `forget_credentials`.
- `tests/test_studio_api.py` — the two new routes.
- Frontend: `src/scopedApi.ts` (`settings` screen in `Screen`/`parseRoute`/`routePath`), `src/Root.tsx` (render `SettingsScreen`), `src/api.ts` (three calls + a type), `src/components/SettingsScreen.tsx` (new), `src/components/ChannelsScreen.tsx` (a Settings link), `tests/test_studio_e2e.py` (disconnect E2E), rebuilt `../static/`.
- Docs: `CLAUDE.md`, `README.md`.

---

## Task 1: `auth.forget_credentials` — delete a channel's stored token (pure)

**Files:**
- Modify: `src/yt_shorts/auth.py` (add one function next to `load_credentials`)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `auth.TokenStore(auth_dir)` with `.path(channel_id) -> Path` (exists already).
- Produces: `auth.forget_credentials(channel_id, *, auth_dir, store=None) -> bool` — deletes `auth/token-<channel_id>.json`; returns `True` if a file was removed, `False` if none existed. Imports no google.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_auth.py` (create the file if it does not exist; if it exists, append these):

```python
from pathlib import Path

from yt_shorts import auth


def test_forget_credentials_deletes_an_existing_token(tmp_path):
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    token = auth_dir / "token-UCabc.json"
    token.write_text("{}", encoding="utf-8")
    removed = auth.forget_credentials("UCabc", auth_dir=auth_dir)
    assert removed is True
    assert not token.exists()


def test_forget_credentials_is_a_noop_when_absent(tmp_path):
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    removed = auth.forget_credentials("UCabc", auth_dir=auth_dir)
    assert removed is False


def test_forget_credentials_touches_only_the_token(tmp_path):
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "token-UCabc.json").write_text("{}", encoding="utf-8")
    secret = auth_dir / "client_secret.json"
    quota = auth_dir / "quota.json"
    secret.write_text("SECRET", encoding="utf-8")
    quota.write_text("{}", encoding="utf-8")
    auth.forget_credentials("UCabc", auth_dir=auth_dir)
    assert secret.exists() and quota.exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_auth.py -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.auth' has no attribute 'forget_credentials'`.

- [ ] **Step 3: Implement**

Add to `src/yt_shorts/auth.py` (after `load_credentials`):

```python
def forget_credentials(channel_id, *, auth_dir, store=None) -> bool:
    """Delete the stored OAuth token for ``channel_id`` (the studio's
    "disconnect"). Removes only ``auth/token-<channel_id>.json`` - never the
    client secret or the quota file - and is reversible by connecting again.
    Returns True if a token file was removed, False if there was none. Imports
    no google: forgetting a token is a local file op, unlike authorize()."""
    store = store or TokenStore(auth_dir)
    path = store.path(channel_id)
    if path.exists():
        path.unlink()
        return True
    return False
```

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_auth.py -q`
Expected: PASS (3 passed, plus any pre-existing tests in the file).

- [ ] **Step 5: Confirm no google import crept in**

Run: `PYTHONPATH=src .venv/bin/python -c "import ast,sys; ast.parse(open('src/yt_shorts/auth.py').read()); print('ok')"` then visually confirm the new function has no `import google`/`from google`.
Expected: `ok`, and no google import at module scope.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/auth.py tests/test_auth.py
git commit -m "Add auth.forget_credentials: delete a channel's stored token"
```

---

## Task 2: Studio API — settings aggregate + disconnect route

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Consumes: `auth.forget_credentials` (Task 1); the app's closure helpers `_load_channel(channel) -> dict`, `_channel_config(channel) -> dict`, `channels_dir`, `_resolve_workspace()`, `list_channels(channels_dir) -> list[ChannelInfo]` (fields `.name`, `.error`), `load_credentials`, `QuotaTracker`, `upload_policy.mode`, `google_require`, `GoogleUnavailable`, and `pathnames.validate_segment`.
- Produces: `GET /api/settings` → `{"workspace": {...}, "channels": [...]}`; `DELETE /api/channels/{channel}/auth` → `{"disconnected": "<id>"}` (200) or 404.

Reference the existing route this mirrors: `get_auth` at `src/yt_shorts/studio/api.py:473-489` (the per-channel version this aggregates). The `_load_channel` helper is at `:244-254`.

- [ ] **Step 1: Write the failing tests**

Append to the auth test class area of `tests/test_studio_api.py` (top-level, alongside the other `Test*` classes). These reuse the module's `client`, `studio_profile`, `manual_client`, `CHANNEL`, `profile_module`, and `api` names already imported/fixtured in the file:

```python
class TestSettings:
    def _fake_workspace(self, root):
        from yt_shorts.workspace import Workspace
        return Workspace(root=root, channels_dir=root / "channels", origin="YT_SHORTS_DATA")

    def test_settings_lists_channels_with_connection_state(self, client, studio_profile, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        (root / "auth").mkdir(exist_ok=True)
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: object())   # connected
        monkeypatch.setattr(api, "google_require", lambda feature: None)          # libs present
        body = client.get("/api/settings").json()
        assert body["workspace"]["origin"] == "YT_SHORTS_DATA"
        assert body["workspace"]["channel_count"] == 1
        assert body["workspace"]["google_upload_available"] is True
        erf = next(r for r in body["channels"] if r["channel"] == "erf")
        assert erf["connected"] is True
        assert erf["upload_mode"] == "api"
        assert erf["channel_id"]                       # the fixture's real id, non-empty
        assert isinstance(erf["remaining_uploads"], int)
        assert erf["error"] is None

    def test_settings_reports_disconnected_and_missing_google(self, client, studio_profile, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: None)        # not connected
        def no_google(feature):
            raise api.GoogleUnavailable("install the libs")
        monkeypatch.setattr(api, "google_require", no_google)
        body = client.get("/api/settings").json()
        assert body["workspace"]["google_upload_available"] is False
        assert body["channels"][0]["connected"] is False

    def test_settings_marks_a_manual_channel(self, manual_client, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: None)
        monkeypatch.setattr(api, "google_require", lambda feature: None)
        body = manual_client.get("/api/settings").json()
        modes = {r["channel"]: r["upload_mode"] for r in body["channels"]}
        assert "manual" in modes.values()

    def test_disconnect_removes_only_the_token(self, client, studio_profile, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        auth_dir = root / "auth"
        auth_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        channel_id = json.loads((profile_module.CHANNELS_DIR / "erf" / "channel.json").read_text())["id"]
        (auth_dir / f"token-{channel_id}.json").write_text("{}", encoding="utf-8")
        secret = auth_dir / "client_secret.json"
        secret.write_text("SECRET", encoding="utf-8")
        r = client.delete(f"/api/channels/{CHANNEL}/auth")
        assert r.status_code == 200
        assert r.json()["disconnected"] == channel_id
        assert not (auth_dir / f"token-{channel_id}.json").exists()
        assert secret.exists()

    def test_disconnect_without_a_token_is_404(self, client, studio_profile, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        (root / "auth").mkdir(exist_ok=True)
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        r = client.delete(f"/api/channels/{CHANNEL}/auth")
        assert r.status_code == 404

    def test_disconnect_rejects_a_bad_channel_segment(self, client, studio_profile):
        # A ".." segment (no slash) reaches the route and must be rejected as a
        # bad name BEFORE any filesystem touch - never read a channel.json outside
        # the channels dir.
        r = client.delete("/api/channels/%2e%2e/auth")
        assert r.status_code in (400, 404, 405)
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py::TestSettings -q`
Expected: FAIL — `GET /api/settings` and `DELETE …/auth` are 404/405 (routes not defined).

- [ ] **Step 3: Harden `_load_channel`**

In `src/yt_shorts/studio/api.py`, add the pathnames import near the other `from ..` imports (around line 69-78):

```python
from .. import pathnames
```

Then change `_load_channel` (currently at ~line 244) to validate the segment first:

```python
    def _load_channel(channel: str) -> dict:
        """This channel's channel.json (its YouTube id), for the channel-scoped
        auth routes - which have no event, so they do not load a full profile.
        The segment is validated as one safe path segment before any filesystem
        touch, so a '..' or dotted {channel} can never read a channel.json
        outside the channels dir."""
        try:
            pathnames.validate_segment(channel, what="channel name")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        path = channels_dir / channel / "channel.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=404, detail=f"Unreadable channel.json for {channel!r}: {error}")
```

- [ ] **Step 4: Add the two routes**

In `src/yt_shorts/studio/api.py`, add right after the existing `post_connect` route (ends ~line 511). The aggregate:

```python
    @app.get("/api/settings")
    def get_settings() -> dict:
        import datetime

        from ..google_oauth import GoogleOAuth
        ws = _resolve_workspace()
        auth_dir = ws.root / "auth"
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            google_require("upload")
            google_ok = True
        except GoogleUnavailable:
            google_ok = False
        infos = list_channels(channels_dir)
        rows = []
        for info in infos:
            row = {"channel": info.name, "channel_id": "", "upload_mode": "api",
                   "connected": False, "remaining_uploads": None, "error": info.error}
            if info.error is None:
                try:
                    channel_id = _load_channel(info.name).get("id", "")
                except HTTPException as error:
                    # An odd directory name (fails segment validation) or a
                    # channel.json that vanished between listing and read - list
                    # the row with the reason, never 500 the whole page.
                    row["error"] = error.detail
                    rows.append(row)
                    continue
                row["channel_id"] = channel_id
                row["upload_mode"] = upload_policy.mode(_channel_config(info.name))
                if channel_id:
                    try:
                        creds = load_credentials(channel_id, auth_dir=auth_dir, oauth=GoogleOAuth())
                        row["connected"] = creds is not None
                    except Exception:   # noqa: BLE001 - any auth failure = "not connected", never a 500
                        row["connected"] = False
                    try:
                        row["remaining_uploads"] = QuotaTracker(
                            auth_dir, channel_id).remaining_uploads(now)
                    except Exception:   # noqa: BLE001 - a warn-only estimate; null if uncomputable
                        row["remaining_uploads"] = None
            rows.append(row)
        return {"workspace": {"root": str(ws.root), "origin": ws.origin,
                              "channel_count": len(infos),
                              "google_upload_available": google_ok},
                "channels": rows}
```

The disconnect route:

```python
    @app.delete(CH + "/auth")
    def disconnect_auth(channel: str) -> dict:
        # Forget the stored token for this channel (its channel.json id). Writes
        # nothing but the removal of auth/token-<id>.json; never touches the
        # client secret or quota, and imports no google. {channel} is validated
        # inside _load_channel before any filesystem touch.
        channel_id = _load_channel(channel)["id"]
        auth_dir = _resolve_workspace().root / "auth"
        if not auth.forget_credentials(channel_id, auth_dir=auth_dir):
            raise HTTPException(
                status_code=404, detail=f"channel {channel!r} was not connected")
        return {"disconnected": channel_id}
```

Ensure `auth` is importable in the module: the file currently does `from ..auth import load_credentials`. Add at the top with the other `from ..` imports:

```python
from .. import auth
```

(Keep the existing `from ..auth import load_credentials` — the tests monkeypatch `api.load_credentials`.)

- [ ] **Step 5: Run to verify the class passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py::TestSettings -q`
Expected: PASS (6 passed).

- [ ] **Step 6: Run the whole studio-api file + the auth/pathnames files**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py tests/test_auth.py -q`
Expected: all pass (the `_load_channel` hardening must not break existing auth/connect tests).

- [ ] **Step 7: Confirm the module still pulls no google at import**

Run: `PYTHONPATH=src .venv/bin/python -c "import yt_shorts.studio.api; import sys; assert not any(m=='google' or m.startswith('google.') for m in sys.modules), sorted(m for m in sys.modules if m.startswith('google')); print('no google at import')"`
Expected: `no google at import`.

- [ ] **Step 8: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "Studio API: settings overview and channel disconnect route"
```

---

## Task 3: Frontend — the Settings screen

**Files:**
- Modify: `src/yt_shorts/studio/web/src/scopedApi.ts` (add `settings` to `Screen`, `parseRoute`, `routePath`), `src/Root.tsx` (render `SettingsScreen`), `src/api.ts` (three calls + a type), `src/components/ChannelsScreen.tsx` (a Settings link).
- Create: `src/yt_shorts/studio/web/src/components/SettingsScreen.tsx`.
- Modify: `tests/test_studio_e2e.py` (disconnect E2E), rebuild `../static/`.

Dispatched to a focused frontend agent with the Task-2 API contract below.

**API contract (Task 2, already built):**
- `GET /api/settings` → `{ workspace: { root: string, origin: string, channel_count: number, google_upload_available: boolean }, channels: Array<{ channel: string, channel_id: string, upload_mode: 'api'|'manual', connected: boolean, remaining_uploads: number|null, error: string|null }> }`.
- `DELETE /api/channels/{channel}/auth` → 200 `{ disconnected: string }`; 404 if there was no token; 400 for a bad `{channel}`.
- Connect / switch-account is the EXISTING `POST /api/channels/{channel}/auth/connect` (body `{ channel_id?, force? }`) returning `{ job_id }`, polled via the existing `getJob`/`useJobPolling`. 503 `{detail}` when google libs are missing — surface the detail as-is.
- Errors are FastAPI `{detail}` — surface via `ApiError.message`.

**Existing frontend facts (verified — trust these, do not re-derive):**
- `src/scopedApi.ts` defines `export type Screen = 'channels' | 'events' | 'editor'`, `export interface Route { screen: Screen; channel?: string; event?: string }`, `parseRoute(pathname)` (splits path segments), and `routePath(route)`. Add a `settings` screen: `parseRoute` returns `{ screen: 'settings' }` when the single segment is `settings` (i.e. path `/settings`); `routePath({ screen: 'settings' })` returns `/settings`. Keep `encodeSegment`. IMPORTANT: `/settings` must be matched BEFORE the generic 1-segment `events` rule, so a literal `settings` path is the settings screen, not a channel named "settings".
- `src/api.ts` exports `asJson<T>`, `ApiError` (`.status`, `.message`), `channelBase(channel)` (from scopedApi), `getJob(jobId)`, and `startConnect(channelId, force)` which uses the ACTIVE scope (`channelScope()`), so it is NOT usable from the settings screen (no active channel). Add a scope-explicit connect (below).
- `src/components/ChannelsScreen.tsx` is the start screen (lists channels) under `NavScreen` chrome. Add a "Settings" link/button in its header that calls `navigate(routePath({ screen: 'settings' }))` (import `navigate` from `../useRoute`, `routePath` from `../scopedApi`).
- `src/components/AuthStatusBar.tsx` shows the connect dialog + job polling for the editor's single channel — read it for the connect-dialog idiom (send a channel id, `startConnect`, poll `job_id` with `useJobPolling`), and reuse that idiom in `SettingsScreen` with the scope-explicit connect.
- `deleteConfirmed(typed, name)` from `../eventAdmin` is the typed-confirmation predicate used by the delete dialogs — reuse it for the disconnect confirmation (type the `channel_id`).
- Dark Mantine styling: `Card`, `Stack`, `Group`, `Badge`, `Alert` (errors), `Loader` (loading), `Modal` (dialogs), `Button`/`ActionIcon`, `Table` — match `ChannelScreen.tsx`/`ChannelsScreen.tsx`.

**What to build:**
1. `src/scopedApi.ts` — extend the router:
```ts
export type Screen = 'channels' | 'events' | 'editor' | 'settings'
// in parseRoute, before the 1-segment events rule:
//   if (segments.length === 1 && segments[0] === 'settings') return { screen: 'settings' }
// in routePath:
//   if (route.screen === 'settings') return '/settings'
```
2. `src/api.ts` — add:
```ts
export interface SettingsResponse {
  workspace: { root: string; origin: string; channel_count: number; google_upload_available: boolean }
  channels: Array<{
    channel: string; channel_id: string; upload_mode: 'api' | 'manual'
    connected: boolean; remaining_uploads: number | null; error: string | null
  }>
}
export function getSettings(): Promise<SettingsResponse> {
  return fetch('/api/settings').then(asJson<SettingsResponse>)
}
export function disconnectAuth(channel: string): Promise<{ disconnected: string }> {
  return fetch(`${channelBase(channel)}/auth`, { method: 'DELETE' }).then(asJson)
}
/** Scope-explicit connect for the settings screen (the editor's startConnect
 * uses the active scope, which settings has none of). Same body/return/503 as
 * POST /api/channels/{channel}/auth/connect. */
export function connectChannel(
  channel: string, channelId: string | null, force = false,
): Promise<{ job_id: string }> {
  return fetch(`${channelBase(channel)}/auth/connect`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel_id: channelId || undefined, force }),
  }).then(asJson<{ job_id: string }>)
}
```
3. `src/Root.tsx` — render `<SettingsScreen />` when `route.screen === 'settings'` (add the branch before the `channels` fallthrough; no channel/event needed).
4. `src/components/SettingsScreen.tsx` — on mount `getSettings()`; under `NavScreen` chrome (breadcrumb `Channels › Settings`):
   - **Workspace panel** (read-only `Card`): data root (monospace), origin rendered friendly (`YT_SHORTS_DATA` → "from $YT_SHORTS_DATA", `default` → "default (~/YT-Shorts-Data)", `repository` → "repository fallback"), channel count. If `google_upload_available` is false, an `Alert` (color gray): upload libraries not installed — `.venv/bin/pip install fastapi uvicorn google-api-python-client google-auth-oauthlib`.
   - **Connection table** (`Table` or a `Stack` of `Card`s), one row per channel: slug, `channel_id` (monospace, or "—" if empty), an upload-class `Badge` (`api`/`manual`), a connection `Badge` (green "connected" / gray "not connected"), the quota estimate ("~N uploads left today", or "—" if null). Actions:
     - `error !== null` → show the error, no actions.
     - `upload_mode === 'manual'` → text "render-only — no API upload", no actions.
     - `upload_mode === 'api'` && `connected` → **Switch account** (opens the connect dialog with `force: true`) and **Disconnect**.
     - `upload_mode === 'api'` && `!connected` → **Connect** (connect dialog, `force: false`).
   - **Connect dialog**: a `Modal` with a `TextInput` prefilled with the row's `channel_id` (editable — a multi-channel operator may connect a different id), a note that consent opens in the operator's browser, and a Connect button that calls `connectChannel(row.channel, enteredId, force)` then polls `job_id` via `useJobPolling`; on done, refetch `getSettings()`. Surface a 503 detail inline (libs missing). Mirror `AuthStatusBar`'s polling.
   - **Disconnect dialog**: a `Modal` explaining this forgets the LOCAL token only (full revocation is a manual step in the operator's Google account settings), a `TextInput` where the operator types the `channel_id` to confirm (gate with `deleteConfirmed(typed, row.channel_id)`), then `disconnectAuth(row.channel)`, then refetch. A 404 ("was not connected") is surfaced then the list refreshed.
   - Loading (`Loader`) / error (`Alert` "check the studio server is still running") states for the initial `getSettings()`.
5. `src/components/ChannelsScreen.tsx` — a **Settings** button in the header (`navigate(routePath({ screen: 'settings' }))`).
6. Match the existing dark Mantine styling. Rebuild `../static/` (`npm run build`).

**E2E (`tests/test_studio_e2e.py`, real Chromium):** add a test that seeds a token file for the erf channel's id in the workspace `auth/` dir the studio resolves (read the file first to learn how it starts the server, which workspace/auth dir it points at, and how it seeds on-disk state — reuse those fixtures; do NOT read the operator's real `~/YT-Shorts-Data`), navigates to `/settings`, confirms the erf row shows "connected", clicks Disconnect, types the channel id to confirm, and asserts the token file is gone and the row flips to "not connected". (Connect is NOT E2E-able — it is a real OAuth browser flow; do not attempt it.)

**Verify:** `npm test -- --run` (Vitest green — no new pure module unless a display helper was extracted, in which case it has its own `.test.ts`); `npm run build` (typecheck clean, `../static/` rebuilt); `PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q` (green, real Chromium). Drive the real page: open Settings, see channels, seed+disconnect works, a bad state shows inline.

- [ ] Build to the above; rebuild+commit `static/`; commit — `Add the workspace Settings page to the studio`.

---

## Task 4: Documentation

**Files:** `CLAUDE.md`, `README.md`.

- [ ] **Step 1: CLAUDE.md** — in the studio write-boundary section, note that the studio now also exposes a workspace-level Settings view: `GET /api/settings` aggregates each channel's connection state (the same per-channel `load_credentials` + `QuotaTracker` + `upload_policy.mode` read `GET …/auth` does, no secret ever returned), and `DELETE /api/channels/{channel}/auth` disconnects a channel by deleting ONLY `auth/token-<id>.json` via the pure `auth.forget_credentials` (never the client secret or quota; reversible by connecting again) — the first and only studio path that deletes an auth token. Note the `_load_channel` hardening: the channel segment is now validated before any filesystem touch, closing the unvalidated-`{channel}` gap on the channel-scoped auth routes.

- [ ] **Step 2: README.md** — in the Studio section, note a **Settings** page (reached from the start screen): a workspace panel (data location + origin, whether the upload libraries are installed) and a per-channel connection overview — connect, switch account, or disconnect each owned channel; render-only channels are shown as such. Disconnect forgets the local token only; full revocation stays a manual step in the operator's Google account.

- [ ] **Step 3: Commit** — `Document the studio settings page`.

---

## Verification for the branch

- Full `pytest` suite green, E2E included; `npm test` green; `static/` rebuilt and committed.
- `GET /api/settings` returns the right rows for a connected api channel, a disconnected one, a manual one, and an unreadable one (error surfaced, page intact); `google_upload_available` reflects the libs.
- `DELETE …/auth` deletes only `token-<id>.json`, 404s without a token, and rejects a bad `{channel}` segment before any filesystem touch. `auth.forget_credentials` never touches the client secret or quota.
- The Settings page: open it, see all channels; connect/switch reuse the existing job; seed+disconnect works end-to-end (E2E).
- `auth.py` imports no google; `create_app()` / `yt_shorts.studio.api` pull no google at import.

## Self-review notes

- **Security:** the disconnect route's `{channel}` is validated inside the hardened `_load_channel` before any filesystem touch — a `..`/dotted segment cannot read a `channel.json` outside the channels dir or delete a token by a smuggled id. The aggregate iterates `list_channels`, which yields only real directory names, so it needs no separate segment guard.
- **No secret leakage:** `GET /api/settings` returns only a boolean, the public channel id, an int/null quota, and the workspace path/origin — never a token, secret, or password. `str(ws.root)` is a local path the operator already knows, not a secret.
- **Google stays optional:** `google_require`/`GoogleOAuth` are imported lazily inside the route (never at module scope); when the libs are absent, `google_upload_available` is `False` and `load_credentials` failures degrade each row to "not connected" instead of 500ing.
- **Reversible write:** disconnect only removes the local token; connecting again restores it. Full grant revocation on Google's side is deliberately left as a manual operator step (documented in the UI).
- **Deferred with reason:** detection defaults (no global store; rarely tuned), token-expiry display (would couple the read path to google), bulk connect/disconnect, and server-side grant revocation — all out of G4 scope.
