"""Resolves an identifier 'channel/event': paths + channel/event profile loading.

A channel folder under channels/<channel>/ contains:
  channel.json  Channel ID, handle, language, footer, display name (identity)
  brand.json    Colors, fonts, output dimensions (appearance)
  glossary.json optional: proper nouns Whisper doesn't know (see
                yt_shorts.glossary) - biases transcription and corrects it
  fonts/        this channel's fonts
  assets/       optional: this channel's assets (e.g. a logo)
  layout.py     optional: function 'decorate' for the channel-specific
                accent element (see yt_shorts.overlay.build_overlay)
  events/<event>/   sources.json, clips/<clip>/{clip.json, edit.json,
                    transcript.json, raw.mp4, short.mp4, short.full.mp4,
                    short.trim.json}
                    short.mp4 is always the deliverable, already cut if a
                    trim is applied; short.full.mp4 is the untrimmed
                    master, present only while a trim is applied (a
                    re-render recreates it); short.trim.json records which
                    trim short.mp4 currently embodies (see yt_shorts.trim)

An event folder MAY additionally carry its own brand.json, glossary.json,
fonts/, assets/ and layout.py - all optional. One channel does not have one
brand: it has one per event (e.g. green 24h branding for one event, a
different look for another on the same channel). Resolution order:

  value      ->  event profile   ->  channel profile  ->  built-in default
  font file  ->  event/fonts/    ->  channel/fonts/
  layout.py  ->  event/          ->  channel/          ->  plain bars
  logo file  ->  event/assets/   ->  channel/assets/    ->  none
  glossary   ->  MERGED: default + the event's track pack + workspace
                 + channel + event

The merge is a deep merge per key (see yt_shorts.merge.deep_merge), event
wins: an event naming only colors.accent keeps every other channel color,
font and output dimension untouched. An event with none of these files
behaves exactly as the channel does.

The glossary and the lexicon are the two exceptions to that deep merge: both
are merged ENTRY BY ENTRY across additive layers, most specific winning, with
a falsy value disabling an inherited entry. The lexicon has four (built-in
default, workspace, channel, event); the glossary has five, because between
its (now empty) default and the workspace sits the vocabulary of whichever
circuit the EVENT named with "track" - see _load_glossary and _load_lexicon.

All error cases (unknown channel, unknown event, missing profile file, or
an incomplete profile) are raised as a ProfileError with a message a human
can understand, instead of a raw traceback.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .glossary import Glossary
from .lexicon import DEFAULT_MARKERS as LEXICON_DEFAULT_MARKERS
from .lexicon import Lexicon
from .lexicon import load as _lexicon_load
from .merge import deep_merge
from .overlay import BAND_KEYS, LOGO_POSITIONS, band_opacities, caption_geometry, validate_caption_box
from .pathnames import validate_segment
from . import glossary as glossary_module
from . import providers
from . import tracks
from . import workspace
from .workspace import resolve as _resolve_workspace

ROOT = Path(__file__).resolve().parent.parent.parent
# Resolved once at import: which dataset a process works on must not change
# halfway through a run. Tests override this attribute directly.
CHANNELS_DIR = _resolve_workspace().channels_dir

DecorateFunction = Callable[..., None]

# What load() requires to be present before it will trust a profile.
REQUIRED_CHANNEL_FIELDS = ["id", "channel_url", "handle", "display_name", "language", "footer"]
REQUIRED_COLOR_KEYS = ["text", "base", "accent", "edge"]
REQUIRED_FONT_KEYS = ["hook", "small"]
REQUIRED_OUTPUT_KEYS = ["width", "height", "video_width", "video_height", "video_y"]


class ProfileError(Exception):
    """Understandable error message about a broken or unknown profile."""


@dataclass
class Profile:
    identifier: str
    channel_name: str
    event_name: str
    channel_dir: Path
    event_dir: Path
    channel: dict
    config: dict  # brand data (brand.json), font paths absolute, decorate if any,
    # config["glossary"] is always a yt_shorts.glossary.Glossary, merged from
    # five layers (see _load_glossary) - but it IS empty for an event that
    # names no track and has no glossary.json above it, because the built-in
    # default no longer carries any entries of its own


def _existing_dirs(dir_: Path) -> str:
    if not dir_.is_dir():
        return "(folder does not exist)"
    names = sorted(p.name for p in dir_.iterdir() if p.is_dir())
    return ", ".join(names) if names else "(none)"


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        raise ProfileError(f"{label} missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ProfileError(f"{label} is not valid JSON: {path}\n{error}") from error


def _load_optional_json(path: Path, label: str) -> dict:
    """Like _load_json, but a simply-absent file returns {} instead of
    raising - used for the optional event-level brand.json override."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ProfileError(f"{label} is not valid JSON: {path}\n{error}") from error


