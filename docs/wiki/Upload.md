# Upload

Upload a rendered short to the right YouTube channel, from the studio or the
CLI. It goes up **private** unless you deliberately ask for otherwise and
confirm it. This is the only step that writes to YouTube, so it needs
OAuth — unlike everything else, there is no yt-dlp path.

**One-time setup (yours to do).** Uploading uses the YouTube Data API, which needs
a Google Cloud project and an OAuth client:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   project, enable the **YouTube Data API v3**, and create an **OAuth client ID**
   of type *Desktop app*.
2. Download its `client_secret.json` and place it in your workspace under
   `auth/` (i.e. `$YT_SHORTS_DATA/auth/client_secret.json`, or
   `~/YT-Shorts-Data/auth/`). **Never move it into the repository** — it is a
   credential; the repo's `.gitignore` guards against it, but keep it in the
   workspace regardless.

**Connect a channel** (browser consent — the tool never sees your password).
Either from the studio — a "Connect channel" action opens your browser to
Google's consent screen, with the channel id pre-filled (editable, so you can
connect another channel you manage) — or from the CLI:

```bash
bin/yt-shorts auth <channel>          # e.g. erf
```

Either way opens your browser to Google's consent screen. After you approve, only
a refresh token is stored, in `auth/token-<channelid>.json`, keyed by the
channel's YouTube id. To upload for a channel you manage, connect it while signed
in to the Google account that owns it — that is all "switching account" means
here.

**Render-only channels.** The YouTube Data API can only upload to a channel your
Google account **owns** (your personal channel and owned brand accounts). A channel
you only **manage or edit** cannot be uploaded to via the API — it does not even
appear in Google's consent chooser. For such a channel, set
`"upload": { "mode": "manual" }` in its `brand.json`: the tool then never offers a
connect or an API upload for it (both are refused with a clear message), and the
studio instead shows a **Download short** button and the prepared
title/description/tags to copy, so you upload the short by hand in YouTube Studio.
Owned channels need no flag — `mode` defaults to `api`.

**Upload:**

```bash
bin/yt-shorts upload <channel>/<event>   # uploads every kept, rendered, not-yet-uploaded short
```

Or from the studio: a kept, rendered clip shows an Upload action that displays the
exact metadata for you to confirm, then uploads and shows the resulting private
video's URL.

- **Private by default, everywhere.** Every upload is `private` unless you ask for
  something else: the CLI has no visibility flag at all, and a queued bulk upload
  is always private with no way to change it. In the studio you can choose
  `unlisted`, `public`, or a scheduled publish time, but only per upload, and only
  by selecting it and then confirming it in a modal that names the exposure - the
  server refuses an unconfirmed one, and a `manual` channel is never API-uploaded
  at all. Scheduling does exist (`publishAt`, accepted only alongside `private`),
  and YouTube itself makes the video public at the moment you set; nothing here
  publishes on a signal of its own, and what lands in `upload.json` is the privacy
  YouTube returned, not the one requested.
  How that is enforced, on the server and in the studio, is in the
  [upload-to-youtube skill](https://github.com/jegr78/yt-shorts/blob/main/.claude/skills/upload-to-youtube/SKILL.md).
- **Re-upload guard.** A successful upload writes `upload.json` in the clip's
  directory; the tool then shows "uploaded" and refuses a second upload unless you
  explicitly ask for it (studio: a re-upload confirmation; the CLI skips
  already-uploaded clips).
- **Quota.** `videos.insert` costs ~1600 of a default 10,000 units/day, so about
  **6 uploads a day**. The tool keeps a local per-day estimate (resetting at
  midnight Pacific, when YouTube's quota resets) and warns as you approach it; a
  `quotaExceeded` from the API is reported plainly. The estimate only warns — the
  API is the authority.

**Metadata** comes from an optional `upload` block in the channel's `brand.json`
(an event may override it), all with defaults:

```json
{
  "upload": {
    "description": "Clip from {source_title}.",
    "tags": ["simracing", "endurance"],
    "category_id": "20",
    "made_for_kids": false
  }
}
```

`{source_title}` and `{title}` interpolate from the clip. The **title is not
configured here** — it is the clip's effective hook, the same text burned into the
short, so what a viewer reads matches what they see. `category_id` defaults to
`"20"` (Gaming); `made_for_kids` is always sent (YouTube requires the
declaration) and defaults to `false`.

The Google libraries are an **optional dependency**, like FastAPI — install them
only if you upload. Everything else in the tool works without them:

```bash
.venv/bin/pip install google-api-python-client google-auth-oauthlib
```

`pip install ".[all]"` already brings both in — they are part of the `cloud`
extra, alongside the model-provider SDKs.
