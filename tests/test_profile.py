"""Tests for yt_shorts.profile: resolving 'channel/event' + loading the channel profile.

Uses partly the ERF channel fixture at tests/fixtures/channels/erf (valid
identifier, channel WITH layout.py - see conftest.py, which points
profile.CHANNELS_DIR there for the whole suite), partly channel folders
built in tmp_path itself (unknown channel, unknown event, missing profile
file, incomplete profile, channel WITHOUT layout.py) - the latter via
monkeypatching profile.CHANNELS_DIR directly, so no reference to the ERF
fixture is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yt_shorts import profile, providers

# A sentinel for "do not write this key at all", distinct from writing null.
_ABSENT = object()


class TestValidIdentifierRealErfChannel:
    def test_erf_channel_loads_successfully(self):
        p = profile.load("erf/community-clips-back-catalogue")
        assert p.channel_name == "erf"
        assert p.event_name == "community-clips-back-catalogue"
        assert p.channel["handle"] == "@ERFofficial"
        assert p.channel["footer"] == "ERF | @ERFofficial"

    def test_font_paths_are_absolute_and_exist(self):
        p = profile.load("erf/community-clips-back-catalogue")
        for path in p.config["fonts"].values():
            assert Path(path).is_absolute()
            assert Path(path).exists()

    def test_channel_with_layout_py_gets_decorate_function(self):
        """ERF has a layout.py with the parallelogram motif: config must
        contain a callable function under 'decorate'."""
        p = profile.load("erf/community-clips-back-catalogue")
        assert callable(p.config.get("decorate"))


def _build_channel_dir(basis: Path, name: str, *, with_layout: bool = False,
                       with_channel_json: bool = True, with_brand_json: bool = True,
                       events: list[str] | None = None) -> Path:
    channel_dir = basis / name
    channel_dir.mkdir(parents=True)
    if with_channel_json:
        (channel_dir / "channel.json").write_text(json.dumps({
            "id": "UCtest", "handle": "@test", "display_name": "Test Channel",
            "language": "en", "footer": "TEST | @test", "channel_url": "https://example.invalid/test",
        }), encoding="utf-8")
    if with_brand_json:
        fonts_dir = channel_dir / "fonts"
        fonts_dir.mkdir(exist_ok=True)
        (fonts_dir / "Dummy.ttf").write_bytes(b"")
        (channel_dir / "brand.json").write_text(json.dumps({
            "colors": {"text": "#FFFFFF", "base": "#000000", "accent": "#333333", "edge": "#00FF00"},
            "fonts": {"hook": "fonts/Dummy.ttf", "small": "fonts/Dummy.ttf"},
            "output": {"width": 1080, "height": 1920, "video_width": 1080,
                       "video_height": 608, "video_y": 600},
        }), encoding="utf-8")
    if with_layout:
        (channel_dir / "layout.py").write_text(
            "def decorate(draw, config, window_top, window_bottom):\n"
            "    pass\n",
            encoding="utf-8",
        )
    events_dir = channel_dir / "events"
    events_dir.mkdir()
    for event in events or []:
        (events_dir / event).mkdir()
    return channel_dir


class TestUnknownChannel:
    def test_raises_understandable_profile_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "present", events=["some-event"])

        with pytest.raises(profile.ProfileError, match="Unknown channel"):
            profile.load("not-present/some-event")

    def test_error_message_is_not_a_raw_traceback(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        try:
            profile.load("not-present/some-event")
            pytest.fail("should have raised ProfileError")
        except profile.ProfileError as error:
            assert "Traceback" not in str(error)
            assert "not-present" in str(error)


class TestUnknownEvent:
    def test_raises_understandable_profile_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "mychannel", events=["real-event"])

        with pytest.raises(profile.ProfileError, match="Unknown event"):
            profile.load("mychannel/made-up-event")


class TestUnsafeIdentifierSegments:
    """The identifier -> path surface (which also execs a workspace layout.py)
    must reject a traversal/unsafe segment BEFORE any filesystem touch, not
    trust its caller."""

    @pytest.mark.parametrize("identifier", [
        "../etc/evt",
        "chan/..",
        "../../secrets/evt",
        ".hidden/evt",
        "chan/.hidden",
        "ch an/evt",
    ])
    def test_unsafe_segment_raises_profile_error(self, monkeypatch, tmp_path, identifier):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        with pytest.raises(profile.ProfileError, match="not a valid"):
            profile.load(identifier)


class TestMissingProfileFile:
    def test_missing_channel_json_raises_understandable_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "broken", with_channel_json=False, events=["event"])

        with pytest.raises(profile.ProfileError, match="channel.json"):
            profile.load("broken/event")

    def test_missing_brand_json_raises_understandable_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "broken", with_brand_json=False, events=["event"])

        with pytest.raises(profile.ProfileError, match="brand.json"):
            profile.load("broken/event")


class TestIncompleteProfile:
    """Finding A: a typo or a gap in a profile must be reported with the
    file it belongs to and what is missing - collected together, not one
    at a time across repeated runs."""

    def test_missing_color_key_raises_understandable_error_not_a_keyerror(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        del brand["colors"]["accent"]
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match=r"colors\.accent") as excinfo:
            profile.load("broken/event")
        assert "Traceback" not in str(excinfo.value)
        assert "brand.json" in str(excinfo.value)

    def test_missing_channel_field_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        channel = json.loads((channel_dir / "channel.json").read_text(encoding="utf-8"))
        del channel["display_name"]
        (channel_dir / "channel.json").write_text(json.dumps(channel), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="display_name"):
            profile.load("broken/event")

    def test_missing_output_dimension_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        del brand["output"]["video_height"]
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match=r"output\.video_height"):
            profile.load("broken/event")

    def test_missing_font_file_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["fonts"] = {"hook": "fonts/DoesNotExist.ttf", "small": "fonts/DoesNotExist.ttf"}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="DoesNotExist.ttf"):
            profile.load("broken/event")

    def test_wrong_type_output_dimension_is_reported_as_profile_error(self, monkeypatch, tmp_path):
        """Finding C1: a type typo in an output dimension (e.g. "video_y":
        "600" instead of 600) used to load cleanly - REQUIRED_OUTPUT_KEYS
        was only checked for presence - and then raise a raw TypeError from
        inside the geometry check, but only when subtitles were enabled.
        With subtitles enabled, this must now be a ProfileError, not a
        traceback."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["output"]["video_y"] = "600"
        brand["subtitles"] = {"enabled": True}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match=r"output\.video_y") as excinfo:
            profile.load("broken/event")
        assert "Traceback" not in str(excinfo.value)

    def test_wrong_type_output_dimension_is_reported_with_subtitles_off_too(
        self, monkeypatch, tmp_path
    ):
        """The other half of finding C1: the same type typo was previously
        completely silent whenever subtitles were off (or absent), because
        nothing else touched 'output' arithmetically. It must be reported
        just the same regardless of subtitles - a wrong type is a defect in
        the profile itself, not only when subtitles happen to use it."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["output"]["height"] = "1920"
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match=r"output\.height") as excinfo:
            profile.load("broken/event")
        assert "Traceback" not in str(excinfo.value)

    def test_wrong_type_output_dimension_is_collected_with_other_defects(
        self, monkeypatch, tmp_path
    ):
        """Finding C1 must honour the same collect-all principle as finding
        A: a type typo and an unrelated missing color must both show up in
        one ProfileError, not short-circuit each other."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["output"]["video_y"] = "600"
        del brand["colors"]["accent"]
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "output.video_y" in message
        assert "colors.accent" in message

    def test_several_gaps_at_once_are_all_collected_in_one_error(self, monkeypatch, tmp_path):
        """The point of finding A: whoever is typing up a profile should not
        need one run per typo. A missing channel field, a missing color, and
        a missing output dimension - introduced together - must all show up
        in a single ProfileError."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        channel = json.loads((channel_dir / "channel.json").read_text(encoding="utf-8"))
        del channel["footer"]
        (channel_dir / "channel.json").write_text(json.dumps(channel), encoding="utf-8")
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        del brand["colors"]["edge"]
        del brand["output"]["video_width"]
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "footer" in message
        assert "colors.edge" in message
        assert "output.video_width" in message


class TestChannelWithoutLayoutPy:
    """A channel without layout.py must still work and produce plain bars:
    config then carries no 'decorate'."""

    def test_loads_successfully_without_decorate_in_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "plain", with_layout=False, events=["event"])

        p = profile.load("plain/event")

        assert "decorate" not in p.config

    def test_build_overlay_produces_plain_bars_without_error(self, monkeypatch, tmp_path):
        from yt_shorts.overlay import build_overlay

        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "plain", with_layout=False, events=["event"])
        p = profile.load("plain/event")

        image = build_overlay("", "", p.config)
        assert image.size == (1080, 1920)


class TestChannelWithLayoutPy:
    def test_config_carries_callable_decorate_function(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "decorated", with_layout=True, events=["event"])

        p = profile.load("decorated/event")

        assert callable(p.config["decorate"])

    def test_decorate_is_actually_called_by_build_overlay(self, monkeypatch, tmp_path):
        from yt_shorts.overlay import build_overlay

        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "decorated", with_layout=False, events=["event"])
        called = []
        (channel_dir / "layout.py").write_text(
            "def decorate(draw, config, window_top, window_bottom):\n"
            "    draw.rectangle([0, 0, 10, 10], fill=(255, 0, 0, 255))\n",
            encoding="utf-8",
        )
        p = profile.load("decorated/event")

        image = build_overlay("", "", p.config)
        assert image.getpixel((5, 5)) == (255, 0, 0, 255)
        del called  # only for readability of the test name, unused

    def test_layout_without_decorate_function_raises_understandable_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", with_layout=False, events=["event"])
        (channel_dir / "layout.py").write_text("# no matching function here\n", encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="decorate"):
            profile.load("broken/event")


class TestSubtitlesValidation:
    """Mirrors _validate_logo's style: 'subtitles' is optional, but if
    present its values must be sane, and every defect is collected
    together rather than reported one run at a time."""

    def test_several_bad_values_at_once_are_all_named(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["subtitles"] = {"max_words": 0, "max_seconds": -1, "enabled": "yes"}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "subtitles.max_words" in message
        assert "subtitles.max_seconds" in message
        assert "subtitles.enabled" in message

    def test_explicit_null_is_rejected(self, monkeypatch, tmp_path):
        """Finding F4: config.get("subtitles", {}) - used by cmd_render and
        overlay.build_caption - only falls back to {} when the key is
        MISSING. An explicit "subtitles": null still returns None there,
        and every downstream .get() on that None raises AttributeError at
        render time, for every single clip. An explicit null must be
        rejected here, the same as any other malformed 'subtitles' value,
        not treated the same as an absent key."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["subtitles"] = None
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="subtitles"):
            profile.load("broken/event")

    def test_absent_key_is_still_accepted(self, monkeypatch, tmp_path):
        """Contrast case for test_explicit_null_is_rejected: a brand.json
        that never mentions 'subtitles' at all must still load fine -
        absent is off, the documented default."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "plain", events=["event"])

        p = profile.load("plain/event")

        assert "subtitles" not in p.config

    def test_size_and_y_must_be_positive_integers(self, monkeypatch, tmp_path):
        """Finding F8: size and y were validated (see the loop over
        ("size", "y") below) but had no covering test."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["subtitles"] = {"size": -5, "y": "not-an-int"}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "subtitles.size" in message
        assert "subtitles.y" in message

    def test_subtitles_not_an_object_is_rejected(self, monkeypatch, tmp_path):
        """Finding F8: the 'subtitles must be an object' branch (a string,
        a list, ... instead of a dict) had no covering test."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["subtitles"] = "on"
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="'subtitles' must be an object"):
            profile.load("broken/event")


class TestSubtitlesGeometryValidation:
    """Finding A1: overlay.build_caption's video-window/footer collision
    check used to only run per clip, inside the subtitle degrade path
    (cmd_render's provider closure) - a bad subtitles.size/y loaded
    cleanly and only surfaced after every single clip had already paid for
    a full download and transcription, as a per-clip NOTE on stderr.

    The geometry is fully known at profile.load time (output dimensions,
    footer size, subtitles.size/y), so it must now be checked there too,
    via the exact same rule overlay.validate_caption_box enforces for
    build_caption - not a second, independently written copy of it (see
    overlay.caption_geometry's docstring for why the worst case it uses is
    always at least as strict as what any real clip could trigger).

    The shipped test channel dir's window spans y=600-1208 (video_y=600 +
    video_height=608); the footer starts at
    height - MARGIN - FOOTER_FONT_SIZE = 1920 - 70 - 54 = 1796.
    """

    def test_caption_overlapping_the_video_window_is_rejected_at_load_time(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        # Window is 600-1208; 1000 lands inside it - mirrors
        # test_caption_drawing.TestExplicitPositionIsValidated's own case,
        # so both checks are proven against the identical value.
        brand["subtitles"] = {"enabled": True, "y": 1000}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="1000") as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "video window" in message
        assert "brand.json" in message

    def test_caption_overlapping_the_footer_is_rejected_at_load_time(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        # footer_top=1796; comfortably below the valid band, so even a
        # single-line caption at the smallest size would still reach the
        # footer - mirrors test_caption_drawing's own "footer_top - 10" case.
        brand["subtitles"] = {"enabled": True, "y": 1786}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="1786") as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "footer" in message

    def test_valid_geometry_still_loads(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel_dir(tmp_path, "fine", events=["event"])
        channel_dir = tmp_path / "fine"
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["subtitles"] = {"enabled": True, "y": 1250}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        p = profile.load("fine/event")

        assert p.config["subtitles"]["y"] == 1250

    def test_geometry_defect_is_collected_with_other_defects_in_one_error(
        self, monkeypatch, tmp_path
    ):
        """The point of finding A1, same as finding A: the geometry defect
        must not short-circuit past an unrelated typo - both must show up
        in the same ProfileError."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        del brand["colors"]["accent"]
        brand["subtitles"] = {"enabled": True, "y": 1000}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "colors.accent" in message
        assert "1000" in message

    def test_geometry_defect_is_collected_with_an_unrelated_subtitle_defect(
        self, monkeypatch, tmp_path
    ):
        """Finding C2: the geometry check used to be gated on `not
        problems`, so an unrelated subtitle defect (max_words: 0, say) made
        it short-circuit and hide a colliding y - two runs for two typos.
        The gate must only depend on subtitles.size/y themselves being
        valid, not on every subtitle field, so both defects are reported
        together."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["subtitles"] = {"enabled": True, "max_words": 0, "y": 1000}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "subtitles.max_words" in message
        assert "1000" in message

    def test_disabled_subtitles_skip_the_geometry_check(self, monkeypatch, tmp_path):
        """A profile with subtitles disabled must behave exactly as it does
        today, even if its (unused) geometry would collide - it will never
        actually be drawn."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["subtitles"] = {"enabled": False, "y": 1000}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        p = profile.load("broken/event")  # must not raise

        assert p.config["subtitles"]["y"] == 1000

    def test_subtitles_present_but_not_explicitly_enabled_skips_the_check(
        self, monkeypatch, tmp_path
    ):
        """Mirrors cmd_render's own gate (config.get("subtitles",
        {}).get("enabled")): a 'subtitles' block without 'enabled: true'
        never draws a caption, so a colliding y in it must not fail the
        load either."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "broken", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["subtitles"] = {"y": 1000}
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        p = profile.load("broken/event")  # must not raise

        assert p.config["subtitles"]["y"] == 1000


class TestGlossary:
    """config["glossary"] is always a yt_shorts.glossary.Glossary, merged from
    FIVE additive layers (the now-empty built-in default, the track pack an
    event selects, workspace, channel, event) - most specific wins per entry,
    and a falsy entry disables one inherited from a less specific layer. This
    replaced the original wholesale rule (an event's glossary.json used to
    replace the channel's outright), which could not survive an additive
    corner-name list: a channel can both need the corners its event's track
    pack ships and have a glossary.json of its own. A malformed layer is
    collected into the same ProfileError as every other profile defect, not
    raised on its own."""

    def test_no_files_yields_the_empty_glossary(self, monkeypatch, tmp_path):
        """The built-in default is EMPTY now (see glossary.DEFAULT_LAYER and
        tracks.py) - with no operator layer and no event track, there is
        nothing to merge."""
        from yt_shorts.glossary import EMPTY
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "plain", events=["event"])

        p = profile.load("plain/event")

        assert p.config["glossary"] == EMPTY

    def test_channel_glossary_ADDS_to_the_pack(self, monkeypatch, tmp_path):
        """The regression this feature exists to prevent: a channel's own
        glossary.json must not replace what the event's track pack
        contributes."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "terms": ["Rei Racing"],
            "replacements": {"very very": "Rei Racing"},
        }), encoding="utf-8")
        (channel_dir / "events" / "event" / "glossary.json").write_text(json.dumps({
            "track": "nurburgring-nordschleife",
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Rei Racing" in p.config["glossary"].terms
        assert "Karussell" in p.config["glossary"].terms
        assert p.config["glossary"].replacements["very very"] == "Rei Racing"
        assert p.config["glossary"].replacements["kessichen"] == "Kesselchen"

    def test_workspace_layer_applies_to_every_channel(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (tmp_path / "glossary.json").write_text(json.dumps({
            "terms": ["Workspace Term"],
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Workspace Term" in p.config["glossary"].terms

    def test_event_layer_adds_to_the_channel_layer(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "terms": ["Channel Term"],
        }), encoding="utf-8")
        (channel_dir / "events" / "event" / "glossary.json").write_text(json.dumps({
            "terms": ["Event Term"],
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Channel Term" in p.config["glossary"].terms
        assert "Event Term" in p.config["glossary"].terms

    def test_event_layer_can_disable_an_inherited_replacement(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "events" / "event" / "glossary.json").write_text(json.dumps({
            "track": "nurburgring-nordschleife",
            "replacements": {"carousel": None},
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "carousel" not in p.config["glossary"].replacements
        # A sibling pack entry is untouched - one disable is not a purge.
        assert p.config["glossary"].replacements["kessichen"] == "Kesselchen"

    def test_glossary_not_an_object_is_collected_with_other_defects(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "broken", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps(["not", "an", "object"]),
                                                    encoding="utf-8")
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        del brand["colors"]["accent"]
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "glossary must be an object" in message
        assert "colors.accent" in message

    def test_terms_not_a_list_of_strings_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "broken", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "terms": ["fine", 42],
        }), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="each term must be a non-empty string"):
            profile.load("broken/event")

    def test_replacement_value_of_the_wrong_type_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "broken", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "replacements": {"very very": 42},
        }), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="must be a string, or null to disable"):
            profile.load("broken/event")

    def test_invalid_json_glossary_is_collected_not_raised_on_its_own(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "broken", events=["event"])
        (channel_dir / "glossary.json").write_text("{not json", encoding="utf-8")
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        del brand["colors"]["accent"]
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "glossary.json" in message
        assert "not valid JSON" in message
        assert "colors.accent" in message

    def test_one_broken_layer_does_not_take_out_the_others(self, monkeypatch, tmp_path):
        """A defect is reported, and the LOADABLE layers are still merged -
        the same degradation _load_lexicon already makes."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text("{not json", encoding="utf-8")
        (channel_dir / "events" / "event" / "glossary.json").write_text(json.dumps({
            "track": "nurburgring-nordschleife",
        }), encoding="utf-8")

        glossary, problems = profile._load_glossary(
            channel_dir / "events" / "event", channel_dir, tmp_path)

        assert len(problems) == 1
        assert "not valid JSON" in problems[0]
        assert "Karussell" in glossary.terms

    def test_missing_terms_or_replacements_default_to_empty(self, monkeypatch, tmp_path):
        """Neither key is mandatory - a glossary.json naming only one of
        them must still load, with the other contributing nothing."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "terms": ["Rei Racing"],
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Rei Racing" in p.config["glossary"].terms

    def test_an_event_track_inserts_the_pack(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "events" / "event" / "glossary.json").write_text(
            json.dumps({"track": "spa-francorchamps"}), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Eau Rouge" in p.config["glossary"].terms
        assert "Raidillon" in p.config["glossary"].terms

    def test_no_track_means_no_pack(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "chan", events=["event"])

        p = profile.load("chan/event")

        assert p.config["glossary"].terms == []
        assert p.config["glossary"].replacements == {}

    def test_the_operator_layers_win_over_the_pack(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(
            json.dumps({"terms": {"Eau Rouge": False}}), encoding="utf-8")
        (channel_dir / "events" / "event" / "glossary.json").write_text(
            json.dumps({"track": "spa-francorchamps"}), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Eau Rouge" not in p.config["glossary"].terms
        assert "Pouhon" in p.config["glossary"].terms

    def test_operator_terms_precede_the_packs(self, monkeypatch, tmp_path):
        """The truncation-survival property: faster-whisper drops the LAST
        hotwords, so the operator's own must come first."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(
            json.dumps({"terms": ["Rei Racing"]}), encoding="utf-8")
        (channel_dir / "events" / "event" / "glossary.json").write_text(
            json.dumps({"track": "monza"}), encoding="utf-8")

        p = profile.load("chan/event")

        assert p.config["glossary"].terms[0] == "Rei Racing"

    def test_an_unknown_track_is_a_reported_defect(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "events" / "event" / "glossary.json").write_text(
            json.dumps({"track": "nope"}), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="unknown track 'nope'"):
            profile.load("chan/event")

    def test_a_track_at_channel_scope_is_a_reported_defect(self, monkeypatch, tmp_path):
        """Writing it at the wrong level must be an error, not a silent
        no-op - otherwise an operator finds out three hours into a transcript
        that their corner names never applied."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(
            json.dumps({"track": "monza"}), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as caught:
            profile.load("chan/event")
        assert "only an event selects a track" in str(caught.value)
        # The offending file, not just the word "channel": the operator has to
        # open one specific glossary.json to remove the key.
        assert str(channel_dir / "glossary.json") in str(caught.value)

    def test_a_track_at_workspace_scope_is_a_reported_defect(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (tmp_path / "glossary.json").write_text(
            json.dumps({"track": "monza"}), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as caught:
            profile.load("chan/event")
        assert "only an event selects a track" in str(caught.value)
        assert str(tmp_path / "glossary.json") in str(caught.value)


class TestIdentifierFormat:
    def test_identifier_without_slash_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        with pytest.raises(profile.ProfileError, match="channel/event"):
            profile.load("only-one-part")


class TestChannelsComeFromTheWorkspace:
    def test_the_channels_directory_follows_workspace_injection(self, monkeypatch, tmp_path):
        """F1 (Important): the new tests cannot fail. A reviewer reverted
        profile.py to hardcoded CHANNELS_DIR and removed the workspace
        import, but all tests still passed because in the default case,
        workspace.resolve().channels_dir *is* ROOT / "channels". This test
        fixes that by injecting a workspace that is demonstrably NOT the
        repository path, reloading the module with that in place, and asserting
        CHANNELS_DIR follows it. Because CHANNELS_DIR is resolved once at
        import, we use importlib.reload plus monkeypatch on workspace.resolve.
        """
        import importlib
        from yt_shorts import workspace, profile as profile_module

        # Create a workspace at an obviously different path
        different_workspace_root = tmp_path / "different-workspace"
        different_workspace_root.mkdir()
        (different_workspace_root / "channels").mkdir()
        different_channels_dir = different_workspace_root / "channels"

        # Assert it is NOT the repository channels dir
        repo_channels = profile_module.ROOT / "channels"
        assert different_channels_dir != repo_channels, \
            f"Test setup failed: {different_channels_dir} equals {repo_channels}"

        # Patch workspace.resolve to return the different workspace
        def fake_resolve(env=None, home=None, repo_channels=None):
            return workspace.Workspace(
                root=different_workspace_root,
                channels_dir=different_channels_dir,
                origin="test_injected"
            )

        monkeypatch.setattr(workspace, "resolve", fake_resolve)

        # Reload profile with the patched workspace.resolve in effect
        importlib.reload(profile_module)

        try:
            # Assert CHANNELS_DIR now points to the injected workspace
            assert profile_module.CHANNELS_DIR == different_channels_dir, \
                f"CHANNELS_DIR={profile_module.CHANNELS_DIR} does not follow injected workspace"
            assert profile_module.CHANNELS_DIR != repo_channels, \
                f"Test failed: CHANNELS_DIR={profile_module.CHANNELS_DIR} equals {repo_channels}"
        finally:
            # Restore the module to its original state (resolved against real workspace)
            # This is critical: a test that leaves profile reloaded against a fake
            # workspace would poison every test that runs after it.
            importlib.reload(profile_module)

    def test_import_does_not_print_to_stdout_or_stderr(self, capsys):
        """F2 (Minor): an import-time print would ship silently. This test
        verifies that importing yt_shorts.profile writes nothing to stdout
        or stderr. The startup line naming the resolved workspace belongs in
        bin/yt-shorts, not at module scope."""
        import importlib
        from yt_shorts import profile as profile_module

        # Reload the module to capture any output from its import
        importlib.reload(profile_module)

        # Capture and assert both stdout and stderr are empty
        captured = capsys.readouterr()
        assert captured.out == "", \
            f"import yt_shorts.profile wrote to stdout: {captured.out!r}"
        assert captured.err == "", \
            f"import yt_shorts.profile wrote to stderr: {captured.err!r}"

    def test_the_repository_fallback_still_finds_a_channel_placed_there(
        self, monkeypatch, tmp_path
    ):
        """I9: this used to assert on channels/erf/brand.json, which the
        repository shipped at the time. It no longer does: the ERF channel
        now lives in the operator's workspace (or, for the suite, in
        tests/fixtures/channels/ - see conftest.py), and the repository's
        own channels/ not existing at all is a legitimate 'no workspace
        configured yet' outcome of the repository-fallback resolution
        order (see workspace.py) rather than a channel to find. What this
        test can still prove without a shipped channel: the
        repository-fallback origin correctly puts CHANNELS_DIR at
        repo_channels, and profile.load finds whatever a channel folder
        placed there actually contains. Made hermetic the same way
        test_the_channels_directory_follows_workspace_injection above is
        (monkeypatch workspace.resolve, reload profile) instead of
        depending on ambient machine state.
        """
        import importlib

        from yt_shorts import profile as profile_module
        from yt_shorts import workspace

        fake_repo_channels = tmp_path / "repo_channels"
        channel_dir = fake_repo_channels / "chan"
        (channel_dir / "events" / "ev").mkdir(parents=True)
        (channel_dir / "channel.json").write_text(json.dumps({
            "id": "x", "handle": "@x", "display_name": "X", "language": "en",
            "footer": "X | @x", "channel_url": "https://example.invalid/x",
        }), encoding="utf-8")
        (channel_dir / "fonts").mkdir()
        (channel_dir / "fonts" / "chan.ttf").write_bytes(b"font")
        (channel_dir / "brand.json").write_text(json.dumps({
            "colors": {"text": "#FFFFFF", "base": "#101010", "accent": "#144E53", "edge": "#B8F5CA"},
            "fonts": {"hook": "fonts/chan.ttf", "small": "fonts/chan.ttf"},
            "output": {"width": 1080, "height": 1920, "video_width": 1080,
                       "video_height": 608, "video_y": 600},
        }), encoding="utf-8")

        def fake_resolve(env=None, home=None, repo_channels=None):
            return workspace.Workspace(
                root=fake_repo_channels.parent, channels_dir=fake_repo_channels,
                origin="repository")

        monkeypatch.setattr(workspace, "resolve", fake_resolve)
        importlib.reload(profile_module)

        try:
            assert profile_module.CHANNELS_DIR == fake_repo_channels
            p = profile_module.load("chan/ev")
            assert p.channel_name == "chan"
            assert p.event_name == "ev"
        finally:
            # Restore, same reasoning as the pattern this is based on: a
            # test that leaves profile reloaded against a fake workspace
            # would poison every test that runs after it.
            importlib.reload(profile_module)


class TestLexiconInProfile:
    def test_channel_lexicon_is_loaded(self):
        from yt_shorts.lexicon import Lexicon
        from yt_shorts.profile import load
        p = load("erf/community-clips-back-catalogue")
        assert isinstance(p.config["lexicon"], Lexicon)
        assert "crash" in p.config["lexicon"].markers


class TestValidateUpload:
    PATH = Path("brand.json")

    def test_absent_upload_is_fine(self):
        assert profile._validate_upload({}, self.PATH) == []

    def test_api_and_manual_are_accepted(self):
        assert profile._validate_upload({"upload": {"mode": "api"}}, self.PATH) == []
        assert profile._validate_upload({"upload": {"mode": "manual"}}, self.PATH) == []

    def test_upload_without_mode_is_fine(self):
        assert profile._validate_upload({"upload": {"tags": ["x"]}}, self.PATH) == []

    def test_unknown_mode_is_rejected_and_names_the_field(self):
        problems = profile._validate_upload({"upload": {"mode": "owner"}}, self.PATH)
        assert len(problems) == 1
        assert "upload.mode" in problems[0]
        assert "owner" in problems[0]

    def test_null_upload_is_rejected(self):
        problems = profile._validate_upload({"upload": None}, self.PATH)
        assert len(problems) == 1
        assert "upload" in problems[0]

    def test_accepts_metadata_fields(self):
        cfg = {
            "upload": {
                "mode": "api",
                "description": "d {title}",
                "tags": ["a", "b"],
                "category_id": "20",
                "made_for_kids": False,
            }
        }
        assert profile._validate_upload(cfg, self.PATH) == []

    def test_rejects_bad_description(self):
        problems = profile._validate_upload({"upload": {"description": 5}}, self.PATH)
        assert len(problems) == 1
        assert "upload.description" in problems[0]

    def test_rejects_bad_tags(self):
        problems = profile._validate_upload({"upload": {"tags": "not-a-list"}}, self.PATH)
        assert len(problems) == 1
        assert "upload.tags" in problems[0]

    def test_rejects_tags_with_non_string_element(self):
        problems = profile._validate_upload({"upload": {"tags": ["a", 1]}}, self.PATH)
        assert len(problems) == 1
        assert "upload.tags" in problems[0]

    def test_rejects_bad_category_id(self):
        problems = profile._validate_upload({"upload": {"category_id": 20.5}}, self.PATH)
        assert len(problems) == 1
        assert "upload.category_id" in problems[0]

    def test_accepts_int_category_id(self):
        assert profile._validate_upload({"upload": {"category_id": 20}}, self.PATH) == []

    def test_rejects_bad_made_for_kids(self):
        problems = profile._validate_upload({"upload": {"made_for_kids": "yes"}}, self.PATH)
        assert len(problems) == 1
        assert "upload.made_for_kids" in problems[0]

    def test_collects_multiple_metadata_problems_together(self):
        cfg = {"upload": {"mode": "owner", "tags": "nope", "made_for_kids": "no"}}
        problems = profile._validate_upload(cfg, self.PATH)
        assert len(problems) == 3

    def test_non_object_upload_is_rejected(self):
        problems = profile._validate_upload({"upload": "manual"}, self.PATH)
        assert len(problems) == 1


class TestDetectValidation:
    """The 'detect' section names which provider scores moments, and with what
    model. Both optional; an unknown provider is a REPORTED DEFECT rather than
    a silent fall back to the default, because a typo that quietly billed a
    different vendor than the operator asked for is exactly the silent
    degradation this project has already paid for elsewhere."""

    PATH = Path("brand.json")

    def _load_with_detect(self, tmp_path, monkeypatch, detect, **brand_over):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        channel_dir = _build_channel_dir(tmp_path, "chan", events=["e"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        if detect is not _ABSENT:
            brand["detect"] = detect
        brand.update(brand_over)
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")
        return profile.load("chan/e")

    def test_an_absent_detect_section_is_fine(self, tmp_path, monkeypatch):
        # Every profile written before this key existed must keep loading.
        p = self._load_with_detect(tmp_path, monkeypatch, _ABSENT)
        assert "detect" not in p.config

    def test_a_known_provider_loads(self, tmp_path, monkeypatch):
        p = self._load_with_detect(tmp_path, monkeypatch,
                                   {"provider": "gemini", "model": "gemini-2.5-flash"})
        assert p.config["detect"]["provider"] == "gemini"

    def test_an_unknown_provider_is_a_reported_defect(self, tmp_path, monkeypatch):
        with pytest.raises(profile.ProfileError) as caught:
            self._load_with_detect(tmp_path, monkeypatch, {"provider": "anthropc"})
        assert "detect.provider" in str(caught.value)
        assert "anthropc" in str(caught.value)

    def test_a_non_object_detect_section_is_a_reported_defect(self, tmp_path, monkeypatch):
        with pytest.raises(profile.ProfileError, match="'detect' must be an object"):
            self._load_with_detect(tmp_path, monkeypatch, "anthropic")

    def test_an_empty_model_is_a_reported_defect(self, tmp_path, monkeypatch):
        with pytest.raises(profile.ProfileError, match="detect.model"):
            self._load_with_detect(tmp_path, monkeypatch, {"model": "   "})

    def test_the_defect_is_collected_with_the_others(self, tmp_path, monkeypatch):
        # The module's whole point: five typos take one run, not five.
        with pytest.raises(profile.ProfileError) as caught:
            self._load_with_detect(
                tmp_path, monkeypatch, {"provider": "anthropc"},
                colors={"text": "#FFFFFF", "base": "#000000", "accent": "#333333"})
        message = str(caught.value)
        assert "detect.provider" in message
        assert "colors.edge" in message

    # --- the unit-level surface, in TestValidateUpload's style --------------

    def test_absent_and_empty_sections_are_clean(self):
        assert profile._validate_detect({}, self.PATH) == []
        assert profile._validate_detect({"detect": {}}, self.PATH) == []

    def test_a_null_detect_is_treated_as_absent(self):
        # `config.get("detect", {}) or {}` is what detect.py and the studio's
        # estimate route both do, so an explicit null already means "the
        # defaults" everywhere downstream - unlike 'subtitles', where null
        # reaches an AttributeError at render time and must be refused.
        assert profile._validate_detect({"detect": None}, self.PATH) == []

    def test_every_registered_provider_is_accepted(self):
        for provider_id in providers.PROVIDERS:
            assert profile._validate_detect(
                {"detect": {"provider": provider_id}}, self.PATH) == []

    def test_the_message_lists_the_providers_that_do_exist(self):
        problems = profile._validate_detect({"detect": {"provider": "anthropc"}}, self.PATH)
        assert len(problems) == 1
        for provider_id in providers.PROVIDERS:
            assert provider_id in problems[0]

    def test_a_non_string_model_is_rejected(self):
        problems = profile._validate_detect({"detect": {"model": 5}}, self.PATH)
        assert len(problems) == 1
        assert "detect.model" in problems[0]

    def test_a_model_is_not_checked_against_the_providers_catalogue(self):
        # Deliberate: validating it would mean carrying three vendors' model
        # lists and re-checking them monthly. An unknown model fails at call
        # time, is wrapped as ModelError, and degrades to the lexicon with the
        # loud log a missing key already produces.
        assert profile._validate_detect(
            {"detect": {"provider": "anthropic", "model": "claude-nonesuch-9"}},
            self.PATH) == []

    def test_both_defects_are_collected_together(self):
        problems = profile._validate_detect(
            {"detect": {"provider": "nope", "model": ""}}, self.PATH)
        assert len(problems) == 2

    # --- M3: the isinstance guard on `provider` ------------------------
    # Without it, `provider not in providers.PROVIDERS` raises TypeError
    # for an unhashable value (a list or a dict) straight out of a
    # function whose whole contract is to COLLECT defects, not throw on
    # the first one - and the entire suite still passed with the guard
    # removed, so this is pinned directly rather than relying on it being
    # exercised incidentally elsewhere.
    @pytest.mark.parametrize("bad_provider", [["anthropic"], {"id": "anthropic"}, 5, True])
    def test_a_hostile_provider_shape_is_one_clean_defect_not_a_crash(self, bad_provider):
        problems = profile._validate_detect({"detect": {"provider": bad_provider}}, self.PATH)
        assert len(problems) == 1
        assert "detect.provider" in problems[0]


class TestBandValidation:
    def _brand(self, tmp_path, monkeypatch, bands):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["bands"] = bands
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")
        return channel_dir

    def test_a_valid_section_loads(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"top": 0.5, "bottom": 0.0})
        assert profile.load("chan/event").config["bands"] == {"top": 0.5, "bottom": 0.0}

    def test_an_absent_section_defaults_to_full_strength(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        assert profile.load("chan/event").config["bands"] == {"top": 1.0, "bottom": 1.0}

    def test_a_non_dict_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, [1, 0])
        with pytest.raises(profile.ProfileError, match="'bands' must be an object"):
            profile.load("chan/event")

    def test_an_unknown_key_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"middle": 0.5})
        with pytest.raises(profile.ProfileError, match="unknown band 'bands.middle'"):
            profile.load("chan/event")

    def test_a_bool_is_a_reported_defect(self, monkeypatch, tmp_path):
        """True is an int in Python - the same trap output's integer check
        already guards. Without this, `"top": true` would load as 1.0."""
        self._brand(tmp_path, monkeypatch, {"top": True})
        with pytest.raises(profile.ProfileError, match="bands.top must be a number"):
            profile.load("chan/event")

    def test_a_string_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"top": "0.5"})
        with pytest.raises(profile.ProfileError, match="bands.top must be a number"):
            profile.load("chan/event")

    def test_a_value_above_one_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"top": 1.5})
        with pytest.raises(profile.ProfileError, match="bands.top must be between 0 and 1"):
            profile.load("chan/event")

    def test_a_negative_value_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"bottom": -0.1})
        with pytest.raises(profile.ProfileError, match="bands.bottom must be between 0 and 1"):
            profile.load("chan/event")

    def test_every_defect_is_reported_together(self, monkeypatch, tmp_path):
        """profile collects all defects rather than stopping at the first -
        someone typing a profile should not need one run per typo."""
        self._brand(tmp_path, monkeypatch, {"top": 2, "bottom": "x", "middle": 1})
        with pytest.raises(profile.ProfileError) as caught:
            profile.load("chan/event")
        message = str(caught.value)
        assert "bands.top" in message
        assert "bands.bottom" in message
        assert "bands.middle" in message

    def test_an_event_overrides_the_channel_band(self, monkeypatch, tmp_path):
        channel_dir = self._brand(tmp_path, monkeypatch, {"top": 0.5, "bottom": 0.5})
        event_dir = channel_dir / "events" / "event"
        (event_dir / "brand.json").write_text(
            json.dumps({"bands": {"top": 0.0}}), encoding="utf-8")
        # deep_merge is per key: the event's top wins, the channel's bottom
        # survives untouched.
        assert profile.load("chan/event").config["bands"] == {"top": 0.0, "bottom": 0.5}
