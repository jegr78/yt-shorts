# The studio (local editor) — architecture notes

Loaded when working under `src/yt_shorts/studio/`. Moved here VERBATIM out of
the repository-root `CLAUDE.md`, which had grown to 178k characters loaded into
every session; nothing below was reworded, because this project's own history
is that restating a rule here is how it becomes false. The root file keeps the
prohibitions and points at this file — read both.

**The studio app is workspace-level (stage G1).** `create_app()` takes **no**
profile: `bin/yt-shorts studio` (no event) opens a start screen, and every
request names its channel (and event) in the path. Event routes live under
`/api/channels/{channel}/events/{event}/…` and resolve their `Profile` from the
path via `_load_profile` (an unknown channel/event or a malformed profile is a
404); auth is channel-scoped under `/api/channels/{channel}/auth` and reads only
`channel.json` (the id) plus, for the upload class, the channel-level `brand.json`
(the same event-independent read `cmd_auth` does) - it never loads a full profile.
Two listing routes (`/api/channels`, `…/events`) feed the start screen. An SPA
fallback (`GET /{full_path}`) serves `index.html` for any non-`/api` path so a
deep link or reload on a client-side route survives; it is registered LAST so it
never shadows `/api` or a built asset, and it refuses any `/api/*` path with a
404. `cmd_studio` picks a free port when 8765 is busy (a stale studio) rather than
dying.

**A rendered short is served by a VERSIONED url, and that is not decoration.**
`GET …/clips/{name}/short` used to be a constant path returning a bare
`FileResponse`, and both halves of that were stale-video bugs. The path never
changed, so after a re-render the refetched clip payload was byte-identical,
React never touched the `<video>`'s `src`, and the element kept the resource it
had already loaded - the operator watched the OLD short until a hard browser
reload. And the response carried NO `Cache-Control`, so the browser fell back to
heuristic freshness and could answer from its own cache without making a request
at all (it also ignored `If-None-Match`, so a revalidation re-downloaded the
whole video).

`_short_version` fixes the first half: one `stat()`, `(st_mtime_ns, st_size)`,
emitted as `short_version` beside `has_short` in `_summary` - so the clip list
and both detail responses carry it - and appended by the client as `?v=`. It is
deliberately NOT a content hash: hashing a multi-megabyte video per clip on
every list request is O(size) where this is one syscall, and `(mtime, size)` is
already the identity Starlette's own `FileResponse` derives its ETag from. The
token is OPAQUE - tests pin that it changed, never what it equals, and the
client never parses it.

Two rules about `v` that are easy to get wrong. It is a cache KEY, never a
precondition: a stale or garbage token still serves the current file, because
refusing it would turn a bookmarked link - or a request already in flight when a
render lands - into a 404. And the hard policy (`private, max-age=31536000,
immutable`) is returned ONLY when the token MATCHES the file's current version;
everything else gets `private, no-cache`. That match check is what keeps
`immutable` from being a lie in the window where a render lands between a
payload read and the video fetch.

**The match check only holds because `short.mp4` is never partial, and that is
`render.compose`'s job.** ffmpeg writes to a scratch sibling
(`short.part.mp4`) and the finished file is MOVED into place with
`Path.replace`, so the target appears complete or not at all. Before that,
ffmpeg wrote straight to the target with `-y`: from the moment a render started,
`has_short` was already true and `_short_version` returned a token for a
half-written file - so the token MATCHED, the route handed out `immutable` for
bytes still being written, and the manual channel's download link offered a
truncated video for an operator to upload to YouTube by hand. A render takes
minutes, so the window was wide open, not theoretical. The match check covers
"token stale, bytes new"; only writing aside covers "token current, bytes
unfinished".

Three things about that follow, and all three are pinned in
`tests/test_render.py`'s `TestComposeIsAtomic`:
- **The `.part` goes BEFORE the extension.** ffmpeg picks its muxer from the
  output's extension, so a scratch name ending in `.part` makes it refuse to
  write at all - the test that pins this fails with a RuntimeError from ffmpeg
  itself, not a tidy assertion.
- **A failed re-render no longer destroys the previous short.** `-y` on the
  target destroyed it the moment ffmpeg opened the file; now the old short is
  untouched until a new one is complete.
- **The scratch file is cleaned up in a `finally`.** The case that matters is a
  failure PART WAY THROUGH an encode (a full disk, a kill, the subprocess
  timeout); a real-ffmpeg test cannot pin it, because given unreadable input
  ffmpeg fails while probing and never opens its output, so the covering test
  stubs the subprocess to fail after writing.

The scratch file must stay a SIBLING of the target: `Path.replace` is
`os.replace`, atomic within one filesystem and not across them.

`shortUrl(name, version)` takes the version as a REQUIRED parameter, so `tsc`
fails if a call site forgets it. That matters because the second caller is
`ManualUploadPanel`'s DOWNLOAD link, and a stale short there means an operator
hand-uploading the wrong video to YouTube - a wrong artifact on the channel, not
a stale preview.

**Trimming a rendered short cuts seconds off the head and tail, and it is a
second write onto `short.mp4` with its own invariant.** `trim.py` is the ONE
place a rendered short is cut - it imports `clipstore`/`editorial` but never
FastAPI, the same rule `subtitle_pipeline.py` and `upload_policy.py` follow.
Three files, each with one job: `short.mp4` is the deliverable - what the
player, the download link and an upload all read, always already cut;
`short.full.mp4` is the untrimmed master, present only while a trim is
applied (derived, not an original - a re-render recreates it); `short.
trim.json` records which trim `short.mp4` currently embodies. THE INVARIANT
everything here exists to keep true: `short.mp4` embodies exactly the trim
recorded in `short.trim.json` - no file means no trim.

Cutting ALWAYS reads the master, never the current `short.mp4` - that is
what stops a second correction from compounding on the first. Trim 3s, then
change your mind and trim 5s, and the result is 5s off the ORIGINAL render,
not 8s off an already-shortened file. `forget_applied` is what a render
calls, BEFORE `ensure_applied`, and it drops the master AND the state file
together: after a render, `short.mp4` is a freshly composed untrimmed file
and IS the new master, so a `short.full.mp4` left over from before that
render is stale composition - cutting from it would build the deliverable
out of old material while the fresh short sat beside it unused. Dropping
only the state file would leave exactly that stale master in place for the
next `ensure_applied` to read from.

The cut re-encodes (a stream copy was measured and rejected: cuts land on
keyframes 4.18s apart in this project's own output, so a cut requested at
5.0s landed at 4.18s), and the ffmpeg invocation has one hard-won trap:
**`-ss` and `-to` must both come BEFORE `-i`.** Measured on this project's
own short: `-ss 5 -to 79 -i in.mp4` yields a 74.08s file; moving `-to` after
`-i` yields 79.10s instead, because the seek has already reset timestamps by
then and `-to` is read as a LENGTH rather than a position - the tail cut
silently does not happen, and ffmpeg exits 0 either way, so nothing but the
measured duration catches the regression. This is why the real-ffmpeg test
asserts the resulting DURATION rather than merely that the command
succeeded.

