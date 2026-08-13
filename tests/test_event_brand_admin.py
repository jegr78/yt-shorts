import json
import os
import shutil
from pathlib import Path

import pytest

from yt_shorts import event_brand_admin as eba

FIXTURE_CHANNELS = Path(__file__).parent / "fixtures" / "channels"


@pytest.fixture
def channels_dir(tmp_path):
    """A tmp copy of the checked-in erf fixture channel (including its
    community-clips-back-catalogue event), same technique as
    test_brand_admin.py's own channels_dir fixture."""
    channels = tmp_path / "channels"
    shutil.copytree(FIXTURE_CHANNELS / "erf", channels / "erf")
    return channels


def _channel(tmp_path, *, with_font="Hook.ttf") -> Path:
    """A minimal complete channel brand + one font, mirroring the shape
    brand_admin._validate accepts."""
    ch = tmp_path / "erf"
    (ch / "fonts").mkdir(parents=True)
    (ch / "events").mkdir()
    if with_font:
        (ch / "fonts" / with_font).write_bytes(b"not-really-a-font")
    brand = {
        "colors": {"text": "#FFFFFF", "base": "#004625", "accent": "#144E53", "edge": "#B8F5CA"},
        "fonts": {"hook": "fonts/Hook.ttf", "small": "fonts/Hook.ttf"},
        "output": {"width": 1080, "height": 1920, "video_width": 1080,
                   "video_height": 608, "video_y": 600},
        "upload": {"mode": "manual"},
    }
    (ch / "brand.json").write_text(json.dumps(brand), encoding="utf-8")
    return tmp_path


def _event(channels_dir: Path, name="ev") -> Path:
    d = channels_dir / "erf" / "events" / name
    d.mkdir(parents=True)
    return d


def test_read_returns_override_channel_effective(tmp_path):
    channels = _channel(tmp_path)
    _event(channels)
    out = eba.read_event_brand(channels, "erf", "ev")
    assert out["override"] == {}
    assert out["channel"]["colors"]["accent"] == "#144E53"
    assert out["effective"]["colors"]["accent"] == "#144E53"  # inherited


def test_override_one_color_writes_only_that_section_and_merges(tmp_path):
    channels = _channel(tmp_path)
    event_dir = _event(channels)
    eba.update_event_brand(channels, "erf", "ev",
                           {"colors": {"text": "#FFFFFF", "base": "#004625",
                                       "accent": "#FF0000", "edge": "#B8F5CA"}})
    written = json.loads((event_dir / "brand.json").read_text())
    assert set(written) == {"colors"}          # ONLY the overridden section
    out = eba.read_event_brand(channels, "erf", "ev")
    assert out["effective"]["colors"]["accent"] == "#FF0000"   # override wins
    assert out["effective"]["fonts"]["hook"] == "fonts/Hook.ttf"  # inherited


def test_empty_patch_deletes_the_override_file(tmp_path):
    channels = _channel(tmp_path)
    event_dir = _event(channels)
    (event_dir / "brand.json").write_text('{"colors": {}}', encoding="utf-8")
    eba.update_event_brand(channels, "erf", "ev", {})
    assert not (event_dir / "brand.json").exists()


def test_upload_section_is_rejected(tmp_path):
    channels = _channel(tmp_path)
    _event(channels)
    with pytest.raises(eba.EventBrandError) as e:
        eba.update_event_brand(channels, "erf", "ev", {"upload": {"mode": "api"}})
    assert e.value.kind == "bad_field"


def test_override_that_breaks_the_merge_is_rejected(tmp_path):
    channels = _channel(tmp_path)
    _event(channels)
    with pytest.raises(eba.EventBrandError) as e:
        eba.update_event_brand(channels, "erf", "ev",
                               {"colors": {"text": "not-a-color", "base": "#004625",
                                           "accent": "#144E53", "edge": "#B8F5CA"}})
    assert e.value.kind == "bad_color"


def test_partial_override_valid_only_after_merge_is_accepted(tmp_path):
    # override sets ONLY accent; text/base/edge + fonts come from the channel.
    channels = _channel(tmp_path)
    _event(channels)
    eba.update_event_brand(channels, "erf", "ev", {"colors": {"accent": "#FF0000"}})
    out = eba.read_event_brand(channels, "erf", "ev")
    assert out["effective"]["colors"]["accent"] == "#FF0000"
    assert out["effective"]["colors"]["text"] == "#FFFFFF"   # from channel


class TestBandsOverride:
    def test_an_event_may_override_bands(self, channels_dir):
        eba.update_event_brand(
            channels_dir, "erf", "community-clips-back-catalogue",
            {"bands": {"top": 0.0, "bottom": 1.0}})
        state = eba.read_event_brand(
            channels_dir, "erf", "community-clips-back-catalogue")
        assert state["override"]["bands"] == {"top": 0.0, "bottom": 1.0}
        assert state["effective"]["bands"] == {"top": 0.0, "bottom": 1.0}

    def test_an_invalid_override_is_refused(self, channels_dir):
        with pytest.raises(eba.EventBrandError):
            eba.update_event_brand(
                channels_dir, "erf", "community-clips-back-catalogue",
                {"bands": {"top": -1}})


def test_event_font_ref_resolves_event_first(tmp_path):
    channels = _channel(tmp_path)
    event_dir = _event(channels)
    (event_dir / "fonts").mkdir()
    (event_dir / "fonts" / "Special.ttf").write_bytes(b"x")
    p = eba.resolve_event_font_ref(event_dir, channels / "erf", "fonts/Special.ttf")
    assert p == event_dir / "fonts" / "Special.ttf"


def test_bad_segment_rejected(tmp_path):
    channels = _channel(tmp_path)
    with pytest.raises(eba.EventBrandError) as e:
        eba.read_event_brand(channels, "erf", "../escape")
    assert e.value.kind == "bad_name"


def test_a_failed_override_leaves_the_previous_brand_json_complete(tmp_path, monkeypatch):
    """Writes through `atomicwrite`, so a reader can never find this file
    empty (see that module's docstring for the CI failure that measured
    the alternative). `os.replace` is the only step that can fail after
    the new bytes exist and before they are in place - failing anything
    earlier would pass under a truncating write too."""
    channels = _channel(tmp_path)
    event_dir = _event(channels)
    eba.update_event_brand(channels, "erf", "ev", {"colors": {
        "text": "#FFFFFF", "base": "#004625", "accent": "#FF0000", "edge": "#B8F5CA"}})
    path = event_dir / "brand.json"
    before = path.read_bytes()

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        eba.update_event_brand(channels, "erf", "ev", {"colors": {
            "text": "#FFFFFF", "base": "#004625", "accent": "#00FF00", "edge": "#B8F5CA"}})

    assert path.read_bytes() == before
