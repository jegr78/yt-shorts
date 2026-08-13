"""Read and update a channel's brand.json colors/fonts/subtitles (stage G3b).
Pure, no FastAPI. Validation mirrors profile._validate_brand so a brand this
accepts is one profile.load accepts. Output dimensions are never changed here."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import ImageColor

from . import atomicwrite, pathnames

REQUIRED_COLOR_KEYS = ["text", "base", "accent", "edge"]
REQUIRED_FONT_KEYS = ["hook", "small"]


class BrandAdminError(Exception):
    """kind: "bad_name" | "not_found" | "bad_color" | "bad_font" | "bad_subtitles" |
    "bad_detect".
    Maps to HTTP: bad_* -> 400, not_found -> 404."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _channel_dir(channels_dir, channel: str) -> Path:
    try:
        pathnames.validate_segment(channel, what="channel name")
    except ValueError as error:
        raise BrandAdminError(str(error), kind="bad_name") from error
    try:
        return pathnames.within(channels_dir, channel)
    except ValueError as error:
        raise BrandAdminError(str(error), kind="bad_name") from error


def _load(path: Path, channel: str) -> dict:
    if not path.exists():
        raise BrandAdminError(f"unknown channel or brand: {channel!r}", kind="not_found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrandAdminError(
            f"brand.json for {channel!r} is unreadable: {error}", kind="not_found") from error


def read_brand(channels_dir, channel: str) -> dict:
    base = _channel_dir(channels_dir, channel)
    return _load(base / "brand.json", channel)


def resolve_font_ref(channel_dir: Path, ref, *, what: str = "font") -> Path:
    """Validate a brand font reference and return the file's path under
    ``channel_dir/fonts``. A ref must be ``fonts/<safe-segment>``
    (``pathnames.validate_segment``) whose file exists - so a client-supplied
    ref can never reach the filesystem as an absolute or ``..``-escaping path
    (``Path("channel/fonts") / "/etc/passwd"`` would discard the base, and
    ``fonts/../..`` would climb out). Both the PUT/save validator and the live
    preview route go through this one function, so neither can open a font path
    the other would reject. Raises ``BrandAdminError(kind="bad_font")``."""
    if not ref or not isinstance(ref, str) or not ref.startswith("fonts/"):
        raise BrandAdminError(f"{what} must be 'fonts/<file>'", kind="bad_font")
    name = ref[len("fonts/"):]
    try:
        pathnames.validate_segment(name, what="font filename")
    except ValueError as error:
        raise BrandAdminError(f"{what} name is invalid: {name!r}", kind="bad_font") from error
    path = channel_dir / "fonts" / name
    if not path.is_file():
        raise BrandAdminError(f"{what} file not found: {ref!r}", kind="bad_font")
    return path


ALLOWED_MODES = ("api", "manual")


def update_brand(channels_dir, channel: str, patch: dict) -> None:
    base = _channel_dir(channels_dir, channel)
    path = base / "brand.json"
    brand = _load(path, channel)
    # The studio may edit every renderable section of the brand: colors, fonts,
    # subtitles, plus the logo, the video-window geometry (output) and the
    # upload class - and, non-renderable but still brand-scoped, the bands
    # opacity and which moment-detection provider/model the channel uses.
    # _validate below runs the SAME checks profile.load does over the whole
    # merged brand, so a patch this accepts is one profile.load accepts - the
    # "accepted => loadable" invariant holds for every section.
    for key in ("colors", "fonts", "subtitles", "logo", "output", "upload", "bands", "detect"):
        if key in patch:
            brand[key] = patch[key]
    _validate(brand, base)
    atomicwrite.write_text(path, json.dumps(brand, indent=2) + "\n")


def set_upload_mode(channels_dir, channel: str, mode: str) -> None:
    """Sets ONLY brand.json's upload.mode ("api"/"manual"), the channel's upload
    class. Deliberately does NOT run the full-brand validation update_brand does:
    the upload class is independent of a channel's aesthetics, so it must be
    settable on a channel whose brand is otherwise still incomplete (a freshly
    created channel has no fonts yet). The Settings screen's api/manual toggle is
    the one caller."""
    if mode not in ALLOWED_MODES:
        raise BrandAdminError(
            f"upload mode must be one of {', '.join(ALLOWED_MODES)}", kind="bad_field")
    base = _channel_dir(channels_dir, channel)
    path = base / "brand.json"
    brand = _load(path, channel)
    upload = brand.get("upload")
    brand["upload"] = {**upload, "mode": mode} if isinstance(upload, dict) else {"mode": mode}
    atomicwrite.write_text(path, json.dumps(brand, indent=2) + "\n")


def _validate(brand: dict, channel_dir: Path) -> None:
    colors = brand.get("colors")
    if not isinstance(colors, dict):
        raise BrandAdminError("the 'colors' section is required", kind="bad_color")
    for key in REQUIRED_COLOR_KEYS:
        value = colors.get(key)
        if not value:
            raise BrandAdminError(f"color {key!r} is required", kind="bad_color")
        try:
            ImageColor.getrgb(value)
        except ValueError as error:
            raise BrandAdminError(
                f"color {key!r} is not a valid color: {value!r}", kind="bad_color") from error

    fonts = brand.get("fonts")
    if not isinstance(fonts, dict):
        raise BrandAdminError("the 'fonts' section is required", kind="bad_font")
    resolved_fonts = {
        key: str(resolve_font_ref(channel_dir, fonts.get(key), what=f"font {key!r}"))
        for key in REQUIRED_FONT_KEYS}

    # Subtitles geometry (size/y and the caption-box collision check) is NOT
    # duplicated here: it is validated by the SAME function profile.load runs
    # (profile._validate_subtitles), so a subtitles block this accepts is one
    # profile.load accepts - the invariant "a saved brand is a loadable brand"
    # cannot drift out of sync with a hand-maintained copy. Imported lazily to
    # keep this admin module's import light and free of any future cycle.
    from . import profile
    brand_path = channel_dir / "brand.json"
    problems = profile._validate_subtitles(brand, brand_path)
    if problems:
        raise BrandAdminError(problems[0], kind="bad_subtitles")

    # Same borrowing as subtitles above: profile owns the rule, so a detect
    # section this accepts is one profile.load accepts.
    problems = profile._validate_detect(brand, brand_path)
    if problems:
        raise BrandAdminError(problems[0], kind="bad_detect")

    # output/logo/upload ARE editable via update_brand (and a pre-existing broken
    # value on disk must be caught too), so validate them exactly the way
    # profile.load does, against a copy with fonts (and logo) resolved to real
    # paths so the output integer check and the logo-file existence check run the
    # same logic. This is what keeps "a brand this accepts is one profile.load
    # accepts" true for the output/logo/upload sections as well.
    resolved = {**brand, "fonts": resolved_fonts}
    profile._resolve_logo(resolved, channel_dir, channel_dir)
    problems = (profile._validate_brand(resolved, brand_path)
                + profile._validate_logo(resolved, brand_path)
                + profile._validate_upload(resolved, brand_path))
    if problems:
        raise BrandAdminError(problems[0], kind="bad_brand")
