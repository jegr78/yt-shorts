/**
 * Client for the studio's HTTP surface (see ../../api.py - read that file's
 * routes and field names before changing anything here; this module must
 * mirror it exactly, not what would be convenient).
 *
 * The API is WORKSPACE-level and every route is path-scoped: the event
 * routes live under /api/channels/{channel}/events/{event}/… and auth under
 * /api/channels/{channel}/auth. The active {channel, event} is threaded
 * through the whole event-scoped surface by a single module-level scope the
 * router sets (setScope) when it enters the editor screen - so every
 * component's own call signature stays unchanged (no prop-drilling the
 * scope through PreviewPane/ClipEditor/…), and every scoped URL is built
 * from the one pure builder in scopedApi.ts. The two listing calls
 * (getChannels/getEvents) and getJob are not event-scoped and take their
 * own path directly.
 *
 * Every base is ABSOLUTE ("/api/…"): the SPA router changes the page path
 * to /{channel}/{event}, so a relative "api/…" would resolve against that
 * and hit the wrong URL (see scopedApi.ts).
 */

import { channelBase, eventBase } from './scopedApi'
import type { JobEntry, JobPlan } from './jobs'
import type { Moment } from './momentList'
import type { ProviderState } from './providers'

// Re-exported for the same reason Moment is: providers.ts owns the shape
// (it is what every pure rule in that module reads), and this module's own
// SettingsResponse carries it, so a call site may import it from either.
export type { ProviderState }

// Re-exported so existing `import type { Moment } from './api'` call sites
// (there are none left as of this writing, but the shape is part of this
// module's public surface via StreamAnalysis below) keep working - momentList.ts
// is the single source of truth for this interface, not a second copy of it.
export type { Moment }

/** The event this browser is currently editing. Set by the router when it
 * mounts the editor screen (see App/useRoute), read by every event-scoped
 * call below. Null until then - a scoped call made before the editor is
 * open is a bug, so it throws rather than silently building "/api/channels/
 * undefined/…". */
let scope: { channel: string; event: string } | null = null

export function setScope(channel: string, event: string): void {
  scope = { channel, event }
}

function eventScope(): string {
  if (!scope) throw new Error('event-scoped API called before the editor scope was set')
  return eventBase(scope.channel, scope.event)
}

function channelScope(): string {
  if (!scope) throw new Error('channel-scoped API called before the editor scope was set')
  return channelBase(scope.channel)
}

/** The {channel, event} the scoped calls above are built from, as VALUES.
 *
 * Every queue entry names its own channel and event in its params (see
 * studio/worker.py's table) - `POST /api/jobs` is workspace-level, so there is
 * no path to carry them. A component that enqueues work therefore needs the
 * two strings, not a URL; without this it would have to prop-drill them from
 * the router into ClipEditor and back out, which is the exact thing this
 * module-level scope exists to avoid (see this file's own docstring).
 * Throws for the same reason `eventScope` does: a scoped call made before the
 * editor is open is a bug, not something to paper over with "undefined". */
export function currentScope(): { channel: string; event: string } {
  if (!scope) throw new Error('event-scoped API called before the editor scope was set')
  return scope
}

export type ClipStatus = 'candidate' | 'kept' | 'discarded'

export interface Word {
  start: number
  end: number
  text: string
}

/** The shape returned by GET /api/clips (one entry per clip). */
export interface ClipSummary {
  name: string
  harvested_title: string
  effective_title: string
  status: ClipStatus
  has_short: boolean
  /** Opaque token that changes whenever the rendered short's bytes do (see
   * _short_version in studio/api.py); null exactly when has_short is false.
   * It belongs in the player's URL: without it the <video> src is a
   * constant, so a re-render leaves the element pointing at the same URL and
   * the browser answers from its own cache. Never parsed here - passed
   * through. */
  short_version: string | null
  has_transcript: boolean
  has_edit: boolean
  /** Whether raw.mp4 has been downloaded for this clip - the exact
   * condition the preview routes 409 on (see PreviewUnavailableError) and
   * the thing a render would still have to fetch before doing anything
   * else with this clip. Surfaced in the empty-state summary. */
  has_raw: boolean
  duration: number
  /** Whether this clip has an upload.json record (see
   * yt_shorts.upload_record.is_uploaded) - the server-authoritative
   * "uploaded" state, so a page reload still shows an uploaded clip as
   * uploaded rather than only what this browser session watched a job
   * finish for (see UploadPanel.tsx's own docstring). */
  has_upload: boolean
  /** The uploaded video's private YouTube URL, or null when has_upload is
   * false. Comes straight from upload.json - never fabricated client-side. */
  upload_url: string | null
  /** The operator's head/tail cut in seconds, or null for none. */
  trim: [number, number] | null
  /** The cut short.mp4 currently embodies, or null. Unequal to `trim` means
   * the cut has not been applied yet - see trim.ts's isPending. */
  trim_applied: [number, number] | null
  /** True when short.mp4's cut state cannot be trusted at all - an
   * untrimmed master survives beside it with no readable record of what
   * short.mp4 currently is (see trim.py's `is_unknown`: a crash between a
   * cut landing and its state being recorded, or a corrupted/deleted
   * sidecar). `trim`/`trim_applied` alone cannot express this - both read
   * null/null here, identical to an ordinary never-trimmed clip - which is
   * why this is its own field rather than inferred client-side. See
   * trim.ts's `trimNeedsAction`/`trimActionLabel`, which are what actually
   * consume it. */
  trim_unknown: boolean
}

/** GET /api/clips/{name} and the body PATCH /api/clips/{name} returns. */
export interface ClipDetail extends ClipSummary {
  words: Word[]
  conflict: boolean
  /** The moment's originally detected window in seconds into the source
   * stream, [start, end] - fixed, derived data (see api.py's get_clip
   * route). GET /api/clips/{name} always carries this (every clip has a
   * start/end, moment or not); GET /api/clips (the list route) never
   * does, which is why this lives here and not on ClipSummary.
   *
   * IMPORTANT: PATCH /api/clips/{name} does NOT echo these two fields
   * back (see api.py's patch_clip route - it returns `_summary` plus only
   * `words`/`conflict`). A caller that just PATCHed must carry
   * `detected_window` forward from the clip it already had and compute
   * the new `effective_window` itself from whatever it just sent, rather
   * than trusting the PATCH response to include either - see
   * ClipEditor.tsx's `withWindow` helper, which is the one place this is
   * done. */
  detected_window: [number, number]
  /** The window actually in effect: the operator's override if one is
   * saved, else `detected_window` (see `yt_shorts.editorial.effective_window`).
   * Same PATCH-response caveat as `detected_window` above. */
  effective_window: [number, number]
}

export interface PatchClipBody {
  /** Present-but-null clears an override back to the harvested title -
   * distinguished from "omitted" by the key literally being absent from
   * the JSON body, so callers must not send `title: undefined` meaning
   * "unset" - leave the key out entirely instead. */
  title?: string | null
  status?: ClipStatus
  words?: Word[]
  /** [start, end] to override the detected window, or null to clear an
   * existing override back to it - same present-but-null-vs-omitted rule
   * as `title` (see api.py's PatchClipBody.window). Omit the key entirely
   * when the window was not touched, so an unrelated save (title-only,
   * status-only) never turns an unset window into an explicit override. */
  window?: [number, number] | null
  /** A per-clip upload metadata override (description/tags/category_id/
   * made_for_kids - see yt_shorts.editorial.effective_upload), or null to
   * clear it back to the channel/event default. Same present-but-null-vs-
   * omitted rule as `title`/`window` above (see api.py's
   * PatchClipBody.upload). Every field within is itself optional - only the
   * ones set here override the channel/event's own upload config. */
  upload?: {
    description?: string
    tags?: string[]
    category_id?: string
    made_for_kids?: boolean
  } | null
  /** [head, tail] seconds to set as the operator's trim, or null to clear an
   * existing one back to none - same present-but-null-vs-omitted rule as
   * `title`/`window` above: omit the key entirely when the trim was not
   * touched. Saved through this SAME PatchClipBody rather than a dedicated
   * route (see CLAUDE.md's trim section) - ClipEditor.tsx's handleApplyTrim
   * is the one caller, sending it together with the job that actually cuts
   * the file - which is a QUEUE ENTRY now (`enqueueJob('trim', …)`), not the
   * direct `POST …/clips/{name}/trim` the deleted `applyTrim` wrapper used to
   * call. See the note where `applyTrim` and `startRender` used to sit, below
   * `createClipFromWindow`. */
  trim?: [number, number] | null
}

export type ClipResultStatus = 'done' | 'failed' | 'skipped'

export interface ClipResult {
  status: ClipResultStatus
  reason: string | null
}

export type JobStatus = 'running' | 'done' | 'failed'