def _resolve_relative(value: str, event_dir: Path, channel_dir: Path) -> str:
    """Resolves a relative path against the event folder first, then the
    channel folder - so an event can name a channel file (e.g. a font it
    doesn't want to copy) without duplicating it. An absolute path is
    returned unchanged. If neither location has the file, the
    channel-relative path is returned anyway, so a subsequent existence
    check names the expected default location instead of the event's."""
    p = Path(value)
    if p.is_absolute():
        return str(p)
    event_candidate = event_dir / p
    if event_candidate.exists():
        return str(event_candidate)
    return str(channel_dir / p)


def _resolve_fonts(fonts, event_dir: Path, channel_dir: Path):
    """Resolves every font path in the merged fonts section. Left untouched
    (for _validate_brand to report) if 'fonts' isn't even a dict."""
    if not isinstance(fonts, dict):
        return fonts
    return {
        key: _resolve_relative(value, event_dir, channel_dir) if isinstance(value, str) else value
        for key, value in fonts.items()
    }


# The logo variants a channel/event may pick between. "color" is the base
# `logo.file`; "white"/"black" are the sibling monochrome cutouts named by
# convention ('<stem>-white.png' / '<stem>-black.png'), so an event can switch
# variant with just {"logo": {"variant": "white"}} and inherit everything else.
LOGO_VARIANTS = ("color", "white", "black")


def _variant_file(file_: str, variant) -> str:
    """The file for a logo variant: the base file for "color" (or anything not a
    known monochrome variant), else its '<stem>-<variant>' sibling in the same
    directory."""
    if variant not in ("white", "black"):
        return file_
    p = Path(file_)
    # as_posix(), not str(): this is a brand.json-style ref like
    # "assets/logo.png", and str() would hand back backslashes on Windows.
    return p.with_name(f"{p.stem}-{variant}{p.suffix}").as_posix()


def _resolve_logo(config: dict, event_dir: Path, channel_dir: Path) -> None:
    """Resolves config['logo']['file'] in place, event/assets/ before
    channel/assets/, mirroring font resolution, and applies the logo variant
    (color/white/black) naming convention first. No-op without a 'logo' key."""
    logo = config.get("logo")
    if not isinstance(logo, dict):
        return
    file_ = logo.get("file")
    if isinstance(file_, str):
        variant_file = _variant_file(file_, logo.get("variant", "color"))
        config["logo"] = {**logo, "file": _resolve_relative(variant_file, event_dir, channel_dir)}


def _validate_channel(channel: dict, path: Path) -> list[str]:
    """Collects every missing mandatory field instead of stopping at the first."""
    problems = []
    for field in REQUIRED_CHANNEL_FIELDS:
        if not channel.get(field):
            problems.append(f"{path.name}: missing required field '{field}'")
    return problems


