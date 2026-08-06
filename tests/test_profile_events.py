"""Tests for the event-level profile layer in yt_shorts.profile.

An event folder (channels/<channel>/events/<event>/) may optionally carry
its own brand.json, fonts/, assets/ and layout.py. Resolution order:

  value      ->  event profile   ->  channel profile  ->  built-in default
  font file  ->  event/fonts/    ->  channel/fonts/
  layout.py  ->  event/          ->  channel/          ->  plain bars
  logo       ->  event/assets/   ->  channel/assets/    ->  none

Merge is a deep merge per key, event wins, leaf values only - lists (there
are none in this format) replace wholesale.

Everything here uses throwaway channel/event folders under tmp_path via
monkeypatching profile.CHANNELS_DIR, so no reference to channels/erf is
needed. That channel used to be covered by a byte-identical reference overlay
comparison; it was deleted because its hashes measured FreeType and HarfBuzz
rather than this project's code (see CLAUDE.md, "Verifying changes").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yt_shorts import profile


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _build_channel(basis: Path, name: str, *, with_layout: bool = False,
                   events: list[str] | None = None) -> Path:
    channel_dir = basis / name
    fonts_dir = channel_dir / "fonts"
    fonts_dir.mkdir(parents=True)
    (fonts_dir / "Channel.ttf").write_bytes(b"")
    _write_json(channel_dir / "channel.json", {
        "id": "UCtest", "handle": "@test", "display_name": "Test Channel",
        "language": "en", "footer": "TEST | @test",
        "channel_url": "https://example.invalid/test",
    })
    _write_json(channel_dir / "brand.json", {
        "colors": {"text": "#FFFFFF", "base": "#101010", "accent": "#144E53", "edge": "#B8F5CA"},
        "fonts": {"hook": "fonts/Channel.ttf", "small": "fonts/Channel.ttf"},
        "output": {"width": 1080, "height": 1920, "video_width": 1080,
                   "video_height": 608, "video_y": 600},
    })
    if with_layout:
        (channel_dir / "layout.py").write_text(
            "def decorate(draw, config, window_top, window_bottom):\n"
            "    draw.rectangle([0, 0, 10, 10], fill=(255, 0, 0, 255))\n",
            encoding="utf-8",
        )
    events_dir = channel_dir / "events"
    events_dir.mkdir()
    for event in events or []:
        (events_dir / event).mkdir()
    return channel_dir


class TestEventWithoutAnyOverrideFiles:
    """An event with none of brand.json/fonts/assets/layout.py must behave
    exactly as the channel does."""

    def test_config_matches_channel_only_profile(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)
        _build_channel(tmp_path, "plain", with_layout=True, events=["bare-event"])

        p = profile.load("plain/bare-event")

        assert p.config["colors"] == {
            "text": "#FFFFFF", "base": "#101010", "accent": "#144E53", "edge": "#B8F5CA",
        }
        assert Path(p.config["fonts"]["hook"]).name == "Channel.ttf"
        assert callable(p.config["decorate"])
        assert "logo" not in p.config


class TestEventBrandOverride:
    def test_accent_only_override_keeps_every_other_color(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        _write_json(event_dir / "brand.json", {"colors": {"accent": "#FF00FF"}})
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        assert p.config["colors"] == {
            "text": "#FFFFFF", "base": "#101010", "accent": "#FF00FF", "edge": "#B8F5CA",
        }

    def test_override_does_not_touch_output_or_fonts(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        _write_json(event_dir / "brand.json", {"colors": {"accent": "#FF00FF"}})
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        assert p.config["output"] == {
            "width": 1080, "height": 1920, "video_width": 1080,
            "video_height": 608, "video_y": 600,
        }
        assert Path(p.config["fonts"]["hook"]).name == "Channel.ttf"

    def test_event_output_override_merges_with_channel_output(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        _write_json(event_dir / "brand.json", {"output": {"video_y": 700}})
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        assert p.config["output"]["video_y"] == 700
        assert p.config["output"]["width"] == 1080  # kept from the channel


class TestEventFontResolution:
    def test_event_font_naming_a_file_in_event_fonts_resolves_there(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        event_fonts = event_dir / "fonts"
        event_fonts.mkdir()
        (event_fonts / "Event.ttf").write_bytes(b"")
        _write_json(event_dir / "brand.json", {"fonts": {"hook": "fonts/Event.ttf"}})
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        assert Path(p.config["fonts"]["hook"]) == event_fonts / "Event.ttf"
        assert Path(p.config["fonts"]["hook"]).exists()
        # small font untouched: still resolves against the channel
        assert Path(p.config["fonts"]["small"]) == channel_dir / "fonts" / "Channel.ttf"

    def test_event_can_name_a_channel_font_without_copying_it(self, monkeypatch, tmp_path):
        """The event's brand.json points 'hook' at a relative path that only
        exists under the channel folder, not under the event's own fonts/ -
        resolution must fall through to the channel."""
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        _write_json(event_dir / "brand.json", {"fonts": {"hook": "fonts/Channel.ttf"}})
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        assert Path(p.config["fonts"]["hook"]) == channel_dir / "fonts" / "Channel.ttf"

    def test_font_missing_from_both_event_and_channel_is_reported(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        _write_json(event_dir / "brand.json", {"fonts": {"hook": "fonts/DoesNotExist.ttf"}})
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        with pytest.raises(profile.ProfileError, match="DoesNotExist.ttf"):
            profile.load("chan/nls-2026")


class TestEventLayoutPy:
    def test_event_layout_overrides_channel_layout(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", with_layout=True, events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        (event_dir / "layout.py").write_text(
            "def decorate(draw, config, window_top, window_bottom):\n"
            "    draw.rectangle([0, 0, 10, 10], fill=(0, 255, 0, 255))\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        from yt_shorts.overlay import build_overlay
        image = build_overlay("", "", p.config)
        assert image.getpixel((5, 5)) == (0, 255, 0, 255)  # event's green, not channel's red

    def test_event_without_layout_falls_back_to_channel_layout(self, monkeypatch, tmp_path):
        _build_channel(tmp_path, "chan", with_layout=True, events=["nls-2026"])
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        from yt_shorts.overlay import build_overlay
        image = build_overlay("", "", p.config)
        assert image.getpixel((5, 5)) == (255, 0, 0, 255)  # channel's red

    def test_event_layout_used_when_channel_has_none(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", with_layout=False, events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        (event_dir / "layout.py").write_text(
            "def decorate(draw, config, window_top, window_bottom):\n"
            "    draw.rectangle([0, 0, 10, 10], fill=(0, 0, 255, 255))\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        from yt_shorts.overlay import build_overlay
        image = build_overlay("", "", p.config)
        assert image.getpixel((5, 5)) == (0, 0, 255, 255)


class TestEventLogo:
    def _make_logo_png(self, path: Path, size=(400, 100)) -> None:
        from PIL import Image
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", size, (255, 255, 0, 255)).save(path)

    def test_event_logo_resolves_against_event_assets(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        self._make_logo_png(event_dir / "assets" / "logo.png")
        _write_json(event_dir / "brand.json", {
            "logo": {"file": "assets/logo.png", "max_height": 160, "gap": 24},
        })
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        assert Path(p.config["logo"]["file"]) == event_dir / "assets" / "logo.png"
        assert p.config["logo"]["max_height"] == 160
        assert p.config["logo"]["gap"] == 24

    def test_channel_logo_used_when_no_event_override(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        self._make_logo_png(channel_dir / "assets" / "logo.png")
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["logo"] = {"file": "assets/logo.png", "max_height": 160, "gap": 24}
        _write_json(channel_dir / "brand.json", brand)
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        assert Path(p.config["logo"]["file"]) == channel_dir / "assets" / "logo.png"

    def test_missing_logo_file_is_reported(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        _write_json(event_dir / "brand.json", {
            "logo": {"file": "assets/does-not-exist.png"},
        })
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        with pytest.raises(profile.ProfileError, match="does-not-exist.png"):
            profile.load("chan/nls-2026")

    def test_no_logo_key_anywhere_means_no_logo_in_config(self, monkeypatch, tmp_path):
        _build_channel(tmp_path, "chan", events=["nls-2026"])
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        p = profile.load("chan/nls-2026")

        assert "logo" not in p.config


class TestEventBrokenJson:
    def test_invalid_event_brand_json_raises_understandable_error(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", events=["nls-2026"])
        event_dir = channel_dir / "events" / "nls-2026"
        (event_dir / "brand.json").write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        with pytest.raises(profile.ProfileError, match="brand.json"):
            profile.load("chan/nls-2026")