export interface Job {
  id: string
  status: JobStatus
  results: Record<string, ClipResult>
  log: string[]
  /** The backing log file's name (see jobs.py's `Job.snapshot`), e.g.
   * `render-<id>.log` - the bare, pre-rotation name even once
   * `finish_job_log` has gzipped it, since `snapshot` reads it straight off
   * `self.log_path` rather than re-stat'ing the directory. Null when the
   * job's log could not be opened (`_open_job_log`'s best-effort failure) -
   * used to gate each job panel's "View log" link (see logs.ts's
   * `logBaseName`, which is what lets that link's name still match the
   * Logs screen's listing after rotation). Optional here, not required, so
   * hand-built `Job` fixtures elsewhere (useJobPolling's synthetic failure,
   * existing tests) need no update. */
  log_name?: string | null
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** A 409 from the preview route specifically - no raw.mp4 to draw from
 * yet. Kept as its own type so callers can show the "render this clip
 * first" message instead of a generic error. */
export class PreviewUnavailableError extends ApiError {}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      if (body && typeof body.detail === 'string') detail = body.detail
    } catch {
      // body wasn't JSON - fall back to statusText already set above
    }
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<T>
}

/** The shape returned by GET /api/channels (one entry per channel), for the
 * start screen. A channel whose channel.json is missing/malformed carries a
 * non-null `error` and empty identity fields - it is listed, but not
 * openable (see workspace_listing.list_channels). */
export interface ChannelInfo {
  name: string
  display_name: string
  handle: string
  event_count: number
  error: string | null
}

/** The shape returned by GET /api/channels/{channel}/events (one per event),
 * for the event-list screen (see workspace_listing.list_events). */
export interface EventInfo {
  name: string
  clip_count: number
  kept_count: number
  rendered_count: number
}

/** GET /api/channels - every channel in the workspace, summarised. Not
 * event-scoped: the start screen calls it before any scope exists. */
export function getChannels(): Promise<ChannelInfo[]> {
  return fetch('/api/channels').then(asJson<ChannelInfo[]>)
}

/** The body POST /api/channels takes: the slug (the new channel's directory
 * name) plus the six required channel.json identity fields (see
 * yt_shorts.channel_admin.REQUIRED_FIELDS). Channel create/list are NOT
 * event-scoped - they use the absolute /api/channels path directly. */
export interface ChannelCreatePayload {
  slug: string
  id: string
  channel_url: string
  handle: string
  display_name: string
  language: string
  footer: string
}

/** POST /api/channels (see api.py's create_channel) - creates the channel
 * directory (channel.json + a scaffold brand.json + empty fonts/ and events/)
 * and returns its listing entry. 400 for a bad slug or an empty field, 409 when
 * the slug already exists; each carries a `detail` the caller should show
 * as-is (see ApiError). A created channel is INCOMPLETE until fonts are
 * uploaded and assigned in its Brand tab. */
export function createChannel(payload: ChannelCreatePayload): Promise<ChannelInfo> {
  return fetch('/api/channels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(asJson<ChannelInfo>)
}

/** The channel.json identity fields GET /api/channels/{channel} returns (see
 * api.py's get_channel). `assets` and any other stored key ride along too. */
export interface ChannelFields {
  id: string
  channel_url: string
  handle: string
  display_name: string
  language: string
  footer: string
  [key: string]: unknown
}

/** GET /api/channels/{channel} (see api.py's get_channel) - the channel.json
 * identity fields, for the channel-details editor. 404 for an unknown channel. */
export function getChannel(channel: string): Promise<ChannelFields> {
  return fetch(channelBase(channel)).then(asJson<ChannelFields>)
}

/** PATCH /api/channels/{channel} (see api.py's edit_channel) - a PARTIAL merge:
 * only the fields present in `fields` are applied to channel.json, the rest are
 * left unchanged. 400 for a blanked required field, 404 for an unknown channel.
 * Returns the updated listing entry. */
export function updateChannel(
  channel: string,
  fields: Partial<Omit<ChannelCreatePayload, 'slug'>>,
): Promise<ChannelInfo> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  }).then(asJson<ChannelInfo>)
}

/** POST /api/channels/{channel}/rename (see api.py's rename_channel) - renames
 * (moves) the channel directory to a new slug and returns the renamed channel's
 * listing entry (its new slug). 400/404/409; a 409 means either the target slug
 * already exists OR one of the channel's events is being rendered, `detail`
 * distinguishing them - show it as-is. */
export function renameChannel(channel: string, name: string): Promise<ChannelInfo> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}/rename`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then(asJson<ChannelInfo>)
}

/** DELETE /api/channels/{channel} (see api.py's delete_channel) - a hard rmtree
 * of the channel directory and ALL its events (no undo; the typed-slug
 * confirmation is a frontend guard). 404 unknown, 409 while one of its events
 * is being rendered - `detail` as-is. */
export function deleteChannel(channel: string): Promise<{ deleted: string }> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}`, {
    method: 'DELETE',
  }).then(asJson<{ deleted: string }>)
}

/** The shape GET /api/channels/{channel}/brand returns (see api.py's
 * get_brand): the channel's brand.json verbatim (colors/fonts/subtitles/
 * output) plus the list of font files actually on disk under fonts/ - the
 * Brand editor needs both to render its Select options and know which
 * assigned fonts, if any, are missing. */
export interface BrandResponse {
  brand: Record<string, unknown>
  fonts: string[]
}

/** The logo section of brand.json (see overlay.py / profile._validate_logo).
 * `file` is the base image ("assets/logo.png"); `variant` picks a monochrome
 * sibling; `position` is the centered badge ("top") or a corner watermark. */
export interface BrandLogo {
  file: string
  variant?: 'color' | 'white' | 'black'
  position?: 'top' | 'top-right' | 'bottom-right'
  opacity?: number
  max_height?: number
}

/** The opacity of the overlay's two band SURFACES - the veil, the channel
 * decoration and the edge accents (see overlay._fade_bands). 1 is exactly
 * how this tool has always drawn them; 0 leaves the blurred backdrop alone,
 * with the hook, footer and logo still on top at full strength. Optional at
 * every layer: an absent section and 1 are the same request. */
export interface BrandBands {
  top: number
  bottom: number
}

/** GET /api/channels/{channel}/brand/palette (see api.py's brand_palette) -
 * colours proposed from the channel's logo, plus the swatches they came
 * from so the editor can offer them as chips. A proposal, never a write:
 * nothing changes until the operator saves. 409 when the brand names no
 * logo or the file cannot be read. */
export interface PaletteResponse {
  colors: Record<string, string>
  swatches: { hex: string; share: number }[]
}

export function getPalette(channel: string): Promise<PaletteResponse> {
  return fetch(`${channelBase(channel)}/brand/palette`).then(asJson<PaletteResponse>)
}

/** The output/video-window geometry (see profile.REQUIRED_OUTPUT_KEYS). */
export interface BrandOutput {
  width: number
  height: number
  video_width: number
  video_height: number
  video_y: number
  accent_offset?: number
}

/** The body PUT and POST …/brand/preview both take (see api.py's
 * BrandPatchBody): any renderable section. Each key omitted (not merely
 * falsy) leaves that section of brand.json untouched server-side; each key
 * present is validated the same way profile.load validates it. */
export interface BrandPatch {
  colors?: Record<string, string>
  fonts?: { hook: string; small: string }
  subtitles?: { enabled: boolean }
  logo?: BrandLogo
  output?: BrandOutput
  bands?: BrandBands
  /** `mode` ("api"/"manual") is the channel's upload class, otherwise set
   * ONLY via setUploadMode/the Settings toggle - see Task 7's BrandEditor,
   * which reads it from the loaded brand and always sends it back
   * unchanged (or omits it if it was never set) rather than letting a
   * channel upload-defaults save silently flip it. The remaining fields
   * are the per-channel upload metadata defaults (description template,
   * tags, category, made-for-kids) - see profile._validate_upload. */
  upload?: {
    mode?: 'api' | 'manual'
    description?: string
    tags?: string[]
    category_id?: string
    made_for_kids?: boolean
  }
  /** Which provider scores this channel's moments, and with what model (see
   * profile._validate_detect). CHANNEL-LEVEL ONLY: the event brand route
   * refuses it outright (400) rather than silently dropping it, because the
   * choice is account-scoped - it decides whose API key and whose quota get
   * spent. `provider` must be a registered id; `model` is not checked against
   * the vendor's catalogue, so it is omitted rather than sent blank. */
  detect?: {
    provider: string
    model?: string
  }
}

/** PUT /api/channels/{channel}/upload (see api.py's put_upload_mode) - sets
 * ONLY brand.json's upload.mode, the channel's api/manual class. Lighter than
 * saveBrand: it does not require an otherwise-complete brand, so a fresh
 * channel with no fonts yet can still be flipped. 400 for an unknown mode,
 * 404 for an unknown channel. */
