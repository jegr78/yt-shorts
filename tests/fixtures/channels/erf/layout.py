"""ERF's own decoration: the slanted parallelogram motif.

Loaded dynamically by yt_shorts.profile when a layout.py with a function
'decorate' is found in the channel folder. A channel without layout.py gets
plain bars instead (see yt_shorts.overlay.build_overlay).

Deliberately kept self-contained (no imports from yt_shorts.overlay): a
channel folder should get by without knowledge of the core package's
internal helpers.
"""

from __future__ import annotations

from PIL import ImageColor, ImageDraw

ALPHA_PARALLELOGRAM = 200  # visible, but not opaque


def _with_alpha(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    r, g, b = ImageColor.getrgb(hex_color)
    return (r, g, b, alpha)


def decorate(draw: ImageDraw.ImageDraw, config: dict, window_top: int, window_bottom: int) -> None:
    """Draws the slanted accent-colored surface into both bands, semi-transparent.

    The cut offset (config["output"]["accent_offset"]) is the same number
    that overlay.build_overlay uses for the space to keep clear at the
    bottom of the hook text - one source of truth instead of two constants
    that could drift apart.
    """
    width = config["output"]["width"]
    offset = config["output"]["accent_offset"]
    accent = _with_alpha(config["colors"]["accent"], ALPHA_PARALLELOGRAM)

    draw.polygon(
        [(0, window_top - 1), (width, window_top - 1),
         (width, window_top - 1 - offset * 2), (0, window_top - 1 - offset)],
        fill=accent,
    )
    draw.polygon(
        [(0, window_bottom), (width, window_bottom),
         (width, window_bottom + offset), (0, window_bottom + offset * 2)],
        fill=accent,
    )
