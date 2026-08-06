"""Builds the brand overlay as a PNG.

ffmpeg here is built without libfreetype, so it cannot draw text. All
typography and graphics are therefore produced in Pillow and later placed
on the video via an ffmpeg overlay.

The channel-specific decoration (for ERF, a slanted parallelogram) is NOT
wired in here: yt_shorts.profile optionally loads it from the channel
folder's layout.py and passes the function in under config["decorate"]
(see build_overlay below). If absent, plain bars are used instead.
"""

from __future__ import annotations

import math
import sys

from PIL import Image, ImageColor, ImageDraw, ImageFont

MARGIN = 70

DEFAULT_MAX_LINES = 3  # editorial rule: at most three hook lines
MIN_SIZE = 52  # smallest font size at which the hook is still drawn
ELLIPSIS = "…"

# Built-in defaults for an optional logo (config["logo"]), used whenever the
# profile names a file but leaves max_height/gap unspecified.
DEFAULT_LOGO_MAX_HEIGHT = 160
DEFAULT_LOGO_GAP = 24

# A logo's placement. "top" (the default) is the original centered-top badge
# that reserves vertical space above the hook. The "*-right" values are a small,
# subtle corner watermark that reserves NO space - it stays inside a veil band
# (never over the video window, which carries burned-in timing/leaderboard
# info) and clear of the centered hook and the footer.
LOGO_POSITIONS = ("top", "top-right", "bottom-right")
DEFAULT_LOGO_WATERMARK_HEIGHT = 96
DEFAULT_LOGO_WATERMARK_MARGIN = 36
DEFAULT_LOGO_WATERMARK_OPACITY = 0.72

# Smallest gap the footer text keeps from a bottom-right watermark, and the
# smallest size it may shrink to so it clears one. See _draw_footer.
FOOTER_LOGO_GAP = 28
FOOTER_MIN_SIZE = 34

# Alpha values of the bar surfaces. The renderer will in future place its
# own blurred, frame-filling video image as the background under this
# layer, so the bars must no longer be fully opaque.
ALPHA_BASE = 150  # darkening veil, lets the background show through
ALPHA_OPAQUE = 255

# The two bands whose surface opacity a brand may scale, and the factor that
# means "exactly as this tool has always drawn it".
BAND_KEYS = ("top", "bottom")
FULL_STRENGTH = 1.0


def band_opacities(config: dict) -> dict[str, float]:
    """The two band opacity factors, defaulted and coerced to float.

    An absent 'bands' section, an absent key inside it and the value 1.0 all
    mean the same thing - full strength - so a brand.json written before
    this feature existed renders identically with no migration.

    Deliberately TOLERANT of a malformed section rather than raising, and
    the result is always in range. profile._validate_brand reports every bad
    value as a readable profile defect, but it does NOT run on every path
    that reaches here: the studio's live preview calls build_overlay on an
    UNSAVED brand, so a half-typed or out-of-range value must degrade rather
    than 500 the preview route or, worse, reach the alpha arithmetic. Both
    kinds of malformation degrade, not just the wrong TYPE: an out-of-range
    number is clamped and a NaN becomes full strength, because a factor
    above 1 would compute an alpha past 255 and NaN would raise inside
    int().
    """
    raw = config.get("bands")
    values = raw if isinstance(raw, dict) else {}
    result: dict[str, float] = {}
    for key in BAND_KEYS:
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            result[key] = FULL_STRENGTH
            continue
        number = float(value)
        # NaN would slip through min/max unclamped.
        result[key] = FULL_STRENGTH if math.isnan(number) else min(1.0, max(0.0, number))
    return result


