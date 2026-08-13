"""Multi-workspace management for the studio (config, recents, manifest,
create/copy, directory listing). Pure filesystem + JSON, no FastAPI; every path
and clock is injected so it tests without touching the operator's real config or
home. workspace.py resolves WHICH workspace to use; this module manages the set
of them and the operations over them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import atomicwrite, pathnames
from .cancel import Stopped

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
    atomicwrite.write_text(path, json.dumps(config, indent=2) + "\n")


def push_recent(recent: list[str], path: str, *, cap: int = RECENT_CAP) -> list[str]:
    deduped = [path] + [p for p in recent if p != path]
    return deduped[:cap]


MANIFEST_NAME = ".yt-shorts-workspace.json"


def read_manifest(directory: Path) -> dict | None:
    try:
        data = json.loads((Path(directory) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_manifest(directory: Path, name: str, created: str) -> None:
    payload = {"yt_shorts_workspace": 1, "name": name, "created": created}
    atomicwrite.write_text(
        Path(directory) / MANIFEST_NAME, json.dumps(payload, indent=2) + "\n")


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


def copy_workspace(src: Path, parent: Path, name: str, created: str, *,
                   cancel=None) -> Path:
    """Clones a whole workspace tree. With a `cancel.CancelToken`, this is
    stoppable AFTER THE CURRENT FILE - which is the entire content of
    `KINDS["copy"].stop_point`.

    The check rides in as `copytree`'s `copy_function`, so it happens once
    per file with no loop of our own: `copytree` catches `OSError` per entry
    and collects it, but `Stopped` is a `RuntimeError` and propagates out
    immediately. The half-copied target is then REMOVED - a directory with
    a `channels/` in it already looks like a workspace to `is_workspace`, so
    leaving it behind would offer the operator a workspace that is missing
    an unknowable part of itself.
    """
    _validate_name(name)
    src = Path(src)
    if not is_workspace(src):
        raise WorkspaceError(f"{src} is not a workspace", kind="not_found")
    target = Path(parent) / name
    if target.exists():
        raise WorkspaceError(f"a folder named {name!r} already exists here", kind="exists")
    if cancel is None:
        shutil.copytree(src, target)
    else:
        def copy_one(source, destination, *, follow_symlinks=True):
            cancel.raise_if_stopped()
            return shutil.copy2(source, destination, follow_symlinks=follow_symlinks)

        try:
            shutil.copytree(src, target, copy_function=copy_one)
        except Stopped:
            shutil.rmtree(target, ignore_errors=True)
            raise
    write_manifest(target, name, created)
    return target
