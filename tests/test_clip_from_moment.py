import json

import pytest

from yt_shorts import clip_from_moment


class TestMomentUrl:
    def test_identity_lives_in_the_path_not_the_query(self):
        # clipid.canonical_url STRIPS the query string, so a "?..." identity
        # would collapse every moment in a stream onto one clip.
        url = clip_from_moment.moment_url("vid123", 61.4, 89.6)
        assert "?" not in url
        assert url.endswith("/vid123/61-90")

    def test_two_windows_are_two_urls(self):
        assert (clip_from_moment.moment_url("v", 10, 20)
                != clip_from_moment.moment_url("v", 30, 40))

    def test_the_same_window_is_the_same_url(self):
        assert (clip_from_moment.moment_url("v", 10.2, 20.4)
                == clip_from_moment.moment_url("v", 10.1, 20.3))


class TestCreateClip:
    def test_writes_one_clip_directory_with_the_chosen_window(self, tmp_path):
        directory = clip_from_moment.create_clip(
            tmp_path, video_id="vid123", start=61.4, end=89.6,
            hook="INTO THE BARRIER", source_title="N24 Race Part 1")
        entry = json.loads((directory / "clip.json").read_text())
        assert entry["start"] == 61.4 and entry["end"] == 89.6
        assert entry["hook"] == "INTO THE BARRIER"
        assert entry["duration"] == pytest.approx(28.2)

    def test_the_same_window_twice_is_one_directory(self, tmp_path):
        first = clip_from_moment.create_clip(tmp_path, video_id="v", start=10, end=20,
                                             hook="A", source_title="T")
        second = clip_from_moment.create_clip(tmp_path, video_id="v", start=10, end=20,
                                              hook="B", source_title="T")
        assert first == second
        # Re-picking the same moment is idempotent: it succeeds and updates
        # the one directory's data (e.g. a corrected hook) rather than
        # erroring or creating a second directory.
        entry = json.loads((second / "clip.json").read_text())
        assert entry["start"] == 10 and entry["end"] == 20
        assert entry["hook"] == "B"

    def test_an_inverted_window_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            clip_from_moment.create_clip(tmp_path, video_id="v", start=30, end=10,
                                         hook="", source_title="T")

    def test_a_colliding_but_different_window_is_refused(self, tmp_path):
        clip_from_moment.create_clip(tmp_path, video_id="v", start=10.1, end=20.3,
                                     hook="first moment", source_title="T")
        with pytest.raises(clip_from_moment.ClipIdentityCollision) as excinfo:
            clip_from_moment.create_clip(tmp_path, video_id="v", start=10.2, end=20.4,
                                         hook="second moment", source_title="T")
        message = str(excinfo.value)
        assert "v" in message
        assert "10.1" in message and "20.3" in message
        assert "10.2" in message and "20.4" in message

    def test_a_refused_collision_leaves_the_first_window_and_its_sidecars_untouched(self, tmp_path):
        # This is the actual data-integrity property: a refused second write
        # must not leave clip.json, edit.json and transcript.json silently
        # disagreeing about which moment the directory describes. It would
        # fail if the collision guard were ever removed or bypassed.
        directory = clip_from_moment.create_clip(
            tmp_path, video_id="v", start=10.1, end=20.3,
            hook="first moment", source_title="T")

        edit_payload = {"title": "A human's correction"}
        (directory / "edit.json").write_text(json.dumps(edit_payload))
        transcript_payload = {"words": [{"start": 10.1, "end": 10.5, "text": " Hi"}]}
        (directory / "transcript.json").write_text(json.dumps(transcript_payload))

        with pytest.raises(clip_from_moment.ClipIdentityCollision):
            clip_from_moment.create_clip(
                tmp_path, video_id="v", start=10.2, end=20.4,
                hook="second moment", source_title="T")

        entry = json.loads((directory / "clip.json").read_text())
        assert entry["start"] == 10.1 and entry["end"] == 20.3
        assert entry["hook"] == "first moment"
        assert json.loads((directory / "edit.json").read_text()) == edit_payload
        assert json.loads((directory / "transcript.json").read_text()) == transcript_payload

    def test_a_corrupted_neighbour_is_refused_not_silently_duplicated(self, tmp_path):
        # Reproduces the hole a reviewer found: _existing_window used to treat
        # an unreadable clip.json the same as "no neighbour", so create_clip
        # fell through to clipstore.write_clip - which independently can't
        # read the same corrupted file either, and so mints a SECOND,
        # differently-named directory sharing the identical identity suffix.
        # Seed a clip, then corrupt its clip.json in place.
        directory = clip_from_moment.create_clip(
            tmp_path, video_id="v", start=10.1, end=20.3,
            hook="first moment", source_title="T")
        edit_payload = {"title": "A human's correction"}
        (directory / "edit.json").write_text(json.dumps(edit_payload))
        (directory / "clip.json").write_text("{not valid json")

        with pytest.raises(clip_from_moment.ClipIdentityUnreadable) as excinfo:
            clip_from_moment.create_clip(
                tmp_path, video_id="v", start=10.2, end=20.4,
                hook="second moment", source_title="T")

        # The message must name the offending directory so the operator knows
        # exactly where to look - a distinct message from the ordinary
        # collision, because the remedy is different (inspect/remove the
        # directory, not "pick a different window").
        message = str(excinfo.value)
        assert str(directory) in message
        assert "cannot be read" in message

        # The load-bearing assertion: refusing must not have left a second
        # directory behind. Exactly one directory may carry this identity -
        # that is the whole invariant, and it is what a revert of this fix
        # would break.
        from yt_shorts import clipstore
        identity = clip_from_moment.clip_id(
            clip_from_moment.moment_url("v", 10.2, 20.4))
        matching = [d for d in clipstore.iter_clip_dirs(tmp_path)
                    if d.name == identity or d.name.endswith(f"--{identity}")]
        assert len(matching) == 1
        assert matching[0] == directory

        # The seeded edit.json beside the corrupted clip.json must be
        # untouched - the refusal must not have written anything either.
        assert json.loads((directory / "edit.json").read_text()) == edit_payload
        assert (directory / "clip.json").read_text() == "{not valid json"

    def test_a_neighbour_missing_start_or_end_is_refused_not_a_bare_typeerror(self, tmp_path):
        # MINOR-3: a clip.json that parses as valid JSON but lacks start/end
        # used to make _existing_window return (None, None), and
        # create_clip's `_close(None, 10.0)` then raised a bare TypeError
        # ("unsupported operand type(s) for -: 'NoneType' and 'float'") -
        # bypassing both typed errors this module exists to raise. Treat it
        # the same as unparseable JSON: ClipIdentityUnreadable.
        directory = clip_from_moment.create_clip(
            tmp_path, video_id="v", start=10.1, end=20.3,
            hook="first moment", source_title="T")
        data = json.loads((directory / "clip.json").read_text())
        del data["start"]
        del data["end"]
        (directory / "clip.json").write_text(json.dumps(data))

        with pytest.raises(clip_from_moment.ClipIdentityUnreadable) as excinfo:
            clip_from_moment.create_clip(
                tmp_path, video_id="v", start=10.2, end=20.4,
                hook="second moment", source_title="T")
        assert str(directory) in str(excinfo.value)
