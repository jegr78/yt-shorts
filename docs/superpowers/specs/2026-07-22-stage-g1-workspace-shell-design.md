# Stage G1 — Workspace shell and navigation

**Date:** 2026-07-22
**Scope:** turn the studio from a single-event editor into a workspace app you can
launch without naming an event. `bin/yt-shorts studio` (no argument) opens a start
screen listing the workspace's channels; opening a channel lists its events;
opening an event is the existing editor, unchanged. Read-only navigation only —
creating, editing, and deleting channels/events are later stages (G2/G3), and the
studio still writes `edit.json` and nothing else in G1.

This is the first of a staged program (roadmap at the end): G1 shell/navigation,
G2 event CRUD, G3 channel CRUD (with a full brand editor), G4 a settings page.

## Problem

`bin/yt-shorts studio <channel>/<event>` is the only way in, and the app is bound
to exactly that one event: `create_app(profile)` closes over a single `Profile`,
and every route (`/api/clips`, `/api/render`, `/api/auth`, …) operates on
`profile.event_dir`/`profile.channel`. There is no way to see what channels or
events exist, or to move between them, without stopping the server and relaunching
with a different identifier. An operator working across their own, managed, and
brand channels — each with several events — has no home screen.

## Why path-scoped routes, not a session-active profile

Two ways to make the app workspace-aware:

1. **Path-scoped** — the app is built for the *workspace*, and the event routes
   carry the channel/event in their path (`…/{channel}/events/{event}/clips`). A
   request resolves its own profile from the path.
2. **Session-active profile** — the app keeps a mutable "current profile" a
   `POST /api/select` sets, and the existing routes use it.

G1 takes **path-scoped**. The active-profile approach is less code to move but is
stateful in a way that fights the tool: two browser tabs (a real thing when
comparing two events) would share and clobber one "current" selection, and the
"one process, one event" wart the current docstring calls out would harden instead
of going away. Path-scoped routes are stateless — each request names what it acts
on — which is also the right foundation for G2/G3, where you edit one channel while
looking at another. The cost is real: every existing route and every frontend
fetch gets re-scoped. That refactor is the bulk of G1, and it changes URLs and
wiring, not editor behaviour — the existing E2E suite is the backstop that the
editor still works.

## Architecture

### Backend

`create_app()` no longer takes a `Profile`. It builds a workspace-level app over
the resolved workspace (`workspace.resolve()`), and profiles are loaded per
request from path parameters.

New read-only routes:

- `GET /api/channels` → the workspace's channels: for each `channels/<name>/` with
  a readable `channel.json`, its `name`, `display_name`, `handle`, and event count.
  A channel whose `channel.json` is missing or malformed is listed with an error
  marker rather than omitted, so an operator can see it needs fixing.
- `GET /api/channels/{channel}/events` → that channel's events: for each
  `events/<name>/`, its `name` and a small summary (clip count, how many are kept,
  how many have a rendered short) so the list is useful at a glance. Cheap counts
  only — no per-clip loading.

Existing event-scoped routes move under `…/{channel}/events/{event}/`:

- `/api/clips` → `/api/channels/{channel}/events/{event}/clips`
- `/api/clips/{name}`, `.../preview`, `.../short`, `.../upload`, `.../upload-preview`
- `/api/render`, `/api/jobs/{id}`, `/api/streams`, `/api/streams/{video_id}/detect`

A single helper resolves and validates the profile from the path
(`_load_profile(channel, event)`), returning 404 for an unknown channel/event and
422-style profile errors for a malformed one, so every scoped route shares one
consistent failure surface. The route bodies are otherwise unchanged — they act on
the resolved profile exactly as they act on the closure profile today.

**`/api/auth` and `/api/auth/connect` stay channel-scoped, not event-scoped**:
authorization is per channel (per YouTube channel id), so these become
`GET /api/channels/{channel}/auth` and `POST /api/channels/{channel}/auth/connect`.
They resolve the channel (not a full event) to read its `channel.json` id.

The job store (`app.state.job_store`) stays app-level and keyed by job id, so a
render/detect/upload/connect job started under any event is pollable at
`/api/jobs/{id}` regardless of which event the UI is currently viewing. The
`EventLock` still guards per-event operations by `event_dir`, unchanged.

### Frontend

The single-page editor becomes a small client-side router with three screens:

