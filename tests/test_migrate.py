import json

import pytest

from yt_shorts import clipstore
from yt_shorts.migrate import (
    MigrationError, migrate_event, sync_channel_file, sync_channel_tree)

CLIP_A = "https://www.youtube.com/clip/AAA"
CLIP_B = "https://www.youtube.com/clip/BBB"


def old_event(tmp_path):
    old = tmp_path / "old"
    (old / "drafts").mkdir(parents=True)
    (old / "transcripts").mkdir()
    (old / "clip_urls.json").write_text(json.dumps(
        [{"url": CLIP_A, "hook": "Speedy!"}]), encoding="utf-8")
    (old / "clips.json").write_text(json.dumps([
        {"url": CLIP_A, "hook": "Speedy!", "source_title": "ERF",
         "start": 1.0, "end": 2.0, "duration": 1.0, "error": None},
        {"url": CLIP_B, "hook": "Barbie", "source_title": "ERF",
         "start": 3.0, "end": 4.0, "duration": 1.0, "error": None},
    ]), encoding="utf-8")
    (old / "drafts" / "speedy.mp4").write_bytes(b"speedy-short")
    (old / "drafts" / "barbie.mp4").write_bytes(b"barbie-short")
    (old / "transcripts" / "speedy.json").write_text(json.dumps(
        {"source": CLIP_A, "words": [{"start": 0.0, "end": 0.5, "text": " hi"}]}),
        encoding="utf-8")
    return old


