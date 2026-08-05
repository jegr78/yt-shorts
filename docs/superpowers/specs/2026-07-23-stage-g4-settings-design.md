# Stage G4 — Studio Settings Page (Design)

**Goal:** A workspace-level **Settings** page in the studio: one OAuth
connection overview across *all* channels (status, upload class, quota
estimate) with connect / switch-account / disconnect, plus a read-only
workspace-info panel.

**Scope decisions (from brainstorming):**
- **In:** OAuth overview + workspace info.
- **Out (YAGNI):** detection defaults. They have no persistent workspace store
  today (only hard-coded fallbacks in `detect.py` — `threshold` 1.0, `min_gap`
  20.0, `preroll` 8.0, `postroll` 4.0, `top_n` 20 — plus an optional per-profile
  `detect` section that isn't studio-editable). Adding a global store + a new
  `profile.py` merge layer is speculative; detection is rarely tuned. Left as-is.
- **Disconnect is in**, guarded by a typed confirmation (like channel delete).

## Architecture

The studio app is workspace-level (`create_app()` takes no profile). G4 adds:
1. one **aggregate read** route that reuses the exact per-channel auth logic
   `GET /api/channels/{channel}/auth` already runs, over the channel listing;
2. one **channel-scoped write** route that deletes a channel's stored token;
3. a **fourth frontend screen** (`settings`) reachable from the start screen.

No new heavy dependency, no google import at module scope, no new profile
layering. The only new write path is deleting `auth/token-<id>.json`.

## Backend

### `auth.forget_credentials(channel_id, *, auth_dir, store=None) -> bool`
New **pure** function in `auth.py` (no google import — like the rest of
`auth.py`'s non-network helpers). Deletes `auth/token-<channel_id>.json` via
`TokenStore(auth_dir).path(channel_id)`; returns `True` if a file was removed,
`False` if there was none. Touches only the token file — never
`client_secret.json`, never `quota.json`. Reversible by connecting again.

```python
def forget_credentials(channel_id, *, auth_dir, store=None) -> bool:
    store = store or TokenStore(auth_dir)
    path = store.path(channel_id)
    if path.exists():
        path.unlink()
        return True
    return False
```

### `GET /api/settings` — the aggregate (read)
Returns the workspace facts plus one row per channel, built by running the same
`load_credentials` + `QuotaTracker` + `upload_policy.mode` logic `get_auth` uses,
looped over `workspace_listing` channels:

```json
{
  "workspace": {
    "root": "/Users/jegr/YT-Shorts-Data",
    "origin": "YT_SHORTS_DATA",           // or "default" (~/YT-Shorts-Data) or "repository"
    "channel_count": 3,
    "google_upload_available": true        // are the optional google libs importable?
  },
  "channels": [
    { "channel": "erf", "channel_id": "UC…", "upload_mode": "api",
      "connected": true, "remaining_uploads": 6, "error": null },
    { "channel": "demo", "channel_id": "", "upload_mode": "api",
      "connected": false, "remaining_uploads": null, "error": "channel.json unreadable" }
  ]
}
```

- `origin` is `workspace.resolve().origin` verbatim (`"YT_SHORTS_DATA"` /
  `"default"` / `"repository"`).
- `google_upload_available`: `True` unless importing the upload libs raises
  (mirror `google_require("upload")` → `GoogleUnavailable`, caught to a bool).
  When `False`, the UI shows the install hint instead of offering Connect.
- Per channel: `connected` from `load_credentials` (any failure → `False`,
  never a 500), `remaining_uploads` from `QuotaTracker.remaining_uploads(now)`
  (a warn-only Pacific-day estimate; `null` if it cannot be computed),
  `upload_mode` from `upload_policy.mode` on the channel-level config.
- A channel whose `channel.json` is unreadable is **listed** with `channel_id:
  ""`, `connected: false`, `remaining_uploads: null`, and a non-null `error`
  string — it never aborts the whole page (same tolerance `GET /api/channels`
  already applies).
- No secret value is ever returned — only booleans, the public channel id, and
  the quota integer.

### `DELETE /api/channels/{channel}/auth` — disconnect (write)
Channel-scoped (consistent with `GET /api/channels/{channel}/auth` and
`…/auth/connect`). Steps:
1. `{channel}` is validated by `pathnames.validate_segment` before any
   filesystem touch (via `_load_channel`, which already validates + reads
   `channel.json`); an unknown channel → 404.
2. Resolve the channel id from `channel.json` (`_load_channel(channel)["id"]`)
   — the same key `get_auth`/connect use.
3. `auth.forget_credentials(channel_id, auth_dir=<workspace>/auth)`.
4. `True` → `200 {"disconnected": "UC…"}`; `False` (no token) → `404` with a
   clear detail ("channel was not connected").

Manual (render-only) channels have no token anyway, so a disconnect there is a
plain 404 — no special-casing needed. Disconnect never runs consent and imports
no google (token deletion is a local file op).

### Untouched invariants
Password never handled (consent is the operator's browser flow via the existing
connect job); secrets never logged/echoed/returned; upload privacy unchanged;
`client_secret.json`/`quota.json` never deleted. Connect / switch-account stay
the existing `POST …/auth/connect` (+ `force`) + job poll — no new path.

## Frontend

### Router (fourth screen)
`useRoute.ts` gains a `settings` screen at path `/settings`; `Root.tsx` renders
`<SettingsScreen />` for it. A **Settings** link in the start screen's
`NavScreen` chrome (the channels list) navigates there. The SPA fallback already
serves `index.html` for any non-`/api` path, so a deep link / reload on
`/settings` lands correctly.

### `SettingsScreen.tsx`
Under `NavScreen` chrome, breadcrumb `Channels › Settings`. Two sections:

- **Workspace panel (read-only):** data root + origin (`YT_SHORTS_DATA` /
  default `~/YT-Shorts-Data` / repository fallback), channel count. If
  `google_upload_available` is `false`, an inline note: upload libs missing,
  `.venv/bin/pip install google-api-python-client google-auth-oauthlib`.
- **Connection table**, one row per channel: slug, `channel_id` (monospace),
  an upload-class badge (`api` / `manual`), a connection badge
  (connected / not connected), the quota estimate ("~6 uploads left today",
  warn-only). Per-row actions:
  - `api` + connected → **Switch account** (existing connect job with
    `force: true`) and **Disconnect**.
  - `api` + not connected → **Connect** (existing connect job).
  - `manual` → no button, text "render-only — no API upload" (mirrors
    `upload_policy`; the backend would refuse connect with 409 anyway).
  - A row with a non-null `error` shows the error and offers no actions.

**Connect / switch** reuse the existing connect dialog behaviour from
`AuthStatusBar` (send a channel id, start `POST …/auth/connect`, poll the job).
**Disconnect** opens a typed-confirmation modal (type the `channel_id`, via the
existing `deleteConfirmed` helper), then `DELETE …/auth`, then reloads the list.

### `api.ts`
Add `getSettings(): Promise<SettingsResponse>` and
`disconnectAuth(channel: string): Promise<{ disconnected: string }>` (reusing
`asJson`/`ApiError`). Connect reuses the existing `startConnect`. No new pure
`*.ts` module is needed (the confirmation reuses `deleteConfirmed` from
`eventAdmin.ts`); if any display-only mapping (status → label) grows non-trivial
it goes in a small pure helper, unit-tested, not exported from a component.

## Error handling

- Connect with google libs missing → backend 503 → shown inline in the panel,
  page stays usable.
- Disconnect with no token → 404 → surfaced as "was not connected", list
  refreshed.
- `manual` channels never offer Connect (backend would 409).
- An unreadable channel is listed with its `error` and no actions — never kills
  the whole page.
- A failed `GET /api/settings` → the standard error alert with "check the studio
  server is still running", like the other screens.

## Testing

- **pytest (studio API):** `GET /api/settings` aggregates a mixed workspace
  (a connected api channel, a not-connected api channel, a manual channel, an
  unreadable channel) with google/quota stubbed the way the existing auth tests
  stub them (no network, no google, no real consent); asserts the workspace
  fields (`origin`, `channel_count`, `google_upload_available`) and each row.
  `DELETE …/auth` removes exactly `token-<id>.json` and nothing else in `auth/`,
  returns the id, 404s with no token, and rejects a traversal `{channel}` before
  any filesystem touch.
- **pytest (unit):** `auth.forget_credentials` deletes an existing token
  (`True`), is a no-op on a missing one (`False`), and never touches
  `client_secret.json`/`quota.json` in the same dir.
- **Vitest:** only if a display helper is added; otherwise nothing new.
- **E2E (real Chromium):** open Settings from the start screen, see the channels
  listed; **seed a fake token file**, click Disconnect, confirm by typing the
  channel id, assert the token file is gone and the row flips to "not
  connected". (Connect is not E2E-able — it is a real OAuth browser flow.)
- Rebuild and commit `static/`.

## Deferred (with reason)
- **Detection defaults** (no global store; rarely tuned) — see scope above.
- **Token expiry / last-refreshed display** — `load_credentials` returns creds
  but exposing expiry adds google coupling to the read path; connected/not is
  enough for the overview.
- **Bulk connect/disconnect** — one channel at a time is fine for a handful of
  channels.
- **Revoking the grant on Google's side** — disconnect only forgets the local
  token; full revocation stays a manual step in the operator's Google account
  settings (documented in the UI copy).
