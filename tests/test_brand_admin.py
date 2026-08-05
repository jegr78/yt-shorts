import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from yt_shorts import brand_admin, channel_admin
from yt_shorts.brand_admin import BrandAdminError

FIXTURE_CHANNELS = Path(__file__).parent / "fixtures" / "channels"

REAL_TTF = (Path(__file__).parent / "fixtures" / "channels" / "erf"
            / "fonts" / "BarlowCondensed-Bold.ttf").read_bytes()


@pytest.fixture
def channels_dir(tmp_path):
    """A tmp copy of the checked-in erf fixture channel (see conftest.py's
    own note on tests/fixtures/channels/erf) so TestBands can exercise the
    real 'erf' channel by name, same technique test_studio_api.py's
    studio_profile fixture uses."""
    channels = tmp_path / "channels"
    shutil.copytree(FIXTURE_CHANNELS / "erf", channels / "erf")
    return channels

FIELDS = {"id": "UCabc", "channel_url": "https://www.youtube.com/channel/UCabc",
          "handle": "@demo", "display_name": "Demo League", "language": "en",
          "footer": "DEMO | @demo"}


def _channel_with_font(tmp_path):
    channels = tmp_path / "channels"
    channels.mkdir()
    channel_admin.create_channel(channels, "demo", FIELDS)   # scaffolds brand.json + fonts/
    (channels / "demo" / "fonts" / "MyFont.ttf").write_bytes(REAL_TTF)
    return channels


VALID_FONTS = {"hook": "fonts/MyFont.ttf", "small": "fonts/MyFont.ttf"}


class TestRead:
    def test_returns_the_channels_brand(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        brand = brand_admin.read_brand(channels, "demo")
        assert "colors" in brand and "output" in brand

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = tmp_path / "channels"
        channels.mkdir()
        with pytest.raises(BrandAdminError) as e:
            brand_admin.read_brand(channels, "ghost")
        assert e.value.kind == "not_found"


class TestUpdate:
    def test_applies_colors_fonts_subtitles_and_keeps_output(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        before = brand_admin.read_brand(channels, "demo")["output"]
        brand_admin.update_brand(channels, "demo", {
            "colors": {"text": "#000000", "base": "#FFFFFF", "accent": "#FF0000", "edge": "#00FF00"},
            "fonts": VALID_FONTS,
            "subtitles": {"enabled": True}})
        brand = brand_admin.read_brand(channels, "demo")
        assert brand["colors"]["text"] == "#000000"
        assert brand["fonts"] == VALID_FONTS
        assert brand["subtitles"]["enabled"] is True
        assert brand["output"] == before          # output untouched

    def test_rejects_a_bad_color(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "colors": {"text": "nope", "base": "#000", "accent": "#000", "edge": "#000"},
                "fonts": VALID_FONTS})
        assert e.value.kind == "bad_color"

    def test_rejects_a_font_not_present(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "fonts": {"hook": "fonts/absent.ttf", "small": "fonts/MyFont.ttf"}})
        assert e.value.kind == "bad_font"

    def test_rejects_a_font_ref_that_escapes(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "fonts": {"hook": "fonts/../../evil.ttf", "small": "fonts/MyFont.ttf"}})
        assert e.value.kind == "bad_font"

    def test_rejects_non_bool_subtitles(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "fonts": VALID_FONTS, "subtitles": {"enabled": "yes"}})
        assert e.value.kind == "bad_subtitles"

    def test_rejects_subtitles_geometry_profile_would_reject(self, tmp_path):
        # A subtitles block update_brand accepts must be one profile.load
        # accepts (validation shares profile._validate_subtitles). An enabled
        # caption at y=5 collides with the video window - profile.load rejects
        # it, so update_brand must too, or the channel is persisted broken.
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "fonts": VALID_FONTS,
                "subtitles": {"enabled": True, "y": 5, "size": 40}})
        assert e.value.kind == "bad_subtitles"
        # nothing persisted: the on-disk brand still has the scaffold subtitles
        assert brand_admin.read_brand(channels, "demo")["subtitles"] == {"enabled": False}

    def test_accepts_valid_subtitles_geometry(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        brand_admin.update_brand(channels, "demo", {
            "fonts": VALID_FONTS,
            "subtitles": {"enabled": True, "y": 1300, "size": 40}})
        assert brand_admin.read_brand(channels, "demo")["subtitles"]["y"] == 1300

    def test_rejects_a_stored_output_that_profile_would_reject(self, tmp_path):
        # output is never patched, but a pre-existing broken output on disk (e.g.
        # a quoting typo "video_y": "600") must be caught so update_brand can't
        # accept-and-rewrite a brand.json profile.load would then reject.
        channels = _channel_with_font(tmp_path)
        brand_path = channels / "demo" / "brand.json"
        brand = json.loads(brand_path.read_text())
        brand["output"]["video_y"] = "600"   # string, not int
        brand_path.write_text(json.dumps(brand))
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {"fonts": VALID_FONTS})
        assert e.value.kind == "bad_brand"

    def test_rejects_a_stored_upload_mode_that_profile_would_reject(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        brand_path = channels / "demo" / "brand.json"
        brand = json.loads(brand_path.read_text())
        brand["upload"] = {"mode": "sometimes"}   # not api/manual
        brand_path.write_text(json.dumps(brand))
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {"fonts": VALID_FONTS})
        assert e.value.kind == "bad_brand"

    def test_an_accepted_brand_then_loads_via_profile(self, tmp_path, monkeypatch):
        from yt_shorts import profile as profile_module
        channels = _channel_with_font(tmp_path)
        (channels / "demo" / "events" / "round-1").mkdir(parents=True)
        brand_admin.update_brand(channels, "demo", {
            "colors": {"text": "#FFFFFF", "base": "#101010", "accent": "#144E53", "edge": "#B8F5CA"},
            "fonts": VALID_FONTS,
            "subtitles": {"enabled": True, "y": 1300, "size": 40}})
        monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels)
        loaded = profile_module.load("demo/round-1")   # must not raise
        assert loaded.config["colors"]["text"] == "#FFFFFF"
        assert loaded.config["subtitles"]["y"] == 1300