def _with_alpha(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    """Turns a '#RRGGBB' color from brand.json into an RGBA tuple with an
    explicit alpha. Needed because Pillow otherwise silently fills hex
    strings with alpha 255 when drawing on an RGBA image."""
    r, g, b = ImageColor.getrgb(hex_color)
    return (r, g, b, alpha)


def wrap_text(text: str, font, max_width: int) -> list[str]:
    """Wraps text to max_width. An overlong single word is left as-is."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        attempt = f"{current} {word}"
        if font.getbbox(attempt)[2] <= max_width:
            current = attempt
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _truncate_with_ellipsis(line: str, font, max_width: int) -> str:
    """Appends an ellipsis to line and truncates line character by character
    until the result fits within max_width. If line+ellipsis already fits,
    nothing is truncated."""
    text = line
    while text and font.getbbox(text + ELLIPSIS)[2] > max_width:
        text = text[:-1]
    return (text + ELLIPSIS) if text else ELLIPSIS


def _truncate_to_line_count(
    lines: list[str], font, max_width: int, max_lines: int
) -> tuple[list[str], bool]:
    """Caps lines at max_lines and makes sure every visible line fits within
    max_width. Prefers showing less text over running out of the image: if
    text still gets dropped afterwards, the last visible line ends with an
    ellipsis; an overly wide single word is truncated to the fitting
    character count (also marked with an ellipsis).

    Also returns whether anything was actually lost - an ellipsis inserted,
    or a whole line dropped by the max_lines cap - so a caller can tell
    "this text needed truncating" apart from "this text just happened to
    settle at the minimum size and still fit completely"."""
    text_is_dropped = len(lines) > max_lines
    visible = list(lines[:max_lines]) if max_lines > 0 else []
    truncated = text_is_dropped
    for i, line in enumerate(visible):
        is_last = i == len(visible) - 1
        too_wide = font.getbbox(line)[2] > max_width
        if too_wide or (is_last and text_is_dropped):
            visible[i] = _truncate_with_ellipsis(line, font, max_width)
            truncated = True
    return visible, truncated


def _fitting_size(text: str, path: str, max_width: int, max_lines: int,
                  start: int = 108, minimum: int = MIN_SIZE):
    """Searches for the largest font size at which the hook fits in
    max_lines AND every single line stays within max_width. If even the
    minimum size still doesn't fit everything, it gets truncated instead of
    overflowing.

    Returns (font, lines, truncated) - truncated is True exactly when text
    was actually lost at the minimum size (see _truncate_to_line_count),
    False whenever the search found a size that fits the text in full."""
    for size in range(start, minimum - 1, -4):
        font = ImageFont.truetype(path, size)
        lines = wrap_text(text, font, max_width)
        if len(lines) <= max_lines and all(font.getbbox(line)[2] <= max_width for line in lines):
            return font, lines, False
    font = ImageFont.truetype(path, minimum)
    lines = wrap_text(text, font, max_width)
    lines, truncated = _truncate_to_line_count(lines, font, max_width, max_lines)
    return font, lines, truncated


def _fit_logo(logo: Image.Image, max_height: int, max_width: int) -> Image.Image:
    """Scales logo proportionally so it fits within (max_width, max_height),
    never distorting the aspect ratio. Capped by height first (the profile's
    'max_height' is the primary knob); if that would still overflow the
    side margins, the width cap takes over instead."""
    width, height = logo.size
    scale = max_height / height
    if width * scale > max_width:
        scale = max_width / width
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return logo.resize(new_size, Image.LANCZOS)


def _apply_opacity(logo: Image.Image, opacity: float) -> Image.Image:
    """Scales a logo's alpha channel by `opacity` (0..1) so a watermark can be
    made subtle. Returns it unchanged at opacity >= 1."""
    if opacity >= 1:
        return logo
    r, g, b, a = logo.split()
    a = a.point(lambda value: round(value * opacity))
    return Image.merge("RGBA", (r, g, b, a))


def _place_watermark(image: Image.Image, config: dict, logo_conf: dict,
                     position: str) -> tuple[int, int] | None:
    """Draws the logo small and subtle in a right-hand corner of a veil band.
    Reserves no vertical layout space (the hook is unchanged); it stays inside
    the veil, never over the video window, and is right-aligned to a margin from
    the frame edge. "top-right" sits at the top of the upper band; "bottom-right"
    sits in the true bottom-right corner of the lower band.

    Returns (left_x, center_y) of the drawn logo for "bottom-right" - the footer
    keeps clear of left_x and is vertically centred on center_y (see
    _draw_footer) - and None otherwise.
    """
    output = config["output"]
    width, height = output["width"], output["height"]
    max_height = logo_conf.get("max_height", DEFAULT_LOGO_WATERMARK_HEIGHT)
    margin = logo_conf.get("margin", DEFAULT_LOGO_WATERMARK_MARGIN)
    opacity = logo_conf.get("opacity", DEFAULT_LOGO_WATERMARK_OPACITY)
    max_width = width - 2 * margin

    with Image.open(logo_conf["file"]) as source:
        logo = _fit_logo(source.convert("RGBA"), max_height, max_width)
    logo = _apply_opacity(logo, opacity)

    x = width - margin - logo.width
    if position == "bottom-right":
        y = height - margin - logo.height
        image.alpha_composite(logo, (x, y))
        return x, y + logo.height // 2
    # "top-right"
    image.alpha_composite(logo, (x, margin))
    return None


def _place_logo(image: Image.Image, config: dict) -> tuple[int, tuple[int, int] | None]:
    """Draws the optional logo and returns (top_reserved, footer_logo).

    The default "top" placement is a centered badge at the top of the upper
    band; top_reserved is the space it consumes below MARGIN (logo height plus
    gap), so the hook layout below shrinks its budget by exactly that much. The
    "*-right" placements are a corner watermark that reserves no vertical space
    (top_reserved 0); a "bottom-right" one additionally returns its
    (left_x, center_y) as footer_logo so the footer can keep clear of it and
    centre on it. With no 'logo' key the return is (0, None) and every formula
    downstream reduces to the pre-logo behaviour unchanged.
    """
    logo_conf = config.get("logo")
    if not logo_conf:
        return 0, None

    position = logo_conf.get("position", "top")
    if position != "top":
        return 0, _place_watermark(image, config, logo_conf, position)

    max_height = logo_conf.get("max_height", DEFAULT_LOGO_MAX_HEIGHT)
    gap = logo_conf.get("gap", DEFAULT_LOGO_GAP)
    max_width = config["output"]["width"] - 2 * MARGIN

    with Image.open(logo_conf["file"]) as source:
        logo = _fit_logo(source.convert("RGBA"), max_height, max_width)

    x = (config["output"]["width"] - logo.width) // 2
    y = MARGIN
    image.alpha_composite(logo, (x, y))

    return logo.height + gap, None


def _draw_footer(draw: ImageDraw.ImageDraw, text: str, config: dict,
                 text_color, footer_logo: tuple[int, int] | None) -> None:
    """Draws the footer centered in the lower band.

    Without a bottom-right watermark (footer_logo is None) this is the original
    behaviour byte-for-byte: full-width centering at FOOTER_FONT_SIZE. With one,
    footer_logo is the logo's (left_x, center_y): the footer is centered in the
    space to the LEFT of left_x and shrinks to fit (down to FOOTER_MIN_SIZE, then
    truncates with an ellipsis) so its text never runs into the logo, and it is
    centered vertically on center_y - measuring the actual ink box, so the
    all-caps text sits on the logo's midline rather than the taller font box
    (whose unused descender space would push the visible text up).
    """
    output = config["output"]
    width, height = output["width"], output["height"]
    font_path = config["fonts"]["small"]

    if footer_logo is None:
        small = ImageFont.truetype(font_path, FOOTER_FONT_SIZE)
        draw.text((width // 2, height - MARGIN - FOOTER_FONT_SIZE), text,
                  font=small, fill=text_color, anchor="ma")
        return

    logo_left, center_y = footer_logo
    right = logo_left - FOOTER_LOGO_GAP
    available = right - MARGIN
    size = FOOTER_FONT_SIZE
    small = ImageFont.truetype(font_path, size)
    while size > FOOTER_MIN_SIZE and small.getbbox(text)[2] > available:
        size -= 2
        small = ImageFont.truetype(font_path, size)
    if small.getbbox(text)[2] > available:
        text = _truncate_with_ellipsis(text, small, available)
    center_x = (MARGIN + right) // 2
    # Vertically center the visible ink on the logo's midline: with anchor "mm"
    # the font box (ascent+descent) is centered, but all-caps ink sits in its
    # upper part, so shift down by the ink box's own center offset.
    ink_top, ink_bottom = draw.textbbox((0, 0), text, font=small, anchor="mm")[1::2]
    draw.text((center_x, center_y - (ink_top + ink_bottom) // 2), text,
              font=small, fill=text_color, anchor="mm")


def _fade_bands(image: Image.Image, bands: dict, window_top: int, window_bottom: int) -> None:
    """Scales the alpha of everything drawn so far, per band, in place.

    Called at exactly ONE point in build_overlay: after the veil, the channel
    decoration and the edge accents, and BEFORE the logo, hook and footer.
    That position is the whole design. At this moment the image holds nothing
    but the band SURFACES, so scaling its alpha scales precisely them -
    including a decoration this module has never seen, since
    config["decorate"] comes from the channel's own layout.py. Passing a
    factor into that call instead would push the responsibility into every
    channel's layout.py, where it would be silently forgotten by the next one
    written.

    A factor of 1.0 is SKIPPED rather than applied as an identity multiply.
    Measured, not assumed: with the skip removed, the six reference overlays
    still rendered byte-identically, so Pillow's identity multiply is lossless
    as things stand. The skip therefore buys
    independence from that behaviour - and a crop and a paste saved on every
    render - rather than the byte-identity itself. Do not read it as
    load-bearing for correctness, and never re-pin those hashes.

    The video window's own rows are never touched: they are alpha 0 and must
    stay exactly that, or the sharp picture would be veiled - the one thing
    this format exists to preserve.
    """
    width, height = image.size
    for key, top, bottom in (("top", 0, window_top), ("bottom", window_bottom, height)):
        factor = bands[key]
        if factor >= FULL_STRENGTH or bottom <= top:
            continue
        region = image.crop((0, top, width, bottom))
        # A 256-entry lookup table rather than a lambda: point() takes either,
        # and a table avoids defining a closure over the loop variable.
        table = [int(value * factor) for value in range(256)]
        region.putalpha(region.getchannel("A").point(table))
        image.paste(region, (0, top))


def build_overlay(hook: str, footer: str, config: dict) -> Image.Image:
    """Creates the 1080x1920 overlay.

    The bar surfaces are semi-transparent so that the blurred,
    frame-filling video background placed underneath by the renderer can
    show through. The video window itself stays exactly alpha 0, the edge
    color directly against it stays exactly alpha 255.

    Of the three things drawn, only the middle one is channel-specific: if
    a function is found under config["decorate"] (loaded by yt_shorts.profile
    from the channel folder's layout.py), it is called between the base
    surface and the edges and may draw its own motif there (for ERF, the
    parallelogram). Without a layout.py, plain bars are used.
    """
    a = config["output"]
    width, height = a["width"], a["height"]
    window_top = a["video_y"]
    window_bottom = a["video_y"] + a["video_height"]

    base = _with_alpha(config["colors"]["base"], ALPHA_BASE)
    edge = _with_alpha(config["colors"]["edge"], ALPHA_OPAQUE)
    text_color = config["colors"]["text"]

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Base surface of the bars: darkening veil, semi-transparent
    draw.rectangle([0, 0, width, window_top - 1], fill=base)
    draw.rectangle([0, window_bottom, width, height], fill=base)

    # Channel-specific decoration, if present (see docstring above)
    decorate = config.get("decorate")
    if decorate is not None:
        decorate(draw, config, window_top, window_bottom)

    # Edge as an accent directly at the video window: stays fully opaque,
    # it frames the sharp image and is the crispest brand element
    draw.rectangle([0, window_top - 6, width, window_top - 1], fill=edge)
    draw.rectangle([0, window_bottom, width, window_bottom + 5], fill=edge)

    # Everything drawn above is a SURFACE; everything below is content. This
    # is the seam the band opacity acts on - see _fade_bands.
    _fade_bands(image, band_opacities(config), window_top, window_bottom)

    # Optional event/channel logo, centered at the top of the upper band.
    # logo_reserved is the vertical space it consumes (0 without a 'logo'
    # key), which the hook layout below must subtract from its own budget -
    # otherwise a tall logo pushes the hook text down into it or past the
    # video window, exactly the bug class the overflow guard exists to
    # prevent. With logo_reserved == 0 every formula below reduces exactly
    # to the pre-logo behaviour.
    logo_reserved, footer_logo = _place_logo(image, config)

    # Hook, large and centered in the upper band
    if hook.strip():
        max_width = width - 2 * MARGIN
        # Available vertical space for the hook block: the upper bar minus
        # the top padding (the margin, plus the logo and its gap if one is
        # configured) and the space an optional decoration takes up at the
        # bottom edge of the bar. The latter is channel-specific (for ERF,
        # the parallelogram offset) and therefore comes from the
        # configuration instead of being hardcoded - a channel without a
        # decoration simply leaves this at 0 (the default). The editorial
        # cap of DEFAULT_MAX_LINES only gives way if even at the minimum
        # font size that many lines wouldn't fit.
        accent_offset = a.get("accent_offset", 0)
        top_pad = MARGIN + logo_reserved
        available_height = window_top - accent_offset * 2 - top_pad
        min_line_height = int(MIN_SIZE * 1.12)
        max_lines = max(1, min(DEFAULT_MAX_LINES, available_height // min_line_height))
        font, lines, _ = _fitting_size(hook.upper(), config["fonts"]["hook"], max_width, max_lines)
        line_height = int(font.size * 1.12)
        block = line_height * len(lines)
        y = max(top_pad, (window_top - accent_offset * 2 - block) // 2)
        for line in lines:
            draw.text((width // 2, y), line, font=font, fill=text_color, anchor="ma")
            y += line_height

    # Footer in the lower band. When a bottom-right watermark is present it
    # yields the footer the space to the logo's left (see _draw_footer);
    # otherwise this is the original full-width centered footer, unchanged.
    if footer.strip():
        _draw_footer(draw, footer.upper(), config, text_color, footer_logo)

    return image


DEFAULT_CAPTION_SIZE = 78
DEFAULT_CAPTION_GAP = 82  # vertical distance from the video window to the caption
CAPTION_MAX_LINES = 2

# The single source of truth for the footer's font size. build_overlay's
# "Footer in the lower band" block draws the footer's text with this size
# at (height - MARGIN - FOOTER_FONT_SIZE), anchor="ma" - see that block.
# _footer_top below derives the caption's lower bound from this same
# constant, so the two can never silently drift out of sync the way a
# second, independently-chosen literal could.
FOOTER_FONT_SIZE = 54


def _footer_top(height: int) -> int:
    """First pixel row build_overlay's footer text can occupy. A caption
    must end at or above this row so it never reaches the footer."""
    return height - MARGIN - FOOTER_FONT_SIZE


def _caption_top(config: dict, explicit_y: bool) -> int:
    """The caption's intended top row: an explicit subtitles.y honoured
    exactly, or a fixed gap below the video window otherwise. Shared by
    build_caption and caption_geometry so the two can never compute this
    differently."""
    output = config["output"]
    subtitles = config.get("subtitles", {})
    window_bottom = output["video_y"] + output["video_height"]
    return int(subtitles["y"]) if explicit_y else window_bottom + DEFAULT_CAPTION_GAP


def validate_caption_box(config: dict, top: int, block_height: int, explicit_y: bool) -> None:
    """The geometry rule a caption box must satisfy: it must never reach
    into the video window above it or the footer below it. Raises
    ValueError, naming the offending value, the bound it violates and the
    acceptable range, otherwise returns None.

    This is the single source of truth for that rule, shared by two
    callers that check it at different times with different knowledge of
    the box's height:

    - build_caption calls it per clip, with block_height measured from the
      FITTED font size and line count _fitting_size actually settles on
      (after any shrinking) for that clip's real caption text - not the
      nominal, pre-shrink size, which would reject captions that in fact
      fit.
    - profile.load calls it once at load time, before any clip exists, with
      block_height from caption_geometry's worst case: the profile's
      nominal subtitles.size and the maximum CAPTION_MAX_LINES, which
      bound whatever any real clip could ever produce. top does not depend
      on block_height in either mode (an explicit subtitles.y is a fixed
      pixel; the default top is a fixed gap below the window), so a larger
      block_height only ever makes a rejection MORE likely - meaning a
      geometry accepted at profile.load is guaranteed to also be accepted
      by build_caption for any real text, and a geometry build_caption
      rejects for some text is guaranteed to already have been rejected at
      profile.load. The two can disagree only in the conservative
      direction (profile.load rejecting a geometry that would in fact have
      been fine for an unusually short caption) - never in the dangerous
      one (profile.load accepting a geometry build_caption then rejects).
    """
    output = config["output"]
    height = output["height"]
    window_top = output["video_y"]
    window_bottom = window_top + output["video_height"]
    footer_top = _footer_top(height)
    bottom = top + block_height

    if explicit_y:
        valid_low = window_bottom
        valid_high = footer_top - block_height
        if top < window_bottom:
            raise ValueError(
                f"subtitles.y={top} is inside the video window "
                f"(window spans y={window_top}-{window_bottom}); "
                f"must be between {valid_low} and {valid_high}"
            )
        if bottom > footer_top:
            raise ValueError(
                f"subtitles.y={top} would draw the caption into the footer "
                f"area (caption would end at y={bottom}, "
                f"footer starts at y={footer_top}); "
                f"must be between {valid_low} and {valid_high}"
            )
    else:
        if bottom > footer_top:
            # Nothing here is misconfigured as such - the operator never set
            # subtitles.y, so pointing at it would send them chasing a value
            # that doesn't exist. The video window simply leaves too little
            # room below it for a caption this tall, so the message points
            # at the geometry that actually causes it instead.
            available = footer_top - window_bottom
            raise ValueError(
                f"the default caption position (output.video_y={window_top} + "
                f"output.video_height={output['video_height']} + "
                f"DEFAULT_CAPTION_GAP={DEFAULT_CAPTION_GAP} = {top}) would draw "
                f"the caption into the footer area (caption would end at "
                f"y={bottom}, footer starts at y={footer_top}); the video "
                f"window leaves only {available}px below it, but the caption "
                f"needs {block_height}px - reduce output.video_height or "
                f"subtitles.size"
            )


def caption_geometry(config: dict) -> tuple[int, int, bool]:
    """Returns (top, block_height, explicit_y) for the WORST-CASE caption
    box this profile could ever need to draw - fully determined at
    profile.load time, without any per-clip caption text: block_height
    uses the profile's nominal subtitles.size and the maximum
    CAPTION_MAX_LINES, both of which upper-bound whatever _fitting_size
    actually settles on for any real clip (font size no larger than
    nominal, line count no more than CAPTION_MAX_LINES - see
    validate_caption_box's docstring for why that makes this a safe,
    one-directional approximation). Used by profile.load together with
    validate_caption_box to reject a bad subtitles.size/y before a run
    starts, instead of only discovering it per clip inside build_caption.
    """
    explicit_y = "y" in config.get("subtitles", {})
    top = _caption_top(config, explicit_y)
    size = int(config.get("subtitles", {}).get("size", DEFAULT_CAPTION_SIZE))
    block_height = int(size * 1.12) * CAPTION_MAX_LINES
    return top, block_height, explicit_y


CAPTION_PREVIEW_LENGTH = 60  # chars shown by _identify_caption before truncating with an ellipsis


def _identify_caption(text: str) -> str:
    """A short, printable identifier for a caption group - enough to spot
    it in a transcript or find it again, without reproducing the whole
    group. At the default max_words (4), a group is always short enough to
    pass through untouched; an operator who raises max_words sharply can
    group far more words into one caption than that, and the truncation
    NOTE that uses this must not become the full group echoed to stderr on
    every occurrence."""
    if len(text) <= CAPTION_PREVIEW_LENGTH:
        return f'"{text}"'
    return f'"{text[:CAPTION_PREVIEW_LENGTH]}…" ({len(text)} chars, {len(text.split())} words)'


def build_caption(text: str, config: dict) -> Image.Image:
    """Draws one caption group into the lower band.

    Returns a full-size transparent image so the renderer can overlay it at
    0:0 exactly like the brand overlay, instead of tracking an offset.

    The caption must never reach into the video window above it or the
    footer below it, so it is laid out inside an explicit box and uses the
    same size search as the hook: shrink until it fits, truncate rather
    than overflow.

    Its top defaults to a fixed gap below the video window (DEFAULT_CAPTION_GAP);
    an explicit subtitles.y is honoured exactly instead. Either way, the
    resulting box is validated (see validate_caption_box) against the
    window and the footer before anything is drawn: silently drawing a
    caption in a colliding position is exactly the bug class this guard
    exists to prevent, so a collision raises ValueError. The validation
    uses the font and line count _fitting_size actually settles on (after
    any shrinking), not the nominal, pre-shrink size - otherwise a caption
    that in fact fits could be rejected.
    """
    output = config["output"]
    subtitles = config.get("subtitles", {})
    width, height = output["width"], output["height"]

    max_width = width - 2 * MARGIN
    size = int(subtitles.get("size", DEFAULT_CAPTION_SIZE))

    # Determine the font and wrapped lines first, so the box validated below
    # reflects what is actually going to be drawn rather than an estimate
    # from the nominal (not yet possibly-shrunk) size.
    font, lines, truncated = _fitting_size(
        text.upper(), config["fonts"]["hook"], max_width,
        CAPTION_MAX_LINES, start=size, minimum=min(MIN_SIZE, size),
    )
    line_height = int(font.size * 1.12)
    block_height = line_height * len(lines)

    explicit_y = "y" in subtitles
    top = _caption_top(config, explicit_y)

    validate_caption_box(config, top, block_height, explicit_y)

    if truncated:
        # Even the smallest allowed size (MIN_SIZE, or the profile's own
        # subtitles.size if that is already smaller) could not fit this
        # text into CAPTION_MAX_LINES - some of it was cut and marked with
        # an ellipsis rather than overflowing the box. That is a silent
        # content loss unless reported: same NOTE: style as every other
        # per-entry trouble this tool surfaces, naming the caption whose
        # text got cut (overlay.py has no clip name to report instead - it
        # knows nothing channel- or clip-specific, only the text it was
        # asked to draw). Identified via _identify_caption rather than
        # reproduced in full (finding C6): harmless at the default
        # max_words, but an operator who raises it sharply would otherwise
        # get the entire caption group echoed to stderr on every truncation.
        print(f'NOTE: caption {_identify_caption(text)}: truncated to fit', file=sys.stderr)

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not text.strip():
        return image

    draw = ImageDraw.Draw(image)
    y = top
    for line in lines:
        draw.text((width // 2, y), line, font=font,
                  fill=_with_alpha(config["colors"]["text"], ALPHA_OPAQUE), anchor="ma")
        y += line_height
    return image
