"""Palette derivation from a channel logo.

Every fixture here is generated in-process rather than committed: the rules
under test are about colour relationships, and a synthetic image states the
relationship it is testing far more legibly than a PNG nobody can inspect in
a diff.
"""

from __future__ import annotations

import pytest
from PIL import Image

from yt_shorts import palette


def _logo(tmp_path, blocks, size=(100, 100), name="logo.png"):
    """An RGBA image built from (colour, pixel count) blocks, padded with
    fully transparent pixels - the shape of a real logo, which is mostly
    padding."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    pixels = image.load()
    x = y = 0
    for colour, count in blocks:
        for _ in range(count):
            pixels[x, y] = colour
            x += 1
            if x >= size[0]:
                x = 0
                y += 1
    path = tmp_path / name
    image.save(path)
    return path


class TestSwatches:
    def test_shares_are_of_opaque_pixels_only(self, tmp_path):
        """A logo is mostly transparent padding. Counting those pixels would
        make every logo's dominant colour the same one, so the shares must
        add up over the OPAQUE pixels alone."""
        path = _logo(tmp_path, [((0, 0, 0, 255), 300), ((255, 255, 255, 255), 100)])
        found = palette.swatches(path)
        total = sum(s.share for s in found)
        assert 0.99 <= total <= 1.01
        assert found[0].share == pytest.approx(0.75, abs=0.02)

    def test_ordered_by_share_descending(self, tmp_path):
        path = _logo(tmp_path, [((10, 10, 10, 255), 100), ((200, 30, 30, 255), 400)])
        found = palette.swatches(path)
        assert [s.share for s in found] == sorted((s.share for s in found), reverse=True)

    def test_a_semi_transparent_pixel_is_excluded(self, tmp_path):
        """Anti-aliased edges carry the blend of two colours and belong to
        neither."""
        path = _logo(tmp_path, [((0, 0, 0, 255), 200), ((255, 0, 0, 120), 200)])
        found = palette.swatches(path)
        assert all(s.hex != "#FF0000" for s in found)

    def test_a_missing_file_is_not_found(self, tmp_path):
        with pytest.raises(palette.PaletteError) as caught:
            palette.swatches(tmp_path / "nope.png")
        assert caught.value.kind == "not_found"

    def test_a_non_image_is_unreadable(self, tmp_path):
        path = tmp_path / "logo.png"
        path.write_text("not a png", encoding="utf-8")
        with pytest.raises(palette.PaletteError) as caught:
            palette.swatches(path)
        assert caught.value.kind == "unreadable"

    def test_a_fully_transparent_image_is_empty(self, tmp_path):
        path = _logo(tmp_path, [])
        with pytest.raises(palette.PaletteError) as caught:
            palette.swatches(path)
        assert caught.value.kind == "empty"


class TestDerive:
    def test_base_is_the_dark_colour_and_edge_the_bright_one(self, tmp_path):
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["base"] == "#050505"
        assert roles["edge"] == "#DC1E14"

    def test_text_is_white_on_a_dark_base(self, tmp_path):
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["text"] == palette.LIGHT_TEXT

    def test_text_is_dark_on_a_light_base(self, tmp_path):
        """A light mark on nothing else - the veil ends up light, so white
        text would be unreadable on it."""
        path = _logo(tmp_path, [((240, 235, 225, 255), 500), ((250, 248, 245, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["text"] == palette.DARK_TEXT

    def test_a_rare_dark_speck_does_not_become_the_base(self, tmp_path):
        """MIN_BASE_SHARE exists for exactly this: a handful of near-black
        anti-aliasing pixels on an otherwise light mark must not be chosen as
        the channel's ground colour."""
        path = _logo(tmp_path, [((250, 250, 250, 255), 980), ((0, 0, 0, 255), 20)])
        roles, _ = palette.derive(path)
        assert roles["base"] != "#000000"

    def test_edge_is_never_the_base(self, tmp_path):
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["edge"] != roles["base"]

    def test_a_near_black_fleck_does_not_beat_a_lighter_contrasting_swatch(self, tmp_path):
        """The erfofficial case in miniature: a tiny near-black navy fleck
        (#00073B) reports HLS saturation 1.00 because one of its channels is
        exactly zero - a degenerate reading, not a real "most vivid colour"
        signal - and it must not outrank a much larger, lower-saturation
        light-blue swatch (#E1E8F9) that actually has contrast against the
        dark navy base (#012269). Without the contrast floor this fails,
        because #00073B's degenerate saturation of 1.00 beats #E1E8F9's 0.67."""
        path = _logo(tmp_path, [
            ((1, 34, 105, 255), 700),      # base, #012269, 70% share
            ((225, 232, 249, 255), 295),   # #E1E8F9, 29.5% share, real contrast
            ((0, 7, 59, 255), 5),          # #00073B, 0.5% share, near-black fleck
        ])
        roles, _ = palette.derive(path)
        assert roles["base"] == "#012269"
        assert roles["edge"] == "#E1E8F9"
        assert roles["edge"] != "#00073B"

    def test_edge_always_clears_the_contrast_floor_when_any_swatch_can(self, tmp_path):
        path = _logo(tmp_path, [
            ((1, 34, 105, 255), 700),      # base
            ((225, 232, 249, 255), 295),   # clears contrast, real edge
            ((0, 7, 59, 255), 5),          # near-black fleck, fails contrast
        ])
        roles, _ = palette.derive(path)
        assert palette._contrast(roles["edge"], roles["base"]) >= palette.MIN_EDGE_CONTRAST

    def test_when_nothing_clears_the_floor_the_highest_contrast_swatch_wins(self, tmp_path):
        """All the non-base swatches are close in darkness to the base, so
        none clears MIN_EDGE_CONTRAST - the fallback must still pick the best
        available option (the one with the most contrast) rather than
        leaving `edge` unset or falling back to raw saturation."""
        path = _logo(tmp_path, [
            ((10, 10, 10, 255), 600),   # base, very dark
            ((40, 10, 10, 255), 200),   # a bit lighter, low contrast, higher "saturation"
            ((30, 30, 30, 255), 200),   # a bit lighter still, best contrast of the two
        ])
        roles, found = palette.derive(path)
        others = [s for s in found if s.hex != roles["base"]]
        assert all(
            palette._contrast(s.hex, roles["base"]) < palette.MIN_EDGE_CONTRAST
            for s in others
        )
        best = max(others, key=lambda s: palette._contrast(s.hex, roles["base"]))
        assert roles["edge"] == best.hex

    def test_the_last_resort_tier_picks_the_best_contrast_not_the_brightest(self, tmp_path):
        """Finding 1: a regression from the fix that collapsed all three
        tiers into `pool = strict or contrasted or others`, ranked
        uniformly by chroma with a brightness fallback for the neutral
        case. That ranking is only a stand-in for visibility on a DARK
        base, where "brighter" and "more contrast" coincide - it is the
        WRONG stand-in on a LIGHT base, where the darker of two washed-out
        swatches is the one with more contrast. Reviewer's synthetic case:
        against a light base (#C8C8C8), neither #787878 (contrast 2.64,
        the darker grey) nor #FFFFF5 (contrast 1.66, the near-white)
        clears MIN_EDGE_CONTRAST, so this falls to the last-resort tier -
        and the right answer there is the swatch with the MOST contrast
        (#787878), not the brightest one (#FFFFF5), which the old chroma/
        brightness ranking picked instead."""
        path = _logo(tmp_path, [
            ((200, 200, 200, 255), 700),   # base, light grey, #C8C8C8
            ((120, 120, 120, 255), 30),    # darker grey - the right pick
            ((255, 255, 245, 255), 270),   # near-white - brighter, less visible
        ])
        roles, found = palette.derive(path)
        others = [s for s in found if s.hex != roles["base"]]
        assert all(
            palette._contrast(s.hex, roles["base"]) < palette.MIN_EDGE_CONTRAST
            for s in others
        )
        assert roles["base"] == "#C8C8C8"
        assert roles["edge"] == "#787878"
        assert roles["edge"] != "#FFFFF5"

    def test_a_fleck_below_min_edge_share_loses_to_a_lower_chroma_real_colour(self, tmp_path):
        """Finding 2: MIN_EDGE_SHARE is the mechanism that closed the
        original bug (a high-chroma anti-aliasing fleck outranking the
        real brand colour), and nothing pinned it - deleting the share
        tier entirely still left every other test in this file green. Here
        the fleck (#FF69B4, chroma 0.588) clears the contrast floor easily
        but holds only ~1.5% share, below MIN_EDGE_SHARE (2%); the real
        colour (#E1E8F9, chroma 0.094, far LOWER) holds ~3.0%, above it.
        Without the share floor, tier 1 collapses to tier 2 and chroma
        alone picks the fleck; with it, the real colour wins."""
        path = _logo(tmp_path, [
            ((10, 10, 10, 255), 950),      # base, very dark, #0A0A0A
            ((225, 232, 249, 255), 30),    # real edge colour, ~3.0% share, low chroma
            ((255, 105, 180, 255), 15),    # fleck, ~1.5% share, high chroma
        ])
        roles, found = palette.derive(path)
        others = [s for s in found if s.hex != roles["base"]]
        assert any(s.share < palette.MIN_EDGE_SHARE for s in others)
        assert roles["base"] == "#0A0A0A"
        assert roles["edge"] == "#E1E8F9"
        assert roles["edge"] != "#FF69B4"

    def test_a_greyscale_logo_still_yields_a_visible_edge(self, tmp_path):
        """With no saturated colour to pick, the brightest swatch is the one
        that reads against a dark base. Returning the base again would give
        the operator an invisible frame."""
        path = _logo(tmp_path, [((20, 20, 20, 255), 500), ((230, 230, 230, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["edge"] == "#E6E6E6"

    def test_a_near_white_fleck_does_not_beat_a_larger_vivid_colour(self, tmp_path):
        """The reviewer's counterexample: a near-white fleck (#FFFAFD) has a
        DEGENERATE HLS saturation of 1.00 - the same failure mode as the
        near-black fleck above, but at the OPPOSITE lightness extreme, which
        the contrast floor alone does not catch (a near-white swatch has HIGH
        contrast against a dark base and sails through). Chroma is not
        degenerate at either extreme: #FFFAFD's chroma is only 0.02 against
        #1EB45A's 0.588, so ranking by chroma instead of saturation picks the
        genuinely vivid green even though the fleck holds enough share
        (5%, above MIN_EDGE_SHARE) and enough contrast to be a candidate at
        all."""
        path = _logo(tmp_path, [
            ((16, 16, 16, 255), 700),      # base, dark
            ((30, 180, 90, 255), 250),     # #1EB45A, 25% share, vivid green
            ((255, 250, 253, 255), 50),    # #FFFAFD, 5% share, near-white fleck
        ])
        roles, _ = palette.derive(path)
        assert roles["edge"] == "#1EB45A"
        assert roles["edge"] != "#FFFAFD"

    def test_a_multi_candidate_neutral_palette_picks_the_brighter_swatch(self, tmp_path):
        """Finding 2: a fixture with only ONE surviving candidate cannot
        distinguish 'brightest of the pool' from 'first of the pool', so the
        greyscale fallback branch was never actually exercised. Here TWO
        genuinely neutral swatches (zero chroma) both clear the contrast and
        share floors - a mid grey at the LARGER share, a brighter near-white
        at the smaller share - and only the brightness rule, not list order
        or share, can pick the correct one."""
        path = _logo(tmp_path, [
            ((10, 10, 10, 255), 600),      # base, very dark
            ((120, 120, 120, 255), 300),   # mid grey, larger share, neutral
            ((225, 225, 225, 255), 100),   # brighter near-white, smaller share, neutral
        ])
        roles, found = palette.derive(path)
        others = [s for s in found if s.hex != roles["base"]]
        assert all(palette._chroma(s.hex) < palette.MIN_CHROMA for s in others)
        assert all(s.share >= palette.MIN_EDGE_SHARE for s in others)
        assert all(
            palette._contrast(s.hex, roles["base"]) >= palette.MIN_EDGE_CONTRAST
            for s in others
        )
        assert roles["edge"] == "#E1E1E1"

    def test_a_single_colour_mark_proposes_no_edge_or_accent(self, tmp_path):
        """Spec: return what was found, do not invent colours."""
        path = _logo(tmp_path, [((17, 34, 51, 255), 600)])
        roles, found = palette.derive(path)
        assert set(roles) == {"base", "text"}
        assert len(found) == 1

    def test_the_darkened_edge_accent_fallback_is_actually_darker(self, tmp_path):
        """Finding 3: with only one candidate reaching `edge`, `accent` falls
        back to a darkened copy of it (the `else` branch of the accent
        assignment). `test_every_role_is_a_valid_hex_colour` only checks that
        the value parses as a colour, which would not catch the multiplier
        being flipped into a LIGHTENING one - assert the actual relationship
        the fallback promises: `accent` is strictly darker than `edge`."""
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, found = palette.derive(path)
        others = [s for s in found if s.hex != roles["base"]]
        assert len(others) == 1  # only one candidate: the fallback branch is exercised
        assert palette._luminance(roles["accent"]) < palette._luminance(roles["edge"])

    def test_the_swatches_are_returned_with_the_roles(self, tmp_path):
        """The editor renders them as clickable chips, so the proposal is a
        starting point rather than an automatic overwrite."""
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, found = palette.derive(path)
        assert roles["base"] in {s.hex for s in found}
        assert len(found) >= 2

    def test_every_role_is_a_valid_hex_colour(self, tmp_path):
        from PIL import ImageColor
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, _ = palette.derive(path)
        for value in roles.values():
            ImageColor.getrgb(value)


class TestNoHeavyImports:
    def test_module_imports_no_web_framework_or_google(self):
        """CLAUDE.md's rule for every pure module, checked over the AST's
        import statements rather than the source text so the docstring stays
        free to NAME the constraint it upholds."""
        import ast
        import pathlib
        source = pathlib.Path(palette.__file__).read_text(encoding="utf-8")
        banned = {"fastapi", "starlette", "pydantic", "googleapiclient",
                  "google", "google_auth_oauthlib"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] not in banned, name
