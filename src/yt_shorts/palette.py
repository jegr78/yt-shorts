"""Proposes a brand palette from a channel's logo.

Pure and stdlib-plus-Pillow only, like pathnames.py and upload_policy.py: no
FastAPI, no google, no import from the studio package. The studio calls IN
(GET /api/channels/{channel}/brand/palette), never the reverse.

What this module is FOR: four of the five channels in the operator's
workspace shipped byte-identical placeholder colours, two of which were
another channel's green. Deriving from the logo makes the common case one
click instead of four eyedropper trips, and makes it repeatable for the next
channel.

What it deliberately is NOT: an automatic overwrite. `derive` returns a
proposal AND the swatches it came from; the editor fills its colour fields
and renders the swatches as chips, and nothing is written until the operator
saves. A palette is a taste decision with a measurable starting point, not a
computation with one right answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

# Pixels below this alpha are dropped before anything is measured. A logo is
# mostly transparent padding, and its glyph edges are anti-aliased blends of
# two colours that belong to neither - counting either would bias every
# result toward whatever the mark sits on.
MIN_ALPHA = 200

# Quantisation target. Eight is enough to separate a mark's real colours
# without splitting one flat fill into near-identical neighbours.
MAX_SWATCHES = 8

# The smallest share a swatch may hold and still be considered for `base`.
# Without it a handful of near-black anti-aliasing survivors on an otherwise
# light mark would be chosen as the channel's ground colour.
MIN_BASE_SHARE = 0.05

# WCAG 2.1's minimum contrast ratio for large text and UI components (not the
# stricter 4.5 for body text - `edge` is a thin accent line, not a paragraph).
# `edge`/`accent` must clear this against `base` before chroma ever gets a
# vote: this decides VISIBILITY against the base and catches a near-black
# anti-aliasing fleck, which has low contrast against a dark base.
MIN_EDGE_CONTRAST = 3.0

# The smallest share a swatch may hold and still be considered for `edge`/
# `accent`. Contrast alone does not catch a near-WHITE fleck on a dark base -
# it has HIGH contrast and would sail through - so a swatch below this share
# of the opaque pixels is treated as a fleck, not a brand colour, regardless
# of how much contrast or chroma it has. Measured on the operator's
# erfofficial logo: of the swatches that clear MIN_EDGE_CONTRAST, the
# largest fleck holds 1.8% share (outranking the real colour on chroma) and
# the smallest genuine brand colour holds 3.6% -
#   #E1E8F9  share 3.6%  chroma 0.094   <- the real light blue
#   #A7ADD5  share 1.8%  chroma 0.180   <- fleck, would win on chroma alone
#   #BBC3E0  share 0.5%  chroma 0.145   <- fleck
#   #8A91B4  share 0.5%  chroma 0.165   <- fleck
# 0.02 sits between those two, closer to the fleck side - a floor chosen
# from evidence rather than a law, and the margin (1.8% vs 3.6%) is narrow
# and measured on one logo, not a wide margin like MIN_CHROMA below.
MIN_EDGE_SHARE = 0.02

# Below this chroma a colour has no visible hue and the palette is
# effectively neutral - `edge` falls back to the brightest candidate instead
# of the most colourful one (see `derive`). Measured across the operator's
# five real channels, the highest chroma among each channel's
# contrast-clearing candidates:
#   communityleagueracing     0.969  (vivid blue)      <- has a hue
#   international-racing-org  0.804  (red)             <- has a hue
#   erfofficial               0.094  (pale blue)       <- effectively neutral
#   jegr                      0.071  (warm grey)       <- effectively neutral
#   communityteamcup          0.000  (black and white) <- effectively neutral
# 0.15 separates the two channels with a real hue from the three without,
# with the nearest values 0.094 below and 0.804 above - a wide margin on
# real data.
MIN_CHROMA = 0.15

LIGHT_TEXT = "#FFFFFF"
DARK_TEXT = "#111111"


class PaletteError(Exception):
    """kind: not_found | unreadable | empty. HTTP: everything -> 409, the
    same way the brand preview route reports a render it cannot perform."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Swatch:
    """One colour the logo actually contains, and how much of it there is.

    `share` is of the OPAQUE pixels only, so it answers "how much of the mark
    is this colour", not "how much of the file"."""
    hex: str
    share: float


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(rgb)


