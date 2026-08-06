"""Acceptance test: an event overriding only colors.accent must differ from
the channel overlay in the accent colour and nothing else - verified by
comparing pixels, not by reading the code.

Uses a throwaway channel (tmp_path, monkeypatched CHANNELS_DIR) with a
layout.py whose decorate() paints a known rectangle in config["colors"]
["accent"], so the accent's effect on the rendered pixels is unambiguous
and localized.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import ImageColor

from yt_shorts import profile
from yt_shorts.overlay import build_overlay

RECT = (100, 100, 979, 299)  # inclusive x0,y0,x1,y1 - matches layout.py below


def _with_alpha(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    r, g, b = ImageColor.getrgb(hex_color)
    return (r, g, b, alpha)


def _rect_coordinates():
    x0, y0, x1, y1 = RECT
    return {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


def _build_channel(basis: Path, name: str, accent: str, events: list[str]) -> Path:
    channel_dir = basis / name
    fonts_dir = channel_dir / "fonts"
    fonts_dir.mkdir(parents=True)
    (fonts_dir / "Channel.ttf").write_bytes(
        (Path(__file__).resolve().parent / "fixtures" / "channels" / "erf" / "fonts" / "Oswald-Bold.ttf").read_bytes()
    )
    (channel_dir / "channel.json").write_text(json.dumps({
        "id": "UCtest", "handle": "@test", "display_name": "Test Channel",
        "language": "en", "footer": "TEST | @test",
        "channel_url": "https://example.invalid/test",
    }), encoding="utf-8")
    (channel_dir / "brand.json").write_text(json.dumps({
        "colors": {"text": "#FFFFFF", "base": "#101010", "accent": accent, "edge": "#00FF00"},
        "fonts": {"hook": "fonts/Channel.ttf", "small": "fonts/Channel.ttf"},
        "output": {"width": 1080, "height": 1920, "video_width": 1080,
                   "video_height": 608, "video_y": 600},
    }), encoding="utf-8")
    (channel_dir / "layout.py").write_text(
        "from PIL import ImageColor\n"
        "def decorate(draw, config, window_top, window_bottom):\n"
        "    r, g, b = ImageColor.getrgb(config['colors']['accent'])\n"
        f"    draw.rectangle({list(RECT)}, fill=(r, g, b, 255))\n",
        encoding="utf-8",
    )
    events_dir = channel_dir / "events"
    events_dir.mkdir()
    for event in events:
        (events_dir / event).mkdir()
    return channel_dir


class TestAccentOnlyOverrideChangesOnlyTheAccent:
    def test_diff_is_exactly_the_accent_painted_rectangle(self, monkeypatch, tmp_path):
        channel_accent = "#101010"
        event_accent = "#FF00AA"
        channel_dir = _build_channel(tmp_path, "chan", channel_accent, events=["bare", "overridden"])
        event_dir = channel_dir / "events" / "overridden"
        (event_dir / "brand.json").write_text(
            json.dumps({"colors": {"accent": event_accent}}), encoding="utf-8"
        )
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        base_profile = profile.load("chan/bare")
        event_profile = profile.load("chan/overridden")

        # Empty hook/footer: isolates the comparison to the accent-painted
        # rectangle alone, the same way tests/test_overlay.py isolates hook
        # pixels for the overflow guard. With text present, identical hook
        # pixels drawn on top of the rectangle in both images would occlude
        # part of the color difference without adding a new one - a
        # correct case, but it would make the diff set a strict subset of
        # the rectangle instead of exactly the rectangle, which is a weaker,
        # noisier assertion than necessary here.
        hook, footer = "", ""
        base_image = build_overlay(hook, footer, base_profile.config)
        event_image = build_overlay(hook, footer, event_profile.config)

        width = base_profile.config["output"]["width"]
        base_pixels = list(base_image.getdata())
        event_pixels = list(event_image.getdata())
        assert len(base_pixels) == len(event_pixels)

        diff_coords = set()
        for index, (a, b) in enumerate(zip(base_pixels, event_pixels, strict=True)):
            if a != b:
                diff_coords.add((index % width, index // width))

        expected_rect = _rect_coordinates()
        assert diff_coords, "Expected the accent override to change some pixels"
        assert diff_coords == expected_rect, (
            "Pixels differ outside the accent-painted rectangle, or not all "
            "of it differs - the override touched more (or less) than the accent"
        )

        # And the differing pixels really do hold the two accent colours,
        # nothing else.
        x, y = next(iter(expected_rect))
        assert base_image.getpixel((x, y)) == _with_alpha(channel_accent)
        assert event_image.getpixel((x, y)) == _with_alpha(event_accent)

    def test_everything_outside_the_rectangle_is_byte_identical(self, monkeypatch, tmp_path):
        channel_dir = _build_channel(tmp_path, "chan", "#101010", events=["bare", "overridden"])
        event_dir = channel_dir / "events" / "overridden"
        (event_dir / "brand.json").write_text(
            json.dumps({"colors": {"accent": "#FF00AA"}}), encoding="utf-8"
        )
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path)

        base_profile = profile.load("chan/bare")
        event_profile = profile.load("chan/overridden")
        # Empty hook/footer: isolates the comparison to the accent-painted
        # rectangle alone, the same way tests/test_overlay.py isolates hook
        # pixels for the overflow guard. With text present, identical hook
        # pixels drawn on top of the rectangle in both images would occlude
        # part of the color difference without adding a new one - a
        # correct case, but it would make the diff set a strict subset of
        # the rectangle instead of exactly the rectangle, which is a weaker,
        # noisier assertion than necessary here.
        hook, footer = "", ""
        base_image = build_overlay(hook, footer, base_profile.config)
        event_image = build_overlay(hook, footer, event_profile.config)

        rect = _rect_coordinates()
        for y in (0, 50, 150, 400, 900, 1500, 1900):
            for x in (0, 200, 540, 900, 1079):
                if (x, y) in rect:
                    continue
                assert base_image.getpixel((x, y)) == event_image.getpixel((x, y)), (x, y)
