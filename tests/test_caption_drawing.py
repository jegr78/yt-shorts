import copy

import pytest

from yt_shorts.overlay import build_caption, DEFAULT_CAPTION_GAP, _footer_top
from yt_shorts.profile import load


@pytest.fixture
def config():
    return load("erf/community-clips-back-catalogue").config


def with_window(config, video_y, video_height):
    """A deep copy of config with the video window geometry overridden -
    used to prove build_caption reacts to output.video_y/video_height
    instead of only ever seeing the one shipped profile."""
    variant = copy.deepcopy(config)
    variant["output"]["video_y"] = video_y
    variant["output"]["video_height"] = video_height
    return variant


def with_subtitle_y(config, y):
    variant = copy.deepcopy(config)
    variant.setdefault("subtitles", {})
    variant["subtitles"] = dict(variant.get("subtitles", {}))
    variant["subtitles"]["y"] = y
    return variant


def white_pixels(image):
    """Coordinates of fully opaque white pixels."""
    pixels = image.load()
    return [(x, y) for y in range(image.height) for x in range(image.width)
            if pixels[x, y] == (255, 255, 255, 255)]


STRESS_TEXTS = ["A" * 80, " ".join(["word"] * 40), " ".join(["word"] * 500)]


class TestBuildCaption:
    def test_dimensions_and_mode(self, config):
        image = build_caption("SHADOW REALM", config)
        assert image.size == (1080, 1920)
        assert image.mode == "RGBA"

    def test_text_is_drawn(self, config):
        assert len(white_pixels(build_caption("SHADOW REALM", config))) > 1000

    def test_nothing_reaches_into_the_video_window(self, config):
        output = config["output"]
        top, bottom = output["video_y"], output["video_y"] + output["video_height"]
        for text in ["SHADOW REALM", "A" * 80, " ".join(["word"] * 40)]:
            ys = [y for _, y in white_pixels(build_caption(text, config))]
            assert all(y >= bottom or y < top for y in ys), f"caption reached the window: {text!r}"

    def test_nothing_leaves_the_side_margins(self, config):
        for text in ["SHADOW REALM", "A" * 80, " ".join(["word"] * 40)]:
            xs = [x for x, _ in white_pixels(build_caption(text, config))]
            assert min(xs) >= 40 and max(xs) <= 1040, f"caption left the margins: {text!r}"

    def test_nothing_reaches_the_very_bottom(self, config):
        """The footer lives there."""
        for text in ["SHADOW REALM", " ".join(["word"] * 40)]:
            ys = [y for _, y in white_pixels(build_caption(text, config))]
            assert max(ys) < 1800, f"caption reached the footer area: {text!r}"

    def test_empty_text_draws_nothing(self, config):
        assert white_pixels(build_caption("", config)) == []

    def test_video_window_is_fully_transparent(self, config):
        image = build_caption("SHADOW REALM", config)
        pixels = image.load()
        assert pixels[540, 900][3] == 0


class TestTruncationIsReported:
    """A caption whose text cannot be made to fit even at the minimum size
    is drawn cut off, ending in an ellipsis, rather than raising or
    overflowing - `_truncate_to_line_count`'s existing behaviour, unchanged
    here. What was missing is that this happened silently: an operator
    reviewing the finished short would see a cut-off caption with no
    record anywhere of why. Reported the same NOTE: way as every other
    per-entry trouble in this tool, naming the caption whose text was cut."""

    def test_unfittable_text_is_reported_with_the_caption_named(self, config, capsys):
        text = "reallylongunbreakableword" * 3  # one word, far too wide at any size
        build_caption(text, config)
        err = capsys.readouterr().err
        assert "NOTE:" in err
        assert "truncated" in err
        assert text[:20] in err  # enough of the caption to identify it

    def test_ordinary_text_that_fits_is_not_reported(self, config, capsys):
        build_caption("SHADOW REALM", config)
        err = capsys.readouterr().err
        assert err == ""

    def test_a_long_caption_is_not_reproduced_in_full(self, config, capsys):
        """Finding C6: at the default max_words: 4 a group is always short,
        but an operator who raises max_words sharply can group far more
        words into one caption - and used to get the entire group echoed to
        stderr on every truncation. The NOTE must identify a long caption
        without reproducing all of it."""
        text = " ".join(["word"] * 500)  # far longer than any real caption at max_words: 4
        build_caption(text, config)
        err = capsys.readouterr().err
        assert "NOTE:" in err
        assert "truncated" in err
        assert text not in err