def _rgb(value: str) -> tuple[int, int, int]:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def _luminance(value: str) -> float:
    """WCAG relative luminance - the perceptual measure, not a naive mean.
    Green contributes far more to how light a colour looks than blue does,
    which a mean would ignore and then pick unreadable text for."""
    channels = []
    for raw in _rgb(value):
        c = raw / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _chroma(value: str) -> float:
    """How colourful a swatch is, on 0..1. Unlike HLS saturation this is NOT
    degenerate at either lightness extreme: a near-black or near-white pixel
    with only a faint colour cast scores near 0 here, where HLS saturation
    would report both as ~1.0 regardless of how visible the colour actually
    is (see `derive`'s docstring)."""
    r, g, b = _rgb(value)
    return (max(r, g, b) - min(r, g, b)) / 255


def _contrast(a: str, b: str) -> float:
    first, second = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def swatches(path) -> list[Swatch]:
    """The logo's own colours, most prominent first.

    Raises PaletteError rather than returning an empty list for a file that
    is missing, unreadable, or has no opaque pixel at all - each is a
    different thing for the operator to fix, and the route reports which.
    """
    path = Path(path)
    if not path.is_file():
        raise PaletteError(f"no logo file at {path.name}", kind="not_found")
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGBA")
    except (OSError, UnidentifiedImageError) as error:
        raise PaletteError(f"{path.name} is not a readable image", kind="unreadable") from error

    opaque = [pixel[:3] for pixel in image.getdata() if pixel[3] >= MIN_ALPHA]
    if not opaque:
        raise PaletteError(f"{path.name} has no opaque pixels", kind="empty")

    strip = Image.new("RGB", (len(opaque), 1))
    strip.putdata(opaque)
    quantised = strip.quantize(colors=MAX_SWATCHES, method=Image.Quantize.FASTOCTREE)
    table = quantised.getpalette()
    total = len(opaque)
    found = [
        Swatch(hex=_hex(table[index * 3:index * 3 + 3]), share=count / total)
        for count, index in quantised.getcolors()
    ]
    found.sort(key=lambda swatch: swatch.share, reverse=True)
    return found


