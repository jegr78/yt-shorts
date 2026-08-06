#!/usr/bin/env python3
"""Standalone probe: does owner-only file protection actually work on Windows?

Self-contained on purpose - no yt_shorts import, no venv, no repo checkout
needed. Copy this one file to a Windows machine and run it:

    python check-windows-acl.py

It answers the question CI cannot: what `icacls` really does to a file and a
directory, whether the writing process keeps its own access afterwards, and
whether another account would be refused. Every step prints what it did, so a
failure says which call was wrong rather than just that something was.
"""

import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT = 30


def user() -> str:
    return os.environ.get("USERNAME") or getpass.getuser()


def run(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT)
    print(f"    $ {' '.join(args)}")
    print(f"      rc={result.returncode}")
    for line in (result.stdout or "").splitlines():
        print(f"      | {line}")
    if result.returncode != 0 and result.stderr.strip():
        print(f"      ! {result.stderr.strip()}")
    return result


def restrict(target: Path) -> bool:
    """What ownermode.restrict does on Windows. Returns True on success."""
    rights = "(OI)(CI)F" if target.is_dir() else "F"
    result = run("icacls", str(target), "/inheritance:r", "/grant:r", f"{user()}:{rights}")
    return result.returncode == 0


def main() -> int:
    if os.name != "nt":
        print("This probe only means anything on Windows.")
        return 2

    print(f"user   : {user()}")
    print(f"domain : {os.environ.get('USERDOMAIN', '(none)')}")
    print(f"python : {sys.version.split()[0]}\n")

    root = Path(tempfile.mkdtemp(prefix="acl-probe-"))
    failures = []

    print("[1] a DIRECTORY, restricted")
    directory = root / "auth"
    directory.mkdir()
    if not restrict(directory):
        failures.append("restricting a directory failed")
    print("    who has access now:")
    run("icacls", str(directory))

    print("\n[2] writing INTO that directory afterwards")
    inside = directory / "token.json"
    try:
        inside.write_text('{"token": "x"}', encoding="utf-8")
        print("    ok - the process can still write into its own restricted dir")
    except OSError as error:
        failures.append(f"cannot write into the restricted directory: {error}")
        print(f"    FAILED: {error}")

    print("\n[3] a FILE, restricted")
    if inside.exists():
        if not restrict(inside):
            failures.append("restricting a file failed")
        run("icacls", str(inside))

        print("\n[4] reading and REWRITING the restricted file")
        try:
            print(f"    read back: {inside.read_text(encoding='utf-8')!r}")
            inside.write_text('{"token": "y"}', encoding="utf-8")
            print("    ok - still readable and writable by its owner")
        except OSError as error:
            failures.append(f"lost access to the restricted file: {error}")
            print(f"    FAILED: {error}")

    print("\n[5] os.chmod, for comparison - this is what the code used to rely on")
    plain = root / "chmod-only.json"
    plain.write_text("{}", encoding="utf-8")
    os.chmod(plain, 0o600)
    mode = oct(plain.stat().st_mode & 0o777)
    print(f"    after chmod(0o600) the mode reads back as {mode}")
    if mode != "0o600":
        print("    ^ this is the whole problem: chmod does not restrict on Windows")

    print("\n[6] does anyone besides the owner still have access?")
    result = run("icacls", str(inside if inside.exists() else directory))
    granted = [
        line.strip() for line in (result.stdout or "").splitlines()[0:]
        if ":" in line and line.strip()
    ]
    outsiders = [
        g for g in granted
        if not any(name.lower() in g.lower()
                   for name in (user(), "Administrators", "SYSTEM", str(root)))
    ]
    if outsiders:
        failures.append(f"other principals still have access: {outsiders}")
        print(f"    STILL GRANTED TO: {outsiders}")
    else:
        print("    nobody outside owner/Administrators/SYSTEM")

    print("\n" + "=" * 60)
    if failures:
        print("RESULT: NOT owner-only. Problems found:")
        for f in failures:
            print(f"  - {f}")
    else:
        print("RESULT: owner-only protection works as intended.")
    print(f"\n(probe directory left at {root} - delete it when done)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