def _validate_brand(config: dict, path: Path) -> list[str]:
    """Collects every missing color, font, and output dimension instead of
    stopping at the first - a raw KeyError on the first typo (e.g. an
    old-style color name) tells whoever is typing up a profile neither
    which file is meant nor what is expected."""
    problems = []

    colors = config.get("colors")
    if not isinstance(colors, dict):
        problems.append(f"{path.name}: missing section 'colors'")
    else:
        for key in REQUIRED_COLOR_KEYS:
            if not colors.get(key):
                problems.append(f"{path.name}: missing color 'colors.{key}'")

    fonts = config.get("fonts")
    if not isinstance(fonts, dict):
        problems.append(f"{path.name}: missing section 'fonts'")
    else:
        for key in REQUIRED_FONT_KEYS:
            font_path = fonts.get(key)
            if not font_path:
                problems.append(f"{path.name}: missing font 'fonts.{key}'")
            elif not Path(font_path).exists():
                problems.append(f"{path.name}: font file for fonts.{key} not found: {font_path}")

    output = config.get("output")
    if not isinstance(output, dict):
        problems.append(f"{path.name}: missing section 'output'")
    else:
        for key in REQUIRED_OUTPUT_KEYS:
            if key not in output:
                problems.append(f"{path.name}: missing output dimension 'output.{key}'")
            elif not isinstance(output[key], int) or isinstance(output[key], bool):
                # Presence alone isn't enough: caption_geometry and
                # validate_caption_box (see _validate_subtitles below) do
                # arithmetic directly on these values at profile.load time,
                # not just at draw time. A string here (e.g. "video_y": "600",
                # a quoting typo) used to load cleanly and then raise a raw
                # TypeError the moment the geometry check ran - and stayed
                # completely silent whenever subtitles were off, since
                # nothing else in this module touches 'output' arithmetically.
                # Naming it here reports it as a ProfileError either way.
                problems.append(
                    f"{path.name}: output.{key} must be an integer, "
                    f"got {output[key]!r}"
                )

        # Geometric bounds - only once every required dimension is a present
        # int, so this never does arithmetic on a missing or typo'd field. The
        # overlay draws the veil around a fixed video window; a window that is
        # non-positive or does not fit inside the frame produces a malformed
        # picture (window_top past the bottom edge, a zero-area video), which
        # used to load cleanly and only surface as a broken render.
        if all(
            isinstance(output.get(key), int) and not isinstance(output.get(key), bool)
            for key in REQUIRED_OUTPUT_KEYS
        ):
            width, height = output["width"], output["height"]
            video_width, video_height, video_y = (
                output["video_width"], output["video_height"], output["video_y"])
            if width <= 0 or height <= 0:
                problems.append(
                    f"{path.name}: output.width and output.height must be positive")
            if not 0 < video_width <= width:
                problems.append(
                    f"{path.name}: output.video_width ({video_width}) must be between "
                    f"1 and output.width ({width})")
            if video_height <= 0:
                problems.append(
                    f"{path.name}: output.video_height must be positive")
            if video_y < 0 or video_y + video_height > height:
                problems.append(
                    f"{path.name}: the video window (output.video_y={video_y} + "
                    f"output.video_height={video_height}) must fit inside the frame "
                    f"height ({height})")

    # 'bands' is OPTIONAL at every layer: an absent section, an absent key
    # and 1.0 are the same request, so nothing written before this feature
    # existed becomes invalid. Only a PRESENT value is checked.
    bands = config.get("bands")
    if bands is not None:
        if not isinstance(bands, dict):
            problems.append(
                f"{path.name}: 'bands' must be an object with 'top'/'bottom', "
                f"got {bands!r}")
        else:
            for key in bands:
                if key not in BAND_KEYS:
                    problems.append(
                        f"{path.name}: unknown band 'bands.{key}' "
                        f"(expected {' or '.join(BAND_KEYS)})")
            for key in BAND_KEYS:
                if key not in bands:
                    continue
                value = bands[key]
                # bool before number: True is an int in Python, so
                # `"top": true` would otherwise load as full strength.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    problems.append(
                        f"{path.name}: bands.{key} must be a number between 0 and 1, "
                        f"got {value!r}")
                elif not 0.0 <= float(value) <= 1.0:
                    problems.append(
                        f"{path.name}: bands.{key} must be between 0 and 1, "
                        f"got {value!r}")

    return problems


def _validate_logo(config: dict, path: Path) -> list[str]:
    """The optional 'logo' section, if present, needs at least a 'file'
    that exists once resolved (see _resolve_logo, called before this)."""
    logo = config.get("logo")
    if logo is None:
        return []
    if not isinstance(logo, dict):
        return [f"{path.name}: 'logo' must be an object with at least 'file'"]

    problems = []
    file_ = logo.get("file")
    if not file_:
        problems.append(f"{path.name}: missing 'logo.file'")
    elif not Path(file_).exists():
        problems.append(f"{path.name}: logo file not found: {file_}")

    variant = logo.get("variant")
    if variant is not None and variant not in LOGO_VARIANTS:
        problems.append(
            f"{path.name}: logo.variant must be one of "
            f"{', '.join(LOGO_VARIANTS)} (got {variant!r})")

    position = logo.get("position")
    if position is not None and position not in LOGO_POSITIONS:
        problems.append(
            f"{path.name}: logo.position must be one of "
            f"{', '.join(LOGO_POSITIONS)} (got {position!r})")

    opacity = logo.get("opacity")
    if opacity is not None and not (isinstance(opacity, (int, float))
                                    and not isinstance(opacity, bool)
                                    and 0 < opacity <= 1):
        problems.append(
            f"{path.name}: logo.opacity must be a number in (0, 1] (got {opacity!r})")
    return problems


