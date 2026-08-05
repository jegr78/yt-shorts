# Workspace Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Eclipse-style workspace management to the studio UI — see/switch the current workspace, pick a recent one, browse to or create one, and copy (clone) one — with an instant, no-restart switch.

**Architecture:** A new pure module `workspaces.py` owns the user config (current + recents), the workspace manifest, and the create/copy/list operations. `workspace.resolve()` gains the config as a resolution source. The studio re-roots live by reassigning the `channels_dir` closure cell (shared by every route) and keeping `profile.CHANNELS_DIR` in sync; new workspace routes plus a server-side directory browser drive a Settings dialog. Copy is a background job.

**Tech Stack:** Python 3 / FastAPI (backend, no new deps), React + Mantine + Vite + TypeScript (frontend), pytest + FastAPI TestClient (backend tests), Vitest (frontend unit tests), Playwright-in-pytest (E2E).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation; tests run with `PYTHONPATH=src .venv/bin/pytest -q`.
- Pure modules (`workspaces.py`) must import no FastAPI and take injectable paths/clock — no `datetime.now()`/`Path.home()`/`os.environ` read inside; callers pass them (same style as `workspace.py`, `channel_admin.py`).
- The mechanical linter must stay green: `python3 tools/lint.py` (ruff + in-house guards). No bare `except: pass` without a comment; no `raise ... from` omission inside `except`.
- Frontend: pure logic lives in non-component `.ts` modules (Vite fast-refresh boundary stays component-only); run `npm test` (Vitest) before committing a frontend change; `npm run build` regenerates `src/yt_shorts/studio/static/` which MUST be committed (the tool serves the built output from a clone).
- Path segments that become directory names go through `pathnames.validate_segment` (`^[A-Za-z0-9][A-Za-z0-9._-]*\Z`) before any filesystem touch.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- All work on the `master` branch (the repo's working branch).

---

### Task 1: `workspaces.py` — user config (current + recents)

**Files:**
- Create: `src/yt_shorts/workspaces.py`
- Test: `tests/test_workspaces.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CONFIG_RELATIVE = Path("yt-shorts/workspaces.json")`
  - `config_path(config_home: Path) -> Path` → `config_home / CONFIG_RELATIVE`
  - `read_config(config_home: Path) -> dict` → `{"current": str|None, "recent": list[str]}`; missing/malformed file → `{"current": None, "recent": []}`.
  - `write_config(config_home: Path, config: dict) -> None` (creates parent dirs; writes pretty JSON + trailing newline).
  - `push_recent(recent: list[str], path: str, *, cap: int = 3) -> list[str]` → new list, `path` first, deduped, capped.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workspaces.py
import json
from pathlib import Path

import pytest

from yt_shorts import workspaces


def test_read_config_missing_returns_empty(tmp_path):
    assert workspaces.read_config(tmp_path) == {"current": None, "recent": []}


def test_write_then_read_roundtrip(tmp_path):
    workspaces.write_config(tmp_path, {"current": "/w/a", "recent": ["/w/a"]})
    assert workspaces.config_path(tmp_path).is_file()
    assert workspaces.read_config(tmp_path) == {"current": "/w/a", "recent": ["/w/a"]}


def test_read_config_malformed_is_empty(tmp_path):
    p = workspaces.config_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert workspaces.read_config(tmp_path) == {"current": None, "recent": []}


def test_push_recent_dedups_and_caps_newest_first():
    assert workspaces.push_recent(["/a", "/b"], "/a") == ["/a", "/b"]
    assert workspaces.push_recent(["/a", "/b", "/c"], "/d") == ["/d", "/a", "/b"]
    assert workspaces.push_recent([], "/a") == ["/a"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspaces.py -q`
Expected: FAIL (ModuleNotFoundError: yt_shorts.workspaces)

- [ ] **Step 3: Write minimal implementation**

```python
# src/yt_shorts/workspaces.py
"""Multi-workspace management for the studio (config, recents, manifest,
create/copy, directory listing). Pure filesystem + JSON, no FastAPI; every path
and clock is injected so it tests without touching the operator's real config or
home. workspace.py resolves WHICH workspace to use; this module manages the set
of them and the operations over them."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_RELATIVE = Path("yt-shorts/workspaces.json")
RECENT_CAP = 3


def config_path(config_home: Path) -> Path:
    return Path(config_home) / CONFIG_RELATIVE


def read_config(config_home: Path) -> dict:
    path = config_path(config_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"current": None, "recent": []}
    if not isinstance(data, dict):
        return {"current": None, "recent": []}
    current = data.get("current")
    recent = data.get("recent")
    return {
        "current": current if isinstance(current, str) else None,
        "recent": [p for p in recent if isinstance(p, str)] if isinstance(recent, list) else [],
    }


def write_config(config_home: Path, config: dict) -> None:
    path = config_path(config_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def push_recent(recent: list[str], path: str, *, cap: int = RECENT_CAP) -> list[str]:
    deduped = [path] + [p for p in recent if p != path]
    return deduped[:cap]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspaces.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/workspaces.py tests/test_workspaces.py
git commit -m "feat(workspaces): user config with current + recents"
```

---

### Task 2: `workspaces.py` — manifest, validity, adopt, directory listing

**Files:**
- Modify: `src/yt_shorts/workspaces.py`
- Test: `tests/test_workspaces.py`

**Interfaces:**
- Consumes: Task 1 module.
- Produces:
  - `MANIFEST_NAME = ".yt-shorts-workspace.json"`
  - `is_workspace(directory: Path) -> bool` — has the manifest OR a `channels/` dir.
  - `read_manifest(directory: Path) -> dict | None` — parsed manifest, or None.
  - `write_manifest(directory: Path, name: str, created: str) -> None`.
  - `adopt(directory: Path, created: str) -> None` — write a manifest if the dir is a legacy workspace lacking one (no-op if it already has one).
  - `workspace_name(directory: Path) -> str` — manifest `name` if present, else the dir basename.
  - `list_dir(path: Path) -> dict` — `{"path": str, "parent": str|None, "entries": [{"name","path","is_workspace"}]}`; subdirectories only, sorted, hidden dirs (leading `.`) skipped; unreadable dirs → empty `entries`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_workspaces.py
def _make_ws(root: Path) -> Path:
    (root / "channels").mkdir(parents=True)
    return root


def test_is_workspace_by_manifest_or_channels(tmp_path):
    legacy = _make_ws(tmp_path / "legacy")
    assert workspaces.is_workspace(legacy) is True          # has channels/
    plain = tmp_path / "plain"
    plain.mkdir()
    assert workspaces.is_workspace(plain) is False
    workspaces.write_manifest(plain, "Plain", "2026-07-24T00:00:00")
    assert workspaces.is_workspace(plain) is True           # now has manifest


def test_adopt_writes_manifest_for_legacy_only(tmp_path):
    legacy = _make_ws(tmp_path / "legacy")
    workspaces.adopt(legacy, "2026-07-24T00:00:00")
    assert (legacy / workspaces.MANIFEST_NAME).is_file()
    # idempotent: adopting again keeps the original name
    workspaces.write_manifest(legacy, "Kept", "2026-07-24T00:00:00")
    workspaces.adopt(legacy, "2026-07-25T00:00:00")
    assert workspaces.read_manifest(legacy)["name"] == "Kept"


def test_workspace_name_prefers_manifest(tmp_path):
    d = _make_ws(tmp_path / "erf-data")
    assert workspaces.workspace_name(d) == "erf-data"       # basename fallback
    workspaces.write_manifest(d, "ERF Prod", "2026-07-24T00:00:00")
    assert workspaces.workspace_name(d) == "ERF Prod"


def test_list_dir_lists_subdirs_with_workspace_flag(tmp_path):
    _make_ws(tmp_path / "a-ws")
    (tmp_path / "b-plain").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    out = workspaces.list_dir(tmp_path)
    names = {e["name"]: e["is_workspace"] for e in out["entries"]}
    assert names == {"a-ws": True, "b-plain": False}        # no hidden, no files
    assert out["parent"] == str(tmp_path.parent)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspaces.py -q`
Expected: FAIL (AttributeError: module has no attribute 'is_workspace')

- [ ] **Step 3: Write minimal implementation** (append to `workspaces.py`)

```python
MANIFEST_NAME = ".yt-shorts-workspace.json"


def read_manifest(directory: Path) -> dict | None:
    try:
        data = json.loads((Path(directory) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_manifest(directory: Path, name: str, created: str) -> None:
    payload = {"yt_shorts_workspace": 1, "name": name, "created": created}
    (Path(directory) / MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def is_workspace(directory: Path) -> bool:
    directory = Path(directory)
    return (directory / MANIFEST_NAME).is_file() or (directory / "channels").is_dir()


def workspace_name(directory: Path) -> str:
    manifest = read_manifest(directory)
    name = manifest.get("name") if isinstance(manifest, dict) else None
    return name if isinstance(name, str) and name else Path(directory).name


def adopt(directory: Path, created: str) -> None:
    directory = Path(directory)
    if is_workspace(directory) and read_manifest(directory) is None:
        write_manifest(directory, directory.name, created)


def list_dir(path: Path) -> dict:
    path = Path(path)
    parent = str(path.parent) if path.parent != path else None
    entries = []
    try:
        children = sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        children = []
    for child in children:
        if child.name.startswith("."):
            continue
        entries.append({"name": child.name, "path": str(child),
                        "is_workspace": is_workspace(child)})
    return {"path": str(path), "parent": parent, "entries": entries}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspaces.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/workspaces.py tests/test_workspaces.py
git commit -m "feat(workspaces): manifest, validity, adopt, directory listing"
```

---

### Task 3: `workspaces.py` — create and copy

**Files:**
- Modify: `src/yt_shorts/workspaces.py`
- Test: `tests/test_workspaces.py`

**Interfaces:**
- Consumes: Task 2, plus `pathnames.validate_segment`.
- Produces:
  - `class WorkspaceError(Exception)` with `.kind` (`"bad_name" | "exists" | "not_found"`).
  - `create_workspace(parent: Path, name: str, created: str) -> Path` — validates `name` as a safe segment; refuses if `parent/name` exists (`exists`); mkdir + `channels/` + manifest; returns the new path.
  - `copy_workspace(src: Path, parent: Path, name: str, created: str) -> Path` — validates `name`; refuses if target exists (`exists`) or `src` is not a workspace (`not_found`); `shutil.copytree(src, target)` (whole tree INCLUDING `auth/`); rewrites the target manifest (new name/created); returns the target path.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_workspaces.py
def test_create_workspace_scaffolds(tmp_path):
    ws = workspaces.create_workspace(tmp_path, "new-ws", "2026-07-24T00:00:00")
    assert ws == tmp_path / "new-ws"
    assert (ws / "channels").is_dir()
    assert workspaces.read_manifest(ws)["name"] == "new-ws"


def test_create_workspace_rejects_bad_name(tmp_path):
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.create_workspace(tmp_path, "../evil", "2026-07-24T00:00:00")
    assert e.value.kind == "bad_name"


def test_create_workspace_refuses_existing(tmp_path):
    (tmp_path / "dup").mkdir()
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.create_workspace(tmp_path, "dup", "2026-07-24T00:00:00")
    assert e.value.kind == "exists"


def test_copy_workspace_clones_including_auth(tmp_path):
    src = _make_ws(tmp_path / "src")
    (src / "channels" / "erf").mkdir()
    (src / "auth").mkdir()
    (src / "auth" / "token-UC1.json").write_text("secret")
    dest = workspaces.copy_workspace(src, tmp_path / "dests", "clone",
                                     "2026-07-24T00:00:00")
    assert (dest / "channels" / "erf").is_dir()
    assert (dest / "auth" / "token-UC1.json").read_text() == "secret"   # true clone
    assert workspaces.read_manifest(dest)["name"] == "clone"


def test_copy_workspace_refuses_non_workspace_src(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.copy_workspace(plain, tmp_path, "x", "2026-07-24T00:00:00")
    assert e.value.kind == "not_found"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspaces.py -q`
Expected: FAIL (AttributeError: create_workspace)

- [ ] **Step 3: Write minimal implementation** (append to `workspaces.py`; add imports `import shutil` and `from . import pathnames` at the top of the file)

```python
class WorkspaceError(Exception):
    """kind: "bad_name" | "exists" | "not_found"."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _validate_name(name: str) -> None:
    try:
        pathnames.validate_segment(name, what="workspace name")
    except ValueError as error:
        raise WorkspaceError(str(error), kind="bad_name") from error


def create_workspace(parent: Path, name: str, created: str) -> Path:
    _validate_name(name)
    target = Path(parent) / name
    if target.exists():
        raise WorkspaceError(f"a folder named {name!r} already exists here", kind="exists")
    (target / "channels").mkdir(parents=True)
    write_manifest(target, name, created)
    return target


def copy_workspace(src: Path, parent: Path, name: str, created: str) -> Path:
    _validate_name(name)
    src = Path(src)
    if not is_workspace(src):
        raise WorkspaceError(f"{src} is not a workspace", kind="not_found")
    target = Path(parent) / name
    if target.exists():
        raise WorkspaceError(f"a folder named {name!r} already exists here", kind="exists")
    shutil.copytree(src, target)
    write_manifest(target, name, created)
    return target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspaces.py -q`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/workspaces.py tests/test_workspaces.py
git commit -m "feat(workspaces): create and copy (full clone incl. auth)"
```

---

### Task 4: `workspace.resolve()` — config as a resolution source

**Files:**
- Modify: `src/yt_shorts/workspace.py`
- Test: `tests/test_workspace.py`

**Interfaces:**
- Consumes: `workspaces.read_config`.
- Produces: `resolve(env=None, home=None, repo_channels=None, config_home=None)` — new keyword `config_home` (defaults to `$XDG_CONFIG_HOME` or `~/.config`). New source order: env `YT_SHORTS_DATA` → config `current` (valid dir) → `~/YT-Shorts-Data` → repo `channels/`. Config-sourced `Workspace.origin == "config"`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_workspace.py
from yt_shorts import workspaces


def test_resolve_uses_config_current_when_set(tmp_path):
    data = tmp_path / "data"
    (data / "channels").mkdir(parents=True)
    config_home = tmp_path / "cfg"
    workspaces.write_config(config_home, {"current": str(data), "recent": [str(data)]})
    ws = resolve(env={}, home=tmp_path / "home", config_home=config_home)
    assert ws.root == data
    assert ws.origin == "config"


def test_env_overrides_config(tmp_path):
    envdir = tmp_path / "envdata"
    (envdir / "channels").mkdir(parents=True)
    cfgdir = tmp_path / "cfgdata"
    (cfgdir / "channels").mkdir(parents=True)
    config_home = tmp_path / "cfg"
    workspaces.write_config(config_home, {"current": str(cfgdir), "recent": []})
    ws = resolve(env={"YT_SHORTS_DATA": str(envdir)}, home=tmp_path,
                 config_home=config_home)
    assert ws.root == envdir and ws.origin == "YT_SHORTS_DATA"


def test_config_missing_falls_through_to_default(tmp_path):
    home = tmp_path / "home"
    (home / "YT-Shorts-Data" / "channels").mkdir(parents=True)
    ws = resolve(env={}, home=home, config_home=tmp_path / "empty-cfg")
    assert ws.origin == "default"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspace.py -q`
Expected: FAIL (TypeError: resolve() got an unexpected keyword 'config_home' — or origin mismatch)

- [ ] **Step 3: Write minimal implementation**

In `src/yt_shorts/workspace.py`, add near the top:

```python
import os  # already imported
from . import workspaces as _workspaces
```

Change the `resolve` signature and insert the config source AFTER the env block, BEFORE `default_root`:

```python
def resolve(env: dict | None = None, home: Path | None = None,
            repo_channels: Path | None = None, config_home: Path | None = None) -> Workspace:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    repo_channels = REPO_CHANNELS if repo_channels is None else repo_channels
    if config_home is None:
        xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
        config_home = Path(xdg) if xdg else home / ".config"

    named = (env.get(ENV_VAR) or "").strip()
    if named:
        ...  # UNCHANGED existing env block, returning origin=ENV_VAR
    # --- NEW: the user config's current selection ---
    current = _workspaces.read_config(config_home).get("current")
    if isinstance(current, str) and current.strip():
        root = Path(current).resolve()
        if root.is_dir():
            return Workspace(root=root, channels_dir=root / "channels", origin="config")
    # --- existing default + repository fallbacks below, UNCHANGED ---
```

Note: a config `current` that no longer exists is skipped silently (falls through), unlike the env var — a stale recent selection is not an error the way an explicit env override is.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspace.py -q`
Expected: PASS (existing + 3 new)

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/workspace.py tests/test_workspace.py
git commit -m "feat(workspace): resolve the user config's current workspace"
```

---

### Task 5: `JobStore.any_running()`

**Files:**
- Modify: `src/yt_shorts/studio/jobs.py`
- Test: `tests/test_studio_jobs.py` (create if absent; otherwise append)

**Interfaces:**
- Produces: `JobStore.any_running() -> bool` — True if any tracked job has status `"running"` OR any connect is in flight (the `begin_connect` set).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_studio_jobs.py
from yt_shorts.studio.jobs import JobStore


def test_any_running_reflects_job_status():
    store = JobStore()
    assert store.any_running() is False
    job = store.create()
    assert store.any_running() is True     # a fresh job starts "running"
    job.finish("done")
    assert store.any_running() is False


def test_any_running_reflects_connect_set():
    store = JobStore()
    assert store.begin_connect("UC1") is True
    assert store.any_running() is True
    store.end_connect("UC1")
    assert store.any_running() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_jobs.py -q`
Expected: FAIL (AttributeError: any_running)

- [ ] **Step 3: Write minimal implementation**

Read `src/yt_shorts/studio/jobs.py` to confirm the lock attribute name and the connect-set attribute name (search `begin_connect`). Add to `JobStore` (using the existing lock; the connect set is the dict/set `begin_connect` mutates):

```python
    def any_running(self) -> bool:
        """True if any job is still running or a connect is in flight - the
        guard the workspace switch/create/copy routes use to refuse re-rooting
        mid-operation."""
        with self._lock:
            if any(job.status == "running" for job in self._jobs.values()):
                return True
            return bool(self._connecting)
```

If the connect set has a different attribute name than `self._connecting`, use that name (confirm by reading `begin_connect`).

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_jobs.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/jobs.py tests/test_studio_jobs.py
git commit -m "feat(jobs): JobStore.any_running() for the workspace-switch guard"
```

---

### Task 6: Studio workspace routes — list, switch, create, and the FS browser

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Consumes: `workspaces` module, `JobStore.any_running`, `workspace.resolve`.
- Produces four routes and a re-root helper. In `create_app`, `channels_dir` is the closure cell every route already references; a new nested `_switch_to(root: Path)` reassigns it via `nonlocal` AND sets `_profile_module.CHANNELS_DIR`, so every route re-roots at once.
  - `GET /api/workspaces` → `{"current": {"path","name","origin","locked"}, "recent": [{"path","name","valid"}]}`. `locked` is `origin == "YT_SHORTS_DATA"`.
  - `POST /api/workspaces/switch {"path": str}` → 409 if `job_store.any_running()` or locked; 400 if not a workspace; else adopt + re-root + push recents + persist config; returns the new `GET /api/workspaces` body.
  - `POST /api/workspaces/create {"parent": str, "name": str}` → create then switch (same guards); 400 bad_name, 409 exists/locked/busy.
  - `GET /api/fs?path=` → `workspaces.list_dir(path or home)`.

**Design notes for the implementer:**
- `create_app` currently does `channels_dir = _profile_module.CHANNELS_DIR`. Keep that line. Add, INSIDE `create_app` (so it closes over `channels_dir`), a helper and the studio's config home:

```python
    config_home = _resolve_workspace_config_home()   # see below
    now = _now_iso                                    # injected clock, see below

    def _switch_to(root: Path) -> None:
        nonlocal channels_dir
        channels_dir = root / "channels"
        _profile_module.CHANNELS_DIR = channels_dir
```

- Add module-level helpers near the other `_resolve_workspace` import usage:

```python
def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _config_home() -> Path:
    import os
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    return Path(xdg) if xdg else Path.home() / ".config"
```

- The route body (place after the Settings routes). `_resolve_workspace()` is already imported as `resolve`; call it to learn the current root+origin:

```python
    @app.get("/api/workspaces")
    def get_workspaces() -> dict:
        ws = _resolve_workspace()
        recent = _workspaces.read_config(_config_home()).get("recent", [])
        return {
            "current": {
                "path": str(ws.root),
                "name": _workspaces.workspace_name(ws.root),
                "origin": ws.origin,
                "locked": ws.origin == "YT_SHORTS_DATA",
            },
            "recent": [
                {"path": p, "name": _workspaces.workspace_name(Path(p)),
                 "valid": _workspaces.is_workspace(Path(p))}
                for p in recent
            ],
        }

    def _guard_reroot() -> None:
        if _resolve_workspace().origin == "YT_SHORTS_DATA":
            raise HTTPException(
                status_code=409,
                detail="workspace is set by the YT_SHORTS_DATA environment variable; "
                       "unset it to manage workspaces here")
        if job_store.any_running():
            raise HTTPException(
                status_code=409,
                detail="a job is running - wait for it to finish before switching workspace")

    def _select(root: Path) -> dict:
        _workspaces.adopt(root, _now_iso())
        _switch_to(root)
        cfg = _workspaces.read_config(_config_home())
        cfg["current"] = str(root)
        cfg["recent"] = _workspaces.push_recent(cfg.get("recent", []), str(root))
        _workspaces.write_config(_config_home(), cfg)
        return get_workspaces()

    @app.post("/api/workspaces/switch")
    def switch_workspace(body: WorkspaceSwitchBody) -> dict:
        _guard_reroot()
        root = Path(body.path)
        if not _workspaces.is_workspace(root):
            raise HTTPException(status_code=400, detail=f"{root} is not a workspace")
        return _select(root)

    @app.post("/api/workspaces/create")
    def create_workspace_route(body: WorkspaceCreateBody) -> dict:
        _guard_reroot()
        try:
            root = _workspaces.create_workspace(Path(body.parent), body.name, _now_iso())
        except _workspaces.WorkspaceError as error:
            status = {"bad_name": 400, "exists": 409, "not_found": 404}.get(error.kind, 400)
            raise HTTPException(status_code=status, detail=str(error)) from error
        return _select(root)

    @app.get("/api/fs")
    def browse_fs(path: str | None = None) -> dict:
        base = Path(path) if path else Path.home()
        return _workspaces.list_dir(base)
```

- Add the Pydantic bodies near the other `*Body` models:

```python
class WorkspaceSwitchBody(BaseModel):
    path: str


class WorkspaceCreateBody(BaseModel):
    parent: str
    name: str
```

- Add `from .. import workspaces as _workspaces` to the imports.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_studio_api.py (new class)
class TestWorkspaces:
    def test_get_workspaces_reports_current(self, client, studio_profile):
        body = client.get("/api/workspaces").json()
        assert "current" in body and body["current"]["path"]
        assert "recent" in body

    def test_switch_to_a_created_workspace(self, client, studio_profile, tmp_path, monkeypatch):
        # point the studio's config home at a tmp dir so the test never writes ~/.config
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        made = client.post("/api/workspaces/create",
                           json={"parent": str(tmp_path), "name": "ws-new"})
        assert made.status_code == 200
        assert made.json()["current"]["name"] == "ws-new"
        # a channel created now lands in the NEW workspace, proving the re-root
        client.post("/api/channels", json={"slug": "demo", "id": "UCx",
            "channel_url": "https://youtube.com/channel/UCx", "handle": "@d",
            "display_name": "D", "language": "en", "footer": "D | @d"})
        assert (tmp_path / "ws-new" / "channels" / "demo" / "channel.json").is_file()

    def test_switch_refused_while_a_job_runs(self, client, studio_profile, tmp_path, monkeypatch):
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        target = api._workspaces.create_workspace(tmp_path, "wsX", "2026-07-24T00:00:00")
        # make a job look like it is running
        from yt_shorts.studio.jobs import JobStore
        monkeypatch.setattr(JobStore, "any_running", lambda self: True)
        r = client.post("/api/workspaces/switch", json={"path": str(target)})
        assert r.status_code == 409

    def test_fs_lists_directories(self, client, tmp_path):
        (tmp_path / "sub").mkdir()
        r = client.get("/api/fs", params={"path": str(tmp_path)})
        assert r.status_code == 200
        assert "sub" in {e["name"] for e in r.json()["entries"]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k Workspaces`
Expected: FAIL (404 on /api/workspaces)

- [ ] **Step 3: Write minimal implementation** — apply the route/body/import/helper changes above.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k Workspaces`
Expected: PASS. Then run the full studio suite: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q` (all pass — the re-root must not break existing routes).

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "feat(studio): workspace list/switch/create routes + FS browser, live re-root"
```

---

### Task 7: Studio copy route — background job

**Files:**
- Modify: `src/yt_shorts/studio/jobs.py`, `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Consumes: `JobStore.create`, `workspaces.copy_workspace`, the `_select` re-root helper from Task 6.
- Produces:
  - `jobs.start_copy_job(job_store, src, parent, name, created, on_done, *, copier=workspaces.copy_workspace) -> str` — creates a Job, runs the copy in a thread, calls `on_done(new_path)` on success, `job.finish("done"|"failed")`. `copier`/threading injected for tests.
  - `POST /api/workspaces/copy {"parent","name"}` → guards (locked/busy) then `start_copy_job`, copying the CURRENT workspace; returns `{"job_id": ...}`. The frontend polls `GET /api/jobs/{id}` (existing `getJob`) and refreshes on done.

- [ ] **Step 1: Write the failing test**

```python
# append to TestWorkspaces in tests/test_studio_api.py
    def test_copy_starts_a_job_and_clones(self, client, studio_profile, tmp_path, monkeypatch):
        import yt_shorts.studio.api as api
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        # run the copy synchronously so the test is deterministic
        import yt_shorts.studio.jobs as jobs
        monkeypatch.setattr(jobs, "_spawn", lambda fn: fn())   # see impl note
        r = client.post("/api/workspaces/copy",
                        json={"parent": str(tmp_path), "name": "clone"})
        assert r.status_code == 200 and "job_id" in r.json()
        job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        assert job["status"] == "done"
        assert (tmp_path / "clone" / "channels").is_dir()   # erf fixture cloned
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k copy`
Expected: FAIL (404 on /api/workspaces/copy)

- [ ] **Step 3: Write minimal implementation**

In `jobs.py`, add a tiny seam so tests can run the job synchronously, and the copy starter:

```python
def _spawn(fn) -> None:
    import threading
    threading.Thread(target=fn, daemon=True).start()


def start_copy_job(job_store: "JobStore", src: Path, parent: Path, name: str,
                   created: str, on_done, *, copier=None) -> str:
    from .. import workspaces
    copier = workspaces.copy_workspace if copier is None else copier
    job = job_store.create()

    def run():
        try:
            new_path = copier(src, parent, name, created)
            on_done(new_path)
            job.finish("done")
        except Exception as error:   # noqa: BLE001 - surfaced as a failed job, not a crash
            job.record(name, "failed", str(error), str(error))
            job.finish("failed")

    _spawn(run)
    return job.job_id
```

In `api.py`, add the route (using `_select` from Task 6 as `on_done`, and the injected `now`/`config_home`):

```python
    @app.post("/api/workspaces/copy")
    def copy_workspace_route(body: WorkspaceCreateBody) -> dict:
        _guard_reroot()
        from ..studio.jobs import start_copy_job
        src = channels_dir.parent   # the CURRENT workspace root
        job_id = start_copy_job(job_store, src, Path(body.parent), body.name,
                                _now_iso(), _select)
        return {"job_id": job_id}
```

(Reuses `WorkspaceCreateBody`. `channels_dir.parent` is the current workspace root because `channels_dir` is always `<root>/channels`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k copy`
Expected: PASS. Then `python3 tools/lint.py` (green — the broad-except carries its noqa+comment).

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/jobs.py src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "feat(studio): copy workspace as a background job"
```

---

### Task 8: Frontend API client + pure workspace helpers

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Create: `src/yt_shorts/studio/web/src/workspaces.ts`
- Test: `src/yt_shorts/studio/web/src/workspaces.test.ts`

**Interfaces:**
- Produces in `api.ts`:
  - `interface WorkspacesResponse { current: { path: string; name: string; origin: string; locked: boolean }; recent: { path: string; name: string; valid: boolean }[] }`
  - `interface FsListing { path: string; parent: string | null; entries: { name: string; path: string; is_workspace: boolean }[] }`
  - `getWorkspaces(): Promise<WorkspacesResponse>` (GET /api/workspaces)
  - `switchWorkspace(path): Promise<WorkspacesResponse>` (POST /api/workspaces/switch)
  - `createWorkspace(parent, name): Promise<WorkspacesResponse>` (POST /api/workspaces/create)
  - `copyWorkspace(parent, name): Promise<{ job_id: string }>` (POST /api/workspaces/copy)
  - `browseFs(path?): Promise<FsListing>` (GET /api/fs)
- Produces in `workspaces.ts`:
  - `joinPath(parent: string, name: string): string` — POSIX join (`parent` + `/` + `name`, collapsing a trailing slash).
  - `isValidWorkspaceName(name: string): boolean` — mirrors `pathnames.validate_segment` (`/^[A-Za-z0-9][A-Za-z0-9._-]*$/`).

- [ ] **Step 1: Write the failing test**

```typescript
// src/yt_shorts/studio/web/src/workspaces.test.ts
import { describe, expect, it } from 'vitest'
import { isValidWorkspaceName, joinPath } from './workspaces'

describe('isValidWorkspaceName', () => {
  it.each(['erf', 'my-ws', 'ws.2', 'A_1'])('accepts %s', (n) => {
    expect(isValidWorkspaceName(n)).toBe(true)
  })
  it.each(['', '.hidden', '../x', 'a/b', 'a b'])('rejects %s', (n) => {
    expect(isValidWorkspaceName(n)).toBe(false)
  })
})

describe('joinPath', () => {
  it('joins parent and name', () => {
    expect(joinPath('/a/b', 'c')).toBe('/a/b/c')
    expect(joinPath('/a/b/', 'c')).toBe('/a/b/c')
    expect(joinPath('/', 'c')).toBe('/c')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `src/yt_shorts/studio/web`): `npm test -- workspaces`
Expected: FAIL (cannot find './workspaces')

- [ ] **Step 3: Write minimal implementation**

```typescript
// src/yt_shorts/studio/web/src/workspaces.ts
/** Pure helpers for the workspace manager, kept out of components so Vite's
 * fast-refresh boundary stays component-only and each rule is unit-tested. */

const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

/** Mirrors pathnames.validate_segment - the server is still the boundary. */
export function isValidWorkspaceName(name: string): boolean {
  return NAME_PATTERN.test(name)
}

/** POSIX-join a parent directory and a child name. */
export function joinPath(parent: string, name: string): string {
  return `${parent.replace(/\/+$/, '')}/${name}`
}
```

Then append to `api.ts` (after the settings functions), using the existing `asJson` helper:

```typescript
export interface WorkspacesResponse {
  current: { path: string; name: string; origin: string; locked: boolean }
  recent: { path: string; name: string; valid: boolean }[]
}

export interface FsListing {
  path: string
  parent: string | null
  entries: { name: string; path: string; is_workspace: boolean }[]
}

export function getWorkspaces(): Promise<WorkspacesResponse> {
  return fetch('/api/workspaces').then(asJson<WorkspacesResponse>)
}

export function switchWorkspace(path: string): Promise<WorkspacesResponse> {
  return fetch('/api/workspaces/switch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(asJson<WorkspacesResponse>)
}

export function createWorkspace(parent: string, name: string): Promise<WorkspacesResponse> {
  return fetch('/api/workspaces/create', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parent, name }),
  }).then(asJson<WorkspacesResponse>)
}

export function copyWorkspace(parent: string, name: string): Promise<{ job_id: string }> {
  return fetch('/api/workspaces/copy', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parent, name }),
  }).then(asJson<{ job_id: string }>)
}

