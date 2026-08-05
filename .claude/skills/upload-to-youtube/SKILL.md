---
name: upload-to-youtube
description: Stage E - the one part of this project that writes to YouTube. Privacy defaults, the per-upload confirmation gate, OAuth and where secrets live, the quota estimate, the api/manual channel split and the re-upload guard. Read BEFORE touching auth.py, youtube_upload.py, upload_record.py, quota.py, upload_policy.py or any upload route.
---

# Upload (stage E)

Moved here VERBATIM out of the repository-root `CLAUDE.md`. The root file keeps
the safety-critical prohibitions in `## Hard constraints` and points here for
the full reasoning.

**Upload (stage E) is the one part that writes to YouTube, and it has hard rules.**
Uploading needs the YouTube Data API + OAuth (no yt-dlp path), so the Google
libraries (`google-api-python-client`, `google-auth-oauthlib`) are an OPTIONAL
dependency, imported LAZILY exactly like FastAPI: `auth.py`, `youtube_upload.py`,
`upload_record.py`, `quota.py` import no google at module scope (`google_oauth.py`
is the one thin wrapper, and even it imports google only inside its methods).
Every OAuth and network boundary is INJECTED — `auth.authorize`/`load_credentials`
take an `oauth` adapter, `youtube_upload.upload_short` takes a built `service`,
`quota` takes a `now` clock — so the whole stage tests with no network, no google,
no real consent. Never add a module-scope google import; never let a test perform
a real OAuth flow or upload.

Non-negotiable invariants:
- **Privacy defaults to `private`; anything more exposed is an explicit,
  confirmed, per-upload operator choice.** Default privacy is `private`. A
  non-private (`unlisted`/`public`) or scheduled (`publishAt`) upload happens
  ONLY when the operator explicitly selects it AND confirms it, per upload, in
  the studio - enforced on BOTH the server (`post_upload` 400s any
  `visibility != "private"` or non-null `publish_at` unless the request body's
  `confirm` is true) and the client (`UploadPanel`'s confirm modal disables
  "Confirm and upload" until an explicit checkbox naming the exposure -
  "I understand this upload will be public"/"...scheduled to go public
  automatically" - is ticked). There is still no auto-publish on any derived
  signal - the only thing that makes a video public is an explicit operator
  choice, immediately or at a `publishAt` time they set themselves. `manual`
  channels never API-upload - the route refuses them (409) before any upload
  job starts (see `upload_policy` below).
  **Actual vs requested privacy:** `upload_short` records the resulting
  `privacyStatus` FROM the insert response, not the request - an unverified
  channel may be forced private by YouTube regardless of what was requested -
  so the stored `upload.json` record and the studio's "Uploaded" UI reflect
  the real, returned value, never just an echo of what was asked for.
- **Secrets live in `<workspace>/auth/` and never touch the repo or any output.**
  `client_secret.json`, `token-<channelid>.json`, `quota.json` are gitignored;
  never log, echo, or commit them. The tool never handles the Google password -
  consent is the operator's, in their browser. Consent can be started from the
  CLI (`auth`) OR from the studio (`POST /api/auth/connect` runs it as a
  background job that opens the operator's browser via run_local_server); either
  way the password stays with Google and only the refresh token is stored. The
  studio connect dialog pre-fills the channel id from the profile but keeps it
  editable, so the common case is one click while a mistyped id (wrong token key)
  is avoided.
- **`upload.json` records an ACTION**, per clip - not derived (cannot be recreated)
  and not editorial (not a human's correction), so it is its own file. It is the
  re-upload guard: a second upload of the same clip is refused without an explicit
  force. Tokens are keyed by YouTube channel id (`channel["id"]`); "switch account"
  = authorize the right Google account for a channel id.
- **Quota is a LOCAL Pacific-day estimate that only WARNS**, never blocks - the API
  is the authority.
- **A channel is either `api` (owned) or `manual` (render-only).**
  `config["upload"]["mode"]` (brand.json, default `api`) declares it; `upload_policy`
  is the single predicate, enforced at four points (`cmd_auth`, `cmd_upload`,
  `POST /api/auth/connect`, `POST /api/clips/{name}/upload`) - a `manual` channel
  (a YouTube manager/editor delegation the Data API cannot upload to) is refused at
  every API-upload path, and the studio offers a download of the short plus
  copy-to-paste metadata (the existing `/short` and `/upload-preview` routes)
  instead. `manual` never gets a token: the connect verify-and-refuse guard would
  reject it anyway, since a manager/editor channel is not returned by
  `channels.list(mine=true)`. `upload_policy.py` imports nothing heavy (no FastAPI,
  no google), like `subtitle_pipeline.py`.

The whole pipeline arc (Stage 1 -> 2a -> A-C -> D1/D2a/D2b -> E) is now built;
what remains (public release, scheduling, thumbnails, deletion) is deliberately
manual in YouTube Studio.