class TestDefaultPositionTracksTheWindow:
    """The shipped profile's window happens to end above the old hardcoded
    DEFAULT_CAPTION_Y = 1290, which is exactly why the bug this guards
    against went unnoticed. These tests vary the window geometry
    independently of everything else, which the old hardcoded default
    could never have passed."""

    def test_taller_window_moves_the_caption_down_and_clear_of_it(self, config):
        variant = with_window(config, video_y=600, video_height=900)  # window ends at 1500
        window_bottom = 1500
        for text in ["SHADOW REALM"] + STRESS_TEXTS:
            ys = [y for _, y in white_pixels(build_caption(text, variant))]
            assert ys, f"nothing drawn for {text!r}"
            assert min(ys) >= window_bottom, (
                f"{text!r}: caption pixel at y={min(ys)} is inside the taller window "
                f"(window ends at {window_bottom})"
            )
        # And it did move down relative to the shipped profile's own default.
        original_min_y = min(y for _, y in white_pixels(build_caption("SHADOW REALM", config)))
        moved_min_y = min(y for _, y in white_pixels(build_caption("SHADOW REALM", variant)))
        assert moved_min_y > original_min_y

    def test_shorter_window_moves_the_caption_up_and_clear_of_it(self, config):
        variant = with_window(config, video_y=600, video_height=300)  # window ends at 900
        window_bottom = 900
        for text in ["SHADOW REALM"] + STRESS_TEXTS:
            ys = [y for _, y in white_pixels(build_caption(text, variant))]
            assert ys, f"nothing drawn for {text!r}"
            assert min(ys) >= window_bottom, (
                f"{text!r}: caption pixel at y={min(ys)} is inside the shorter window "
                f"(window ends at {window_bottom})"
            )
        original_min_y = min(y for _, y in white_pixels(build_caption("SHADOW REALM", config)))
        moved_min_y = min(y for _, y in white_pixels(build_caption("SHADOW REALM", variant)))
        assert moved_min_y < original_min_y

    def test_default_top_equals_window_bottom_plus_the_gap(self, config):
        variant = with_window(config, video_y=200, video_height=444)  # window ends at 644
        output = variant["output"]
        window_bottom = output["video_y"] + output["video_height"]
        # Compare two renders that differ only in the gap-derived top: the
        # measured offset between the shipped profile's own caption top and
        # this variant's must equal the difference the formula predicts.
        expected_shipped_top = config["output"]["video_y"] + config["output"]["video_height"] + DEFAULT_CAPTION_GAP
        expected_variant_top = window_bottom + DEFAULT_CAPTION_GAP
        shipped_min_y = min(y for _, y in white_pixels(build_caption("SHADOW REALM", config)))
        variant_min_y = min(y for _, y in white_pixels(build_caption("SHADOW REALM", variant)))
        assert variant_min_y - shipped_min_y == expected_variant_top - expected_shipped_top


class TestExplicitPositionIsValidated:
    def test_position_inside_the_window_raises(self, config):
        # Window is 600-1208; 1000 lands inside it.
        variant = with_subtitle_y(config, 1000)
        with pytest.raises(ValueError, match="1000"):
            build_caption("SHADOW REALM", variant)

    def test_position_at_the_windows_bottom_edge_is_accepted(self, config):
        # video_y + video_height == 1208, the boundary itself must be legal.
        variant = with_subtitle_y(config, 1208)
        build_caption("SHADOW REALM", variant)  # must not raise

    def test_position_that_would_reach_the_footer_raises(self, config):
        output = config["output"]
        footer_top = _footer_top(output["height"])
        # Comfortably below the valid band, so the two-line block reaches
        # the footer no matter what caption size it uses.
        variant = with_subtitle_y(config, footer_top - 10)
        with pytest.raises(ValueError, match=str(footer_top - 10)):
            build_caption("SHADOW REALM", variant)

    def test_position_in_the_valid_band_is_honoured_exactly(self, config):
        output = config["output"]
        window_bottom = output["video_y"] + output["video_height"]
        low = with_subtitle_y(config, window_bottom + 20)
        high = with_subtitle_y(config, window_bottom + 120)
        low_min_y = min(y for _, y in white_pixels(build_caption("SHADOW REALM", low)))
        high_min_y = min(y for _, y in white_pixels(build_caption("SHADOW REALM", high)))
        # Same font/text, different requested top: the drawn offset must
        # shift by exactly the difference between the two requested values.
        assert high_min_y - low_min_y == 100


