from pathlib import Path

import pytest
from PIL import ImageFont

from yt_shorts import overlay
from yt_shorts.overlay import build_overlay, wrap_text
from yt_shorts.profile import load as profile_load

ROOT = Path(__file__).resolve().parent.parent
CHANNEL_DIR = ROOT / "tests" / "fixtures" / "channels" / "erf"


def _white_pixel_coordinates(image):
    """Returns (x, y) of every opaque white pixel in the image.

    Uses getdata() instead of getpixel() per coordinate, because that's
    noticeably faster on a full 1080x1920 image.
    """
    width = image.size[0]
    coordinates = []
    for index, pixel in enumerate(image.getdata()):
        if pixel[:3] == (255, 255, 255) and pixel[3] > 0:
            coordinates.append((index % width, index // width))
    return coordinates


@pytest.fixture
def config():
    return profile_load("erf/community-clips-back-catalogue").config


class TestWrapText:
    def test_short_text_stays_on_one_line(self):
        font = ImageFont.truetype(str(CHANNEL_DIR / "fonts" / "Oswald-Bold.ttf"), 80)
        assert wrap_text("BOX THIS LAP", font, 1000) == ["BOX THIS LAP"]

    def test_long_text_gets_wrapped(self):
        font = ImageFont.truetype(str(CHANNEL_DIR / "fonts" / "Oswald-Bold.ttf"), 80)
        lines = wrap_text("TWENTY FOUR HOURS AND IT COMES DOWN TO THIS", font, 1000)
        assert len(lines) > 1
        assert " ".join(lines) == "TWENTY FOUR HOURS AND IT COMES DOWN TO THIS"

    def test_overlong_single_word_stays_its_own_line(self):
        font = ImageFont.truetype(str(CHANNEL_DIR / "fonts" / "Oswald-Bold.ttf"), 80)
        assert wrap_text("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", font, 200) == ["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]


class TestBuildOverlay:
    def test_dimensions_are_correct(self, config):
        image = build_overlay("BOX THIS LAP", "ERF | @ERFofficial", config)
        assert image.size == (1080, 1920)
        assert image.mode == "RGBA"

    def test_video_window_is_transparent(self, config):
        image = build_overlay("BOX THIS LAP", "ERF | @ERFofficial", config)
        # Middle of the video window: y=600..1208
        assert image.getpixel((540, 900))[3] == 0

    def test_bands_are_semi_transparent(self, config):
        """The base surface of the bands is a darkening veil, not an opaque
        fill: the blurred video background must be able to show through."""
        image = build_overlay("BOX THIS LAP", "ERF | @ERFofficial", config)
        top = image.getpixel((20, 20))[3]        # top band, away from text/motif
        bottom = image.getpixel((20, 1900))[3]    # bottom band, away from text/motif
        assert 0 < top < 255
        assert 0 < bottom < 255

    def test_edge_stays_opaque(self, config):
        """The edges directly at the video window frame the sharp image and
        therefore stay fully opaque."""
        image = build_overlay("BOX THIS LAP", "ERF | @ERFofficial", config)
        assert image.getpixel((540, 596))[3] == 255    # top edge
        assert image.getpixel((540, 1210))[3] == 255   # bottom edge

    def test_hook_leaves_white_pixels(self, config):
        image = build_overlay("BOX THIS LAP", "ERF | @ERFofficial", config)
        top = image.crop((0, 0, 1080, 600))
        white = sum(1 for p in top.getdata() if p[:3] == (255, 255, 255) and p[3] == 255)
        assert white > 2000, "Hook text does not appear to have been drawn"

    def test_empty_hook_is_allowed(self, config):
        image = build_overlay("", "ERF | @ERFofficial", config)
        assert image.size == (1080, 1920)


class TestOverflowProtection:
    """Covers both findings measured by the reviewer: an overly long single
    word must never run out of the image sideways (finding 1), and too many
    lines must never make the top bar reach down towards the video window
    (finding 2). Both tests measure actual white text pixels instead of
    only indirectly checking the size search."""

    def test_long_single_word_without_spaces_stays_within_the_frame_sideways(self, config):
        hook = "A" * 80  # a single 80-character word without spaces
        image = build_overlay(hook, "", config)  # empty footer: isolates the hook pixels
        pixels = _white_pixel_coordinates(image)
        assert pixels, "Hook text does not appear to have been drawn"
        xs = [x for x, _ in pixels]
        assert min(xs) >= overlay.MARGIN, "Text runs out of the allowed margin on the left"
        assert max(xs) <= config["output"]["width"] - overlay.MARGIN, \
            "Text runs out of the allowed margin on the right"

    def test_many_short_words_do_not_touch_the_video_window(self, config):
        hook = " ".join(["banana"] * 100)  # 100 short words, far too many lines
        image = build_overlay(hook, "", config)  # empty footer: isolates the hook pixels
        window_top = config["output"]["video_y"]
        # Deliberately NOT cropped to the allowed area: otherwise overflowing
        # pixels below the bar would be hidden by the crop itself and the
        # test would falsely pass.
        pixels = _white_pixel_coordinates(image)
        assert pixels, "Hook text does not appear to have been drawn"
        ys = [y for _, y in pixels]
        assert max(ys) < window_top, \
            "Hook text reaches into or under the video window"

    def test_long_text_with_spaces_stays_fully_within_the_band(self, config):
        hook = " ".join(["Word"] * 25)  # ~124 characters with spaces, many lines needed
        assert len(hook) > 120
        image = build_overlay(hook, "", config)  # empty footer: isolates the hook pixels
        width = config["output"]["width"]
        window_top = config["output"]["video_y"]
        pixels = _white_pixel_coordinates(image)
        assert pixels, "Hook text does not appear to have been drawn"
        xs = [x for x, _ in pixels]
        ys = [y for _, y in pixels]
        assert min(xs) >= overlay.MARGIN
        assert max(xs) <= width - overlay.MARGIN
        assert max(ys) < window_top


class TestBandOpacities:
    def test_an_absent_section_is_full_strength(self):
        assert overlay.band_opacities({}) == {"top": 1.0, "bottom": 1.0}

    def test_an_absent_key_is_full_strength(self):
        assert overlay.band_opacities({"bands": {"top": 0.25}}) == {"top": 0.25, "bottom": 1.0}

    def test_an_int_becomes_a_float(self):
        values = overlay.band_opacities({"bands": {"top": 0, "bottom": 1}})
        assert values == {"top": 0.0, "bottom": 1.0}
        assert all(isinstance(v, float) for v in values.values())

    def test_a_malformed_section_falls_back_rather_than_raising(self):
        """Validation is profile._validate_brand's job and runs before this
        is ever called on a loaded profile. This must still not raise for a
        caller that hands it a raw dict - build_overlay is also called
        directly by the studio's preview on an UNSAVED brand."""
        assert overlay.band_opacities({"bands": "nope"}) == {"top": 1.0, "bottom": 1.0}

    def test_an_out_of_range_value_is_clamped(self):
        """The studio's live preview renders an UNSAVED brand, so
        profile._validate_brand has not run on this path. A factor above 1
        would compute an alpha past 255 in _fade_bands' lookup table."""
        assert overlay.band_opacities({"bands": {"top": 5, "bottom": -2}}) == {
            "top": 1.0, "bottom": 0.0}

    def test_a_nan_degrades_to_full_strength(self):
        """json.loads accepts the bare NaN token, so this is reachable from a
        hand-edited brand.json as well as from the preview. NaN compares
        False against everything, so a plain min/max clamp would pass it
        straight through to int(), which raises."""
        assert overlay.band_opacities({"bands": {"top": float("nan")}}) == {
            "top": 1.0, "bottom": 1.0}


class TestBandFading:
    """Uses this module's existing `config` fixture (the loaded erf profile),
    never a hand-built dict: a fading rule proven only against a synthetic
    config would not be proven against the real one, which carries a
    layout.py decoration and an accent_offset."""

    def _with(self, config, bands):
        """A COPY of the fixture's config carrying a bands section. A copy so
        one test can render the same profile both faded and unfaded without
        the first render's mutation reaching the second."""
        return {**config, "bands": bands}

    def _alpha(self, image, x, y):
        return image.getpixel((x, y))[3]

    def test_full_strength_leaves_the_veil_at_its_usual_alpha(self, config):
        image = build_overlay("HOOK", "footer", config)
        assert self._alpha(image, 40, 40) == overlay.ALPHA_BASE

    def test_zero_removes_the_veil_from_that_band_only(self, config):
        window_bottom = config["output"]["video_y"] + config["output"]["video_height"]
        # The erf fixture's own decoration (the slanted parallelogram in its
        # layout.py) reaches up to accent_offset*2 below window_bottom at the
        # left edge, so window_bottom+40 would land ON the decoration rather
        # than the plain veil at this fixture's accent_offset (48). Go past
        # its reach so this checks the veil, not the decoration's own alpha.
        safe_y = window_bottom + config["output"].get("accent_offset", 0) * 2 + 20
        image = build_overlay("HOOK", "footer", self._with(config, {"top": 0.0}))
        assert self._alpha(image, 40, 40) == 0
        assert self._alpha(image, 40, safe_y) == overlay.ALPHA_BASE

    def test_zero_removes_the_edge_accent_too(self, config):
        """The edge is a surface, not content - decision 3 of the spec."""
        window_top = config["output"]["video_y"]
        full = build_overlay("HOOK", "footer", config)
        faded = build_overlay("HOOK", "footer", self._with(config, {"top": 0.0}))
        assert self._alpha(full, 40, window_top - 3) == overlay.ALPHA_OPAQUE
        assert self._alpha(faded, 40, window_top - 3) == 0

    def test_a_half_factor_halves_every_surface_in_that_band(self, config):
        window_top = config["output"]["video_y"]
        faded = build_overlay("HOOK", "footer", self._with(config, {"top": 0.5}))
        assert self._alpha(faded, 40, 40) == int(overlay.ALPHA_BASE * 0.5)
        assert self._alpha(faded, 40, window_top - 3) == int(overlay.ALPHA_OPAQUE * 0.5)

    def test_the_hook_survives_a_zero_band(self, config):
        """Decision 2: the surfaces fade, the content does not. Without this
        the feature would silently delete the hook along with the veil."""
        window_top = config["output"]["video_y"]
        image = build_overlay("HOOK", "footer", self._with(config, {"top": 0.0}))
        band = image.crop((0, 0, image.width, window_top))
        assert max(pixel[3] for pixel in band.getdata()) == 255

    def test_the_footer_survives_a_zero_band(self, config):
        window_bottom = config["output"]["video_y"] + config["output"]["video_height"]
        image = build_overlay("HOOK", "footer", self._with(config, {"bottom": 0.0}))
        band = image.crop((0, window_bottom, image.width, image.height))
        assert max(pixel[3] for pixel in band.getdata()) == 255

    def test_the_video_window_stays_fully_transparent_at_every_factor(self, config):
        """The one invariant that must never bend: the window is where the
        burned-in timing tower and leaderboard live."""
        window_top = config["output"]["video_y"]
        for bands in ({"top": 1.0, "bottom": 1.0}, {"top": 0.0, "bottom": 0.0},
                      {"top": 0.5, "bottom": 0.25}):
            image = build_overlay("HOOK", "footer", self._with(config, bands))
            assert self._alpha(image, 540, window_top + 10) == 0, bands

    def test_a_decoration_fades_with_its_band(self, config):
        """The reason the factor is applied to the image rather than passed
        into decorate(): a channel's own layout.py never learns about bands,
        and must not have to. The injected decoration replaces the erf
        fixture's own so the assertion can name an exact pixel."""
        def decorate(draw, _config, _window_top, _window_bottom):
            draw.rectangle([0, 100, 200, 140], fill=(255, 0, 0, 255))

        faded = {**self._with(config, {"top": 0.5}), "decorate": decorate}
        image = build_overlay("HOOK", "footer", faded)
        assert self._alpha(image, 50, 120) == int(255 * 0.5)