export function setUploadMode(channel: string, mode: 'api' | 'manual'): Promise<{ mode: string }> {
  return fetch(`${channelBase(channel)}/upload`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  }).then(asJson<{ mode: string }>)
}

/** GET /api/channels/{channel}/brand - see BrandResponse. 404 for an
 * unknown channel or an unreadable brand.json. */
export function getBrand(channel: string): Promise<BrandResponse> {
  return fetch(`${channelBase(channel)}/brand`).then(asJson<BrandResponse>)
}

/** PUT /api/channels/{channel}/brand (see api.py's put_brand) - a partial
 * merge into brand.json, validated the same way profile.load itself
 * validates a brand (brand_admin._validate): 400 (bad_color/bad_font/
 * bad_subtitles) for an incomplete or invalid patch, 404 for an unknown
 * channel. Returns the brand as saved. */
export function saveBrand(channel: string, patch: BrandPatch): Promise<{ brand: Record<string, unknown> }> {
  return fetch(`${channelBase(channel)}/brand`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }).then(asJson<{ brand: Record<string, unknown> }>)
}

/** POST /api/channels/{channel}/fonts/{filename} (see api.py's upload_font)
 * - the body is the RAW font bytes (no multipart, no JSON): the caller
 * reads the File as an ArrayBuffer and sends it as-is with an
 * application/octet-stream Content-Type. 400 (bad_name/bad_type/too_big/
 * invalid - a file PIL's ImageFont cannot load), 404 for an unknown
 * channel. Returns the fonts now on disk, sorted, same shape as
 * BrandResponse.fonts. */
export function uploadFont(channel: string, filename: string, bytes: ArrayBuffer): Promise<{ fonts: string[] }> {
  return fetch(`${channelBase(channel)}/fonts/${encodeURIComponent(filename)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: bytes,
  }).then(asJson<{ fonts: string[] }>)
}

/** DELETE /api/channels/{channel}/fonts/{filename} (see api.py's
 * delete_font) - 404 unknown font, 409 "in_use" (kept as its own status so
 * the caller can show the specific "assigned as hook/small - reassign it
 * first" reason) when the font is currently referenced by brand.json's
 * fonts.hook/small. Returns the fonts remaining on disk. */
export function deleteFont(channel: string, filename: string): Promise<{ fonts: string[] }> {
  return fetch(`${channelBase(channel)}/fonts/${encodeURIComponent(filename)}`, {
    method: 'DELETE',
  }).then(asJson<{ fonts: string[] }>)
}

/** POST /api/channels/{channel}/brand/preview (see api.py's brand_preview)
 * - renders build_overlay against the SAVED brand with `patch` applied on
 * top (nothing is written), returning a PNG as an object URL exactly like
 * fetchPreview/fetchPreviewForWords (reuses the same previewBlobUrl
 * helper). A 409 means a selected font is missing or otherwise unusable -
 * the caller should show "upload and assign a font to see the preview"
 * rather than a broken image (see ApiError.message). */
export function brandPreview(channel: string, patch: BrandPatch): Promise<string> {
  return fetch(`${channelBase(channel)}/brand/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }).then(previewBlobUrl)
}

/** The shape GET .../brand (event-scoped) returns (see api.py's
 * get_event_brand / event_brand_admin.read_event_brand): the event's OWN
 * brand.json override (only the sections it overrides - empty when the
 * event has no brand.json at all, i.e. fully inherited), the channel's full
 * brand for reference, the effective (deep-merged) brand actually used to
 * render this event, and the fonts available from each level (an event can
 * use its own uploaded fonts or fall back to the channel's - see
 * event_brand_admin.resolve_event_font_ref). Unlike BrandResponse, `fonts`
 * is split by level rather than one flat list. */
export interface EventBrandResponse {
  override: Record<string, unknown>
  channel: Record<string, unknown>
  effective: Record<string, unknown>
  fonts: { channel: string[]; event: string[] }
}

/** GET .../events/{event}/brand - see EventBrandResponse. 404 for an
 * unknown channel/event or an unreadable channel brand.json. */
export function getEventBrand(channel: string, event: string): Promise<EventBrandResponse> {
  return fetch(`${eventBase(channel, event)}/brand`).then(asJson<EventBrandResponse>)
}

/** PUT .../events/{event}/brand (see api.py's put_event_brand) - replaces
 * the event's brand.json OVERRIDE with `patch` (validated against the
 * MERGED result, event-first font resolution - see
 * event_brand_admin.update_event_brand / _validate_merged): 400
 * (bad_field/bad_color/bad_font/bad_subtitles/bad_brand) for an invalid
 * patch or for including 'upload' (never event-overridable), 404 for an
 * unknown channel/event. Returns the brand as EventBrandResponse, same
 * shape as the GET. */
export function saveEventBrand(
  channel: string,
  event: string,
  patch: BrandPatch,
): Promise<EventBrandResponse> {
  return fetch(`${eventBase(channel, event)}/brand`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }).then(asJson<EventBrandResponse>)
}

/** POST .../events/{event}/fonts/{filename} (see api.py's upload_event_font)
 * - same raw-bytes contract as uploadFont, but stores the font under the
 * EVENT's own fonts/ dir rather than the channel's. Returns the fonts now on
 * disk for this event (event-only, not the channel's - see
 * EventBrandResponse.fonts for both lists together). */
export function uploadEventFont(
  channel: string,
  event: string,
  filename: string,
  bytes: ArrayBuffer,
): Promise<{ fonts: string[] }> {
  return fetch(`${eventBase(channel, event)}/fonts/${encodeURIComponent(filename)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: bytes,
  }).then(asJson<{ fonts: string[] }>)
}

/** DELETE .../events/{event}/fonts/{filename} (see api.py's
 * delete_event_font) - same in_use-refusal contract as deleteFont, scoped
 * to the event's own fonts/. Returns the event's fonts remaining on disk. */
export function deleteEventFont(
  channel: string,
  event: string,
  filename: string,
): Promise<{ fonts: string[] }> {
  return fetch(`${eventBase(channel, event)}/fonts/${encodeURIComponent(filename)}`, {
    method: 'DELETE',
  }).then(asJson<{ fonts: string[] }>)
}

/** POST .../events/{event}/brand/preview (see api.py's event_brand_preview)
 * - renders build_overlay against the event's SAVED brand (channel merged
 * with the event's stored override) with `patch` applied on top of THAT
 * (nothing is written), same object-URL/409 contract as brandPreview. A 409
 * means a selected font (event or channel) is missing or otherwise
 * unusable. */
export function eventBrandPreview(
  channel: string,
  event: string,
  patch: BrandPatch,
): Promise<string> {
  return fetch(`${eventBase(channel, event)}/brand/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }).then(previewBlobUrl)
}

/** GET /api/channels/{channel}/events - one channel's events with counts.
 * Takes its channel explicitly (the event-list screen has a channel but no
 * event yet, so it does not go through the editor scope). */
export function getEvents(channel: string): Promise<EventInfo[]> {
  return fetch(`${channelBase(channel)}/events`).then(asJson<EventInfo[]>)
}

/** POST /api/channels/{channel}/events (see api.py's create_event) - creates
 * an EMPTY event directory and returns its listing entry. 400 for a bad name,
 * 404 for an unknown channel, 409 when the event already exists; each carries
 * a `detail` the caller should show as-is (see ApiError). Not event-scoped -
 * the event-list screen has a channel but no event yet. */
export function createEvent(channel: string, name: string): Promise<EventInfo> {
  return fetch(`${channelBase(channel)}/events`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then(asJson<EventInfo>)
}

/** PATCH /api/channels/{channel}/events/{event} (see api.py's rename_event) -
 * renames (moves) the event directory and returns the renamed event's listing
 * entry. 400/404/409; a 409 means either the target name already exists OR a
 * job (render/detect) is holding the event's lock, `detail` distinguishing
 * them - show it as-is. */
export function renameEvent(channel: string, event: string, name: string): Promise<EventInfo> {
  return fetch(`${channelBase(channel)}/events/${encodeURIComponent(event)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then(asJson<EventInfo>)
}

/** DELETE /api/channels/{channel}/events/{event} (see api.py's delete_event) -
 * a hard rmtree of the event directory (no undo; the typed-name confirmation
 * is a frontend guard). 404 unknown, 409 while a job holds the event's lock -
 * `detail` as-is. */
export function deleteEvent(channel: string, event: string): Promise<{ deleted: string }> {
  return fetch(`${channelBase(channel)}/events/${encodeURIComponent(event)}`, {
    method: 'DELETE',
  }).then(asJson<{ deleted: string }>)
}

export function listClips(): Promise<ClipSummary[]> {
  return fetch(`${eventScope()}/clips`).then(asJson<ClipSummary[]>)
}

export function getClip(name: string): Promise<ClipDetail> {
  return fetch(`${eventScope()}/clips/${encodeURIComponent(name)}`).then(asJson<ClipDetail>)
}

export function patchClip(name: string, body: PatchClipBody): Promise<ClipDetail> {
  return fetch(`${eventScope()}/clips/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(asJson<ClipDetail>)
}

async function previewBlobUrl(response: Response): Promise<string> {
  if (response.status === 409) {
    const body = await response.json().catch(() => ({}))
    throw new PreviewUnavailableError(409, body.detail ?? 'No raw clip to preview.')
  }
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText)
  }
  const blob = await response.blob()
  return URL.createObjectURL(blob)
}

