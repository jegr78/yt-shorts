import json

import pytest

from yt_shorts import workspace as workspace_module
from yt_shorts import workspaces
from yt_shorts.workspace import DEFAULT_DIR_NAME, WorkspaceError, resolve


class TestResolutionOrder:
    def test_the_environment_variable_wins(self, tmp_path):
        chosen = tmp_path / "chosen"
        (chosen / "channels").mkdir(parents=True)
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={"YT_SHORTS_DATA": str(chosen)}, home=home,
                         repo_channels=repo)

        assert result.root == chosen
        assert result.channels_dir == chosen / "channels"
        assert result.origin == "YT_SHORTS_DATA"

    def test_the_default_directory_is_used_when_it_exists(self, tmp_path):
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={}, home=home, repo_channels=repo)

        assert result.root == home / DEFAULT_DIR_NAME
        assert result.origin == "default"

    def test_the_repository_is_the_last_resort(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={}, home=home, repo_channels=repo)

        assert result.channels_dir == repo
        assert result.root == repo.parent
        assert result.origin == "repository"


class TestErrors:
    def test_a_set_but_missing_path_is_an_error_not_a_fallback(self, tmp_path):
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)
        missing = tmp_path / "nope"

        with pytest.raises(WorkspaceError) as error:
            resolve(env={"YT_SHORTS_DATA": str(missing)}, home=home,
                    repo_channels=repo)

        assert str(missing) in str(error.value)

    def test_an_empty_environment_variable_is_treated_as_unset(self, tmp_path):
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={"YT_SHORTS_DATA": ""}, home=home, repo_channels=repo)

        assert result.origin == "default"


class TestDescription:
    def test_the_workspace_describes_itself_for_the_startup_line(self, tmp_path):
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={}, home=home, repo_channels=repo)

        assert str(result.root) in result.describe()
        assert "default" in result.describe()


class TestF1ErrorMessageForFile:
    """F1: Error message distinguishes between missing and wrong type."""
    def test_env_var_pointing_at_file_says_exists_but_is_file(self, tmp_path):
        """YT_SHORTS_DATA pointing at a file should say so, not 'does not exist'."""
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)
        file_path = tmp_path / "data_file"
        file_path.write_text("not a directory")

        with pytest.raises(WorkspaceError) as error:
            resolve(env={"YT_SHORTS_DATA": str(file_path)}, home=home,
                    repo_channels=repo)

        error_msg = str(error.value)
        assert str(file_path) in error_msg
        # The error should NOT say "does not exist" but should clarify it's a file
        assert "does not exist" not in error_msg
        assert ("file" in error_msg.lower() or "not a directory" in error_msg.lower())


class TestF2RelativePathMadeAbsolute:
    """F2: Relative YT_SHORTS_DATA is resolved to absolute path."""
    def test_relative_env_var_becomes_absolute(self, tmp_path, monkeypatch):
        """I10: the previous version of this test passed an
        ALREADY-absolute path (tmp_path / "abs_data"), so
        result.root.is_absolute() was trivially true whether or not
        workspace.py's ``root = root.resolve()`` line existed at all -
        removing that line left the full suite green, a pin that pinned
        nothing. This uses a genuinely relative value instead. A relative
        Path resolves against the process's real current directory, so
        this chdirs into tmp_path first, and checks the actual value
        produced, not merely that it happens to already be absolute.

        Verified by temporarily deleting workspace.py's `root =
        root.resolve()` line: this test then fails (result.root stays the
        relative Path("relative_data")), and passes again once the line
        is restored.
        """
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        (tmp_path / "relative_data" / "channels").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        result = resolve(env={"YT_SHORTS_DATA": "relative_data"}, home=home,
                         repo_channels=repo)

        assert result.root.is_absolute()
        assert result.root == tmp_path / "relative_data"


class TestF2TildeExpansion:
    """F2: Tilde expansion still works for YT_SHORTS_DATA."""
    def test_tilde_in_env_var_is_expanded(self, tmp_path):
        """~/ in YT_SHORTS_DATA should be expanded to home directory."""
        # This uses actual home parameter, not the machine's home
        custom_home = tmp_path / "custom_home"
        data_dir = custom_home / "my_data"
        (data_dir / "channels").mkdir(parents=True)

        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={"YT_SHORTS_DATA": "~/my_data"}, home=custom_home,
                         repo_channels=repo)

        assert result.root == data_dir


class TestF3DefaultFileHandling:
    """F3: Default directory being a file is not silently ignored."""
    def test_default_dir_being_file_raises_error(self, tmp_path):
        """If ~/YT-Shorts-Data is a file, it should raise an error."""
        home = tmp_path / "home"
        home.mkdir()
        # Create YT-Shorts-Data as a file instead of directory
        default_path = home / DEFAULT_DIR_NAME
        default_path.write_text("not a directory")
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        with pytest.raises(WorkspaceError) as error:
            resolve(env={}, home=home, repo_channels=repo)

        error_msg = str(error.value)
        # The error should mention "file" and either the relative or absolute path
        assert "file" in error_msg.lower() or "not a directory" in error_msg.lower()
        assert DEFAULT_DIR_NAME in error_msg or str(default_path) in error_msg
