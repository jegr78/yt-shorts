"""Where the data lives.

The tool's repository holds code; a workspace holds channels, events, clips
and everything derived from them. Keeping them apart means a workspace can
be backed up as a unit and a checkout can be replaced without touching data.

Resolution deliberately has no flag and no cutover date: creating the default
directory is the entire migration switch. What it must not have is ambiguity,
which is why a set-but-missing YT_SHORTS_DATA is an error rather than a quiet
fall back to a different dataset, and why the caller is expected to report
`describe()` once at startup.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import workspaces as _workspaces

DEFAULT_DIR_NAME = "YT-Shorts-Data"
ENV_VAR = "YT_SHORTS_DATA"

ROOT = Path(__file__).resolve().parent.parent.parent
REPO_CHANNELS = ROOT / "channels"

CENTRAL_LOG_NAME = "yt-shorts.log"

MOMENTS_FILE = "moments.json"
GLOSSARY_FILE = "glossary.json"
SETTINGS_FILE = "settings.json"


def settings_path(root) -> Path:
    """The workspace's own settings (the job queue's pool limits today).

    Workspace data, a sibling of jobs.json/logs/auth - never repository
    data, and never created on demand: an absent file means "every setting
    is at its default", which is the ordinary state.
    """
    return Path(root) / SETTINGS_FILE


def read_settings(root) -> dict:
    """The workspace's settings, or `{}`.

    Best-effort by design, like every other read of a hand-editable file in
    this project: an absent, unreadable or malformed file reads as "no
    settings" rather than raising, because these settings are read on the
    path that BUILDS the studio's queue at startup - a stray character in
    this file must not be the reason the studio has no queue at all. What
    the values mean is the caller's business; this only guarantees a dict.
    """
    try:
        data = json.loads(settings_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Absent (the normal case), unreadable, or not JSON - all of them
        # mean "nothing configured here", see the docstring above.
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(root, settings: dict) -> None:
    """Replaces the workspace's settings file, whole.

    Write-aside-then-replace, the same mechanic `job_queue.save` and
    `render.compose` use: the scratch file is a SIBLING of the target and is
    moved into place with os.replace, so a process killed mid-write leaves
    the old complete file rather than a half-written one.
    """
    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".part")
    scratch.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(scratch, path)


def moments_path(root) -> Path:
    """The workspace-central moments lexicon (see yt_shorts.lexicon).

    Deliberately NOT created on demand, unlike logs_dir: an absent file is the
    normal state and simply means this layer contributes nothing."""
    return Path(root) / MOMENTS_FILE


def glossary_path(root) -> Path:
    """The workspace-central glossary layer (see yt_shorts.glossary).

    Deliberately NOT created on demand, like moments_path and unlike
    logs_dir: an absent file is the normal state and simply means this layer
    contributes nothing on top of the built-in default."""
    return Path(root) / GLOSSARY_FILE


def logs_dir(root) -> Path:
    """The workspace's log directory, created on demand.

    Logs are workspace data, not repository data - they sit beside auth/ and
    streams/ so they are per-operator, backed up with the rest of the
    workspace, and readable by the studio (see logsetup.py)."""
    path = Path(root) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_logs_dir(root) -> Path:
    """Where a background job's own log file lives (see studio/jobs.py)."""
    path = logs_dir(root) / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


class WorkspaceError(Exception):
    """Understandable error message about an unusable workspace."""


@dataclass(frozen=True)
class Workspace:
    root: Path
    channels_dir: Path
    origin: str

    def describe(self) -> str:
        return f"data: {self.root} ({self.origin})"


def resolve(env: dict | None = None, home: Path | None = None,
            repo_channels: Path | None = None,
            config_home: Path | None = None) -> Workspace:
    """Returns the workspace to use, in this order:

    1. ``YT_SHORTS_DATA`` if set to a non-empty value. A path that does not
       exist raises WorkspaceError - falling back silently would mean the
       operator asked for one dataset and quietly got another.
    2. the user config's ``current`` workspace (see ``workspaces.py``), if
       set and still an existing directory. Unlike the env var, a ``current``
       that no longer exists is skipped silently rather than raising - it is
       a remembered choice, not an explicit override, and a stale recent
       selection should not block startup.
    3. ``~/YT-Shorts-Data`` if it exists.
    4. the repository's own ``channels/`` - the layout that predates this
       module.

    The parameters exist for testing; production callers pass nothing.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    repo_channels = REPO_CHANNELS if repo_channels is None else repo_channels
    if config_home is None:
        xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
        config_home = Path(xdg) if xdg else home / ".config"

    named = (env.get(ENV_VAR) or "").strip()
    if named:
        # Expand tilde using the home parameter, not Path.expanduser() which uses the
        # machine's home directory. Then resolve relative paths to absolute.
        if named.startswith("~"):
            root = home / named[1:].lstrip("/")
        else:
            root = Path(named)
        root = root.resolve()

        # Distinguish between "doesn't exist" and "exists but not a directory"
        if root.is_dir():
            return Workspace(root=root, channels_dir=root / "channels",
                             origin=ENV_VAR)
        elif root.exists():
            raise WorkspaceError(
                f"{ENV_VAR} points at {root}, which is a file.\n"
                f"It must be a directory. Create a directory at that path,\n"
                f"or unset {ENV_VAR} to use ~/{DEFAULT_DIR_NAME} or the repository's channels/."
            )
        else:
            raise WorkspaceError(
                f"{ENV_VAR} points at {root}, which does not exist.\n"
                f"Create it, or unset {ENV_VAR} to use "
                f"~/{DEFAULT_DIR_NAME} or the repository's channels/."
            )

    current = _workspaces.read_config(config_home).get("current")
    if isinstance(current, str) and current.strip():
        current_root = Path(current).resolve()
        if current_root.is_dir():
            return Workspace(root=current_root, channels_dir=current_root / "channels",
                             origin="config")

    default_root = home / DEFAULT_DIR_NAME
    if default_root.is_dir():
        return Workspace(root=default_root,
                         channels_dir=default_root / "channels",
                         origin="default")
    elif default_root.exists():
        # F3: The default location exists but is not a directory - this is an error
        raise WorkspaceError(
            f"~/{DEFAULT_DIR_NAME} exists but is a file, not a directory.\n"
            f"It must be a directory. Either move or remove it, or set\n"
            f"{ENV_VAR} to point at an actual data directory."
        )

    return Workspace(root=repo_channels.parent, channels_dir=repo_channels,
                     origin="repository")