/** Fetches the preview PNG of the clip's SAVED state as an object URL the
 * caller must revoke, or throws PreviewUnavailableError on a 409 (no
 * raw.mp4 yet). The right call when nothing is being edited locally. */
export function fetchPreview(name: string, at: number): Promise<string> {
  return fetch(
    `${eventScope()}/clips/${encodeURIComponent(name)}/preview?at=${encodeURIComponent(at)}`,
  ).then(previewBlobUrl)
}

/** Fetches the preview PNG rendered from words (and, optionally, a title)
 * the CLIENT is holding but has not saved - see POST
 * /api/clips/{name}/preview in api.py. Same return contract as
 * fetchPreview (object URL, or PreviewUnavailableError on 409); the
 * difference is entirely in what hook/caption get drawn. `title` omitted
 * means "draw the saved effective title", exactly like the GET route -
 * the caller only passes one when the title field itself is being
 * edited (see ClipEditor's `titleDirty`). Never writes anything - it is
 * a read, same as the GET route. */
export function fetchPreviewForWords(
  name: string,
  at: number,
  words: Word[],
  title?: string,
): Promise<string> {
  return fetch(`${eventScope()}/clips/${encodeURIComponent(name)}/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(title === undefined ? { at, words } : { at, words, title }),
  }).then(previewBlobUrl)
}

/** GET /api/streams (see api.py) - a channel's CATALOGUE: the Streams tab
 * and every playlist's members, unioned. Cached server-side for the
 * session; pass `refresh: true` to force a re-fetch of the yt-dlp half.
 *
 * The list is the union, so it holds videos the Streams tab does not -
 * measured on ERF: 8 of them, two multi-hour broadcasts that no screen in
 * this studio could reach before. */
export interface StreamVideo {
  video_id: string
  title: string
  duration_seconds: number | null
  view_count: number | null
  /** Every playlist this video sits in. A LIST: a video may be in several,
   * even though none is on ERF today. */
  playlist_ids: string[]
  /** Whether a transcribe job has already produced this stream's
   * transcript, and a detect job its moments. Read fresh by the server on
   * every response - never cached with the yt-dlp answer - so a marker
   * cannot outlive the fact it reports. */
  has_transcript: boolean
  has_analysis: boolean
}

export interface StreamPlaylist {
  id: string
  title: string
  /** Usable videos in it. */
  count: number
  /** Entries dropped for being deleted or private. Carried so a "(6)" is
   * never silently a 6 that came from 8. */
  unavailable: number
}

/** A playlist whose fetch failed. Rendered, never swallowed: a half
 * catalogue that looks whole is this project's recurring failure mode. */
export interface FailedPlaylist {
  title: string
  reason: string
}

export interface StreamCatalogue {
  videos: StreamVideo[]
  playlists: StreamPlaylist[]
  failed_playlists: FailedPlaylist[]
}

export function listStreams(refresh = false): Promise<StreamCatalogue> {
  return fetch(`${eventScope()}/streams${refresh ? '?refresh=true' : ''}`)
    .then(asJson<StreamCatalogue>)
}

/** GET .../streams for an EXPLICIT channel/event, unlike listStreams above.
 * The stream screen mounts as a sibling of App (see getStreamAnalysis's own
 * comment on why channel/event are parameters there) - the module scope
 * listStreams relies on is only ever set by App, so a scoped call here would
 * throw on mount instead of fetching. Used only to look up a stream's real
 * title for the heading (see StreamScreen): the analysis's cached
 * `stream_title` is often empty ("" - a cold cache, see api.py's post_detect),
 * and this is the same catalogue the event's Streams tab already shows - which
 * since the playlist change also holds streams the Streams tab itself omits,
 * so a title is found for those too. Not cached client-side; the server's own
 * per-channel cache absorbs repeat calls within a session. Can be slow on a
 * cold cache (several yt-dlp calls) - callers must not await this before
 * rendering. */
export function getStreams(channel: string, event: string): Promise<StreamCatalogue> {
  return fetch(`${eventBase(channel, event)}/streams`).then(asJson<StreamCatalogue>)
}

/** The shape GET …/streams/{video_id}/moments returns (see api.py's
 * get_stream_moments) - a stream's detection RESULT, not the transcript
 * itself. Answers 200 with this EMPTY shape when detection has never run
 * (video_id/duration_seconds carried, everything else empty/null) - only a
 * corrupt moments.json is a 500. That asymmetry with getStreamTranscript's
 * 404 is deliberate: a screen built on this route must treat "never
 * analysed" as the ordinary starting state, not an error. */
export interface StreamAnalysis {
  video_id: string
  stream_title: string
  engine: string | null
  /** Which VENDOR the run was configured for, beside `engine` and never
   * folded into it (see detect.py, which writes both): `engine` stays the
   * authority on what actually produced these moments ("model:<model>",
   * "lexicon" or "none"), while this names whose key was consulted - the
   * useful half of the diagnosis when a run fell back to the lexicon. Null
   * for a stream never analysed, and for an analysis written before the
   * field existed (api.py defaults it on both paths). */
  configured_provider: string | null
  created_at: string | null
  duration_seconds: number
  activity: number[]
  moments: Moment[]
  missing_windows: number[]
  missing_chunks: number[]
}

/** The shape GET …/streams/{video_id}/transcript returns (see api.py's
 * get_stream_transcript). Unlike getStreamAnalysis, this 404s when there is
 * no transcript yet - the one real error the stream screen can show. */
export interface StreamTranscript {
  video_id: string
  duration_seconds: number
  words: Word[]
  missing_chunks: number[]
}

// channel/event are parameters, not eventScope(): the stream screen mounts as a
// sibling of App, which is the only place that sets the module scope, so a
// scoped call would throw on mount instead of fetching.
export function getStreamAnalysis(
  channel: string, event: string, videoId: string,
): Promise<StreamAnalysis> {
  return fetch(
    `${eventBase(channel, event)}/streams/${encodeURIComponent(videoId)}/moments`,
  ).then(asJson<StreamAnalysis>)
}

export function getStreamTranscript(
  channel: string, event: string, videoId: string,
): Promise<StreamTranscript> {
  return fetch(
    `${eventBase(channel, event)}/streams/${encodeURIComponent(videoId)}/transcript`,
  ).then(asJson<StreamTranscript>)
}

// There is deliberately NO `startDetect` here any more, and none for a render
// or a trim either. The three routes still exist server-side
// (`POST …/streams/{video_id}/detect`, `POST …/render`, `POST …/clips/{name}/
// trim` - see api.py, which says at each of them why they stay), but this
// browser enqueues that work instead: `enqueueJob('detect' | 'render' |
// 'trim', …)` at the bottom of this file. Routing a click straight at a start
// route makes work that cannot be scheduled, paused, reordered or stopped, and
// that never appears on the Jobs screen at all - which was true of exactly
// these three until this change.
//
// They are DELETED rather than left unused on purpose: an exported wrapper is
// an invitation, and a future call site that picked one up would compile
// silently and quietly reintroduce the unschedulable path. Same reasoning as
// `shortUrl`'s required `version`/`purpose` parameters below - make the
// mistake a build failure rather than something a test has to notice.
//
// `upload` is the one that stays direct (`startUpload`), and not by omission:
// it cannot be stopped at any level, and it carries a per-upload confirmation
// that a queue entry - written now, run later from a state file - cannot
// carry. That is the same reason `_validate_enqueue` refuses a non-private
// queued upload outright.

/** POST …/streams/{video_id}/clips - creates a clip from a window the
 * operator picked on the stream screen (see api.py's post_clip_from_window
 * and CLAUDE.md's amended write boundary). channel/event are explicit
 * parameters, not eventScope(), for the same reason getStreamAnalysis's are:
 * this screen mounts as a sibling of App. The server's message is the useful
 * half of a failure here - it names both windows on a collision (409) and
 * the directory on an unreadable neighbour (409); a bad window is a 400. */
export function createClipFromWindow(
  channel: string, event: string, videoId: string,
  body: { start: number; end: number; hook: string; source_title: string },
): Promise<{ name: string }> {
  return fetch(
    `${eventBase(channel, event)}/streams/${encodeURIComponent(videoId)}/clips`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  ).then(asJson<{ name: string }>)
}

/** The shape POST …/streams/{video_id}/estimate returns (see api.py's
 * post_estimate and yt_shorts.estimate's own docstring). Local and
 * approximate - characters divided by four, not a real token count - which
 * is why `estimated` is always true: no caller may present this as what a
 * run will actually be billed. */
export interface RunEstimate {
  model: string
  windows: number
  input_tokens: number
  output_tokens: number
  usd: number
  estimated: boolean
  /** False when `model` has no entry in estimate.py's PRICES table - `usd`
   * is then 0.0 for "unpriced", not "free", and a caller must not show it as
   * a real number. See estimate.py's own docstring. */
  priced: boolean
}

/** POST …/streams/{video_id}/estimate - what a detection run over this
 * stream would cost, worked out locally with no key and no network call
 * (see yt_shorts.estimate). channel/event are explicit parameters, not
 * eventScope(), for the same reason getStreamAnalysis's are: this screen
 * mounts as a sibling of App. `model` is an explicit override, or null to
 * use the channel's configured/default model. 404 when there is no
 * transcript yet. */
export function estimateRun(
  channel: string, event: string, videoId: string, model: string | null,
): Promise<RunEstimate> {
  return fetch(
    `${eventBase(channel, event)}/streams/${encodeURIComponent(videoId)}/estimate`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    },
  ).then(asJson<RunEstimate>)
}

/**
 * The rendered short's URL, carrying the file's version so that a changed
 * file means a changed URL - which is what a mounted <video> needs, since it
 * only reloads when its src actually changes, and what stops the browser
 * answering from a cache keyed on a URL that never moves.
 *
 * `version` is REQUIRED rather than optional deliberately. Both callers must
 * pass it - the player in ClipEditor and the DOWNLOAD LINK in
 * ManualUploadPanel - and a stale short in the download link means an
 * operator hand-uploading the wrong video to YouTube, which is worse than a
 * stale preview. Making the parameter required turns "someone forgot at one
 * call site" into a tsc failure instead of something a test has to notice.
 * Pass null for the deliberately unversioned form.
 *
 * `purpose` is required for the SAME reason, and for the same reason it is
 * NOT defaulted: the server refuses the download form (`as=download`) while
 * a trim is pending (see api.py's get_short), but the plain form the player
 * uses must keep working then - previewing the cut against the untrimmed
 * short is exactly how an operator judges the head/tail values before
 * applying them (see ClipEditor.tsx's player block). A default of 'play'
 * would let a future download-shaped call site compile silently and hand an
 * operator the untrimmed file with no guard at all; requiring the caller to
 * say which one it wants turns that mistake into a tsc failure instead.
 */
export function shortUrl(
  name: string, version: string | null, purpose: 'play' | 'download',
): string {
  const base = `${eventScope()}/clips/${encodeURIComponent(name)}/short`
  const params = new URLSearchParams()
  if (version !== null) params.set('v', version)
  if (purpose === 'download') params.set('as', 'download')
  const query = params.toString()
  return query ? `${base}?${query}` : base
}

// `applyTrim` and `startRender` used to sit here. See the note where
// `startDetect` was, above `createClipFromWindow`: all three actions go
// through `enqueueJob` now, and the routes they called still exist for
// anything that is not this browser.

export function getJob(jobId: string): Promise<Job> {
  return fetch(`/api/jobs/${encodeURIComponent(jobId)}`).then(asJson<Job>)
}

/** GET /api/auth (see api.py's get_auth) - whether THIS channel has a
 * stored, valid token, and today's local quota estimate. Never carries a
 * token, a client secret or a password: the API itself only ever returns
 * a boolean, the YouTube channel id and a remaining-count (see api.py's
 * module docstring and CLAUDE.md's stage E boundaries) - there is nothing
 * secret in this shape for the UI to accidentally render. */
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

export function getAuth(): Promise<AuthStatus> {
  return fetch(`${channelScope()}/auth`).then(asJson<AuthStatus>)
}

/** POST /api/auth/connect (see api.py's post_connect) - starts a background
 * job that runs the OAuth browser consent flow for `channelId` and stores
 * the resulting token. `channelId` null/empty means this studio's own
 * channel. `force` re-runs consent even when a valid token is already
 * stored - the way to switch which Google account a channel is connected
 * as (see ConnectBody in api.py); the first-time connect flow leaves it
 * false. Poll the returned job id with useJobPolling, exactly like a
 * render/detect/upload job - same Job shape, same GET /api/jobs/{id}
 * route. Throws ApiError(503, <install message>) when the Google
 * libraries are not installed - the caller should show that message
 * as-is, not a generic error. Never touches a password or token itself:
 * consent happens in the operator's own browser, started by the server. */
export function startConnect(
  channelId: string | null,
  force = false,
): Promise<{ job_id: string }> {
  return fetch(`${channelScope()}/auth/connect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel_id: channelId || null, force }),
  }).then(asJson<{ job_id: string }>)
}

/** The shape returned by GET /api/settings (see api.py's get_settings) - the
 * workspace-level Settings screen's one read: the resolved workspace's
 * location/origin (see yt_shorts.workspace.resolve) plus every channel's
 * connection state in one page, so the operator never has to open each
 * channel's editor just to check whether it is connected. Same
 * never-a-secret contract as AuthStatus - a boolean, the public channel id,
 * an int/null quota estimate, nothing token-shaped. A channel whose
 * channel.json is missing/malformed carries a non-null `error` and no
 * connection state (mirrors ChannelInfo's own error field). */
export interface SettingsResponse {
  workspace: {
    root: string
    origin: string
    channel_count: number
    google_upload_available: boolean
  }
  channels: Array<{
    channel: string
    channel_id: string
    upload_mode: 'api' | 'manual'
    connected: boolean
    remaining_uploads: number | null
    /** Which provider scores this channel's moments, and with what model -
     * read-only here (the channel Brand editor is where it is set). The
     * provider is always a real id, defaulting to `providers.DEFAULT_PROVIDER`
     * for a brand.json with no `detect` section; the model is "" when that
     * channel does not override the provider's own default. */
    detect_provider: string
    detect_model: string
    error: string | null
  }>
  /** Every registered model provider and its state - booleans, shipped
   * constants and a price table, never a key (see providers.ts). */
  providers: ProviderState[]
  /** The job queue's pool limits, workspace-level like everything else on
   * this screen (see api.py's get_settings, `"queue"`). `limits`/`pools`
   * come from the LIVE queue when one is running and from settings.json
   * otherwise, so a studio whose workspace has not resolved yet can still
   * show - and set - the number it will use once one does; `available`
   * is what tells the two states apart. `pools` is derived from
   * jobs.KINDS server-side, never hardcoded here, so a future pool needs
   * no client change to be offered a field. */
  queue: {
    available: boolean
    limits: Record<string, number>
    pools: string[]
    worker_running: boolean
  }
}

/** GET /api/settings - see SettingsResponse. Not scoped: the Settings screen
 * has no active channel/event. */
export function getSettings(): Promise<SettingsResponse> {
  return fetch('/api/settings').then(asJson<SettingsResponse>)
}

/** PUT /api/settings/limits (see api.py's put_queue_limits) - how many jobs
 * each pool runs at once, for THIS workspace. A PARTIAL patch: a pool this
 * call does not name keeps its current limit, so `putQueueLimits({cpu: 2})`
 * cannot accidentally reset `net`. Written to `<workspace>/settings.json`
 * AND applied to the LIVE queue immediately - no restart needed, see
 * `test_the_limits_can_be_changed_on_a_live_queue` in test_job_queue.py.
 *
 * The server is the sole authority on what counts as usable (a whole
 * number, at least 1, a pool this workspace actually has -
 * `_validated_limits`): a 400 here carries the server's own reason and
 * MUST be shown, never silently retried or swallowed - the caller has no
 * business re-deciding what the server just refused. */
export function putQueueLimits(
  limits: Record<string, number>,
): Promise<{ limits: Record<string, number> }> {
  return fetch('/api/settings/limits', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ limits }),
  }).then(asJson<{ limits: Record<string, number> }>)
}

/** PUT /api/providers/{id}/key (see api.py's put_provider_key) - stores one
 * provider's API key at <workspace>/auth/<provider>.json, 0600 and
 * atomically. The key travels UP only: nothing this route (or any other)
 * returns ever carries it back, which is why the Settings field is never
 * pre-filled from the server. 400 whenever the key is unusable (empty, too
 * long, control characters, or a body that is not an object) with a `detail`
 * naming the constraint and quoting nothing; 404 for an unknown provider. */
export function putProviderKey(id: string, apiKey: string): Promise<{
  provider: string
  key_present: boolean
}> {
  return fetch(`/api/providers/${encodeURIComponent(id)}/key`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: apiKey }),
  }).then(asJson<{ provider: string; key_present: boolean }>)
}

/** DELETE /api/providers/{id}/key (see api.py's delete_provider_key) - forgets
 * that one file and nothing else in auth/ (not the client secret, not another
 * provider's key, not a YouTube token), reversible by pasting the key again.
 * 404 when no key was stored, and for an unknown provider. */
export function deleteProviderKey(id: string): Promise<{
  provider: string
  key_present: boolean
}> {
  return fetch(`/api/providers/${encodeURIComponent(id)}/key`, {
    method: 'DELETE',
  }).then(asJson<{ provider: string; key_present: boolean }>)
}

/** DELETE /api/channels/{channel}/auth (see api.py's disconnect_auth) - the
 * studio's "disconnect": forgets ONLY the locally stored OAuth token for
 * this channel (auth.forget_credentials), never the client secret or quota
 * file, reversible by connecting again. 404 ("was not connected") when
 * there was no token, 400 for a bad `{channel}` segment - `detail` as-is. */
export function disconnectAuth(channel: string): Promise<{ disconnected: string }> {
  return fetch(`${channelBase(channel)}/auth`, { method: 'DELETE' }).then(
    asJson<{ disconnected: string }>,
  )
}

/** Scope-explicit connect for the Settings screen - the SAME route startConnect
 * uses (POST /api/channels/{channel}/auth/connect), but taking `channel` as an
 * explicit argument rather than reading the module-level editor scope (see
 * `scope`/`channelScope()` above), because the Settings screen has no active
 * channel/event: it lists every channel in the workspace at once. Same body/
 * return/503-when-google-libs-missing contract as startConnect - see that
 * function's own docstring. */
export function connectChannel(
  channel: string,
  channelId: string | null,
  force = false,
): Promise<{ job_id: string }> {
  return fetch(`${channelBase(channel)}/auth/connect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel_id: channelId || undefined, force }),
  }).then(asJson<{ job_id: string }>)
}

