# Workspace management (Eclipse-style) — design

Date: 2026-07-24
Status: approved (design), ready for implementation plan

## Motivation

`YT_SHORTS_DATA` (the workspace = data root that holds every channel, event,
clip and auth token) is resolved once at process start and is read-only in the
studio: the Settings page shows the path but cannot change it, so switching data
roots means editing an environment variable and relaunching. The operator wants
Eclipse-style workspace management in the UI: see the current workspace, pick a
recent one, browse to an existing one, create a new one, and copy (clone) one —
without hand-editing anything.

This is the one setting that is genuinely different from per-channel config
(brand.json / channel.json): the workspace is the *container* for all of that,
so changing it re-roots the whole app.

## Decided requirements

- **Switch is instant, no restart.** The app re-roots live. Unlike Eclipse (deep
  framework/plugin state), the studio's only workspace binding is a single
  `Path` (`channels_dir`), so per-request resolution is a bounded change.
- **Switch/copy/create are refused while any background job is running**
  (render, detect, connect, upload, and the copy job itself). Returns 409 with a
  clear message. This avoids a job writing the old workspace while the UI shows
  the new one, and avoids copying an inconsistent tree.
- **Operations (core only):** select a recent, open an existing (browse),
  create new, copy (clone). No delete, no rename (do that in Finder).
- **Copy is a true clone including `auth/`** (tokens duplicated; disconnect and
  quota are then per-workspace). Runs as a background job (workspaces can be GBs
  of rendered shorts). Switches to the copy on completion.
- **Recents:** last 3 distinct workspace paths, most-recent-first, auto-pruned.
- **Manifest** identifies a valid workspace; legacy folders (a `channels/` dir,
  no manifest) are adopted (manifest written) on first use.
- **`YT_SHORTS_DATA` env stays an override** and, when set, locks UI switching
  (the UI says so and disables the control).

## Architecture

### Workspace manifest

A marker file at the workspace root:

```
.yt-shorts-workspace.json
{ "yt_shorts_workspace": 1, "name": "<optional label>", "created": "<ISO-8601>" }
```

- A directory is a **valid workspace** if it has this manifest OR (legacy) a
  `channels/` subdirectory.
- Opening a valid-but-manifest-less directory through the workspace manager
  **adopts** it: the manifest is written (name defaults to the directory
  basename). Adoption is a deliberate UI action (switch/open), never a silent
  write at server startup — a workspace resolved as the plain default still
  displays fine (name = basename) until the operator opens it. The existing
  `~/YT-Shorts-Data` is adopted the first time it is opened this way.
- Creating a new workspace writes the manifest and an empty `channels/`.
- `created` is passed in by the caller (the studio stamps it); `workspaces.py`
  never calls `datetime.now()` itself, keeping it pure/testable.

### User config (recents + current selection)

Workspace-independent, user-level file:

```
$XDG_CONFIG_HOME/yt-shorts/workspaces.json   (default ~/.config/yt-shorts/workspaces.json)
{ "current": "/abs/path", "recent": ["/abs/path", ...] }   // recent: max 3, newest first
```

- `current` = the active workspace path.
- `recent` = the last 3 distinct paths (includes current at the head).
- Missing/malformed config is treated as "no selection" (falls through to the
  next resolution source), never an error.

### Resolution order (`workspace.resolve`)

1. `YT_SHORTS_DATA` env (explicit override — unchanged; a set-but-missing path is
   still an error). Origin `"YT_SHORTS_DATA"`.
2. **NEW:** the user config's `current`, if set and a valid directory. Origin
   `"config"`.
3. `~/YT-Shorts-Data` if it exists. Origin `"default"`.
4. the repository's own `channels/`. Origin `"repository"`.