# F4's "expand user works in all resolution paths" is already covered:
# the tilde-expansion path itself by TestF2TildeExpansion above, and the
# no-env-var default-directory path by
# TestResolutionOrder::test_the_default_directory_is_used_when_it_exists.
# This trailing class duplicated the latter under a misleading name (it
# used env={} and contained no tilde at all) and added nothing.


class TestTask4ConfigAsResolutionSource:
    def test_resolve_uses_config_current_when_set(self, tmp_path):
        data = tmp_path / "data"
        (data / "channels").mkdir(parents=True)
        config_home = tmp_path / "cfg"
        workspaces.write_config(config_home, {"current": str(data), "recent": [str(data)]})
        ws = resolve(env={}, home=tmp_path / "home", config_home=config_home)
        assert ws.root == data
        assert ws.origin == "config"

    def test_env_overrides_config(self, tmp_path):
        envdir = tmp_path / "envdata"
        (envdir / "channels").mkdir(parents=True)
        cfgdir = tmp_path / "cfgdata"
        (cfgdir / "channels").mkdir(parents=True)
        config_home = tmp_path / "cfg"
        workspaces.write_config(config_home, {"current": str(cfgdir), "recent": []})
        ws = resolve(env={"YT_SHORTS_DATA": str(envdir)}, home=tmp_path,
                     config_home=config_home)
        assert ws.root == envdir and ws.origin == "YT_SHORTS_DATA"

    def test_config_missing_falls_through_to_default(self, tmp_path):
        home = tmp_path / "home"
        (home / "YT-Shorts-Data" / "channels").mkdir(parents=True)
        ws = resolve(env={}, home=home, config_home=tmp_path / "empty-cfg")
        assert ws.origin == "default"


class TestSettingsFile:
    """`<workspace>/settings.json` - the job queue's pool limits today.

    Two properties, and neither had a test until a review mutated both and
    watched the suite pass: the write is ATOMIC (this repo pins the same
    mechanic for `job_queue.save` and `render.compose` and pinned nothing
    here), and the read DEGRADES rather than raising, because it runs on the
    path that builds the studio's queue at startup - a stray character in a
    hand-editable file must not be the reason the studio has no queue at all.
    """

    def test_an_absent_file_reads_as_no_settings(self, tmp_path):
        assert workspace_module.read_settings(tmp_path) == {}
        # …and reading did not create it: an absent settings.json is the
        # ordinary state, not something to materialise on first look.
        assert not workspace_module.settings_path(tmp_path).exists()

    def test_what_was_written_is_what_is_read_back(self, tmp_path):
        workspace_module.write_settings(tmp_path, {"limits": {"cpu": 4}})
        assert workspace_module.read_settings(tmp_path) == {"limits": {"cpu": 4}}

    def test_the_file_is_written_aside_and_replaced(self, tmp_path, monkeypatch):
        # Exactly tests/test_job_queue.py's
        # TestPersistence.test_the_file_is_written_aside_and_replaced, one
        # file over: a crash BETWEEN writing the scratch sibling and moving
        # it into place must leave the last complete file, never a partial
        # one. Replacing the body with a plain path.write_text passes every
        # other test in this repo, which is why this one exists.
        path = workspace_module.settings_path(tmp_path)
        workspace_module.write_settings(tmp_path, {"limits": {"cpu": 1}})
        good = path.read_text(encoding="utf-8")

        def boom(_src, _dst):
            raise OSError("simulated crash between write and replace")

        monkeypatch.setattr(workspace_module.os, "replace", boom)
        with pytest.raises(OSError):
            workspace_module.write_settings(tmp_path, {"limits": {"cpu": 9}})

        assert path.read_text(encoding="utf-8") == good
        assert json.loads(path.read_text(encoding="utf-8")) == {"limits": {"cpu": 1}}
        # The scratch sibling is what caught the crash, so it is still there;
        # what matters is that it is NOT the file anything reads.
        assert workspace_module.read_settings(tmp_path) == {"limits": {"cpu": 1}}

    @pytest.mark.parametrize("raw", [
        "{ not json at all",              # a hand edit that lost a brace
        "[1, 2, 3]",                      # valid JSON, wrong top-level shape
        '"just a string"',
        "null",
        "",                               # truncated to nothing by a crash
    ])
    def test_an_unusable_file_reads_as_no_settings(self, tmp_path, raw):
        workspace_module.settings_path(tmp_path).write_text(raw, encoding="utf-8")
        assert workspace_module.read_settings(tmp_path) == {}

    def test_an_unreadable_file_reads_as_no_settings(self, tmp_path):
        # A directory where the file should be: read_text raises OSError,
        # not ValueError, and that path must degrade the same way.
        workspace_module.settings_path(tmp_path).mkdir()
        assert workspace_module.read_settings(tmp_path) == {}