/** GET /api/workspaces (see api.py's get_workspaces) - the currently
 * resolved workspace plus this studio's recently-used list, for the
 * workspace switcher. `current.locked` mirrors the server's own guard
 * (`_guard_reroot`): true when YT_SHORTS_DATA pins the workspace, in which
 * case switch/create/copy all 409 and the UI should disable them rather
 * than let the operator hit that error. A recent entry's `valid` is false
 * when that path is no longer a workspace (moved/deleted) - the caller
 * should show it but refuse to switch to it. */
export interface WorkspacesResponse {
  current: { path: string; name: string; origin: string; locked: boolean }
  recent: { path: string; name: string; valid: boolean }[]
}

/** GET /api/fs (see api.py's browse_fs) - lists the subdirectories of
 * `path` (or the operator's home directory when omitted), for the
 * workspace picker's directory browser. `parent` is null only at the
 * filesystem root. Each entry's `is_workspace` flags a directory that is
 * already a workspace, so the picker can distinguish "browse into this"
 * from "select this". */
export interface FsListing {
  path: string
  parent: string | null
  entries: { name: string; path: string; is_workspace: boolean }[]
}

/** GET /api/workspaces - see WorkspacesResponse. Not scoped: the workspace
 * switcher has no active channel/event. */
export function getWorkspaces(): Promise<WorkspacesResponse> {
  return fetch('/api/workspaces').then(asJson<WorkspacesResponse>)
}

