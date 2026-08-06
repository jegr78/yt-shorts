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
    # than adds, so a repeated call is idempotent. The (OI)(CI) inheritance
    # flags are DIRECTORY-only: passing them for a file leaves the grant
    # unapplied while the inherited rights are already gone, which locked the
    # writing process itself out.
    rights = "(OI)(CI)F" if target.is_dir() else "F"
    # `*<SID>` rather than a user name: icacls takes either, and the SID cannot
    # be wrong about casing, machine prefix or locale.
    own = _powershell(_PS_OWN_SID)
    principal = f"*{own[0]}" if own else _current_user()
    # /reset FIRST. /inheritance:r only drops INHERITED entries and /grant:r
    # only replaces the named principal's own - an explicit ACE for anyone else
    # (a directory someone granted Everyone) would survive both, so a loose
    # auth/ would not actually be repaired. /reset clears the explicit ACEs
    # back to inherited, which /inheritance:r then removes in turn.
    subprocess.run(["icacls", str(target), "/reset"],
                   capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
    result = subprocess.run(
        ["icacls", str(target), "/inheritance:r", "/grant:r", f"{principal}:{rights}"],
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
    granted = _granted_sids(target)
    return bool(granted) and granted <= _allowed_sids()


# Well-known SIDs, identical on every Windows in every language - which is the
# entire reason this compares SIDs rather than the names icacls prints. Those
# are localised (NT-AUTORITÄT\SYSTEM, Administratoren), so any string match on
# them breaks on a German, French or Japanese install.
_SID_SYSTEM = "S-1-5-18"
_SID_ADMINISTRATORS = "S-1-5-32-544"
_SID_OWNER_RIGHTS = "S-1-3-4"   # rights of whoever OWNS the object

_PS_ACL_SIDS = (
    "(Get-Acl -LiteralPath '{path}').Access | ForEach-Object {{ "
    "$_.IdentityReference.Translate("
    "[System.Security.Principal.SecurityIdentifier]).Value }}"
)
_PS_OWN_SID = "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"


def _powershell(script: str) -> list[str]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _granted_sids(target: Path) -> set[str]:
    return set(_powershell(_PS_ACL_SIDS.format(path=str(target).replace("'", "''"))))


def _allowed_sids() -> set[str]:
    """The owner, plus SYSTEM and Administrators - neither of which can
    meaningfully be excluded, since both can take ownership regardless."""
    own = _powershell(_PS_OWN_SID)
    return {_SID_SYSTEM, _SID_ADMINISTRATORS, _SID_OWNER_RIGHTS} | set(own)