The player's URL and the download link are guarded differently, and
deliberately so: `GET …/clips/{name}/short` refuses `as=download` (409)
while a trim is pending, but the plain form the player uses keeps serving
the file regardless. The guard belongs to LEAVING the studio, not to
previewing - an operator judges the head/tail values by watching the
CURRENT (possibly untrimmed) short play, seeking to `head` and pausing at
`duration - tail`, which costs nothing precisely because it never asks the
server to cut anything. `shortUrl`'s third parameter, `purpose: 'play' |
'download'`, is REQUIRED for the same reason `version` is (see above): a
default of `'play'` would let a future download-shaped call site compile
silently and hand an operator an untrimmed file with no guard at all.

The last piece is `App.tsx` refetching the clip list and the open clip on BOTH
`window`'s `focus` and `document`'s `visibilitychange`. A render started by the
CLI is a different process: the studio's job runner and the CLI deliberately do
not know about each other, so the only evidence is the files on disk, and coming
back to the window is the trigger. Both events, because switching browser TABS
reliably fires `visibilitychange` while alt-tabbing to another APPLICATION does
not do so dependably. This is safe for unsaved edits only because `ClipEditor`
resets its staged edits when `clip.name` changes and not on every prop update -
if that ever becomes an unconditional sync, this refetch starts destroying
corrections.

**Event CRUD (G2) and channel CRUD (G3a) are where the studio writes outside
`edit.json`.** `event_admin.py` creates/renames/deletes event *directories*;
`channel_admin.py` creates/edits/renames/deletes *channels* (its `channel.json`
identity plus a scaffold `brand.json` + empty `fonts/`/`events/` on create - a
created channel is INCOMPLETE until a font is uploaded and assigned via the G3b
Brand editor). Both are pure (no FastAPI,
like `workspace_listing.py`); the studio routes are a thin layer over them
(`POST /api/channels`, `PATCH`/`DELETE /api/channels/{channel}`, `POST …/rename`,
and the G2 `…/events…` routes), mapping the module's `*AdminError.kind` to
400/404/409. Three rules make it safe:
- **Every path segment is validated** as one safe segment via the shared
  `pathnames.validate_segment` (`^[A-Za-z0-9][A-Za-z0-9._-]*\Z`) BEFORE any
  filesystem touch - the event name, the channel slug, every `{event}`/
  `{channel}` URL segment, and every clip name (via
  `clipstore.clip_dir_by_name`). A segment becomes a directory name, so
  `..`/slash/leading-dot never reach disk (the G2 review caught exactly this
  gap on the channel segment; do the same for any future write op).
- **Locks gate restructuring.** Event rename/delete take the event's `EventLock`;
  channel rename/delete refuse (409) while ANY of the channel's events holds a
  live lock, checked read-only by `EventLock.is_held()`.
- **Hard delete, typed confirmation.** Delete is `shutil.rmtree`, guarded by a
  typed-name confirmation in the UI. Channel delete never touches
  `<workspace>/auth/token-<id>.json` (outside the channel dir). This used to add
  "the studio writes `auth/` only via connect", and `PUT
  /api/providers/{provider_id}/key` falsified it: connect and that route are the
  TWO studio paths that write into `auth/`, mirroring the two that remove from
  it (see G4 below, which was amended for the delete side while this sentence
  was missed).

Inside an event the studio never edits a clip's `transcript.json` or
`sources.json`. It writes `edit.json`, and there are TWO further writes, not
one: a render it starts (which may replace that clip's own short - see the
boundary section) and a clip created from a window picked in the stream view
- CREATE for a genuinely new window, but an ordinary, idempotent UPDATE of
that same clip's `clip.json` (never its window, only a hook/title
correction) when the operator re-picks the identical window; it still may
not redefine a colliding, genuinely different one (see the boundary section
below).

**Brand & font editing (G3b) is the other channel-level write.** `brand_admin.py`
reads and validate-updates a channel's `brand.json`. The authority on WHICH
sections a patch may carry is the tuple in `update_brand`, and this sentence
must match it: `colors`, `fonts`, `subtitles`, `logo`, `output`, `upload`,
`bands` and `detect` - EIGHT. This file used to say "`colors`, `fonts` and
`subtitles` only - `output` is NEVER taken from the patch", and that had been
false for some time before the `detect` key made it wrong twice over; read the
tuple rather than this paragraph if the two ever disagree again. What makes the
wide list safe is that `_validate` then runs the SAME checks `profile.load`
does over the whole MERGED brand, so a patch this accepts is one `profile.load`
accepts - the "accepted => loadable" invariant holds for every section, which
is the property that actually matters, not the length of the list.
(`set_upload_mode` is the one deliberate exception to that validation: the
Settings screen's api/manual toggle must work on a channel whose brand is
otherwise still incomplete - a freshly created channel has no fonts yet.)
`font_admin.py` adds/removes files under a channel's
`fonts/` (a `.ttf`/`.otf` that `PIL.ImageFont.truetype` can load, ≤ 10 MB, a
safe-segment filename - and a font currently assigned as `brand.json`'s
`fonts.hook`/`small` is refused deletion with 409 `in_use` until reassigned). Both
are pure (no FastAPI; PIL is fine), like the other admin modules; the routes
(`GET`/`PUT /api/channels/{channel}/brand`, `POST`/`DELETE …/fonts/{filename}`)
are a thin layer mapping `*AdminError.kind` to 400/404/409, and the filename and
`{channel}` segment go through `pathnames.validate_segment` before any filesystem
touch, same as every other write op. The font ref stored in `brand.json` is
itself validated as `fonts/<safe-segment>`, so a `fonts/../..` ref cannot smuggle
a traversal into a later `profile.load`. Font upload reads the RAW request body
(`await request.body()`) - no `python-multipart` dependency. `POST
…/brand/preview` renders `overlay.build_overlay` on the edited (unsaved) brand and
returns a PNG - a read, like the clip preview (`build_overlay` reads
`config.get("decorate")`/`config.get("logo")`, so the minimal colors/fonts/output
preview config does not KeyError; any render failure returns 409, not 500). Still
no editing of event content.

**Settings (G4) is workspace-level auth management.** `GET /api/settings`
aggregates each channel's connection state - the same event-independent read
`GET /api/channels/{channel}/auth` does (`load_credentials` + `QuotaTracker` +
`upload_policy.mode`), looped over `list_channels`. Its payload was once
described here as "only booleans, the public channel id, an int/null quota, and
the workspace path/origin"; that is now too narrow. It ALSO carries, per
channel, `detect_provider`/`detect_model` (which provider scores that channel's
moments, read-only - the Brand editor is where it is SET), and, per registered
provider, `id`, `default_model`, `key_present`, `sdk_installed`, `install`,
`verified` and `prices`. The rule that has not changed, and is the one worth
stating: every one of those is a BOOLEAN, a shipped constant, or a public
identifier - never a token, secret, password, API key or a path into `auth/`.
The per-channel `detect` read is type-checked field by field rather than merely
defaulted, because `brand.json` is hand-editable and this read does NOT go
through `profile.load`'s validation: `{"detect": "gemini"}` reaches it intact,
and a bare `.get()` on that would AttributeError the WHOLE Settings page over
one channel's typo.

`DELETE /api/channels/{channel}/auth` is the studio's
"disconnect": it deletes ONLY `auth/token-<id>.json` via the pure
`auth.forget_credentials` (no google import; never the client secret or
`quota.json`), reversible by connecting again. It was described here as "the
first and only studio path that removes an auth token", and it is no longer
alone: `DELETE /api/providers/{provider_id}/key` deletes that provider's
`auth/<provider>.json` via the pure `providers.forget_api_key`, equally
reversible (paste the key again) and equally narrow - never another provider's
key, never the client secret, never a token. Say "the two studio paths that
remove a file from `auth/`", not "the only one". Channel *delete* still never
touches `auth/` at all. What guards the provider routes is not
`pathnames.validate_segment` but something stronger: the set of providers is
CLOSED, so `_provider_or_404` resolves the URL's id through `providers.get`
(404 on anything else) and the filename then comes from the resolved MODULE's
`KEY_FILENAME`, never from the URL - which is what makes it safe that
`save_api_key`/`forget_api_key` take their `filename` unvalidated. The
channel-scoped auth routes now validate `{channel}` via
`pathnames.validate_segment` inside `_load_channel` before any filesystem touch
(G4 closed the pre-existing unvalidated-segment gap there). Connect/switch stay
the existing `POST …/auth/connect` job. `google_require`/`GoogleOAuth` are still
imported lazily inside the route, so `create_app()` pulls no google at import.

**`yt_shorts/studio/` is the local editor's boundary, and it has two rules.**
First: it never REDEFINES a clip's harvested or transcribed data — not a
`transcript.json`, not a `sources.json`, and not an existing clip's WINDOW
(the field its identity is keyed on). `edit.json` is the one file it edits
outright, and `editorial.save` is the only code that edits it. Three things
it may WRITE besides, all only because the operator explicitly asked:
- **a rendered short**, from a render it starts (`studio.jobs`, guarded the
  same way `cmd_render` is). This one MAY replace an existing `short.mp4` -
  re-rendering a clip is ordinary, and `render.compose` is built to make that
  replacement atomic (see "A rendered short is served by a VERSIONED url" and
  `TestComposeIsAtomic`). Anyone restating this rule as "never overwrites"
  makes it false: that phrasing was tried here and had to be withdrawn.
  A render can also rewrite that clip's cached `transcript.json`, in one
  narrow case that is easy to miss: `transcribe()` re-transcribes and
  re-writes the cache when its recorded `source` does not match the current
  one - a cache written before that field existed, most concretely. It is
  self-healing (the next render of the same clip hits the refreshed cache and
  writes nothing) and it is not the studio's doing: `transcribe()` is shared
  verbatim with `cmd_render`. Named here anyway, because "never touches a
  transcript.json" is the kind of absolute this section has already been
  wrong about before.
- **a new clip**, from a window the operator picked in the stream view. This
  one may NOT redefine an existing, DIFFERENT window: `create_clip` refuses a
  colliding identity (`ClipIdentityCollision`) rather than overwriting it,
  because the directory's `edit.json` and `transcript.json` would survive
  describing the other moment.
- **that same clip's `clip.json`, but only on an exact re-pick.** Calling
  `create_clip` again with the IDENTICAL window (the ordinary case of an
  operator adjusting the hook and re-submitting) is an idempotent UPDATE, not
  a new clip: `clipstore.write_clip` rewrites `clip.json` in place, exactly as
  it always has for any other caller. This is the one place "never touches an
  existing clip.json" is genuinely false, and every attempt at this rule that
  omitted the carve-out has had to be corrected - see
  `test_studio_api.py`'s `TestCreateClipFromWindow.
  test_a_same_window_repick_with_a_different_hook_rewrites_clip_json`, which
  pins it.

So the rule is not one sentence about filenames. It is: transcribed data and
an existing clip's WINDOW are never redefined; a render may replace its own
output; clip creation may add a new clip, or idempotently correct an existing
one's hook/title on an exact re-pick of the same window - never its window.
Attempts at compressing that into a single "never" have repeatedly shipped a
claim the code contradicted — the flat list "never `clip.json`", "the only
write path is `editorial.save`", "never rewrites... a rendered short", and
then "never touches an existing clip.json" again once the stream view's
re-pick path made that false a second time. Do not try another bare "never
touches clip.json" - say "never redefines a window" instead, which is the
part that has actually held.
Second: a
render started from the studio takes the SAME `EventLock` `cmd_render`
does, acquired before a background job even starts, so a studio render and
a CLI render against the same event can never race each other - this is
what stopped a repeat of the incident that destroyed reference files
earlier in this project (see `stage-2a` in `.superpowers/sdd/progress.md`).

The first rule has a second exception besides that render, added with the
stream view (the ordinals are easy to trip over: this is a further carve-out
from rule ONE, not a third rule): `POST
…/streams/{video_id}/clips` CREATES a clip directory and its `clip.json`
through `clip_from_moment.create_clip`. That is a write outside `edit.json`,
and it is deliberate rather than an erosion - detection deliberately produces
no clips, so an operator picking a window is the only way one can come into
existence from the studio at all, and refusing it would leave the whole
analysis unusable without dropping to the CLI. It takes the same `EventLock`
a render does, for the same reason. **This is the SIXTH time this one rule
has been restated because an earlier phrasing turned out to be false** - see
the bullet list above for what actually holds (a new clip may be created; an
exact re-pick idempotently updates that same clip's `clip.json`; a genuinely
different, colliding window is refused; `transcript.json`/`sources.json`/a
rendered short are never touched from this path). "The studio never EDITS an
existing clip.json" was itself wrong the moment the idempotent re-pick path
shipped (see `test_studio_api.py`'s `TestCreateClipFromWindow.
test_a_same_window_repick_with_a_different_hook_rewrites_clip_json`, which
pins it). If a future pass restates this rule again, say "creates or
idempotently updates on an exact re-pick, never redefines a window" - not a
bare "never edits" or "never touches".

`yt_shorts/studio/web/` is the editor's frontend (React + Vite + Mantine,
TypeScript); `api.py` serves its BUILT output from `yt_shorts/studio/static/`,
which is git-ignored (see the wiki's
[Studio](https://github.com/jegr78/yt-shorts/wiki/Studio)) and built by
`npm run build`,
by `tools/build-binary.py` and by `hatch_build.py` - changing the frontend
without rebuilding leaves the served page stale, and the E2E tests serve
whatever `static/` currently holds.
The frontend is a SEVEN-screen client-side router (`Root.tsx`/`useRoute.ts`,
parsed by `scopedApi.ts`'s `parseRoute`): channels, events, the editor,
settings, logs, the stream view (below) and the Jobs screen (the job queue,
below) - up from the three (channels, events, editor) stage G1 originally
shipped, and from the six this sentence claimed until the queue landed; do not
let that count drift back out of date again. Its pure logic lives in its own
modules - `words.ts`,
`format.ts`, `window.ts`, `upload.ts`, `scopedApi.ts` (the scoped-URL
builder), `streamTimeline.ts` (the stream view's own geometry, below),
`providers.ts` (the model-provider labels, blockers and the cost disclosure
below), `jobs.ts` (the plan's own rules - state labels, allowed actions, stop
warnings, and `waitNote`, the sentence every panel that queues work shows while
it waits; no longer the Jobs SCREEN's alone, see below), `streams.ts` (the
stream list's own rules - the playlist filter, and `bulkPlan`, what a
multi-row queue action actually queues and skips, below) - NOT
exported from component files, so Vite's fast-refresh boundary stays
component-only, and is unit-tested with Vitest (`npm test`, jsdom). Vite's
`base` is `/` (absolute), because the router puts real paths like `/erf/<event>`
in the address bar and a relative asset base would resolve against that deep path. Vitest
is SEPARATE from the pytest suite (a JS runner is not folded in, exactly as
`npm run build` is separate); the integrated flows stay covered by the Playwright
E2E inside pytest. Run `npm test` before committing a frontend change.
**Bare `npx tsc --noEmit` is INERT here - it type-checks nothing, silently.**
`web/tsconfig.json` is solution-style (`"files": []`, only `"references"`),
so bare `tsc` obeys the empty file list; `npx tsc --noEmit --listFiles` lists
ZERO files, and a deliberate type error added to `src/api.ts` still exited 0
under bare `--noEmit` while `npx tsc -b` (what `npm run build` runs) and
`npx tsc --noEmit -p tsconfig.app.json` both caught it - measured, see
`web/README.md`. `npm run build` is the real type-check; treat a clean bare
`tsc --noEmit` as no signal at all, not as proof of anything.

**The stream view is the router's fourth route level and the studio's only
screen that reaches inside a stream rather than an event.** The channel/event
chain otherwise bottoms out at `/{channel}/{event}`, the editor;
`/{channel}/{event}/streams/{video_id}` (`StreamScreen.tsx`, reached by
clicking a stream in the event screen's Streams tab) goes one level deeper,
because a stream is hours long and none of its content - transcript,
analysis, a picked window - has an identity at the event level the way a clip
does. (`/settings` and `/logs` are separate, workspace-level screens outside
this chain entirely - see their own sections above.)

It is built to be **useful with no API key at all**: `GET …/moments`
answers 200 with an EMPTY analysis (`engine: null`, `moments: []`) when
detection has never run, rather than 404ing the screen into uselessness, and
`POST …/estimate` (`estimate.py`) is a local, character-counted, deliberately
approximate cost preview - no network, no key - so an operator can decide
whether configuring one is worth it before spending anything. A transcript
alone is enough to search it, drag a window on the zoom lane and make a
clip; detection only adds a ranked hit list on top. This is the property
`TestStreamScreenNeverAnalysedJourney` in `tests/test_studio_e2e.py` pins
end to end - transcript search, a hand-dragged window and a created clip on
disk, with no `moments.json` in existence at any point.

**Two lanes, not one, because one cannot do the job at eight hours.** An
endurance stream is six to eight hours long; over a 1200-pixel overview
strip that is roughly 24 seconds per pixel, so a 20-second clip is under a
single pixel and no boundary could be dragged there at all (`streamTimeline.ts`'s
own module docstring works this out in full). The overview strip LOCATES -
click it and it re-centres the zoom window, it never sets a selection
directly - and the zoom lane EDITS, wide enough that a second is several
pixels and a drag is actually precise. `zoomAround` is the bridge: picking a
hit list row moves the zoom lane to straddle it, moves the player and
scrolls the transcript to it, all three at once (`StreamScreen.handlePick`).
`streamTimeline.ts`'s own `curveBucket` (second-to-activity-bucket lookup) is
the same kind of deliberate, tested groundwork as `pump_subprocess` in
`logsetup.py` (see that entry below) - covered in `streamTimeline.test.ts`,
including the lower-clamp regression that motivated pinning it, but not
called by any component today; it exists for a later addition (e.g.
highlighting the overview strip under the player's current position) rather
than anything the two lanes need right now. Do not delete it as dead code.

**The hit list surfaces `engine` and `missing_windows` because a silent
degradation is this project's recurring failure mode** (see subtitle_pipeline's
history above, and "One engine per run" below). A lexicon-fallback hit list
looks identical to a model-scored one unless the engine is named, and a
window that failed mid-scan is an hour of the stream nobody looked at that
is otherwise indistinguishable from a genuinely uneventful hour - `HitList.tsx`
shows both rather than letting either pass as ordinary. `TestHitListBadNewsAlerts`
pins all four engine/missing-window combinations - lexicon alone, a clean
model run (asserting NEITHER alert fires), missing windows alone, and
lexicon together with missing windows - plus the "never analysed" state (no
alert, "not analysed yet" message, zoom lane still offered) as a fifth case
outside that grid. The lexicon-plus-missing-windows combination is the one
that matters most, not just the one that completes the count: both alerts
firing together is exactly what starved `HitList`'s own `ScrollArea` to 0px
in an earlier task (see its 120px-floor comment in `HitList.tsx`), and it is
the worst case an operator can actually hit - a weak engine AND hours of the
stream nobody looked at, at the same time. That test also proves the hit
list's rows stay reachable underneath both alerts, with the wheel-based
helper below, not just that the alerts render.

**A reachability E2E assertion drives a real mouse wheel, never
`scroll_into_view_if_needed()`.** `tests/test_studio_e2e.py`'s
`_wheel_scroll_until_visible` hovers a fixed point over the pane that should
own the scroll and repeats `page.mouse.wheel()` until the target's own
bounding box sits inside the viewport - because `scroll_into_view_if_needed()`
was PROVEN, on a real regression in this branch, to pass on a broken build: it
drives an element's `scrollIntoView` algorithm, which sets `scrollLeft`/
`scrollTop` on ANY ancestor whose computed `overflow` is not `visible` -
including an `overflow: hidden` container with no scrollbar and no wheel
handler, which is exactly what this screen's pre-fix layout had. A reviewer
measured it landing the search box's bounding box back on screen (`scrollLeft:
853` on a container no real wheel could ever move) while the underlying bug
was still there. The one pre-existing use of `scroll_into_view_if_needed()`
elsewhere in this file (`TestGlossaryEditor`) stays safe only because its
scrolling ancestor is `overflow: auto`, checked by hand, not assumed - do not
generalise from it. Any NEW reachability assertion in this suite should use
the wheel helper, not that call.

**Stream discovery is `youtube.list_streams`, not a YouTube Data API call.**
It runs one `yt-dlp --flat-playlist --dump-json <channel_url>/streams` with
the subprocess boundary injected as `runner` (so parsing and error handling
test without the network, the same way `harvest` isolates its yt-dlp call),
tolerates a malformed entry per-line, and raises `YouTubeError` on failure.
Like `subtitle_pipeline`, it lives OUTSIDE `yt_shorts/studio/` and must not
import FastAPI - the studio's `GET /api/streams` calls into it, never the
reverse. There is no API key anywhere: yt-dlp is the tool's downloader
already, streams are public, and cookies remain a future addition for
unlisted ones (see the stage D1 design).

**And the studio now reads a CATALOGUE, not just that tab.**
`youtube.channel_catalogue` composes three reads - the Streams tab, the
channel's playlist list, and every playlist's members (parallel, six
threads, each worker calling the same injected `runner`) - into one answer
the Streams tab filters by playlist. Measured on ERF, 2026-08-04, and every
one of these is a reading of one channel on one day rather than a property
of YouTube: 91 streams, 17 playlists, 99 distinct videos, all 17 member
fetches in 2.5s at six threads - both measured directly, and each playlist
fetch on its own costs about 1.2s, also measured. The "20s sequential"
figure beside it is not a third measurement - it is 17 x 1.2s extrapolated
from the per-playlist figure, not a run actually timed one playlist after
another.

**The union is the point, not a side effect.** Eight ERF videos live in a
playlist and NOT in the Streams tab, two of them multi-hour broadcasts
(`was_live`, 2h30 and 2h06) - which means they were unreachable from the
studio entirely. That is why a selected playlist shows the PLAYLIST's
contents rather than the Streams tab filtered by membership: the filtered
reading cannot show a video the tab never listed, and would present a
partly-empty playlist with no explanation.

One loss is counted rather than swallowed, for the reason this file records
everywhere else: a playlist entry with no title is a deleted or private
video (two on ERF) and is dropped but counted as that playlist's
`unavailable`, so a displayed "(6)" is never silently a 6 that came from 8
because of a deleted or private member - `streams.ts`'s `playlistOptions`
folds a nonzero `unavailable` into the playlist's own filter-dropdown label
("<title> (<count> + <unavailable> unavailable)"), the one place in this
screen that reads the field at all. `list_playlist_videos` drops two other
malformed shapes - an unparseable JSON line, and an entry with no `id` at
all - WITHOUT counting them; both are yt-dlp output defects rather than a
YouTube-reported unavailability. And a playlist whose fetch fails is named
in `failed_playlists` while the rest is served. The Streams tab's OWN
failure still raises - without it there is no list at all.

**Two drops, two different answers, and the difference is the rule.**
`list_playlist_videos` COUNTS a titleless entry as that playlist's
`unavailable` and does NOT count a video listed twice, which it simply
collapses. The test that pins this is not pedantry: `unavailable` means
"this playlist holds a video you cannot have", so counting a duplicate
there would report a loss that did not happen - the same dishonesty as
hiding a real one, pointed the other way. Anything else this parser ever
drops has to be sorted into one of those two boxes before it is written.

`playlist_ids` is a LIST on every video. No ERF video is in two playlists
today; that is an observation, not a guarantee, and the field must not be
narrowed to a single id on the strength of it.

**`GET …/streams` caches the expensive half only.** The yt-dlp reads are
cached per channel for the session, as they always were. `has_transcript`
and `has_analysis` (`detect.has_cached_transcript`/`has_analysis`, one
`Path.is_file` each) are stat'd FRESH on every response: a cached "no
transcript" would survive a finished transcription until someone pressed
refresh, and a marker that outlives the fact it reports is worse than no
marker. Both answer False for an id that is not a safe segment rather than
raising - they are asked once per video over a whole catalogue, and one odd
id must not 500 a list of 99. `detect.stream_dir` still raises, which is
what `require_cached_transcript` needs.

**Building a path helper on another one is not borrowing its guard.**
`analysis_path` and `windows_dir` are `stream_dir(...) / <name>` now, and
each used to spell out its own `validate_segment`. The rule they carried -
a write op validates its own path segment rather than relying on a caller
having done it - is unchanged and still true: the check runs inside the
call, before any filesystem touch. What changed is that it lives in one
place instead of three. `tests/test_detect.py`'s
`TestTheStreamPathHelpersAgree` pins both halves, so the refactor cannot
later be read as having dropped the rule. `stream_transcribe._stream_dir`
is a fourth copy and stays one: `detect` imports `stream_transcribe`, so
it cannot import back without a cycle.

**And that tolerance depended on a promise `validate_segment` did not quite
keep.** `_has` catches `ValueError`; a truthy NON-STRING id (an int from a
hand-edited `jobs.json`, a malformed yt-dlp line) used to reach `len()` or
`NAME_PATTERN.match()` inside `validate_segment` and leave as a
**TypeError**, which walked straight past that handler - so the one shape
the tolerance exists for was the one it did not cover, and a single odd id
sank the catalogue after all. The falsy non-strings (`None`, `0`, `[]`)
answered `ValueError` all along, which is why one defect had two answers
depending on the value. `validate_segment` now refuses a non-string as
`ValueError` like any other bad segment, and every docstring in this
project that promises `ValueError` is true rather than nearly true.

**The server side of that promise is worthless if the client never asks
again, and for a while it did not.** `StreamPanel`'s catalogue is fetched
once from a mount effect, the ⟲ icon and the error Retry - and
`Tabs.Panel` keeps the panel mounted once the operator has visited the
Streams tab, so those were the only three triggers there were. A tracked
transcription or detection finishing left the row's badge stale until one
of them fired, and `bulkPlan` kept offering to queue work already done -
the exact staleness the fresh-stat design above exists to prevent, just
moved one layer up to a component that never re-asked. `App.tsx`'s own
settle effect (the one that raises `batchNotice` when a tracked stream
entry reaches a terminal state - see "Every panel that queues work KEEPS
its entry" below) now also bumps a plain counter, `streamCatalogueStaleAt`,
threaded down as a prop; `StreamPanel` reacts to a CHANGE in it (skipping
its own first render, since the mount effect above already loads once) by
calling its own `load()` with NO `refresh`. That is the deliberate half of
the fix: a plain `load()` hits the per-channel yt-dlp cache from memory and
only re-runs the two `stat` calls per video, while `refresh: true` would
re-invoke yt-dlp for a fact - whether a transcript/analysis exists on THIS
machine's disk - that has nothing to do with YouTube. The counter is bumped
ONLY on a genuine settle (once per finished entry, in the same place the
notification fires), never once per 750ms poll, so a long-running batch
does not turn this into a re-load loop for as long as anything is tracked.

**The studio's background work is planned by a QUEUE, and the queue is two
pools rather than one number.** `job_queue.py` owns the plan, the pools, the
state file and the transitions; it is pure - stdlib only, no FastAPI, no
`yt_shorts.studio.*` - the same rule `subtitle_pipeline.py`,
`upload_policy.py` and `pathnames.py` follow. It does not import
`studio.jobs` even though that is where the real kind table (`jobs.KINDS`)
lives: `kinds` is duck-typed, anything with a `.pool` and a `.queueable` per
kind name, so `studio/` imports the queue and never the reverse. It spawns
nothing and runs nothing - it decides ORDER and ELIGIBILITY, and
`studio/worker.py` is the one thread that turns a claimed entry back into the
`start_*_job` call a route would otherwise have made. That split is why
ordering, pool limits, transitions and restart recovery are unit-tested with
no server, no network and no work of any kind.

**One "max N jobs" is the wrong dial, because the kinds saturate different
resources.** A transcription pins the CPU for over two hours (see the
corrected measurement above); a detection spends nearly all of its time
waiting on a model API. A single limit either serialises two detections that
could happily have run together, or lets two transcriptions each make the
other half as fast while they fight for memory. So a kind maps to what it
actually saturates - `cpu` for `transcribe`/`render`/`trim`, `net` for
`detect`/`upload` - and each pool carries its own cap (`worker.DEFAULT_LIMITS`:
cpu 1, net 3, a starting point for this machine rather than a measurement). A
pool ABSENT from the limits dict is unlimited, by construction rather than by
a special case: an operator who configures no limit for a pool gets no limit.

**Two workspace files, both gitignored ROOTED, and the rooting is not
pedantry.** `<workspace>/jobs.json` is the plan;
`<workspace>/settings.json` holds the operator's pool limits. Both sit beside
`logs/` and `auth/`, and both are ignored with a LEADING SLASH for exactly the
reason `/moments.json` and `/glossary.json` already are: `workspace.resolve()`'s
last resort is the repository's own root, so on an unconfigured checkout that
root IS this repository and these files land in the tree. The leading slash
holds the pattern to that one file instead of to every file of that name at
any depth - which is what keeps the suite's own tracked fixtures safe in the
glossary/moments case, and is the habit to copy for the next workspace-root
file rather than a rule that has already been tested by one. The two files stay separate on
purpose - `JobQueue.set_limits` deliberately does NOT save, because a setting
with two homes is a setting that can disagree with itself; the route writes
`settings.json` and calls `set_limits`, so the live queue and the next restart
agree. `jobs.json` is written write-aside-then-`os.replace` (the mechanic
`render.compose` and `providers.save_api_key` use), and a file it cannot parse
is RENAMED ASIDE and reported through `load_error` - never overwritten, never
silently discarded, with the aside name taken from the injected clock so a
second corruption cannot clobber the first one's evidence. Losing an
operator's plan quietly is worse than starting empty loudly.

**No parameter that looks like a secret is ever written, and that is this
module's property rather than its callers' good manners.** `enqueue` REFUSES
(never strips) a params key whose NAME carries key/token/secret/password/
credential, and refuses a nested dict at any depth besides. The nested rule is
not defensive tidiness: a review drove `{"creds": {"api_key": "sk-ant-..."}}`
straight past the name check into `jobs.json` and back out through
`GET /api/jobs`, because `"creds"` looks like nothing and nothing ever looked
inside it. Lists of scalars stay allowed (`render`'s `clips` needs one). A
refusal rather than a redaction, for the same reason the name check refuses:
no shipped kind takes a nested param, so an entry carrying one is a mistake to
report, not a value to quietly rewrite. `api._safe_params` still redacts on
the way OUT, at any depth, which covers a `jobs.json` this version never wrote.

**A queued upload is always private, and it is refused at both ends.**
Anything more exposed is an explicit, confirmed, per-upload operator choice
(stage E above), and a queue entry is written now and run hours later out of a
state file that can hold no confirmation. `POST /api/jobs` refuses a
non-private `visibility` or any `publish_at` at the click, and
`worker._start_upload` refuses it again when the entry runs. Quietly
downgrading it to private would upload something the operator asked to be
public without telling them; honouring it would publish without the
confirmation the gate exists to require.

**The cancellation safety property everything else rests on: a hard stop
terminates only the SUBPROCESS the work is waiting on, never the thread.**
Python threads are not killable, so stopping is cooperative: a `CancelToken`
(`cancel.py`, injected as a parameter like `runner`, `logger`, `on_note` and
`caller` elsewhere) has two levels - `request_stop` is the ask the work checks
at a boundary it chooses, and `request_kill` additionally terminates the child
`cancel.run_cancellable` is blocked on, REAPS it, and raises `Stopped`. The
thread then runs its own `finally` blocks, and that is precisely what makes a
cancel unable to leave a half-written artifact: `trim.ensure_applied` removes
`short.trim-part.mp4` in one, `render.compose` removes `short.part.mp4` in the
other, and those two `finally`s are the whole reason the deliverable is either
complete or untouched (see "The match check only holds because `short.mp4` is
never partial" above). A kill that ended the thread would skip both. Nothing
in this subsystem tries to kill a thread, and `Stopped` is never relabelled a
failure - each job body handles it explicitly and AHEAD of any blanket
`except Exception` (`_run_detect`, `_run_transcribe` and `_run_trim` each name
it first; the render loop breaks on it and finishes `stopped`, which outranks
`failed` because the run did not finish), because a stop is what the operator
asked for and reporting it as `failed` sends them looking for a cause that does
not exist. `trim.ensure_applied` goes one step further and lets `Stopped` ride
alongside `TrimError` through its recovery handler so the deliverable is put
back either way, while the exception still reaches the runner as itself.

**One window of a render is deliberately NOT interruptible: the trim that
follows it.** `studio.jobs._render_one` calls `trim.ensure_applied` after
`build_short` has already succeeded, and passes no `cancel=` - so a stop
asked for during those seconds is not seen until the loop's next clip. That
is deliberate, not an oversight. `ensure_applied` checks the token at its
own ENTRY, so handing it one would make it raise `Stopped` immediately
after a good compose, costing the operator a completed clip's record for a
cut that takes seconds - the stop would destroy exactly the work it was
meant to leave alone. The window is short and bounded (a re-encode of one
already-rendered short), the deliverable is still atomic either way
(`short.part.mp4`/`short.trim-part.mp4` and their `finally`s), and the stop
lands one clip later. Written down because in this repo an undocumented gap
is how tomorrow's false claim starts: "everything in a render is
stoppable" is the sentence someone would otherwise write next.

**`upload` has no stop at ANY level, and the UI must never offer one.** A
half-finished upload to YouTube is worse than waiting for it.
`STARTERS["upload"]` takes no token deliberately rather than by omission, so
there is nothing for a stop route to reach, and `Worker.request_stop` answers
`not_stoppable` (409) rather than pretending. **`stoppable` is therefore a FACT
on the wire, derived from whether the starter TAKES a token - deliberately not
from parsing `stop_point`'s prose.** The client used to infer it by comparing
the literal phrase `"cannot be stopped"`, so a case-only edit to
`KINDS["upload"].stop_point` left 389 Python tests green while putting a Stop
button on a running upload, the one thing that entry exists to forbid.
`stop_point` is a LABEL - what to tell the operator about WHEN a stop would
land - and nothing else. A hard stop is likewise refused where
`hard_stop_allowed` is False rather than quietly downgraded to a graceful one;
`copy` is the instructive case, since its stop point ("after the current file")
is real while a `copytree` waits on no subprocess at all, so a hard stop there
could never do anything the graceful one does not.

**`stopping` is a real state, and it keeps holding its pool slot.** After the
click the entry shows `stopping`, not `stopped`, because the work has not
reached its safe point yet and a screen that said otherwise would invite a
second click. The slot is not handed on while it sits there: the thread is
still using the CPU or the network, so giving the slot to the next entry would
oversubscribe the pool silently. A visibly stuck job is better than a queue
that quietly runs more than it says. `_pool_has_room` counts `running` and
`stopping` alike, which is where that rule actually lives. And a stop route
must go through `Worker.request_stop`, never `JobQueue.mark_stopping`: the
queue holds no token and reaches no thread, so marking first would produce an
entry claiming a stop was asked for while nothing had told the work anything -
a button that lies. An entry ALREADY `stopping` may still be escalated to a
hard stop, and that escalation performs the kill and skips the mark rather
than raising for something it did.

**`EventLock` is not replaced by the pools, and a held lock leaves the entry
QUEUED with a reason, never failed.** They are different mechanisms: the pools
bound LOAD, the lock protects ONE event (see the incident in
`.superpowers/sdd/progress.md`'s `stage-2a`). Two different events may run at
once; the same event never may, and a CLI render holding that lock is normal,
temporary and nobody's mistake - so `JobQueue.defer` puts the entry back where
it was with the reason recorded, and `claim_next(skip=...)` passes over it for
the rest of that pass so a locked event cannot block an entry for a different
one. There are TWO paths into that waiting state and both are needed:
`Worker._blocked_by` is asked BEFORE the claim, which is what stops a lock held
for hours from costing a claim and a defer - two rewrites of `jobs.json` - on
every pass, with the entry oscillating queued -> running -> queued ON DISK
where a Jobs screen reading at the wrong instant showed `running` for an entry
that never ran; `defer` still handles the race that pre-check cannot close,
where the lock is taken between the question and the starter's own acquire.
The worker never acquires the lock itself - each `start_*_job` does, so a
worker that took it first would make every starter refuse its own job.

**An interrupted entry never restarts by itself.** `Worker.start()` calls
`JobQueue.recover`, which turns anything left `running`/`stopping` by a dead
process into `interrupted` with its reason. Only an explicit `retry`
re-queues it, because a detection run spends real money and a job that quietly
starts on launch and bills for it is a bad surprise. `retry` re-enqueues the
entry and does not resume from a saved position - the queue holds none, and
neither long kind needs one: a `transcribe` restarts at the first missing chunk
and a `detect` at the first unscored window, because each caches its own unit
of work. That symmetry is why the UI needs no per-kind warning about what a
retry costs.

**`start()`, NOT `__init__` - recovery is not a side effect of constructing
an app.** It used to run in `Worker.__init__`, and it writes entry states
into a file another process may own, which cost twice: every `create_app()`
in a ~2200-test suite rewrote whatever plan it found, and merely STARTING a
second studio against a live workspace marked the first studio's
genuinely-running two-hour transcription `interrupted` - a state whose own
text says it was running when the studio died. The rule that "`create_app`
starts nothing" is therefore now "`create_app` starts nothing and WRITES
nothing", which is the stronger reading and the one `TestCreateAppWiring`
was always aiming at. Recovery runs before the thread inside `start()`, not
after: an entry left `running` still occupies its pool slot, so a pass that
ran first could find a full pool and start nothing.

**`retry` takes `stopped` as well as `failed` and `interrupted` - only
`done` is refused.** That is the operator's own decision, and it makes a
promise the studio was already printing come true: the stop dialog says "a
retry resumes at the first window nobody reached" (detect) and "from the
first missing chunk" (transcribe) BEFORE the click, while `allowedActions`
offered a stopped row nothing but Remove and the route answered 409. The
per-window and per-chunk caches that make "stopping costs nothing" true are
exactly what makes a resumption cheap, so the fix was to offer the control
rather than to soften the sentence. `done` stays refused: re-running work
that succeeded is a new request, not a retry, and for a paid kind it spends
the money again.

**ONE STUDIO PER WORKSPACE, and that is what makes `jobs.json`'s
single-writer persistence safe.** `JobQueue.save()` replaces the whole file
from an in-memory list read once at construction, so two processes on one
`jobs.json` destroy each other's plan - measured, not feared: the second
studio's startup interrupted the first's running jobs (above), and the next
`save()` from either silently dropped everything the other had queued, with
nothing logged anywhere. Nothing else stopped it, because `cmd_studio`
deliberately picks a FREE PORT when 8765 is busy (a stale studio must not
brick the tool), so a second `bin/yt-shorts studio` started perfectly
happily. `lock.StudioLock` refuses it now: a `.studio.lock` at the workspace
root, taken by `cmd_studio` BEFORE the app is built and released when the
server exits, naming the holding pid and what to do instead. It shares
`_PidLock` with `EventLock` rather than reinventing the mechanism - so a
stale lock from a killed studio is taken over exactly the way a crashed
render's is, and one crash cannot cost an operator their tool. It is NOT
taken by `create_app()`, for the same reason the thread is not started
there. A caller that opens a second `JobQueue` on a live workspace outside
that guard is asking for the corruption above.

**An entry whose KIND this build does not know fails that one entry, and
that is the difference between a defect and a wedge.** `load` accepts any
`kind` string (`Entry(**item)` over a hand-editable file, or a downgrade
after a later version added a kind), and `claim_next`/`_pool_has_room` used
to index the kinds table with it. The `KeyError` was caught by the worker's
loop, logged and retried a second later, forever: the thread survived, NO
job of any kind for any event ever started again, `GET /api/jobs` answered
200, `worker_running` reported true, and `waitNote` told the operator their
entry was next in line. It is failed with a reason naming the kind now -
the same stance `_start` already took for a kind it has no starter for, and
the one `drain_once`'s own docstring promises ("never raises for a defect in
ONE entry"). An active entry of an unknown kind is counted against no pool
rather than raising from the room check.

**Stopping a detection costs nothing, because its windows are cached.** An
earlier draft of the design said the opposite - that stopping after window 3
of 9 discarded three paid windows and the UI had to warn before the click -
and that was a workaround for a constraint this project does not have. A
warning is the consolation prize; not losing the windows is the fix.
`moment_scan.scan` takes two additive, injected seams that both default to
None: `should_stop`, checked at the TOP of the window loop and OUTSIDE the
`try`, and `window_cache` with `get(index)`/`put(index, moments)`. The
placement of `should_stop` is the whole point and it is measured, not argued -
the handler inside that loop catches `Exception` per window and records it in
`missing_windows`, so a stop signalled as an exception from inside the injected
`caller` is swallowed and the scan carries straight on (a test pins that
swallow). `moment_scan.py` stays FILESYSTEM-FREE: the disk-backed cache is
`detect.WindowCache` at `streams/<video_id>/windows/`, exactly symmetric with
the chunk cache at `streams/<video_id>/chunks/` that already makes
transcription free to stop and resume. Three details there are load-bearing:
each window is written as it is scored rather than batched at the end (a cache
written only on a clean finish would be worthless to the one run that needs
it), a FAILED window is never cached (caching a failure would make the hole
permanent), and every file records a FINGERPRINT of what the model was asked -
the engine, the above-zero markers and the transcript's own words - so a
re-decoded chunk is a miss rather than last week's scores served for text that
no longer exists. `scan` records windows after the stop in `unscanned_windows`,
NEVER `missing_windows`: "nobody looked" and "attempted and failed" are
different facts, and conflating them tells an operator an hour was examined
when no request was ever made for it. A stopped detection therefore writes NO
analysis at all and raises `Stopped` - a `moments.json` covering three of nine
windows would read as a complete run, with a model named in `engine` and six
silent hours, which is this project's recurring failure mode in a new costume.

Detection also no longer transcribes in the studio while the CLI still does;
that split, and why the two callers get different policies, is recorded in
"Whole-stream transcription" above rather than restated here.

**The Jobs screen is the router's seventh screen, at workspace level beside
`/settings` and `/logs`** - not in the channel/event chain, because a queue
belongs to the workspace. `GET /api/jobs` returns the plan in three sections -
`running` (which HOLDS `stopping`), `queued` (which holds `paused`) and
`finished` (the four terminal states) - plus the pool limits, `worker_running`
and `load_error`; `jobs.ts` holds every rule about what a control may promise,
so they are unit-tested without rendering. Four things about that payload a
screen gets wrong by default:
- **`Entry.progress` is written by three kinds and by no others, and the
  shape is the QUEUE's rather than each kind's.** The work reports two
  numbers - `progress(done, total)`, per chunk in
  `stream_transcribe.transcribe_stream`, per window in `moment_scan.scan`,
  per clip in `studio.jobs._run`'s loop - and `studio.worker.Worker.
  _progress_reporter` adds the UNIT from `jobs.KINDS[kind].progress_unit`,
  writing `{"unit", "done", "total"}` through `JobQueue.mark_running`. So
  `progressLabel` reads "chunk 20 of 50" without knowing which kind produced
  it. Four rules hold this together, and each of them was a way to get it
  wrong:
  - **A kind that counts nothing is handed no callback at all.**
    `progress_unit` is None for `trim` and `upload` (and it is a REQUIRED
    keyword-only field on `KindSpec`, so a kind added to the table has to
    say), `progressLabel` returns null, and the row renders nothing. A trim
    is one ffmpeg cut; "1 of 1" for it is a decoration, and worse than
    silence.
  - **A reading is cleared by every transition that takes an entry out of
    `_ACTIVE_STATES`** - `mark_finished`, `defer` and `recover` all call
    `_clear_progress`, as `retry` already did. `mark_stopping`
    (`running` -> `stopping`) deliberately does NOT clear: the work is
    still in progress and `stopping` is still inside `_ACTIVE_STATES`, so
    blanking the reading there would hide the last thing known about a job
    that has not finished. A `done` row still saying "chunk 20 of 50" is a
    stale claim about a job that is over, and this file is read back after a
    restart. `mark_running` refusing any state but `running` is the other
    half: a callback that arrives late (the work carries on while the entry
    sits in `stopping`) cannot resurrect one.
  - **A reading must never cost the run.** The callback crosses threads -
    the job's thread writing into a queue the worker's thread and the routes
    share - so it takes the queue's own lock and never `Worker.lock` (the
    order is Worker.lock first, and the reverse is the inversion that would
    deadlock), and it swallows every exception. Both of the ways it fails
    are ordinary rather than exotic: the whole-file save can fail, and
    `mark_running` REFUSES a `stopping` entry. One unreported unit costs one
    reading, never a two-hour transcription or a paid detection run. This
    used to say that guarantee lives in ONE place, the reporter, "because it
    is the one place a production callback is built" - false: `bin/yt-shorts`
    builds a second one, a bare `print` handed straight to `moment_scan.scan`
    for its `detect` command, and a closed stdout (`yt-shorts detect ... |
    head`) used to raise `BrokenPipeError` out of `scan`'s three unwrapped
    `progress(...)` call sites and abort a paid scan after one reading,
    writing no `moments.json` at all - measured, not theorised. The CLI's own
    callback (`bin/yt-shorts`'s `_report_progress`) now swallows every
    exception the same way, for the same reason, so the guarantee holds at
    BOTH places a production callback is built today - not because the code
    lives in one module, but because every place that builds one applies the
    same rule. The three producers (`stream_transcribe`, `moment_scan`, the
    render loop) still call their callback plainly, as `moment_scan.scan`
    always has.
  - **A whole-file save per unit is deliberate.** A chunk is minutes, a
    window is an hour of stream, a clip is minutes. Anything finer needs
    `logsetup.LineThrottle`, which exists for exactly that and is still
    called by nothing.
- **`worker_running: false` is not "idle".** `create_app()` builds the worker
  and leaves it STOPPED - only `bin/yt-shorts studio` starts it - because over
  two thousand tests construct an app, and one that acquired an `EventLock`,
  spawned a thread OR rewrote the plan on disk as a side effect of
  construction would be a defect in every one of them (see "`start()`, NOT
  `__init__`" above for the third one, which was true until a whole-branch
  review found it). So a full plan can sit motionless with nothing wrong, and
  `stallNote` must say "the worker is not running", never "nothing to do".
- **`finished` arrives in QUEUE order, oldest first** - it is the plan sliced
  by state, not an activity feed; the screen reverses it for its own heading.
  `_trim_finished` keeps the most recent 50 terminal entries and never caps the
  pending ones, so `jobs.json` cannot become a second, unpruned copy of
  `logs/jobs/`.
- **`/api/jobs/{id}` is two disjoint id spaces.** `GET /api/jobs/{job_id}`
  and `…/log` address a `studio.jobs.Job`; `DELETE /api/jobs/{id}` and every
  `POST …/{id}/…` address a queue `Entry`. They are minted by different code
  and never equal - `Entry.job_id`, set when the worker starts the entry, is
  the only link, which is why a row's log link has to be built from it.

Nothing on that screen re-enqueues a row it read, and that is a rule rather
than an omission: a listed row's `params` may carry the literal `"[redacted]"`,
so a control built from what the screen read would write that string into the
plan as if it were the real parameter. `Retry` is not a counter-example - it
sends no params and the server re-queues the entry from the ones it already
holds.

**Four of the five buttons ENQUEUE, and `upload` is the one that does not.**
Transcribe, Detect moments, Render and Apply trim all call `enqueueJob` (`POST
/api/jobs`); the direct routes they used to call (`POST …/streams/{id}/detect`,
`POST …/render`, `POST …/clips/{name}/trim`) are still there, still tested, and
say at each definition that the browser no longer uses them - they are the API
for anything that is not this page. `upload` stays direct because it cannot be
stopped at any level and carries a per-upload confirmation a state file cannot
hold (the same reason `_validate_enqueue` refuses a non-private queued upload).

The consequence is the whole point and the easy thing to get wrong: **a queued
job does not start when the operator clicks.** Every panel that used to flip to
"running" on the click must now be able to say "queued, and here is why it has
not started" - `jobs.ts`'s `waitNote` writes that sentence once. It has grown
past the two original honest reasons that exist on the wire (`worker_running:
false`, which is a dead end, and the entry's own `reason` when another job
holds the event lock, which is normal and temporary): see "A dependent entry
is no longer told a free slot will start it" below for the third, a
dependency branch reachable through the Streams tab's chained "Transcribe +
detect". `hooks/useQueuedJob.ts` is what a panel following a SINGLE entry
uses (Render, Trim, and the stream view's own Transcribe - there is no
stream-view Detect): it polls the PLAN, exposes the entry's state and that
sentence, and only reaches for `GET /api/jobs/{job_id}` once the worker has
claimed the entry and there IS a job. A panel following several entries at
once - the Streams tab's bulk actions - uses `useQueuedEntries` instead (see
"`useQueuedJob` is now `useQueuedEntries` with one id" below); `useQueuedJob`
itself is now a one-id wrapper over it, so the plan-polling and per-entry
state rules (pending/running/outcome, `waitNote`, the `seen` race guard, the
error budget) hold for both - the job fetch does not: `useQueuedEntries`
never fetches a job at all, and only `useQueuedJob`'s own wrapper reaches for
`GET /api/jobs/{job_id}`. A `TrackedEntry` (the Streams tab, the playlist
bulk actions) therefore has no job to build a log link from, and builds it
from `entry.job_id` - but that is NOT why it does so, and reading it as a
consequence would invite a job-holding panel to build its link from the job
instead: `RenderPanel` HAS a job and still calls `jobLogFile(entry)`,
because the two id spaces are disjoint (below) whatever the panel holds. A spinner in that place, or a
notification saying "Render started.", is the lying button this queue exists to
remove. Two rules follow that a rewiring breaks silently: freeze the clip
editor on a RUNNING render only (a queued one has read no `edit.json`, and with
the worker stopped it would freeze forever), and build a log link from
`job_id`, which is null until the claim - so no link at all while an entry
waits. `tests/test_studio_e2e.py`'s `TestTheOtherButtonsGoThroughTheQueueToo`
pins all of it against a studio whose worker is deliberately NOT running -
including the log link once per panel that draws one, because the single
detect case it started as was justified by "that is the one that already had
a link" and that reason expired in the very commit that gave `RenderPanel` one
too.

**And the first of those two had NO test at all for a long time, behind one
that looked like its test.** `TestRenderFreezesEditor`'s stub
(`_stub_render_job`) omitted the `progress` keyword `worker._start_render`
forwards unconditionally, so the `TypeError` was caught by `Worker._start`'s
blanket handler, the entry went queued -> running -> failed in microseconds,
and the stub never ran. `renderWork.running` was therefore never true, and
the test's assertions were satisfied by `renderStarting` alone - the
optimistic flag `handleRender` holds for the duration of the enqueue POST.
Fixing the stub was NECESSARY BUT NOT SUFFICIENT, and that is the part worth
remembering: with a working stub the test still passed under
`disabled={rendering}` alone AND under `disabled={renderStarting}` alone,
because a freeze asserted immediately after the click cannot tell the two
apart. It pins the rule only now that it WAITS for RenderPanel's badge to
read `running` before asserting - measured, `disabled={renderStarting}` alone
then fails 3 of 3. Any future assertion about a running job has the same
shape problem: an optimistic flag makes the click-then-assert version green
either way.

**Every panel that queues work KEEPS its entry, including the longest kind
of all.** The two Transcribe buttons (the event screen's Streams tab and the
stream view's own) enqueued and then threw the entry away, so the kind that
runs for HOURS was the one with no badge, no `waitNote`, no failure surface
and no completion signal - and their notification asserted "It starts as
soon as the worker is free" without ever consulting `worker_running`, which
is the one sentence `waitNote` exists to prevent. Both hold state now, but not
the same shape, because the Streams tab tracks many rows' worth of work and
the stream view tracks exactly one: `App.tsx` owns a
`Record<string, StreamEntryIds>` (one row's transcribe/detect ids each) through
`useQueuedEntries(streamEntryIds)` - the plural hook the playlist bulk actions
also needed (see "`useQueuedJob` is now `useQueuedEntries` with one id"
below) - while `StreamScreen` still holds a single `transcribeEntryId` through
`useQueuedJob`, because it is a route with nothing above it and never more
than one Transcribe in flight. Where that state LIVES follows the screen:
`App.tsx`'s survives switching the navbar's tabs (the same reason the detect
entry is hoisted); `StreamScreen` owns its own.

**A stop is never reported in the failure colour, and the decision is a PURE
FUNCTION for that reason.** `App.tsx`'s detect notification and
`ClipEditor`'s trim notification were red for every outcome that was not
`done`, which relabelled the operator's own "stop this" as a crash at the
last mile of the branch that restates that rule most often. `jobs.ts`'s
`endedNotice(what, kind, outcome, reason)` is where it lives now - title,
message and colour, the colour straight from `stateColor` so a notification
and the Jobs screen's badge for the same entry can never disagree, and the
message saying what a stop KEPT (the same facts `stopWarning` promised
before the click) rather than pointing at a log for a cause that does not
exist. In a component it would be untestable: this project covers component
behaviour by E2E, not by component tests, so three copies of a colour
ternary were exactly the shape nothing could catch. Its four cases are
unit-tested in `jobs.test.ts`.

**And the same honesty must survive the entry going away.** `useQueuedJob`
stops for three reasons, and only ONE of them is the entry finishing: it also
stops when the row leaves the plan and when it gives up reading the plan at
all. Both of those used to leave `pending` true for the life of the screen -
the panel's controls disabled, still saying work was queued, recoverable only
by a page reload. It is reached by an ordinary supported flow, not a corner
case: `allowedActions` offers `remove` on a `queued` entry, so "queue a trim,
change your mind on the Jobs screen, remove it" was enough. A control disabled
for good while the panel claims work is queued is the same lie as a button
that claims to have started something, pointed the other way. So `error` now
carries WHICH of the two happened, and `pending`/`running` are both false
whenever it is set - the rule `useJobPolling` already states in its own
docstring and implements by synthesising a `failed` job. The two cases are not
interchangeable: a removed entry CLEARS `entry` (a badge still reading
"queued" for a row that does not exist is a lie), a failed read KEEPS it
(nothing was learned about whether it is still there). All three panels are
pinned removing their own entry, and both branches are unit-tested in
`hooks/useQueuedJob.test.tsx` - which exists because this hook carried the
`seen` race guard, the terminal stop and the error budget with no unit test
at all while its simpler sibling had one.

An aside worth keeping, because it is what a browser probe of this bug looks
like: pre-fix, the SAME hook could leave one panel wedged and another
perfectly usable, decided by a 750 ms race and nothing else. A row removed
AFTER the first poll saw it wedges (`seen.current` is true, so polling stops
with the stale pending row in hand); a row removed BEFORE that first poll does
not (`seen` is false, the hook keeps polling, `entry` stays null and the panel
reports nothing). Measured both ways - all three panels wedge identically
under the after-seen case. There is no per-panel difference to look for.

That is also why the E2E harness starts a worker where nothing else does. Its
`live_studio` fixture gives each test its own `jobs.json` and calls
`worker.start()`, which leaves the "`create_app` starts nothing" property
untouched and is safe only because `tests/conftest.py`'s autouse
`_no_real_job_starter` makes a real starter unreachable without an explicit
opt-in. Its teardown stops BOTH the worker it started and
`app.state.worker` - a workspace SWITCH rebuilds the queue and its worker
(`_build_queue_and_worker`) and starts the NEW one when the old was running, so
stopping only the fixture's own leaked the replacement, which then failed
`TestCreateAppWiring`'s process-wide thread enumeration hundreds of tests later
in another file.

**A dependent entry is no longer told a free slot will start it.**
`Entry.after` has existed since the queue shipped, but no browser call site
ever SENT one - `JobsScreen` merely displayed it - so `waitNote` had no
branch for it: a `detect` waiting on a RUNNING transcription has nothing
queued in front of it, so `ahead` was 0 and it answered "It is next in
line, and starts as soon as the worker has a free slot", which is false in
the one way that matters. The Streams tab's "Transcribe + detect" is what
made that reachable, so the branch landed with it. A dependency the plan no
longer holds stays quiet: `_trim_finished` ages a long-since-done one out
and `_dependency_status` treats absence as SATISFIED, so saying otherwise
would contradict the queue.

**The failed-dependency half of that branch itself disagreed with the
queue, for a dependency state this build does not know.** `jobs.ts`'s
`activity()` answers `'terminal'` for a `JobState` this build has never
heard of - the safe default for a panel merely FOLLOWING that row, so it
stops treating it as in-flight - but `job_queue._dependency_status` answers
`"waiting"` for the identical state: the Python side only recognises
`done`/`failed`/`stopped`/`interrupted` as settled, and treats anything else
as "not yet", the same as an ordinary `queued`/`running` dependency. The
branch used to test `dependency.state !== 'done'`, which agreed with
`activity()`'s optimistic terminal-by-default and disagreed with the queue
itself - it told the operator the dependent "will be failed on the worker's
next pass" for a dependency `claim_next` will in fact hold `queued`
indefinitely. Reachable only from a `jobs.json` another build wrote (a
downgrade, or a future state this build predates) - a provenance this
repo already designs for elsewhere (see `job_queue.py`'s own "An entry
whose KIND this build does not know" below). The branch now fires only for
a dependency state this file can actually NAME as a dead end
(`failed`/`stopped`/`interrupted`), pinned in `jobs.test.ts`.

**The chained bulk action needed no new queue mechanism, and an early
design of it wanted one.** The first draft added a `defer` reason to
`Worker._blocked_by` for a detect whose transcript was missing. `after` is
strictly better and already there: it cannot wait forever, and a dependent
whose dependency ends without succeeding is FAILED with a reason naming it.
Do not add the deferral rule back.

**A bulk action's rules are `streams.ts`, and its skips are not tidiness -
but the two things they guard against are not what an earlier version of
this file claimed, and both were corrected after review measured the real
mechanism rather than assuming it.** `bulkPlan` skips a stream that already
has a transcript, and one that already has an analysis, unless the operator
ticks the matching "anyway" box. Neither skip exists because a re-run is
expensive by nature - for a stream that already has one, both engines are
built to make a plain re-run cheap, and the skip is protection against
paying for that ordinary case thirteen times over on one bulk click, not
against something that is always costly:
- **Re-transcribing.** `stream_transcribe.ytdlp_downloader` asks yt-dlp for
  the stream's true duration (metadata only) and probes any existing
  `streams/<video_id>/audio.*` with ffprobe; when the two agree - the
  ordinary case for a stream that already has a transcript, since that
  transcript could not exist without that audio and its decoded chunks
  already on disk - it reuses the file and downloads nothing, and every
  chunk that decoded cleanly then comes from the `chunks/` cache; a chunk
  recorded in `missing_chunks` was never cached and is re-decoded on every
  run, which is itself one reason to re-transcribe. So the ordinary re-run
  is a metadata call, an ffprobe and a re-assembly, not a download. It
  genuinely costs a re-download and hours of decode when the workspace's own
  `audio.*` or `chunks/` were cleared, and also whenever
  `ytdlp_downloader`'s own local-file check falls through to a real
  download - no local `audio.*` file, a short/partial one, or an ffprobe
  that cannot read it (missing, corrupt file, unparseable output). What a
  re-transcription always does, cheap or not, is rewrite `transcript.json` -
  which is exactly what an operator wants after a glossary edit, and the
  reason bulk-ticking several rows for that purpose is still worth a
  warning before the click.
- **Re-detecting.** `moment_scan.scan`'s `window_cache` hit skips the model
  call entirely, and `detect.WindowCache` (`streams/<video_id>/windows/`) is
  disk-backed and never cleared after a successful window scan - see
  "Stopping a detection costs nothing" below for the same cache from the
  other direction. So a plain re-detect of a stream already analysed BY A
  MODEL costs nothing at the provider beyond the fingerprint-miss cases
  below. **It is not free when no windows were ever cached for that stream
  at all**, which is not a corner case: `detect.py` constructs a
  `WindowCache` only in its model branch (`if caller and words:`), so an
  analysis produced by the LEXICON fallback - the ordinary state of any
  workspace that had no API key configured when detection ran, which this
  file documents at length - writes no windows, and the first model-backed
  detect over that stream then pays for the whole thing. The same is true
  of a cleared `windows/`, and of any window that failed mid-scan: a failed
  window is deliberately never cached (see `moment_scan.scan`'s own
  docstring), so it is re-attempted, at full cost, on every later run.
  Short of those, a re-detect spends money only when the fingerprint
  (engine, above-zero markers, transcript words) misses - a changed
  provider or model, a marker crossing zero, or a re-decoded transcript
  (see the glossary-edit case above) - which is precisely the case an
  operator forces on purpose.

The bar says what will be skipped BEFORE the click, and disables the button
when nothing is left - a control that clicks into silence is the same lie
as a spinner that never moves. **A per-ROW Transcribe/Detect click is
deliberately exempt from that skip and always forces the work**
(`StreamPanel.tsx`'s `ROW_FORCE`) - the skip is the bulk bar's protection
against a click over many rows, not a rule that should also silently
swallow one explicit, single-row request; an earlier version made the row
buttons skip too, and a click that did nothing was a real defect found in
review.

**The bar's own line shows BOTH halves of what will happen, not one or the
other.** The design's own example is "3 selected · 2 transcriptions
skipped: already transcribed · 1 will be queued" - the count of what WILL
be queued, alongside the count of what is skipped and why. An earlier
version rendered `planned.note ?? \`${n} will be queued\`` - the note when
one existed, the count only otherwise - so the count vanished exactly when
part of the selection was skipped, which is the one case an operator
cannot infer it from the disabled/enabled state of the button alone (a
partial skip still leaves the button enabled). Both are shown together now
whenever `planned.note` is non-null.

