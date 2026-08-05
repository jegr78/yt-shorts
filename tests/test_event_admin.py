import os

import pytest

from yt_shorts import event_admin
from yt_shorts.event_admin import EventAdminError
from yt_shorts.lock import LOCK_NAME


def _channel(tmp_path, name="erf", events=()):
    channel_dir = tmp_path / "channels" / name
    (channel_dir / "events").mkdir(parents=True)
    for event in events:
        (channel_dir / "events" / event).mkdir()
    return tmp_path / "channels"


class TestValidateName:
    @pytest.mark.parametrize("good", ["race-1", "Round_2", "a", "a.b-c_d", "2026-07"])
    def test_accepts_a_safe_slug(self, good):
        event_admin.validate_name(good)  # must not raise

    @pytest.mark.parametrize("bad", ["", ".hidden", "..", "a/b", "/abs", "a b", "a" * 101,
                                     "-x", "round-1\n", "a\nb"])
    def test_rejects_unsafe_names(self, bad):
        with pytest.raises(EventAdminError) as error:
            event_admin.validate_name(bad)
        assert error.value.kind == "bad_name"


class TestChannelSegmentIsValidatedToo:
    """The channel segment becomes a path component just like the event name,
    so create/rename/delete must reject a traversal channel (e.g. '..') BEFORE
    touching the filesystem - otherwise a DELETE could rmtree outside channels/.
    """

    @pytest.mark.parametrize("op", ["create", "rename", "delete"])
    def test_traversal_channel_is_rejected_and_nothing_escapes(self, tmp_path, op):
        channels = tmp_path / "channels"
        channels.mkdir()
        # A sibling directory the '..' channel would let events/ reach into.
        outside = tmp_path / "events" / "victim"
        outside.mkdir(parents=True)
        (outside / "precious.txt").write_text("keep me")

        with pytest.raises(EventAdminError) as error:
            if op == "create":
                event_admin.create_event(channels, "..", "pwned")
            elif op == "rename":
                event_admin.rename_event(channels, "..", "victim", "moved")
            else:
                event_admin.delete_event(channels, "..", "victim")
        assert error.value.kind == "bad_name"
        assert (outside / "precious.txt").read_text() == "keep me"   # untouched
        assert not (tmp_path / "events" / "pwned").exists()
        assert not (tmp_path / "events" / "moved").exists()


class TestCreate:
    def test_creates_an_empty_event_directory(self, tmp_path):
        channels = _channel(tmp_path)
        event_admin.create_event(channels, "erf", "round-1")
        made = channels / "erf" / "events" / "round-1"
        assert made.is_dir()
        assert list(made.iterdir()) == []          # empty - no seeding

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = tmp_path / "channels"
        with pytest.raises(EventAdminError) as error:
            event_admin.create_event(channels, "nope", "round-1")
        assert error.value.kind == "not_found"

    def test_existing_event_is_a_conflict(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        with pytest.raises(EventAdminError) as error:
            event_admin.create_event(channels, "erf", "round-1")
        assert error.value.kind == "exists"

    def test_bad_name_is_rejected_before_touching_disk(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(EventAdminError) as error:
            event_admin.create_event(channels, "erf", "../escape")
        assert error.value.kind == "bad_name"
        assert not (channels.parent / "escape").exists()


class TestRename:
    def test_moves_the_directory_and_its_contents(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        (channels / "erf" / "events" / "round-1" / "marker").write_text("x")
        event_admin.rename_event(channels, "erf", "round-1", "round-2")
        assert not (channels / "erf" / "events" / "round-1").exists()
        assert (channels / "erf" / "events" / "round-2" / "marker").read_text() == "x"

    def test_unknown_event_is_not_found(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(EventAdminError) as error:
            event_admin.rename_event(channels, "erf", "ghost", "round-2")
        assert error.value.kind == "not_found"

    def test_target_existing_is_a_conflict(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1", "round-2"])
        with pytest.raises(EventAdminError) as error:
            event_admin.rename_event(channels, "erf", "round-1", "round-2")
        assert error.value.kind == "exists"

    def test_a_live_lock_blocks_the_rename(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        # A live lock = a lock file naming a running pid (this test process).
        (channels / "erf" / "events" / "round-1" / LOCK_NAME).write_text(str(os.getpid()))
        with pytest.raises(EventAdminError) as error:
            event_admin.rename_event(channels, "erf", "round-1", "round-2")
        assert error.value.kind == "locked"
        assert (channels / "erf" / "events" / "round-1").exists()    # untouched

    def test_rename_leaves_no_live_lock_behind(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        event_admin.rename_event(channels, "erf", "round-1", "round-2")
        # The lock this rename acquired must not survive into the renamed event,
        # or the next render would see a live-pid lock and refuse.
        assert not (channels / "erf" / "events" / "round-2" / LOCK_NAME).exists()


class TestDelete:
    def test_removes_the_event_directory(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        (channels / "erf" / "events" / "round-1" / "clips").mkdir()
        event_admin.delete_event(channels, "erf", "round-1")
        assert not (channels / "erf" / "events" / "round-1").exists()

    def test_unknown_event_is_not_found(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(EventAdminError) as error:
            event_admin.delete_event(channels, "erf", "ghost")
        assert error.value.kind == "not_found"

    def test_a_live_lock_blocks_the_delete(self, tmp_path):
        channels = _channel(tmp_path, events=["round-1"])
        (channels / "erf" / "events" / "round-1" / LOCK_NAME).write_text(str(os.getpid()))
        with pytest.raises(EventAdminError) as error:
            event_admin.delete_event(channels, "erf", "round-1")
        assert error.value.kind == "locked"
        assert (channels / "erf" / "events" / "round-1").exists()
