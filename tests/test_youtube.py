import json

import pytest

from yt_shorts.youtube import (
    Stream, YouTubeError, channel_catalogue, list_playlist_videos,
    list_playlists, list_streams,
)

CHANNEL = "https://www.youtube.com/channel/UCb3S2oA7lANdg5IS0QtF46w"

# Recorded from a real `yt-dlp --flat-playlist --dump-json .../streams` run,
# trimmed to the fields we read. Newest first, as the /streams tab returns them.
LINES = "\n".join(json.dumps(d) for d in [
    {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
     "duration": 28431, "view_count": 1800, "live_status": "was_live"},
    {"id": "2O_lQrxEHWo", "title": "ERF 24h Nürburgring 2026 | Part 2",
     "duration": 29478, "view_count": 1300, "live_status": "was_live"},
    {"id": "Esm9vv5-PdU", "title": "ERF 24h Nürburgring 2026 | Part 1",
     "duration": 29975, "view_count": 2200, "live_status": "was_live"},
])


def runner_returning(text):
    def run(args):
        return text
    return run


class TestListStreams:
    def test_url_follows_a_double_dash_terminator(self):
        seen = {}
        def capture(args):
            seen["args"] = args
            return ""
        list_streams(CHANNEL, runner=capture)
        url_arg = f"{CHANNEL}/streams"
        assert seen["args"][seen["args"].index(url_arg) - 1] == "--"

    @pytest.mark.parametrize("bad", ["--exec=x", "-rf", "file:///etc", "javascript:1"])
    def test_non_http_channel_url_is_rejected_before_running(self, bad):
        def boom(args):  # must never be called
            raise AssertionError("runner should not run for a bad URL")
        with pytest.raises(ValueError, match="http"):
            list_streams(bad, runner=boom)

    def test_fields_are_extracted(self):
        streams = list_streams(CHANNEL, runner=runner_returning(LINES))
        assert streams[0] == Stream(
            video_id="xQlD7MkC-Eo",
            title="ERF 24h Nürburgring 2026 | Part 3",
            duration_seconds=28431, view_count=1800)

    def test_order_is_preserved(self):
        streams = list_streams(CHANNEL, runner=runner_returning(LINES))
        assert [s.video_id for s in streams] == [
            "xQlD7MkC-Eo", "2O_lQrxEHWo", "Esm9vv5-PdU"]

    def test_the_runner_is_asked_for_the_streams_tab(self):
        seen = {}

        def run(args):
            seen["args"] = args
            return LINES

        list_streams(CHANNEL, runner=run)
        assert seen["args"][-1] == f"{CHANNEL}/streams"
        assert "--flat-playlist" in seen["args"]
        assert "--dump-json" in seen["args"]

    def test_a_malformed_line_is_skipped_not_fatal(self):
        text = LINES + "\n{not json\n"
        streams = list_streams(CHANNEL, runner=runner_returning(text))
        assert len(streams) == 3

    def test_a_missing_duration_or_view_count_is_tolerated(self):
        text = json.dumps({"id": "x", "title": "T"})
        streams = list_streams(CHANNEL, runner=runner_returning(text))
        assert streams[0] == Stream("x", "T", None, None)

    def test_an_entry_without_an_id_is_skipped(self):
        text = json.dumps({"title": "no id"})
        assert list_streams(CHANNEL, runner=runner_returning(text)) == []

    def test_blank_lines_are_ignored(self):
        text = "\n\n" + LINES + "\n\n"
        assert len(list_streams(CHANNEL, runner=runner_returning(text))) == 3

    def test_empty_output_is_an_empty_list_not_an_error(self):
        assert list_streams(CHANNEL, runner=runner_returning("")) == []


class TestErrors:
    def test_a_runner_failure_becomes_a_youtube_error(self):
        def run(args):
            raise RuntimeError("yt-dlp: channel not found")
        with pytest.raises(YouTubeError) as error:
            list_streams(CHANNEL, runner=run)
        assert "channel not found" in str(error.value)


# Recorded from a real `yt-dlp --flat-playlist --dump-json .../playlists`
# run on 2026-08-04, trimmed to the fields we read. `playlist_count` is
# deliberately included and deliberately ignored: on the /playlists tab it
# is the number of PLAYLISTS (identical on every row), not the size of that
# playlist - see list_playlists' own docstring.
PLAYLIST_LINES = "\n".join(json.dumps(d) for d in [
    {"_type": "url", "id": "PLaaa", "title": "2026 Nürburgring 24 Hour",
     "playlist_count": 2},
    {"_type": "url", "id": "PLbbb", "title": "Bathurst 12 Hour 2025",
     "playlist_count": 2},
])

