from pathlib import Path
from unittest.mock import patch

import os
import pytest

from yt_shorts.clipid import clip_id
from yt_shorts.clipstore import (
    ClipStoreError, clip_dir_by_name, clips_dir, iter_clip_dirs, raw_path,
    read_clip, short_path, subs_track_path, subs_work_dir, transcript_path,
    write_clip)

CLIP = "https://www.youtube.com/clip/UgkxSpeedy123"
OTHER = "https://www.youtube.com/clip/UgkxBarbie456"


def entry(url=CLIP, hook="Speedy!", error=None):
    return {"url": url, "hook": hook, "source_title": "ERF Round 3",
            "start": 10.0, "end": 70.0, "duration": 60.0, "error": error}


class TestWritingAClip:
    def test_the_directory_is_named_after_url_and_title(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        assert directory.name == f"speedy--{clip_id(CLIP)}"
        assert directory.parent == clips_dir(tmp_path)

    def test_the_derived_data_round_trips(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        assert read_clip(directory) == entry()

    def test_writing_twice_updates_rather_than_duplicates(self, tmp_path):
        write_clip(tmp_path, entry())
        write_clip(tmp_path, entry(hook="Speedy!!"))
        assert len(iter_clip_dirs(tmp_path)) == 1

    def test_a_retitled_clip_keeps_its_original_directory(self, tmp_path):
        first = write_clip(tmp_path, entry(hook="Speedy!"))
        second = write_clip(tmp_path, entry(hook="Abschied von Speedy"))
        assert second == first

    def test_an_entry_without_a_url_is_refused(self, tmp_path):
        broken = entry()
        del broken["url"]
        with pytest.raises(ClipStoreError):
            write_clip(tmp_path, broken)


class TestReading:
    def test_an_unreadable_clip_file_is_reported(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        (directory / "clip.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ClipStoreError):
            read_clip(directory)

    def test_clips_are_listed_in_a_stable_order(self, tmp_path):
        write_clip(tmp_path, entry(url=OTHER, hook="Barbie"))
        write_clip(tmp_path, entry())
        names = [d.name for d in iter_clip_dirs(tmp_path)]
        assert names == sorted(names)

    def test_an_event_without_clips_lists_nothing(self, tmp_path):
        assert iter_clip_dirs(tmp_path) == []


class TestPaths:
    def test_every_file_of_a_clip_lives_in_its_directory(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        for path in (transcript_path(directory), raw_path(directory),
                     short_path(directory), subs_track_path(directory),
                     subs_work_dir(directory)):
            assert path.parent == directory

    def test_the_filenames_are_the_documented_ones(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        assert transcript_path(directory).name == "transcript.json"
        assert raw_path(directory).name == "raw.mp4"
        assert short_path(directory).name == "short.mp4"


class TestExistingDirVerification:
    """Verify _existing_dir checks clip.json contents, not just name (F1+F2)."""

    def test_empty_directory_with_matching_name_is_not_adopted(self, tmp_path):
        """Empty directory with matching name should NOT be adopted."""
        identity = clip_id(CLIP)

        # Create an unrelated empty directory with matching name
        empty_dir = clips_dir(tmp_path) / f"aaa-empty--{identity}"
        empty_dir.mkdir(parents=True, exist_ok=True)
        assert not (empty_dir / "clip.json").exists()

        # Write a clip - should NOT use the empty directory
        directory = write_clip(tmp_path, entry())

        # The returned directory should have clip.json
        assert (directory / "clip.json").exists()
        data = read_clip(directory)
        assert data["url"] == CLIP

        # Empty dir should remain empty
        assert not (empty_dir / "clip.json").exists()

        # iter_clip_dirs should list only the real clip
        clip_dirs = iter_clip_dirs(tmp_path)
        assert len(clip_dirs) == 1
        assert clip_dirs[0].name == f"speedy--{identity}"

    def test_identity_collision_raises_error(self, tmp_path):
        """Identity collision (two URLs with same ID) raises ClipStoreError."""
        # Write first clip
        write_clip(tmp_path, entry(url=CLIP))

        # Force collision by monkeypatching clip_id
        original_clip_id = clip_id

        def collision_clip_id(url):
            return original_clip_id(CLIP)

        with patch('yt_shorts.clipstore.clip_id', side_effect=collision_clip_id):
            # Try to write a different clip - should raise ClipStoreError
            with pytest.raises(ClipStoreError) as exc_info:
                write_clip(tmp_path, entry(url=OTHER, hook="Barbie"))

            error_msg = str(exc_info.value)
            assert "collision" in error_msg.lower() or "different" in error_msg.lower()

    def test_matching_url_in_existing_directory_is_accepted(self, tmp_path):
        """Existing directory with matching URL should be reused."""
        dir1 = write_clip(tmp_path, entry())
        dir2 = write_clip(tmp_path, entry(hook="Speedy!!"))

        assert dir1 == dir2
        assert len(iter_clip_dirs(tmp_path)) == 1
        data = read_clip(dir2)
        assert data["hook"] == "Speedy!!"

    def test_a_share_parameter_is_recognised_as_the_same_clip_not_a_collision(self, tmp_path):
        """I2: clip_id() deliberately canonicalises away a query string (a
        YouTube share parameter), so clip_id(u) == clip_id(u + "?si=...").
        _existing_dir used to compare the RAW url strings when verifying a
        name match, which reported this exact case - the same clip, just
        addressed with a share parameter - as an "Identity collision"
        against itself. This is the real-world trigger the existing
        collision test (above) only fakes by patching clip_id."""
        dir1 = write_clip(tmp_path, entry(url=CLIP))
        dir2 = write_clip(tmp_path, entry(url=f"{CLIP}?si=share123", hook="Speedy!!"))

        assert dir1 == dir2
        assert len(iter_clip_dirs(tmp_path)) == 1
        assert read_clip(dir2)["hook"] == "Speedy!!"


class TestValueErrorHandling:
    """Verify ValueError from clip_id is caught and re-raised as ClipStoreError (F3)."""

    def test_whitespace_only_url_raises_clipstore_error(self, tmp_path):
        """Whitespace-only URL raises ClipStoreError, not ValueError."""
        with pytest.raises(ClipStoreError):
            write_clip(tmp_path, entry(url="   "))

    def test_query_only_url_raises_clipstore_error(self, tmp_path):
        """Query-only URL raises ClipStoreError, not ValueError."""
        with pytest.raises(ClipStoreError):
            write_clip(tmp_path, entry(url="?share=1"))

    def test_fragment_only_url_raises_clipstore_error(self, tmp_path):
        """Fragment-only URL raises ClipStoreError, not ValueError."""
        with pytest.raises(ClipStoreError):
            write_clip(tmp_path, entry(url="#frag"))


class TestIterClipDirsFilter:
    """Verify iter_clip_dirs filters out directories without clip.json (F4)."""

    def test_directory_without_clip_json_is_not_listed(self, tmp_path):
        """Directory without clip.json should not appear in iter_clip_dirs."""
        write_clip(tmp_path, entry())

        # Create a stray directory without clip.json
        stray_dir = clips_dir(tmp_path) / "stray-directory"
        stray_dir.mkdir(parents=True, exist_ok=True)

        # iter_clip_dirs should only return the valid clip
        clip_dirs = iter_clip_dirs(tmp_path)
        assert len(clip_dirs) == 1
        assert clip_dirs[0].name.startswith("speedy--")

        # Verify stray dir exists but is not listed
        assert stray_dir.exists()
        assert stray_dir not in clip_dirs


class TestClipDirByName:
    """`clip_dir_by_name` is the one place a caller-supplied clip NAME becomes
    a path, and the one place it is validated (see its own docstring for what
    the unvalidated join let through - a render writing its short outside the
    event, and a queued trim re-encoding a short.mp4 anywhere on disk)."""

    def test_an_ordinary_clip_directory_name_resolves_under_clips(self, tmp_path):
        # The control for the refusals below: the names real clip directories
        # actually carry (clipid.directory_name's '<slug>--<id>') must pass.
        directory = write_clip(tmp_path, entry())
        assert clip_dir_by_name(tmp_path, directory.name) == directory

    def test_a_bare_identity_without_a_slug_resolves_too(self, tmp_path):
        # clipid.directory_name drops the '--' when a title yields no slug,
        # so an id on its own is a legitimate directory name.
        assert clip_dir_by_name(tmp_path, "1a2b3c4d") == clips_dir(tmp_path) / "1a2b3c4d"

    @pytest.mark.parametrize("name", [
        "..", "../../etc", "a/b", ".hidden", "", "  ", "with space",
    ])
    def test_a_name_that_is_not_one_safe_segment_is_refused(self, tmp_path, name):
        with pytest.raises(ValueError):
            clip_dir_by_name(tmp_path, name)

    @pytest.mark.parametrize("name", [None, 12345, ["a"], Path("a")])
    def test_a_non_string_name_is_a_value_error_not_a_type_error(self, tmp_path, name):
        # Same promise pathnames.validate_segment makes everywhere else: every
        # unusable segment leaves as ValueError, so a caller's `except
        # ValueError` is true rather than nearly true.
        with pytest.raises(ValueError):
            clip_dir_by_name(tmp_path, name)

    def test_it_refuses_before_touching_the_filesystem(self, tmp_path):
        # The guard is worth nothing if it runs after the path has been used.
        # There is no clips/ dir here at all, and a refusal must not create one.
        with pytest.raises(ValueError):
            clip_dir_by_name(tmp_path, "../../../OUTSIDE")
        assert not clips_dir(tmp_path).exists()


class TestTheWriteIsAtomic:
    def test_a_failed_write_leaves_the_previous_clip_json_complete(self, tmp_path, monkeypatch):
        """Writes through `atomicwrite`, so a reader can never find this file
    empty (see that module's docstring for the CI failure that measured
    the alternative). `os.replace` is the only step that can fail after
    the new bytes exist and before they are in place - failing anything
    earlier would pass under a truncating write too."""
        directory = write_clip(tmp_path, entry())
        before = (directory / "clip.json").read_bytes()

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            write_clip(tmp_path, entry(hook="Speedy again!"))

        assert (directory / "clip.json").read_bytes() == before
