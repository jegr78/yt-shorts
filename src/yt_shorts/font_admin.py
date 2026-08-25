"""Upload, list and delete a channel's font files (stage G3b). Pure filesystem
ops plus a PIL load-check that a font is renderable - no FastAPI. A saved font
is one build_overlay can use, because save_font rejects anything ImageFont
cannot load."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import ImageFont

from . import atomicwrite, pathnames

FONT_EXTENSIONS = (".ttf", ".otf")
MAX_FONT_BYTES = 10 * 1024 * 1024   # 10 MB


class FontAdminError(Exception):
    """kind: "bad_name" | "bad_type" | "too_big" | "invalid" | "not_found" | "in_use".
    Maps to HTTP: bad_*/too_big/invalid -> 400, not_found -> 404, in_use -> 409."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _validate_segment(value: str, what: str) -> None:
    try:
        pathnames.validate_segment(value, what=what)
    except ValueError as error:
        raise FontAdminError(str(error), kind="bad_name") from error


def _within(root, *parts: str) -> Path:
    """The containment layer behind _validate_segment - see pathnames.within
    for why it exists when the validation above already covers it."""
    try:
        return pathnames.within(root, *parts)
    except ValueError as error:
        raise FontAdminError(str(error), kind="bad_name") from error


def _fonts_dir(channels_dir, channel: str) -> Path:
    _validate_segment(channel, "channel name")
    base = _within(channels_dir, channel)
    if not base.is_dir():
        raise FontAdminError(f"unknown channel: {channel!r}", kind="not_found")
    return base / "fonts"


def _list_in(fonts: Path) -> list[str]:
    if not fonts.is_dir():
        return []
    return sorted(p.name for p in fonts.iterdir()
                  if p.is_file() and p.suffix.lower() in FONT_EXTENSIONS)


def _save_in(fonts: Path, filename: str, data: bytes) -> None:
    _validate_segment(filename, "font filename")
    if not filename.lower().endswith(FONT_EXTENSIONS):
        raise FontAdminError(
            f"a font must be a {' or '.join(FONT_EXTENSIONS)} file", kind="bad_type")
    if len(data) > MAX_FONT_BYTES:
        raise FontAdminError(
            f"font is larger than {MAX_FONT_BYTES // (1024 * 1024)} MB", kind="too_big")
    try:
        ImageFont.truetype(io.BytesIO(data))
    except Exception as error:   # noqa: BLE001 - any PIL failure means "not a usable font"
        raise FontAdminError(f"not a usable font file: {error}", kind="invalid") from error
    fonts.mkdir(parents=True, exist_ok=True)
    atomicwrite.write_bytes(_within(fonts, filename), data)


def list_fonts(channels_dir, channel: str) -> list[str]:
    return _list_in(_fonts_dir(channels_dir, channel))


def save_font(channels_dir, channel: str, filename: str, data: bytes) -> None:
    _save_in(_fonts_dir(channels_dir, channel), filename, data)


def _brand_assigns_font(brand_path: Path, channel: str, fonts: Path,
                        target: Path, filename: str) -> bool:
    """True if the brand.json at `brand_path` assigns `target` as hook/small.

    Raises FontAdminError(in_use) if the file exists but is unreadable - the
    font may be assigned and we must not risk bricking a profile by deleting it.
    Font refs resolve against the channel `fonts/` dir (same base the channel
    brand uses); an event that shadows the name with its own copy would refuse
    conservatively rather than risk a broken load."""
    if not brand_path.exists():
        return False
    try:
        brand = json.loads(brand_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FontAdminError(
            f"cannot verify font assignments ({channel!r} {brand_path.name} is "
            f"unreadable); fix it first", kind="in_use") from error
    refs = brand.get("fonts", {}) if isinstance(brand, dict) else {}
    for role in ("hook", "small"):
        ref = refs.get(role)
        if not isinstance(ref, str) or not ref.startswith("fonts/"):
            continue
        name = ref[len("fonts/"):]
        assigned = fonts / name
        # Compare by identity, not by raw string: on a case-insensitive
        # filesystem (macOS default) deleting "Font.TTF" removes the file a
        # ref to "Font.ttf" still points at - samefile() sees they share an
        # inode and refuses, where an exact string compare would miss it.
        # If the ref points at a file that no longer exists, fall back to a
        # case-insensitive name compare so a case-variant is still caught.
        try:
            if assigned.samefile(target):
                return True
        except OSError:
            if name.casefold() == filename.casefold():
                return True
    return False


def _assigning_brand_paths(fonts: Path) -> list[Path]:
    """The channel brand.json plus every event brand.json override - all the
    brands profile.load deep-merges, any of which may assign a channel font via
    its fonts.hook/small (the event-level ones were previously not checked, so a
    font referenced only by an event override could be deleted and brick it)."""
    channel_dir = fonts.parent
    paths = [channel_dir / "brand.json"]
    events = channel_dir / "events"
    if events.is_dir():
        paths += [event / "brand.json"
                  for event in sorted(p for p in events.iterdir() if p.is_dir())]
    return paths


def delete_font(channels_dir, channel: str, filename: str) -> None:
    fonts = _fonts_dir(channels_dir, channel)
    _validate_segment(filename, "font filename")
    target = _within(fonts, filename)
    if not target.is_file():
        raise FontAdminError(f"unknown font: {filename!r}", kind="not_found")
    for brand_path in _assigning_brand_paths(fonts):
        if _brand_assigns_font(brand_path, channel, fonts, target, filename):
            raise FontAdminError(
                f"font {filename!r} is assigned in {brand_path.name}; "
                f"reassign it first", kind="in_use")
    target.unlink()


def _event_fonts_dir(channels_dir, channel: str, event: str) -> Path:
    _validate_segment(channel, "channel name")
    _validate_segment(event, "event name")
    base = _within(channels_dir, channel, "events", event)
    if not base.is_dir():
        raise FontAdminError(f"unknown event: {event!r}", kind="not_found")
    return base / "fonts"


def list_event_fonts(channels_dir, channel: str, event: str) -> list[str]:
    return _list_in(_event_fonts_dir(channels_dir, channel, event))


def save_event_font(channels_dir, channel: str, event: str, filename: str, data: bytes) -> None:
    _save_in(_event_fonts_dir(channels_dir, channel, event), filename, data)


def delete_event_font(channels_dir, channel: str, event: str, filename: str) -> None:
    fonts = _event_fonts_dir(channels_dir, channel, event)
    _validate_segment(filename, "font filename")
    target = _within(fonts, filename)
    if not target.is_file():
        raise FontAdminError(f"unknown font: {filename!r}", kind="not_found")
    # The event's OWN brand.json is the only brand that can assign an event font.
    brand_path = fonts.parent / "brand.json"
    if _brand_assigns_font(brand_path, f"{channel}/{event}", fonts, target, filename):
        raise FontAdminError(
            f"font {filename!r} is assigned in {brand_path.name}; reassign it first",
            kind="in_use")
    target.unlink()
