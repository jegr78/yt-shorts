"""Tests for the command line (`yt_shorts.cli`, invoked as bin/yt-shorts).

build_short is stubbed so the tests run without network and without ffmpeg:
the stub calls the real ytdlp_command() check (the same place where the real
failure originates), but instead of a real download it only creates an empty
target file.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from yt_shorts import cli as _cli_module
from yt_shorts import clipstore
from yt_shorts import editorial
from yt_shorts import subtitle_pipeline
from yt_shorts.glossary import EMPTY as GLOSSARY_EMPTY
from yt_shorts.harvest import ClipEntry
from yt_shorts.lock import EventLock
from yt_shorts.profile import load as profile_load
from yt_shorts.render import ytdlp_command
from yt_shorts import render as render_module


def _load_cli():
    """The CLI module. It used to be loaded through SourceFileLoader because
    bin/yt-shorts has no .py suffix; the code now lives in the package, so a
    plain import is all this needs."""
    return _cli_module


@pytest.fixture
def cli():
    return _load_cli()


def _build_short_stub(source, hook, footer, target, config, work_dir, *,
                      keep_raw=False, subtitle_provider=None):
    """Replaces render.build_short: checks the source like the original
    (that's exactly where the real failure is triggered), but instead of a
    real download only creates an empty file.

    Calls ``subtitle_provider``, if given, with a raw-file path shaped
    like the one the real build_short passes it (same work_dir, same
    "<stem>.raw.mp4" naming) - cmd_render's provider closure is exercised
    end to end this way, the same as it would be for a real render,
    instead of being built and handed over but never actually called
    (Finding F8).

    ``keep_raw`` is accepted only so this stub keeps the real function's
    signature - cmd_render now always passes keep_raw=True (see
    yt_shorts.render.build_short's own docstring), and this stub has no
    raw file of its own to keep or discard."""
    ytdlp_command(source, target)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    if subtitle_provider is not None:
        raw_path = str(Path(work_dir) / f"{Path(target).stem}.raw.mp4")
        subtitle_provider(raw_path)
    Path(target).write_bytes(b"stub-mp4")
    return target


def _seed_clip(dir_: Path, url: str, hook: str, **fields) -> Path:
    """Writes one clip into the store (tmp_path itself is the event
    directory, exactly like the brief's own TestRenderUsesTheEditorialLayer
    fixture) and returns its directory. Replaces the flat-clips.json era's
    _event_dir(): cmd_render and cmd_gallery no longer read a shared
    clips.json at all, so seeding one no longer sets anything up for them."""
    entry = {"url": url, "hook": hook, "source_title": "ERF", "start": 1.0,
              "end": 2.0, "duration": 1.0, "error": None}
    entry.update(fields)
    return clipstore.write_clip(dir_, entry)


class TestMigrateThenHarvest:
    """C1: the one shape of test the branch was missing. migrate_event
    writes the new layout's source list as sources.json; cmd_harvest used
    to hardcode the old layout's clip_urls.json and crashed with a raw
    FileNotFoundError the moment it ran against a just-migrated event -
    exactly the first command of the documented workflow, on exactly the
    data this branch exists to produce. Every per-task unit test passed
    anyway because each one writes whichever filename its own module
    expects; none of them ever ran migrate and harvest back to back on the
    same event, which is what this test does."""

    def _old_repo_event(self, tmp_path, channel="erf", event="backcatalogue"):
        old_channel = tmp_path / "repo_channels" / channel
        old_event = old_channel / "events" / event
        old_event.mkdir(parents=True)
        (old_event / "clip_urls.json").write_text(json.dumps(
            [{"url": "https://example.invalid/a", "hook": "Speedy!"}]),
            encoding="utf-8")
        (old_event / "clips.json").write_text(json.dumps([]), encoding="utf-8")
        return old_channel.parent, old_event

    def _patch_workspace(self, cli, monkeypatch, repo_channels, workspace_root):
        (workspace_root / "channels").mkdir(parents=True)
        monkeypatch.setattr(cli.workspace, "REPO_CHANNELS", repo_channels)
        monkeypatch.setattr(
            cli.workspace, "resolve",
            lambda: cli.workspace.Workspace(
                root=workspace_root, channels_dir=workspace_root / "channels",
                origin="test"))

    def test_harvest_succeeds_immediately_after_a_migration(
        self, cli, monkeypatch, tmp_path
    ):
        repo_channels, _ = self._old_repo_event(tmp_path)
        workspace_root = tmp_path / "workspace"
        self._patch_workspace(cli, monkeypatch, repo_channels, workspace_root)

        assert cli.cmd_migrate("erf/backcatalogue") == 0

        new_event = workspace_root / "channels" / "erf" / "events" / "backcatalogue"
        assert (new_event / "sources.json").exists()
        assert not (new_event / "clip_urls.json").exists()

        def fake_harvest(entries, ytdlp="yt-dlp"):
            for e in entries:
                yield cli.ClipEntry(url=e["url"], hook=e["hook"],
                                    source_title="ERF", start=1.0, end=2.0,
                                    duration=1.0, error=None)
        monkeypatch.setattr(cli, "harvest", fake_harvest)

        assert cli.cmd_harvest(new_event) == 0
        assert len(clipstore.iter_clip_dirs(new_event)) == 1

    def test_an_unmigrated_event_still_reads_the_old_clip_urls_json(
        self, cli, monkeypatch, tmp_path
    ):
        """Repository-fallback mode is still documented and supported: an
        event that has never been migrated only has clip_urls.json, and
        harvest must keep working against it."""
        dir_ = tmp_path / "event"
        dir_.mkdir()
        (dir_ / "clip_urls.json").write_text(json.dumps(
            [{"url": "https://example.invalid/a", "hook": "Speedy!"}]),
            encoding="utf-8")

        def fake_harvest(entries, ytdlp="yt-dlp"):
            for e in entries:
                yield cli.ClipEntry(url=e["url"], hook=e["hook"],
                                    source_title="ERF", start=1.0, end=2.0,
                                    duration=1.0, error=None)
        monkeypatch.setattr(cli, "harvest", fake_harvest)

        assert cli.cmd_harvest(dir_) == 0
        assert len(clipstore.iter_clip_dirs(dir_)) == 1

    def test_a_missing_source_list_is_an_understandable_error_not_a_traceback(
        self, cli, tmp_path, capsys
    ):
        dir_ = tmp_path / "event"
        dir_.mkdir()

        code = cli.cmd_harvest(dir_)

        assert code == 2
        err = capsys.readouterr().err
        assert "sources.json" in err
        assert "Traceback" not in err


class TestCmdMigrateRejectsAMalformedIdentifier:
    """Minor cleanup: `bin/yt-shorts migrate erf` (no /event) used to
    crash with a raw ValueError (identifier.split("/", 1) unpacking into
    two variables). Mirrors profile.load's own identifier validation."""

    def test_an_identifier_without_a_slash_is_reported_not_raised(
        self, cli, monkeypatch, tmp_path, capsys
    ):
        workspace_root = tmp_path / "workspace"
        (workspace_root / "channels").mkdir(parents=True)
        monkeypatch.setattr(
            cli.workspace, "resolve",
            lambda: cli.workspace.Workspace(
                root=workspace_root, channels_dir=workspace_root / "channels",
                origin="test"))

        code = cli.cmd_migrate("erf")

        assert code == 2
        assert "channel/event" in capsys.readouterr().err


class TestCmdMigrateChannelLevelCopies:
    """I3: there was no CLI-level test of `migrate` at all. Also covers the
    channel-level copies (channel.json, brand.json, fonts/) now being
    verified by checksum (see sync_channel_file/sync_channel_tree) instead
    of a plain, unverified shutil.copy2/copytree that skipped entirely
    once the target existed - unable to pick up a font added to the repo
    after the first migration, or to tell a stale copy from an operator's
    own hand-edited one."""

    def _workspace(self, cli, monkeypatch, tmp_path):
        repo_channels = tmp_path / "repo_channels"
        channel = repo_channels / "erf"
        (channel / "events" / "ev").mkdir(parents=True)
        (channel / "events" / "ev" / "clip_urls.json").write_text("[]", encoding="utf-8")
        (channel / "events" / "ev" / "clips.json").write_text("[]", encoding="utf-8")
        (channel / "channel.json").write_text('{"id": "x"}', encoding="utf-8")
        (channel / "brand.json").write_text('{"colors": {}}', encoding="utf-8")
        (channel / "fonts").mkdir()
        (channel / "fonts" / "a.ttf").write_bytes(b"font-a")

        workspace_root = tmp_path / "workspace"
        (workspace_root / "channels").mkdir(parents=True)
        monkeypatch.setattr(cli.workspace, "REPO_CHANNELS", repo_channels)
        monkeypatch.setattr(
            cli.workspace, "resolve",
            lambda: cli.workspace.Workspace(
                root=workspace_root, channels_dir=workspace_root / "channels",
                origin="test"))
        return repo_channels, workspace_root

    def test_channel_level_files_are_copied_and_verified(self, cli, monkeypatch, tmp_path):
        _, workspace_root = self._workspace(cli, monkeypatch, tmp_path)

        assert cli.cmd_migrate("erf/ev") == 0

        target_channel = workspace_root / "channels" / "erf"
        assert (target_channel / "channel.json").read_text(encoding="utf-8") == '{"id": "x"}'
        assert (target_channel / "brand.json").read_text(encoding="utf-8") == '{"colors": {}}'
        assert (target_channel / "fonts" / "a.ttf").read_bytes() == b"font-a"

    def test_a_second_migration_picks_up_a_font_added_since_the_first(
        self, cli, monkeypatch, tmp_path
    ):
        repo_channels, workspace_root = self._workspace(cli, monkeypatch, tmp_path)
        assert cli.cmd_migrate("erf/ev") == 0

        (repo_channels / "erf" / "fonts" / "b.ttf").write_bytes(b"font-b")
        assert cli.cmd_migrate("erf/ev") == 0

        assert (workspace_root / "channels" / "erf" / "fonts" / "b.ttf").read_bytes() == b"font-b"

    def test_a_hand_edited_workspace_brand_json_is_not_silently_overwritten(
        self, cli, monkeypatch, tmp_path, capsys
    ):
        repo_channels, workspace_root = self._workspace(cli, monkeypatch, tmp_path)
        target_channel = workspace_root / "channels" / "erf"
        target_channel.mkdir(parents=True)
        (target_channel / "brand.json").write_text(
            '{"colors": {"accent": "hand-edited"}}', encoding="utf-8")

        assert cli.cmd_migrate("erf/ev") == 0

        assert (target_channel / "brand.json").read_text(encoding="utf-8") == (
            '{"colors": {"accent": "hand-edited"}}')
        err = capsys.readouterr().err
        assert "brand.json" in err


class TestCmdMigrateReportsWhatItActuallyDid:
    """I4: a real six-clip migration used to print "migrated 6 clip(s), 1
    file(s) copied" - the six clip.json writes and the channel-level
    copies were not counted at all, and raw/ (deliberately not migrated,
    since it is scratch re-downloaded on render) was never mentioned."""

    def test_the_report_counts_clip_json_and_channel_level_copies(
        self, cli, monkeypatch, tmp_path, capsys
    ):
        repo_channels = tmp_path / "repo_channels"
        channel = repo_channels / "erf"
        (channel / "events" / "ev").mkdir(parents=True)
        (channel / "events" / "ev" / "clip_urls.json").write_text(json.dumps(
            [{"url": "https://example.invalid/a", "hook": "Speedy!"}]), encoding="utf-8")
        (channel / "events" / "ev" / "clips.json").write_text(json.dumps([
            {"url": "https://example.invalid/a", "hook": "Speedy!", "source_title": "ERF",
             "start": 1.0, "end": 2.0, "duration": 1.0, "error": None},
        ]), encoding="utf-8")
        (channel / "channel.json").write_text('{"id": "x"}', encoding="utf-8")
        (channel / "brand.json").write_text('{"colors": {}}', encoding="utf-8")

        workspace_root = tmp_path / "workspace"
        (workspace_root / "channels").mkdir(parents=True)
        monkeypatch.setattr(cli.workspace, "REPO_CHANNELS", repo_channels)
        monkeypatch.setattr(
            cli.workspace, "resolve",
            lambda: cli.workspace.Workspace(
                root=workspace_root, channels_dir=workspace_root / "channels",
                origin="test"))

        assert cli.cmd_migrate("erf/ev") == 0

        out = capsys.readouterr().out
        assert "1 clip.json written" in out
        assert "raw/" in out


class TestMainReportsAWorkspaceErrorInsteadOfATraceback:
    """I1: yt_shorts.profile resolves the workspace again at ITS OWN import
    time (CHANNELS_DIR = workspace.resolve().channels_dir), and this file
    used to import it at module scope - before __main__'s own
    try/except WorkspaceError below ever got a chance to run. A bad
    YT_SHORTS_DATA therefore shipped as a raw traceback with exit code 1,
    and the try/except was dead code.

    This has to be a real subprocess: the ordering bug only reproduces
    when the file is actually run as __main__ - _load_cli() (used by
    every other test in this file) loads it as a plain module, under a
    different __name__, so the __main__ block never executes there."""

    def test_a_bad_workspace_produces_the_message_not_a_traceback(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        shim = Path(__file__).resolve().parent.parent / "bin" / "yt-shorts"

        # Windows cannot execute an extensionless file by its shebang
        # (WinError 193), so name the interpreter there.
        argv = ([sys.executable, str(shim)] if os.name == "nt" else [str(shim)])
        result = subprocess.run(
            [*argv, "gallery", "erf/community-clips-back-catalogue"],
            env={**os.environ, "YT_SHORTS_DATA": str(missing)},
            capture_output=True, text=True,
        )

        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert str(missing) in result.stderr


class TestCmdRenderIsolatesFailures:
    """Finding 1, ported onto the clip store: a single broken clip must
    never abort the run, no matter which exception type triggers it.

    In the flat-clips.json era this covered a null entry, a string
    instead of an object, and an object missing "hook" or "url" outright
    - all entries a bare list index accepted without complaint, so a
    corrupt or hand-edited clips.json could take the whole run down with
    it. Several of those specific triggers can no longer reach cmd_render
    at all: clipstore.write_clip() already refuses an entry with no url
    before a directory is ever created (see clipstore.ClipStoreError), so
    "entry without a url" is not a state a clip directory can be in. What
    IS still reachable, and is exercised below instead, is disk-level
    corruption of an already-written clip: a clip.json that is not a JSON
    object (mirroring the old null/string cases - clipstore.read_clip()
    rejects both), and a clip.json missing "hook" (mirroring the old
    "entry without hook key" case) - the same "malformed entry between two
    good ones must not abort the run" guarantee, triggered the way it can
    actually still occur on disk.
    """

    def test_entry_already_marked_as_failed_is_reported_not_attempted(
        self, cli, monkeypatch, tmp_path
    ):
        calls = []
        monkeypatch.setattr(
            cli, "build_short",
            lambda *a, **k: (calls.append(1), _build_short_stub(*a, **k))[1],
        )
        _seed_clip(tmp_path, "https://example.invalid/a", "Broken", error="previously failed")
        valid = _seed_clip(tmp_path, "https://example.invalid/b", "Valid")

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert len(calls) == 1, "only the valid entry may reach build_short"
        assert clipstore.short_path(valid).exists()
        assert code != 0

    def test_a_non_object_clip_json_does_not_abort_the_run(self, cli, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        broken = clipstore.clip_dir(tmp_path, "https://example.invalid/broken", "Broken")
        broken.mkdir(parents=True)
        (broken / clipstore.CLIP_FILENAME).write_text("null", encoding="utf-8")
        valid = _seed_clip(tmp_path, "https://example.invalid/b", "Valid clip")

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert clipstore.short_path(valid).exists()
        assert code != 0

    def test_a_clip_json_missing_hook_does_not_abort_the_run(self, cli, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        # write_clip() itself never requires "hook" - only harvest always
        # supplies one. A hand-corrupted (or hand-truncated) clip.json
        # without it must still fail only itself.
        broken = clipstore.write_clip(tmp_path, {"url": "https://example.invalid/a", "error": None})
        valid = _seed_clip(tmp_path, "https://example.invalid/b", "Valid clip")

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert clipstore.short_path(valid).exists()
        assert not clipstore.short_path(broken).exists()
        assert code != 0

    def test_error_reason_ends_up_in_the_summary_message(self, cli, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        broken = _seed_clip(tmp_path, "https://example.invalid/a", "Broken clip",
                             error="ConnectionError: timed out")
        _seed_clip(tmp_path, "https://example.invalid/b", "Valid clip")

        cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        out = capsys.readouterr()
        assert "ConnectionError" in out.err
        # An already-failed clip is reported under its DIRECTORY name, not
        # the raw hook text - cmd_render has not read editorial data for it
        # yet at the point the error is recorded (see brief Step 3).
        assert broken.name in out.err

    def test_three_broken_clips_between_valid_ones_do_not_abort_the_run(
        self, cli, monkeypatch, tmp_path, capsys
    ):
        """W1, ported: three differently-broken clips (a non-object
        clip.json, a clip.json missing "hook", and an unreadable edit.json)
        sit between two valid ones. All three must be reported
        individually and neither valid clip may be lost."""
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        first = _seed_clip(tmp_path, "https://example.invalid/a", "First valid clip")

        non_object = clipstore.clip_dir(tmp_path, "https://example.invalid/x", "Not an object")
        non_object.mkdir(parents=True)
        (non_object / clipstore.CLIP_FILENAME).write_text('"just a string"', encoding="utf-8")

        clipstore.write_clip(tmp_path, {"url": "https://example.invalid/y", "error": None})

        bad_edit = _seed_clip(tmp_path, "https://example.invalid/z", "Bad edit")
        (bad_edit / editorial.EDIT_FILENAME).write_text("{not json", encoding="utf-8")

        second = _seed_clip(tmp_path, "https://example.invalid/b", "Second valid clip")

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert clipstore.short_path(first).exists()
        assert clipstore.short_path(second).exists()
        assert code != 0
        out = capsys.readouterr()
        assert out.err.count("ERROR:") == 3, "each of the three broken clips must be reported individually"
        summary = out.err
        assert "ClipStoreError" in summary, "the non-object clip.json must be reported with its exception type"
        assert "KeyError" in summary, "a clip.json missing 'hook' must be reported with its exception type"
        assert "EditError" in summary, "an unreadable edit.json must be reported with its exception type"


class TestCmdRenderLocksTheEvent:
    """Finding A2: two concurrent `render` processes against the same event
    collide on each clip's own raw.mp4, subs/ and short.mp4 - cmd_render
    must take an exclusive lock on the event directory before it does
    anything else, refuse a second concurrent run, and always release the
    lock on the way out."""

    def test_second_run_refuses_while_the_first_holds_the_lock(
        self, cli, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        valid = _seed_clip(tmp_path, "https://example.invalid/a", "Valid clip")
        # Simulate another, still-running render process already holding
        # the lock, instead of relying on cmd_render itself not releasing
        # in time.
        other = EventLock(tmp_path)
        other.acquire()

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert code != 0
        assert not clipstore.short_path(valid).exists(), (
            "a locked-out run must not render anything"
        )
        out = capsys.readouterr()
        assert tmp_path.name in out.err
        other.release()

    def test_stale_lock_from_a_dead_process_is_taken_over(self, cli, monkeypatch, tmp_path):
        """Checked from INSIDE the render (not just the end result), so
        this actually proves the stale lock's content was overwritten with
        this process's own pid rather than merely that a render happened
        to succeed anyway - the same end result a build without any lock
        wiring at all would also produce."""
        _seed_clip(tmp_path, "https://example.invalid/a", "Valid clip")
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()  # guarantees the pid is no longer alive
        (tmp_path / ".render.lock").write_text(str(proc.pid), encoding="utf-8")

        pid_seen_during_render = []

        def _stub_reading_lock(*a, **k):
            pid_seen_during_render.append(
                (tmp_path / ".render.lock").read_text(encoding="utf-8").strip())
            return _build_short_stub(*a, **k)

        monkeypatch.setattr(cli, "build_short", _stub_reading_lock)

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert code == 0
        assert pid_seen_during_render == [str(os.getpid())], (
            "the stale lock must be overwritten with this process's own pid"
        )

    def test_lock_is_held_during_the_run_and_released_after_success(
        self, cli, monkeypatch, tmp_path
    ):
        _seed_clip(tmp_path, "https://example.invalid/a", "Valid clip")
        held_during_render = []

        def _stub_checking_lock(*a, **k):
            held_during_render.append((tmp_path / ".render.lock").exists())
            return _build_short_stub(*a, **k)

        monkeypatch.setattr(cli, "build_short", _stub_checking_lock)

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert code == 0
        assert held_during_render == [True], "the lock must be held while the clip renders"
        assert not (tmp_path / ".render.lock").exists(), "the lock must be released after success"

    def test_lock_is_held_during_the_run_and_released_after_failure(
        self, cli, monkeypatch, tmp_path
    ):
        # write_clip() itself refuses an empty url (there is no identity to
        # file it under), so this - unlike the flat-clips.json era's
        # {"url": ""} row - is written directly: a clip whose STORED url
        # is empty, the same shape a hand-edited clip.json could produce.
        # ytdlp_command (exercised for real inside _build_short_stub, same
        # as before) rejects it with ValueError, so build_short still
        # fails this candidate exactly like before - the failure just
        # originates from disk corruption now rather than an unfiltered
        # clips.json row.
        directory = clipstore.clip_dir(tmp_path, "https://example.invalid/empty-url", "Empty URL")
        directory.mkdir(parents=True)
        (directory / clipstore.CLIP_FILENAME).write_text(json.dumps({
            "url": "", "hook": "Empty URL", "source_title": "ERF",
            "start": 1.0, "end": 2.0, "duration": 1.0, "error": None,
        }), encoding="utf-8")
        held_during_render = []

        def _stub_checking_lock(*a, **k):
            held_during_render.append((tmp_path / ".render.lock").exists())
            return _build_short_stub(*a, **k)

        monkeypatch.setattr(cli, "build_short", _stub_checking_lock)

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert code != 0
        assert held_during_render == [True], "the lock must be held while the candidate is attempted"
        assert not (tmp_path / ".render.lock").exists(), "the lock must be released after a failure"

    def test_lock_is_released_even_when_an_unexpected_error_occurs_before_the_loop(
        self, cli, monkeypatch, tmp_path
    ):
        """Ported from the flat-clips.json era's 'released even when
        clips.json is unreadable': that specific trigger no longer exists
        - there is no single shared file left to corrupt.
        clipstore.iter_clip_dirs() never raises, even given a missing or
        malformed clip.json, since it only checks that clip.json EXISTS,
        not that it parses (a broken clip.json is instead isolated per-clip
        inside the loop, see TestCmdRenderIsolatesFailures). The GUARANTEE
        this test protected - the lock releases even on a crash before the
        per-candidate loop starts, not only for a failure recognised
        INSIDE it - still needs to hold, so it is exercised here by making
        iter_clip_dirs itself raise, the way any future change to it might
        accidentally make happen. Checked by tracking EventLock.release
        itself (not just the file's absence afterwards), since an
        unmodified cmd_render never creates the file at all and so would
        also leave it absent - proving nothing about a release actually
        having happened."""
        released = []
        original_release = cli.EventLock.release

        def _tracking_release(self):
            released.append(True)
            return original_release(self)

        monkeypatch.setattr(cli.EventLock, "release", _tracking_release)

        def _boom(*a, **k):
            raise RuntimeError("simulated crash before the per-candidate loop")

        monkeypatch.setattr(cli.clipstore, "iter_clip_dirs", _boom)

        with pytest.raises(RuntimeError):
            cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert released == [True]
        assert not (tmp_path / ".render.lock").exists()


class TestClipsSharingATitleGetSeparateDirectories:
    """Finding 2, re-scoped for the clip store. In the flat-clips.json era,
    two hooks that slugified to the same name ("Hello World!" and "Hello,
    World?") collided on drafts/hello-world.mp4 and unique_short_name()
    existed purely to append a counter and keep both. On the clip store a
    clip's directory name carries the URL's id suffix (see
    clipid.directory_name) as well as the slug, so two DIFFERENT clips can
    no longer collide on a name at all - unique_short_name() and
    short_name() are gone along with the mechanism they protected against.
    What replaces the guarantee: two clips that happen to share a title
    must still end up in two separate directories, each with its own
    short, never one clobbering the other."""

    def _two_same_titled_clips(self, tmp_path):
        a = _seed_clip(tmp_path, "https://example.invalid/a", "Hello World!")
        b = _seed_clip(tmp_path, "https://example.invalid/b", "Hello World!")
        return a, b

    def test_two_clips_with_the_same_title_get_separate_directories(self, tmp_path):
        a, b = self._two_same_titled_clips(tmp_path)

        assert a != b
        assert a.exists() and b.exists()
        assert len(clipstore.iter_clip_dirs(tmp_path)) == 2

    def test_both_are_rendered_to_their_own_short_file(self, cli, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        a, b = self._two_same_titled_clips(tmp_path)

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert code == 0
        assert clipstore.short_path(a).exists()
        assert clipstore.short_path(b).exists()
        assert clipstore.short_path(a) != clipstore.short_path(b), (
            "neither short may be overwritten by the other"
        )


def _harvest_event_dir(tmp_path: Path, clip_urls: list[dict], clips: list[dict] | None = None) -> Path:
    """Like _event_dir, but for cmd_harvest: clip_urls.json is the input,
    and any pre-existing clips are seeded into the clip store (one
    directory per clip, keyed on url) rather than into a flat clips.json -
    that flat file no longer exists once harvest writes into clipstore."""
    from yt_shorts import clipstore

    dir_ = tmp_path / "event"
    dir_.mkdir()
    (dir_ / "clip_urls.json").write_text(json.dumps(clip_urls), encoding="utf-8")
    if clips is not None:
        for entry in clips:
            clipstore.write_clip(dir_, entry)
    return dir_


def _read_clip_store(dir_: Path) -> dict:
    """Every stored clip, keyed on url - the clip-store equivalent of
    ``{e["url"]: e for e in json.loads(clips_json)}`` from the flat-file
    era."""
    from yt_shorts import clipstore

    return {clipstore.read_clip(d)["url"]: clipstore.read_clip(d)
            for d in clipstore.iter_clip_dirs(dir_)}


class TestCmdHarvestPreservesGoodEntries:
    """W2: a second harvest run must not replace already-good entries with
    a failure (rate limiting, network down, ...). Ported onto the clip
    store: instead of one shared clips.json being rewritten from scratch,
    each already-good clip keeps its own clip.json untouched."""

    def test_already_good_entries_are_not_requeried_on_total_failure(
        self, cli, monkeypatch, tmp_path
    ):
        clip_urls = [
            {"url": "https://example.invalid/a", "hook": "Hook A"},
            {"url": "https://example.invalid/b", "hook": "Hook B"},
        ]
        before = [
            {"url": "https://example.invalid/a", "hook": "Hook A", "source_title": "Source A",
             "start": 10.0, "end": 25.0, "duration": 15.0, "error": None},
            {"url": "https://example.invalid/b", "hook": "Hook B", "source_title": "Source B",
             "start": 5.0, "end": 65.0, "duration": 60.0, "error": None},
        ]
        dir_ = _harvest_event_dir(tmp_path, clip_urls, before)

        def _harvest_must_not_be_called(*a, **k):
            pytest.fail("an already error-free entry was queried again")

        monkeypatch.setattr(cli, "harvest", _harvest_must_not_be_called)

        code = cli.cmd_harvest(dir_)

        after = _read_clip_store(dir_)
        assert after == {e["url"]: e for e in before}, "good entries must be preserved unchanged"
        assert code == 0

    def test_good_entries_survive_a_partial_failure_new_one_gets_marked(
        self, cli, monkeypatch, tmp_path
    ):
        """Simulates a rate limit/network disruption: two already-good
        entries plus a third, not yet resolved clip. The failure must only
        hit the third one, the two good ones stay untouched."""
        clip_urls = [
            {"url": "https://example.invalid/a", "hook": "Hook A"},
            {"url": "https://example.invalid/b", "hook": "Hook B"},
            {"url": "https://example.invalid/c", "hook": "Hook C - new"},
        ]
        before = [
            {"url": "https://example.invalid/a", "hook": "Hook A", "source_title": "Source A",
             "start": 10.0, "end": 25.0, "duration": 15.0, "error": None},
            {"url": "https://example.invalid/b", "hook": "Hook B", "source_title": "Source B",
             "start": 5.0, "end": 65.0, "duration": 60.0, "error": None},
        ]
        dir_ = _harvest_event_dir(tmp_path, clip_urls, before)

        def _harvest_simulates_failure(inputs, ytdlp="yt-dlp"):
            return [
                ClipEntry(url=e.get("url", ""), hook=e.get("hook", ""), source_title="",
                          start=0.0, end=0.0, duration=0.0, error="RuntimeError: simulated failure")
                for e in inputs
            ]

        monkeypatch.setattr(cli, "harvest", _harvest_simulates_failure)

        code = cli.cmd_harvest(dir_)

        after = _read_clip_store(dir_)
        assert after["https://example.invalid/a"]["error"] is None
        assert after["https://example.invalid/a"]["start"] == 10.0
        assert after["https://example.invalid/b"]["error"] is None
        assert after["https://example.invalid/b"]["duration"] == 60.0
        assert after["https://example.invalid/c"]["error"] is not None
        assert code != 0

    def test_broken_ytdlp_binary_does_not_wipe_out_already_good_entries(self, cli, tmp_path):
        """As suggested in the assignment: a real harvest() with a
        non-existent yt-dlp binary name, instead of stubbed."""
        clip_urls = [
            {"url": "https://example.invalid/a", "hook": "Hook A"},
            {"url": "https://example.invalid/b", "hook": "Hook B"},
            {"url": "https://example.invalid/c", "hook": "Hook C - new"},
        ]
        before = [
            {"url": "https://example.invalid/a", "hook": "Hook A", "source_title": "Source A",
             "start": 10.0, "end": 25.0, "duration": 15.0, "error": None},
            {"url": "https://example.invalid/b", "hook": "Hook B", "source_title": "Source B",
             "start": 5.0, "end": 65.0, "duration": 60.0, "error": None},
        ]
        dir_ = _harvest_event_dir(tmp_path, clip_urls, before)

        code = cli.cmd_harvest(dir_, ytdlp="definitely-not-a-real-binary-xyz")

        after = _read_clip_store(dir_)
        assert after["https://example.invalid/a"] == before[0]
        assert after["https://example.invalid/b"] == before[1]
        assert after["https://example.invalid/c"]["error"] is not None
        assert code != 0

    def test_hook_is_frozen_after_the_first_harvest(self, cli, monkeypatch, tmp_path):
        """Deliberate behaviour change from the flat-clips.json era (was:
        'hook text is taken from clip_urls.json regardless'). The hook is
        what names the clip's directory (clipid.directory_name treats it as
        the harvested title), so - exactly like a hand-edited title in
        editorial.py - it is frozen once the directory exists rather than
        re-read from clip_urls.json on every run. A typo now needs either a
        forced re-harvest (delete the clip's clip.json) or, once render
        reads editorial data, an edit.json override - not a silent rename
        of a directory another file (transcript.json, raw.mp4, ...) may
        already point at."""
        clip_urls = [{"url": "https://example.invalid/a", "hook": "Typed hook, typo fixed"}]
        before = [
            {"url": "https://example.invalid/a", "hook": "Typed hokk", "source_title": "Source A",
             "start": 10.0, "end": 25.0, "duration": 15.0, "error": None},
        ]
        dir_ = _harvest_event_dir(tmp_path, clip_urls, before)
        monkeypatch.setattr(
            cli, "harvest", lambda *a, **k: pytest.fail("must not be called for an already-good entry")
        )

        cli.cmd_harvest(dir_)

        after = _read_clip_store(dir_)
        assert after["https://example.invalid/a"]["hook"] == "Typed hokk"
        assert after["https://example.invalid/a"]["start"] == 10.0


class TestHarvestReportsClipsDroppedFromTheSourceList:
    """I7: before this branch, the rewritten clips.json dropped an entry
    the moment it left clip_urls.json. clip.json is now never rewritten by
    a derivation step for a clip that is no longer in the source list -
    deleting a clip's directory is the operator's own decision - but that
    must be REPORTED, not silently reintroduced as auto-deletion and not
    silently ignored either."""

    def test_a_clip_no_longer_in_the_source_list_is_reported_not_deleted(
        self, cli, monkeypatch, tmp_path, capsys
    ):
        dir_ = tmp_path / "event"
        dir_.mkdir()
        (dir_ / "sources.json").write_text(json.dumps([]), encoding="utf-8")
        directory = clipstore.write_clip(dir_, {
            "url": "https://example.invalid/a", "hook": "Dropped clip",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        monkeypatch.setattr(
            cli, "harvest", lambda *a, **k: pytest.fail("nothing to (re-)harvest")
        )

        code = cli.cmd_harvest(dir_)

        assert code == 0
        assert directory.exists(), "the clip's directory must not be deleted"
        err = capsys.readouterr().err
        assert directory.name in err
        assert "no longer in the source list" in err

    def test_a_clip_still_in_the_source_list_is_not_reported(
        self, cli, monkeypatch, tmp_path, capsys
    ):
        dir_ = tmp_path / "event"
        dir_.mkdir()
        (dir_ / "sources.json").write_text(json.dumps(
            [{"url": "https://example.invalid/a", "hook": "Kept clip"}]),
            encoding="utf-8")
        clipstore.write_clip(dir_, {
            "url": "https://example.invalid/a", "hook": "Kept clip",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        monkeypatch.setattr(
            cli, "harvest", lambda *a, **k: pytest.fail("already resolved")
        )

        cli.cmd_harvest(dir_)

        assert "no longer in the source list" not in capsys.readouterr().err


class TestExitCode:
    """Finding 3: the exit code must make failures recognizable to the caller."""

    def test_render_exit_code_0_when_everything_succeeds(self, cli, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        _seed_clip(tmp_path, "https://example.invalid/a", "Valid clip")

        assert cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial") == 0

    def test_render_exit_code_nonzero_on_failure(self, cli, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        _seed_clip(tmp_path, "https://example.invalid/a", "Broken", error="previously failed")

        assert cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial") != 0

    def test_harvest_exit_code_nonzero_on_failure(self, cli, monkeypatch, tmp_path):
        """Ported onto the clip store with a second, valid entry added
        alongside the url-less one (the pre-Stage-A version had only the
        broken entry, whose ``error`` field on the single resulting
        clips.json row was enough to prove the point). That no longer
        works here: a source row with no url at all has no identity for
        clipstore.write_clip() to file it under - it raises ClipStoreError
        instead of producing a clip.json to read `error` off. What must
        still hold, and is what this test now actually exercises, is the
        'one failed clip must never abort a run' guarantee: the url-less
        entry's ClipStoreError must not stop the second, valid entry from
        getting its own directory, and the run must still report failure
        via the exit code. harvest() is stubbed (mirroring what the real
        harvest() does for a missing url - see harvest.harvest) so this
        stays independent of the network and of yt-dlp being installed."""
        dir_ = tmp_path / "event"
        dir_.mkdir()
        (dir_ / "clip_urls.json").write_text(json.dumps([
            {"hook": "without url"},
            {"url": "https://example.invalid/ok", "hook": "Fine"},
        ]), encoding="utf-8")

        def fake_harvest(inputs, ytdlp="yt-dlp"):
            entries = []
            for e in inputs:
                if "url" not in e:
                    entries.append(ClipEntry(url="", hook=e.get("hook", ""), source_title="",
                                              start=0.0, end=0.0, duration=0.0,
                                              error="KeyError: 'url'"))
                else:
                    entries.append(ClipEntry(url=e["url"], hook=e["hook"], source_title="Source",
                                              start=1.0, end=2.0, duration=1.0, error=None))
            return entries

        monkeypatch.setattr(cli, "harvest", fake_harvest)

        code = cli.cmd_harvest(dir_)

        after = _read_clip_store(dir_)
        assert set(after) == {"https://example.invalid/ok"}
        assert code != 0


class TestSubtitleProviderIsExercised:
    """Finding F8: the ``provider`` closure cmd_render builds when
    subtitles are enabled was untested - _build_short_stub used to accept
    and ignore ``subtitle_provider`` outright, so nothing ever called it in
    a test. Now the stub calls it (see _build_short_stub above), so these
    tests exercise the real pipeline (yt_shorts.subtitle_pipeline, called
    from cmd_render via make_subtitle_provider), only with the heavy parts
    (transcription) stubbed out. transcribe/build_track/group_words are
    patched on yt_shorts.subtitle_pipeline itself, not on the cli module -
    that pipeline is a real, normally-imported package module - same as
    yt_shorts.cli itself, per _load_cli's plain `import yt_shorts.cli` above -
    and it is the module whose own global names the pipeline's closure
    actually looks up."""

    def test_no_speech_still_renders_and_says_so(self, cli, monkeypatch, tmp_path, capsys, caplog):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        monkeypatch.setattr(subtitle_pipeline, "transcribe", lambda *a, **k: [])
        clip = _seed_clip(tmp_path, "https://example.invalid/a", "Speedy!")
        config = {"subtitles": {"enabled": True}, "glossary": GLOSSARY_EMPTY}

        code = cli.cmd_render(tmp_path, config, "ERF | @ERFofficial")

        assert clipstore.short_path(clip).exists(), "a clip with no speech must still render"
        assert code == 0
        # These notes go through the logger now, not a bare stderr print:
        # that is what lets a STUDIO render report a lost caption track at
        # all (its job thread has no reader for stderr). A CLI run on a TTY
        # still shows them - logsetup adds a stdout handler - and they now
        # also persist in the workspace's central log.
        assert "no speech detected, no subtitles" in caplog.text
        assert "speedy" in caplog.text.lower()

    def test_subtitles_disabled_never_calls_the_provider(self, cli, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)

        def _must_not_be_called(*a, **k):
            pytest.fail("transcribe must not run when subtitles are disabled")

        monkeypatch.setattr(subtitle_pipeline, "transcribe", _must_not_be_called)
        clip = _seed_clip(tmp_path, "https://example.invalid/a", "Valid clip")

        code = cli.cmd_render(tmp_path, {}, "ERF | @ERFofficial")

        assert code == 0
        assert clipstore.short_path(clip).exists()

    def test_transcribe_is_called_with_the_clips_effective_title(self, cli, monkeypatch, tmp_path):
        """Finding B2, ported: transcribe()'s dropped-segment NOTE used to
        name the raw download's path, not the clip - every other NOTE in
        this tool names the clip instead. transcribe() takes an optional
        ``display_name`` for exactly this, and cmd_render's provider
        closure must actually pass it. On the clip store the name passed
        is the clip's EFFECTIVE title (editorial.effective_title: the
        operator's own title if one was set, otherwise the harvested hook)
        rather than a collision-suffixed filesystem slug - there is no
        such slug left to pass now that names cannot collide."""
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        calls = []

        def _spy_transcribe(*args, **kwargs):
            calls.append(kwargs)
            return []

        monkeypatch.setattr(subtitle_pipeline, "transcribe", _spy_transcribe)
        _seed_clip(tmp_path, "https://example.invalid/a", "Speedy!")
        config = {"subtitles": {"enabled": True}, "glossary": GLOSSARY_EMPTY}

        cli.cmd_render(tmp_path, config, "ERF | @ERFofficial")

        assert len(calls) == 1, "transcribe must have been called exactly once"
        assert calls[0]["display_name"] == "Speedy!"


class TestSubtitleFailureDegradesGracefully:
    """Finding F5: TranscriptionError already degraded to "no subtitles,
    short still renders" - every OTHER subtitle-pipeline failure (a
    malformed caption list rejected by build_track, an ffmpeg problem
    building the track) used to propagate out of the provider closure,
    fail the whole candidate, and cost the run its exit code. Subtitles
    are the optional layer: any failure in that pipeline must degrade the
    same way TranscriptionError already did, not destroy the clip. Only a
    failure of the render itself (inside build_short, not inside the
    provider) may still fail the candidate - that is not touched here."""

    def test_build_track_value_error_still_renders_with_a_note(
        self, cli, monkeypatch, tmp_path, capsys, caplog):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        monkeypatch.setattr(
            subtitle_pipeline, "transcribe",
            lambda *a, **k: [{"start": 0.0, "end": 1.0, "text": "hi"}],
        )

        def _broken_build_track(*a, **k):
            raise ValueError("malformed caption list")

        monkeypatch.setattr(subtitle_pipeline, "build_track", _broken_build_track)
        clip = _seed_clip(tmp_path, "https://example.invalid/a", "Speedy!")
        config = {"subtitles": {"enabled": True}, "glossary": GLOSSARY_EMPTY}

        code = cli.cmd_render(tmp_path, config, "ERF | @ERFofficial")

        assert clipstore.short_path(clip).exists(), "the short must still render without subtitles"
        assert code == 0, "nothing else failed - the exit code must stay 0"
        # These notes go through the logger now, not a bare stderr print:
        # that is what lets a STUDIO render report a lost caption track at
        # all (its job thread has no reader for stderr). A CLI run on a TTY
        # still shows them - logsetup adds a stdout handler - and they now
        # also persist in the workspace's central log.
        assert "ValueError" in caplog.text
        assert "malformed caption list" in caplog.text
        assert "speedy" in caplog.text.lower()

    def test_build_track_ffmpeg_runtime_error_still_renders_with_a_note(
        self, cli, monkeypatch, tmp_path, capsys, caplog):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        monkeypatch.setattr(
            subtitle_pipeline, "transcribe",
            lambda *a, **k: [{"start": 0.0, "end": 1.0, "text": "hi"}],
        )

        def _broken_build_track(*a, **k):
            raise RuntimeError("ffmpeg failed building the subtitle track.")

        monkeypatch.setattr(subtitle_pipeline, "build_track", _broken_build_track)
        clip = _seed_clip(tmp_path, "https://example.invalid/a", "Speedy!")
        config = {"subtitles": {"enabled": True}, "glossary": GLOSSARY_EMPTY}

        code = cli.cmd_render(tmp_path, config, "ERF | @ERFofficial")

        assert clipstore.short_path(clip).exists()
        assert code == 0
        # These notes go through the logger now, not a bare stderr print:
        # that is what lets a STUDIO render report a lost caption track at
        # all (its job thread has no reader for stderr). A CLI run on a TTY
        # still shows them - logsetup adds a stdout handler - and they now
        # also persist in the workspace's central log.
        assert "RuntimeError" in caplog.text
        assert "ffmpeg failed building the subtitle track" in caplog.text

    def test_keyboard_interrupt_is_not_swallowed(self, cli, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "build_short", _build_short_stub)
        monkeypatch.setattr(
            subtitle_pipeline, "transcribe",
            lambda *a, **k: [{"start": 0.0, "end": 1.0, "text": "hi"}],
        )

        def _interrupt(*a, **k):
            raise KeyboardInterrupt()

        monkeypatch.setattr(subtitle_pipeline, "build_track", _interrupt)
        _seed_clip(tmp_path, "https://example.invalid/a", "Speedy!")
        config = {"subtitles": {"enabled": True}, "glossary": GLOSSARY_EMPTY}

        with pytest.raises(KeyboardInterrupt):
            cli.cmd_render(tmp_path, config, "ERF | @ERFofficial")

    def test_render_failure_itself_still_fails_the_candidate(
        self, cli, monkeypatch, tmp_path
    ):
        """Contrast case: a failure of build_short ITSELF (not the
        subtitle pipeline inside it) must keep failing the candidate,
        exactly as before - only the subtitle layer degrades."""
        def _broken_build_short(*a, **k):
            raise RuntimeError("yt-dlp failed")

        monkeypatch.setattr(cli, "build_short", _broken_build_short)
        clip = _seed_clip(tmp_path, "https://example.invalid/a", "Speedy!")
        config = {"subtitles": {"enabled": True}, "glossary": GLOSSARY_EMPTY}

        code = cli.cmd_render(tmp_path, config, "ERF | @ERFofficial")

        assert code != 0
        assert not clipstore.short_path(clip).exists()


class TestSubtitleWorkDirIsolationAndCleanup:
    """Findings F2 and B1, ported onto the clip store.

    F2 - the original trigger for a separate "isolation" class - no longer
    has a way to occur. In the flat-clips.json era, every clip's raw file,
    overlay and subtitle work dir lived in ONE shared raw/ directory,
    named from the hook's slug; a hook slugifying to "caption-*" could
    have its own raw/<name>.overlay.png swept up by build_track's own
    "clear stale caption-*.png" glob, because both lived in the same
    directory under overlapping names. On the clip store every clip has
    its OWN directory (raw.mp4, overlay.png, short.mp4, transcript.json,
    ... all live directly in it, under FIXED names, never derived from the
    hook), and the subtitle work dir is always the fixed "subs/"
    subdirectory of that - a name that can never collide with the clip's
    other files, and that no hook text can influence at all. There is no
    longer any hook that reproduces the collision, so no test is ported
    for it specifically. What still needs proving, and is exercised
    below, is the underlying guarantee both classes protected: the real
    subtitle pipeline (yt-dlp and transcription stubbed, everything else
    real - ffmpeg, build_track) works end to end on the clip store, and
    B1's guarantee that a clip's subs/ work dir is removed after a
    successful build but preserved (with its PNGs) after a failed one for
    troubleshooting."""

    def _real_pipeline(self, monkeypatch, tmp_path, *, intercept=None):
        """Shared setup: a real short source clip, yt-dlp stubbed to hand
        it back instead of downloading, transcription stubbed with one
        real word so group_words/build_track actually run for real.

        ``subprocess`` is a single shared module object - yt_shorts.render
        and yt_shorts.subtitle_track both do `import subprocess` and get
        the SAME module, so a second, separate `monkeypatch.setattr(...,
        "run", ...)` would silently replace this one instead of adding to
        it. ``intercept``, if given, is therefore composed into the SAME
        stub instead: tried first, and only falls through to the yt-dlp
        redirection (then real subprocess.run) when it declines by
        returning None.
        """
        source_video = tmp_path / "source.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(source_video),
        ], check=True)

        real_run = subprocess.run

        def _stub(command, *args, **kwargs):
            if intercept is not None:
                result = intercept(command, *args, **kwargs)
                if result is not None:
                    return result
            if Path(command[0]).name == "yt-dlp":
                target = Path(command[command.index("-o") + 1])
                shutil.copyfile(source_video, target)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(render_module.subprocess, "run", _stub)
        return real_run

    def test_the_work_dir_is_gone_after_a_successful_build(self, cli, monkeypatch, tmp_path):
        profile = profile_load("erf/community-clips-back-catalogue")
        config = dict(profile.config)
        config["subtitles"] = {"enabled": True}
        self._real_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(
            subtitle_pipeline, "transcribe",
            lambda *a, **k: [{"start": 0.0, "end": 1.0, "text": "hi"}],
        )
        clip = _seed_clip(tmp_path, "https://example.invalid/a", "Speedy!")

        code = cli.cmd_render(tmp_path, config, profile.channel["footer"])

        assert code == 0
        assert clipstore.short_path(clip).exists()
        assert not clipstore.subs_work_dir(clip).exists()

    def test_the_work_dir_survives_a_failed_track_build_for_troubleshooting(
        self, cli, monkeypatch, tmp_path, capsys, caplog):
        profile = profile_load("erf/community-clips-back-catalogue")
        config = dict(profile.config)
        config["subtitles"] = {"enabled": True}

        def _fail_track_ffmpeg(command, *args, **kwargs):
            # subtitle_track.build_track's own ffmpeg call is the only one
            # in this pipeline that names the qtrle codec - render.compose
            # uses libx264, and yt-dlp is stubbed away by _real_pipeline.
            if "qtrle" in command:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="forced failure")
            return None  # decline - let _real_pipeline's own stub handle it

        self._real_pipeline(monkeypatch, tmp_path, intercept=_fail_track_ffmpeg)
        monkeypatch.setattr(
            subtitle_pipeline, "transcribe",
            lambda *a, **k: [{"start": 0.0, "end": 1.0, "text": "hi"}],
        )
        clip = _seed_clip(tmp_path, "https://example.invalid/a", "Speedy!")

        code = cli.cmd_render(tmp_path, config, profile.channel["footer"])

        assert code == 0, "a subtitle-pipeline failure must still let the clip render"
        assert clipstore.short_path(clip).exists()
        work_dir = clipstore.subs_work_dir(clip)
        assert work_dir.exists(), "the failed build's work dir must survive for troubleshooting"
        assert list(work_dir.glob("caption-*.png")), (
            "the PNGs the failed build wrote must survive too"
        )
        # These notes go through the logger now, not a bare stderr print:
        # that is what lets a STUDIO render report a lost caption track at
        # all (its job thread has no reader for stderr). A CLI run on a TTY
        # still shows them - logsetup adds a stdout handler - and they now
        # also persist in the workspace's central log.
        assert "no subtitles" in caplog.text
        assert "RuntimeError" in caplog.text


class TestHarvestWritesTheClipStore:
    def test_each_clip_gets_its_own_directory(self, cli, tmp_path, monkeypatch):
        import yt_shorts.clipstore as clipstore
        (tmp_path / "clip_urls.json").write_text(json.dumps([
            {"url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!"},
            {"url": "https://www.youtube.com/clip/BBB", "hook": "Barbie"},
        ]), encoding="utf-8")

        def fake_harvest(entries, ytdlp="yt-dlp"):
            for e in entries:
                yield cli.ClipEntry(url=e["url"], hook=e["hook"],
                                    source_title="ERF", start=1.0, end=2.0,
                                    duration=1.0, error=None)

        monkeypatch.setattr(cli, "harvest", fake_harvest)
        assert cli.cmd_harvest(tmp_path) == 0

        directories = clipstore.iter_clip_dirs(tmp_path)
        assert len(directories) == 2
        assert {clipstore.read_clip(d)["hook"] for d in directories} == {
            "Speedy!", "Barbie"}

    def test_a_good_entry_is_not_re_queried_on_a_second_run(self, cli, tmp_path, monkeypatch):
        # Deviation from the brief's literal version of this test: calling
        # cmd_harvest twice in a row cannot distinguish old from new
        # behaviour here, because BOTH the old clips.json path and the new
        # clip-store path short-circuit harvest() once an entry has been
        # written by their OWN first run - the test passed unmodified even
        # against the pre-change code (see task-6-report.md). Seeding the
        # clip store directly, bypassing cmd_harvest entirely, and calling
        # it only ONCE is what actually exercises whether cmd_harvest reads
        # its "already resolved" entries from clipstore rather than from
        # clips.json.
        import yt_shorts.clipstore as clipstore
        (tmp_path / "clip_urls.json").write_text(json.dumps([
            {"url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!"},
        ]), encoding="utf-8")
        clipstore.write_clip(tmp_path, {
            "url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None,
        })

        calls = []

        def fake_harvest(entries, ytdlp="yt-dlp"):
            calls.append(list(entries))
            for e in entries:
                yield cli.ClipEntry(url=e["url"], hook=e["hook"],
                                    source_title="ERF", start=1.0, end=2.0,
                                    duration=1.0, error=None)

        monkeypatch.setattr(cli, "harvest", fake_harvest)
        cli.cmd_harvest(tmp_path)
        # harvest() must not be called at all: the clip store already holds
        # a resolved entry for this url.
        assert calls == []

    def test_a_hand_edited_title_is_not_overwritten_by_a_second_harvest(
            self, cli, tmp_path, monkeypatch):
        import yt_shorts.clipstore as clipstore
        import yt_shorts.editorial as editorial
        (tmp_path / "clip_urls.json").write_text(json.dumps([
            {"url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!"},
        ]), encoding="utf-8")

        def fake_harvest(entries, ytdlp="yt-dlp"):
            for e in entries:
                yield cli.ClipEntry(url=e["url"], hook=e["hook"],
                                    source_title="ERF", start=1.0, end=2.0,
                                    duration=1.0, error=None)

        monkeypatch.setattr(cli, "harvest", fake_harvest)
        cli.cmd_harvest(tmp_path)
        directory = clipstore.iter_clip_dirs(tmp_path)[0]
        editorial.save(directory, editorial.Edit(
            title="Abschied von Speedy", status=editorial.CANDIDATE,
            transcript=None))

        cli.cmd_harvest(tmp_path)

        assert editorial.load(directory).title == "Abschied von Speedy"


class TestRenderUsesTheEditorialLayer:
    def _event(self, tmp_path):
        import yt_shorts.clipstore as clipstore
        clipstore.write_clip(tmp_path, {
            "url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        return clipstore.iter_clip_dirs(tmp_path)[0]

    def test_the_overridden_title_is_used_as_the_hook(self, cli, tmp_path, monkeypatch):
        import yt_shorts.editorial as editorial
        directory = self._event(tmp_path)
        editorial.save(directory, editorial.Edit(
            title="Abschied von Speedy", status=editorial.CANDIDATE,
            transcript=None))

        seen = []

        def stub(source, hook, footer, target, config, work_dir,
                 keep_raw=False, subtitle_provider=None):
            seen.append(hook)
            Path(target).write_bytes(b"x")
            return target

        monkeypatch.setattr(cli, "build_short", stub)
        assert cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER") == 0
        assert seen == ["Abschied von Speedy"]

    def test_a_discarded_clip_is_not_rendered(self, cli, tmp_path, monkeypatch):
        import yt_shorts.editorial as editorial
        directory = self._event(tmp_path)
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.DISCARDED, transcript=None))

        def stub(*a, **k):
            raise AssertionError("a discarded clip must not be rendered")

        monkeypatch.setattr(cli, "build_short", stub)
        assert cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER") == 0

    def test_the_short_lands_in_the_clips_directory(self, cli, tmp_path, monkeypatch):
        import yt_shorts.clipstore as clipstore
        directory = self._event(tmp_path)

        def stub(source, hook, footer, target, config, work_dir,
                 keep_raw=False, subtitle_provider=None):
            Path(target).write_bytes(b"x")
            return target

        monkeypatch.setattr(cli, "build_short", stub)
        cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER")
        assert clipstore.short_path(directory).exists()

    def test_a_broken_editorial_file_fails_only_that_clip(self, cli, tmp_path, monkeypatch):
        import yt_shorts.editorial as editorial
        directory = self._event(tmp_path)
        (directory / editorial.EDIT_FILENAME).write_text("{not json",
                                                         encoding="utf-8")

        def stub(*a, **k):
            raise AssertionError("must not render a clip whose edit is unreadable")

        monkeypatch.setattr(cli, "build_short", stub)
        assert cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER") == 1


class TestCmdRenderAppliesTheTrim:
    """Mirrors yt_shorts.studio.jobs's TestRenderAppliesTheTrim for the CLI
    path: cmd_render wires trim.forget_applied + trim.ensure_applied around
    build_short exactly the way the studio's _render_one does, but nothing
    pinned that here - a reviewer swapped the two lines and deleted them
    outright and all of test_cli.py still passed. That is the exact bug
    this test exists to catch.

    A render writes an UNTRIMMED short.mp4. Without re-applying, the
    operator's trim would silently vanish on every re-render - and the
    stale master (short.full.mp4) from before this render must go first,
    via forget_applied, or ensure_applied would cut from the OLD
    composition instead of the one just built. forget_applied deletes the
    master as well as the state file, which is why it must run first.
    """

    def test_forgets_the_old_master_then_re_applies_the_trim(
            self, cli, tmp_path, monkeypatch):
        directory = clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/a", "hook": "Speedy!",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE, transcript=None,
            trim=(3.0, 2.0)))

        def stub(source, hook, footer, target, config, work_dir,
                 keep_raw=False, subtitle_provider=None):
            Path(target).write_bytes(b"x")
            return target

        monkeypatch.setattr(cli, "build_short", stub)

        calls = []
        monkeypatch.setattr(
            cli.trim, "forget_applied",
            lambda d: calls.append(("forget", Path(d).name)))
        monkeypatch.setattr(
            cli.trim, "ensure_applied",
            lambda d, e, **kw: calls.append(("apply", Path(d).name, e.trim)) or True)

        code = cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER")

        assert code == 0
        assert [c[0] for c in calls] == ["forget", "apply"], calls
        assert calls[1][1] == directory.name
        assert calls[1][2] == (3.0, 2.0)


class TestSubtitleConflictIsReported:
    def test_a_stale_correction_is_used_and_reported(self, cli, tmp_path, monkeypatch, capsys, caplog):
        import yt_shorts.clipstore as clipstore
        import yt_shorts.editorial as editorial
        clipstore.write_clip(tmp_path, {
            "url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        directory = clipstore.iter_clip_dirs(tmp_path)[0]

        derived = [{"start": 0.0, "end": 0.5, "text": " very"}]
        corrected = [{"start": 0.0, "end": 0.5, "text": " Rei Racing"}]
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE,
            transcript={"based_on": editorial.checksum(derived),
                        "words": corrected}))

        changed = derived + [{"start": 0.5, "end": 1.0, "text": " more"}]
        monkeypatch.setattr(subtitle_pipeline, "transcribe", lambda *a, **k: changed)

        used = []
        monkeypatch.setattr(subtitle_pipeline, "group_words",
                            lambda words, **k: used.append(words) or [])

        def stub(source, hook, footer, target, config, work_dir,
                 keep_raw=False, subtitle_provider=None):
            if subtitle_provider is not None:
                subtitle_provider(str(work_dir) + "/raw.mp4")
            Path(target).write_bytes(b"x")
            return target

        monkeypatch.setattr(cli, "build_short", stub)
        cli.cmd_render(tmp_path, {"subtitles": {"enabled": True}, "glossary": GLOSSARY_EMPTY}, "FOOTER")

        assert used == [corrected]                     # hand work wins
        # These notes go through the logger now, not a bare stderr print:
        # that is what lets a STUDIO render report a lost caption track at
        # all (its job thread has no reader for stderr). A CLI run on a TTY
        # still shows them - logsetup adds a stdout handler - and they now
        # also persist in the workspace's central log.
        assert "conflict" in caplog.text.lower()


class TestCmdGalleryReadsTheClipStore:
    """cmd_gallery, ported from a flat glob over drafts/*.mp4 (with the
    hook read back off the filename, dashes turned back into spaces - a
    lossy round trip) to the clip store + editorial layer: the true hook
    (editorial-overridden title if the operator set one, otherwise the
    harvested one) comes straight from clip.json/edit.json, and a
    discarded clip is excluded from the page the same way cmd_render
    excludes it from rendering."""

    def test_a_rendered_clip_appears_with_its_effective_title(self, cli, tmp_path):
        directory = clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/a", "hook": "Speedy!",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        editorial.save(directory, editorial.Edit(
            title="Abschied von Speedy", status=editorial.CANDIDATE,
            transcript=None))
        clipstore.short_path(directory).write_bytes(b"x")

        cli.cmd_gallery(tmp_path)

        html = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "Abschied von Speedy" in html
        assert f"{clipstore.CLIPS_DIRNAME}/{directory.name}/short.mp4" in html

    def test_a_clip_with_no_rendered_short_is_not_listed(self, cli, tmp_path):
        clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/a", "hook": "Not rendered yet",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        # No short.mp4 written for this one.
        # A second, rendered clip is a positive control: without it, an
        # empty page (e.g. from cmd_gallery silently listing nothing at
        # all) would make the assertion below pass for the wrong reason.
        rendered = clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/b", "hook": "Rendered clip",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        clipstore.short_path(rendered).write_bytes(b"x")

        cli.cmd_gallery(tmp_path)

        html = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "Not rendered yet" not in html
        assert "Rendered clip" in html

    def test_a_discarded_clip_is_not_listed_even_if_rendered(self, cli, tmp_path):
        directory = clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/a", "hook": "Discarded but rendered",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        clipstore.short_path(directory).write_bytes(b"x")
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.DISCARDED, transcript=None))
        # A second, kept clip is a positive control - same reasoning as
        # above.
        kept = clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/b", "hook": "Kept and rendered",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        clipstore.short_path(kept).write_bytes(b"x")

        cli.cmd_gallery(tmp_path)

        html = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "Discarded but rendered" not in html
        assert "Kept and rendered" in html

    def test_a_corrupt_edit_json_does_not_abort_the_gallery(self, cli, tmp_path, capsys):
        """Finding 7, ported onto the clip store: a corrupt edit.json in
        the middle of rendered clips must not abort the gallery command or
        lose the good clips. The two good clips must appear in index.html,
        and a NOTE must name the broken clip and its reason."""
        first = clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/a", "hook": "First good clip",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        clipstore.short_path(first).write_bytes(b"x")

        broken = clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/b", "hook": "Broken edit",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        clipstore.short_path(broken).write_bytes(b"x")
        (broken / editorial.EDIT_FILENAME).write_text("{not json",
                                                       encoding="utf-8")

        second = clipstore.write_clip(tmp_path, {
            "url": "https://example.invalid/c", "hook": "Second good clip",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        clipstore.short_path(second).write_bytes(b"x")

        code = cli.cmd_gallery(tmp_path)

        # index.html must exist
        html = (tmp_path / "index.html").read_text(encoding="utf-8")
        # Both good clips must appear
        assert "First good clip" in html
        assert "Second good clip" in html
        # The broken clip's hook is skipped, not listed (since editorial
        # could not be read to determine its status)
        assert "Broken edit" not in html
        # The error must be reported on stderr with the exception type
        out = capsys.readouterr()
        assert "EditError" in out.err
        assert broken.name in out.err or "Broken edit" in out.err
        # The gallery command must succeed (exit code 0) since the problem
        # is recoverable and index.html was written with good clips
        assert code == 0


class TestCmdStudio:
    """cmd_studio imports FastAPI/uvicorn INSIDE itself (see the function's
    own docstring) so the other four commands stay usable without them -
    the other tests in this file (and CLAUDE.md's own constraint) already
    cover that harvest/render/gallery/migrate import cleanly without
    FastAPI installed; these two tests cover cmd_studio's own two paths
    without ever calling the real (blocking) uvicorn.run."""

    def test_reports_what_to_install_when_fastapi_or_uvicorn_is_missing(
            self, cli, monkeypatch, capsys):
        # sys.modules[name] = None is what makes `import uvicorn` (called
        # from inside cmd_studio) raise ImportError, without touching the
        # real package actually installed in this venv.
        import sys
        monkeypatch.setitem(sys.modules, "uvicorn", None)

        code = cli.cmd_studio("erf/community-clips-back-catalogue")

        assert code == 2
        err = capsys.readouterr().err
        assert "pip install fastapi uvicorn" in err
        assert "harvest, render, gallery, migrate" in err

    def test_starts_the_workspace_app_with_the_scoped_routes(self, cli, monkeypatch):
        import uvicorn

        captured = {}

        def fake_run(app, *, host, port, log_level):
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port

        monkeypatch.setattr(uvicorn, "run", fake_run)

        # No profile argument any more: the studio is workspace-level and
        # resolves each event from the URL path (see studio.api.create_app).
        # open_url is injected so the test never launches a real browser.
        code = cli.cmd_studio("erf/community-clips-back-catalogue",
                              open_url=lambda _url: None)

        assert code == 0
        assert captured["host"] == "127.0.0.1"
        # The real app built by create_app() - not a stand-in - confirmed by
        # its own workspace-level routes, the same routes test_studio_api.py
        # exercises against a TestClient.
        paths = {route.path for route in captured["app"].routes}
        assert "/api/channels" in paths
        assert "/api/channels/{channel}/events/{event}/render" in paths

    def test_launches_with_no_identifier(self, cli, monkeypatch):
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        # `bin/yt-shorts studio` with no channel/event opens the start screen.
        assert cli.cmd_studio(None, open_url=lambda _url: None) == 0

    def test_opens_the_browser_at_the_served_url(self, cli, monkeypatch):
        import uvicorn
        captured = {}
        monkeypatch.setattr(uvicorn, "run",
                            lambda app, **kw: captured.update(port=kw["port"]))
        opened = []
        # cmd_studio opens the operator's browser at the URL it serves (racecast
        # does the same for its UI) - injected here so the test asserts the URL
        # instead of spawning a real browser.
        cli.cmd_studio("erf/community-clips-back-catalogue",
                       open_url=opened.append)

        assert len(opened) == 1
        assert opened[0] == f"http://127.0.0.1:{captured['port']}/erf/community-clips-back-catalogue"

    def test_opens_the_start_screen_url_when_no_identifier(self, cli, monkeypatch):
        import uvicorn
        captured = {}
        monkeypatch.setattr(uvicorn, "run",
                            lambda app, **kw: captured.update(port=kw["port"]))
        opened = []
        cli.cmd_studio(None, open_url=opened.append)
        assert opened == [f"http://127.0.0.1:{captured['port']}/"]

    @staticmethod
    def _own_workspace(tmp_path, monkeypatch):
        """Point the studio at a workspace of this test's own.

        The suite's autouse fixture shares ONE workspace root for the whole
        session, and these tests start a real worker thread against whatever
        `jobs.json` it finds there - so an entry another test planted would
        be picked up and actually run. Imported inside the method for the
        same reason cmd_studio imports it inside itself: this file must never
        be what first drags FastAPI into a run.

        BOTH resolvers are pointed at it, and that is not belt-and-braces:
        `studio.api` holds its own already-bound `_resolve_workspace` (see
        conftest's own note on the three names), while `cmd_studio` calls
        `workspace.resolve()` through the module to find the workspace it
        must LOCK. Patching only the first would leave the studio lock on
        the session-wide root.
        """
        import yt_shorts.studio.api as api
        from yt_shorts import workspace as workspace_module
        root = tmp_path / "ws"
        (root / "channels").mkdir(parents=True)
        fixed = workspace_module.Workspace(root=root, channels_dir=root / "channels",
                                           origin="test")
        monkeypatch.setattr(api, "_resolve_workspace", lambda: fixed)
        monkeypatch.setattr(workspace_module, "resolve", lambda *a, **k: fixed)
        return root

    def test_the_queue_worker_runs_while_the_server_serves(
            self, cli, tmp_path, monkeypatch):
        """This is the ONLY place in the product that starts the job
        queue's worker - `create_app()` deliberately does not (see
        studio/worker.py's docstring, and the three tests in
        tests/test_studio_worker.py that pin the mirror property). Deleting
        the two lines from cmd_studio left the whole suite green: the queue
        would simply never run, and nothing anywhere said so.

        `uvicorn.run` stands in for "the server is serving", so the worker
        is asked whether it is running at the one moment it must be.
        """
        import uvicorn
        self._own_workspace(tmp_path, monkeypatch)
        observed = {}

        def fake_run(app, **kwargs):
            worker = app.state.worker
            observed["worker"] = worker
            observed["running_while_serving"] = worker.is_running()

        monkeypatch.setattr(uvicorn, "run", fake_run)

        assert cli.cmd_studio(None, open_url=lambda _url: None) == 0

        assert observed["running_while_serving"] is True, (
            "the studio served requests with the job queue's worker stopped")
        assert observed["worker"].is_running() is False, (
            "the worker thread outlived the server it belongs to")

    def test_the_worker_is_stopped_even_when_the_server_dies(
            self, cli, tmp_path, monkeypatch):
        # The stop sits in a `finally` for this case: uvicorn failing to
        # bind, or a Ctrl-C, must not leave a thread draining the queue
        # (starting renders, taking event locks) after the studio is gone.
        import uvicorn
        self._own_workspace(tmp_path, monkeypatch)
        seen = {}

        def exploding_run(app, **kwargs):
            seen["worker"] = app.state.worker
            raise RuntimeError("address already in use")

        monkeypatch.setattr(uvicorn, "run", exploding_run)

        with pytest.raises(RuntimeError):
            cli.cmd_studio(None, open_url=lambda _url: None)

        assert seen["worker"].is_running() is False

    def test_a_second_studio_on_one_workspace_is_refused_loudly(
            self, cli, tmp_path, monkeypatch, capsys):
        """One studio per workspace, and the refusal has to be LOUD.

        The port is not the guard and never was: `_studio_port` deliberately
        picks a free one when 8765 is busy, so a second `bin/yt-shorts
        studio` against the same workspace used to start perfectly happily.
        The two then shared one `jobs.json`, which `JobQueue` replaces
        wholesale from a list it read once - so the second's own startup
        marked the first's running jobs `interrupted`, and either one's next
        write deleted what the other had queued, silently.

        The first studio is stood in for by its lock file (holding THIS
        process's pid, i.e. a live holder), rather than by a second server:
        what is under test is the refusal, not uvicorn.
        """
        import uvicorn
        from yt_shorts import lock as lock_module
        root = self._own_workspace(tmp_path, monkeypatch)
        lock_module.StudioLock(root).path.write_text(str(os.getpid()), encoding="utf-8")

        def must_not_run(*args, **kwargs):
            raise AssertionError("the second studio served requests anyway")

        monkeypatch.setattr(uvicorn, "run", must_not_run)

        code = cli.cmd_studio(None, open_url=lambda _url: None)

        assert code == 2
        err = capsys.readouterr().err
        assert "already running" in err
        assert str(os.getpid()) in err          # names what holds the workspace
        assert str(root) in err

    def test_the_studio_releases_its_workspace_lock_on_the_way_out(
            self, cli, tmp_path, monkeypatch):
        # Held for exactly as long as the server serves, and released in a
        # `finally` - a studio that crashed leaving its lock behind would
        # still be recoverable (the stale-takeover path), but an ordinary
        # exit must not need it.
        import uvicorn
        from yt_shorts import lock as lock_module
        root = self._own_workspace(tmp_path, monkeypatch)
        held = {}

        def fake_run(app, **kwargs):
            held["while_serving"] = lock_module.StudioLock(root).is_held()

        monkeypatch.setattr(uvicorn, "run", fake_run)

        assert cli.cmd_studio(None, open_url=lambda _url: None) == 0

        assert held["while_serving"] is True, (
            "the studio served without holding its workspace lock")
        assert lock_module.StudioLock(root).is_held() is False, (
            "the workspace stayed locked after the server exited")

    def test_falls_back_to_a_free_port_when_the_preferred_one_is_busy(self, cli):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            busy = taken.getsockname()[1]
            notes = []
            chosen = cli._studio_port(busy, log=notes.append)
            assert chosen != busy                       # did not reuse the busy port
            assert any(str(busy) in n for n in notes)   # and said so

    def test_uses_the_preferred_port_when_it_is_free(self, cli):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        # Nothing is listening on `free` now (the socket above is closed).
        assert cli._studio_port(free, log=lambda _m: None) == free


class FakeOAuthCLI:
    """A stand-in for the Google adapter in cmd_auth: no network. Its
    ``authorized_channel`` returns the channel.json id the test uses ("UCabc")
    so authorize()'s verify passes on the happy path."""
    def __init__(self, granted_channel="UCabc"):
        self.consented = 0
        self.granted_channel = granted_channel

    def run_consent(self, client_secret_path, scopes):
        self.consented += 1
        return {"token": "fresh"}

    def authorized_channel(self, creds):
        return self.granted_channel, "Test Channel"

    def to_json(self, creds):
        import json as _json
        return _json.dumps(creds)

    def from_json(self, text):
        import json as _json
        return _json.loads(text)

    def valid(self, creds):
        return True

    def ensure_fresh(self, creds):
        return creds


class TestCmdAuth:
    def _channel(self, channels_dir):
        (channels_dir / "erf").mkdir(parents=True)
        (channels_dir / "erf" / "channel.json").write_text(
            json.dumps({"id": "UCabc", "handle": "@ERFofficial"}), encoding="utf-8")

    def test_authorizes_and_reports_the_channel(self, cli, tmp_path, capsys):
        channels_dir = tmp_path / "channels"
        self._channel(channels_dir)
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        (auth_dir / "client_secret.json").write_text("{}", encoding="utf-8")
        oauth = FakeOAuthCLI()
        code = cli.cmd_auth("erf", channels_dir, auth_dir,
                            oauth=oauth, require=lambda feature: None)
        assert code == 0
        assert oauth.consented == 1
        assert (auth_dir / "token-UCabc.json").exists()
        assert "UCabc" in capsys.readouterr().out

    def test_render_only_channel_is_refused_before_any_consent(self, cli, tmp_path, capsys):
        channels_dir = tmp_path / "channels"
        self._channel(channels_dir)
        (channels_dir / "erf" / "brand.json").write_text(
            json.dumps({"upload": {"mode": "manual"}}), encoding="utf-8")
        oauth = FakeOAuthCLI()
        code = cli.cmd_auth("erf", channels_dir, tmp_path / "auth",
                            oauth=oauth, require=lambda feature: None)
        assert code == 2
        assert oauth.consented == 0                       # never reached consent
        assert "render-only" in capsys.readouterr().err

    def test_missing_google_libraries_is_reported(self, cli, tmp_path):
        from yt_shorts._google import GoogleUnavailable
        channels_dir = tmp_path / "channels"
        self._channel(channels_dir)

        def boom(feature):
            raise GoogleUnavailable("install it")

        code = cli.cmd_auth("erf", channels_dir, tmp_path / "auth",
                            oauth=FakeOAuthCLI(), require=boom)
        assert code == 2


class TestCmdUpload:
    def test_uploads_kept_rendered_and_records_it(self, cli, tmp_path, capsys):
        event_dir = tmp_path / "event"
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"mp4")
        editorial.save(directory, editorial.Edit(title=None, status="kept", transcript=None))

        calls = []

        def fake_upload_one(d, clip, edit):
            calls.append(d.name)
            return {"video_id": "VID1", "url": "https://youtu.be/VID1"}

        code = cli.cmd_upload(event_dir, {}, {"id": "UCabc"}, tmp_path / "auth",
                              "erf", upload_one=fake_upload_one)
        assert code == 0
        assert calls == [directory.name]
        assert "VID1" in capsys.readouterr().out

    def test_render_only_channel_uploads_nothing(self, cli, tmp_path, capsys):
        event_dir = tmp_path / "event"
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"mp4")
        editorial.save(directory, editorial.Edit(title=None, status="kept", transcript=None))
        calls = []

        def fake_upload_one(d, clip, edit):
            calls.append(d.name)
            return {"video_id": "X", "url": "u"}

        code = cli.cmd_upload(event_dir, {"upload": {"mode": "manual"}},
                              {"id": "UCabc"}, tmp_path / "auth", "erf",
                              upload_one=fake_upload_one)
        assert code == 2
        assert calls == []                                # no clip was uploaded
        assert "render-only" in capsys.readouterr().err

    def test_skips_a_clip_with_a_trim_not_yet_applied(self, cli, tmp_path, capsys):
        # Reproduces the branch-review finding: `edit.trim` is saved (the
        # studio PATCHes it in before the apply job even starts), but
        # short.trim.json never landed - an over-trim, an ffmpeg failure, or
        # simply nobody having run the apply job yet. `trim.is_pending` is
        # the same check the studio's own post_upload route makes; the CLI
        # used to call it nowhere at all, so bin/yt-shorts upload shipped
        # the untrimmed short.mp4 to the channel.
        event_dir = tmp_path / "event"
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-84", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 84.0,
            "duration": 84.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"UNTRIMMED-84-SECOND-SHORT")
        editorial.save(directory, editorial.Edit(
            title=None, status="kept", transcript=None, trim=(2.0, 3.0)))

        calls = []

        def fake_upload_one(d, clip, edit):
            calls.append(d.name)
            return {"video_id": "VID1", "url": "https://youtu.be/VID1"}

        code = cli.cmd_upload(event_dir, {}, {"id": "UCabc"}, tmp_path / "auth",
                              "erf", upload_one=fake_upload_one)
        assert code == 0
        assert calls == []                                 # nothing was uploaded
        err = capsys.readouterr().err
        assert "trim not applied" in err
        from yt_shorts import upload_record
        assert not upload_record.is_uploaded(directory)     # no false "uploaded" record

    def test_skips_a_clip_whose_applied_trim_state_is_unknown(self, cli, tmp_path, capsys):
        # THE BLOCKER's third delivery path: short.full.mp4 (a real master)
        # survives beside a CUT short.mp4 with no readable short.trim.json -
        # a crash between the cut landing and its state being recorded, or a
        # deleted/corrupted sidecar. edit.trim is untouched (None), so before
        # trim.is_unknown existed this read as "nothing pending" (None vs
        # None) and cmd_upload shipped the cut file as the "full" render.
        from yt_shorts import trim
        event_dir = tmp_path / "event"
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-84", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 84.0,
            "duration": 84.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"CUT-BUT-LOOKS-FINE")
        clipstore.short_master_path(directory).write_bytes(b"FULL-MASTER")
        editorial.save(directory, editorial.Edit(
            title=None, status="kept", transcript=None))

        calls = []

        def fake_upload_one(d, clip, edit):
            calls.append(d.name)
            return {"video_id": "VID1", "url": "https://youtu.be/VID1"}

        assert trim.is_unknown(directory) is True    # the state this test targets
        code = cli.cmd_upload(event_dir, {}, {"id": "UCabc"}, tmp_path / "auth",
                              "erf", upload_one=fake_upload_one)
        assert code == 0
        assert calls == []                                 # nothing was uploaded
        err = capsys.readouterr().err
        assert "trim not applied" in err
        from yt_shorts import upload_record
        assert not upload_record.is_uploaded(directory)

    def test_skips_candidate_and_already_uploaded(self, cli, tmp_path):
        from yt_shorts import upload_record
        event_dir = tmp_path / "event"
        cand = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "x", "source_title": "y", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(cand).write_bytes(b"mp4")
        editorial.save(cand, editorial.Edit(title=None, status="candidate", transcript=None))
        done = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/20-32", "video_id": "vid",
            "hook": "x", "source_title": "y", "start": 20.0, "end": 32.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(done).write_bytes(b"mp4")
        editorial.save(done, editorial.Edit(title=None, status="kept", transcript=None))
        upload_record.save(done, "OLD", "https://youtu.be/OLD", "private",
                           when="2026-07-22T00:00:00Z")

        calls = []
        code = cli.cmd_upload(event_dir, {}, {"id": "UCabc"}, tmp_path / "auth",
                              "erf", upload_one=lambda d, c, e: calls.append(d.name))
        assert code == 0
        assert calls == []   # candidate skipped, already-uploaded skipped


def _stream(video_id, title):
    from yt_shorts.youtube import Stream
    return Stream(video_id, title, 600, 1000)


class TestCmdDetect:
    # An older brief's own snippet loaded bin/yt-shorts via
    # importlib.util.spec_from_file_location("ytshorts_cli", "bin/yt-shorts"),
    # which returns None (AttributeError: 'NoneType' has no 'loader') because
    # that path has no importable extension and spec_from_file_location can't
    # guess a loader for it - confirmed by actually running it. The CLI now
    # lives in the package proper, so the file's own `cli` fixture above
    # (_load_cli, a plain `import yt_shorts.cli`) is the working technique
    # every other test in this module already relies on.
    def test_reports_the_analysis_path_and_returns_zero(self, cli, tmp_path, capsys):
        written = tmp_path / "streams" / "vid123" / "moments.json"
        written.parent.mkdir(parents=True)
        written.write_text('{"engine": "lexicon", "moments": []}')
        (tmp_path / "event").mkdir()        # EventLock needs a real directory

        code = cli.cmd_detect(tmp_path / "event", {"lexicon": None}, "vid123",
                              tmp_path, detect_fn=lambda *a, **k: written)
        assert code == 0
        assert "moments.json" in capsys.readouterr().out

    def test_a_failure_returns_one_and_says_why(self, cli, tmp_path, capsys):
        (tmp_path / "event").mkdir()        # EventLock needs a real directory

        def boom(*a, **k):
            raise RuntimeError("yt-dlp went away")
        code = cli.cmd_detect(tmp_path / "event", {"lexicon": None}, "vid123",
                              tmp_path, detect_fn=boom)
        assert code == 1
        assert "yt-dlp went away" in capsys.readouterr().err

    def test_records_the_streams_real_title_not_its_video_id(
            self, cli, tmp_path, capsys):
        # This used to pass `stream_title=video_id` unconditionally. The
        # analysis's title is what the studio's stream screen shows as its
        # heading, and - via clip_from_moment - what a created clip carries as
        # `source_title` into an upload description template. A video id in
        # either is a wrong artifact; in the upload case, on the channel.
        written = tmp_path / "streams" / "vid123" / "moments.json"
        written.parent.mkdir(parents=True)
        written.write_text("{}")
        (tmp_path / "event").mkdir()
        seen = {}

        def record(video_id, workspace_dir, config, *, stream_title, **kwargs):
            seen["title"] = stream_title
            return written

        code = cli.cmd_detect(
            tmp_path / "event", {"lexicon": None}, "vid123", tmp_path,
            "https://www.youtube.com/@erf", detect_fn=record,
            list_fn=lambda url: [_stream("other", "Race 2"),
                                 _stream("vid123", "ERF 24H Qualifying 1")])
        assert code == 0
        assert seen["title"] == "ERF 24H Qualifying 1"

    def test_an_unlistable_channel_records_no_title_rather_than_the_id(
            self, cli, tmp_path, capsys):
        # Degrade AND announce: the run is worth finishing (detection scores a
        # transcript, not a title), but "" - not the id - is what lets the
        # studio fall back to its own stream-list lookup.
        written = tmp_path / "streams" / "vid123" / "moments.json"
        written.parent.mkdir(parents=True)
        written.write_text("{}")
        (tmp_path / "event").mkdir()
        seen = {}

        def record(video_id, workspace_dir, config, *, stream_title, **kwargs):
            seen["title"] = stream_title
            return written

        def boom(url):
            raise RuntimeError("yt-dlp went away")

        code = cli.cmd_detect(
            tmp_path / "event", {"lexicon": None}, "vid123", tmp_path,
            "https://www.youtube.com/@erf", detect_fn=record, list_fn=boom)
        assert code == 0
        assert seen["title"] == ""
        assert "could not look up the stream's title" in capsys.readouterr().err

    def test_a_stream_missing_from_the_list_records_no_title_and_says_so(
            self, cli, tmp_path, capsys):
        written = tmp_path / "streams" / "vid123" / "moments.json"
        written.parent.mkdir(parents=True)
        written.write_text("{}")
        (tmp_path / "event").mkdir()
        seen = {}

        def record(video_id, workspace_dir, config, *, stream_title, **kwargs):
            seen["title"] = stream_title
            return written

        code = cli.cmd_detect(
            tmp_path / "event", {"lexicon": None}, "vid123", tmp_path,
            "https://www.youtube.com/@erf", detect_fn=record,
            list_fn=lambda url: [_stream("other", "Race 2")])
        assert code == 0
        assert seen["title"] == ""
        assert "not in this channel's stream list" in capsys.readouterr().err

    def test_the_cli_detect_still_transcribes(self, cli, tmp_path, monkeypatch):
        # Task 4 splits transcription into its own studio job kind and makes
        # the STUDIO's detect job require a cached transcript - but
        # bin/yt-shorts detect must keep its current one-command behaviour
        # exactly: a CLI operator watching a terminal is a different
        # situation from a background job nobody is looking at (see
        # detect.py's own module docstring on the two policies).
        #
        # This drives the REAL detect_moments -> transcribe_stream chain
        # (no detect_fn override at all - the CLI's own default), with only
        # transcribe_stream's OWN downloader/decoder swapped for fakes
        # (network and a real Whisper decode are out of bounds for this
        # suite). No streams/vid123/transcript.json exists anywhere under
        # tmp_path beforehand - if a future change made the CLI path require
        # one too (mistakenly reusing the studio's policy), this would raise
        # detect.TranscriptNotCached and the test would fail.
        from yt_shorts import stream_transcribe as st
        from yt_shorts.lexicon import Lexicon

        def fake_downloader(video_id, dest_dir, **_kwargs):
            return st.DownloadedAudio(path=dest_dir / "audio.webm", duration_seconds=1.0)

        def fake_decoder(audio_path, start, length, **_kwargs):
            return [{"start": start, "end": start + 0.5, "text": " hello"}]

        monkeypatch.setitem(st.transcribe_stream.__kwdefaults__, "downloader", fake_downloader)
        monkeypatch.setitem(st.transcribe_stream.__kwdefaults__, "decoder", fake_decoder)

        (tmp_path / "event").mkdir()
        assert not (tmp_path / "streams" / "vid123" / "transcript.json").exists()

        code = cli.cmd_detect(tmp_path / "event", {"lexicon": Lexicon(markers={})},
                              "vid123", tmp_path)

        assert code == 0
        assert (tmp_path / "streams" / "vid123" / "transcript.json").exists(), (
            "the CLI must still transcribe on its own - no fresh transcript was written"
        )
        assert (tmp_path / "streams" / "vid123" / "moments.json").exists()

    def test_a_broken_pipe_while_reporting_progress_costs_only_that_reading(
            self, cli, tmp_path, monkeypatch):
        # I-1: moment_scan.scan calls its three progress(...) sites OUTSIDE
        # any try of its own (see that module's own docstring) - so an
        # unwrapped `print` here used to let a closed pipe
        # (`yt-shorts detect ... | head`) raise BrokenPipeError straight out
        # of the whole scan, aborting a paid run after one reading and
        # writing no moments.json at all. Measured, not theorised: this test
        # dies (code becomes 1, not 0) if `_report_progress`'s own try/except
        # is removed.
        written = tmp_path / "streams" / "vid123" / "moments.json"
        written.parent.mkdir(parents=True)
        written.write_text("{}")
        (tmp_path / "event").mkdir()
        captured = {}

        def record(video_id, workspace_dir, config, *, stream_title, progress, **kwargs):
            captured["progress"] = progress
            # Mirrors moment_scan.scan's own three call sites: called
            # plainly, exactly as detect_moments -> scan would call the real
            # `_report_progress` - nothing here protects the call, so
            # whatever `run`/detect_fn was actually handed is what is on
            # trial.
            progress(1, 3)
            return written

        real_print = print

        def selectively_broken_pipe(*args, **kwargs):
            text = args[0] if args else ""
            if isinstance(text, str) and text.startswith("  window"):
                raise BrokenPipeError("stdout closed")
            return real_print(*args, **kwargs)

        monkeypatch.setattr(cli, "print", selectively_broken_pipe, raising=False)

        code = cli.cmd_detect(tmp_path / "event", {"lexicon": None}, "vid123",
                              tmp_path, detect_fn=record)

        assert code == 0, (
            "a closed pipe while reporting progress must not abort the scan")
        assert captured["progress"] is not None


class TestTheEntrypointIsImportable:
    """console_scripts and PyInstaller can only call `module:function`. The
    dispatch used to live in bin/yt-shorts's __main__ block, which neither can
    reach, so `main` has to be a real importable function."""

    def test_main_is_importable_and_callable(self):
        from yt_shorts.cli import main

        assert callable(main)

    def test_main_returns_an_exit_code_rather_than_raising_systemexit(self, capsys):
        from yt_shorts.cli import main

        assert main(["definitely-not-a-command"]) == 2
        assert "yt-shorts" in capsys.readouterr().err

    def test_no_arguments_prints_the_usage_and_returns_two(self, capsys):
        from yt_shorts.cli import main

        assert main([]) == 2
        assert "harvest" in capsys.readouterr().err


class TestTheVersionFlag:
    def test_version_prints_the_resolved_version_and_returns_zero(self, capsys):
        from yt_shorts import version
        from yt_shorts.cli import main

        assert main(["--version"]) == 0
        assert version.resolve() in capsys.readouterr().out

    def test_the_short_form_works_too(self, capsys):
        from yt_shorts.cli import main

        assert main(["-V"]) == 0
        assert capsys.readouterr().out.strip()


class TestTheNoBrowserFlag:
    """cmd_studio's `open_url` is injectable as a keyword but was unreachable
    from the command line. Task 9's binary smoke test needs it: a build agent
    has no browser and must not have one opened at it.

    These reach main()'s workspace resolution, which conftest.py's autouse
    `_isolated_resolved_workspace` already points at a fixture-owned root -
    that is why none of them touch YT_SHORTS_DATA. Setting that variable would
    do nothing anyway: the fixture patches the RESOLVER, not the environment."""

    def test_no_browser_suppresses_the_opener(self, monkeypatch):
        from yt_shorts import cli

        opened = []
        monkeypatch.setattr(cli, "cmd_studio",
                            lambda identifier=None, *, open_url: opened.append(open_url) or 0)
        assert cli.main(["studio", "--no-browser"]) == 0
        # Must be a no-op lambda, NOT the real _open_browser_soon - that one
        # starts a threading.Timer and does not raise, so calling it below
        # would not fail even if the --no-browser strip regressed and this
        # assertion were missing.
        assert opened[0] is not cli._open_browser_soon
        opened[0]("http://127.0.0.1:8000/")  # the injected opener must do nothing

    def test_the_flag_is_not_mistaken_for_an_identifier(self, monkeypatch):
        """`studio` accepts 0 or 1 identifier; the flag must be stripped BEFORE
        that count is checked, or `studio --no-browser erf` would be refused."""
        from yt_shorts import cli

        seen = []
        monkeypatch.setattr(cli, "cmd_studio",
                            lambda identifier=None, *, open_url: seen.append(identifier) or 0)
        assert cli.main(["studio", "--no-browser", "erf"]) == 0
        assert seen == ["erf"]

    def test_without_the_flag_the_real_opener_is_used(self, monkeypatch):
        from yt_shorts import cli

        seen = []
        monkeypatch.setattr(cli, "cmd_studio",
                            lambda identifier=None, *, open_url: seen.append(open_url) or 0)
        assert cli.main(["studio"]) == 0
        assert seen == [cli._open_browser_soon]


class TestTheCliPutsTheManagedToolsOnPath:
    """A yt-dlp that install-tools downloaded into <workspace>/.tools is
    invisible to render.py's ["yt-dlp", ...] until this runs. One call, right
    after the workspace resolves - the studio inherits it because cmd_studio
    runs uvicorn in the same process and jobs run on its threads."""

    def test_main_calls_ensure_tool_path_with_the_workspace_root(self, monkeypatch):
        from yt_shorts import cli, workspace

        seen = []
        monkeypatch.setattr(cli.install_tools, "ensure_tool_path", seen.append)
        monkeypatch.setattr(cli, "cmd_studio", lambda identifier=None, *, open_url: 0)

        assert cli.main(["studio", "--no-browser"]) == 0
        # conftest.py's autouse _isolated_resolved_workspace already points
        # resolve() at a fixture-owned root; assert against that rather than
        # setting YT_SHORTS_DATA, which the fixture would ignore (it patches
        # the RESOLVER, not the environment).
        assert seen == [workspace.resolve().root]


class TestTheInstallToolsCommand:
    """Like TestTheNoBrowserFlag, these rely on conftest.py's autouse
    _isolated_resolved_workspace for the workspace - never on YT_SHORTS_DATA,
    which that fixture ignores."""

    def test_it_is_a_known_command(self):
        from yt_shorts import cli

        assert "install-tools" in cli.COMMANDS

    def test_it_takes_no_identifier(self, monkeypatch):
        from yt_shorts import cli

        monkeypatch.setattr(cli.install_tools, "run", lambda root, **kw: 0)
        assert cli.main(["install-tools"]) == 0

    def test_update_reaches_the_runner(self, monkeypatch):
        from yt_shorts import cli

        seen = {}
        monkeypatch.setattr(cli.install_tools, "run",
                            lambda root, **kw: seen.update(kw) or 0)
        cli.main(["install-tools", "--update"])

        assert seen["update"] is True

    def test_yes_reaches_the_runner(self, monkeypatch):
        from yt_shorts import cli

        seen = {}
        monkeypatch.setattr(cli.install_tools, "run",
                            lambda root, **kw: seen.update(kw) or 0)
        cli.main(["install-tools", "--yes"])

        assert seen["assume_yes"] is True

    def test_it_refuses_an_identifier(self, monkeypatch):
        """install-tools is workspace-level, not event-level - unlike studio,
        it takes NO identifier. `yt-shorts install-tools erf/typo` used to
        exit 0 having silently ignored the argument."""
        from yt_shorts import cli

        called = []
        monkeypatch.setattr(cli.install_tools, "run", lambda root, **kw: called.append(1) or 0)
        assert cli.main(["install-tools", "erf/typo"]) == 2
        assert called == []

    def test_update_and_yes_together_are_both_accepted(self, monkeypatch):
        from yt_shorts import cli

        seen = {}
        monkeypatch.setattr(cli.install_tools, "run",
                            lambda root, **kw: seen.update(kw) or 0)
        assert cli.main(["install-tools", "--update", "--yes"]) == 0
        assert seen["update"] is True
        assert seen["assume_yes"] is True


class TestTheDoctorCommand:
    """Like TestTheInstallToolsCommand, relies on conftest.py's autouse
    _isolated_resolved_workspace for the workspace."""

    def test_it_is_a_known_command(self):
        from yt_shorts import cli

        assert "doctor" in cli.COMMANDS

    def test_it_takes_no_identifier(self, monkeypatch):
        from yt_shorts import cli

        monkeypatch.setattr(cli.doctor, "checks", lambda root: [])
        monkeypatch.setattr(cli.doctor, "report", lambda results: 0)
        assert cli.main(["doctor"]) == 0

    def test_it_refuses_an_identifier(self, monkeypatch):
        """doctor answers "can this machine run the tool at all" - nothing to
        do with any one event's profile. `yt-shorts doctor erf/typo` used to
        exit 0 having silently ignored the argument."""
        from yt_shorts import cli

        called = []
        monkeypatch.setattr(cli.doctor, "checks", lambda root: called.append(1) or [])
        assert cli.main(["doctor", "erf/typo"]) == 2
        assert called == []

    def test_it_refuses_a_flag_no_command_of_its_own_accepts(self):
        """doctor accepts none of the three flags stripped in main() - it is
        neither browser-launching nor a package-manager runner."""
        from yt_shorts import cli

        assert cli.main(["doctor", "--update"]) == 2


class TestFlagsAreRefusedByCommandsThatDoNotAcceptThem:
    """M2: the three flags used to be stripped GLOBALLY, so every command
    silently accepted and ignored them - `yt-shorts render erf/ev --update`
    rendered, refreshed no yt-dlp, and said nothing, which is the precise
    failure the managed yt-dlp exists to prevent."""

    def test_render_does_not_accept_update(self):
        from yt_shorts import cli

        assert cli.main(["render", "erf/community-clips-back-catalogue", "--update"]) == 2

    def test_harvest_does_not_accept_no_browser(self):
        from yt_shorts import cli

        assert cli.main(["harvest", "erf/community-clips-back-catalogue", "--no-browser"]) == 2

    def test_studio_still_accepts_no_browser(self, monkeypatch):
        from yt_shorts import cli

        monkeypatch.setattr(cli, "cmd_studio", lambda identifier=None, *, open_url: 0)
        assert cli.main(["studio", "--no-browser"]) == 0

    def test_install_tools_still_accepts_update_and_yes(self, monkeypatch):
        from yt_shorts import cli

        monkeypatch.setattr(cli.install_tools, "run", lambda root, **kw: 0)
        assert cli.main(["install-tools", "--update", "--yes"]) == 0