export function browseFs(path?: string): Promise<FsListing> {
  const q = path ? `?path=${encodeURIComponent(path)}` : ''
  return fetch(`/api/fs${q}`).then(asJson<FsListing>)
}
```

- [ ] **Step 4: Run test + typecheck**

Run (in `.../web`): `npm test -- workspaces` → PASS; then `npx tsc -b` → exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/api.ts src/yt_shorts/studio/web/src/workspaces.ts src/yt_shorts/studio/web/src/workspaces.test.ts
git commit -m "feat(studio-web): workspace API client + pure helpers"
```

---

### Task 9: FS browser dialog component

**Files:**
- Create: `src/yt_shorts/studio/web/src/components/FsBrowser.tsx`

**Interfaces:**
- Consumes: `browseFs`, `FsListing`, `ApiError` from `../api`.
- Produces: `FsBrowser` — a controlled directory navigator.

```typescript
export function FsBrowser({ value, onChange }: {
  value: string | null                 // the currently-highlighted directory path
  onChange: (path: string, isWorkspace: boolean) => void
}): JSX.Element
```

Behaviour: loads `browseFs()` (home) on mount; shows `listing.path` as a header with an "Up" button (disabled when `parent === null`); lists `entries` as clickable rows (folder icon; a green "workspace" badge when `is_workspace`); a single click selects a row (`onChange(entry.path, entry.is_workspace)` and highlights it); a double click navigates into it (re-fetch `browseFs(entry.path)`). On load error, show an Alert. Keep it ~120 lines; use Mantine `Stack`, `Group`, `Button`, `Badge`, `Alert`, `Loader`, `ScrollArea` (cap height ~320, so it scrolls inside the modal).

