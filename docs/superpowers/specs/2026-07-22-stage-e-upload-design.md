# Stage E — Upload to YouTube

**Date:** 2026-07-22
**Scope:** upload a rendered short to the right YouTube channel, as a private
video, from the studio or the CLI — with OAuth, per-channel account handling, a
re-upload guard, and quota awareness. This is the last stage of the pipeline: it
turns a finished `short.mp4` into a video on the channel, which until now has been
a manual step.

## Problem

Everything before this stage produces `short.mp4` files an operator then uploads
by hand through the YouTube Studio web UI. For a channel posting several shorts a
day across its own, managed, and brand channels, that hand-work is the last piece
of friction the tool has not removed.

## Why OAuth and the official library, not yt-dlp

Unlike discovery (D1) and transcription (D2a), uploading has **no yt-dlp path**.
Publishing a video is a write to the operator's own channel and can only go
through the **YouTube Data API v3 `videos.insert`**, which requires **OAuth 2.0**.
That brings setup this project has so far avoided, and the design does not hide it:

- The operator creates a **Google Cloud project and an OAuth client** themselves
  and places the resulting `client_secret.json` in the workspace. The tool cannot
  create it — that needs the operator's Google account.
- The **first-time consent** happens in the operator's browser; the tool never
  handles the Google password. After consent, only a refresh token is stored.
- Upload is **quota-limited**: `videos.insert` costs ~1600 units against a default
  10,000/day, so roughly **6 uploads/day**. The tool tracks this.

The official Google library (`google-api-python-client`,
`google-auth-oauthlib`) handles the OAuth flow, automatic token refresh, and
**resumable** upload (an upload that survives a dropped connection) far more
safely than hand-rolled `requests` code would. This is the case where "standard"
earns its place.

## Dependencies (optional, like FastAPI)

`google-api-python-client` and `google-auth-oauthlib` are installed into `.venv`
but are an **optional layer**: only the upload/auth modules import them.
`harvest`, `render`, `gallery`, and the studio's non-upload routes must keep
working with them uninstalled, and their absence produces a readable "run `pip
install …`" message, not an `ImportError` traceback — exactly how FastAPI is
handled for the studio today.

## Architecture

Two new modules, both outside `yt_shorts/studio/` so the CLI can use them without
FastAPI, and both with the Google client injected so tests never touch the
network or a real OAuth flow.

### `auth.py` — credentials and accounts

- `authorize(channel_id, *, flow_factory=..., token_store=...) -> Credentials` —
  runs the installed-app OAuth flow (browser consent) the first time, then stores
  the refresh token for that channel and reuses/refreshes it after. `flow_factory`
  and `token_store` are injected so tests exercise storage, refresh, and the
  channel-to-token mapping without a real Google round-trip.
- **Tokens are keyed by YouTube channel id.** A tool channel (`channels/<name>/`)
  already carries its YouTube channel id in `channel.json`; uploading its shorts
  targets that channel, so its token is stored as `token-<channelid>.json`.
  "Switch account" is nothing more than authorizing the right Google account for a
  given channel id — the account whose consent grants upload to that channel.
- `AuthError` for a missing `client_secret.json`, a revoked token, or a consent
  that does not grant the upload scope — each an understandable message, not a
  traceback.
- The requested scope is exactly `youtube.upload` (the narrowest scope that can
  insert a video); listing the operator's channels to confirm the id uses
  `youtube.readonly` if needed, requested together.

### `youtube_upload.py` — the upload itself

- `build_metadata(clip, edit, config) -> dict` — pure: the video's snippet and
  status from the clip and the channel's `upload` config. Title is the effective
  hook (`editorial.effective_title`); description, tags, `categoryId`, and
  `madeForKids` come from the channel `upload` block (defaults below); privacy is
  **always `private`** at upload time.
- `upload_short(short_path, metadata, *, service=...) -> UploadResult` — a
  **resumable** `videos.insert`; `service` (the built API client) is injected so
  tests pass a fake that records the call and returns a video id. `UploadResult`
  carries the new `video_id` and its watch URL.
- Quota errors (`quotaExceeded`, 403) and transient errors surface as a clear
  `UploadError`; the resumable upload retries transient failures itself.

## Metadata and the channel `upload` config

A per-channel `upload` block in `brand.json` (event may override, like the rest of
brand config), all optional with defaults so a channel needs none:

- `description` — a template; `{source_title}` and `{title}` may be interpolated
  from the clip. Default: a short line crediting the source stream.
- `tags` — list; default empty.
- `category_id` — YouTube category; default `"20"` (Gaming) — sim racing sits
  there more naturally than Sports; the operator can set `"17"` (Sports).
- `made_for_kids` — default `false`. YouTube **requires** this declaration on every
  upload, so it is always sent, defaulting to not-for-kids.
- The title is **not** configured here — it is the clip's effective hook, the same
  text burned into the short, so what a viewer reads matches what they see.

## Re-upload guard

An upload has an irreversible external effect and costs scarce quota, so it must
not happen twice by accident. On success, a per-clip `upload.json` records the
`video_id`, the time, and the privacy it was uploaded at. It is neither derived
(it cannot be re-created) nor editorial (it is not a human's correction) — it is a
record of an action, so it gets its own file rather than living in `edit.json` or
being written by a derivation step. The studio shows an "uploaded" state from it
and refuses a second upload of the same clip unless the operator explicitly asks
to upload again.

## Quota awareness

A `quota.json` in the workspace auth area tracks units spent per day, per channel
account, resetting at midnight **Pacific time** (when YouTube's quota resets). An
`videos.insert` books ~1600 units. The tool warns as the day's uploads approach
the ~6 the default quota allows, and a `quotaExceeded` from the API is reported as
"today's quota is used up, it resets at midnight Pacific", not a raw error. The
count is a local estimate — the API is the authority — so it never *blocks* an
upload the API would accept; it only warns.

## Secrets and the workspace

Everything secret lives in the workspace, never the repository, and is never
printed:

```
<workspace>/auth/
  client_secret.json          operator-provided (their GCP OAuth client)
  token-<channelid>.json       per-channel refresh token (created on consent)
  quota.json                   local per-day upload-unit estimate