class TestMigration:
    def test_every_clip_gets_a_directory(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        report = migrate_event(old, new)
        assert report.clips == 2
        assert len(clipstore.iter_clip_dirs(new)) == 2

    def test_a_transcript_is_mapped_by_its_recorded_source(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        migrate_event(old, new)
        directory = clipstore.clip_dir(new, CLIP_A, "Speedy!")
        payload = json.loads(
            clipstore.transcript_path(directory).read_text(encoding="utf-8"))
        assert payload["source"] == CLIP_A

    def test_a_transcript_maps_when_source_differs_only_by_query_or_slash(self, tmp_path):
        # A transcript source that canonicalizes to the same clip as clips.json's
        # url (only a share param / trailing slash differs) must map, not land in
        # unmapped and be silently skipped.
        old, new = old_event(tmp_path), tmp_path / "new"
        (old / "transcripts" / "speedy.json").write_text(json.dumps(
            {"source": CLIP_A + "?si=shareToken", "words": [
                {"start": 0.0, "end": 0.5, "text": " hi"}]}), encoding="utf-8")
        report = migrate_event(old, new)
        directory = clipstore.clip_dir(new, CLIP_A, "Speedy!")
        assert clipstore.transcript_path(directory).exists()
        assert not any("speedy.json" in u for u in report.unmapped)

    def test_a_draft_is_copied_byte_for_byte(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        migrate_event(old, new)
        directory = clipstore.clip_dir(new, CLIP_A, "Speedy!")
        assert clipstore.short_path(directory).read_bytes() == b"speedy-short"

    def test_the_source_list_is_carried_over(self, tmp_path):
        # Checksum, not just .exists() - the one migration assertion in
        # this file that used to only check presence, not content.
        old, new = old_event(tmp_path), tmp_path / "new"
        migrate_event(old, new)
        assert (new / "sources.json").read_bytes() == (old / "clip_urls.json").read_bytes()

    def test_the_original_is_left_untouched(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        before = {p.name: p.read_bytes() for p in (old / "drafts").iterdir()}
        migrate_event(old, new)
        after = {p.name: p.read_bytes() for p in (old / "drafts").iterdir()}
        assert before == after

    def test_an_unmappable_transcript_is_reported_not_dropped_silently(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        (old / "transcripts" / "ghost.json").write_text(json.dumps(
            {"source": "https://www.youtube.com/clip/ZZZ", "words": []}),
            encoding="utf-8")
        report = migrate_event(old, new)
        assert any("ghost.json" in item for item in report.unmapped)

    def test_a_corrupted_copy_is_detected(self, tmp_path, monkeypatch):
        old, new = old_event(tmp_path), tmp_path / "new"
        import yt_shorts.migrate as migrate_module

        real = migrate_module._digest
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            return "wrong" if calls["n"] % 2 == 0 else real(path)

        monkeypatch.setattr(migrate_module, "_digest", flaky)
        with pytest.raises(MigrationError):
            migrate_event(old, new)

    def test_migrating_onto_itself_is_refused(self, tmp_path):
        old = old_event(tmp_path)
        with pytest.raises(MigrationError):
            migrate_event(old, old)

    def test_the_events_own_overrides_are_carried_over(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        (old / "brand.json").write_text(json.dumps(
            {"colors": {"accent": "#FF3355"}}), encoding="utf-8")
        (old / "layout.py").write_text("def decorate(*a, **k): pass\n",
                                       encoding="utf-8")
        (old / "assets").mkdir()
        (old / "assets" / "logo.png").write_bytes(b"png-bytes")

        migrate_event(old, new)

        assert json.loads((new / "brand.json").read_text(encoding="utf-8")) == {
            "colors": {"accent": "#FF3355"}}
        assert (new / "layout.py").exists()
        assert (new / "assets" / "logo.png").read_bytes() == b"png-bytes"


class TestOldNamingRulesAreReproducedFaithfully:
    """I5: the old layout's draft filename was not just a base slug -
    unique_short_name() also appended a -2/-3/... suffix on a collision and
    fell back to clip-<N> (1-indexed position) for a hook with no usable
    slug. A migration that only reproduces the base slug looks for the
    wrong filename in either case, finds nothing, and silently copies
    nothing across - exactly the unrecoverable loss this branch exists to
    prevent, since the operator is invited to delete the original once
    satisfied."""

    def test_a_slug_collision_still_finds_its_own_draft(self, tmp_path):
        old = tmp_path / "old"
        (old / "drafts").mkdir(parents=True)
        (old / "transcripts").mkdir()
        clip_a = "https://www.youtube.com/clip/AAA"
        clip_b = "https://www.youtube.com/clip/BBB"
        (old / "clip_urls.json").write_text(json.dumps(
            [{"url": clip_a, "hook": "Hello World!"},
             {"url": clip_b, "hook": "Hello, World?"}]), encoding="utf-8")
        (old / "clips.json").write_text(json.dumps([
            {"url": clip_a, "hook": "Hello World!", "source_title": "ERF",
             "start": 1.0, "end": 2.0, "duration": 1.0, "error": None},
            {"url": clip_b, "hook": "Hello, World?", "source_title": "ERF",
             "start": 3.0, "end": 4.0, "duration": 1.0, "error": None},
        ]), encoding="utf-8")
        # Both hooks slugify to "hello-world" - old cmd_render's
        # unique_short_name() gave the second entry the "-2" suffix.
        (old / "drafts" / "hello-world.mp4").write_bytes(b"first-short")
        (old / "drafts" / "hello-world-2.mp4").write_bytes(b"second-short")

        new = tmp_path / "new"
        report = migrate_event(old, new)

        dir_a = clipstore.clip_dir(new, clip_a, "Hello World!")
        dir_b = clipstore.clip_dir(new, clip_b, "Hello, World?")
        assert clipstore.short_path(dir_a).read_bytes() == b"first-short"
        assert clipstore.short_path(dir_b).read_bytes() == b"second-short"
        assert not any("no clip" in item for item in report.unmapped)

    def test_an_empty_hook_falls_back_to_its_position(self, tmp_path):
        old = tmp_path / "old"
        (old / "drafts").mkdir(parents=True)
        (old / "transcripts").mkdir()
        clip_a = "https://www.youtube.com/clip/AAA"
        (old / "clip_urls.json").write_text(json.dumps(
            [{"url": clip_a, "hook": "???"}]), encoding="utf-8")
        (old / "clips.json").write_text(json.dumps([
            {"url": clip_a, "hook": "???", "source_title": "ERF",
             "start": 1.0, "end": 2.0, "duration": 1.0, "error": None},
        ]), encoding="utf-8")
        # "???" slugifies to nothing usable - old cmd_render fell back to
        # the 1-indexed position, "clip-1".
        (old / "drafts" / "clip-1.mp4").write_bytes(b"positional-short")

        new = tmp_path / "new"
        report = migrate_event(old, new)

        directory = clipstore.clip_dir(new, clip_a, "???")
        assert clipstore.short_path(directory).read_bytes() == b"positional-short"
        assert not any("no clip" in item for item in report.unmapped)

    def test_an_unlocatable_draft_is_reported_not_silently_skipped(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        # Matches no name this migration can compute for any clip - the
        # exact silent-loss scenario I5 describes: nothing links it back.
        (old / "drafts" / "ghost-draft.mp4").write_bytes(b"orphan")

        report = migrate_event(old, new)

        assert any("ghost-draft.mp4" in item for item in report.unmapped)


class TestSyncChannelFile:
    """I3: channel-level copies (channel.json, brand.json, layout.py) used
    to be plain shutil.copy2 with no verification at all, contradicting
    this module's own docstring, and were skipped WHOLESALE the moment the
    target already existed - unable to tell a stale copy from an
    intentional one, and silent either way."""

    def test_an_absent_source_is_a_no_op(self, tmp_path):
        source = tmp_path / "does-not-exist.json"
        target = tmp_path / "workspace" / "brand.json"

        assert sync_channel_file(source, target) == "absent"
        assert not target.exists()

    def test_a_missing_target_is_copied_and_verified(self, tmp_path):
        source = tmp_path / "brand.json"
        source.write_text('{"a": 1}', encoding="utf-8")
        target = tmp_path / "workspace" / "brand.json"

        assert sync_channel_file(source, target) == "copied"
        assert target.read_text(encoding="utf-8") == '{"a": 1}'

    def test_a_matching_target_is_reported_unchanged(self, tmp_path):
        source = tmp_path / "brand.json"
        source.write_text('{"a": 1}', encoding="utf-8")
        target = tmp_path / "workspace" / "brand.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"a": 1}', encoding="utf-8")

        assert sync_channel_file(source, target) == "unchanged"

    def test_a_differing_target_is_left_alone_and_reported(self, tmp_path):
        """An operator's own workspace customization must never be
        silently clobbered by a later migration run."""
        source = tmp_path / "brand.json"
        source.write_text('{"a": 2}', encoding="utf-8")
        target = tmp_path / "workspace" / "brand.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"a": 1}', encoding="utf-8")

        assert sync_channel_file(source, target) == "differs"
        assert target.read_text(encoding="utf-8") == '{"a": 1}'


class TestSyncChannelTree:
    def test_an_absent_source_directory_is_a_no_op(self, tmp_path):
        result = sync_channel_tree(tmp_path / "no-fonts", tmp_path / "workspace" / "fonts")
        assert result == {"copied": [], "unchanged": [], "differs": []}

    def test_every_file_in_a_missing_target_is_copied_and_verified(self, tmp_path):
        source = tmp_path / "fonts"
        source.mkdir()
        (source / "a.ttf").write_bytes(b"font-a")
        target = tmp_path / "workspace" / "fonts"

        result = sync_channel_tree(source, target)

        assert result["copied"] == ["a.ttf"]
        assert (target / "a.ttf").read_bytes() == b"font-a"

    def test_a_font_added_after_the_first_migration_is_picked_up_by_the_second(
        self, tmp_path
    ):
        source = tmp_path / "fonts"
        source.mkdir()
        (source / "a.ttf").write_bytes(b"font-a")
        target = tmp_path / "workspace" / "fonts"
        sync_channel_tree(source, target)  # first migration

        (source / "b.ttf").write_bytes(b"font-b")  # added to the repo later
        result = sync_channel_tree(source, target)  # second migration

        assert result["copied"] == ["b.ttf"]
        assert result["unchanged"] == ["a.ttf"]
        assert (target / "b.ttf").read_bytes() == b"font-b"

    def test_a_stale_target_file_is_left_alone_and_reported(self, tmp_path):
        source = tmp_path / "fonts"
        source.mkdir()
        (source / "a.ttf").write_bytes(b"font-a-new")
        target = tmp_path / "workspace" / "fonts"
        target.mkdir(parents=True)
        (target / "a.ttf").write_bytes(b"font-a-old")

        result = sync_channel_tree(source, target)

        assert result["differs"] == ["a.ttf"]
        assert (target / "a.ttf").read_bytes() == b"font-a-old"
