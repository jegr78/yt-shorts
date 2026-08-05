"""Tests for yt_shorts.workspace_listing - the studio start screen's data.

Uses temp channel dirs (not the shared fixture) so the counts are seeded
here and assertions are tied to exactly what was written."""

from __future__ import annotations

import json

from yt_shorts import clipstore, editorial, workspace_listing


def _clip(event_dir, url, *, kept=False, rendered=False):
    directory = clipstore.write_clip(event_dir, {
        "url": url, "video_id": "vid", "hook": "H", "source_title": "S",
        "start": 0.0, "end": 12.0, "duration": 12.0, "error": None})
    if kept:
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.KEPT, transcript=None))
    if rendered:
        clipstore.short_path(directory).write_bytes(b"mp4")
    return directory


class TestListChannels:
    def test_lists_valid_and_broken_channels_with_event_counts(self, tmp_path):
        channels = tmp_path / "channels"
        # A valid channel with two event dirs.
        good = channels / "erf"
        (good / "events" / "round-1").mkdir(parents=True)
        (good / "events" / "round-2").mkdir(parents=True)
        (good / "channel.json").write_text(json.dumps({
            "display_name": "Endurance Racing Federation",
            "handle": "@ERFofficial"}), encoding="utf-8")
        # A channel whose channel.json is malformed.
        broken = channels / "clr"
        (broken / "events").mkdir(parents=True)
        (broken / "channel.json").write_text("{not json", encoding="utf-8")

        result = {c.name: c for c in workspace_listing.list_channels(channels)}
        assert set(result) == {"erf", "clr"}

        erf = result["erf"]
        assert erf.display_name == "Endurance Racing Federation"
        assert erf.handle == "@ERFofficial"
        assert erf.event_count == 2
        assert erf.error is None

        clr = result["clr"]
        assert clr.error is not None                 # broken channel is listed, not dropped
        assert clr.display_name == ""
        assert clr.event_count == 0

    def test_missing_channels_dir_is_empty(self, tmp_path):
        assert workspace_listing.list_channels(tmp_path / "nope") == []


class TestListEvents:
    def test_counts_clips_kept_and_rendered_per_event(self, tmp_path):
        channels = tmp_path / "channels"
        event_dir = channels / "erf" / "events" / "round-1"
        event_dir.mkdir(parents=True)
        _clip(event_dir, "https://youtube.com/watch/a/0-12")                     # candidate
        _clip(event_dir, "https://youtube.com/watch/b/0-12", kept=True)          # kept, not rendered
        _clip(event_dir, "https://youtube.com/watch/c/0-12", kept=True, rendered=True)

        events = workspace_listing.list_events(channels, "erf")
        assert len(events) == 1
        e = events[0]
        assert e.name == "round-1"
        assert e.clip_count == 3
        assert e.kept_count == 2
        assert e.rendered_count == 1

    def test_missing_events_dir_is_empty(self, tmp_path):
        assert workspace_listing.list_events(tmp_path / "channels", "erf") == []

    def test_traversal_channel_is_refused_not_enumerated(self, tmp_path):
        # A '../..' channel must not enumerate an events/-shaped tree outside
        # the channels dir.
        channels = tmp_path / "channels"
        outside = tmp_path / "elsewhere" / "events" / "evt"
        outside.mkdir(parents=True)
        assert workspace_listing.list_events(channels, "../../elsewhere") == []