# One playlist's members. The third entry is a deleted or private video -
# yt-dlp reports it with a null title and no duration, and two such entries
# really do sit in ERF's playlists.
MEMBER_LINES = "\n".join(json.dumps(d) for d in [
    {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
     "duration": 28431, "view_count": 1800},
    {"id": "newvid0001", "title": "ERF Special Catalunya 6H Part 2",
     "duration": 8983, "view_count": 400},
    {"id": "goneforever", "title": None, "duration": None, "view_count": None},
])


def runner_for(mapping, default=""):
    """A runner that answers by which URL it is asked for.

    Called from several THREADS by channel_catalogue, so it records into a
    list (append is atomic under the GIL) and the tests compare the record
    as a SET - the order threads finish in is not a property worth pinning.
    """
    calls = []

    def run(args):
        calls.append(args[-1])
        for fragment, text in mapping.items():
            if fragment in args[-1]:
                return text
        return default

    run.calls = calls
    return run


class TestListPlaylists:
    def test_the_playlists_tab_is_asked_for(self):
        runner = runner_for({"/playlists": PLAYLIST_LINES})
        list_playlists(CHANNEL, runner=runner)
        assert runner.calls == [f"{CHANNEL}/playlists"]

    def test_id_and_title_are_read(self):
        runner = runner_for({"/playlists": PLAYLIST_LINES})
        playlists = list_playlists(CHANNEL, runner=runner)
        assert [(p.id, p.title) for p in playlists] == [
            ("PLaaa", "2026 Nürburgring 24 Hour"),
            ("PLbbb", "Bathurst 12 Hour 2025")]

    def test_the_sizes_are_not_taken_from_the_tab(self):
        """yt-dlp's `playlist_count` on this tab is the number of playlists,
        identical on every row - reading it as the playlist's own size is
        the mistake this pins shut. channel_catalogue fills these in."""
        runner = runner_for({"/playlists": PLAYLIST_LINES})
        playlists = list_playlists(CHANNEL, runner=runner)
        assert [(p.count, p.unavailable) for p in playlists] == [(0, 0), (0, 0)]

    def test_a_failure_is_a_youtube_error(self):
        def boom(args):
            raise RuntimeError("yt-dlp is not installed")
        with pytest.raises(YouTubeError, match="yt-dlp is not installed"):
            list_playlists(CHANNEL, runner=boom)


class TestListPlaylistVideos:
    def test_members_are_read_with_no_playlist_ids_yet(self):
        """The `playlist_ids` field has exactly ONE owner - the catalogue -
        so this returns it empty rather than half-filling it."""
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(MEMBER_LINES))
        assert [v.video_id for v in contents.videos] == [
            "xQlD7MkC-Eo", "newvid0001"]
        assert all(v.playlist_ids == [] for v in contents.videos)

    def test_a_titleless_entry_is_dropped_and_counted(self):
        """A deleted or private video. Dropped, because it can never be
        transcribed - and COUNTED, so a "(2)" in the dropdown is not
        silently a 2 that came from 3."""
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(MEMBER_LINES))
        assert len(contents.videos) == 2
        assert contents.unavailable == 1

    # The non-strings sit in the SAME list as the unsafe strings on purpose:
    # they are one class of bad id and must get one class of refusal. The
    # falsy ones (None, "") were already ValueError via the old `or ""`; a
    # truthy non-string reached re.match and left as a TypeError instead -
    # the same "one defect, two answers" `pathnames.validate_segment` had.
    @pytest.mark.parametrize("bad", ["PL&list=other", "../etc", "PL id", "",
                                     None, 5, ["PLaaa"], b"PLaaa", 1.5])
    def test_an_unsafe_playlist_id_never_reaches_the_runner(self, bad):
        def boom(args):
            raise AssertionError("runner should not run for a bad id")
        with pytest.raises(ValueError, match="playlist id"):
            list_playlist_videos(bad, runner=boom)