**The bar's three buttons stay live during their own batch, and that was a
real double-queue hazard, not a cosmetic gap.** `handleQueueStreams` is an
async loop of sequential POSTs (see "The chain breaks PER VIDEO" just
below); the row buttons were already gated per leg on `busyLegs`
(`queueingLegs` in `App.tsx`), but the bar's three were gated on
`planned.steps.length === 0` alone. A second click on the same button, or a
different bar button, during that window recomputed the identical plan and
queued the WHOLE BATCH AGAIN - N duplicate multi-hour transcriptions or N
duplicate paid detections - and invisibly so, because `setStreamEntries`'s
per-video merge let the second batch's entry id silently overwrite the
first's, leaving the first entry with no badge, no `waitNote` and no
finish notification. All three bar buttons are now also disabled (and
relabelled "Queuing…", not a silent disable - a control that clicks into
silence is the lie this whole tab exists to avoid) whenever `busyLegs` is
non-empty, from ANY origin, not only this bar's own batch. **This closes
the sibling finding that `queueingLegs` is a plain `Set` with no
refcount, which would otherwise let two concurrent batches touching the
same leg un-busy each other's slot early:** with the bar itself blocked
while any enqueue is in flight, two concurrent batches touching the same
leg are no longer reachable at all, so there is nothing left for a
refcount to protect against. Do not "simplify" this guard away on the
reasoning that a `Set` looks too primitive for the job - removing it
reopens exactly the hazard above, silently.

