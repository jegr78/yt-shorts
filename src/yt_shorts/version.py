"""The version to report, and the one exception a frozen build makes.

`__version__.py` carries the number release-please writes, and hatchling reads
its version from that same file. A PyInstaller build is the exception:
`tools/build-binary.py` is handed the git TAG (`v1.2.0`), writes it into the
bundle as a `VERSION` file, and this resolver prefers that file when frozen.
Without it, a binary built from tag `v1.2.0` would report `1.2.0`, and the
build's own smoke test - whose whole job is to prove the artefact knows what it
is - could not be written honestly. Ported from racecast, unchanged in spirit.
"""

import sys
from pathlib import Path

from yt_shorts.__version__ import __version__ as _declared


def bundled_version_path() -> Path | None:
    """The `VERSION` file inside a PyInstaller bundle, or None when not frozen."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "VERSION" if base else None


def resolve(read=None) -> str:
    """The version to report. A readable, non-empty bundled `VERSION` wins;
    otherwise the declared package version. Never raises - a version string is
    not worth failing a command over. `read` is the injectable seam for tests."""
    path = bundled_version_path()
    if path is not None:
        read = read or (lambda p: p.read_text(encoding="utf-8"))
        try:
            text = read(path).strip()
        except OSError:
            text = ""
        if text:
            return text
    return _declared
