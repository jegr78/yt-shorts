from pathlib import Path

import pytest

from yt_shorts import font_admin
from yt_shorts.font_admin import FontAdminError

REAL_TTF = (Path(__file__).parent / "fixtures" / "channels" / "erf"
            / "fonts" / "BarlowCondensed-Bold.ttf").read_bytes()


def _channel(tmp_path):
    fonts = tmp_path / "channels" / "demo" / "fonts"
    fonts.mkdir(parents=True)
    return tmp_path / "channels"


class TestSave:
    def test_saves_a_valid_font_and_lists_it(self, tmp_path):
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont-Bold.ttf", REAL_TTF)
        assert (channels / "demo" / "fonts" / "MyFont-Bold.ttf").is_file()
        assert font_admin.list_fonts(channels, "demo") == ["MyFont-Bold.ttf"]

    def test_rejects_a_non_font_extension(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "demo", "evil.exe", REAL_TTF)
        assert e.value.kind == "bad_type"

    def test_rejects_non_font_bytes(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "demo", "broken.ttf", b"not a font")
        assert e.value.kind == "invalid"

    def test_rejects_oversize(self, tmp_path):
        channels = _channel(tmp_path)
        big = b"\x00" * (font_admin.MAX_FONT_BYTES + 1)
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "demo", "big.ttf", big)
        assert e.value.kind == "too_big"

    def test_rejects_a_traversal_filename(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "demo", "../escape.ttf", REAL_TTF)
        assert e.value.kind == "bad_name"
        assert not (channels.parent / "escape.ttf").exists()

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = tmp_path / "channels"
        channels.mkdir()
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "ghost", "MyFont.ttf", REAL_TTF)
        assert e.value.kind == "not_found"


