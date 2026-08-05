"""Loads a brand.json file and makes its font paths absolute.

Knows no fixed channel: the path to brand.json and the folder against
which relative font paths are resolved are handed in to load(). yt_shorts.
profile calls this for the respective channel folder; calling it by hand
(e.g. in a test) works just as well.
"""

from __future__ import annotations

import json
from pathlib import Path


def load(path: str | Path, basis: str | Path | None = None) -> dict:
    """Loads brand.json from path. Relative font paths are made absolute:
    against 'basis' if given, otherwise against the folder of path."""
    file = Path(path)
    base_dir = Path(basis) if basis is not None else file.parent
    data = json.loads(file.read_text(encoding="utf-8"))
    for key, value in data.get("fonts", {}).items():
        p = Path(value)
        data["fonts"][key] = str(p if p.is_absolute() else base_dir / p)
    return data