class TestGeometryStressCombinations:
    """For several window geometries, the existing overflow stress texts
    must never land a caption pixel inside the window or at/below the
    footer limit."""

    WINDOW_GEOMETRIES = [
        (600, 608),  # the shipped profile, unchanged
        (600, 900),  # taller window
        (600, 300),  # shorter window
        (150, 500),  # window starts higher up too
    ]

    @pytest.mark.parametrize("video_y,video_height", WINDOW_GEOMETRIES)
    def test_stress_texts_stay_clear_of_window_and_footer(self, config, video_y, video_height):
        variant = with_window(config, video_y, video_height)
        window_bottom = video_y + video_height
        footer_top = _footer_top(variant["output"]["height"])
        for text in STRESS_TEXTS:
            ys = [y for _, y in white_pixels(build_caption(text, variant))]
            assert ys, f"nothing drawn for {text!r}"
            assert min(ys) >= window_bottom, (
                f"{text!r} at window {video_y}-{window_bottom}: "
                f"pixel at y={min(ys)} is inside the window"
            )
            assert max(ys) <= footer_top, (
                f"{text!r} at window {video_y}-{window_bottom}: "
                f"pixel at y={max(ys)} reaches the footer (starts at {footer_top})"
            )


class TestDefaultPositionIsValidated:
    """Finding 1: subtitles.y unset is the path every profile without an
    explicit setting takes, and it used to skip validation entirely. A tall
    enough video window pushes the default (window_bottom + gap) straight
    through the footer with no error - these tests demonstrate exactly
    that geometry now raises, and that ordinary geometries still don't."""

    @pytest.mark.parametrize("video_height", [1050, 1200])
    def test_tall_window_collides_with_footer_and_raises(self, config, video_height):
        # Reviewer's exact case: video_y=600, video_height=1050 -> window_bottom=1650,
        # footer_top=1796, old code silently drew at 1755-1809. Must now raise.
        variant = with_window(config, video_y=600, video_height=video_height)
        with pytest.raises(ValueError) as excinfo:
            build_caption("SHADOW REALM", variant)
        message = str(excinfo.value)
        # The operator didn't set subtitles.y, so the message must not blame
        # subtitles.y - it must point at the geometry that actually caused it.
        assert "output.video_height" in message
        assert "subtitles.size" in message

    @pytest.mark.parametrize("video_y,video_height", [
        (600, 608),  # the shipped profile
        (600, 900),
        (600, 300),
        (150, 500),
    ])
    def test_ordinary_window_keeps_the_default_position_and_does_not_raise(
        self, config, video_y, video_height
    ):
        variant = with_window(config, video_y, video_height)
        build_caption("SHADOW REALM", variant)  # must not raise


class TestValidationUsesTheFittedSizeNotTheNominalOne:
    """Finding 2: the explicit-y check used to compute the caption's height
    from the configured (nominal) size, not the size _fitting_size actually
    settles on after shrinking - so a safe configuration could be rejected."""

    def test_shrunk_text_that_fits_is_accepted_even_though_nominal_would_not(self, config):
        # subtitles.size=108, subtitles.y=1675, 500 words. Nominal 2-line
        # block at size 108 (height 240) would end at 1915, past footer_top
        # (1796) - the old code rejected this. The text actually shrinks to
        # size 52 (real height 116), ending at 1791, which is safe.
        variant = copy.deepcopy(config)
        variant["subtitles"] = {"size": 108, "y": 1675}
        text = " ".join(["word"] * 500)
        footer_top = _footer_top(variant["output"]["height"])

        image = build_caption(text, variant)  # must not raise

        ys = [y for _, y in white_pixels(image)]
        assert ys, "expected the caption to actually be drawn"
        assert max(ys) <= footer_top


class TestSubtitlesSizeKnob:
    """Mutating DEFAULT_CAPTION_SIZE leaves every existing caption-drawing
    test green, because they all assert the collision contract and relative
    offsets rather than literal sizes - correct, since that's what makes
    them survive a deliberate re-tuning of the default. But that means
    nothing here actually proves the documented ``subtitles.size`` knob
    does anything at all. This measures the rendered glyphs, not an
    internal variable: a bigger requested size must produce a visibly
    bigger drawn caption, both taller and wider, for the same text."""

    def test_a_larger_size_draws_a_visibly_larger_caption(self, config):
        small = copy.deepcopy(config)
        small["subtitles"] = {"size": 40}
        large = copy.deepcopy(config)
        large["subtitles"] = {"size": 90}

        small_pixels = white_pixels(build_caption("SHADOW REALM", small))
        large_pixels = white_pixels(build_caption("SHADOW REALM", large))
        assert small_pixels and large_pixels, "expected both sizes to actually draw something"

        small_height = max(y for _, y in small_pixels) - min(y for _, y in small_pixels)
        large_height = max(y for _, y in large_pixels) - min(y for _, y in large_pixels)
        small_width = max(x for x, _ in small_pixels) - min(x for x, _ in small_pixels)
        large_width = max(x for x, _ in large_pixels) - min(x for x, _ in large_pixels)

        assert large_height > small_height, "subtitles.size=90 must draw taller text than size=40"
        assert large_width > small_width, "subtitles.size=90 must draw wider text than size=40"