class TestDelete:
    def test_deletes_an_unreferenced_font(self, tmp_path):
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont.ttf", REAL_TTF)
        font_admin.delete_font(channels, "demo", "MyFont.ttf")
        assert font_admin.list_fonts(channels, "demo") == []

    def test_refuses_a_font_assigned_in_brand_json(self, tmp_path):
        import json
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont.ttf", REAL_TTF)
        (channels / "demo" / "brand.json").write_text(
            json.dumps({"fonts": {"hook": "fonts/MyFont.ttf", "small": "fonts/MyFont.ttf"}}))
        with pytest.raises(FontAdminError) as e:
            font_admin.delete_font(channels, "demo", "MyFont.ttf")
        assert e.value.kind == "in_use"
        assert (channels / "demo" / "fonts" / "MyFont.ttf").is_file()

    def test_refuses_a_case_variant_of_an_assigned_font(self, tmp_path):
        # On a case-insensitive filesystem, deleting "MyFont.TTF" removes the
        # very file a ref to "fonts/MyFont.ttf" points at; the guard must catch
        # that, not just an exact-case string match. On a case-sensitive FS the
        # variant is a genuinely different (non-existent) file, so delete_font
        # raises not_found first - either way it is never silently deleted.
        import json
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont.ttf", REAL_TTF)
        (channels / "demo" / "brand.json").write_text(
            json.dumps({"fonts": {"hook": "fonts/MyFont.ttf", "small": "fonts/MyFont.ttf"}}))
        with pytest.raises(FontAdminError) as e:
            font_admin.delete_font(channels, "demo", "MyFont.TTF")
        assert e.value.kind in ("in_use", "not_found")
        assert (channels / "demo" / "fonts" / "MyFont.ttf").is_file()

    def test_refuses_a_font_assigned_only_by_an_event_override(self, tmp_path):
        # A font referenced only by an event-level brand.json override (which
        # profile.load deep-merges over the channel brand) must not be deletable -
        # doing so would brick that event, which the channel-only check missed.
        import json
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont.ttf", REAL_TTF)
        # Channel brand uses a different font; the event override uses MyFont.
        (channels / "demo" / "brand.json").write_text(
            json.dumps({"fonts": {"hook": "fonts/Other.ttf", "small": "fonts/Other.ttf"}}))
        event = channels / "demo" / "events" / "round-1"
        event.mkdir(parents=True)
        (event / "brand.json").write_text(
            json.dumps({"fonts": {"hook": "fonts/MyFont.ttf"}}))
        with pytest.raises(FontAdminError) as e:
            font_admin.delete_font(channels, "demo", "MyFont.ttf")
        assert e.value.kind == "in_use"
        assert (channels / "demo" / "fonts" / "MyFont.ttf").is_file()

    def test_refuses_when_brand_json_is_unreadable(self, tmp_path):
        # A corrupt brand.json means the assignments can't be read; deleting a
        # font that MIGHT be assigned could brick the channel, so refuse.
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont.ttf", REAL_TTF)
        (channels / "demo" / "brand.json").write_text("{ not json")
        with pytest.raises(FontAdminError) as e:
            font_admin.delete_font(channels, "demo", "MyFont.ttf")
        assert e.value.kind == "in_use"
        assert (channels / "demo" / "fonts" / "MyFont.ttf").is_file()

    def test_unknown_font_is_not_found(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(FontAdminError) as e:
            font_admin.delete_font(channels, "demo", "ghost.ttf")
        assert e.value.kind == "not_found"


import json as _json

from yt_shorts import font_admin as _fa


def _chan_with_event(tmp_path):
    ch = tmp_path / "erf"
    (ch / "fonts").mkdir(parents=True)
    ev = ch / "events" / "ev"
    ev.mkdir(parents=True)
    return tmp_path, ev


def test_event_font_save_list_delete(tmp_path):
    channels, ev = _chan_with_event(tmp_path)
    # use an existing fixture font's bytes (a real loadable font so save passes)
    import pathlib
    font_bytes = next((pathlib.Path("tests/fixtures/channels/erf/fonts")).glob("*.ttf")).read_bytes()
    _fa.save_event_font(channels, "erf", "ev", "Special.ttf", font_bytes)
    assert "Special.ttf" in _fa.list_event_fonts(channels, "erf", "ev")
    _fa.delete_event_font(channels, "erf", "ev", "Special.ttf")
    assert "Special.ttf" not in _fa.list_event_fonts(channels, "erf", "ev")


def test_event_font_in_use_is_refused(tmp_path):
    channels, ev = _chan_with_event(tmp_path)
    import pathlib
    font_bytes = next((pathlib.Path("tests/fixtures/channels/erf/fonts")).glob("*.ttf")).read_bytes()
    _fa.save_event_font(channels, "erf", "ev", "Special.ttf", font_bytes)
    (ev / "brand.json").write_text(_json.dumps(
        {"fonts": {"hook": "fonts/Special.ttf", "small": "fonts/Special.ttf"}}), encoding="utf-8")
    with pytest.raises(_fa.FontAdminError) as e:
        _fa.delete_event_font(channels, "erf", "ev", "Special.ttf")
    assert e.value.kind == "in_use"


class TestTheContainmentCheck:
    """Exercised directly: through the public functions NAME_PATTERN gets
    there first."""

    def test_a_traversal_channel_is_refused(self, tmp_path):
        with pytest.raises(FontAdminError) as error:
            font_admin._within(tmp_path, "../pwned")
        assert error.value.kind == "bad_name"

    def test_a_traversal_filename_is_refused(self, tmp_path):
        with pytest.raises(FontAdminError) as error:
            font_admin._within(tmp_path, "..", "pwned.ttf")
        assert error.value.kind == "bad_name"

    def test_an_absolute_part_is_refused(self, tmp_path):
        # os.path.join DISCARDS everything before an absolute part, so without
        # the check this would land outside the root entirely.
        with pytest.raises(FontAdminError) as error:
            font_admin._within(tmp_path, str(tmp_path.parent / "pwned"))
        assert error.value.kind == "bad_name"

    def test_an_ordinary_name_resolves_inside(self, tmp_path):
        assert font_admin._within(tmp_path, "erf") == tmp_path / "erf"

    def test_an_event_fonts_path_resolves_inside(self, tmp_path):
        got = font_admin._within(tmp_path, "erf", "events", "round-1")
        assert got == tmp_path / "erf" / "events" / "round-1"