```

The tool never logs, echoes, or includes these in any output. `client_secret.json`
and the token files are the operator's credentials. In normal use the workspace is
outside the repo (`YT_SHORTS_DATA` or `~/YT-Shorts-Data`, see `workspace.py`), so
secrets are not near the repo at all. But `workspace.resolve()`'s **last-resort
fallback is the repository's own `channels/`**, and if that path is ever taken the
`auth/` directory would sit inside the repo tree — so as defense in depth:

- `.gitignore` gains explicit entries for `auth/`, `client_secret*.json`,
  `token-*.json`, and `quota.json`, so these can never be staged even if the
  workspace resolves to the repo.
- When the tool would write a secret and the resolved workspace root **is** the
  repository, it warns that secrets are being stored inside the repo and points to
  `YT_SHORTS_DATA` / `~/YT-Shorts-Data` as the proper location.

The docs also say plainly: never move these files into the repo.

## Studio integration

- **Auth status** — the studio shows whether the current channel is connected and,
  if not, a "Connect account" action that starts the consent flow (which opens the
  operator's browser; the tool never sees the password).
- **Upload action** — offered only on a clip that is **kept** and has a rendered
  `short.mp4`. It shows the exact metadata (title, description, tags, privacy:
  private) for the operator to confirm, uploads on confirmation, and then shows the
  resulting private video's URL. A clip with an `upload.json` shows "uploaded" and
  the upload action asks for explicit confirmation before uploading again.
- **Quota indicator** — how many of today's uploads remain by the local estimate.
- Uploading is a background job like rendering (a resumable upload of a video can
  take a while), reusing the existing job surface and progress polling.

## CLI

- `bin/yt-shorts auth <channel>` — authorizes the channel (browser consent) and
  stores its token; reports which YouTube channel it connected.
- `bin/yt-shorts upload <channel>/<event>` — uploads the kept, rendered, not-yet-
  uploaded shorts as private, honouring the re-upload guard and the quota warning,
  and reporting each resulting video URL. One failed upload never aborts the run,
  the same guarantee `render` gives.

## Security (hard requirements, wired into the design)

- **Default privacy is `private`.** A short is never uploaded public by default.
- **Every upload is explicitly confirmed** — from the studio, the operator confirms
  the metadata; from the CLI, `upload` is itself the explicit act, and it uploads
  private. Nothing auto-publishes.
- **The tool never handles the Google password.** OAuth consent is the operator's,
  in their browser.
- **Secrets are never committed or printed.** Workspace-only, gitignored by living
  outside the repo, and never echoed in logs or output.
- Making a video **public** is out of scope for this stage — it stays a manual step
  the operator does in YouTube Studio after reviewing the private upload.

## Testing

- Auth: token stored and reused, refresh path, channel-to-token mapping, a missing
  `client_secret.json` and a missing token each an `AuthError` — all against an
  injected flow/token store, no real OAuth.
- Metadata: title is the effective hook, description template interpolation, the
  `upload` config defaults, privacy always `private`, `made_for_kids` always
  present.
- Upload: a fake injected `service` records a resumable `videos.insert` and returns
  a video id; `UploadError` on a simulated `quotaExceeded`; the watch URL is built
  from the returned id.
- Re-upload guard: a clip with `upload.json` is refused a second upload unless
  forced; the record is written on success.
- Quota: the per-day counter books units, resets at the Pacific-time boundary
  (injected clock), warns near the limit, never blocks.
- Studio and CLI: the upload route/command against a stubbed uploader; the
  optional-dependency message when the Google libraries are absent.
- Nothing in the suite performs a real OAuth flow, a real upload, or a network
  call.

## Not in scope

- Publishing a video (making it public), scheduling, playlists, thumbnails,
  end screens — the operator does these in YouTube Studio after reviewing the
  private upload.
- Editing or deleting an already-uploaded video.
- Raising the API quota (a Google-side request), or CMS/content-owner
  (`onBehalfOfContentOwner`) uploads — a managed-channel operator authorizes each
  channel's own account instead.
- Automatic public release on any signal — deliberately never built.
