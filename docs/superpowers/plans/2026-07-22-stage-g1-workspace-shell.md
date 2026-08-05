# Stage G1 — Workspace shell and navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

> **Historical note (added after stage G4 / the stream-view branch):** every "three screens"/"three-screen router" mention below is an accurate record of what THIS stage delivered - channels, events, the editor - and is left as written rather than silently rewritten to match later stages. The router has since grown: G4 added Settings and Logs (five screens), and the stream-view branch added the stream detail screen (six). See CLAUDE.md's studio section for the current count and table.

**Goal:** Launch the studio without naming an event: `bin/yt-shorts studio` opens a start screen of channels → events → the existing editor. Read-only navigation only; the studio still writes `edit.json` and nothing else.

**Architecture:** `create_app()` becomes workspace-level (no bound `Profile`). Event routes move under `/api/channels/{channel}/events/{event}/…` and resolve their profile from the path; auth routes move under `/api/channels/{channel}/auth`. Two listing routes and an SPA fallback are added. The frontend becomes a three-screen client-side router over the same bundle. See the G1 design spec.

**Tech Stack:** FastAPI (existing), React + Vite + Mantine (existing), Vitest + Playwright (existing). A small client-side router (hand-rolled or a light library — Task 4 decides).

## Global Constraints

- `PYTHONPATH=src` mandatory. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` — 611 tests pass at the start of this plan.
- **Read-only in G1.** No create/edit/delete of channels or events. The studio writes `edit.json` and nothing else; every G1 route is a read or an existing editor action whose behaviour is unchanged — only its URL moves.
- **No editor behaviour change.** The re-scoping changes URLs and wiring, not what the editor does. The existing Playwright E2E is the backstop: re-point selectors/URLs, never weaken assertions.
- FastAPI stays optional to the CLI's non-studio commands; `yt_shorts/studio/` stays the only FastAPI importer. The listing functions (Task 1) must not import FastAPI.
- Tests must not depend on `~/YT-Shorts-Data`; `tests/conftest.py` pins `profile.CHANNELS_DIR` to `tests/fixtures/channels`. New listing/nav tests use that fixture.
- Built `static/` stays committed; `node_modules` untracked. English only. Imperative commit messages.

---

## Task 1: Workspace listing (pure, no FastAPI)

**Files:** create `src/yt_shorts/workspace_listing.py`, `tests/test_workspace_listing.py`; add a second channel to `tests/fixtures/channels/` if only `erf` exists.

**Interfaces:**
- Produces:
  - `workspace_listing.ChannelInfo` — dataclass `name: str`, `display_name: str`, `handle: str`, `event_count: int`, `error: str | None`
  - `workspace_listing.EventInfo` — dataclass `name: str`, `clip_count: int`, `kept_count: int`, `rendered_count: int`
  - `list_channels(channels_dir) -> list[ChannelInfo]` — one per `channels/<name>/`; a missing/malformed `channel.json` yields an entry with `error` set and empty identity fields, not an omission
  - `list_events(channels_dir, channel) -> list[EventInfo]` — one per `channels/<channel>/events/<name>/`, with cheap counts

- [ ] **Step 1: Write the failing test** (`tests/test_workspace_listing.py`): a temp channels dir with two channels — one valid (`channel.json` with display_name/handle + two event dirs), one with a broken `channel.json` — asserts `list_channels` returns both, the broken one carrying `error` and the valid one `event_count == 2`; `list_events` returns the event names with clip/kept/rendered counts (seed clips with `clipstore.write_clip` + an `editorial.save(status=kept)` + a `short.mp4`). Read `clipstore`/`editorial` helpers the rest of the suite uses; count with `clipstore.iter_clip_dirs`.

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `workspace_listing.py`:

```python
"""List a workspace's channels and a channel's events for the studio's start
screen (see the stage G1 design). Pure filesystem reads over the resolved
workspace's channels dir - no FastAPI, no profile loading, cheap counts only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import clipstore, editorial


@dataclass
class ChannelInfo:
    name: str
    display_name: str
    handle: str
    event_count: int
    error: str | None = None


@dataclass
class EventInfo:
    name: str
    clip_count: int
    kept_count: int
    rendered_count: int


def list_channels(channels_dir) -> list[ChannelInfo]:
    root = Path(channels_dir)
    if not root.is_dir():
        return []
    out: list[ChannelInfo] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        events_dir = entry / "events"
        event_count = sum(1 for e in events_dir.iterdir() if e.is_dir()) \
            if events_dir.is_dir() else 0
        try:
            data = json.loads((entry / "channel.json").read_text(encoding="utf-8"))
            out.append(ChannelInfo(
                name=entry.name,
                display_name=data.get("display_name", ""),
                handle=data.get("handle", ""),
                event_count=event_count))
        except (OSError, json.JSONDecodeError) as error:
            out.append(ChannelInfo(name=entry.name, display_name="", handle="",
                                   event_count=event_count,
                                   error=f"channel.json unreadable: {error}"))
    return out


def list_events(channels_dir, channel) -> list[EventInfo]:
    events_dir = Path(channels_dir) / channel / "events"
    if not events_dir.is_dir():
        return []
    out: list[EventInfo] = []
    for entry in sorted(p for p in events_dir.iterdir() if p.is_dir()):
        clip_count = kept_count = rendered_count = 0
        for directory in clipstore.iter_clip_dirs(entry):
            clip_count += 1
            try:
                if editorial.load(directory).status == editorial.KEPT:
                    kept_count += 1
            except editorial.EditError:
                pass
            if clipstore.short_path(directory).exists():
                rendered_count += 1
        out.append(EventInfo(name=entry.name, clip_count=clip_count,
                            kept_count=kept_count, rendered_count=rendered_count))
    return out
```

- [ ] **Step 4: Run tests + full suite.** Confirm no FastAPI import: `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.workspace_listing; assert 'fastapi' not in sys.modules; print('clean')"`.
- [ ] **Step 5: Commit** — `List a workspace's channels and a channel's events`.

---

## Task 2: Re-scope the studio app to the workspace

**Files:** `src/yt_shorts/studio/api.py`; `tests/test_studio_api.py`, `tests/test_studio_jobs.py` (update fixtures/URLs).

This is the core refactor. `create_app()` stops taking a `Profile`; routes resolve their profile from the path.

**Interfaces:**
- Produces:
  - `create_app() -> FastAPI` (no argument) — workspace-level
  - `GET /api/channels` → `[{name, display_name, handle, event_count, error}]` (from `list_channels`)
  - `GET /api/channels/{channel}/events` → `[{name, clip_count, kept_count, rendered_count}]` (from `list_events`)
  - every existing event route moved under `/api/channels/{channel}/events/{event}/…`
  - auth routes moved under `/api/channels/{channel}/auth` and `…/auth/connect`
  - an SPA fallback serving `index.html` for non-`/api`, non-asset paths

- [ ] **Step 1: Write the failing tests.** Update `tests/test_studio_api.py`'s `client` fixture to build `create_app()` (no profile) and drive scoped URLs. Add:
  - `GET /api/channels` lists the ERF fixture channel.
  - `GET /api/channels/erf/events` lists its events with counts.
  - a representative scoped route works: `GET /api/channels/erf/events/{event}/clips` returns the seeded clips (seed under the ERF fixture's event, or a tmp event — match how the current fixture seeds).
  - `404` for `/api/channels/nope/events/x/clips` (unknown channel) and for an unknown event.
  - the SPA fallback: `GET /somewhere/deep` returns the `index.html` (200, HTML), while `GET /api/does-not-exist` still 404s (the fallback must not shadow `/api`).

  The existing per-route tests (clips, render, preview, upload, streams, detect, auth, connect, upload-preview, window) must be updated to the new scoped URLs — mechanical, one prefix change each. Keep every assertion; only the URL changes.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement.** In `create_app()`:
  - Drop the `profile` parameter. Resolve the workspace once: `workspace = _resolve_workspace()`; `channels_dir = workspace.channels_dir`.
  - Add a path-scoped loader (module-level or closure), the single failure surface for every scoped route:

```python
    def _load_profile(channel: str, event: str) -> Profile:
        from ..profile import ProfileError
        from ..profile import load as profile_load
        try:
            return profile_load(f"{channel}/{event}")
        except ProfileError as error:
            # An unknown channel/event and a malformed profile both surface here.
            raise HTTPException(status_code=404, detail=str(error))
```

  - The existing inner helpers (`_load_clip_or_404`, `_load_edit_or_500`, `_preview_response`, `_summary` calls, the streams cache, the job store) currently close over `profile`. Change them to take a `profile` argument (or an `event_dir`/`config`) so each scoped route resolves its profile first and passes it in. Keep `app.state.job_store` app-level (unchanged) — a job id is global.
  - Convert each route. Pattern (shown for clips; apply the same shape to all):

```python
    @app.get("/api/channels/{channel}/events/{event}/clips")
    def list_clips(channel: str, event: str) -> list[dict]:
        profile = _load_profile(channel, event)
        out = []
        for directory in clipstore.iter_clip_dirs(profile.event_dir):
            ...
        return out
```

  Apply to every route in this list, keeping the trailing shape identical to today, only adding `{channel}/{event}` params + the `_load_profile` call:
  - `…/events/{event}/clips` (GET), `…/clips/{name}` (GET, PATCH), `…/clips/{name}/preview` (GET, POST), `…/clips/{name}/short` (GET), `…/clips/{name}/upload` (POST), `…/clips/{name}/upload-preview` (GET), `…/render` (POST), `…/streams` (GET), `…/streams/{video_id}/detect` (POST).
  - Auth, channel-scoped (resolve the channel, not a full event — read `channel.json` for the id):

```python
    def _load_channel(channel: str) -> dict:
        import json
        path = channels_dir / channel / "channel.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/channels/{channel}/auth")
    def get_auth(channel: str) -> dict:
        channel_json = _load_channel(channel)
        channel_id = channel_json["id"]
        ...   # body unchanged from today, using this channel_id
```

    and `POST /api/channels/{channel}/auth/connect` the same way.

  - The two listing routes:

```python
    @app.get("/api/channels")
    def get_channels() -> list[dict]:
        return [vars(c) for c in list_channels(channels_dir)]

    @app.get("/api/channels/{channel}/events")
    def get_events(channel: str) -> list[dict]:
        return [vars(e) for e in list_events(channels_dir, channel)]
```

  - **SPA fallback**, registered before the `StaticFiles` mount so `/api` and assets win, serving `index.html` for any other path:

```python
    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        asset = STATIC_DIR / full_path
        if full_path and asset.is_file():
            return FileResponse(str(asset))
        return FileResponse(str(STATIC_DIR / "index.html"))
```

    Confirm this coexists with the existing `StaticFiles` mount (the catch-all may replace `html=True`'s root-serving — keep exactly one path that serves `index.html` at `/`, and verify the built asset URLs still resolve).

  - Import `list_channels`, `list_events` from `..workspace_listing`.

- [ ] **Step 4: Run the studio tests, then the full suite.** Expect the new + updated tests green.
- [ ] **Step 5: Confirm the app still imports no google at module scope** (the auth import chain is unchanged): the check from stage E still passes.
- [ ] **Step 6: Commit** — `Scope the studio app to the workspace with per-path profiles`.

---

## Task 3: Launch the studio without an event

**Files:** `bin/yt-shorts`; a CLI test in `tests/test_cli.py`.

**Interfaces:**
- `bin/yt-shorts studio` (no identifier) → workspace app at the start screen
- `bin/yt-shorts studio <channel>` → opens at that channel's event list
- `bin/yt-shorts studio <channel>/<event>` → deep-links into the editor (unchanged entry, now via the router)

- [ ] **Step 1: Write the failing test.** In `tests/test_cli.py`, a test that `cmd_studio` builds the workspace app via `create_app()` and that the app carries the new routes (assert `"/api/channels" in {r.path for r in app.routes}`), mirroring the existing `cmd_studio` test that checks `/api/clips`. Since `create_app()` no longer takes a profile, `cmd_studio` changes shape — the test drives the new signature.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement.**
  - `cmd_studio` no longer needs a `profile`: it builds `create_app()` and runs uvicorn, printing the URL. It optionally takes the identifier only to print a friendlier "opening at …" line; the router handles the actual screen from the URL path the operator opens (or we can print `http://host:port/#/{identifier}` / `/{identifier}` as a convenience deep link).
  - Relax `__main__`'s arg parsing so `studio` accepts 0 or 1 positional identifier while every other command still requires exactly one. Concretely: special-case `studio` before the generic `len(sys.argv) != 3` check — `studio` with no identifier launches the workspace; `studio <identifier>` deep-links.
  - Keep the FastAPI-missing `ImportError` message path exactly as today.

- [ ] **Step 4: Run the CLI test + full suite.**
- [ ] **Step 5: Commit** — `Launch the studio without naming an event`.

---

## Task 4: The frontend router (three screens)

**Files:** `src/yt_shorts/studio/web/` (rebuild `static/`), `tests/test_studio_e2e.py`.

Frontend task, dispatched to a focused agent with the Task-2 API contract. Neutral timing-tower look (studio redesign brief). This is where the single-page editor becomes a router.

**What to build:**
- A client-side router with three screens: `/` channel list (`GET /api/channels`), `/{channel}` event list (`GET /api/channels/{channel}/events`), `/{channel}/{event}` the existing editor — unchanged in behaviour, but every fetch in `api.ts` now targets the scoped `/api/channels/{channel}/events/{event}/…` (and auth `/api/channels/{channel}/auth…`) URLs. Thread the current `{channel, event}` (and `{channel}` for auth) through the api layer; a small scoped-URL builder is the natural place — unit-test it with Vitest (a wrong scoped URL 404s silently, exactly what a unit test should catch).
- Channel rows: display name, handle, event count; a channel with a config `error` shows it and is not openable. Event rows: clip/kept/rendered counts. Breadcrumbs back up. Loading/empty states that say what to do.
- Deep-link/reload on any of the three URLs lands on the right screen (the SPA fallback from Task 2 serves `index.html`; the router reads the path).

**Acceptance (E2E, real Chromium):** re-point every existing studio E2E to navigate from the seeded start screen (or deep-link to `/{channel}/{event}`) — update selectors/URLs, keep assertions. Add: the start screen lists the seeded ERF fixture channel; opening it lists its events; opening an event shows the clip list; a reload on the event URL stays in the editor. Reuse the Python seeding the E2E already does.

**Verify:** `npm run build` typechecks; `npm test` (Vitest) green incl. the scoped-URL builder; `static/` rebuilt and committed; full `pytest` green, E2E included. Drive the real page, screenshot the three screens, look before reporting.

- [ ] Build to the above; rebuild+commit `static/`; commit — `Add the workspace navigation shell to the studio`.

---

## Task 5: Documentation

**Files:** `README.md`, `CLAUDE.md`, `src/yt_shorts/studio/web/README.md`.

- [ ] **README** — the studio now launches with `bin/yt-shorts studio` (no event) to a start screen of channels → events → editor; `studio <channel>/<event>` still deep-links. Note it is read-only navigation (create/edit/delete come later).
- [ ] **CLAUDE.md** — the studio app is workspace-level: `create_app()` takes no profile; event routes are path-scoped under `/api/channels/{channel}/events/{event}/…` and resolve their profile via `_load_profile`; auth is channel-scoped; an SPA fallback serves `index.html` for non-`/api` paths. The `edit.json`-only write boundary is unchanged in G1.
- [ ] **web/README.md** — the three-screen router and the scoped API.
- [ ] Commit — `Document the workspace shell`.

---

## Verification for the branch

- Full `pytest` suite green, E2E included; `npm test` green.
- **`studio` with no argument launches and shows channels; `studio <channel>/<event>` deep-links into the editor** — both driven in the E2E.
- **No editor behaviour change:** the existing editor E2E pass against the scoped URLs with unchanged assertions.
- The SPA fallback serves `index.html` for a deep link/reload without shadowing `/api` or the built assets (tested).
- `workspace_listing.py` imports no FastAPI; `create_app()` still pulls no google at module scope.
- Read-only holds: no route creates, edits, or deletes a channel or event; the studio still writes only `edit.json`.

## Self-review notes

- The re-scoping is mechanical but wide; the risk is an inconsistently converted route (wrong path or a forgotten `_load_profile`). Task 2 lists every route explicitly and the E2E exercises them, so a missed one 404s a test.
- The SPA fallback is the subtle piece — it must not shadow `/api` or assets. Task 2 Step 1 tests both the fallback and that `/api/*` still 404s correctly.
- Pre-flight to run before dispatch (per this project's discipline): execute Task 1's listing functions against the fixture, and stand up `create_app()` with two scoped routes + the SPA fallback in a scratch app to confirm the path params resolve and the fallback/`api` precedence works, before converting all 14 routes.

Deferred with reason (not gaps): create/edit/delete (G2/G3), the brand editor (G3), the settings page (G4) — this stage is navigation only.