def _channel_with_font_and_logo(tmp_path):
    """A channel whose fonts are valid AND that has an assets/logo.png, so a
    logo/output/upload patch validates as a whole."""
    channels = _channel_with_font(tmp_path)
    assets = channels / "demo" / "assets"
    assets.mkdir()
    Image.new("RGBA", (120, 120), (255, 255, 0, 255)).save(assets / "logo.png")
    return channels


class TestUpdateExtendedSections:
    """update_brand now also writes logo, output and upload (each validated the
    same way profile.load validates it)."""

    def test_applies_logo(self, tmp_path):
        channels = _channel_with_font_and_logo(tmp_path)
        brand_admin.update_brand(channels, "demo", {
            "fonts": VALID_FONTS,
            "logo": {"file": "assets/logo.png", "position": "bottom-right",
                     "variant": "color", "opacity": 0.7}})
        logo = brand_admin.read_brand(channels, "demo")["logo"]
        assert logo["position"] == "bottom-right"
        assert logo["variant"] == "color"
        assert logo["opacity"] == 0.7

    def test_rejects_bad_logo_position(self, tmp_path):
        channels = _channel_with_font_and_logo(tmp_path)
        with pytest.raises(BrandAdminError):
            brand_admin.update_brand(channels, "demo", {
                "fonts": VALID_FONTS,
                "logo": {"file": "assets/logo.png", "position": "middle"}})

    def test_applies_output_geometry(self, tmp_path):
        channels = _channel_with_font_and_logo(tmp_path)
        before = brand_admin.read_brand(channels, "demo")["output"]
        new_output = {**before, "video_y": before["video_y"] + 8}
        brand_admin.update_brand(channels, "demo", {"fonts": VALID_FONTS, "output": new_output})
        assert brand_admin.read_brand(channels, "demo")["output"]["video_y"] == before["video_y"] + 8

    def test_rejects_output_window_outside_frame(self, tmp_path):
        channels = _channel_with_font_and_logo(tmp_path)
        before = brand_admin.read_brand(channels, "demo")["output"]
        # push the window past the bottom of the frame -> profile._validate_brand rejects
        broken = {**before, "video_y": before["height"] + 100}
        with pytest.raises(BrandAdminError):
            brand_admin.update_brand(channels, "demo", {"fonts": VALID_FONTS, "output": broken})

    def test_applies_upload_mode(self, tmp_path):
        channels = _channel_with_font_and_logo(tmp_path)
        brand_admin.update_brand(channels, "demo", {"fonts": VALID_FONTS, "upload": {"mode": "manual"}})
        assert brand_admin.read_brand(channels, "demo")["upload"]["mode"] == "manual"