When origin is `"YT_SHORTS_DATA"`, the studio treats the workspace as locked: the
switch/create/copy routes refuse (409) and the UI disables the controls with an
explanatory note ("set by the YT_SHORTS_DATA environment variable — unset it to
manage workspaces here").

### Backend

**New pure module `workspaces.py`** (no FastAPI; paths/clock injected for tests,
same style as `workspace.py` / `channel_admin.py`):
- `read_config(config_path) -> {current, recent}` / `write_config(...)`.
- `push_recent(config, path)` — rotate recents to max 3, newest first, deduped.
- `read_manifest(dir)` / `is_workspace(dir)` (manifest OR `channels/`) /
  `write_manifest(dir, name, created)`.
- `adopt(dir, created)` — write a manifest into a legacy workspace if missing.
- `create_workspace(parent, name, created)` — validate the name as a safe
  segment (reuse `pathnames.validate_segment`), mkdir `parent/name`, create
  `channels/`, write the manifest. Refuse if it already exists.
- `copy_workspace(src, dest_parent, name, created)` — validate name, refuse if
  the target exists, `shutil.copytree` the whole `src` tree (INCLUDING `auth/`),
  then write/refresh the destination manifest.
- `list_dir(path)` — the directory entries under `path` that are directories,
  each annotated with whether it is a valid workspace, for the browse dialog.
  Returns dirs only, never file contents.

**`workspace.py`**: `resolve()` gains source 2 above by consulting
`workspaces.read_config` (config path injected; default `~/.config/yt-shorts`).

**Studio dynamic re-rooting**: today `create_app()` snapshots
`channels_dir = profile.CHANNELS_DIR` and every route closes over it. Change so
routes resolve the **current** workspace per request via a single accessor
(e.g. `_current_channels_dir()`), backed by studio state that a switch updates.
The switch also updates `profile.CHANNELS_DIR` (the module global `profile.load`
reads) so the two stay consistent. Because there is exactly one current
workspace at a time, concurrent requests see a consistent value; the only race
is a single request in flight during the switch, acceptable for a local
single-user tool.

**Routes** (workspace-level, not channel-scoped):
- `GET /api/workspaces` → `{ current: {path, name, origin, locked}, recent:
  [{path, name, valid}] }`.
- `POST /api/workspaces/switch {path}` → validate it is a workspace; **409 if a
  job is running or the env lock is set**; else set current, push recents,
  re-root, return the new state.
- `POST /api/workspaces/create {parent, name}` → create + switch (same guards).
- `POST /api/workspaces/copy {parent, name}` → start a **background copy job**
  (same job infra as render/detect); on completion switch to the copy. Guarded
  like switch; the running copy job itself blocks further switches.
- `GET /api/fs?path=…` → server-side **directory** listing for the browse dialog
  (dir names only + valid-workspace flag). Starts at the user's home when
  `path` is omitted; a breadcrumb allows going up to `/`.

**Job running check** reuses the studio's existing `JobStore` (whatever
`studio.jobs` exposes for "is anything running"); the copy is a new job kind.

### Frontend (Settings)

The Workspace panel becomes interactive:
- Shows current workspace (path + name + origin). If env-locked, a note and the
  controls disabled.
- **"Switch workspace"** opens a dialog offering:
  - **Recents** — up to 3 quick-select rows (path + name).
  - **Open…** — the FS browser to pick an existing valid workspace.
  - **New…** — the FS browser to pick a parent, plus a name field.
  - **Copy…** — the FS browser to pick a destination parent, plus a name field;
    shows the copy job's progress.
- **FS browser dialog** — navigates server directories (breadcrumb up to `/`, a
  list of subdirectories, a "✓ workspace" badge on valid ones). "Open" enables
  only on a valid workspace; "New"/"Copy" enable on any directory as the parent.
- After a successful switch/create/copy, the app reloads to the channel list
  (the whole dataset changed).
- Pure helpers (recents shaping, manifest/validity display, path joining) live in
  their own `.ts` module (Vite fast-refresh boundary stays component-only) and
  are unit-tested, like `brand.ts` / `eventAdmin.ts`.

### Security

`GET /api/fs` exposes local directory names over the localhost API. Acceptable
for a local single-user tool (it is the operator's own machine): directory names
only, no file contents, no write. Stated explicitly so it is a deliberate choice.
The copy clones `auth/` (secrets) per the operator's decision; this is the only
path that duplicates tokens, and it never leaves the local filesystem.

## Testing

- **`workspaces.py` (pure):** resolution order with a config `current`; manifest
  read/validity (manifest, legacy `channels/`, neither); adopt writes a manifest;
  recents rotation/dedup/cap; `create_workspace` (scaffold + manifest, refuse
  existing, reject bad name); `copy_workspace` (clones the tree incl. `auth/`,
  refuse existing); `list_dir` (dirs only, valid flags). Paths/clock injected.
- **`workspace.py`:** `resolve()` picks the config `current` when present, env
  still overrides, default/repo fallbacks unchanged.
- **Studio API:** `GET /api/workspaces`; `switch` happy path + **409 while a job
  runs** + **409 when env-locked**; `create`; `copy` (starts a job); `GET /api/fs`
  listing + workspace flags. In-process `TestClient`, same pattern as the other
  studio API tests.
- **Vitest:** the pure frontend helpers (recents shaping, validity display).
- **Playwright E2E (in pytest):** a switch flow at API level against the live
  app (the existing `live_server` fixture), asserting the re-root took effect.
- Full pytest suite green, `npm test` green, `npm run build` committed
  (`static/`), byte-identical no-regression overlays unaffected (no overlay
  change here).

## Out of scope (explicitly)

- Deleting or renaming a workspace from the app (use Finder).
- Multi-workspace-at-once / side-by-side (there is one current workspace).
- Remote/networked workspaces (local filesystem only).
- Excluding rendered shorts or any subtree from a copy (copy is a full clone).

## Notable risks / decisions carried forward

- **Env override vs UI selection:** if `YT_SHORTS_DATA` is set, UI switching is
  locked and this is surfaced — avoids "I set it in the UI but nothing changed".
- **In-flight jobs:** the block-while-busy guard is the mitigation for a job
  bound to the old workspace during a switch.
- **`profile.CHANNELS_DIR` global:** kept in sync with the studio's current
  workspace on every switch; one current workspace keeps concurrent requests
  consistent.
- **Copy duplicates auth tokens:** deliberate (operator chose a true clone);
  documented so disconnect/quota being per-workspace is not a surprise.
