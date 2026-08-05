# Upload metadata & publishing control (tags / description / category / visibility / scheduling) — design

Date: 2026-07-24
Status: approved (design), ready for implementation plan

## Motivation

YouTube upload works (ERF tested), but everything beyond the title is baked in:
`youtube_upload.build_metadata` reads `description` (a template), `tags`,
`category_id` and `made_for_kids` only from `brand.json`'s `upload` section
(per channel/event, NOT UI-editable, NOT per-clip), and hard-codes
`privacyStatus="private"`. The operator wants to control all of it from the app
so nothing has to be fixed up in YouTube Studio afterward:
1. **Tags** — editable, and today only in `brand.json` by hand (no UI).
2. **Description** — editable as real per-short text, not just a template.
3. **Category** — the Gaming category is settable; the operator wants it in the UI.
4. **Visibility** — choose private / unlisted / public per upload.
5. **Publish date** — schedule an upload to go public at a chosen time.

This deliberately **relaxes a documented safety invariant** (see §"Safety
redesign"): the operator has explicitly chosen (in brainstorming) to allow
non-private and scheduled uploads from the app, with guardrails.

## Decided requirements

- **Editable metadata (description / tags / category / made_for_kids):** BOTH
  levels — a channel-level default (UI-editable, in `brand.json`'s `upload`
  section, which `profile.load` already merges per event) AND a per-clip
  override (in `edit.json`). Effective = per-clip override → merged
  channel/event `upload` default → built-in default.
- **Visibility & scheduling:** chosen per upload at upload time in the
  UploadPanel — Private (default) / Unlisted / Public / Scheduled(date+time).
  "Scheduled" maps to `privacyStatus=private` + `status.publishAt` (YouTube
  scheduling always publishes to **public** at that time — the UI says so).
- **Guardrails:** default is Private (unchanged behavior for anyone who does
  nothing). Any non-private or scheduled upload requires an **explicit
  confirmation** in the upload dialog before it fires. Nothing auto-publishes
  except a `publishAt` time the operator set, per upload, with confirmation.
- **The game title (e.g. Gran Turismo 7) is out of scope** — the public
  YouTube Data API cannot write a video's specific game; category (Gaming)
  stays settable. (Decided: no description/tag workaround either.)
- **`upload_policy` unchanged:** a `manual` channel still cannot API-upload at
  all; all of this only applies to `api` channels.

## Architecture

### Backend — `youtube_upload.build_metadata`

`build_metadata(clip, edit, config, *, visibility="private", publish_at=None)`:
- `snippet.title` = `editorial.effective_title` (unchanged).
- `snippet.description` = the **effective** description:
  - a per-clip free-text description (`edit.upload["description"]`) if set — used
    verbatim (already the final text, no templating);
  - else the event/channel `upload.description` **template**, formatted with
    `{source_title}`/`{title}` (unchanged behavior, incl. the existing
    invalid-template `UploadError`).
- `snippet.tags` = effective tags: per-clip list if set, else the event/channel
  `upload.tags`, else `[]`. (Per-clip **replaces** the default list — simplest,
  predictable; a merge is the kind of surprise the merge module already avoids.)
- `snippet.categoryId` = effective category: per-clip → event/channel
  `upload.category_id` → `DEFAULT_CATEGORY` ("20", Gaming).
- `status.privacyStatus` = `visibility` (validated ∈ {private, unlisted,
  public}); **defaults to "private"**.
- `status.publishAt` = `publish_at` (RFC3339 UTC) **only when set**; per YouTube
  it is honored only with `privacyStatus=private`, so a "scheduled" upload is
  `visibility=private` + `publish_at=<time>`. `build_metadata` rejects
  `publish_at` combined with a non-private `visibility` (a caller bug).
- `status.selfDeclaredMadeForKids` = effective made_for_kids (unchanged source).
- Length/shape guardrails mirroring YouTube limits so the operator gets a clear
  error, not a raw API 400: title ≤ 100 chars, description ≤ 5000, the tags'
  combined length ≤ 500, each tag a non-empty string; `publish_at` a valid
  RFC3339 timestamp. Violations raise `UploadError` with a clear message.

`build_metadata` stays pure and fully unit-testable (no network).

### Backend — per-clip upload metadata in `editorial`

`editorial.Edit` gains an optional `upload: dict | None = None` field carrying
only the keys the operator overrode (`description`, `tags`, `category_id`,
`made_for_kids`). `load`/`save` read/write it (save omits it when absent, like
every other optional field). A validator rejects wrong types (description a
string, tags a list of strings, category_id a string/int, made_for_kids a
bool). A helper `editorial.effective_upload(edit, config) -> dict` resolves the
per-clip-over-event/channel merge for `build_metadata` to consume. This keeps
edit.json the single home for a human's per-clip decisions, consistent with
title/window/transcript.

### Backend — event/channel upload defaults admin

The `upload` section's metadata fields (`description`, `tags`, `category_id`,
`made_for_kids` — NOT `mode`) become UI-editable at the **channel** level
(`brand.json`'s `upload` section). Deliberately NOT the event brand editor:
`upload` was intentionally excluded from event-level overrides (OVERRIDE_SECTIONS
in `event_brand_admin`) and stays so. `profile.load` still deep-merges the whole
brand per event, so an event `brand.json` that already carries an `upload`
override keeps working — this feature just doesn't add an event-level editor for
it. The "default" level is therefore the channel `upload` section; the per-clip
override in `edit.json` sits on top. The plan extends `brand_admin` (least
duplication) with the metadata fields; validation mirrors
`profile._validate_upload` so an accepted default is one `profile.load` accepts,
and `upload.mode` stays managed only by the existing api/manual toggle.

### Backend — routes & job

- `POST …/clips/{name}/upload` (`post_upload`) gains a body: `{ visibility?,
  publish_at?, confirm? }`. It refuses (400) a non-private or scheduled upload
  unless `confirm` is true (the server-side half of the guardrail — the UI
  checkbox is the client half). `visibility` defaults to `private`. It threads
  `visibility`/`publish_at` into the upload job → `build_metadata`.
- The upload job records the **actual** resulting `privacyStatus` returned by
  the insert (YouTube may force `private` on an unverified channel even when
  `public` was requested), so the UI can report "YouTube kept this private
  (channel not verified for public API uploads)" instead of implying success.
- `PATCH …/clips/{name}` (the existing edit route) gains the per-clip `upload`
  override fields, saved to `edit.json` via `editorial.save`.
- `GET …/clips/{name}/upload-preview` returns the full effective metadata
  (title/description/tags/category/made_for_kids) so the panel shows exactly
  what an upload would send; visibility/scheduling are a per-upload choice shown
  live in the panel, not part of the stored preview.

### Frontend — UploadPanel

- An **editable metadata** area (per clip): description (textarea, seeded from
  the effective/template), tags (chips/comma input), category (Select over a
  curated list incl. Gaming=20, Sports=17, Autos=2, People&Blogs=22,
  Entertainment=24), made_for_kids (switch). Saved to `edit.json` via the edit
  route (its own Save, like the clip editor's — or folded into the existing
  Save). Character counters against the YT limits.
- A **visibility** control at upload time: Private (default) / Unlisted /
  Public / Scheduled. "Scheduled" reveals a date-time picker (operator's local
  tz → RFC3339 UTC) and states clearly "the video becomes **public** at this
  time." Anything other than Private reveals a **confirmation checkbox** that
  must be ticked before the Upload button enables.
- The event/channel **upload defaults** get a small editor (in the channel/
  event brand or settings area), same widgets, so the per-clip fields have a
  sensible seed.
- Pure helpers (effective-metadata shaping, tag parsing, RFC3339 conversion,
  the confirm-gate predicate) live in a non-component `.ts` module, Vitest-
  tested, like `brand.ts`/`eventBrand.ts`.

### Safety redesign (documented, deliberate)

The current hard invariant — "Privacy is always `private`; config cannot
override it; nothing uploads public or auto-publishes on any signal" — is
**replaced** by a new, still-conservative contract, and `CLAUDE.md` plus the
`youtube_upload.py`/`build_metadata` comments are updated coherently (the code
must not contradict its own docs):

> **Default privacy is `private`.** A non-private (`unlisted`/`public`) or
> scheduled (`publishAt`) upload happens ONLY when the operator explicitly
> selects it AND confirms it, per upload, in the studio. There is still no
> auto-publish on any derived signal — the only thing that makes a video public
> is an explicit operator choice (immediately, or at a `publishAt` time they
> set). `manual` channels never API-upload at all.

This is the crux change and must be reviewed as such.

### Security / correctness

- `visibility` is validated against a fixed allow-list; `publish_at` parsed as
  RFC3339 and required to be in the future; `category_id` numeric; tags a list
  of strings — all before any API call.
- Secrets unchanged: tokens stay in `<workspace>/auth/`, never logged; the
  upload job logs only the video id/url and the resulting privacy status.
- The re-upload guard (`upload.json`) is unchanged: a second upload of the same
  clip still needs an explicit force.

## Testing

- **`build_metadata` (pure):** default private; explicit unlisted/public;
  scheduled = private+publishAt; publishAt+non-private rejected; effective
  metadata resolution (per-clip over event/channel over built-in); length/shape
  guardrails (title/description/tags caps, bad publishAt, bad category) raise
  `UploadError`; the existing invalid-template error still fires.
- **`editorial`:** the new `upload` override round-trips through save/load,
  validates types, and `effective_upload` merges correctly; absent → omitted.
- **upload defaults admin:** UI-editable defaults validate like `profile.load`;
  `mode` never altered here.
- **Studio API:** `post_upload` refuses non-private/scheduled without `confirm`
  (400) and honors it with `confirm`; threads visibility/publishAt; records the
  actual resulting privacyStatus; `PATCH` saves per-clip upload overrides;
  `upload-preview` shows effective metadata. All with the injected fake
  service — no google, no network (as today).
- **Vitest:** the pure UI helpers (effective metadata, tag parsing, RFC3339
  conversion, confirm-gate).
- **Playwright E2E:** open UploadPanel, edit description/tags/category (assert
  saved to edit.json + reflected in upload-preview), pick "Public" → confirm
  checkbox gates the button → (stubbed upload job like the existing upload E2E)
  assert the chosen visibility reaches `build_metadata`; a "Scheduled" case
  asserts private+publishAt.
- Full pytest suite green, `npm test` green, `python3 tools/lint.py` green,
  `npm run build` committed (`static/`).

## Out of scope (explicitly)

- The specific **game title** field (not writable via the public Data API) — no
  description/tag workaround either (decided).
- The **moments/keyword lexicon UI** (`moments.json`) for short detection — a
  separate follow-up conversation the operator flagged.
- Bulk visibility/scheduling across many clips at once (per-upload for now).
- Editing an already-uploaded video's metadata/visibility after the fact
  (that stays a manual step; this feature is about the upload itself).
- Playlists, thumbnails, end screens, captions upload.

## Notable risks / decisions carried forward

- **Relaxing the privacy invariant is the central, deliberate change** — the
  guardrail (default private + explicit per-upload confirmation, enforced on
  BOTH the server and the client) is what keeps an accidental public upload of
  private race footage from happening. Review this as the highest-risk item.
- **Scheduling always goes public** at the chosen time (YouTube behavior) — the
  UI must state this so "scheduled" is never mistaken for "scheduled unlisted."
- **Unverified-channel reality:** YouTube may force `private` regardless of the
  request; the tool reports the actual resulting status rather than implying the
  operator's choice took effect.
- **Per-clip vs default merge:** per-clip tags REPLACE the default list (not
  merge); description free-text REPLACES the template. Chosen for predictability.