/** POST /api/workspaces/switch (see api.py's switch_workspace) - re-roots
 * the whole app onto an existing workspace at `path`. 400 when `path` is
 * not a workspace, 409 when YT_SHORTS_DATA pins the workspace or a job is
 * currently running (see `_guard_reroot`) - `detail` as-is. Returns the
 * workspace list as it looks after switching. */
export function switchWorkspace(path: string): Promise<WorkspacesResponse> {
  return fetch('/api/workspaces/switch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(asJson<WorkspacesResponse>)
}

/** POST /api/workspaces/create (see api.py's create_workspace_route) -
 * creates a new, empty workspace directory `name` under `parent` and
 * switches onto it. 400 for a bad name, 404 for a missing `parent`, 409 for
 * a name collision or the same reroot guards as switchWorkspace - `detail`
 * as-is. Returns the workspace list as it looks after switching. */
export function createWorkspace(parent: string, name: string): Promise<WorkspacesResponse> {
  return fetch('/api/workspaces/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parent, name }),
  }).then(asJson<WorkspacesResponse>)
}

/** POST /api/workspaces/copy (see api.py's copy_workspace_route) - starts a
 * background job that clones the CURRENT workspace to `name` under
 * `parent`, then switches onto the copy once it finishes. Poll the
 * returned job id with useJobPolling, same Job shape, same GET
 * /api/jobs/{id} route as a render/detect/upload job. Same reroot guards
 * as switchWorkspace (409) apply before the job even starts. */
export function copyWorkspace(parent: string, name: string): Promise<{ job_id: string }> {
  return fetch('/api/workspaces/copy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parent, name }),
  }).then(asJson<{ job_id: string }>)
}

/** GET /api/fs - see FsListing. `path` omitted browses the operator's home
 * directory (see api.py's browse_fs). Not scoped: the picker has no active
 * channel/event. */
export function browseFs(path?: string): Promise<FsListing> {
  const q = path ? `?path=${encodeURIComponent(path)}` : ''
  return fetch(`/api/fs${q}`).then(asJson<FsListing>)
}

/** The body POST …/clips/{name}/upload takes (see api.py's
 * UploadRequestBody) - every field optional/defaulted, mirroring the
 * server's own safe defaults so an accidental bare call still uploads
 * private and unscheduled. `visibility` other than "private", or any
 * `publish_at`, is an irreversible-ish, more-exposed choice the server
 * refuses (400) unless `confirm` is also true - see needsConfirm in
 * uploadMeta.ts, which is what the caller should use to decide whether to
 * set it. `publish_at` must already be RFC3339 UTC (see uploadMeta.ts's
 * toRfc3339) - this function does not convert it. */
export interface UploadOptions {
  visibility?: string
  publish_at?: string | null
  confirm?: boolean
  force?: boolean
}

/** POST /api/clips/{name}/upload (see api.py's post_upload) - starts a
 * background upload job for a KEPT, RENDERED clip; 409s (with a `detail`
 * the caller should show as-is) when the clip is not kept, has no
 * short.mp4, or was already uploaded and `force` was not passed; 400 when
 * a non-private/scheduled upload is sent without `confirm`. Poll the
 * returned job id with useJobPolling, exactly like a render or detect job
 * - same Job shape, same GET /api/jobs/{id} route.
 *
 * `force`/`visibility`/`publish_at`/`confirm` travel in the JSON BODY, not
 * a query string (see api.py's UploadRequestBody docstring: re-uploading
 * or changing visibility is as deliberate a per-upload choice as any other
 * field here, so all four live together). */
export function startUpload(
  name: string,
  opts: UploadOptions = {},
): Promise<{ job_id: string }> {
  return fetch(`${eventScope()}/clips/${encodeURIComponent(name)}/upload`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  }).then(asJson<{ job_id: string }>)
}

/** The shape returned by GET /api/clips/{name}/upload-preview (see
 * api.py's get_upload_preview) - the EXACT metadata `videos.insert` would
 * send for this clip right now, computed server-side by
 * `youtube_upload.build_metadata`: title is the effective hook, and
 * description/tags/category_id/made_for_kids come from the channel's own
 * `upload` config block. Visibility and scheduling are NOT part of this
 * shape - they are a per-upload operator choice made at upload time (see
 * UploadOptions/startUpload below), not something the preview predicts.
 * This is what lets the upload confirmation modal show the real payload
 * instead of only stating where it comes from. A build failure (e.g. a
 * title over YouTube's 100-character limit, or a bad description
 * template) is a 409 here, not a 500 - see api.py's get_upload_preview. */
export interface UploadPreview {
  title: string
  description: string
  tags: string[]
  category_id: string
  made_for_kids: boolean
}

/** GET /api/clips/{name}/upload-preview - a plain read, never a write (see
 * UploadPreview's own docstring). 404s for an unknown clip, same as
 * getClip; 409s when the effective metadata fails one of YouTube's own
 * limits. The upload confirmation modal fetches this when it opens; a
 * failure here must never block confirming the upload itself, since the
 * title is already known client-side without it - see UploadPanel.tsx. */
export function getUploadPreview(name: string): Promise<UploadPreview> {
  return fetch(`${eventScope()}/clips/${encodeURIComponent(name)}/upload-preview`).then(
    asJson<UploadPreview>,
  )
}

/** Pulls the uploaded video's URL out of a FINISHED upload job's log line
 * (see studio/jobs.py's start_upload_job: on success it records
 * `f"uploaded: {name} -> {record.get('url')}"`). The job's `results` map
 * only ever carries {status, reason} - never the URL - so the log is the
 * only place it appears; this is also why "uploaded" state can only ever
 * be known for a clip THIS session actually watched a job finish for (see
 * UploadPanel.tsx's own docstring on that limit). Returns null if the job
 * never logged a matching line, e.g. it failed before getting that far. */
