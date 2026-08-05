"""Record that a clip was uploaded, so it is not uploaded twice by accident.

upload.json records an ACTION - it is neither derived (cannot be recreated) nor
editorial (not a human's correction), so it is its own file. An upload has an
irreversible external effect and costs scarce quota; the studio and CLI read this
to show 'uploaded' and refuse a second upload without an explicit force.
"""

from __future__ import annotations

import json
from pathlib import Path

RECORD_FILENAME = "upload.json"


def record_path(clip_dir) -> Path:
    return Path(clip_dir) / RECORD_FILENAME


def load(clip_dir) -> dict | None:
    path = record_path(clip_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_uploaded(clip_dir) -> bool:
    return load(clip_dir) is not None


def save(clip_dir, video_id, url, privacy, *, when: str) -> None:
    record_path(clip_dir).write_text(json.dumps({
        "video_id": video_id, "url": url, "privacy": privacy, "uploaded_at": when,
    }, indent=2), encoding="utf-8")