- [ ] **Step 1: Write the component** (no unit test — it is exercised by the E2E and by hand; pure logic already lives in `workspaces.ts`)

```typescript
// src/yt_shorts/studio/web/src/components/FsBrowser.tsx
import { useCallback, useEffect, useState } from 'react'
import { Alert, Badge, Button, Group, Loader, ScrollArea, Stack, Text, UnstyledButton } from '@mantine/core'
import { ApiError, browseFs, type FsListing } from '../api'

export function FsBrowser({
  value,
  onChange,
}: {
  value: string | null
  onChange: (path: string, isWorkspace: boolean) => void
}) {
  const [listing, setListing] = useState<FsListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback((path?: string) => {
    setLoading(true)
    browseFs(path)
      .then((data) => {
        setListing(data)
        setError(null)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  return (
    <Stack gap="xs">
      <Group gap="xs" wrap="nowrap">
        <Button
          size="xs"
          variant="default"
          disabled={!listing?.parent || loading}
          onClick={() => listing?.parent && load(listing.parent)}
        >
          ↑ Up
        </Button>
        <Text size="xs" ff="monospace" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
          {listing?.path ?? '…'}
        </Text>
      </Group>
      {error ? (
        <Alert color="red" title="Could not read this folder">
          {error}
        </Alert>
      ) : (
        <ScrollArea.Autosize mah={320} type="auto">
          <Stack gap={2}>
            {loading && !listing ? (
              <Group gap="xs" p="sm">
                <Loader size={16} color="steel" />
                <Text size="xs" c="dimmed">
                  Loading…
                </Text>
              </Group>
            ) : listing && listing.entries.length === 0 ? (
              <Text size="xs" c="dimmed" p="sm">
                No sub-folders here.
              </Text>
            ) : (
              listing?.entries.map((entry) => (
                <UnstyledButton
                  key={entry.path}
                  onClick={() => onChange(entry.path, entry.is_workspace)}
                  onDoubleClick={() => load(entry.path)}
                  p="6px 8px"
                  style={{
                    borderRadius: 6,
                    background:
                      value === entry.path ? 'var(--mantine-color-dark-5)' : 'transparent',
                  }}
                >
                  <Group justify="space-between" wrap="nowrap">
                    <Text size="sm" ff="monospace">
                      📁 {entry.name}
                    </Text>
                    {entry.is_workspace ? (
                      <Badge size="xs" color="green" variant="light">
                        workspace
                      </Badge>
                    ) : null}
                  </Group>
                </UnstyledButton>
              ))
            )}
          </Stack>
        </ScrollArea.Autosize>
      )}
      <Text size="xs" c="dimmed">
        Single-click to select a folder, double-click to open it.
      </Text>
    </Stack>
  )
}
```