export function extractUploadUrl(job: Job, name: string): string | null {
  const prefix = `uploaded: ${name} -> `
  const line = job.log.find((entry) => entry.startsWith(prefix))
  return line ? line.slice(prefix.length) : null
}

/**
 * Returns `url` unchanged only if it is an http(s) URL, else null. The uploaded
 * URL is sliced out of a job-log line or read from upload.json; in the normal
 * case it is YouTube's own https URL, but a fabricated/tampered value could
 * carry a `javascript:`/`data:` scheme, which must never reach an href. A null
 * return means "show it as plain text, not a link".
 */
export function safeExternalUrl(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? url : null
  } catch {
    return null
  }
}

/** One log file's stat, as GET /api/logs and its listing entries describe it
 * (see api.py's _describe_log). `modified` is a Unix timestamp in SECONDS
 * (Python's st_mtime), not milliseconds - a caller building a JS Date must
 * multiply by 1000. */
export interface LogFile {
  name: string
  size: number
  modified: number
}

/** GET /api/logs (see api.py's list_log_files) - the central app log, the
 * dated archives it has been rotated into (YYYY-MM-DD, newest first), and
 * every background job's own log file. Not scoped: the Logs screen is
 * workspace-level, like Settings. */
export interface LogListing {
  central: LogFile
  archives: string[]
  jobs: LogFile[]
}

/** The body GET /api/logs/{name} and GET /api/jobs/{id}/log both return (see
 * api.py's _log_body). `date` is null for a live plain-file read; set to the
 * requested YYYY-MM-DD when reading a gzipped archive, in which case the
 * whole archive is returned in one call and `position` merely echoes back
 * whatever `after` was passed (a gzip archive is not incrementally tailable
 * the way the live file is). */
export interface LogContent {
  lines: string[]
  position: number
  name: string
  date: string | null
}

/** GET /api/logs - see LogListing. Not scoped: the Logs screen has no active
 * channel/event. */
export function listLogs(): Promise<LogListing> {
  return fetch('/api/logs').then(asJson<LogListing>)
}

/** GET /api/logs/{name} (see api.py's read_log) - `after` tails a live plain
 * file from that byte position (omit/0 to read from the start); `date`
 * instead serves the gzipped archive rotated on that day, returning its
 * whole content. 400 for a bad `name` segment, 404 for a name/date that
 * resolves to no file (see _resolve_log's containment guard - nothing
 * outside <workspace>/logs/ is ever reachable through this route). */
export function readLog(name: string, opts: { after?: number; date?: string } = {}): Promise<LogContent> {
  const params = new URLSearchParams()
  if (opts.after !== undefined) params.set('after', String(opts.after))
  if (opts.date !== undefined) params.set('date', opts.date)
  const query = params.toString()
  return fetch(`/api/logs/${encodeURIComponent(name)}${query ? `?${query}` : ''}`).then(
    asJson<LogContent>,
  )
}

/** One entry of a moments lexicon's `effective` map (see api.py's
 * GET/PUT …/moments routes and lexicon_admin.read's own docstring): the
 * weight actually in effect at this scope, and which layer set it. A weight
 * of 0 here means "disabled at this layer" - lexicon_admin.read deliberately
 * keeps a 0 rather than dropping it the way the scoring merge does, so the
 * editor can show it struck through instead of making it silently vanish. */
export interface MarkerSource {
  weight: number
  source: 'default' | 'workspace' | 'channel' | 'event'
}

/** The shape every moments-lexicon route returns (GET/PUT at all three
 * scopes, and POST /api/moments/adopt-default) - see lexicon_admin.read.
 * `own` is exactly this scope's own moments.json (what a PUT would send
 * back unchanged); `effective` is every marker visible at this scope, least
 * specific layer overwritten by a more specific one, weight-0 entries
 * included (see MarkerSource). `problems` lists any layer (by path and
 * error) that failed to load and was therefore treated as empty rather than
 * raised - a malformed moments.json at one layer must not 500 the route; the
 * editor surfaces these as a warning instead (see MomentsEditor.tsx). */
export interface MomentsLexicon {
  scope: 'workspace' | 'channel' | 'event'
  own: Record<string, number>
  effective: Record<string, MarkerSource>
  problems: string[]
}

/** Builds the moments-lexicon base URL for a scope: `/api/moments` when
 * neither `channel` nor `event` is given, `/api/channels/{channel}/moments`
 * for a channel scope, `/api/channels/{channel}/events/{event}/moments` for
 * an event scope - mirroring the three route pairs in api.py exactly (see
 * that file's route list). Every segment is encoded via channelBase/
 * eventBase, same as the rest of this module. An `event` given without a
 * `channel` is a caller bug (there is no such backend route); it falls back
 * to the workspace base rather than building a malformed URL. */
function momentsBase(scope: { channel?: string; event?: string }): string {
  if (scope.channel && scope.event) return `${eventBase(scope.channel, scope.event)}/moments`
  if (scope.channel) return `${channelBase(scope.channel)}/moments`
  return '/api/moments'
}

/** GET the moments lexicon for `scope` - see MomentsLexicon. `{}` reads the
 * workspace layer, `{channel}` the channel layer (workspace inherited),
 * `{channel, event}` the event layer (channel inherited). */
export function getMoments(scope: { channel?: string; event?: string }): Promise<MomentsLexicon> {
  return fetch(momentsBase(scope)).then(asJson<MomentsLexicon>)
}

/** PUT the moments lexicon for `scope` - OVERWRITES that scope's own layer
 * with exactly `markers` (see lexicon_admin.update's own docstring: this is
 * never a merge, so a caller must always send the FULL own layer it wants,
 * e.g. via rowsToMarkers in momentsLexicon.ts). Returns the freshly-read
 * state, same shape as getMoments. */
export function putMoments(
  scope: { channel?: string; event?: string },
  markers: Record<string, number>,
): Promise<MomentsLexicon> {
  return fetch(momentsBase(scope), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ markers }),
  }).then(asJson<MomentsLexicon>)
}

/** POST /api/moments/adopt-default (see api.py's post_adopt_default_moments)
 * - copies the built-in default lexicon into the WORKSPACE layer's own
 * entries, so an operator can start editing it instead of only adding on
 * top. Idempotent (see lexicon_admin.adopt_default). Returns the workspace
 * lexicon as read afterward, same shape as getMoments({}). */
export function adoptDefaultMoments(): Promise<MomentsLexicon> {
  return fetch('/api/moments/adopt-default', { method: 'POST' }).then(asJson<MomentsLexicon>)
}

/** GET /api/jobs/{job_id}/log (see api.py's read_job_log) - the same tailing
 * contract as readLog's plain-file case (`after` a byte position), but for
 * one job's own log file rather than a name under logs/jobs/ directly - it
 * looks the job up in the job store instead of trusting a filename. 404 when
 * the job has no log (unknown id, or the job never started writing one). */
export function readJobLog(jobId: string, opts: { after?: number } = {}): Promise<LogContent> {
  const query = opts.after !== undefined ? `?after=${encodeURIComponent(opts.after)}` : ''
  return fetch(`/api/jobs/${encodeURIComponent(jobId)}/log${query}`).then(asJson<LogContent>)
}

// ---- The job queue (see api.py's own "The job queue" section) ----------
//
// EIGHT calls over the plan `yt_shorts.job_queue` owns. The shapes live in
// jobs.ts, beside the pure rules that read them, and are re-exported here
// for the same reason `Moment` is (see the top of this file): one source of
// truth for the interface, importable from either module.
//
// Two things about this surface that are easy to get wrong, both recorded
// at length in `list_queue`'s docstring and in jobs.ts:
//
// - `/api/jobs/{id}` addresses TWO disjoint id spaces. `getJob`/`readJobLog`
//   above take a `studio.jobs.Job` id; everything below takes a queue
//   `Entry` id. They are minted by different code and never equal - passing
//   one where the other belongs is a 404, not a subtle bug.
// - a listed row's `params` may carry the literal "[redacted]" (see api.py's
//   `_safe_params`). `enqueueJob` therefore builds its params from the
//   operator's own context, never from a row read back out of the listing:
//   a re-enqueue built that way would write that string into the plan as if
//   it were the real parameter. There is deliberately no "re-queue this
//   entry" call here.

export type { JobEntry, JobPlan } from './jobs'

/** GET /api/jobs - the whole plan in three sections, plus the pool limits,
 * whether the worker is running and any load error. 503 when this studio
 * could not open a workspace to read jobs.json from (the message names the
 * cause; do not render it as "no jobs"). */
export function listJobs(): Promise<JobPlan> {
  return fetch('/api/jobs').then(asJson<JobPlan>)
}

