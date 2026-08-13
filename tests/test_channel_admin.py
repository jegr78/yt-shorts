import json
import os

import pytest

from yt_shorts import channel_admin
from yt_shorts.channel_admin import ChannelAdminError
from yt_shorts.lock import LOCK_NAME

FIELDS = {"id": "UCabc", "channel_url": "https://www.youtube.com/channel/UCabc",
          "handle": "@demo", "display_name": "Demo League", "language": "en",
          "footer": "DEMO | @demo"}


def _channels(tmp_path):
    d = tmp_path / "channels"
    d.mkdir()
    return d


class TestCreate:
    def test_creates_channel_json_and_scaffold(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        base = channels / "demo"
        assert json.loads((base / "channel.json").read_text())["display_name"] == "Demo League"
        assert (base / "brand.json").is_file()          # default brand scaffold
        assert (base / "fonts").is_dir()
        assert (base / "events").is_dir()

    def test_rejects_a_traversal_slug_and_nothing_escapes(self, tmp_path):
        channels = _channels(tmp_path)
        outside = tmp_path / "pwned"
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.create_channel(channels, "..", {**FIELDS})
        assert error.value.kind == "bad_name"
        assert not outside.exists()

    def test_existing_channel_is_a_conflict(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.create_channel(channels, "demo", FIELDS)
        assert error.value.kind == "exists"

    @pytest.mark.parametrize("missing", ["id", "handle", "display_name", "footer",
                                         "language", "channel_url"])
    def test_missing_required_field_is_bad_field(self, tmp_path, missing):
        channels = _channels(tmp_path)
        fields = {**FIELDS, missing: ""}
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.create_channel(channels, "demo", fields)
        assert error.value.kind == "bad_field"
        assert not (channels / "demo").exists()


class TestUpdate:
    def test_merges_fields_into_channel_json(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        channel_admin.update_channel(channels, "demo", {"display_name": "Renamed League"})
        data = json.loads((channels / "demo" / "channel.json").read_text())
        assert data["display_name"] == "Renamed League"
        assert data["handle"] == "@demo"               # untouched field kept

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = _channels(tmp_path)
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.update_channel(channels, "ghost", {"footer": "x"})
        assert error.value.kind == "not_found"

    def test_blanking_a_required_field_is_bad_field(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.update_channel(channels, "demo", {"footer": ""})
        assert error.value.kind == "bad_field"

    def test_a_corrupt_channel_json_is_a_clean_error_not_a_crash(self, tmp_path):
        # A broken channel.json must surface as a typed ChannelAdminError (→ 4xx),
        # not a raw JSONDecodeError (→ 500). Reachable only by a direct API call
        # (the UI hides Edit for a channel the listing already flags as broken).
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        (channels / "demo" / "channel.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.update_channel(channels, "demo", {"footer": "x"})
        assert error.value.kind == "bad_field"


class TestRename:
    def test_moves_the_directory_and_its_events(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        (channels / "demo" / "events" / "round-1").mkdir()
        channel_admin.rename_channel(channels, "demo", "demo2")
        assert not (channels / "demo").exists()
        assert (channels / "demo2" / "events" / "round-1").is_dir()

    def test_target_existing_is_a_conflict(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        channel_admin.create_channel(channels, "demo2", {**FIELDS, "id": "UCxyz"})
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.rename_channel(channels, "demo", "demo2")
        assert error.value.kind == "exists"

    def test_a_live_event_lock_blocks_rename(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        event = channels / "demo" / "events" / "round-1"
        event.mkdir()
        (event / LOCK_NAME).write_text(str(os.getpid()))
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.rename_channel(channels, "demo", "demo2")
        assert error.value.kind == "locked"
        assert (channels / "demo").exists()

    def test_successful_rename_leaves_no_orphan_lock_files(self, tmp_path):
        # rename acquires each event's lock (closing the TOCTOU vs a starting
        # render); those lock files must not be left behind in the moved dir.
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        (channels / "demo" / "events" / "round-1").mkdir()
        channel_admin.rename_channel(channels, "demo", "demo2")
        assert not (channels / "demo2" / "events" / "round-1" / LOCK_NAME).exists()


class TestDelete:
    def test_removes_the_channel_directory(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        channel_admin.delete_channel(channels, "demo")
        assert not (channels / "demo").exists()

    def test_a_live_event_lock_blocks_delete(self, tmp_path):
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        event = channels / "demo" / "events" / "round-1"
        event.mkdir()
        (event / LOCK_NAME).write_text(str(os.getpid()))
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.delete_channel(channels, "demo")
        assert error.value.kind == "locked"
        assert (channels / "demo").exists()

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = _channels(tmp_path)
        with pytest.raises(ChannelAdminError) as error:
            channel_admin.delete_channel(channels, "ghost")
        assert error.value.kind == "not_found"


class TestScaffoldColorsAreUnbranded:
    """A created channel must not ship looking like an existing one.

    DEFAULT_BRAND's colors were ERF's petrol/mint pair for most of this
    project's life, so every channel scaffolded from it inherited ERF's
    brand - four of the five channels in the operator's workspace ended up
    carrying it, which is the bug palette.py was written to fix. Pinning the
    INTENT (greyscale, i.e. no hue to be wrong about) rather than the exact
    hex values leaves the shades free to be tuned without a test edit, while
    still failing the moment someone drops a channel's colour back in here.
    """

    def _channel(self, value: str) -> tuple[int, int, int]:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))

    def test_accent_and_edge_carry_no_hue(self):
        for role in ("accent", "edge"):
            red, green, blue = self._channel(channel_admin.DEFAULT_BRAND["colors"][role])
            assert red == green == blue, (
                f"DEFAULT_BRAND's {role} has a hue - a scaffolded channel would "
                f"ship wearing it, and it is wrong for every channel but the one "
                f"it came from. Use 'Derive from logo' instead.")

    def test_the_scaffold_writes_those_colors(self, tmp_path):
        """The constant is only worth pinning if create_channel actually uses
        it - otherwise this class would pass while the scaffold on disk kept
        a hue of its own."""
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        written = json.loads((channels / "demo" / "brand.json").read_text(encoding="utf-8"))
        assert written["colors"] == channel_admin.DEFAULT_BRAND["colors"]

    def test_the_template_matches_the_embedded_default(self):
        """channel_admin embeds a copy of templates/example-channel/brand.json
        so it stays independent of the repo layout. Two copies drift; this is
        what notices."""
        import pathlib
        template = json.loads(
            (pathlib.Path(__file__).resolve().parent.parent
             / "templates/example-channel/brand.json").read_text(encoding="utf-8"))
        assert template["colors"] == channel_admin.DEFAULT_BRAND["colors"]


class TestTheWriteIsAtomic:
    def test_a_failed_update_leaves_the_previous_channel_json_complete(
            self, tmp_path, monkeypatch):
        """Writes through `atomicwrite`, so a reader can never find this file
    empty (see that module's docstring for the CI failure that measured
    the alternative). `os.replace` is the only step that can fail after
    the new bytes exist and before they are in place - failing anything
    earlier would pass under a truncating write too."""
        channels = _channels(tmp_path)
        channel_admin.create_channel(channels, "demo", FIELDS)
        path = channels / "demo" / "channel.json"
        before = path.read_bytes()

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            channel_admin.update_channel(channels, "demo", {"display_name": "Renamed"})

        assert path.read_bytes() == before