- [ ] **Step 2: Typecheck**

Run (in `.../web`): `npx tsc -b` → exit 0.

- [ ] **Step 3: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/FsBrowser.tsx
git commit -m "feat(studio-web): server-side folder browser component"
```

---

### Task 10: Workspace panel + manager dialog in Settings

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/SettingsScreen.tsx`

**Interfaces:**
- Consumes: `getWorkspaces`, `switchWorkspace`, `createWorkspace`, `copyWorkspace`, `getJob`, `WorkspacesResponse`, `ApiError` from `../api`; `FsBrowser`; `isValidWorkspaceName`, `joinPath` from `../workspaces`; existing `useJobPolling` hook.
- Produces: replaces the read-only `WorkspacePanel` with an interactive one (current workspace + name + origin; "Manage workspaces…" button) and a `WorkspaceManagerModal` with three modes (Open / New / Copy) driven by `FsBrowser`, a recents quick-pick, and the env-lock notice.

**Behaviour:**
- The panel calls `getWorkspaces()` on mount; shows `current.name` + `current.path` + origin; a "Manage workspaces…" button (disabled with a tooltip when `current.locked`).
- The modal:
  - **Recents:** rows from `recent` (name + path), each a "Switch" button, disabled when `!valid`.
  - **Open existing:** `FsBrowser`; "Open" enabled only when a selected dir `isWorkspace`; calls `switchWorkspace(selected)`.
  - **New:** `FsBrowser` (pick parent) + a name `TextInput` (validated with `isValidWorkspaceName`); "Create" calls `createWorkspace(parent, name)`.
  - **Copy:** `FsBrowser` (pick destination parent) + name field; "Copy" calls `copyWorkspace(parent, name)`, then polls the returned `job_id` with `useJobPolling`; on `done`, reload.
  - Any successful switch/create/copy-done triggers `window.location.assign('/')` (reload to the channel list — the dataset changed).
  - 409 errors (job running / locked) surface as an inline Alert with the server `detail`.

