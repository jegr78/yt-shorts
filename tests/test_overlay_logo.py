"""Tests for the logo capability in yt_shorts.overlay.build_overlay.

The overlay draws shapes and text only today; embedding an image is new.
The core risk: a tall logo must shrink the space the hook has to lay out
in, otherwise hook text collides with the logo or spills into the video
window - exactly the bug class the existing overflow guard was built to
prevent. These tests measure actual pixels, not just that no exception was
raised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from yt_shorts import overlay, profile
from yt_shorts.overlay import build_overlay
from yt_shorts.profile import load as profile_load

ROOT = Path(__file__).resolve().parent.parent
CHANNEL_DIR = ROOT / "tests" / "fixtures" / "channels" / "erf"


def _white_pixel_coordinates(image):
    width = image.size[0]
    coordinates = []
    for index, pixel in enumerate(image.getdata()):
        if pixel[:3] == (255, 255, 255) and pixel[3] > 0:
            coordinates.append((index % width, index // width))
    return coordinates


def _yellow_pixel_coordinates(image):
    width = image.size[0]
    coordinates = []
    for index, pixel in enumerate(image.getdata()):
        if pixel[:3] == (255, 255, 0) and pixel[3] > 0:
            coordinates.append((index % width, index // width))
    return coordinates


@pytest.fixture
def config():
    return profile_load("erf/community-clips-back-catalogue").config


@pytest.fixture
def tall_logo_path(tmp_path):
    """A deliberately tall, wide logo - big enough to meaningfully eat into
    the upper band's vertical budget, the case that stresses the guard."""
    path = tmp_path / "logo.png"
    Image.new("RGBA", (900, 300), (255, 255, 0, 255)).save(path)
    return path


@pytest.fixture
def config_with_logo(config, tall_logo_path):
    logo_config = dict(config)
    logo_config["logo"] = {"file": str(tall_logo_path), "max_height": 220, "gap": 24}
    return logo_config


class TestLogoIsDrawn:
    def test_logo_pixels_appear_near_the_top(self, config_with_logo):
        image = build_overlay("BOX THIS LAP", "", config_with_logo)
        yellow = _yellow_pixel_coordinates(image.crop((0, 0, 1080, 260)))
        assert yellow, "Logo does not appear to have been drawn near the top of the band"

    def test_logo_is_horizontally_centered(self, config_with_logo):
        image = build_overlay("", "", config_with_logo)
        yellow = _yellow_pixel_coordinates(image)
        xs = [x for x, _ in yellow]
        width = config_with_logo["output"]["width"]
        # center of mass of the logo should be close to the frame's center
        center = sum(xs) / len(xs)
        assert abs(center - width / 2) < 5

    def test_no_logo_key_draws_no_extra_pixels(self, config):
        """Baseline: without a 'logo' key, config must behave exactly as
        before - no yellow pixels, nothing new drawn."""
        image = build_overlay("BOX THIS LAP", "", config)
        assert _yellow_pixel_coordinates(image) == []


class TestHookRespectsLogoSpace:
    def test_hook_pixels_start_below_the_logo_and_gap(self, config_with_logo):
        image = build_overlay("BOX THIS LAP", "", config_with_logo)
        white = _white_pixel_coordinates(image)
        assert white, "Hook text does not appear to have been drawn"
        logo_conf = config_with_logo["logo"]
        top_bound = overlay.MARGIN + logo_conf["max_height"] + logo_conf["gap"]
        ys = [y for _, y in white]
        assert min(ys) >= top_bound, \
            "Hook text starts above the reserved logo space"

    def test_hook_and_logo_do_not_share_any_row_of_pixels(self, config_with_logo):
        """Stronger check than the bound above: no row that has a hook pixel
        may also have a logo pixel."""
        image = build_overlay("BOX THIS LAP", "", config_with_logo)
        white_ys = {y for _, y in _white_pixel_coordinates(image)}
        yellow_ys = {y for _, y in _yellow_pixel_coordinates(image)}
        assert not (white_ys & yellow_ys)