/** POST /api/jobs - adds one unit of work to the plan. `params` must carry
 * `channel` and `event` for every kind, plus whatever that kind needs (see
 * studio/worker.py's table: `video_id` for transcribe/detect, `clip` for
 * trim/upload). Answers the new entry with its position and how many
 * entries are actually waiting in front of it. */
export function enqueueJob(
  kind: string,
  params: Record<string, unknown>,
  after: string | null = null,
): Promise<{ entry: JobEntry; position: number; queued_ahead: number }> {
  return fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, params, after }),
  }).then(asJson<{ entry: JobEntry; position: number; queued_ahead: number }>)
}

/** POST /api/jobs/{id}/stop - asks the job running this entry to stop. 202,
 * not 200: the stop is ACCEPTED and the work ends at its own safe point, so
 * the entry comes back `stopping`, never `stopped`. `force` is the hard stop
 * (terminate the subprocess the work waits on); 409 `no_hard_stop` where the
 * kind allows none, and 409 `not_stoppable` where the job carries no cancel
 * token at all - never a quiet downgrade to a graceful stop. */
export function stopJob(entryId: string, force = false): Promise<{ entry: JobEntry }> {
  const query = force ? '?force=true' : ''
  return fetch(`/api/jobs/${encodeURIComponent(entryId)}/stop${query}`, {
    method: 'POST',
  }).then(asJson<{ entry: JobEntry }>)
}

/** POST /api/jobs/{id}/pause - holds a queued entry without losing its
 * place in line. 409 on anything that is not `queued`. */
export function pauseJob(entryId: string): Promise<{ entry: JobEntry }> {
  return fetch(`/api/jobs/${encodeURIComponent(entryId)}/pause`, {
    method: 'POST',
  }).then(asJson<{ entry: JobEntry }>)
}

/** POST /api/jobs/{id}/resume - puts a paused entry back in line, where it
 * was. 409 on anything that is not `paused`. */
export function resumeJob(entryId: string): Promise<{ entry: JobEntry }> {
  return fetch(`/api/jobs/${encodeURIComponent(entryId)}/resume`, {
    method: 'POST',
  }).then(asJson<{ entry: JobEntry }>)
}

/** POST /api/jobs/{id}/move - reorders a QUEUED entry. `index` is into the
 * WHOLE plan, the same index space each row's `position` reports - not a
 * queued-only one. 409 on anything that is not `queued`. */
export function moveJob(entryId: string, index: number): Promise<{ entry: JobEntry }> {
  return fetch(`/api/jobs/${encodeURIComponent(entryId)}/move`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index }),
  }).then(asJson<{ entry: JobEntry }>)
}

/** POST /api/jobs/{id}/retry - re-queues a failed, interrupted or STOPPED
 * entry. It does not resume from a saved position; a transcribe restarts at
 * the first missing chunk and a detect at the first unscored window, because
 * each caches its own unit of work - which is exactly why a stopped entry is
 * retryable, and why `stopWarning` may promise that resumption. 409 on
 * `done` and on any state that is not terminal at all. */
export function retryJob(entryId: string): Promise<{ entry: JobEntry }> {
  return fetch(`/api/jobs/${encodeURIComponent(entryId)}/retry`, {
    method: 'POST',
  }).then(asJson<{ entry: JobEntry }>)
}

/** DELETE /api/jobs/{id} - drops an entry from the plan. 409 on a running
 * or stopping one, with a message pointing at stop: dropping a plan and
 * halting work in progress are two different operations. */
export function removeJob(entryId: string): Promise<{ removed: string }> {
  return fetch(`/api/jobs/${encodeURIComponent(entryId)}`, {
    method: 'DELETE',
  }).then(asJson<{ removed: string }>)
}

/** The five layers a glossary entry can come from (see
 * profile._load_glossary): the built-in default in code (now EMPTY - see
 * glossary.DEFAULT_LAYER), the event's selected track pack (see tracks.py,
 * e.g. the Nürburgring Nordschleife's corner names), the workspace file, the
 * channel's, the event's own - least to most specific. */
export type LayerSource = 'default' | 'track' | 'workspace' | 'channel' | 'event'

/** One venue in the shipped registry (see tracks.py), as GET /api/tracks
 * returns it. Id and name only - the selector needs a label, and shipping
 * every pack's vocabulary to fill a dropdown would be waste. */
export interface TrackRow {
  id: string
  name: string
}

/** GET /api/tracks - every venue, sorted by name. A read of shipped code, so
 * it takes no scope and never 404s. */
export function getTracks(): Promise<TrackRow[]> {
  return fetch('/api/tracks').then(asJson<{ tracks: TrackRow[] }>).then((body) => body.tracks)
}

/** One entry of a glossary's effective `terms` map: the spelling to bias the
 * decoder with, whether it is enabled, and which layer set it. `enabled:
 * false` means "disabled at that layer" - glossary_admin.read deliberately
 * KEEPS a disabled entry rather than dropping it the way the transcription
 * merge does, so the editor can show it struck through. */
export interface EffectiveTerm {
  term: string
  enabled: boolean
  source: LayerSource
}

/** One entry of a glossary's effective `replacements` map: the key as its
 * author wrote it, the replacement text, and the layer that set it. A `value`
 * of null means disabled at that layer (see EffectiveTerm). */
export interface EffectiveReplacement {
  key: string
  value: string | null
  source: LayerSource
}

/** The shape every glossary route returns (GET/PUT at all three scopes -
 * there is no glossary adopt-default route; that POST existed only for the
 * moments/lexicon default, see adoptDefaultMoments below) - see
 * glossary_admin.read. `own` is exactly this scope's own glossary.json in
 * the shape a PUT sends back;
 * `own_keys` is the SERVER's own already-normalised keys for that same own
 * layer (`glossary.normalise_term`/`normalise_key`'s output, not re-derived
 * client-side) - this is what glossaryLayers.ts's `toTermRows`/
 * `toReplacementRows` use to decide which `effective` row is this scope's
 * own, precisely so the client never has to reproduce Python's
 * normalisation to answer that question (see glossaryLayers.ts's
 * `normaliseKey` docstring for why that used to be required, and what went
 * wrong). `effective` is every entry visible at this scope, least specific
 * layer overwritten by a more specific one, disabled entries included.
 * `problems` lists any layer that failed to load and was therefore treated
 * as empty rather than raised. */
export interface GlossaryLayers {
  scope: 'workspace' | 'channel' | 'event'
  own: { terms: Record<string, boolean>; replacements: Record<string, string | null> }
  own_keys: { terms: string[]; replacements: string[] }
  effective: {
    terms: Record<string, EffectiveTerm>
    replacements: Record<string, EffectiveReplacement>
  }
  problems: string[]
  /** The venue this EVENT selected, or null. Only ever non-null at event
   * scope. A PUT overwrites the whole layer, so a caller that means to keep
   * it must send it back — see putGlossary. Why a correction is scoped to
   * one selected venue rather than shipped globally (see tracks.py): a
   * global `carousel -> Karussell` would also rewrite the Carousel at Road
   * America, Sears Point and Watkins Glen, and faster-whisper truncates the
   * hotword prompt at 224 tokens, so a global list of every venue's corner
   * names would be silently cut. */
  track: string | null
}

/** Builds the glossary base URL for a scope, mirroring momentsBase and the
 * three route pairs in api.py exactly. An `event` without a `channel` is a
 * caller bug (there is no such backend route); it falls back to the
 * workspace base rather than building a malformed URL. */
function glossaryBase(scope: { channel?: string; event?: string }): string {
  if (scope.channel && scope.event) return `${eventBase(scope.channel, scope.event)}/glossary`
  if (scope.channel) return `${channelBase(scope.channel)}/glossary`
  return '/api/glossary'
}

/** GET the glossary layers for `scope` - see GlossaryLayers. */
export function getGlossary(scope: { channel?: string; event?: string }): Promise<GlossaryLayers> {
  return fetch(glossaryBase(scope)).then(asJson<GlossaryLayers>)
}

/** PUT the glossary for `scope` - OVERWRITES that scope's own layer with
 * exactly `terms` and `replacements` (see glossary_admin.update: never a
 * merge, so a caller must always send the FULL own layer it wants, e.g. via
 * rowsToOwn in glossaryLayers.ts). `track` is NOT sticky - omitting it CLEARS
 * the event's venue selection, because the server overwrites the whole
 * layer. Always pass back what the last GET reported unless the operator
 * changed it. Returns the freshly-read state. */
export function putGlossary(
  scope: { channel?: string; event?: string },
  terms: Record<string, boolean>,
  replacements: Record<string, string | null>,
  track: string | null = null,
): Promise<GlossaryLayers> {
  return fetch(glossaryBase(scope), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ terms, replacements, track }),
  }).then(asJson<GlossaryLayers>)
}