def derive(path) -> tuple[dict[str, str], list[Swatch]]:
    """A proposed {text, base, accent, edge} plus the swatches behind it.

    The assignment rules, and why each is what it is:

    - `base` is the DARKEST swatch holding at least MIN_BASE_SHARE. The veil
      sits behind white hook text, so darkness outranks prominence: a light
      base is a legibility bug, not a style choice.
    - `edge` is chosen from three tiers, tried in order, and the ranking
      metric is NOT the same in all three:
        1. Swatches that clear BOTH MIN_EDGE_CONTRAST against `base` AND
           MIN_EDGE_SHARE of the opaque pixels - the crispest brand
           element, the one that reads as "this channel's colour". Contrast
           and share are both checked BEFORE chroma ever gets a vote, for
           two different reasons: contrast decides VISIBILITY against the
           base (a swatch invisible on the base is useless regardless of
           its colour), and share exists because contrast alone does not
           catch a near-white anti-aliasing fleck - it has HIGH contrast
           against a dark base and would otherwise sail through.
        2. If tier 1 is empty, swatches that clear MIN_EDGE_CONTRAST alone
           (the share floor dropped).
        Within tiers 1 and 2, the winner is the highest-CHROMA swatch -
        `(max(r,g,b) - min(r,g,b)) / 255`, which replaces HLS saturation as
        the ranking metric because saturation is degenerate at BOTH
        lightness extremes: a near-black AND a near-white pixel both report
        saturation ~1.0 regardless of visible colour, where chroma is near
        0 for both. With nothing left that has real chroma (< MIN_CHROMA, a
        greyscale mark) the BRIGHTEST candidate wins instead, because an
        edge the colour of the base is an invisible frame.
        3. If NOTHING clears MIN_EDGE_CONTRAST at all (any non-base
           swatch) - the last resort, reached only when no candidate is
           properly visible against the base. Here the winner is the
           HIGHEST-CONTRAST swatch outright, never chroma or brightness:
           those are not proxies for visibility, and against a LIGHT base
           they are inversely related to it (the darker of two washed-out
           greys is the one with more contrast, not the brighter one).
           Ranking this tier by chroma/brightness the way tiers 1-2 do was
           a regression - it happened to look right on a dark base, where
           brighter and higher-contrast coincide, and wrong on a light one.
           At this point nothing is properly visible, so visibility
           (contrast) is the only thing left worth maximising.
    - `accent` is drawn from the same candidate pool and ranking as `edge`,
      the next entry after it in that ranking (highest-chroma in tiers 1-2,
      highest-contrast in tier 3), else a darkened edge. Only channel
      decorations use it, so a reasonable fallback beats failing.
    - `text` is whichever of LIGHT_TEXT/DARK_TEXT has more contrast against
      the chosen base.

    A mark with only ONE swatch yields only `base` and `text`: the caller
    fills what it was given and leaves the rest alone, rather than being
    handed invented colours it cannot tell apart from measured ones.
    """
    found = swatches(path)
    # The `or found` fallback is UNREACHABLE at today's MAX_SWATCHES: at most 8
    # swatches sum to 100% of the opaque pixels, so one of them holds at least
    # 12.5% - comfortably over MIN_BASE_SHARE's 5%. It is kept rather than
    # removed because that argument is a consequence of the two constants, not
    # of anything structural: raise MAX_SWATCHES past 20 and a logo of many
    # near-equal colours makes it reachable, at which point silently having no
    # base would be far worse than this line.
    candidates = [s for s in found if s.share >= MIN_BASE_SHARE] or found
    base = min(candidates, key=lambda swatch: _luminance(swatch.hex)).hex

    roles = {"base": base}
    roles["text"] = max(
        (LIGHT_TEXT, DARK_TEXT), key=lambda candidate: _contrast(candidate, base))

    others = [s for s in found if s.hex != base]
    if not others:
        return roles, found

    strict = [
        s for s in others
        if _contrast(s.hex, base) >= MIN_EDGE_CONTRAST and s.share >= MIN_EDGE_SHARE
    ]
    contrasted = [s for s in others if _contrast(s.hex, base) >= MIN_EDGE_CONTRAST]

    if strict or contrasted:
        # Tiers 1-2: something is properly visible against the base -
        # chroma picks the most colourful of the visible candidates, with a
        # brightness fallback for a genuinely neutral pool.
        pool = strict or contrasted
        ranked = sorted(pool, key=lambda swatch: _chroma(swatch.hex), reverse=True)
        edge = ranked[0].hex
        if _chroma(edge) < MIN_CHROMA:
            edge = max(pool, key=lambda swatch: _luminance(swatch.hex)).hex
    else:
        # Tier 3 (last resort): nothing clears the contrast floor at all,
        # so no candidate is properly visible against the base. Chroma and
        # brightness are not stand-ins for visibility here - maximise
        # contrast itself instead.
        pool = others
        ranked = sorted(pool, key=lambda swatch: _contrast(swatch.hex, base), reverse=True)
        edge = ranked[0].hex
    roles["edge"] = edge

    remaining = [s for s in ranked if s.hex != edge]
    if remaining:
        roles["accent"] = remaining[0].hex
    else:
        roles["accent"] = _hex(tuple(int(channel * 0.6) for channel in _rgb(edge)))
    return roles, found