class TestChannelCatalogue:
    def _runner(self):
        return runner_for({
            "/streams": LINES,
            "/playlists": PLAYLIST_LINES,
            "list=PLaaa": MEMBER_LINES,
            "list=PLbbb": "",
        })

    def test_every_playlist_is_fetched(self):
        runner = self._runner()
        channel_catalogue(CHANNEL, runner=runner)
        assert set(runner.calls) == {
            f"{CHANNEL}/streams",
            f"{CHANNEL}/playlists",
            "https://www.youtube.com/playlist?list=PLaaa",
            "https://www.youtube.com/playlist?list=PLbbb",
        }

    def test_the_video_list_is_the_union_streams_first(self):
        """A playlist may hold a broadcast the Streams tab does not list -
        measured: 8 such videos on ERF, two of them multi-hour races that
        were unreachable from the studio before this. They are APPENDED, so
        the order the Streams tab already had is untouched."""
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        assert [v.video_id for v in catalogue.videos] == [
            "xQlD7MkC-Eo", "2O_lQrxEHWo", "Esm9vv5-PdU", "newvid0001"]

    def test_membership_is_recorded_on_the_video(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        by_id = {v.video_id: v for v in catalogue.videos}
        assert by_id["xQlD7MkC-Eo"].playlist_ids == ["PLaaa"]
        assert by_id["newvid0001"].playlist_ids == ["PLaaa"]
        assert by_id["2O_lQrxEHWo"].playlist_ids == []

    def test_playlist_sizes_come_from_the_members(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        sizes = {p.id: (p.count, p.unavailable) for p in catalogue.playlists}
        assert sizes == {"PLaaa": (2, 1), "PLbbb": (0, 0)}

    def test_one_failing_playlist_does_not_sink_the_catalogue(self):
        """The same per-entry tolerance list_streams already applies to a
        malformed line. A half catalogue that looks whole is the failure
        mode this project keeps paying for, so the loss is REPORTED."""
        def run(args):
            url = args[-1]
            if "list=PLbbb" in url:
                raise RuntimeError("HTTP Error 404: Not Found")
            if "/streams" in url:
                return LINES
            if "/playlists" in url:
                return PLAYLIST_LINES
            return MEMBER_LINES

        catalogue = channel_catalogue(CHANNEL, runner=run)
        assert [p.id for p in catalogue.playlists] == ["PLaaa"]
        assert len(catalogue.failed_playlists) == 1
        assert catalogue.failed_playlists[0].title == "Bathurst 12 Hour 2025"
        assert "404" in catalogue.failed_playlists[0].reason

    def test_a_failing_playlist_tab_still_serves_the_streams(self):
        """A channel with no playlists tab, or a failure fetching it, must
        leave the list exactly as useful as it was before this feature."""
        def run(args):
            if "/playlists" in args[-1]:
                raise RuntimeError("HTTP Error 404: Not Found")
            return LINES

        catalogue = channel_catalogue(CHANNEL, runner=run)
        assert [v.video_id for v in catalogue.videos] == [
            "xQlD7MkC-Eo", "2O_lQrxEHWo", "Esm9vv5-PdU"]
        assert catalogue.playlists == []
        assert len(catalogue.failed_playlists) == 1

    def test_a_failing_streams_tab_still_raises(self):
        """The one failure that is NOT tolerated: without the Streams tab
        there is no list at all, and the route turns this into a 502."""
        def run(args):
            raise RuntimeError("yt-dlp is not installed")
        with pytest.raises(YouTubeError):
            channel_catalogue(CHANNEL, runner=run)


# The same video listed twice inside ONE playlist. yt-dlp does not
# ordinarily produce this - YouTube rejects a duplicate in a playlist - but
# the parser must not turn it into a size of 2.
DUPLICATE_MEMBER_LINES = "\n".join(json.dumps(d) for d in [
    {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
     "duration": 28431, "view_count": 1800},
    {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
     "duration": 28431, "view_count": 1800},
])

# A playlists tab carrying an id that could alter the URL it is
# interpolated into. Never observed from YouTube; this pins what happens if
# it ever is.
HOSTILE_PLAYLIST_LINES = "\n".join(json.dumps(d) for d in [
    {"_type": "url", "id": "PLaaa", "title": "2026 Nürburgring 24 Hour"},
    {"_type": "url", "id": "PL&list=somebody-elses", "title": "Hostile"},
])


class TestAVideoInSeveralPlaylists:
    """`playlist_ids` is a LIST, and this is the property it is a list FOR.

    No ERF video sits in two playlists today - that is an observation of one
    channel on one day, recorded as such in `channel_catalogue`'s docstring,
    and exactly the kind of observation a later change would quietly encode
    as a guarantee. This pins the general case instead.
    """

    def _runner(self):
        return runner_for({
            "/streams": LINES,
            "/playlists": PLAYLIST_LINES,
            # Both playlists hold the SAME video.
            "list=PLaaa": MEMBER_LINES,
            "list=PLbbb": MEMBER_LINES,
        })

    def test_both_playlists_are_recorded_on_the_one_video(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        by_id = {v.video_id: v for v in catalogue.videos}
        assert by_id["xQlD7MkC-Eo"].playlist_ids == ["PLaaa", "PLbbb"]

    def test_the_video_appears_once_in_the_union(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        ids = [v.video_id for v in catalogue.videos]
        assert ids.count("xQlD7MkC-Eo") == 1

    def test_a_playlist_only_video_in_two_playlists_is_also_recorded_once(self):
        # newvid0001 is NOT in the Streams tab, so it enters the union from a
        # playlist fetch - the branch that CREATES the Video rather than
        # finding it. Both memberships must still land on the one object.
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        by_id = {v.video_id: v for v in catalogue.videos}
        assert by_id["newvid0001"].playlist_ids == ["PLaaa", "PLbbb"]
        assert [v.video_id for v in catalogue.videos].count("newvid0001") == 1


class TestADuplicateInsideOnePlaylist:
    def test_it_is_collapsed_rather_than_counted_twice(self):
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(DUPLICATE_MEMBER_LINES))
        assert [v.video_id for v in contents.videos] == ["xQlD7MkC-Eo"]

    def test_it_is_not_reported_as_unavailable(self):
        """A duplicate is NOT a loss and must not be counted as one.

        `unavailable` says "this playlist holds a video you cannot have" -
        a deleted or private entry. Collapsing a repeat of a video that IS
        in the list loses nothing an operator could act on, so counting it
        there would make the dropdown claim a loss that did not happen.
        """
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(DUPLICATE_MEMBER_LINES))
        assert contents.unavailable == 0

    def test_the_playlist_size_matches_what_is_offered(self):
        runner = runner_for({
            "/streams": "", "/playlists": PLAYLIST_LINES,
            "list=PLaaa": DUPLICATE_MEMBER_LINES, "list=PLbbb": "",
        })
        catalogue = channel_catalogue(CHANNEL, runner=runner)
        sizes = {p.id: p.count for p in catalogue.playlists}
        assert sizes["PLaaa"] == 1


class TestAnIdWithMixedTitledness:
    """One id, several occurrences, some titled and some not - the corner
    the plain duplicate and the plain titleless cases do not cover between
    them. The rule: group by id, and the BEST occurrence wins - a title
    anywhere makes it available, and only an id titleless everywhere is a
    reported loss, counted once.
    """

    def test_two_titleless_lines_with_the_same_id_count_once(self):
        lines = "\n".join(json.dumps(d) for d in [
            {"id": "goneforever", "title": None,
             "duration": None, "view_count": None},
            {"id": "goneforever", "title": None,
             "duration": None, "view_count": None},
        ])
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(lines))
        assert contents.videos == []
        assert contents.unavailable == 1

    def test_titleless_then_titled_is_offered_and_not_unavailable(self):
        lines = "\n".join(json.dumps(d) for d in [
            {"id": "xQlD7MkC-Eo", "title": None,
             "duration": None, "view_count": None},
            {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
             "duration": 28431, "view_count": 1800},
        ])
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(lines))
        assert [v.video_id for v in contents.videos] == ["xQlD7MkC-Eo"]
        assert contents.unavailable == 0

    def test_titled_then_titleless_is_offered_and_not_unavailable(self):
        lines = "\n".join(json.dumps(d) for d in [
            {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
             "duration": 28431, "view_count": 1800},
            {"id": "xQlD7MkC-Eo", "title": None,
             "duration": None, "view_count": None},
        ])
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(lines))
        assert [v.video_id for v in contents.videos] == ["xQlD7MkC-Eo"]
        assert contents.unavailable == 0

    def test_two_different_titleless_ids_are_not_over_collapsed(self):
        lines = "\n".join(json.dumps(d) for d in [
            {"id": "goneforever", "title": None,
             "duration": None, "view_count": None},
            {"id": "alsogoneforever", "title": None,
             "duration": None, "view_count": None},
        ])
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(lines))
        assert contents.videos == []
        assert contents.unavailable == 2


class TestAnUnusablePlaylistIdFromTheTab:
    """The id `list_playlists` reads is not validated there, and this pins
    what makes that safe rather than leaving it an accident of call order.

    `list_playlist_videos` validates, `channel_catalogue` catches, and the
    playlist is REPORTED as failed - so a hostile id never reaches a URL and
    never silently shrinks the catalogue either.
    """

    def _runner(self):
        return runner_for({
            "/streams": LINES,
            "/playlists": HOSTILE_PLAYLIST_LINES,
            "list=PLaaa": MEMBER_LINES,
        })

    def test_no_url_is_ever_built_from_it(self):
        runner = self._runner()
        channel_catalogue(CHANNEL, runner=runner)
        assert not any("somebody-elses" in url for url in runner.calls)

    def test_it_is_reported_as_a_failed_playlist_not_dropped(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        assert [p.id for p in catalogue.playlists] == ["PLaaa"]
        assert [f.title for f in catalogue.failed_playlists] == ["Hostile"]

    def test_the_rest_of_the_catalogue_is_served(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        assert [v.video_id for v in catalogue.videos] == [
            "xQlD7MkC-Eo", "2O_lQrxEHWo", "Esm9vv5-PdU", "newvid0001"]