- **`/`** — the channel list (from `GET /api/channels`): each channel a row with
  its display name, handle, and event count; a channel with a config error shows
  the error and is not openable. Selecting a channel routes to its events.
- **`/{channel}`** — the event list (from `GET /api/channels/{channel}/events`):
  each event a row with its clip/kept/rendered counts; selecting one routes to the
  editor. A breadcrumb back to the channel list.
- **`/{channel}/{event}`** — the existing editor (clip list, editor, streams,
  upload, auth bar), unchanged in behaviour, but every fetch now targets the
  scoped URLs. A breadcrumb back to the event list and channel list.

Routing is client-side over the built static bundle. For a deep link or a reload
on `/{channel}/{event}` to land on the right screen, the FastAPI server must serve
`index.html` for any non-`/api` path — an **SPA fallback that does not exist today**
(the studio is currently a single page) and that G1 adds, without shadowing the
`/api` routes or the built asset paths. No new heavyweight dependency if a small
router suffices; the choice (a tiny hand-rolled router vs. a library) is the plan's
to make against the bundle already shipped.

### CLI

- `bin/yt-shorts studio` (no identifier) launches the workspace app at the start
  screen. The argument count check is relaxed for `studio` specifically.
- `bin/yt-shorts studio <channel>/<event>` still works and **deep-links straight
  into that event's editor** (the router opens at `/{channel}/{event}`), so the
  existing muscle-memory and any scripts keep working.
- A `studio <channel>` form (channel only) opens at that channel's event list.

## What stays the same

- **The studio still writes `edit.json` and nothing else in G1.** No create, edit,
  or delete of channels/events — that boundary expansion is G2/G3, deliberately not
  here. Every G1 route is a read or an existing editor action.
- Editor behaviour, rendering, subtitles, moment detection, upload — all unchanged;
  only their URLs move.
- FastAPI stays optional to the CLI's non-studio commands; the studio package stays
  the only FastAPI importer.
- The built `static/` stays committed; `node_modules` stays untracked; Vitest and
  the Playwright E2E stay the frontend's tests.

## Testing

- `list_channels`/`list_events` (or the routes over them) against a workspace
  fixture with two channels and several events: names, counts, and the
  malformed-channel-listed-with-an-error case, all against
  `tests/fixtures/channels` (no dependency on `~/YT-Shorts-Data`).
- The scoped routes: one representative existing route (e.g. clips) proven to work
  under `/api/channels/{channel}/events/{event}/clips`, plus 404 for an unknown
  channel/event and the malformed-profile failure.
- The path-scoped `_load_profile` helper: resolves a valid channel/event, 404s an
  unknown one, surfaces a profile error understandably.
- **The existing E2E suite is re-pointed, not weakened:** every studio E2E now
  navigates from a seeded start screen (or deep-links) to the editor; selectors are
  updated for the new markup, assertions unchanged. New E2E: the start screen lists
  seeded channels, opening one lists its events, opening one shows the editor; a
  reload/deep-link on `/{channel}/{event}` lands in the editor.
- Vitest for any new pure client logic (a route/URL builder for the scoped paths is
  the likely candidate — test it, since a wrong scoped URL would 404 silently).

## Migration and compatibility

- No data migration: channels and events on disk are unchanged; G1 only adds ways
  to see and navigate them.
- The re-scoping is a breaking change to the studio's own HTTP API, which is
  internal (only this repo's frontend calls it), so it is renamed in place with the
  frontend updated in lockstep — there is no external consumer to keep the old
  paths for.

## Not in scope (later stages)

- **G2** — create/edit/delete events (expands the write boundary; destructive
  delete needs confirmation and `EventLock`).
- **G3** — create/edit/delete channels, including a full brand editor (colors,
  font upload, output dimensions) and channel identity (`channel.json`).
- **G4** — a settings page: OAuth-account overview across channels, per-channel
  brand, moment-detection defaults, and read-only workspace info.
- Any writing of channel/brand config or directory creation/deletion — none in G1.

## Roadmap (the staged program this begins)

1. **G1 (this)** — workspace shell + navigation, read-only.
2. **G2** — event CRUD.
3. **G3** — channel CRUD + brand editor.
4. **G4** — settings page (OAuth overview, brand-per-channel, detection defaults,
   workspace info).

Each stage is its own spec → plan → implementation, mergeable on its own.