class TestSafetyHoldsWheneverACaptionIsProduced:
    """Broad sweep: whatever the window geometry, whenever build_caption
    manages to produce a caption at all (i.e. does not raise), no pixel of
    it may land inside the video window or at/below the footer limit. A
    geometry that leaves no room is allowed to raise instead - that is the
    whole point of the guard - but it must never silently draw a
    violation.

    This used to be one parametrized test per geometry, each of which could
    pass vacuously (every text in that one geometry's sweep raising or
    drawing nothing) without proving the safety property held for anything
    at all. A single global `produced_any` flag across the whole sweep
    (finding C4) fixed that on paper but not in practice: of the eight
    geometries below, (600, 1200) and (0, 1700) legitimately produce nothing
    for any of the three stress texts, and (600, 1050) produces a caption
    for only one of the three - so the flag was satisfied by a small
    minority of the sweep and would stay satisfied even if a regression
    silenced the other five geometries entirely.

    Each geometry's expectation is now explicit and per-text: EXPECT_DRAWN
    (must produce a caption, safety must hold for it) or EXPECT_RAISES (must
    refuse - there is legitimately no room). A geometry silently producing
    nothing, or silently refusing to draw something it always used to,
    fails immediately instead of being absorbed into one shared flag.
    Ground truth for every entry below was captured by actually running
    build_caption against the shipped profile for each (geometry, text)
    pair."""

    EXPECT_DRAWN = "drawn"
    EXPECT_RAISES = "raises"

    # (video_y, video_height): expected outcome for STRESS_TEXTS[0..2]
    # (80-char single word, 40 words, 500 words), in that order.
    GEOMETRY_EXPECTATIONS = [
        ((600, 608), (EXPECT_DRAWN, EXPECT_DRAWN, EXPECT_DRAWN)),   # the shipped profile
        ((600, 900), (EXPECT_DRAWN, EXPECT_DRAWN, EXPECT_DRAWN)),   # taller window
        ((600, 300), (EXPECT_DRAWN, EXPECT_DRAWN, EXPECT_DRAWN)),   # shorter window
        ((150, 500), (EXPECT_DRAWN, EXPECT_DRAWN, EXPECT_DRAWN)),   # window starts higher up too
        # window_bottom=1650, footer_top=1796: only 126px of room. The
        # single 80-char word still fits on one line; both multi-word texts
        # wrap to a second line and no longer do.
        ((600, 1050), (EXPECT_DRAWN, EXPECT_RAISES, EXPECT_RAISES)),
        # window_bottom=1800, past footer_top=1796 already - no text fits.
        ((600, 1200), (EXPECT_RAISES, EXPECT_RAISES, EXPECT_RAISES)),
        ((100, 1400), (EXPECT_DRAWN, EXPECT_DRAWN, EXPECT_DRAWN)),
        # window_bottom=1700, footer_top=1796: only 96px of room, less than
        # even a single line at the minimum allowed size - nothing fits.
        ((0, 1700), (EXPECT_RAISES, EXPECT_RAISES, EXPECT_RAISES)),
    ]

    @pytest.mark.parametrize("geometry,expectations", GEOMETRY_EXPECTATIONS)
    def test_each_geometry_matches_its_explicit_expectation(self, config, geometry, expectations):
        video_y, video_height = geometry
        variant = with_window(config, video_y, video_height)
        window_bottom = video_y + video_height
        footer_top = _footer_top(variant["output"]["height"])
        for text, expected in zip(STRESS_TEXTS, expectations, strict=True):
            if expected == self.EXPECT_RAISES:
                with pytest.raises(ValueError):
                    build_caption(text, variant)
                continue
            image = build_caption(text, variant)  # must not raise
            ys = [y for _, y in white_pixels(image)]
            assert ys, (
                f"{text!r} at window {video_y}-{window_bottom}: expected a caption "
                f"to be drawn, but nothing was"
            )
            assert min(ys) >= window_bottom, (
                f"{text!r} at window {video_y}-{window_bottom}: "
                f"pixel at y={min(ys)} is inside the window"
            )
            assert max(ys) <= footer_top, (
                f"{text!r} at window {video_y}-{window_bottom}: "
                f"pixel at y={max(ys)} reaches the footer (starts at {footer_top})"
            )
