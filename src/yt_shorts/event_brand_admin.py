"""Read and update an event's brand.json OVERRIDE - the partial layer
profile.load deep-merges over the channel brand. Pure, no FastAPI. Unlike
brand_admin (which validates a COMPLETE channel brand), this validates the
MERGED result (channel + override) and stores only the overridden sections; a
fully-inherited event has no brand.json at all.

OVERRIDE_SECTIONS below is the authority on what may be overridden, and this
sentence is the prose that must match it: colors, fonts, logo, output,
subtitles and bands. Two brand sections are deliberately absent from it, and
`update_event_brand` refuses either by name rather than dropping it - `upload`
(which channel a short is published to) and `detect` (which provider scores
its moments). Both are ACCOUNT-scoped: they decide whose credentials, whose
quota and whose bill an operation spends, which is a property of the channel,
not of one event's look. The studio's PUT route refuses both a step earlier,
so a request naming one never reaches here as a silent no-op."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import ImageColor

from . import atomicwrite, brand_admin, pathnames
from .merge import deep_merge

OVERRIDE_SECTIONS = ("colors", "fonts", "logo", "output", "subtitles", "bands")


class EventBrandError(Exception):
    """kind: bad_name | not_found | bad_field | bad_color | bad_font |
    bad_subtitles | bad_brand. HTTP: not_found -> 404, everything else -> 400."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _dirs(channels_dir, channel: str, event: str) -> tuple[Path, Path]:
    for value, what in ((channel, "channel name"), (event, "event name")):
        try:
            pathnames.validate_segment(value, what=what)
        except ValueError as error:
            raise EventBrandError(str(error), kind="bad_name") from error
    channel_dir = Path(channels_dir) / channel
    return channel_dir, channel_dir / "events" / event


def _load_json(path: Path, label: str, *, optional: bool) -> dict:
    if not path.exists():
        if optional:
            return {}
        raise EventBrandError(f"{label} not found", kind="not_found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EventBrandError(f"{label} is unreadable: {error}", kind="not_found") from error


def resolve_event_font_ref(event_dir: Path, channel_dir: Path, ref, *, what: str = "font") -> Path:
    """Event-first font resolver: a ref must be 'fonts/<safe-segment>' whose
    file exists under the event's fonts/ dir, else the channel's - mirroring
    profile._resolve_relative's event-first precedence but for validation
    (raises rather than returning a not-yet-checked path)."""
    if not ref or not isinstance(ref, str) or not ref.startswith("fonts/"):
        raise EventBrandError(f"{what} must be 'fonts/<file>'", kind="bad_font")
    name = ref[len("fonts/"):]
    try:
        pathnames.validate_segment(name, what="font filename")
    except ValueError as error:
        raise EventBrandError(f"{what} name is invalid: {name!r}", kind="bad_font") from error
    for base in (event_dir, channel_dir):
        candidate = base / "fonts" / name
        if candidate.is_file():
            return candidate
    raise EventBrandError(f"{what} file not found: {ref!r}", kind="bad_font")


def read_event_brand(channels_dir, channel: str, event: str) -> dict:
    channel_dir, event_dir = _dirs(channels_dir, channel, event)
    if not event_dir.is_dir():
        raise EventBrandError(f"unknown event: {event!r}", kind="not_found")
    channel_brand = _load_json(channel_dir / "brand.json", "channel brand.json", optional=False)
    override = _load_json(event_dir / "brand.json", "event brand.json", optional=True)
    return {"override": override, "channel": channel_brand,
            "effective": deep_merge(channel_brand, override)}


def update_event_brand(channels_dir, channel: str, event: str, patch: dict) -> None:
    channel_dir, event_dir = _dirs(channels_dir, channel, event)
    if not event_dir.is_dir():
        raise EventBrandError(f"unknown event: {event!r}", kind="not_found")
    for key in patch:
        if key not in OVERRIDE_SECTIONS:
            raise EventBrandError(
                f"{key!r} cannot be overridden at the event level", kind="bad_field")
    channel_brand = _load_json(channel_dir / "brand.json", "channel brand.json", optional=False)
    merged = deep_merge(channel_brand, patch)
    _validate_merged(merged, event_dir, channel_dir)
    path = event_dir / "brand.json"
    if patch:
        atomicwrite.write_text(path, json.dumps(patch, indent=2) + "\n")
    elif path.exists():
        path.unlink()


def _validate_merged(merged: dict, event_dir: Path, channel_dir: Path) -> None:
    """Mirrors brand_admin._validate but resolves fonts/logo event-first (see
    resolve_event_font_ref) and never runs profile._validate_upload - upload
    is not an event-overridable section, so the merged dict's upload (always
    the channel's, untouched by any patch) is left unvalidated here; the
    channel brand editor is the only place that can be wrong about it."""
    colors = merged.get("colors")
    if not isinstance(colors, dict):
        raise EventBrandError("the 'colors' section is required", kind="bad_color")
    for key in brand_admin.REQUIRED_COLOR_KEYS:
        value = colors.get(key)
        if not value:
            raise EventBrandError(f"color {key!r} is required", kind="bad_color")
        try:
            ImageColor.getrgb(value)
        except ValueError as error:
            raise EventBrandError(
                f"color {key!r} is not a valid color: {value!r}", kind="bad_color") from error

    fonts = merged.get("fonts")
    if not isinstance(fonts, dict):
        raise EventBrandError("the 'fonts' section is required", kind="bad_font")
    resolved_fonts = {
        key: str(resolve_event_font_ref(event_dir, channel_dir, fonts.get(key),
                                        what=f"font {key!r}"))
        for key in brand_admin.REQUIRED_FONT_KEYS}

    # Subtitles/output/logo validation mirrors profile.load's own checks
    # exactly (imported lazily, same reasoning as brand_admin._validate: keeps
    # this module's import light and avoids a cycle), so a merged brand this
    # accepts is one profile.load accepts.
    from . import profile
    brand_path = event_dir / "brand.json"
    problems = profile._validate_subtitles(merged, brand_path)
    if problems:
        raise EventBrandError(problems[0], kind="bad_subtitles")

    resolved = {**merged, "fonts": resolved_fonts}
    profile._resolve_logo(resolved, event_dir, channel_dir)
    problems = (profile._validate_brand(resolved, brand_path)
                + profile._validate_logo(resolved, brand_path))
    if problems:
        raise EventBrandError(problems[0], kind="bad_brand")