class TestSetUploadMode:
    """The lightweight Settings toggle: sets only upload.mode, without requiring
    an otherwise-complete brand."""

    def test_sets_mode_on_an_incomplete_scaffold_brand(self, tmp_path):
        # a fresh channel: brand.json points fonts at a font that does not exist
        channels = tmp_path / "channels"
        channels.mkdir()
        channel_admin.create_channel(channels, "demo", FIELDS)
        brand_admin.set_upload_mode(channels, "demo", "manual")
        assert brand_admin.read_brand(channels, "demo")["upload"]["mode"] == "manual"
        brand_admin.set_upload_mode(channels, "demo", "api")
        assert brand_admin.read_brand(channels, "demo")["upload"]["mode"] == "api"

    def test_rejects_an_unknown_mode(self, tmp_path):
        channels = tmp_path / "channels"
        channels.mkdir()
        channel_admin.create_channel(channels, "demo", FIELDS)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.set_upload_mode(channels, "demo", "public")
        assert e.value.kind == "bad_field"

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = tmp_path / "channels"
        channels.mkdir()
        with pytest.raises(BrandAdminError) as e:
            brand_admin.set_upload_mode(channels, "ghost", "manual")
        assert e.value.kind == "not_found"


class TestBands:
    def test_a_valid_bands_section_is_stored(self, channels_dir):
        brand_admin.update_brand(channels_dir, "erf", {"bands": {"top": 0.5, "bottom": 0.0}})
        brand = brand_admin.read_brand(channels_dir, "erf")
        assert brand["bands"] == {"top": 0.5, "bottom": 0.0}

    def test_an_out_of_range_band_is_refused(self, channels_dir):
        with pytest.raises(brand_admin.BrandAdminError) as caught:
            brand_admin.update_brand(channels_dir, "erf", {"bands": {"top": 4}})
        assert caught.value.kind == "bad_brand"

    def test_a_bool_band_is_refused(self, channels_dir):
        with pytest.raises(brand_admin.BrandAdminError):
            brand_admin.update_brand(channels_dir, "erf", {"bands": {"top": True}})


class TestDetectSection:
    """detect (which model provider scores moments, and with what model) is
    accepted by update_brand and validated by borrowing profile._validate_detect
    (Task 4) - the same borrowing _validate already does for subtitles/output/
    logo/upload above."""

    def test_a_valid_detect_section_is_stored(self, channels_dir):
        brand_admin.update_brand(channels_dir, "erf",
                                  {"detect": {"provider": "gemini", "model": "x"}})
        assert brand_admin.read_brand(channels_dir, "erf")["detect"]["provider"] == "gemini"

    def test_an_unknown_provider_is_refused(self, channels_dir):
        with pytest.raises(brand_admin.BrandAdminError) as caught:
            brand_admin.update_brand(channels_dir, "erf", {"detect": {"provider": "nope"}})
        assert caught.value.kind == "bad_detect"

    def test_a_non_object_detect_section_is_refused(self, channels_dir):
        with pytest.raises(brand_admin.BrandAdminError) as caught:
            brand_admin.update_brand(channels_dir, "erf", {"detect": "gemini"})
        assert caught.value.kind == "bad_detect"

    def test_an_accepted_detect_section_round_trips_through_profile_load(
            self, channels_dir, monkeypatch):
        # ONE DIRECTION of the module's standing invariant, and only one: a
        # detect section update_brand accepted is written in a shape
        # profile.load reads back intact. It does NOT prove the stronger
        # claim its old name made ("a patch this accepts is one profile.load
        # accepts") - measured, not assumed: a reviewer replaced _validate's
        # borrowed profile._validate_detect call with a plausible divergent
        # reimplementation (a shape check with no membership check) and this
        # test stayed GREEN, because it only ever sends a VALID value, which
        # stays valid under any laxer copy. What caught that divergence was
        # the negative case above, test_an_unknown_provider_is_refused - so
        # the pair is what pins the invariant, and this half alone is a
        # round-trip.
        from yt_shorts import profile as profile_module
        brand_admin.update_brand(channels_dir, "erf",
                                  {"detect": {"provider": "gemini", "model": "x"}})
        monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels_dir)
        loaded = profile_module.load("erf/community-clips-back-catalogue")   # must not raise
        assert loaded.config["detect"]["provider"] == "gemini"
