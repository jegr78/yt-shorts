"""Restrict a secret file or directory to its owner, on POSIX and on Windows.

`auth/` holds OAuth refresh tokens and provider API keys, so it must not be
readable by other accounts. `os.chmod` delivers that on POSIX and does almost
nothing on Windows, where it only toggles the read-only bit - a mode that reads
back as 0o666/0o777 no matter what was asked for. Windows access control is
ACL-based, so the equivalent is done with `icacls`: drop inherited entries and
grant the current user alone.

STDLIB ONLY and no project imports, like logsetup.py: this is reachable from
the CLI, which runs in a venv that may have installed neither FastAPI nor
anything else.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path

WINDOWS = os.name == "nt"
DIR_MODE = 0o700
FILE_MODE = 0o600
_TIMEOUT_SECONDS = 30


class OwnerModeError(Exception):
    """Owner-only access could not be established."""


def _current_user() -> str:
    return os.environ.get("USERNAME") or getpass.getuser()


def restrict(path: Path | str) -> None:
    """Make `path` accessible to its owner only. Raises OwnerModeError if that
    cannot be established - a secret written world-readable must not pass
    silently."""
    target = Path(path)
    if not WINDOWS:
        os.chmod(target, DIR_MODE if target.is_dir() else FILE_MODE)
        return
    # /inheritance:r drops the entries inherited from the parent (which is what
    # would otherwise leave Users with read access); /grant:r replaces rather
    # than adds, so a repeated call is idempotent.
    result = subprocess.run(
        ["icacls", str(target), "/inheritance:r", "/grant:r", f"{_current_user()}:(OI)(CI)F"],
        capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise OwnerModeError(
            f"could not restrict {target} to its owner: {result.stderr.strip()}"
        )


def is_owner_only(path: Path | str) -> bool:
    """True iff `path` grants access to nobody but its owner (and, on Windows,
    the built-in administrative accounts, which can take ownership anyway)."""
    target = Path(path)
    if not WINDOWS:
        # No group or other bits at all. Deliberately not an equality check
        # against 0o600: the managed yt-dlp binary is 0o700 because it has to
        # be executable, and it is no less owner-only for that.
        return (target.stat().st_mode & 0o077) == 0
    result = subprocess.run(["icacls", str(target)], capture_output=True, text=True,
                            timeout=_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return False
    granted = {
        line.split(":", 1)[0].strip()
        for line in result.stdout.splitlines()[1:]
        if ":" in line and line.strip()
    }
    granted.discard(str(target))
    allowed = {_current_user(), "BUILTIN\\Administrators", "NT AUTHORITY\\SYSTEM"}
    return bool(granted) and granted <= {a for a in allowed} | {
        f"{os.environ.get('USERDOMAIN', '')}\\{_current_user()}"
    }