**A bulk action's own "N queued" count could overstate what actually got
queued.** `handleQueueStreams` always wrote `created[step.videoId] = ids`
for a video it attempted, even when `ids` ended up `{}` - the case where a
video's only requested leg was `detect` and that POST threw (`transcribe`
was never requested, so there was no earlier `continue` to skip the
assignment). Counting `Object.keys(created).length` therefore counted that
video as queued when nothing was queued for it at all. The count is now
built from videos that actually got AN entry id (`ids.transcribe !==
undefined || ids.detect !== undefined`), not from every video the loop
merely visited.

**"Select all shown" used to destroy every off-view pick, which
contradicted the very reason selection survives a filter change.**
Selection is by video id and deliberately SURVIVES switching the playlist
filter (see `selectionNote`'s own docstring: a race weekend split across
two playlists is the ordinary case) - but the button wrote
`setSelected(visibleIds)`, replacing the whole selection with only what the
CURRENT filter shows. It now ADDS the visible ids to whatever was already
selected. The button only ever renders once something is already selected
(the bar itself is conditional on `selected.length > 0`), so it still
cannot START a selection on its own - that is unchanged and is not a defect,
just a fact worth stating so a future change does not "fix" it into
something the button was never meant to do.

The chain breaks PER VIDEO, not per batch: if a video's `transcribe` POST is
refused, its `detect` is not enqueued at all, because a detect without its
`after` would quietly run on an untranscribed stream and fail with
`TranscriptNotCached` - which reads as a bug in detection rather than as the
refused request it is. Entries are enqueued SEQUENTIALLY in catalogue order;
parallel requests would scramble the queue's order away from the list the
operator was looking at.