def _validate_subtitles(config: dict, path: Path) -> list[str]:
    """The optional 'subtitles' section, if present, must hold sane values.
    Absent means off, which is the default.

    An explicit ``null`` is NOT treated as absent, even though it also
    means "no subtitles" to a human skimming JSON: downstream,
    ``config.get("subtitles", {})`` (in ``cmd_render`` and in
    ``overlay.build_caption``) only falls back to ``{}`` when the key is
    missing entirely - an explicit ``"subtitles": null`` makes ``.get``
    return ``None`` instead, and every caller then calls ``.get`` on that
    ``None``. Left unrejected here, this validates cleanly and then raises
    ``AttributeError`` on every single clip at render time, which is
    exactly the kind of gap collect-all validation exists to catch before
    a render even starts.
    """
    if "subtitles" not in config:
        return []
    subtitles = config["subtitles"]
    if subtitles is None:
        return [f"{path.name}: 'subtitles' must be an object, not null"]
    if not isinstance(subtitles, dict):
        return [f"{path.name}: 'subtitles' must be an object"]

    problems = []
    if "enabled" in subtitles and not isinstance(subtitles["enabled"], bool):
        problems.append(f"{path.name}: 'subtitles.enabled' must be true or false")
    if "max_words" in subtitles:
        value = subtitles["max_words"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"{path.name}: 'subtitles.max_words' must be an integer of at least 1")
    if "max_seconds" in subtitles:
        value = subtitles["max_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            problems.append(f"{path.name}: 'subtitles.max_seconds' must be a positive number")
    # size_and_y_are_usable tracks only these two fields (not "problems" as
    # a whole, see below) so that an unrelated defect elsewhere in this same
    # block - max_words: 0, say - does not also suppress the geometry check.
    size_and_y_are_usable = True
    for key in ("size", "y"):
        if key in subtitles:
            value = subtitles[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                problems.append(f"{path.name}: 'subtitles.{key}' must be a positive integer")
                size_and_y_are_usable = False

    # The real constraint on subtitles.size/y - that the caption must not
    # overlap the video window or the footer - is enforced by
    # overlay.build_caption, but only per clip, inside the subtitle degrade
    # path (see cmd_render's provider closure): a bad geometry used to load
    # cleanly and only surface after every clip had already paid for a full
    # download and transcription. The geometry is fully known here (output
    # dimensions, footer size, subtitles.size/y - nothing about it depends
    # on a clip), so it is checked with the same rule build_caption itself
    # uses (see overlay.validate_caption_box) before a run ever starts.
    #
    # Gated on subtitles.size/y specifically being known-good, not on
    # "problems" as a whole (see finding C2): a profile with both an
    # unrelated defect (e.g. max_words: 0) and a colliding y must report
    # both in the same run, not just the first - the same collect-all
    # principle every other check in this module already honours. Also
    # gated on the output section being both present and usable - every
    # REQUIRED_OUTPUT_KEYS value actually being an int (see _validate_brand,
    # finding C1: a string dimension is reported there, and must not also
    # be dereferenced here) - and only attempted when subtitles will
    # actually be drawn (explicitly enabled). A profile with no subtitles
    # block, subtitles left disabled, or a broken 'output' section (already
    # reported by _validate_brand) must behave exactly as it does today.
    output = config.get("output")
    output_is_usable = isinstance(output, dict) and all(
        key in output and isinstance(output[key], int) and not isinstance(output[key], bool)
        for key in REQUIRED_OUTPUT_KEYS
    )
    if size_and_y_are_usable and subtitles.get("enabled") is True and output_is_usable:
        top, block_height, explicit_y = caption_geometry(config)
        try:
            validate_caption_box(config, top, block_height, explicit_y)
        except ValueError as error:
            problems.append(f"{path.name}: {error}")

    return problems


def _validate_upload(config: dict, path: Path) -> list[str]:
    """The optional 'upload' section's class flag, if present, must be a known
    value. Absent 'upload' or absent 'mode' means "api" (see
    upload_policy.mode) - the default, which needs no declaration - and every
    existing profile that never wrote an 'upload' block keeps loading. An
    explicit null or a non-object is rejected here the same collect-all way
    _validate_subtitles rejects a null 'subtitles', so a typo is named in the
    one ProfileError alongside every other defect.

    The same section also carries the per-clip upload metadata defaults
    (description/tags/category_id/made_for_kids) that brand_admin.update_brand
    lets the studio edit (Task 7's brand editor). They are validated here too,
    collected the same collect-all way, so a bad default is rejected before it
    can break an upload rather than surfacing only when youtube_upload builds
    metadata from it."""
    if "upload" not in config:
        return []
    upload = config["upload"]
    if upload is None:
        return [f"{path.name}: 'upload' must be an object, not null"]
    if not isinstance(upload, dict):
        return [f"{path.name}: 'upload' must be an object"]
    problems = []
    if "mode" in upload and upload["mode"] not in ("api", "manual"):
        problems.append(
            f"{path.name}: 'upload.mode' must be \"api\" or \"manual\", "
            f"got {upload['mode']!r}"
        )
    if "description" in upload and not isinstance(upload["description"], str):
        problems.append(f"{path.name}: 'upload.description' must be a string")
    if "tags" in upload and not (
        isinstance(upload["tags"], list) and all(isinstance(t, str) for t in upload["tags"])
    ):
        problems.append(f"{path.name}: 'upload.tags' must be a list of strings")
    if "category_id" in upload and not isinstance(upload["category_id"], (str, int)):
        problems.append(f"{path.name}: 'upload.category_id' must be a string or number")
    if "made_for_kids" in upload and not isinstance(upload["made_for_kids"], bool):
        problems.append(f"{path.name}: 'upload.made_for_kids' must be a boolean")
    return problems


def _validate_detect(brand: dict, path: Path) -> list[str]:
    """The `detect` section: which provider scores moments, and with what
    model. Both optional - an absent section means Anthropic's default, which
    is what every profile written before this existed gets. An explicit null
    means the same, because `config.get("detect", {}) or {}` is what both
    consumers (detect.detect_moments and the studio's estimate route) already
    do with it; that is the opposite of 'subtitles', where a null survives to
    an AttributeError at render time and so has to be refused here.

    An unknown provider is a REPORTED DEFECT, never a silent fall back to the
    default. A typo that quietly ran a different vendor than the operator
    asked for is exactly the silent degradation this project has paid for
    before.

    The MODEL is deliberately NOT checked against the provider's catalogue:
    that would mean carrying three vendors' model lists and re-checking them
    monthly. What an unknown one costs is NOT a lexicon fallback, which this
    docstring claimed for a while and which is measurably wrong: nothing reads
    the model name until the first request, so `detect._caller_from_config`
    builds a caller successfully, the run COMMITS to `engine = "model:<name>"`,
    every window then raises ModelError, and `moment_scan.scan` logs each cause
    and records the window in `missing_windows`. The analysis is written with
    ZERO moments. That is loud, and it is the ONE ENGINE PER RUN rule working
    as designed - but the result is an EMPTY analysis, not a weaker one. The
    lexicon takes over only where `_caller_from_config` returns None: no key
    file, the provider's SDK not installed, or the client failing to build.
    """
    detect = brand.get("detect")
    if detect is None:
        return []
    if not isinstance(detect, dict):
        return [f"{path.name}: 'detect' must be an object"]
    problems = []
    provider = detect.get("provider")
    # `isinstance` first, and not merely for tidiness: a list or a dict here
    # is unhashable, and `x in PROVIDERS` would raise TypeError out of a
    # function whose whole contract is to COLLECT defects rather than throw
    # on the first one.
    if provider is not None and (not isinstance(provider, str)
                                 or provider not in providers.PROVIDERS):
        known = ", ".join(sorted(providers.PROVIDERS))
        problems.append(
            f"{path.name}: 'detect.provider' must be one of {known}, not {provider!r}")
    model = detect.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        problems.append(f"{path.name}: 'detect.model' must be a non-empty string")
    return problems


def merge_glossaries(layers) -> Glossary:
    """Fold GlossaryLayers, least specific first, into one runtime Glossary.

    The more specific layer wins an entry, and a DISABLED winner (a term
    mapped to false, a replacement mapped to null) drops the entry entirely,
    so neither the decoder bias nor the corrections ever see it - the same
    contract merge_lexicons has for a winning weight of 0. The dropped entry
    still exists in its own raw layer, which is what lets the studio render
    it struck through (see glossary_admin.read).

    `terms` is emitted MOST-SPECIFIC-LAYER-FIRST, not in the layer order this
    function receives them. This is deliberate, not cosmetic:
    faster-whisper's own hotword prompt is truncated once it reaches 224
    tokens (cut to 223 - see `glossary.HOTWORD_BUDGET_CHARS`), and truncation
    always drops whatever sits at the END of the emitted list. An
    operator's own channel- or event-level terms are exactly the ones most
    likely to matter to THEM; the built-in default is the one this project
    can most afford to lose a tail of. Emitting default-first (a plain
    forward `terms.update(...)` pass, which was this function's original
    shape) means truncation eats the operator's own entries first and the
    built-in default survives intact - backwards from what anyone editing a
    glossary would want, and silent: nothing here or in the studio told them
    their own 14th term never reached the decoder. Iterating the materialised
    layer list in REVERSE and taking each key the first time it is seen
    achieves both things at once: the first layer to define a key, walking
    most-specific-first, is also the MOST specific one that defines it, so
    this reverse/first-wins pass reproduces exactly the winning value the
    original forward/last-wins pass produced - only the emitted ORDER
    changes. Do not "simplify" this back to a single forward pass; that
    change is what this comment exists to prevent.

    `replacements` ordering does not matter - it stays a dict, consumed by
    key rather than iterated (see `glossary._replacement_keys`, which sorts
    its own view by token count) - so that half keeps the original
    forward, last-write-wins pass unchanged."""
    layers = list(layers)  # a generator would otherwise be exhausted by the first pass below

    terms: dict[str, tuple[str, bool]] = {}
    for layer in reversed(layers):
        for key, value in layer.terms.items():
            terms.setdefault(key, value)

    replacements: dict[str, tuple[str, str | None]] = {}
    for layer in layers:
        replacements.update(layer.replacements)

    return Glossary(
        terms=[spelling for spelling, enabled in terms.values() if enabled],
        replacements={raw: text for raw, text in replacements.values() if text is not None},
    )


def _load_glossary(event_dir: Path, channel_dir: Path,
                   workspace_root: Path) -> tuple[Glossary, list[str]]:
    """Loads this profile's Glossary by MERGING five layers.

    Least to most specific: the built-in default (now EMPTY - see
    glossary.DEFAULT_LAYER), the track pack the EVENT selects, the
    workspace-central glossary.json, the channel's, then the event's. The most
    specific layer wins per entry and a falsy entry disables one inherited
    from a less specific layer - see merge_glossaries.

    The pack is REFERENCED, not copied: an event names a venue and gets
    whatever tracks.py currently says about it, so correcting a corner name
    corrects every event at that venue with no migration.

    Only an EVENT may select a track. The same key at workspace or channel
    scope is reported as a defect rather than ignored - an operator who writes
    it at the wrong level must find out from the error, not from three hours
    of transcript with no corner names in it.

    This REPLACED an earlier wholesale rule, under which an event's own
    glossary.json replaced the channel's outright. The ambiguity that rule
    avoided is now resolved rather than dodged: *add* is the rule, and "only
    these" has an explicit spelling. Restoring the override would silently
    drop the corner names for the one channel that both needs them and has a
    glossary.json of its own. Do not.

    A malformed file at any layer is reported as a problem string, not raised,
    so the caller collects it with every other profile defect - mirroring
    _load_lexicon, which sits right below this. Every layer being absent is
    not an error: the profile simply has no proper nouns to correct.
    """
    problems: list[str] = []
    paths = [(workspace.glossary_path(workspace_root), "workspace"),
             (channel_dir / "glossary.json", "channel"),
             (event_dir / "glossary.json", "event")]
    loaded: list[tuple[glossary_module.GlossaryLayer, str, Path]] = []
    for path, scope in paths:
        if not path.exists():
            continue
        try:
            loaded.append((glossary_module.load(path), scope, path))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            problems.append(f"{path}: {error}")

    track = None
    for layer, scope, path in loaded:
        if layer.track is None:
            continue
        if scope != "event":
            # Name the file, not just its scope: a workspace glossary.json is
            # several directories away from the event being loaded, and the
            # operator has to open the right one to remove the key.
            problems.append(
                f"{path}: only an event selects a track, "
                f"found 'track': {layer.track!r}")
            continue
        track = layer.track

    layers = [glossary_module.DEFAULT_LAYER]
    if track is not None:
        pack = tracks.get(track)
        if pack is None:
            problems.append(
                f"{event_dir / 'glossary.json'}: unknown track {track!r} - "
                f"valid ids: {', '.join(sorted(tracks.PACKS))}")
        else:
            layers.append(tracks.as_layer(pack))
    layers.extend(layer for layer, _scope, _path in loaded)
    return merge_glossaries(layers), problems


def merge_lexicons(layers) -> dict[str, float]:
    """Merge marker->weight layers, least specific first.

    The more specific layer wins a collision, and a winning weight of 0 DROPS
    the marker so scoring never sees it - that is how a channel or event
    disables something inherited from a broader layer (see the stage-D2b
    lexicon design). The dropped entry still exists in the raw layer, which is
    what lets the studio render it struck through."""
    merged: dict[str, float] = {}
    for layer in layers:
        merged.update(layer)
    return {marker: weight for marker, weight in merged.items() if weight > 0}


def _load_lexicon(event_dir: Path, channel_dir: Path,
                  workspace_root: Path) -> tuple[Lexicon, list[str]]:
    """Loads this profile's excitement Lexicon by MERGING four layers.

    Least to most specific: the built-in racing default (lexicon.DEFAULT_MARKERS),
    the workspace-central moments.json, the channel's, then the event's. The most
    specific weight wins and a weight of 0 disables an inherited marker - see
    merge_lexicons.

    This mirrors _load_glossary, which sits next to it and merges its layers
    the same additive, most-specific-wins way (see that function's own
    docstring for why the glossary's original wholesale rule was retired).
    The two are no longer the same FOUR layers, though: the glossary gained a
    fifth for the circuit an event names with "track", which has no lexicon
    equivalent - excitement markers are not venue-specific the way corner
    names are.

    A malformed file at any layer is reported as a problem string, not raised,
    so the caller collects it with every other profile defect, mirroring
    _load_glossary. Every layer being absent is not an error: the built-in
    default still applies.
    """
    problems: list[str] = []
    layers: list[dict[str, float]] = [dict(LEXICON_DEFAULT_MARKERS)]
    for path in (workspace.moments_path(workspace_root),
                 channel_dir / "moments.json",
                 event_dir / "moments.json"):
        if not path.exists():
            continue
        try:
            layers.append(_lexicon_load(path).markers)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            problems.append(f"{path}: {error}")
    return Lexicon(markers=merge_lexicons(layers)), problems


def _load_decorate(event_dir: Path, channel_dir: Path) -> DecorateFunction | None:
    """Optionally loads the function 'decorate' from layout.py: the event
    folder's layout.py wins if present, otherwise the channel's.

    Returns None if neither has a layout.py (plain bars). A layout.py that
    exists but has no matching function is an error in that folder and is
    reported as a ProfileError, not silently ignored.
    """
    event_path = event_dir / "layout.py"
    channel_path = channel_dir / "layout.py"
    path = event_path if event_path.exists() else channel_path
    if not path.exists():
        return None
    module_name = f"yt_shorts_layout_{channel_dir.name}_{event_dir.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ProfileError(f"layout.py could not be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise ProfileError(f"layout.py raises an error while loading: {path}\n{error}") from error
    decorate = getattr(module, "decorate", None)
    if decorate is None or not callable(decorate):
        raise ProfileError(f"layout.py has no callable function 'decorate': {path}")
    return decorate


def load(identifier: str) -> Profile:
    """Resolves 'channel/event' and loads the layered channel+event profile.

    The channel's brand.json supplies the defaults; if the event folder has
    its own brand.json, it is deep-merged on top (event wins, per key - see
    yt_shorts.merge.deep_merge). Font and logo paths named in either profile
    are then resolved against the event folder first, then the channel
    folder. layout.py is loaded from the event folder if present, else the
    channel folder, else omitted (plain bars). config["glossary"] is loaded
    by MERGING five layers - the (empty) built-in default, the track pack of
    the circuit the event named with "track", workspace, channel, event, most
    specific wins per entry - see _load_glossary; an event that names a track
    gets that circuit's vocabulary with no glossary.json of its own. An event
    with none of these files behaves exactly as the channel does.

    Raises ProfileError with an understandable message for an unknown
    channel, an unknown event, a missing/broken profile file, or a profile
    missing mandatory fields (every such gap is collected and reported
    together, not just the first one found).
    """
    parts = identifier.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ProfileError(
            f"Identifier must have the form 'channel/event', not: {identifier!r}"
        )
    channel_name, event_name = parts

    # Both segments become directory names below (and channel_name/event_name
    # feed _load_decorate's exec_module of a workspace layout.py), so validate
    # them as safe single path segments BEFORE any filesystem touch - the same
    # guard every studio write op applies. This is the one identifier->path
    # surface that used to trust its caller; close it at the source.
    for segment, what in ((channel_name, "channel"), (event_name, "event")):
        try:
            validate_segment(segment, what=what)
        except ValueError as error:
            raise ProfileError(str(error)) from error

    channel_dir = CHANNELS_DIR / channel_name
    if not channel_dir.is_dir():
        raise ProfileError(
            f"Unknown channel '{channel_name}' (looked under {channel_dir}).\n"
            f"Existing channels: {_existing_dirs(CHANNELS_DIR)}"
        )

    event_dir = channel_dir / "events" / event_name
    if not event_dir.is_dir():
        raise ProfileError(
            f"Unknown event '{event_name}' for channel '{channel_name}' "
            f"(looked under {event_dir}).\n"
            f"Existing events: {_existing_dirs(channel_dir / 'events')}"
        )

    channel = _load_json(channel_dir / "channel.json", "Channel profile (channel.json)")

    channel_brand = _load_json(channel_dir / "brand.json", "Brand profile (brand.json)")
    event_brand = _load_optional_json(event_dir / "brand.json", "Event brand profile (brand.json)")
    config = deep_merge(channel_brand, event_brand)

    config["fonts"] = _resolve_fonts(config.get("fonts"), event_dir, channel_dir)
    _resolve_logo(config, event_dir, channel_dir)

    glossary, glossary_problems = _load_glossary(
        event_dir, channel_dir, CHANNELS_DIR.parent)
    config["glossary"] = glossary

    lexicon_value, lexicon_problems = _load_lexicon(
        event_dir, channel_dir, CHANNELS_DIR.parent)
    config["lexicon"] = lexicon_value

    problems = (
        _validate_channel(channel, channel_dir / "channel.json")
        + _validate_brand(config, channel_dir / "brand.json")
        + _validate_logo(config, channel_dir / "brand.json")
        + _validate_subtitles(config, channel_dir / "brand.json")
        + _validate_upload(config, channel_dir / "brand.json")
        + _validate_detect(config, channel_dir / "brand.json")
        + glossary_problems
        + lexicon_problems
    )
    if problems:
        listing = "\n".join(f"  - {p}" for p in problems)
        raise ProfileError(
            f"Incomplete profile for channel '{channel_name}' "
            f"({len(problems)} problem(s)):\n{listing}"
        )

    # AFTER validation, never before - and not for the reason it looks like.
    # band_opacities cannot raise; it is deliberately tolerant. The hazard is
    # that it OVERWRITES config["bands"] with its sanitised result, so a call
    # placed before the validator would hand _validate_brand an already-clean
    # dict and every defect it exists to report would vanish silently:
    # {"top": true} would reach it as {"top": 1.0, "bottom": 1.0}. Moving
    # this line above the raise fails five of TestBandValidation's tests.
    config["bands"] = band_opacities(config)

    decorate = _load_decorate(event_dir, channel_dir)
    if decorate is not None:
        config["decorate"] = decorate

    return Profile(
        identifier=identifier,
        channel_name=channel_name,
        event_name=event_name,
        channel_dir=channel_dir,
        event_dir=event_dir,
        channel=channel,
        config=config,
    )