class TestOverflowProtectionWithLogo:
    """Same stress cases as the plain overflow guard (tests/test_overlay.py),
    repeated with a tall logo present - the case most likely to break if the
    guard doesn't account for the logo's reserved space."""

    def _assert_hook_within_bounds(self, image, config_with_logo):
        logo_conf = config_with_logo["logo"]
        top_bound = overlay.MARGIN + logo_conf["max_height"] + logo_conf["gap"]
        window_top = config_with_logo["output"]["video_y"]
        width = config_with_logo["output"]["width"]

        pixels = _white_pixel_coordinates(image)
        assert pixels, "Hook text does not appear to have been drawn"
        xs = [x for x, _ in pixels]
        ys = [y for _, y in pixels]

        assert min(xs) >= overlay.MARGIN, "Text runs out of the left margin"
        assert max(xs) <= width - overlay.MARGIN, "Text runs out of the right margin"
        assert min(ys) >= top_bound, "Text overlaps the logo/gap area"
        assert max(ys) < window_top, "Text reaches into or under the video window"

    def test_single_80_char_word(self, config_with_logo):
        hook = "A" * 80
        image = build_overlay(hook, "", config_with_logo)
        self._assert_hook_within_bounds(image, config_with_logo)

    def test_100_short_words(self, config_with_logo):
        hook = " ".join(["banana"] * 100)
        image = build_overlay(hook, "", config_with_logo)
        self._assert_hook_within_bounds(image, config_with_logo)

    def test_500_short_words(self, config_with_logo):
        hook = " ".join(["banana"] * 500)
        image = build_overlay(hook, "", config_with_logo)
        self._assert_hook_within_bounds(image, config_with_logo)


class TestWatermarkPlacement:
    """The '*-right' corner watermark: a small logo that reserves NO layout
    space, stays inside a veil band (never over the video window) and is
    right-aligned, clear of the centered hook and the footer."""

    @pytest.fixture
    def wm_config(self, config, tall_logo_path):
        c = dict(config)
        c["logo"] = {"file": str(tall_logo_path), "position": "top-right",
                     "max_height": 96, "margin": 44, "opacity": 1.0}
        return c

    def test_watermark_reserves_no_hook_space(self, config, wm_config):
        """A watermark must not push the hook down - the first hook row is
        identical to the no-logo baseline (unlike the centered 'top' badge)."""
        baseline = build_overlay("BOX THIS LAP", "", config)
        watermarked = build_overlay("BOX THIS LAP", "", wm_config)
        base_top = min(y for _, y in _white_pixel_coordinates(baseline))
        wm_top = min(y for _, y in _white_pixel_coordinates(watermarked))
        assert base_top == wm_top

    def test_top_right_sits_in_the_upper_band_corner(self, wm_config):
        image = build_overlay("", "", wm_config)
        yellow = _yellow_pixel_coordinates(image)
        assert yellow, "watermark not drawn"
        xs = [x for x, _ in yellow]
        ys = [y for _, y in yellow]
        width = wm_config["output"]["width"]
        margin = wm_config["logo"]["margin"]
        assert max(ys) < wm_config["output"]["video_y"], "watermark reaches the video window"
        assert min(ys) < 200, "top-right watermark is not near the top"
        assert max(xs) <= width - margin, "watermark runs past the right margin"
        assert min(xs) > width // 2, "top-right watermark is not on the right"

    def test_bottom_right_sits_in_the_corner(self, wm_config):
        c = dict(wm_config)
        c["logo"] = {**wm_config["logo"], "position": "bottom-right"}
        image = build_overlay("", "", c)
        yellow = _yellow_pixel_coordinates(image)
        assert yellow, "watermark not drawn"
        xs = [x for x, _ in yellow]
        ys = [y for _, y in yellow]
        output = c["output"]
        width, height = output["width"], output["height"]
        window_bottom = output["video_y"] + output["video_height"]
        margin = c["logo"]["margin"]
        assert min(ys) >= window_bottom, "watermark overlaps the video window"
        assert max(ys) >= overlay._footer_top(height), "watermark is not down in the corner"
        assert max(ys) <= height - margin, "watermark runs off the bottom"
        assert max(xs) <= width - margin, "watermark runs past the right margin"
        assert min(xs) > width // 2, "bottom-right watermark is not on the right"

    def test_footer_keeps_clear_of_bottom_right_logo(self, wm_config):
        """A long footer must dodge the corner watermark - every footer pixel
        stays left of the logo, and the footer is still drawn (not dropped)."""
        c = dict(wm_config)
        c["logo"] = {**wm_config["logo"], "position": "bottom-right"}
        # empty hook, so the only white pixels are the footer text
        image = build_overlay("", "IRO | @INTERNATIONAL-RACING-ORG", c)
        footer = _white_pixel_coordinates(image)
        yellow = _yellow_pixel_coordinates(image)
        assert footer, "footer text was not drawn"
        assert yellow, "watermark not drawn"
        logo_left = min(x for x, _ in yellow)
        assert max(x for x, _ in footer) < logo_left, "footer text runs into the logo"

    def test_footer_is_vertically_centered_on_bottom_right_logo(self, wm_config):
        """The footer's visible text must sit on the logo's midline, not below it."""
        c = dict(wm_config)
        c["logo"] = {**wm_config["logo"], "position": "bottom-right"}
        image = build_overlay("", "CLR | @COMMUNITYLEAGUERACING", c)
        footer_ys = [y for _, y in _white_pixel_coordinates(image)]
        logo_ys = [y for _, y in _yellow_pixel_coordinates(image)]
        assert footer_ys and logo_ys
        footer_center = (min(footer_ys) + max(footer_ys)) / 2
        logo_center = (min(logo_ys) + max(logo_ys)) / 2
        assert abs(footer_center - logo_center) <= 6, \
            f"footer center {footer_center} not aligned to logo center {logo_center}"

    def test_opacity_scales_the_alpha_channel(self):
        """opacity < 1 dims the logo by scaling its alpha; >= 1 is a no-op.
        Tested on _apply_opacity directly - once composited over the veil a
        semi-transparent logo blends with it, so its colour is no longer a
        clean signal."""
        logo = Image.new("RGBA", (10, 10), (255, 255, 0, 255))
        dimmed = overlay._apply_opacity(logo, 0.5)
        assert dimmed.split()[3].getextrema() == (128, 128)  # round(255 * 0.5)
        assert overlay._apply_opacity(logo, 1.0) is logo