**The bar's three buttons deliberately share no substring with a row's
two.** "Queue transcription for selected", "Queue detection for selected"
and "Queue transcription and detection for selected" name nothing a row's
"Transcribe"/"Detect moments" also names, because Playwright's
`get_by_role(name=...)` matches a name as a SUBSTRING by default - a bar
label containing a row label would make every non-exact lookup in the E2E
suite ambiguous the moment a single row was ticked next to the bar. Worth
recording on its own: it is the kind of thing the next person re-discovers
the hard way rather than reads.

**`useQueuedJob` is now `useQueuedEntries` with one id.** The plan is a
single `GET /api/jobs`, so a bulk action followed by thirteen copies of the
old hook would have fetched it thirteen times a second - and a second hook
beside it would have been a second copy of the `seen` race guard, the error
budget, and the rule that `error` set means `pending` and `running` are both
false. `useQueuedJob.test.tsx` passes unchanged across that move, which is
what makes it a refactor rather than a rewrite - `useQueuedJob` itself
survives as a one-id wrapper over `useQueuedEntries` (see
`hooks/useQueuedJob.ts`), so every panel that only ever followed a single
entry keeps its existing shape. `useQueuedEntries` itself keeps per-id state
across a change to the tracked id SET and drops only the ids that left it -
a full reset used to make an aged-out id poll forever and every still-tracked
row flicker idle for a tick. **That fix kept the STATE but not the
CONCLUSION drawn from it, and cost a second round.** The poll loop's own
`settled` set - which ids this run of the effect has already decided not to
re-check - was still rebuilt empty on every id-set change, so an id whose
row was already terminal got put back up for re-interrogation on the very
next id-set change anyway; for one that had by then aged out of `finished`
(`_trim_finished`'s 50-row cap), that re-check reported it GONE - false for
an entry that genuinely ran for hours and finished, on the exact workflow
this feature exists for (three 13-row "both" batches is 78 terminal
entries). `settled` is now seeded from each id's last known row (via
`rowsRef`, mirrored on every render the same way `latest` mirrors `ids`)
whenever that row already reads terminal; an id last known ACTIVE is
deliberately NOT seeded this way, because if IT vanishes on the next poll
that is a genuine removal and must still be reported as one.
`useQueuedEntries.test.tsx` pins both: an id seen `done` then aged out on a
LATER id-set change keeps its outcome and reports no error, right beside
the pre-existing "removed while queued reports GONE" case, so the two
cannot collapse into one behaviour again. `jobs.ts`'s `batchNotice` is the same argument
one layer up: one notification per batch. For a batch of ONE entry it
delegates to `endedNotice` for every outcome except `done` (a batch of one
that succeeded gets its own shape - `endedNotice` is built for the not-done
cases, and pointing a successful job at a log for "details of nothing" would
be exactly the kind of small lie this module exists to avoid); a
multi-entry batch never delegates, and builds its own summary line instead.
So a single-row action cannot drift from a bulk one, and never reporting a
stop in the failure colour.

**A deviation from the design document, recorded rather than hidden.** The
design says `copy` is `io` and "runs outside both pools, unbounded as today".
`copy` is NOT queueable: `start_copy_job` takes an `on_done` callback that
re-roots the LIVE app onto the copy, and no state file can hold a callback -
the same reason `connect` is not queueable. So it keeps its own route
(`POST /api/workspaces/copy`), and the consequence is that the `io` pool is
unreachable through the queue in production. The unlimited-pool RULE is
unchanged and still pinned (`test_io_runs_outside_both_pools` builds its own
kinds table to do it); what is gone is the shipped kind that exercised it.
`api._queue_pools` derives the settable pools from the QUEUEABLE kinds for the
same reason - offering a limit for `io` would be a control that does nothing.
The design document carries this as a dated amendment; the two must not be
left disagreeing.