- [ ] **Step 1: Implement the panel + modal**

Replace the existing `WorkspacePanel` function and add the modal. Key structure (the implementer fills the three mode bodies following the ConnectDialog pattern already in this file):

```typescript
// add imports at top of SettingsScreen.tsx
import { Modal, SegmentedControl, TextInput, Tooltip } from '@mantine/core' // (some already imported)
import {
  copyWorkspace, createWorkspace, getJob, getWorkspaces, switchWorkspace,
  type WorkspacesResponse,
} from '../api'
import { isValidWorkspaceName, joinPath } from '../workspaces'
import { FsBrowser } from './FsBrowser'
```

```typescript
function WorkspacePanel() {
  const [ws, setWs] = useState<WorkspacesResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [manageOpen, setManageOpen] = useState(false)

  const refresh = useCallback(
    () => getWorkspaces().then(setWs).catch((e) =>
      setError(e instanceof ApiError ? e.message : String(e))),
    [],
  )
  useEffect(() => {
    refresh()
  }, [refresh])

  if (error) return <Alert color="red" title="Could not load workspace">{error}</Alert>
  if (!ws) return (
    <Card padding="md"><Group gap="xs"><Loader size={16} color="steel" /><Text size="sm" c="dimmed">Loading workspace…</Text></Group></Card>
  )

  return (
    <Card padding="md">
      <Stack gap="xs">
        <Group justify="space-between">
          <Text fw={600}>Workspace</Text>
          <Tooltip
            disabled={!ws.current.locked}
            label="Set by the YT_SHORTS_DATA environment variable — unset it to manage workspaces here."
          >
            <Button size="xs" variant="default" disabled={ws.current.locked}
                    onClick={() => setManageOpen(true)}>
              Manage workspaces…
            </Button>
          </Tooltip>
        </Group>
        <Text size="sm">{ws.current.name}</Text>
        <Text size="xs" ff="monospace" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
          {ws.current.path}
        </Text>
        <Text size="xs" c="dimmed">Source: {ws.current.origin}</Text>
      </Stack>
      <WorkspaceManagerModal
        opened={manageOpen}
        current={ws}
        onClose={() => setManageOpen(false)}
      />
    </Card>
  )
}
```