class TestLogoValidation:
    """profile._validate_logo guards the new placement fields so a bad brand.json
    is a clean ProfileError, not a silently-ignored typo."""

    @pytest.fixture
    def logo_file(self, tmp_path):
        path = tmp_path / "logo.png"
        Image.new("RGBA", (10, 10), (255, 255, 0, 255)).save(path)
        return path

    def test_bad_position_is_rejected(self, tmp_path, logo_file):
        cfg = {"logo": {"file": str(logo_file), "position": "left"}}
        problems = profile._validate_logo(cfg, tmp_path / "brand.json")
        assert any("position" in p for p in problems)

    def test_bad_opacity_is_rejected(self, tmp_path, logo_file):
        for bad in (0, 1.5, "x", True):
            cfg = {"logo": {"file": str(logo_file), "opacity": bad}}
            problems = profile._validate_logo(cfg, tmp_path / "brand.json")
            assert any("opacity" in p for p in problems), f"opacity {bad!r} was accepted"

    def test_valid_watermark_is_accepted(self, tmp_path, logo_file):
        cfg = {"logo": {"file": str(logo_file), "position": "bottom-right", "opacity": 0.8}}
        assert profile._validate_logo(cfg, tmp_path / "brand.json") == []


class TestLogoVariant:
    """logo.variant (color/white/black) selects a monochrome sibling of the base
    logo file by naming convention, so an event can switch it with just
    {"logo": {"variant": "white"}} and inherit position/opacity via deep_merge."""

    @pytest.fixture
    def channel_dir(self, tmp_path):
        assets = tmp_path / "assets"
        assets.mkdir()
        for name in ("logo.png", "logo-white.png", "logo-black.png"):
            Image.new("RGBA", (10, 10), (255, 255, 0, 255)).save(assets / name)
        return tmp_path

    def test_variant_file_convention(self):
        assert profile._variant_file("assets/logo.png", "white") == "assets/logo-white.png"
        assert profile._variant_file("assets/logo.png", "black") == "assets/logo-black.png"
        assert profile._variant_file("assets/logo.png", "color") == "assets/logo.png"
        assert profile._variant_file("assets/logo.png", None) == "assets/logo.png"

    def test_resolve_applies_variant(self, channel_dir):
        config = {"logo": {"file": "assets/logo.png", "variant": "white"}}
        profile._resolve_logo(config, channel_dir, channel_dir)
        assert Path(config["logo"]["file"]).as_posix().endswith("assets/logo-white.png")
        assert Path(config["logo"]["file"]).is_file()

    def test_color_variant_keeps_base_file(self, channel_dir):
        config = {"logo": {"file": "assets/logo.png", "variant": "color"}}
        profile._resolve_logo(config, channel_dir, channel_dir)
        assert Path(config["logo"]["file"]).as_posix().endswith("assets/logo.png")

    def test_bad_variant_is_rejected(self, channel_dir):
        config = {"logo": {"file": "assets/logo.png", "variant": "silver"}}
        profile._resolve_logo(config, channel_dir, channel_dir)
        problems = profile._validate_logo(config, channel_dir / "brand.json")
        assert any("variant" in p for p in problems)

    def test_missing_variant_file_is_reported(self, tmp_path):
        assets = tmp_path / "assets"
        assets.mkdir()
        Image.new("RGBA", (10, 10)).save(assets / "logo.png")  # only the color file exists
        config = {"logo": {"file": "assets/logo.png", "variant": "white"}}
        profile._resolve_logo(config, tmp_path, tmp_path)
        problems = profile._validate_logo(config, tmp_path / "brand.json")
        assert any("not found" in p for p in problems)

    def test_valid_variant_is_accepted(self, channel_dir):
        config = {"logo": {"file": "assets/logo.png", "variant": "black",
                           "position": "bottom-right"}}
        profile._resolve_logo(config, channel_dir, channel_dir)
        assert profile._validate_logo(config, channel_dir / "brand.json") == []