The `WorkspaceManagerModal` (a `Modal` with a `SegmentedControl` of `recent | open | new | copy`, an `FsBrowser` where relevant, a name `TextInput` guarded by `isValidWorkspaceName`, and inline `ApiError` handling). On success:

```typescript
  const applied = () => window.location.assign('/')   // reload onto the new dataset
```

For the copy tab, poll with the existing hook:

```typescript
  const [copyJobId, setCopyJobId] = useState<string | null>(null)
  const copyJob = useJobPolling(copyJobId)
  useEffect(() => {
    if (copyJob?.status === 'done') applied()
  }, [copyJob?.status])
```

Write the full modal (~140 lines) following the existing `ConnectDialog` idioms in this same file (Modal, Stack, inline Alert on `startError`, buttons with `loading`/`disabled`). Do NOT introduce new patterns.

- [ ] **Step 2: Typecheck + Vitest**

Run (in `.../web`): `npx tsc -b` (exit 0); `npm test` (all pass — no new unit tests here, but nothing breaks).

- [ ] **Step 3: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/SettingsScreen.tsx
git commit -m "feat(studio-web): interactive workspace panel + manager dialog"
```

---

### Task 11: Build, E2E, full verification, commit static

**Files:**
- Modify: `src/yt_shorts/studio/static/**` (built), optionally `tests/test_studio_e2e.py` (one E2E)

- [ ] **Step 1: Add a Playwright/E2E or API-level switch flow** in `tests/test_studio_e2e.py` using the existing `live_server` fixture: create a workspace via `POST /api/workspaces/create` against a tmp parent (monkeypatch `_config_home`), then assert `GET /api/workspaces` reports it as current and a subsequently-created channel lands under it. (If the E2E harness makes monkeypatching the live server's `_config_home` awkward, keep this assertion at the in-process `TestClient` level in `tests/test_studio_api.py` instead — Task 6 already covers the re-root; this step is only to confirm nothing regressed end-to-end.)

- [ ] **Step 2: Build the frontend**

Run (in `.../web`): `npm run lint` (oxlint clean) then `npm run build` (exit 0 — regenerates `../static`).

- [ ] **Step 3: Run the full suites**

Run: `npm test` (in `.../web`) → all pass.
Run: `PYTHONPATH=src .venv/bin/pytest -q` → all pass.
Run: `python3 tools/lint.py` → All checks passed.

- [ ] **Step 4: Commit the built static + any E2E**

```bash
git add src/yt_shorts/studio/static tests/test_studio_e2e.py
git commit -m "build(studio): rebuild static for workspace management; e2e switch flow"
```

- [ ] **Step 5: Manual smoke (optional)**

Start `bin/yt-shorts studio`, open Settings, confirm: the page scrolls, the Workspace panel shows the current workspace, "Manage workspaces…" opens the dialog, recents/open/new/copy work, and a switch reloads onto the new dataset.

---

## Notes for the implementer

- **The re-root trick (Task 6) is the crux:** every studio route references the enclosing `channels_dir` local through its closure cell. Reassigning that cell (`nonlocal channels_dir`) inside `_switch_to` updates all routes at once — no per-route refactor. `profile.load` reads `profile.CHANNELS_DIR` (a module global), so `_switch_to` must set BOTH. Verify with Task 6's `test_switch_to_a_created_workspace` (a channel created after the switch must land in the new workspace).
- **Do not** read `os.environ`/`Path.home()`/`datetime.now()` inside `workspaces.py`; the studio injects them (`_config_home`, `_now_iso`), which is what makes the tests hermetic.
- **Copy clones `auth/`** on purpose (operator's decision). Say so in the Copy dialog's helper text ("Copies everything, including your YouTube connections.").
- Confirm `JobStore`'s connect-set attribute name in Task 5 by reading `begin_connect` before writing `any_running`.
